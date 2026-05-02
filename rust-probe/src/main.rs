use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::{anyhow, Context, Result};
use base64::Engine;
use clap::Parser;
use parking_lot::Mutex;
use regex::Regex;
use reqwest::header::{HeaderMap, HeaderValue, ACCEPT, USER_AGENT};
use reqwest::{Client, StatusCode};
use serde::Serialize;
use serde_json::Value;
use tokio::sync::Semaphore;
use url::Url;

#[derive(Parser, Debug)]
#[command(about = "Fast Rust SEO header probe (HTTP/2 + sitemap seed).")]
struct Args {
    #[arg(long, default_value = "https://data.stateglobe.com")]
    base_url: String,

    #[arg(long, default_value_t = 70_000)]
    requests: usize,

    #[arg(long, default_value_t = 800)]
    concurrency: usize,

    #[arg(long, default_value_t = 25)]
    timeout: u64,

    #[arg(long, default_value = "HeaderProbeRust/0.1 (+https://data.stateglobe.com)")]
    user_agent: String,

    #[arg(long, default_value = "reports/data-stateglobe-rust")]
    out_dir: PathBuf,

    #[arg(long, default_value_t = 2.0)]
    progress_interval: f64,

    #[arg(long, default_value_t = false)]
    cache_bust: bool,

    #[arg(long, default_value_t = true)]
    use_sitemap: bool,

    #[arg(long, default_value_t = 4)]
    max_attempts: u32,
}

#[derive(Default)]
struct UrlRecord {
    hits: u32,
    statuses: BTreeMap<u16, u32>,
    errors: BTreeMap<String, u32>,
    header_present: u32,
    decoded_ok: u32,
    decoded_failed: u32,
    max_header_bytes: u32,
    total_elapsed_ms: f64,
    links: BTreeSet<String>,
}

#[derive(Default)]
struct State {
    queue: Mutex<std::collections::VecDeque<String>>,
    crawl_seen: Mutex<HashSet<String>>,
    discovery_seen: Mutex<HashSet<String>>,
    stats: Mutex<HashMap<String, UrlRecord>>,
    statuses: Mutex<BTreeMap<u16, u32>>,
    errors: Mutex<BTreeMap<String, u32>>,
    decode_errors: Mutex<BTreeMap<String, u32>>,
    payload_shapes: Mutex<BTreeMap<String, u32>>,
    counters: Mutex<Counters>,
}

#[derive(Default, Debug, Clone, Copy)]
struct Counters {
    completed: usize,
    scheduled: usize,
    in_flight: usize,
}

const HEADER_NAME: &str = "x-internal-links";

#[tokio::main(flavor = "multi_thread")]
async fn main() -> Result<()> {
    tracing_subscriber::fmt().with_target(false).init();
    let args = Args::parse();

    let base = Url::parse(&args.base_url)
        .or_else(|_| Url::parse(&format!("https://{}", args.base_url)))
        .context("invalid base url")?;
    let base_root = format!("{}://{}/", base.scheme(), base.host_str().unwrap_or(""));
    let base_root_url = Url::parse(&base_root)?;

    let mut headers = HeaderMap::new();
    headers.insert(USER_AGENT, HeaderValue::from_str(&args.user_agent)?);
    headers.insert(ACCEPT, HeaderValue::from_static("*/*"));

    let client = Client::builder()
        .http2_prior_knowledge()
        .pool_max_idle_per_host(args.concurrency.min(2048))
        .timeout(Duration::from_secs(args.timeout))
        .connect_timeout(Duration::from_secs(15))
        .default_headers(headers)
        .gzip(true)
        .brotli(true)
        .redirect(reqwest::redirect::Policy::none())
        .build()?;

    let state = Arc::new(State::default());

    if args.use_sitemap {
        let seeded = seed_from_sitemap(&client, &base_root_url, args.timeout).await?;
        println!("sitemap_seeded={}", seeded.len());
        let mut q = state.queue.lock();
        let mut crawl = state.crawl_seen.lock();
        let mut disc = state.discovery_seen.lock();
        for path in seeded {
            if crawl.insert(path.clone()) {
                disc.insert(path.clone());
                q.push_back(path);
            }
        }
    }

    if state.queue.lock().is_empty() {
        let mut q = state.queue.lock();
        let mut crawl = state.crawl_seen.lock();
        let mut disc = state.discovery_seen.lock();
        if crawl.insert("/".to_string()) {
            disc.insert("/".to_string());
            q.push_back("/".to_string());
        }
    }

    let started = Instant::now();
    let semaphore = Arc::new(Semaphore::new(args.concurrency));
    let progress_state = Arc::clone(&state);
    let progress_handle = tokio::spawn(progress_loop(progress_state, started, args.progress_interval));

    let mut handles = Vec::new();
    loop {
        let path = {
            let mut q = state.queue.lock();
            let mut counters = state.counters.lock();
            if counters.scheduled >= args.requests {
                None
            } else if let Some(p) = q.pop_front() {
                counters.scheduled += 1;
                counters.in_flight += 1;
                Some(p)
            } else if counters.in_flight == 0 {
                None
            } else {
                Some(String::new())
            }
        };

        match path {
            None => break,
            Some(p) if p.is_empty() => {
                tokio::time::sleep(Duration::from_millis(20)).await;
                continue;
            }
            Some(p) => {
                let permit = Arc::clone(&semaphore).acquire_owned().await?;
                let st = Arc::clone(&state);
                let cl = client.clone();
                let base = base_root_url.clone();
                let cache_bust = args.cache_bust;
                let max_attempts = args.max_attempts;
                let max_requests = args.requests;
                handles.push(tokio::spawn(async move {
                    let _permit = permit;
                    handle_path(st, cl, base, p, cache_bust, max_attempts, max_requests).await;
                }));
            }
        }
    }

    for handle in handles {
        let _ = handle.await;
    }

    progress_handle.abort();
    let elapsed = started.elapsed();
    write_reports(&state, &base_root, &args.out_dir, elapsed.as_secs_f64())?;
    print_summary(&state, &base_root, &args.out_dir, elapsed.as_secs_f64());

    Ok(())
}

