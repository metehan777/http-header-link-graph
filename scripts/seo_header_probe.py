#!/usr/bin/env python3
"""
High-concurrency SEO probe for X-Internal-Links headers.

This intentionally uses only Python's standard library. It opens raw HTTP/1.1
connections, reads headers only, decodes X-Internal-Links, and reports crawl
metadata quality without downloading page bodies.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import csv
import json
import ssl
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse


HEADER_NAME = "x-internal-links"
DEFAULT_BASE_URL = "https://data.stateglobe.com"
DEFAULT_USER_AGENT = "StateGlobeHeaderSEOProbe/0.1 (+https://data.stateglobe.com)"
MAX_HEADER_READ_BYTES = 128 * 1024


@dataclass(slots=True)
class FetchResult:
    url: str
    path: str
    status: int | None
    elapsed_ms: float
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None
    decoded: Any | None = None
    decoded_error: str | None = None
    attempts: int = 1


@dataclass(slots=True)
class UrlStats:
    url: str
    hits: int = 0
    statuses: Counter[int] = field(default_factory=Counter)
    errors: Counter[str] = field(default_factory=Counter)
    header_present: int = 0
    decoded_ok: int = 0
    decoded_failed: int = 0
    max_header_bytes: int = 0
    links: set[str] = field(default_factory=set)
    total_elapsed_ms: float = 0.0

    @property
    def avg_elapsed_ms(self) -> float:
        if self.hits == 0:
            return 0.0
        return self.total_elapsed_ms / self.hits


class ProbeState:
    def __init__(self, base_url: str, max_requests: int) -> None:
        self.base_url = normalize_base_url(base_url)
        self.max_requests = max_requests
        self.queue: deque[str] = deque(["/"])
        self.discovery_seen: set[str] = {"/"}
        self.crawl_seen: set[str] = {"/"}
        self.scheduled = 0
        self.completed = 0
        self.in_flight = 0
        self.condition = asyncio.Condition()
        self.stats: dict[str, UrlStats] = {}
        self.statuses: Counter[int] = Counter()
        self.errors: Counter[str] = Counter()
        self.decode_errors: Counter[str] = Counter()
        self.payload_shapes: Counter[str] = Counter()

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

    async def record(self, result: FetchResult) -> None:
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

            raw_header = result.headers.get(HEADER_NAME)
            if raw_header:
                header_bytes = len(raw_header.encode("ascii", errors="ignore"))
                stats.header_present += 1
                stats.max_header_bytes = max(stats.max_header_bytes, header_bytes)

            if result.decoded is not None:
                stats.decoded_ok += 1
                shape, raw_links = extract_links(result.decoded)
                self.payload_shapes[shape] += 1
                links = normalize_links(self.base_url, raw_links)
                stats.links.update(links)

                for link in links:
                    if link not in self.discovery_seen:
                        self.discovery_seen.add(link)

                    if link not in self.crawl_seen and self.scheduled + len(self.queue) < self.max_requests:
                        self.crawl_seen.add(link)
                        self.queue.append(link)

            if result.decoded_error:
                stats.decoded_failed += 1
                self.decode_errors[result.decoded_error] += 1

            self.in_flight -= 1
            self.condition.notify_all()

    async def snapshot(self) -> dict[str, Any]:
        async with self.condition:
            return {
                "scheduled": self.scheduled,
                "completed": self.completed,
                "in_flight": self.in_flight,
                "queued": len(self.queue),
                "unique_discovered": len(self.discovery_seen),
                "unique_queued_for_crawl": len(self.crawl_seen),
                "statuses": dict(self.statuses),
                "errors": dict(self.errors),
                "decode_errors": dict(self.decode_errors),
                "payload_shapes": dict(self.payload_shapes),
                "done": self.in_flight == 0 and not self.queue,
            }


async def main() -> None:
    args = parse_args()
    state = ProbeState(args.base_url, args.requests)
    started_at = time.perf_counter()

    pool = ConnectionPool(max_idle_per_host=args.max_idle_per_host)

    workers = [
        asyncio.create_task(
            worker(
                state,
                args.timeout,
                args.user_agent,
                args.cache_bust,
                args.max_attempts,
                args.retry_backoff,
                pool,
                args.method,
            )
        )
        for _ in range(args.concurrency)
    ]
    progress = asyncio.create_task(progress_reporter(state, started_at, args.progress_interval))

    await asyncio.gather(*workers)
    await progress
    await pool.close_all()
    elapsed_s = time.perf_counter() - started_at

    write_reports(state, elapsed_s, Path(args.out_dir))
    print_summary(state, elapsed_s, Path(args.out_dir))


RETRYABLE_NETWORK_ERRORS = {
    "TimeoutError",
    "ConnectionResetError",
    "ConnectionRefusedError",
    "BrokenPipeError",
    "OSError",
    "IncompleteReadError",
    "ssl.SSLError",
    "SSLError",
}


async def worker(
    state: "ProbeState",
    timeout: float,
    user_agent: str,
    cache_bust: bool,
    max_attempts: int,
    backoff_base: float,
    pool: ConnectionPool,
    method: str,
) -> None:
    while True:
        path = await state.next_path()
        if path is None:
            return

        result = None
        last_attempt = 0

        for attempt in range(1, max_attempts + 1):
            last_attempt = attempt
            result = await fetch_headers(state.base_url, path, timeout, user_agent, cache_bust, pool, method)

            if result.headers.get(HEADER_NAME):
                result.decoded, result.decoded_error = decode_header(result.headers[HEADER_NAME])
            elif result.error is None and result.status is not None:
                result.decoded_error = "missing_header"

            should_retry = (
                attempt < max_attempts
                and (
                    (result.error in RETRYABLE_NETWORK_ERRORS)
                    or (result.status is not None and result.status >= 500)
                )
            )

            if not should_retry:
                break

            await asyncio.sleep(min(backoff_base * (2 ** (attempt - 1)), 5.0))

        assert result is not None
        result.attempts = last_attempt
        await state.record(result)


async def progress_reporter(state: ProbeState, started_at: float, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        snapshot = await state.snapshot()
        elapsed_s = max(time.perf_counter() - started_at, 0.001)
        rps = snapshot["completed"] / elapsed_s
        print(
            "progress "
            f"fetched={snapshot['completed']} "
            f"scheduled={snapshot['scheduled']} "
            f"in_flight={snapshot['in_flight']} "
            f"queued={snapshot['queued']} "
            f"discovered={snapshot['unique_discovered']} "
            f"crawl_unique={snapshot['unique_queued_for_crawl']} "
            f"rps={rps:.2f} "
            f"statuses={snapshot['statuses']} "
            f"decode_errors={snapshot['decode_errors']}",
            flush=True,
        )

        if snapshot["done"] or snapshot["completed"] >= state.max_requests:
            return


class ConnectionPool:
    def __init__(self, max_idle_per_host: int = 64) -> None:
        self._idle: dict[tuple[str, int, bool], deque[tuple[asyncio.StreamReader, asyncio.StreamWriter]]] = {}
        self._lock = asyncio.Lock()
        self._max_idle_per_host = max_idle_per_host

    async def acquire(
        self,
        host: str,
        port: int,
        use_tls: bool,
        timeout: float,
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        key = (host, port, use_tls)

        async with self._lock:
            pool = self._idle.get(key)
            while pool:
                reader, writer = pool.popleft()
                if not writer.is_closing():
                    return reader, writer

        ssl_context = ssl.create_default_context() if use_tls else None
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_context, server_hostname=host if use_tls else None),
            timeout=timeout,
        )
        return reader, writer

    async def release(
        self,
        host: str,
        port: int,
        use_tls: bool,
        connection: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None,
    ) -> None:
        if connection is None:
            return

        reader, writer = connection
        if writer.is_closing():
            return

        key = (host, port, use_tls)

        async with self._lock:
            pool = self._idle.setdefault(key, deque())
            if len(pool) >= self._max_idle_per_host:
                writer.close()
                return
            pool.append((reader, writer))

    async def close_all(self) -> None:
        async with self._lock:
            for pool in self._idle.values():
                while pool:
                    _, writer = pool.popleft()
                    if not writer.is_closing():
                        writer.close()
            self._idle.clear()


async def fetch_headers(
    base_url: str,
    path: str,
    timeout: float,
    user_agent: str,
    cache_bust: bool,
    pool: ConnectionPool,
    method: str,
) -> FetchResult:
    target_url = urljoin(base_url, path)
    parsed = urlparse(target_url)
    started_at = time.perf_counter()
    status: int | None = None
    headers: dict[str, str] = {}
    error: str | None = None

    host = parsed.hostname or ""
    use_tls = parsed.scheme == "https"
    port = parsed.port or (443 if use_tls else 80)
    request_target = build_request_target(parsed.path or "/", parsed.query, cache_bust)

    connection: tuple[asyncio.StreamReader, asyncio.StreamWriter] | None = None
    keep_alive = False

    try:
        connection = await pool.acquire(host, port, use_tls, timeout)
        reader, writer = connection

        request = (
            f"{method} {request_target} HTTP/1.1\r\n"
            f"Host: {parsed.netloc}\r\n"
            f"User-Agent: {user_agent}\r\n"
            "Accept: */*\r\n"
            f"{'Cache-Control: no-cache' + chr(13) + chr(10) if cache_bust else ''}"
            "Connection: keep-alive\r\n"
            "\r\n"
        )
        writer.write(request.encode("ascii"))
        await asyncio.wait_for(writer.drain(), timeout=timeout)

        raw_headers = await read_header_block(reader, timeout)
        status, headers = parse_header_block(raw_headers)

        keep_alive = await drain_body(reader, headers, timeout, method)

        connection_header = headers.get("connection", "").lower()
        if "close" in connection_header:
            keep_alive = False
    except Exception as exc:  # noqa: BLE001 - report network/OS errors as data.
        error = exc.__class__.__name__
        keep_alive = False
    finally:
        if connection is not None:
            if keep_alive and error is None:
                await pool.release(host, port, use_tls, connection)
            else:
                _, writer = connection
                if not writer.is_closing():
                    writer.close()

    elapsed_ms = (time.perf_counter() - started_at) * 1000
    return FetchResult(
        url=target_url,
        path=urlparse(target_url).path or "/",
        status=status,
        elapsed_ms=elapsed_ms,
        headers=headers,
        error=error,
    )


async def drain_body(
    reader: asyncio.StreamReader,
    headers: dict[str, str],
    timeout: float,
    method: str,
) -> bool:
    if method == "HEAD":
        return True

    transfer_encoding = headers.get("transfer-encoding", "").lower()
    content_length_raw = headers.get("content-length")

    try:
        if "chunked" in transfer_encoding:
            while True:
                size_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
                if not size_line:
                    return False
                try:
                    chunk_size = int(size_line.strip().split(b";", 1)[0] or b"0", 16)
                except ValueError:
                    return False
                if chunk_size == 0:
                    await asyncio.wait_for(reader.readline(), timeout=timeout)
                    return True
                remaining = chunk_size
                while remaining > 0:
                    chunk = await asyncio.wait_for(reader.read(min(remaining, 65536)), timeout=timeout)
                    if not chunk:
                        return False
                    remaining -= len(chunk)
                await asyncio.wait_for(reader.readline(), timeout=timeout)

        if content_length_raw is not None:
            try:
                remaining = int(content_length_raw)
            except ValueError:
                return False
            while remaining > 0:
                chunk = await asyncio.wait_for(reader.read(min(remaining, 65536)), timeout=timeout)
                if not chunk:
                    return False
                remaining -= len(chunk)
            return True

        return False
    except Exception:  # noqa: BLE001 - drain failures invalidate connection.
        return False


async def read_header_block(reader: asyncio.StreamReader, timeout: float) -> bytes:
    data = bytearray()

    while b"\r\n\r\n" not in data:
        chunk = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        if not chunk:
            break
        data.extend(chunk)

        if len(data) > MAX_HEADER_READ_BYTES:
            raise ValueError("header_block_too_large")

    return bytes(data).split(b"\r\n\r\n", 1)[0]


def parse_header_block(raw_headers: bytes) -> tuple[int | None, dict[str, str]]:
    lines = raw_headers.decode("iso-8859-1", errors="replace").split("\r\n")
    if not lines or not lines[0].startswith("HTTP/"):
        return None, {}

    parts = lines[0].split(" ", 2)
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    headers: dict[str, str] = {}

    for line in lines[1:]:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        key = name.strip().lower()
        clean_value = value.strip()
        if key in headers:
            headers[key] = f"{headers[key]}, {clean_value}"
        else:
            headers[key] = clean_value

    return status, headers


def decode_header(value: str) -> tuple[Any | None, str | None]:
    try:
        padded = value + ("=" * ((4 - len(value) % 4) % 4))
        decoded_bytes = base64.urlsafe_b64decode(padded.encode("ascii"))
        payload = json.loads(decoded_bytes.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - report malformed headers as data.
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


def build_request_target(path: str, query: str, cache_bust: bool) -> str:
    params = []
    if query:
        params.append(query)
    if cache_bust:
        params.append(f"__header_probe={time.time_ns()}")

    if params:
        return f"{path}?{'&'.join(params)}"

    return path


def normalize_links(base_url: str, links: Any) -> set[str]:
    base_host = urlparse(base_url).netloc
    normalized: set[str] = set()

    if not isinstance(links, list):
        return normalized

    for link in links:
        if not isinstance(link, str) or not link:
            continue

        absolute = urljoin(base_url, link)
        parsed = urlparse(absolute)
        if parsed.netloc != base_host:
            continue

        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        normalized.add(path)

    return normalized


def write_reports(state: ProbeState, elapsed_s: float, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    rows = sorted(state.stats.values(), key=lambda item: item.url)

    with (out_dir / "seo-header-report.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow([
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
        ])

        for stats in rows:
            writer.writerow([
                stats.url,
                stats.hits,
                json.dumps(dict(stats.statuses), sort_keys=True),
                stats.header_present,
                stats.decoded_ok,
                stats.decoded_failed,
                stats.max_header_bytes,
                round(stats.avg_elapsed_ms, 2),
                len(stats.links),
                " ".join(sorted(stats.links)),
                json.dumps(dict(stats.errors), sort_keys=True),
            ])

    summary = {
        "baseUrl": state.base_url,
        "requestsScheduled": state.scheduled,
        "requestsCompleted": state.completed,
        "elapsedSeconds": round(elapsed_s, 3),
        "elapsedHuman": format_duration(elapsed_s),
        "requestsPerSecond": round(state.completed / elapsed_s, 2) if elapsed_s > 0 else 0,
        "estimatedTimeFor65000PagesAtCurrentRps": format_duration(65000 / (state.completed / elapsed_s))
        if state.completed > 0 and elapsed_s > 0
        else None,
        "estimatedTimeFor66000PagesAtCurrentRps": format_duration(66000 / (state.completed / elapsed_s))
        if state.completed > 0 and elapsed_s > 0
        else None,
        "totalDiscoveredUrls": len(state.discovery_seen),
        "actualUniquePagesCrawled": len(state.stats),
        "uniqueUrlsDiscovered": len(state.discovery_seen),
        "statusCounts": dict(state.statuses),
        "networkErrors": dict(state.errors),
        "decodeErrors": dict(state.decode_errors),
        "payloadShapes": dict(state.payload_shapes),
        "urls": [
            {
                "url": stats.url,
                "hits": stats.hits,
                "statuses": dict(stats.statuses),
                "headerPresent": stats.header_present,
                "decodedOk": stats.decoded_ok,
                "decodedFailed": stats.decoded_failed,
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


def print_summary(state: ProbeState, elapsed_s: float, out_dir: Path) -> None:
    print(f"base_url={state.base_url}")
    print(f"requests={state.completed}/{state.scheduled}")
    print(f"elapsed_s={elapsed_s:.3f}")
    print(f"elapsed_human={format_duration(elapsed_s)}")
    print(f"rps={(state.completed / elapsed_s) if elapsed_s > 0 else 0:.2f}")
    if state.completed > 0 and elapsed_s > 0:
        current_rps = state.completed / elapsed_s
        print(f"estimated_time_for_65000_pages_at_current_rps={format_duration(65000 / current_rps)}")
        print(f"estimated_time_for_66000_pages_at_current_rps={format_duration(66000 / current_rps)}")
    print(f"total_discovered_urls={len(state.discovery_seen)}")
    print(f"actual_unique_pages_crawled={len(state.stats)}")
    print(f"unique_urls_discovered={len(state.discovery_seen)}")
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
    total_seconds = int(round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)

    if hours:
        return f"{hours}h {minutes}m {secs}s"

    if minutes:
        return f"{minutes}m {secs}s"

    return f"{secs}s"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe SEO crawl metadata from X-Internal-Links headers.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help=f"Site origin. Default: {DEFAULT_BASE_URL}")
    parser.add_argument("--requests", type=int, default=70000, help="Total unique URLs to crawl. Default: 70000")
    parser.add_argument("--concurrency", type=int, default=200, help="Parallel open requests. Default: 200")
    parser.add_argument("--timeout", type=float, default=15.0, help="Per-request timeout in seconds. Default: 15")
    parser.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="User-Agent header for requests.")
    parser.add_argument("--out-dir", default="reports", help="Directory for CSV/JSON reports. Default: reports")
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=1.0,
        help="Seconds between realtime progress updates. Default: 1",
    )
    parser.add_argument(
        "--cache-bust",
        action="store_true",
        help="Append a unique query parameter to avoid stale CDN cache while testing headers.",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=4,
        help="Max attempts per URL when network errors or 5xx responses happen. Default: 4",
    )
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=0.25,
        help="Initial seconds for exponential retry backoff. Default: 0.25",
    )
    parser.add_argument(
        "--max-idle-per-host",
        type=int,
        default=128,
        help="Max idle keep-alive connections cached per host. Default: 128",
    )
    parser.add_argument(
        "--method",
        choices=["GET", "HEAD"],
        default="GET",
        help="HTTP method. HEAD is faster but some origins ignore it. Default: GET",
    )

    args = parser.parse_args()

    if args.requests < 1:
        parser.error("--requests must be >= 1")
    if args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    if args.progress_interval <= 0:
        parser.error("--progress-interval must be > 0")

    return args


if __name__ == "__main__":
    asyncio.run(main())
