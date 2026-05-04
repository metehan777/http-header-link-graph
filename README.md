# http-header — publishing a site's link graph and heading map in HTTP response headers

> Companion code for the post: ["I crawled 65,000 pages of my own site without parsing a single line of HTML"](https://metehan.ai/blog/header-link-graph](https://metehan.ai/blog/http-headers-internal-links) (the idea was sketched at SEO Week 2026, NYC, organized by iPullRank).

This repo is a working experiment in publishing a page's structural metadata — its outbound internal links and its heading hierarchy — directly inside HTTP response headers, so crawlers, agents, and your own SEO tooling can read them without parsing any HTML.

A demo site (`data.stateglobe.com`, ~65k pages) emits two custom headers on every page:

```http
X-Internal-Links: <base64url(JSON array of relative paths)>
X-Internal-Links-Encoding: json+base64url
X-Internal-Links-Count: 31
X-Internal-Links-Bytes: 1455

X-Headings: <base64url(JSON array of {l: 1-6, t: string})>
X-Headings-Encoding: json+base64url
X-Headings-Count: 8
X-Headings-Bytes: 534
X-Headings-Schema: [{l:1-6,t:string}]

Access-Control-Expose-Headers: X-Internal-Links, X-Internal-Links-Encoding,
  X-Internal-Links-Count, X-Internal-Links-Bytes, X-Headings,
  X-Headings-Encoding, X-Headings-Count, X-Headings-Bytes, X-Headings-Schema
```

Then a Rust crawler walks the entire graph in seconds without parsing one byte of HTML.

## What's in this repo

```
src/
  headers.ts             ⭐ Drop-in TS module: attachStructuralHeaders()
                            Enforces a combined byte budget (default 12 KB)
                            and gracefully truncates so your origin never 500s.
                            Pure, framework-agnostic, no runtime deps.
  index.ts               Cloudflare Worker reference implementation that uses it
rust-probe/              Rust crawler that reads only response headers (reqwest + tokio)
scripts/
  probe_100.py           100-URL targeted probe; captures BOTH X-Internal-Links + X-Headings
  seo_header_probe.py    Python asyncio header-only crawler (raw sockets)
  seo_header_probe_fast.py httpx + HTTP/2 + sitemap-seeded crawler
  seo_insights.py        Builds SEO insights from a crawl summary (hubs, orphans,
                         click depth, clusters, payload risk, equity Gini)
  render_link_graph.py   Force-directed D3 graph visualization
  test-headers-budget.mjs Stress test for the budget cap (5,000 links + headings)
reports/probe-100/       Sample 100-URL fresh-cache probe output
blog/                    Long-form post about the experiment
wrangler.jsonc           Cloudflare Worker config
```

## The drop-in module

If you only want one thing from this repo, take this:

```ts
import { attachStructuralHeaders } from "./src/headers";

return attachStructuralHeaders(
  new Response(html, { status: 200 }),
  {
    url: req.url,
    links: getInternalLinks(page),  // can be huge, will be safely capped
    headings: getHeadings(page),    // can be huge, will be safely capped
  }
  // Defaults: 6 KB per header, 12 KB combined.
  // Truncated payloads emit X-Internal-Links-Truncated: 1 + X-Internal-Links-Original: N
  // for monitoring.
);
```

It works in **Cloudflare Workers, Next.js middleware, Deno, Bun, Node 18+** — anywhere a `Response` and `TextEncoder` exist.

Verify it never overflows:

```bash
npm run test:budget
# 5 passed, 0 failed
```

## Quick start

```bash
# 1. install Worker deps and run locally
npm install
npm run dev

# 2. quick local sanity check — you should see X-Internal-Links populated
curl -sI http://127.0.0.1:8787/ | grep -i x-internal

# 3. deploy to Cloudflare (uses your wrangler login)
npm run deploy

# 4. run a 100-URL header probe against the deployed site
python3 -m pip install 'httpx[http2]'
python3 scripts/probe_100.py \
  --base-url https://your-domain.example.com \
  --count 100 \
  --concurrency 16 \
  --out-dir reports/probe-100
```

## Building and running the Rust crawler

```bash
cd rust-probe
cargo build --release
./target/release/header-probe \
  --base-url https://your-domain.example.com \
  --requests 70000 \
  --concurrency 800 \
  --timeout 30 \
  --out-dir reports/full-run
```

It seeds the queue from `/sitemap.xml`, makes a single GET per URL, and reads only the `X-Internal-Links` header. On the demo site (`data.stateglobe.com`, 65k pages) the warm-cache run completes in **1m 39s at ~660 req/s** (peaks ~970 req/s).

## Generating SEO insights from a crawl

After a crawl writes `seo-header-summary.json`, run:

```bash
python3 scripts/seo_insights.py \
  --input reports/full-run/seo-header-summary.json \
  --out-dir reports/full-run/insights
```

You get:

- `seo-insights.md` (human-readable: hubs, orphans, dead-ends, click depth, clusters, equity Gini, payload risk, anomalies)
- `seo-insights.json` (machine-readable)
- `recrawl-list.txt` (URLs whose header was missing this run — re-crawl these to clean the dataset)

## Production warning — read this before shipping it

**This is an experiment.** If you do this wrong, you can break your own site. Specifically:

1. **HTTP response header size limits are real and vary by server / CDN.** The combined size of all response headers must fit under your origin's limit (Cloudflare default ~16 KB, many origins enforce 8 KB or less). If you push too much JSON into too many custom headers, the origin will return a `5xx` to real users, not just to crawlers.
2. **High-link or deep-heading hub pages are the danger zone.** A homepage with 200+ links and a long heading map can easily blow past 16 KB. Test every hub.
3. **Always cap the payload defensively.** Implement a hard byte limit (e.g. 6 KB per header, 12 KB combined) and gracefully truncate or omit the header when over budget. Better to ship 50 of 200 links than to 500 the page.
4. **Cache it at the edge.** The first crawl will hit your Worker for every URL (slow). Cache the response with `caches.default.put` and a sane `Cache-Control`, then purge once when the header shape changes.
5. **Do not roll this out without your dev team.** Especially in enterprise. This touches your CDN config, your origin response-header budget, and your bot-handling rules. Coordinate with platform/SRE and SEO together. Run it on a small subset of pages first, monitor 5xx rates, and roll forward only after a clean staging run.
6. **Scope this to sites you own.** It's a publishing technique for site owners, not a bypass tool for someone else's WAF.

## License

MIT