async fn progress_loop(state: Arc<State>, started: Instant, interval: f64) {
    let interval = Duration::from_secs_f64(interval.max(0.1));
    loop {
        tokio::time::sleep(interval).await;
        let counters = *state.counters.lock();
        let queued = state.queue.lock().len();
        let discovered = state.discovery_seen.lock().len();
        let crawl_unique = state.crawl_seen.lock().len();
        let statuses = state.statuses.lock().clone();
        let errors = state.errors.lock().clone();
        let decode_errors = state.decode_errors.lock().clone();
        let elapsed = started.elapsed().as_secs_f64().max(0.001);
        let rps = counters.completed as f64 / elapsed;
        println!(
            "progress fetched={} scheduled={} in_flight={} queued={} discovered={} crawl_unique={} rps={:.2} statuses={:?} errors={:?} decode_errors={:?}",
            counters.completed, counters.scheduled, counters.in_flight, queued,
            discovered, crawl_unique, rps, statuses, errors, decode_errors
        );
    }
}

async fn handle_path(
    state: Arc<State>,
    client: Client,
    base: Url,
    path: String,
    cache_bust: bool,
    max_attempts: u32,
    max_requests: usize,
) {
    let mut last_status: Option<StatusCode> = None;
    let mut last_error: Option<String> = None;
    let mut header_value: Option<String> = None;
    let mut elapsed_ms: f64 = 0.0;

    for attempt in 1..=max_attempts {
        let request_path = if cache_bust {
            let sep = if path.contains('?') { '&' } else { '?' };
            format!("{}{}__hp={}", path, sep, std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0))
        } else {
            path.clone()
        };

        let target = base.join(&request_path).unwrap_or_else(|_| base.clone());
        let started = Instant::now();
        let result = client.get(target.clone()).send().await;
        elapsed_ms = started.elapsed().as_secs_f64() * 1000.0;

        match result {
            Ok(resp) => {
                last_status = Some(resp.status());
                if let Some(value) = resp.headers().get(HEADER_NAME) {
                    if let Ok(s) = value.to_str() {
                        header_value = Some(s.to_string());
                    }
                }
                let _ = resp.bytes().await;
                if resp_is_retryable(last_status) && attempt < max_attempts {
                    tokio::time::sleep(retry_delay(attempt)).await;
                    continue;
                }
                last_error = None;
                break;
            }
            Err(err) => {
                last_error = Some(error_kind(&err));
                if attempt < max_attempts {
                    tokio::time::sleep(retry_delay(attempt)).await;
                    continue;
                }
                break;
            }
        }
    }

    let mut decoded_value: Option<Value> = None;
    let mut decoded_error: Option<String> = None;
    let mut shape: Option<String> = None;
    let mut links: Vec<String> = Vec::new();

    if let Some(ref h) = header_value {
        match decode_header(h) {
            Ok(value) => {
                let (shape_name, raw) = extract_links(&value);
                shape = Some(shape_name.to_string());
                links = normalize_links(&base, raw);
                decoded_value = Some(value);
            }
            Err(name) => {
                decoded_error = Some(name);
            }
        }
    } else if last_error.is_none() && last_status.is_some() {
        decoded_error = Some("missing_header".to_string());
    }

    let mut record_path = path.clone();
    if let Ok(parsed) = base.join(&path) {
        if let Some(p) = Some(parsed.path().to_string()) {
            if !p.is_empty() {
                record_path = p;
            }
        }
    }

    {
        let mut counters = state.counters.lock();
        counters.completed += 1;
        counters.in_flight = counters.in_flight.saturating_sub(1);
    }

    {
        let mut stats = state.stats.lock();
        let entry = stats.entry(record_path.clone()).or_default();
        entry.hits += 1;
        entry.total_elapsed_ms += elapsed_ms;
        if let Some(s) = last_status {
            *entry.statuses.entry(s.as_u16()).or_insert(0) += 1;
            *state.statuses.lock().entry(s.as_u16()).or_insert(0) += 1;
        }
        if let Some(err) = last_error.as_ref() {
            *entry.errors.entry(err.clone()).or_insert(0) += 1;
            *state.errors.lock().entry(err.clone()).or_insert(0) += 1;
        }
        if let Some(ref h) = header_value {
            entry.header_present += 1;
            entry.max_header_bytes = entry.max_header_bytes.max(h.len() as u32);
        }
        if let Some(shape_name) = shape.as_ref() {
            *state.payload_shapes.lock().entry(shape_name.clone()).or_insert(0) += 1;
        }
        if decoded_value.is_some() {
            entry.decoded_ok += 1;
        }
        if let Some(err) = decoded_error.as_ref() {
            entry.decoded_failed += 1;
            *state.decode_errors.lock().entry(err.clone()).or_insert(0) += 1;
        }
        for link in &links {
            entry.links.insert(link.clone());
        }
    }

    {
        let mut discovery = state.discovery_seen.lock();
        let mut crawl = state.crawl_seen.lock();
        let mut queue = state.queue.lock();
        for link in links {
            discovery.insert(link.clone());
            if crawl.len() < max_requests && crawl.insert(link.clone()) {
                queue.push_back(link);
            }
        }
    }
}

