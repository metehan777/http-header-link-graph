#!/usr/bin/env python3
"""Targeted 100-URL probe that captures BOTH X-Internal-Links and X-Headings.

Designed for the SEO Week 2026 follow-up experiment. Uses the live
sitemap.xml to pick a representative sample, hits each URL with a unique
cache-bust query string so we measure the Worker (not the edge cache),
decodes the base64url JSON payloads, and writes a compact report.

Usage:
    python3 scripts/probe_100.py \
        --base-url https://data.stateglobe.com \
        --count 100 \
        --concurrency 16 \
        --timeout 25 \
        --out-dir reports/probe-100

Outputs:
    <out_dir>/probe-100-raw.json     per-URL records
    <out_dir>/probe-100-summary.md   human-readable summary
    <out_dir>/probe-100-summary.json machine-readable summary
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import random
import re
import statistics
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

try:
    import httpx
except ImportError:
    raise SystemExit("Install dependency: python3 -m pip install 'httpx[http2]'")

USER_AGENT = "metehan-header-probe/1.0 (+https://metehan.ai)"
SITEMAP_NS_RE = re.compile(r"<loc>(.*?)</loc>", re.IGNORECASE | re.DOTALL)
HEADER_BUDGET = 8 * 1024


def b64url_decode(value: str) -> Optional[Any]:
    if not value:
        return None
    pad = "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(value + pad)
        return json.loads(raw.decode("utf-8"))
    except Exception:
        return None


@dataclass
class Probe:
    url: str
    status: Optional[int] = None
    elapsed_ms: float = 0.0
    error: Optional[str] = None

    links_header_bytes: int = 0
    links_count: Optional[int] = None
    links_decoded_ok: bool = False
    links_sample: list[str] = field(default_factory=list)

    headings_header_bytes: int = 0
    headings_count: Optional[int] = None
    headings_decoded_ok: bool = False
    headings_sample: list[dict] = field(default_factory=list)
    heading_levels: dict[int, int] = field(default_factory=dict)

    cf_ray: Optional[str] = None
    cache_status: Optional[str] = None


async def fetch_sitemap(client: httpx.AsyncClient, base: str) -> list[str]:
    r = await client.get(f"{base.rstrip('/')}/sitemap.xml", timeout=30)
    r.raise_for_status()
    text = r.text
    locs = SITEMAP_NS_RE.findall(text)
    if locs and locs[0].endswith(".xml"):
        # Sitemap index: pick first child sitemap
        child = await client.get(locs[0], timeout=30)
        text = child.text
        locs = SITEMAP_NS_RE.findall(text)
    return [u.strip() for u in locs if u.strip()]


async def probe_url(client: httpx.AsyncClient, url: str) -> Probe:
    cb = f"_cb={int(time.time() * 1000)}_{random.randint(1000, 9999)}"
    sep = "&" if "?" in url else "?"
    target = f"{url}{sep}{cb}"
    rec = Probe(url=url)
    t0 = time.perf_counter()
    try:
        r = await client.get(target, timeout=httpx.Timeout(25.0, connect=10.0))
        rec.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        rec.status = r.status_code
        rec.cf_ray = r.headers.get("cf-ray")
        rec.cache_status = r.headers.get("cf-cache-status")

        links_h = r.headers.get("x-internal-links")
        if links_h:
            rec.links_header_bytes = len(links_h.encode("utf-8"))
            decoded = b64url_decode(links_h)
            if decoded is not None:
                rec.links_decoded_ok = True
                if isinstance(decoded, list):
                    links = decoded
                elif isinstance(decoded, dict) and isinstance(decoded.get("links"), list):
                    links = decoded["links"]
                else:
                    links = []
                rec.links_count = len(links)
                rec.links_sample = links[:5]
            else:
                rec.links_count = None

        headings_h = r.headers.get("x-headings") or r.headers.get("x-page-headings")
        if headings_h:
            rec.headings_header_bytes = len(headings_h.encode("utf-8"))
            decoded = b64url_decode(headings_h)
            if decoded is not None:
                rec.headings_decoded_ok = True
                if isinstance(decoded, list):
                    headings = decoded
                elif isinstance(decoded, dict) and isinstance(decoded.get("headings"), list):
                    headings = decoded["headings"]
                else:
                    headings = []
                rec.headings_count = len(headings)
                rec.headings_sample = headings[:5]
                level_counts: Counter[int] = Counter()
                for h in headings:
                    if isinstance(h, dict):
                        level = h.get("l") or h.get("level")
                        if isinstance(level, int):
                            level_counts[level] += 1
                rec.heading_levels = dict(level_counts)
    except httpx.HTTPError as exc:
        rec.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        rec.error = type(exc).__name__
    except Exception as exc:
        rec.elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        rec.error = f"unexpected:{type(exc).__name__}"
    return rec


def render_summary_md(records: list[Probe], elapsed_s: float, base_url: str) -> str:
    n = len(records)
    statuses = Counter(r.status for r in records)
    errors = Counter(r.error for r in records if r.error)
    cache_states = Counter(r.cache_status for r in records if r.cache_status)

    has_links = [r for r in records if r.links_decoded_ok]
    has_headings = [r for r in records if r.headings_decoded_ok]
    both = [r for r in records if r.links_decoded_ok and r.headings_decoded_ok]
    fives = [r for r in records if r.status and 500 <= r.status < 600]

    link_bytes = [r.links_header_bytes for r in has_links]
    heading_bytes = [r.headings_header_bytes for r in has_headings]
    combined_bytes = [r.links_header_bytes + r.headings_header_bytes for r in both]
    elapsed = [r.elapsed_ms for r in records if r.elapsed_ms]

    link_counts = [r.links_count for r in has_links if r.links_count is not None]
    heading_counts = [r.headings_count for r in has_headings if r.headings_count is not None]

    all_levels: Counter[int] = Counter()
    for r in has_headings:
        for lvl, c in r.heading_levels.items():
            all_levels[lvl] += c

    def pct(p: int, w: int) -> str:
        return f"{(p / w * 100):.1f}%" if w else "-"

    def stats(values: list[float]) -> str:
        if not values:
            return "-"
        values = sorted(values)
        return (
            f"min={min(values):.0f} | "
            f"p50={values[len(values)//2]:.0f} | "
            f"p95={values[int(len(values)*0.95)]:.0f} | "
            f"max={max(values):.0f}"
        )

    L: list[str] = []
    add = L.append
    add(f"# 100-URL header probe — {base_url}")
    add("")
    add(f"_Run: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}_")
    add(f"_Elapsed: {elapsed_s:.2f} s_")
    add(f"_Concurrency cache-busted (one Worker invocation per URL, no edge hits)_")
    add("")
    add("## Crawl health")
    add("")
    add(f"- URLs probed: **{n}**")
    add(f"- Status mix: " + ", ".join(f"`{k}`={v}" for k, v in statuses.most_common()))
    add(f"- 5xx pages: **{len(fives)}** ({pct(len(fives), n)})")
    add(f"- Network errors: **{sum(errors.values())}** {dict(errors)}")
    if cache_states:
        add(f"- `cf-cache-status` distribution: {dict(cache_states)}")
    add(f"- Latency (ms): {stats(elapsed)}")
    add("")
    add("## Header coverage")
    add("")
    add(f"- Pages exposing `X-Internal-Links` (decoded ok): **{len(has_links)}** "
        f"({pct(len(has_links), n)})")
    add(f"- Pages exposing `X-Headings` (decoded ok): **{len(has_headings)}** "
        f"({pct(len(has_headings), n)})")
    add(f"- Pages exposing **both**: **{len(both)}** ({pct(len(both), n)})")
    add("")
    add("## Payload size (bytes)")
    add("")
    add(f"- `X-Internal-Links` header bytes: {stats(link_bytes)}")
    add(f"- `X-Headings` header bytes: {stats(heading_bytes)}")
    add(f"- Combined (both headers, when present): {stats(combined_bytes)}")
    add(f"- 8 KB budget reference: **{HEADER_BUDGET}** B")
    over_budget = [b for b in combined_bytes if b > HEADER_BUDGET]
    add(f"- Pages where combined headers exceed 8 KB: **{len(over_budget)}** "
        f"({pct(len(over_budget), len(combined_bytes))})")
    add("")
    add("## Link payload contents")
    add("")
    add(f"- Pages with link list: **{len(link_counts)}**")
    add(f"- Links per page: {stats([float(c) for c in link_counts])}")
    add(f"- Mean links per page: "
        f"**{statistics.mean(link_counts):.1f}**" if link_counts else "- Mean links: -")
    add("")
    add("## Heading payload contents")
    add("")
    add(f"- Pages with headings: **{len(heading_counts)}**")
    add(f"- Headings per page: {stats([float(c) for c in heading_counts])}")
    add(f"- Mean headings per page: "
        f"**{statistics.mean(heading_counts):.1f}**" if heading_counts else "- Mean: -")
    if all_levels:
        levels_summary = ", ".join(
            f"H{lvl}={all_levels[lvl]}" for lvl in sorted(all_levels)
        )
        add(f"- Heading-level distribution: {levels_summary}")
    add("")
    add("## Sample successful page")
    add("")
    sample = next((r for r in records if r.links_decoded_ok and r.headings_decoded_ok), None)
    if sample:
        add(f"- URL: `{sample.url}`")
        add(f"- HTTP status: `{sample.status}`")
        add(f"- Latency: `{sample.elapsed_ms:.1f} ms`")
        add(f"- Link count: `{sample.links_count}` "
            f"(header bytes: {sample.links_header_bytes})")
        add(f"- Heading count: `{sample.headings_count}` "
            f"(header bytes: {sample.headings_header_bytes})")
        if sample.headings_sample:
            add("- First few headings:")
            for h in sample.headings_sample:
                if isinstance(h, dict):
                    add(f"    - H{h.get('l', '?')}: {h.get('t', '')}")
        if sample.links_sample:
            add(f"- First few links: `{', '.join(sample.links_sample)}`")
    add("")
    add("## Failing pages (first 10)")
    add("")
    failing = [r for r in records if (r.status and r.status >= 400) or r.error]
    if failing:
        for r in failing[:10]:
            add(f"- `{r.url}` — status `{r.status}`, error `{r.error}`")
    else:
        add("- None.")
    add("")
    return "\n".join(L) + "\n"


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--count", type=int, default=100)
    ap.add_argument("--concurrency", type=int, default=16)
    ap.add_argument("--timeout", type=float, default=25.0)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    random.seed(args.seed)

    limits = httpx.Limits(max_connections=args.concurrency * 2,
                          max_keepalive_connections=args.concurrency)
    timeout = httpx.Timeout(args.timeout, connect=10.0)
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html,*/*"}

    async with httpx.AsyncClient(http2=True, limits=limits, timeout=timeout,
                                  headers=headers, follow_redirects=False) as client:
        print(f"[1/3] Fetching sitemap.xml from {args.base_url}", flush=True)
        urls = await fetch_sitemap(client, args.base_url)
        print(f"      sitemap returned {len(urls)} URLs", flush=True)
        if len(urls) > args.count:
            sampled = random.sample(urls, args.count)
        else:
            sampled = urls
        print(f"[2/3] Probing {len(sampled)} URLs (concurrency={args.concurrency})",
              flush=True)
        sem = asyncio.Semaphore(args.concurrency)
        progress = {"done": 0}
        async def bound(url: str) -> Probe:
            async with sem:
                rec = await probe_url(client, url)
                progress["done"] += 1
                if progress["done"] % 10 == 0 or progress["done"] == len(sampled):
                    print(f"      progress: {progress['done']}/{len(sampled)}",
                          flush=True)
                return rec

        t0 = time.perf_counter()
        records = await asyncio.gather(*(bound(u) for u in sampled))
        elapsed = time.perf_counter() - t0

    print(f"[3/3] Writing reports to {out_dir}", flush=True)
    raw_path = out_dir / "probe-100-raw.json"
    md_path = out_dir / "probe-100-summary.md"
    summary_json_path = out_dir / "probe-100-summary.json"

    raw_path.write_text(json.dumps([asdict(r) for r in records], indent=2))
    md = render_summary_md(records, elapsed, args.base_url)
    md_path.write_text(md)

    summary_payload = {
        "baseUrl": args.base_url,
        "count": len(records),
        "elapsedSeconds": round(elapsed, 2),
        "statuses": dict(Counter(r.status for r in records)),
        "errors": dict(Counter(r.error for r in records if r.error)),
        "linksDecoded": sum(1 for r in records if r.links_decoded_ok),
        "headingsDecoded": sum(1 for r in records if r.headings_decoded_ok),
        "both": sum(1 for r in records if r.links_decoded_ok and r.headings_decoded_ok),
        "linkBytesP95": (
            sorted(r.links_header_bytes for r in records if r.links_decoded_ok)[
                int(0.95 * sum(1 for r in records if r.links_decoded_ok))
            ] if any(r.links_decoded_ok for r in records) else 0
        ),
        "headingBytesP95": (
            sorted(r.headings_header_bytes for r in records if r.headings_decoded_ok)[
                int(0.95 * sum(1 for r in records if r.headings_decoded_ok))
            ] if any(r.headings_decoded_ok for r in records) else 0
        ),
    }
    summary_json_path.write_text(json.dumps(summary_payload, indent=2))

    print()
    print("=" * 60)
    print(md)
    print("=" * 60)
    print(f"raw      -> {raw_path}")
    print(f"summary  -> {md_path}")
    print(f"summary  -> {summary_json_path}")


if __name__ == "__main__":
    asyncio.run(main())
