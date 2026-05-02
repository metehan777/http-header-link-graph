#!/usr/bin/env python3
"""
Fast SEO header probe.

Uses httpx + HTTP/2 multiplexing, optional sitemap seeding, and an in-process
worker pool over an httpx.AsyncClient. Multiplexes hundreds of requests over a
small number of TLS connections.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import re
import sys
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx


HEADER_NAME = "x-internal-links"
DEFAULT_USER_AGENT = "StateGlobeHeaderSEOProbe-Fast/0.1 (+https://data.stateglobe.com)"


@dataclass(slots=True)
class FetchResult:
    path: str
    url: str
    status: int | None
    elapsed_ms: float
    header: str | None = None
    decoded: Any | None = None
    decoded_error: str | None = None
    error: str | None = None
    attempts: int = 1


@dataclass(slots=True)
class UrlStats:
    url: str
    hits: int = 0
    statuses: Counter = field(default_factory=Counter)
    errors: Counter = field(default_factory=Counter)
    header_present: int = 0
    decoded_ok: int = 0
    decoded_failed: int = 0
    max_header_bytes: int = 0
    links: set = field(default_factory=set)
    total_elapsed_ms: float = 0.0

    @property
    def avg_elapsed_ms(self) -> float:
        return (self.total_elapsed_ms / self.hits) if self.hits else 0.0


class State:
    def __init__(self, base_url: str, max_requests: int) -> None:
        self.base_url = normalize_base_url(base_url)
        self.host = urlparse(self.base_url).netloc
        self.max_requests = max_requests
        self.queue: deque[str] = deque()
        self.crawl_seen: set[str] = set()
        self.discovery_seen: set[str] = set()
        self.scheduled = 0
        self.completed = 0
        self.in_flight = 0
        self.condition = asyncio.Condition()
        self.stats: dict[str, UrlStats] = {}
        self.statuses: Counter = Counter()
        self.errors: Counter = Counter()
        self.decode_errors: Counter = Counter()
        self.payload_shapes: Counter = Counter()

    def seed(self, paths: list[str]) -> None:
        for path in paths:
            if path in self.crawl_seen:
                continue
            self.crawl_seen.add(path)
            self.discovery_seen.add(path)
            self.queue.append(path)

    async def next_path(self) -> str | None:
        async with self.condition:
            while True:
                if self.scheduled >= self.max_requests:
                    return None
                if self.queue:
                    path = self.queue.popleft()
                    self.scheduled += 1
                    self.in_flight += 1
                    return path
                if self.in_flight == 0:
                    return None
                await self.condition.wait()

    async def record(self, result: FetchResult, links: list[str], shape: str | None) -> None:
        async with self.condition:
            self.completed += 1
            stats = self.stats.setdefault(result.path, UrlStats(url=urljoin(self.base_url, result.path)))
            stats.hits += 1
            stats.total_elapsed_ms += result.elapsed_ms
            if result.status is not None:
                stats.statuses[result.status] += 1
                self.statuses[result.status] += 1
            if result.error:
                stats.errors[result.error] += 1
                self.errors[result.error] += 1
            if result.header:
                stats.header_present += 1
                stats.max_header_bytes = max(stats.max_header_bytes, len(result.header.encode("ascii", errors="ignore")))
            if shape:
                self.payload_shapes[shape] += 1
            if result.decoded is not None:
                stats.decoded_ok += 1
            if result.decoded_error:
                stats.decoded_failed += 1
                self.decode_errors[result.decoded_error] += 1

            for link in links:
                if link not in self.discovery_seen:
                    self.discovery_seen.add(link)
                if link not in self.crawl_seen and self.scheduled + len(self.queue) < self.max_requests:
                    self.crawl_seen.add(link)
                    self.queue.append(link)

            self.in_flight -= 1
            self.condition.notify_all()


async def main() -> None:
    args = parse_args()
    state = State(args.base_url, args.requests)
    started_at = time.perf_counter()

    limits = httpx.Limits(
        max_connections=args.max_connections,
        max_keepalive_connections=args.max_connections,
        keepalive_expiry=30.0,
    )
    timeout = httpx.Timeout(connect=10.0, read=args.timeout, write=10.0, pool=args.timeout)

    async with httpx.AsyncClient(
        http2=True,
        limits=limits,
        timeout=timeout,
        headers={"user-agent": args.user_agent, "accept": "*/*"},
        follow_redirects=False,
    ) as client:

        if args.use_sitemap:
            seeded = await seed_from_sitemap(client, state.base_url, args.timeout)
            print(f"sitemap_seeded={len(seeded)}", flush=True)
            if seeded:
                state.seed(seeded)

        if not state.queue:
            state.seed(["/"])

        progress_task = asyncio.create_task(progress_reporter(state, started_at, args.progress_interval))
        workers = [
            asyncio.create_task(
                worker(
                    state,
                    client,
                    args.cache_bust,
                    args.max_attempts,
                    args.retry_backoff,
                )
            )
            for _ in range(args.concurrency)
        ]
        await asyncio.gather(*workers)
        await progress_task

    elapsed_s = time.perf_counter() - started_at
    write_reports(state, elapsed_s, Path(args.out_dir))
    print_summary(state, elapsed_s, Path(args.out_dir))


SITEMAP_LOC_RE = re.compile(r"<loc>([^<]+)</loc>", re.IGNORECASE)


async def seed_from_sitemap(client: httpx.AsyncClient, base_url: str, timeout: float) -> list[str]:
    seen: set[str] = set()
    queue = [urljoin(base_url, "/sitemap.xml")]

    for _ in range(20):
        if not queue:
            break
        url = queue.pop(0)
        try:
            response = await client.get(url, timeout=timeout)
        except Exception:  # noqa: BLE001
            continue

        if response.status_code != 200:
            continue

        text = response.text
        for match in SITEMAP_LOC_RE.finditer(text):
            loc = match.group(1).strip()
            if loc.endswith(".xml") and "sitemap" in loc.lower():
                queue.append(loc)
                continue
            parsed = urlparse(loc)
            if parsed.netloc and parsed.netloc != urlparse(base_url).netloc:
                continue
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"
            seen.add(path)

    return sorted(seen)


async def worker(
    state: State,
    client: httpx.AsyncClient,
    cache_bust: bool,
    max_attempts: int,
    backoff_base: float,
) -> None:
    while True:
        path = await state.next_path()
        if path is None:
            return

        result, links, shape = await fetch_with_retry(state, client, path, cache_bust, max_attempts, backoff_base)
        await state.record(result, links, shape)


async def fetch_with_retry(
    state: State,
    client: httpx.AsyncClient,
    path: str,
    cache_bust: bool,
    max_attempts: int,
    backoff_base: float,
) -> tuple[FetchResult, list[str], str | None]:
    last_result: FetchResult | None = None
    last_links: list[str] = []
    last_shape: str | None = None

    for attempt in range(1, max_attempts + 1):
        result, links, shape = await fetch_once(state.base_url, client, path, cache_bust)
        result.attempts = attempt
        last_result, last_links, last_shape = result, links, shape

        retryable = (
            attempt < max_attempts
            and (
                (result.error is not None)
                or (result.status is not None and result.status >= 500)
            )
        )
        if not retryable:
            break

        await asyncio.sleep(min(backoff_base * (2 ** (attempt - 1)), 5.0))

    assert last_result is not None
    return last_result, last_links, last_shape


async def fetch_once(
    base_url: str,
    client: httpx.AsyncClient,
    path: str,
    cache_bust: bool,
) -> tuple[FetchResult, list[str], str | None]:
    url_path = path
    if cache_bust:
        sep = "&" if "?" in path else "?"
        url_path = f"{path}{sep}__hp={time.time_ns()}"

    target = urljoin(base_url, url_path)
    started = time.perf_counter()
    status: int | None = None
    header_value: str | None = None
    error: str | None = None

    try:
        response = await client.get(target)
        status = response.status_code
        header_value = response.headers.get(HEADER_NAME)
    except Exception as exc:  # noqa: BLE001
        error = exc.__class__.__name__

    elapsed_ms = (time.perf_counter() - started) * 1000

    decoded = None
    decoded_error = None
    if header_value:
        decoded, decoded_error = decode_header(header_value)
    elif error is None and status is not None:
        decoded_error = "missing_header"

    shape, raw_links = extract_links(decoded)
    links = normalize_links(base_url, raw_links)

    return (
        FetchResult(
            path=urlparse(target).path or "/",
            url=target,
            status=status,
            elapsed_ms=elapsed_ms,
            header=header_value,
            decoded=decoded,
            decoded_error=decoded_error,
            error=error,
        ),
        links,
        shape if decoded is not None else None,
    )


def decode_header(value: str) -> tuple[Any | None, str | None]:
    try:
        padded = value + ("=" * ((4 - len(value) % 4) % 4))
        decoded_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(decoded_bytes.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, exc.__class__.__name__

    if isinstance(payload, list):
        return payload, None
    if not isinstance(payload, dict):
        return None, "payload_not_object_or_list"
    if "links" in payload and not isinstance(payload.get("links"), list):
        return None, "links_not_list"
    return payload, None


def extract_links(payload: Any) -> tuple[str, Any]:
    if isinstance(payload, list):
        return "array", payload
    if isinstance(payload, dict):
        return "object", payload.get("links", [])
    return "unknown", []


def normalize_links(base_url: str, links: Any) -> list[str]:
    if not isinstance(links, list):
        return []
    base_host = urlparse(base_url).netloc
    out: set[str] = set()
    for link in links:
        if not isinstance(link, str) or not link:
            continue
        absolute = urljoin(base_url, link)
        parsed = urlparse(absolute)
        if parsed.netloc and parsed.netloc != base_host:
            continue
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        out.add(path)
    return sorted(out)


async def progress_reporter(state: State, started_at: float, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        async with state.condition:
            snapshot = {
                "completed": state.completed,
                "scheduled": state.scheduled,
                "in_flight": state.in_flight,
                "queued": len(state.queue),
                "discovered": len(state.discovery_seen),
                "crawl_unique": len(state.crawl_seen),
                "statuses": dict(state.statuses),
                "decode_errors": dict(state.decode_errors),
                "errors": dict(state.errors),
                "done": state.in_flight == 0 and not state.queue,
            }

        elapsed = max(time.perf_counter() - started_at, 0.001)
        rps = snapshot["completed"] / elapsed
        print(
            "progress "
            f"fetched={snapshot['completed']} "
            f"scheduled={snapshot['scheduled']} "
            f"in_flight={snapshot['in_flight']} "
            f"queued={snapshot['queued']} "
            f"discovered={snapshot['discovered']} "
            f"crawl_unique={snapshot['crawl_unique']} "
            f"rps={rps:.2f} "
            f"statuses={snapshot['statuses']} "
            f"errors={snapshot['errors']} "
            f"decode_errors={snapshot['decode_errors']}",
            flush=True,
        )

        if snapshot["done"] or snapshot["completed"] >= state.max_requests:
            return


def write_reports(state: State, elapsed_s: float, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = sorted(state.stats.values(), key=lambda item: item.url)

    with (out_dir / "seo-header-report.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow([
            "url", "hits", "statuses", "header_present", "decoded_ok", "decoded_failed",
            "max_header_bytes", "avg_elapsed_ms", "unique_links", "links", "errors",
        ])
        for stats in rows:
            writer.writerow([
                stats.url, stats.hits,
                json.dumps(dict(stats.statuses), sort_keys=True),
                stats.header_present, stats.decoded_ok, stats.decoded_failed,
                stats.max_header_bytes, round(stats.avg_elapsed_ms, 2),
                len(stats.links), " ".join(sorted(stats.links)),
                json.dumps(dict(stats.errors), sort_keys=True),
            ])

    rps = (state.completed / elapsed_s) if elapsed_s > 0 else 0
    summary = {
        "baseUrl": state.base_url,
        "requestsScheduled": state.scheduled,
        "requestsCompleted": state.completed,
        "elapsedSeconds": round(elapsed_s, 3),
        "elapsedHuman": format_duration(elapsed_s),
        "requestsPerSecond": round(rps, 2),
        "totalDiscoveredUrls": len(state.discovery_seen),
        "actualUniquePagesCrawled": len(state.stats),
        "uniqueUrlsDiscovered": len(state.discovery_seen),
        "statusCounts": dict(state.statuses),
        "networkErrors": dict(state.errors),
        "decodeErrors": dict(state.decode_errors),
        "payloadShapes": dict(state.payload_shapes),
        "estimatedTimeFor65000PagesAtCurrentRps": format_duration(65000 / rps) if rps > 0 else None,
        "estimatedTimeFor66000PagesAtCurrentRps": format_duration(66000 / rps) if rps > 0 else None,
        "urls": [
            {
                "url": stats.url, "hits": stats.hits,
                "statuses": dict(stats.statuses),
                "headerPresent": stats.header_present,
                "decodedOk": stats.decoded_ok, "decodedFailed": stats.decoded_failed,
                "maxHeaderBytes": stats.max_header_bytes,
                "avgElapsedMs": round(stats.avg_elapsed_ms, 2),
                "uniqueLinks": len(stats.links),
                "links": sorted(stats.links),
                "errors": dict(stats.errors),
            }
            for stats in rows
        ],
    }

    with (out_dir / "seo-header-summary.json").open("w", encoding="utf-8") as file:
        json.dump(summary, file, indent=2, sort_keys=True)
        file.write("\n")


def print_summary(state: State, elapsed_s: float, out_dir: Path) -> None:
    rps = (state.completed / elapsed_s) if elapsed_s > 0 else 0
    print(f"base_url={state.base_url}")
    print(f"requests={state.completed}/{state.scheduled}")
    print(f"elapsed_s={elapsed_s:.3f}")
    print(f"elapsed_human={format_duration(elapsed_s)}")
    print(f"rps={rps:.2f}")
    if rps > 0:
        print(f"estimated_time_for_65000_pages_at_current_rps={format_duration(65000 / rps)}")
        print(f"estimated_time_for_66000_pages_at_current_rps={format_duration(66000 / rps)}")
    print(f"total_discovered_urls={len(state.discovery_seen)}")
    print(f"actual_unique_pages_crawled={len(state.stats)}")
    print(f"status_counts={dict(state.statuses)}")
    print(f"network_errors={dict(state.errors)}")
    print(f"decode_errors={dict(state.decode_errors)}")
    print(f"payload_shapes={dict(state.payload_shapes)}")
    print(f"csv={out_dir / 'seo-header-report.csv'}")
    print(f"json={out_dir / 'seo-header-summary.json'}")


def normalize_base_url(value: str) -> str:
    parsed = urlparse(value if "://" in value else f"https://{value}")
    return f"{parsed.scheme}://{parsed.netloc}/"


def format_duration(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fast HTTP/2 SEO header probe with sitemap seeding.")
    parser.add_argument("--base-url", default="https://data.stateglobe.com")
    parser.add_argument("--requests", type=int, default=70000)
    parser.add_argument("--concurrency", type=int, default=400)
    parser.add_argument("--max-connections", type=int, default=24, help="Max concurrent TCP connections (HTTP/2 multiplexes per connection).")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT)
    parser.add_argument("--out-dir", default="reports/data-stateglobe-fast")
    parser.add_argument("--progress-interval", type=float, default=2.0)
    parser.add_argument("--cache-bust", action="store_true")
    parser.add_argument("--use-sitemap", action="store_true", help="Seed the queue from /sitemap.xml first.")
    parser.add_argument("--max-attempts", type=int, default=4)
    parser.add_argument("--retry-backoff", type=float, default=0.25)
    return parser.parse_args()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(1)