fn resp_is_retryable(status: Option<StatusCode>) -> bool {
    matches!(status.map(|s| s.as_u16()), Some(s) if s >= 500)
}

fn retry_delay(attempt: u32) -> Duration {
    let base = 250u64;
    let ms = base.saturating_mul(2u64.saturating_pow(attempt.saturating_sub(1)));
    Duration::from_millis(ms.min(5_000))
}

fn error_kind(err: &reqwest::Error) -> String {
    if err.is_timeout() {
        "Timeout".into()
    } else if err.is_connect() {
        "Connect".into()
    } else if err.is_request() {
        "Request".into()
    } else if err.is_decode() {
        "Decode".into()
    } else {
        "Other".into()
    }
}

fn decode_header(value: &str) -> std::result::Result<Value, String> {
    let mut padded = value.to_string();
    while padded.len() % 4 != 0 {
        padded.push('=');
    }
    let bytes = base64::engine::general_purpose::URL_SAFE
        .decode(&padded)
        .or_else(|_| base64::engine::general_purpose::URL_SAFE_NO_PAD.decode(value))
        .map_err(|e| e.to_string())?;
    serde_json::from_slice::<Value>(&bytes).map_err(|e| e.to_string())
}

fn extract_links(payload: &Value) -> (&'static str, Vec<&Value>) {
    if let Value::Array(arr) = payload {
        return ("array", arr.iter().collect());
    }
    if let Value::Object(map) = payload {
        if let Some(Value::Array(arr)) = map.get("links") {
            return ("object", arr.iter().collect());
        }
    }
    ("unknown", Vec::new())
}

