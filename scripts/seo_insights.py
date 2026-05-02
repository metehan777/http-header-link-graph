#!/usr/bin/env python3
"""SEO insights generator for header-discovered link graphs.

Reads a summary JSON produced by the Rust/Python header probe
(e.g. reports/rust-second-run/seo-header-summary.json) and emits:

  * <out_dir>/seo-insights.json         machine-readable findings
  * <out_dir>/seo-insights.md           human-readable report

Insights:

  1.  Crawl health (status mix, header coverage, payload-truncation risk).
  2.  Top hubs by inbound links (PageRank-style importance proxy).
  3.  Orphan pages (0 inbound links) -> wasted crawl budget.
  4.  Dead-end pages (0 outbound links) -> link equity sink.
  5.  Click depth from "/" -> pages 3+ clicks deep are SEO risks.
  6.  Reciprocal link pairs (cross-linking strength).
  7.  Link-equity distribution (top-N share, Gini coefficient).
  8.  Topical clusters by URL path prefix (cohesion + size).
  9.  Header payload risk (pages approaching 8 KB).
  10. Anomalies (pages under-linked vs cluster median).

Usage:
    python3 scripts/seo_insights.py \
        --input reports/rust-second-run/seo-header-summary.json \
        --out-dir reports/rust-second-run/insights
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Dict, Iterable, List, Sequence, Set, Tuple
from urllib.parse import urlparse

HEADER_BUDGET_BYTES = 8 * 1024
PAYLOAD_RISK_RATIO = 0.85   # >=85% of 8 KB
TOP_N = 25
MAX_DEPTH_FOR_SCAN = 12


# ---------- helpers ----------------------------------------------------------

def normalize_path(url_or_path: str) -> str:
    """Reduce both absolute URLs and relative links to a canonical /path."""
    if url_or_path.startswith("http://") or url_or_path.startswith("https://"):
        path = urlparse(url_or_path).path or "/"
    else:
        path = url_or_path
    if not path.startswith("/"):
        path = "/" + path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    return path


def cluster_for(path: str) -> str:
    """Group a path into a top-level cluster.

    Examples:
      /                              -> __root__
      /afghanistan                   -> /afghanistan
      /afghanistan/ai-adoption-...   -> /afghanistan
      /category/ecommerce            -> /category
      /blog/some-post                -> /blog
    """
    if path in ("", "/"):
        return "__root__"
    parts = path.lstrip("/").split("/", 1)
    return "/" + parts[0]


def gini(values: Sequence[int]) -> float:
    """Gini coefficient. 0 = perfectly equal, 1 = maximally concentrated."""
    if not values:
        return 0.0
    sorted_v = sorted(values)
    n = len(sorted_v)
    cum = 0
    for i, v in enumerate(sorted_v, start=1):
        cum += i * v
    total = sum(sorted_v)
    if total == 0:
        return 0.0
    return (2 * cum) / (n * total) - (n + 1) / n


def fmt_int(n: int) -> str:
    return f"{n:,}"


def pct(part: int, whole: int) -> str:
    if whole == 0:
        return "0.00%"
    return f"{(part / whole) * 100:.2f}%"


# ---------- core analysis ----------------------------------------------------

def build_graph(urls: List[dict]) -> Tuple[
    Dict[str, dict],            # nodes: path -> meta
    Dict[str, Set[str]],        # outbound: path -> {target,...}
    Dict[str, Set[str]],        # inbound:  path -> {source,...}
]:
    nodes: Dict[str, dict] = {}
    out: Dict[str, Set[str]] = defaultdict(set)
    inb: Dict[str, Set[str]] = defaultdict(set)

    for entry in urls:
        path = normalize_path(entry["url"])
        nodes[path] = {
            "path": path,
            "url": entry["url"],
            "hits": entry.get("hits", 0),
            "headerPresent": entry.get("headerPresent", 0),
            "decodedOk": entry.get("decodedOk", 0),
            "uniqueLinks": entry.get("uniqueLinks", 0),
            "maxHeaderBytes": entry.get("maxHeaderBytes", 0),
            "avgElapsedMs": entry.get("avgElapsedMs", 0.0),
            "statuses": entry.get("statuses", {}),
        }
        for link in entry.get("links", []) or []:
            target = normalize_path(link)
            if target == path:
                continue  # skip self-loops; they add noise
            out[path].add(target)

    for src, targets in out.items():
        for t in targets:
            inb[t].add(src)

    return nodes, out, inb


def click_depth(out_edges: Dict[str, Set[str]], known_nodes: Set[str], root: str = "/") -> Dict[str, int]:
    """BFS shortest-path depth from root using known nodes only."""
    depth: Dict[str, int] = {root: 0}
    q: deque[str] = deque([root])
    while q:
        node = q.popleft()
        d = depth[node]
        if d >= MAX_DEPTH_FOR_SCAN:
            continue
        for nxt in out_edges.get(node, ()):
            if nxt in known_nodes and nxt not in depth:
                depth[nxt] = d + 1
                q.append(nxt)
    return depth


def reciprocal_pairs(out_edges: Dict[str, Set[str]]) -> int:
    seen: Set[Tuple[str, str]] = set()
    count = 0
    for src, targets in out_edges.items():
        for t in targets:
            if src in out_edges.get(t, ()):
                key = tuple(sorted((src, t)))
                if key not in seen:
                    seen.add(key)
                    count += 1
    return count


def cluster_stats(nodes: Dict[str, dict],
                  out_edges: Dict[str, Set[str]],
                  inb_edges: Dict[str, Set[str]]) -> List[dict]:
    grouped: Dict[str, List[str]] = defaultdict(list)
    for p in nodes:
        grouped[cluster_for(p)].append(p)

    out: List[dict] = []
    for cluster, paths in grouped.items():
        path_set = set(paths)
        internal_links = 0
        external_links = 0
        for p in paths:
            for tgt in out_edges.get(p, ()):
                if tgt in path_set:
                    internal_links += 1
                else:
                    external_links += 1
        in_degrees = [len(inb_edges.get(p, ())) for p in paths]
        out_degrees = [len(out_edges.get(p, ())) for p in paths]
        out.append({
            "cluster": cluster,
            "size": len(paths),
            "internalLinks": internal_links,
            "outboundToOtherClusters": external_links,
            "cohesion": round(internal_links / (internal_links + external_links), 4)
            if (internal_links + external_links) else 0.0,
            "medianInDegree": int(statistics.median(in_degrees)) if in_degrees else 0,
            "medianOutDegree": int(statistics.median(out_degrees)) if out_degrees else 0,
        })
    out.sort(key=lambda c: c["size"], reverse=True)
    return out


def under_linked_anomalies(nodes: Dict[str, dict],
                           inb_edges: Dict[str, Set[str]]) -> List[dict]:
    """Pages whose inbound count is < 25% of their cluster median."""
    by_cluster: Dict[str, List[str]] = defaultdict(list)
    for p in nodes:
        by_cluster[cluster_for(p)].append(p)

    flagged: List[dict] = []
    for cluster, paths in by_cluster.items():
        if len(paths) < 20:
            continue
        in_degrees = [len(inb_edges.get(p, ())) for p in paths]
        med = statistics.median(in_degrees) if in_degrees else 0
        if med < 4:
            continue
        threshold = max(1, med * 0.25)
        for p in paths:
            d = len(inb_edges.get(p, ()))
            if d < threshold:
                flagged.append({
                    "path": p,
                    "cluster": cluster,
                    "inDegree": d,
                    "clusterMedianInDegree": int(med),
                })
    flagged.sort(key=lambda r: (r["clusterMedianInDegree"] - r["inDegree"]), reverse=True)
    return flagged


# ---------- markdown report --------------------------------------------------

def render_markdown(report: dict) -> str:
    L: List[str] = []
    add = L.append

    meta = report["meta"]
    health = report["crawlHealth"]
    eq = report["linkEquity"]
    payload = report["headerPayload"]

    add(f"# SEO insights — {meta['baseUrl']}")
    add("")
    add(f"_Source: `{meta['inputFile']}`_")
    add(f"_Generated: {meta['generatedAt']}_")
    add("")

    add("## 1. Crawl health")
    add("")
    add(f"- Pages crawled: **{fmt_int(health['pagesCrawled'])}**")
    add(f"- HTTP 200: **{fmt_int(health['ok2xx'])}** "
        f"({pct(health['ok2xx'], health['pagesCrawled'])})")
    add(f"- HTTP 5xx: **{fmt_int(health['err5xx'])}** "
        f"({pct(health['err5xx'], health['pagesCrawled'])})")
    add(f"- Network errors: **{fmt_int(health['networkErrors'])}**")
    add(f"- Header present: **{fmt_int(health['headerPresent'])}** "
        f"({pct(health['headerPresent'], health['pagesCrawled'])})")
    add(f"- Header decoded ok: **{fmt_int(health['decodedOk'])}** "
        f"({pct(health['decodedOk'], health['pagesCrawled'])})")
    add(f"- Average TTFB-ish (avg elapsed ms across crawl): "
        f"**{health['avgElapsedMs']:.1f} ms**")
    add("")

    add("## 2. Header payload usage (8 KB budget)")
    add("")
    add(f"- Max payload observed: **{fmt_int(payload['maxBytes'])} B** "
        f"({payload['maxBytes'] / HEADER_BUDGET_BYTES * 100:.1f}% of budget)")
    add(f"- p50 payload: **{fmt_int(payload['p50'])} B**")
    add(f"- p95 payload: **{fmt_int(payload['p95'])} B**")
    add(f"- Pages at risk (>= {int(PAYLOAD_RISK_RATIO * 100)}% of 8 KB): "
        f"**{fmt_int(payload['atRisk'])}** "
        f"({pct(payload['atRisk'], health['pagesCrawled'])})")
    if payload["atRiskExamples"]:
        add("- At-risk examples:")
        for ex in payload["atRiskExamples"][:10]:
            add(f"    - `{ex['path']}` — {fmt_int(ex['bytes'])} B, "
                f"{ex['uniqueLinks']} links")
    add("")

    add("## 3. Top hubs (most internally linked pages)")
    add("")
    add("These are your strongest internal-link recipients. Treat them as "
        "high-priority pages: keep them indexable, fast, and well-linked outward.")
    add("")
    add("| # | Path | Inbound | Outbound | Cluster |")
    add("|---|------|--------:|---------:|---------|")
    for i, h in enumerate(report["topHubs"][:TOP_N], 1):
        add(f"| {i} | `{h['path']}` | {fmt_int(h['inDegree'])} | "
            f"{fmt_int(h['outDegree'])} | `{h['cluster']}` |")
    add("")

    add("## 4. Orphans (0 inbound internal links)")
    add("")
    add(f"Total orphans: **{fmt_int(report['orphans']['count'])}** "
        f"({pct(report['orphans']['count'], health['pagesCrawled'])})")
    add("")
    add("Orphans are reachable only via the sitemap. Google may rarely recrawl them "
        "and they accrue no internal PageRank. Add inbound links from cluster hubs.")
    add("")
    if report["orphans"]["examples"]:
        add("Sample orphans (first 25):")
        add("")
        for ex in report["orphans"]["examples"][:25]:
            add(f"- `{ex}`")
    add("")

    add("## 5. Dead ends (0 outbound internal links)")
    add("")
    add(f"Total dead ends: **{fmt_int(report['deadEnds']['count'])}** "
        f"({pct(report['deadEnds']['count'], health['pagesCrawled'])})")
    add("")
    add("Dead ends absorb link equity but don't pass it on. Add a 'related pages' "
        "block that links back to the cluster index and 3-5 sibling pages.")
    add("")
    add(f"_Note: {fmt_int(report['crawlIncomplete']['count'])} pages had a missing or "
        f"undecoded `X-Internal-Links` header on this run and were excluded from "
        f"the dead-end list. Re-crawl them to be sure._")
    add("")
    if report["deadEnds"]["examples"]:
        add("Sample dead ends (first 25):")
        add("")
        for ex in report["deadEnds"]["examples"][:25]:
            add(f"- `{ex}`")
    add("")
    add("### 5b. Crawl-incomplete (re-crawl these)")
    add("")
    add(f"Total: **{fmt_int(report['crawlIncomplete']['count'])}**. "
        f"Outbound link list is unknown for these pages.")
    add("")
    if report["crawlIncomplete"]["examples"]:
        add("Sample crawl-incomplete (first 25):")
        add("")
        for ex in report["crawlIncomplete"]["examples"][:25]:
            add(f"- `{ex}`")
    add("")

    add("## 6. Click depth from `/`")
    add("")
    dist = report["clickDepth"]["distribution"]
    add("| Depth | Pages | Share |")
    add("|------:|------:|------:|")
    total = sum(dist.values())
    for d in sorted(dist):
        add(f"| {d} | {fmt_int(dist[d])} | {pct(dist[d], total)} |")
    add("")
    add(f"- Unreachable from `/`: **{fmt_int(report['clickDepth']['unreachable'])}**")
    add(f"- Pages at depth >= 4: **{fmt_int(report['clickDepth']['deepCount'])}** "
        f"(SEO-risky — push these into hub navigation)")
    add("")

    add("## 7. Link-equity distribution")
    add("")
    add(f"- Total internal edges: **{fmt_int(eq['totalEdges'])}**")
    add(f"- Average inbound per page: **{eq['avgInDegree']:.2f}**")
    add(f"- Median inbound per page: **{eq['medianInDegree']}**")
    add(f"- Gini coefficient (inbound): **{eq['giniInDegree']:.3f}** "
        f"(0 = perfectly equal, 1 = winner-takes-all)")
    add(f"- Top 10 pages hold **{pct(eq['top10Inbound'], eq['totalEdges'])}** of inbound links")
    add(f"- Top 100 pages hold **{pct(eq['top100Inbound'], eq['totalEdges'])}** of inbound links")
    add("")

    add("## 8. Topical clusters")
    add("")
    add("Cohesion = internal-cluster links / (internal + outbound-to-other clusters).")
    add("High cohesion (> 0.7) is normally good for topical authority, but "
        "isolated clusters (cohesion ~ 1.0) may need bridge links to siblings.")
    add("")
    add("| Cluster | Size | Internal links | To other clusters | Cohesion | Median in | Median out |")
    add("|---------|-----:|---------------:|------------------:|---------:|----------:|-----------:|")
    for c in report["clusters"][:30]:
        add(f"| `{c['cluster']}` | {fmt_int(c['size'])} | "
            f"{fmt_int(c['internalLinks'])} | {fmt_int(c['outboundToOtherClusters'])} | "
            f"{c['cohesion']:.3f} | {c['medianInDegree']} | {c['medianOutDegree']} |")
    add("")

    add("## 9. Reciprocal links")
    add("")
    add(f"- Reciprocal pairs (A↔B): **{fmt_int(report['reciprocalPairs'])}**")
    add("")
    add("Reciprocity is healthy in moderation (sibling pages, breadcrumb links). "
        "If this is very high it can hint at boilerplate over-linking.")
    add("")

    add("## 10. Anomalies — under-linked pages within cluster")
    add("")
    add(f"Flagged: **{fmt_int(len(report['underlinked']))}** pages below 25% of their cluster's median inbound count.")
    add("")
    if report["underlinked"]:
        add("| # | Path | In | Cluster median |")
        add("|--:|------|---:|---------------:|")
        for i, r in enumerate(report["underlinked"][:30], 1):
            add(f"| {i} | `{r['path']}` | {r['inDegree']} | "
                f"{r['clusterMedianInDegree']} |")
    add("")

    add("## Action checklist")
    add("")
    add("1. **Re-crawl crawl-incomplete pages** so the dataset is complete before you act.")
    add("2. **Lift orphans into cluster hubs** — add inbound links from the country/category index.")
    add("3. **Compress click depth** — for pages 4+ clicks deep, add a hub-page link or breadcrumb.")
    add("4. **Equalize clusters** — if two clusters have the same size but very different median in-degree, copy the linking pattern from the strong cluster onto the weak one.")
    add("5. **Bridge isolated clusters** (cohesion ≈ 1.0) with 1-2 contextual links to adjacent topics.")
    add("6. **Audit under-linked pages** within clusters — boost them or noindex if low quality.")
    add("7. **Watch payload growth** — if p95 nears 7 KB, your `X-Internal-Links` header is close to the 8 KB ceiling and may start truncating.")
    add("")

    return "\n".join(L) + "\n"


# ---------- main -------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True, help="Path to seo-header-summary.json")
    ap.add_argument("--out-dir", required=True, help="Directory to write insights into")
    ap.add_argument("--top-orphans", type=int, default=200)
    ap.add_argument("--top-dead-ends", type=int, default=200)
    args = ap.parse_args()

    in_path = Path(args.input)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/6] Loading {in_path} ...", flush=True)
    summary = json.loads(in_path.read_text())
    urls = summary["urls"]
    base_url = summary.get("baseUrl", "")

    print(f"[2/6] Building graph from {len(urls):,} URLs ...", flush=True)
    nodes, out_edges, inb_edges = build_graph(urls)
    known = set(nodes.keys())
    print(f"      nodes={len(nodes):,}  outbound entries={len(out_edges):,}  "
          f"inbound entries={len(inb_edges):,}", flush=True)

    print("[3/6] Computing degrees, hubs, orphans, dead ends ...", flush=True)
    in_deg = {p: len(inb_edges.get(p, ())) for p in nodes}
    out_deg = {p: len(out_edges.get(p, ())) for p in nodes}

    hubs_sorted = sorted(in_deg.items(), key=lambda kv: kv[1], reverse=True)
    top_hubs = [
        {
            "path": p,
            "inDegree": d,
            "outDegree": out_deg.get(p, 0),
            "cluster": cluster_for(p),
        }
        for p, d in hubs_sorted[:TOP_N]
    ]

    # Distinguish "crawl-incomplete" pages (header missing, can't trust their
    # outbound links) from real structural dead-ends.
    crawl_incomplete = sorted(
        p for p, n in nodes.items()
        if n["headerPresent"] == 0 or n["decodedOk"] == 0
    )
    crawl_incomplete_set = set(crawl_incomplete)

    orphan_paths = [p for p in nodes if in_deg[p] == 0 and p != "/"]
    dead_end_paths = [
        p for p in nodes
        if out_deg[p] == 0 and p not in crawl_incomplete_set
    ]

    print("[4/6] Computing click depth from / ...", flush=True)
    depth_map = click_depth(out_edges, known, root="/")
    depth_dist: Dict[int, int] = Counter()
    for p in nodes:
        d = depth_map.get(p)
        if d is None:
            continue
        depth_dist[min(d, MAX_DEPTH_FOR_SCAN)] += 1
    deep_count = sum(c for d, c in depth_dist.items() if d >= 4)
    unreachable = sum(1 for p in nodes if p not in depth_map)

    print("[5/6] Computing equity, payload, clusters, anomalies ...", flush=True)
    in_values = list(in_deg.values())
    total_edges = sum(in_values)
    sorted_in = sorted(in_values, reverse=True)
    top10 = sum(sorted_in[:10])
    top100 = sum(sorted_in[:100])

    payload_bytes = [n["maxHeaderBytes"] for n in nodes.values() if n["maxHeaderBytes"] > 0]
    payload_bytes.sort()
    p50 = payload_bytes[len(payload_bytes) // 2] if payload_bytes else 0
    p95 = payload_bytes[int(len(payload_bytes) * 0.95)] if payload_bytes else 0
    risk_threshold = HEADER_BUDGET_BYTES * PAYLOAD_RISK_RATIO
    at_risk = [
        {"path": p, "bytes": n["maxHeaderBytes"], "uniqueLinks": n["uniqueLinks"]}
        for p, n in nodes.items()
        if n["maxHeaderBytes"] >= risk_threshold
    ]
    at_risk.sort(key=lambda r: r["bytes"], reverse=True)

    clusters = cluster_stats(nodes, out_edges, inb_edges)
    underlinked = under_linked_anomalies(nodes, inb_edges)
    recip = reciprocal_pairs(out_edges)

    statuses = summary.get("statusCounts", {})
    ok2xx = sum(v for k, v in statuses.items() if str(k).startswith("2"))
    err5xx = sum(v for k, v in statuses.items() if str(k).startswith("5"))
    avg_elapsed = (
        statistics.mean(n["avgElapsedMs"] for n in nodes.values() if n["avgElapsedMs"])
        if nodes else 0.0
    )

    report = {
        "meta": {
            "baseUrl": base_url,
            "inputFile": str(in_path),
            "generatedAt": __import__("datetime").datetime.utcnow().isoformat() + "Z",
            "totalNodes": len(nodes),
            "totalEdges": total_edges,
        },
        "crawlHealth": {
            "pagesCrawled": len(nodes),
            "ok2xx": ok2xx,
            "err5xx": err5xx,
            "networkErrors": sum(summary.get("networkErrors", {}).values()),
            "headerPresent": sum(1 for n in nodes.values() if n["headerPresent"] > 0),
            "decodedOk": sum(1 for n in nodes.values() if n["decodedOk"] > 0),
            "avgElapsedMs": round(avg_elapsed, 2),
        },
        "headerPayload": {
            "maxBytes": max(payload_bytes) if payload_bytes else 0,
            "p50": p50,
            "p95": p95,
            "atRisk": len(at_risk),
            "atRiskExamples": at_risk[:50],
        },
        "topHubs": top_hubs,
        "orphans": {
            "count": len(orphan_paths),
            "examples": orphan_paths[: args.top_orphans],
        },
        "deadEnds": {
            "count": len(dead_end_paths),
            "examples": dead_end_paths[: args.top_dead_ends],
        },
        "crawlIncomplete": {
            "count": len(crawl_incomplete),
            "note": (
                "Pages where the X-Internal-Links header was missing / not decoded. "
                "Their outbound links are unknown, so they were excluded from the "
                "dead-end list. Re-crawl these to refine results."
            ),
            "examples": crawl_incomplete[:200],
        },
        "clickDepth": {
            "distribution": dict(sorted(depth_dist.items())),
            "deepCount": deep_count,
            "unreachable": unreachable,
        },
        "linkEquity": {
            "totalEdges": total_edges,
            "avgInDegree": round(sum(in_values) / len(in_values), 2) if in_values else 0,
            "medianInDegree": int(statistics.median(in_values)) if in_values else 0,
            "giniInDegree": round(gini(in_values), 4),
            "top10Inbound": top10,
            "top100Inbound": top100,
        },
        "clusters": clusters,
        "reciprocalPairs": recip,
        "underlinked": underlinked[:500],
    }

    print("[6/6] Writing reports ...", flush=True)
    json_path = out_dir / "seo-insights.json"
    md_path = out_dir / "seo-insights.md"
    recrawl_path = out_dir / "recrawl-list.txt"
    json_path.write_text(json.dumps(report, indent=2))
    md_path.write_text(render_markdown(report))
    recrawl_path.write_text("\n".join(crawl_incomplete) + ("\n" if crawl_incomplete else ""))

    # console summary -------------------------------------------------------
    print()
    print("=" * 60)
    print("SEO insights summary")
    print("=" * 60)
    print(f"  pages crawled       : {fmt_int(report['crawlHealth']['pagesCrawled'])}")
    print(f"  internal edges      : {fmt_int(total_edges)}")
    print(f"  orphans (no inbound): {fmt_int(len(orphan_paths))}")
    print(f"  dead ends (no out)  : {fmt_int(len(dead_end_paths))}")
    print(f"  unreachable from /  : {fmt_int(unreachable)}")
    print(f"  pages depth >= 4    : {fmt_int(deep_count)}")
    print(f"  crawl-incomplete    : {fmt_int(len(crawl_incomplete))} (header missing)")
    print(f"  reciprocal pairs    : {fmt_int(recip)}")
    print(f"  Gini (inbound)      : {report['linkEquity']['giniInDegree']:.3f}")
    print(f"  payload max         : {fmt_int(report['headerPayload']['maxBytes'])} B "
          f"(p95 {fmt_int(p95)} B)")
    print(f"  payload at risk     : {fmt_int(len(at_risk))}")
    print(f"  clusters            : {len(clusters)}")
    print()
    print(f"  json     -> {json_path}")
    print(f"  md       -> {md_path}")
    print(f"  recrawl  -> {recrawl_path}")


if __name__ == "__main__":
    main()