fn normalize_links(base: &Url, raw: Vec<&Value>) -> Vec<String> {
    let host = base.host_str().unwrap_or("");
    let mut out = BTreeSet::new();

    for item in raw {
        let s = match item {
            Value::String(s) if !s.is_empty() => s.as_str(),
            _ => continue,
        };

        let absolute = match base.join(s) {
            Ok(u) => u,
            Err(_) => continue,
        };

        if absolute.host_str().unwrap_or("") != host {
            continue;
        }

        let mut path = absolute.path().to_string();
        if let Some(q) = absolute.query() {
            path.push('?');
            path.push_str(q);
        }
        if path.is_empty() {
            path = "/".to_string();
        }
        out.insert(path);
    }

    out.into_iter().collect()
}

async fn seed_from_sitemap(client: &Client, base: &Url, timeout_s: u64) -> Result<Vec<String>> {
    let loc_re = Regex::new(r"(?i)<loc>([^<]+)</loc>").unwrap();
    let mut queue: Vec<Url> = vec![base.join("/sitemap.xml")?];
    let mut seen: BTreeSet<String> = BTreeSet::new();

    let mut iter = 0usize;
    while let Some(url) = queue.pop() {
        iter += 1;
        if iter > 50 {
            break;
        }
        let resp = match client
            .get(url.clone())
            .timeout(Duration::from_secs(timeout_s))
            .send()
            .await
        {
            Ok(r) if r.status().is_success() => r,
            _ => continue,
        };
        let text = match resp.text().await {
            Ok(t) => t,
            Err(_) => continue,
        };

        for cap in loc_re.captures_iter(&text) {
            if let Some(loc) = cap.get(1) {
                let loc = loc.as_str().trim();
                if loc.to_lowercase().contains("sitemap") && loc.to_lowercase().ends_with(".xml") {
                    if let Ok(u) = Url::parse(loc) {
                        queue.push(u);
                    }
                    continue;
                }
                if let Ok(u) = Url::parse(loc) {
                    if u.host_str().unwrap_or("") != base.host_str().unwrap_or("") {
                        continue;
                    }
                    let mut p = u.path().to_string();
                    if let Some(q) = u.query() {
                        p.push('?');
                        p.push_str(q);
                    }
                    if p.is_empty() {
                        p = "/".to_string();
                    }
                    seen.insert(p);
                }
            }
        }
    }

    Ok(seen.into_iter().collect())
}

#[derive(Serialize)]
struct UrlSummary {
    url: String,
    hits: u32,
    statuses: BTreeMap<u16, u32>,
    headerPresent: u32,
    decodedOk: u32,
    decodedFailed: u32,
    maxHeaderBytes: u32,
    avgElapsedMs: f64,
    uniqueLinks: usize,
    links: Vec<String>,
    errors: BTreeMap<String, u32>,
}

#[derive(Serialize)]
struct Summary {
    baseUrl: String,
    requestsCompleted: usize,
    elapsedSeconds: f64,
    elapsedHuman: String,
    requestsPerSecond: f64,
    totalDiscoveredUrls: usize,
    actualUniquePagesCrawled: usize,
    statusCounts: BTreeMap<u16, u32>,
    networkErrors: BTreeMap<String, u32>,
    decodeErrors: BTreeMap<String, u32>,
    payloadShapes: BTreeMap<String, u32>,
    estimatedTimeFor65000PagesAtCurrentRps: Option<String>,
    estimatedTimeFor66000PagesAtCurrentRps: Option<String>,
    urls: Vec<UrlSummary>,
}

fn write_reports(state: &State, base_url: &str, out_dir: &PathBuf, elapsed_s: f64) -> Result<()> {
    fs::create_dir_all(out_dir).context("create out dir")?;

    let stats = state.stats.lock();
    let mut rows: Vec<(String, &UrlRecord)> = stats.iter().map(|(k, v)| (k.clone(), v)).collect();
    rows.sort_by(|a, b| a.0.cmp(&b.0));

    let csv_path = out_dir.join("seo-header-report.csv");
    let mut wtr = csv::Writer::from_path(&csv_path)?;
    wtr.write_record([
        "url",
        "hits",
        "statuses",
        "header_present",
        "decoded_ok",
        "decoded_failed",
        "max_header_bytes",
        "avg_elapsed_ms",
        "unique_links",
        "links",
        "errors",
    ])?;

    for (path, rec) in &rows {
        let avg = if rec.hits > 0 {
            rec.total_elapsed_ms / rec.hits as f64
        } else {
            0.0
        };
        let url = format!("{}{}", base_url.trim_end_matches('/'), path);
        wtr.write_record(&[
            url,
            rec.hits.to_string(),
            serde_json::to_string(&rec.statuses).unwrap_or_default(),
            rec.header_present.to_string(),
            rec.decoded_ok.to_string(),
            rec.decoded_failed.to_string(),
            rec.max_header_bytes.to_string(),
            format!("{:.2}", avg),
            rec.links.len().to_string(),
            rec.links.iter().cloned().collect::<Vec<_>>().join(" "),
            serde_json::to_string(&rec.errors).unwrap_or_default(),
        ])?;
    }
    wtr.flush()?;

    let counters = *state.counters.lock();
    let rps = if elapsed_s > 0.0 {
        counters.completed as f64 / elapsed_s
    } else {
        0.0
    };

    let summary = Summary {
        baseUrl: base_url.to_string(),
        requestsCompleted: counters.completed,
        elapsedSeconds: round2(elapsed_s),
        elapsedHuman: format_duration(elapsed_s),
        requestsPerSecond: round2(rps),
        totalDiscoveredUrls: state.discovery_seen.lock().len(),
        actualUniquePagesCrawled: stats.len(),
        statusCounts: state.statuses.lock().clone(),
        networkErrors: state.errors.lock().clone(),
        decodeErrors: state.decode_errors.lock().clone(),
        payloadShapes: state.payload_shapes.lock().clone(),
        estimatedTimeFor65000PagesAtCurrentRps: if rps > 0.0 {
            Some(format_duration(65000.0 / rps))
        } else {
            None
        },
        estimatedTimeFor66000PagesAtCurrentRps: if rps > 0.0 {
            Some(format_duration(66000.0 / rps))
        } else {
            None
        },
        urls: rows
            .iter()
            .map(|(path, rec)| UrlSummary {
                url: format!("{}{}", base_url.trim_end_matches('/'), path),
                hits: rec.hits,
                statuses: rec.statuses.clone(),
                headerPresent: rec.header_present,
                decodedOk: rec.decoded_ok,
                decodedFailed: rec.decoded_failed,
                maxHeaderBytes: rec.max_header_bytes,
                avgElapsedMs: round2(if rec.hits > 0 {
                    rec.total_elapsed_ms / rec.hits as f64
                } else {
                    0.0
                }),
                uniqueLinks: rec.links.len(),
                links: rec.links.iter().cloned().collect(),
                errors: rec.errors.clone(),
            })
            .collect(),
    };

    let json_path = out_dir.join("seo-header-summary.json");
    fs::write(&json_path, serde_json::to_string_pretty(&summary)? + "\n")?;

    Ok(())
}

fn print_summary(state: &State, base_url: &str, out_dir: &PathBuf, elapsed_s: f64) {
    let counters = *state.counters.lock();
    let stats = state.stats.lock();
    let rps = if elapsed_s > 0.0 {
        counters.completed as f64 / elapsed_s
    } else {
        0.0
    };
    println!("base_url={}", base_url);
    println!("requests={}/{}", counters.completed, counters.scheduled);
    println!("elapsed_s={:.3}", elapsed_s);
    println!("elapsed_human={}", format_duration(elapsed_s));
    println!("rps={:.2}", rps);
    if rps > 0.0 {
        println!(
            "estimated_time_for_65000_pages_at_current_rps={}",
            format_duration(65000.0 / rps)
        );
        println!(
            "estimated_time_for_66000_pages_at_current_rps={}",
            format_duration(66000.0 / rps)
        );
    }
    println!("total_discovered_urls={}", state.discovery_seen.lock().len());
    println!("actual_unique_pages_crawled={}", stats.len());
    println!("status_counts={:?}", state.statuses.lock());
    println!("network_errors={:?}", state.errors.lock());
    println!("decode_errors={:?}", state.decode_errors.lock());
    println!("payload_shapes={:?}", state.payload_shapes.lock());
    println!("csv={:?}", out_dir.join("seo-header-report.csv"));
    println!("json={:?}", out_dir.join("seo-header-summary.json"));
}

fn format_duration(seconds: f64) -> String {
    let total = seconds.max(0.0).round() as u64;
    let h = total / 3600;
    let m = (total % 3600) / 60;
    let s = total % 60;
    if h > 0 {
        format!("{}h {}m {}s", h, m, s)
    } else if m > 0 {
        format!("{}m {}s", m, s)
    } else {
        format!("{}s", s)
    }
}

fn round2(value: f64) -> f64 {
    (value * 100.0).round() / 100.0
}

#[allow(dead_code)]
fn _unused() -> Result<()> {
    Err(anyhow!("unused"))
}
