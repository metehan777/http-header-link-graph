#!/usr/bin/env python3
"""
Offline link graph renderer.

Reads seo-header-summary.json from a probe run and writes a self-contained
HTML file with an interactive force-directed graph (D3.js). Defaults to the
top-N hubs plus their neighbors; toggle "Show all" in the UI to render the
full graph.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse


HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>Internal Link Graph</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; background: #0b1020; color: #e2e8f0; font-family: ui-sans-serif, system-ui, sans-serif; }
  header { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; padding: 12px 16px; border-bottom: 1px solid #1e293b; position: sticky; top: 0; background: rgba(11,16,32,0.95); backdrop-filter: blur(8px); z-index: 10; }
  header h1 { margin: 0; font-size: 14px; letter-spacing: 0.06em; text-transform: uppercase; color: #38bdf8; }
  header .stat { font-size: 12px; color: #94a3b8; }
  header .stat strong { color: #e2e8f0; }
  header button, header select, header input { background: #1e293b; color: #f8fafc; border: 1px solid #334155; border-radius: 8px; padding: 6px 10px; font: inherit; cursor: pointer; }
  header button.active { background: #38bdf8; color: #0b1020; border-color: #38bdf8; font-weight: 600; }
  main { display: grid; grid-template-columns: 1fr 320px; height: calc(100vh - 53px); }
  #graph { background: radial-gradient(circle at 50% 40%, #111c34 0%, #0b1020 70%); }
  aside { border-left: 1px solid #1e293b; overflow: auto; padding: 16px; font-size: 13px; }
  aside h2 { margin: 0 0 8px; font-size: 12px; letter-spacing: 0.08em; text-transform: uppercase; color: #38bdf8; }
  aside .url { font-family: ui-monospace, monospace; word-break: break-all; color: #f8fafc; }
  aside .panel { margin-bottom: 16px; padding-bottom: 16px; border-bottom: 1px solid #1e293b; }
  aside ul { padding-left: 16px; margin: 4px 0; }
  aside li { font-family: ui-monospace, monospace; font-size: 12px; color: #cbd5e1; }
  .node-circle { stroke: #0b1020; stroke-width: 1px; cursor: pointer; }
  .node-label { fill: #94a3b8; font-size: 10px; pointer-events: none; }
  .link-line { stroke: #334155; stroke-opacity: 0.45; }
  .link-line.highlight { stroke: #38bdf8; stroke-opacity: 0.9; }
  .node-circle.dim { opacity: 0.15; }
  .link-line.dim { opacity: 0.05; }
</style>
</head>
<body>
<header>
  <h1>Internal Link Graph</h1>
  <span class="stat">site <strong id="stat-host">—</strong></span>
  <span class="stat">crawled <strong id="stat-crawled">0</strong></span>
  <span class="stat">discovered <strong id="stat-discovered">0</strong></span>
  <span class="stat">edges <strong id="stat-edges">0</strong></span>
  <span class="stat">view <strong id="stat-view">top hubs</strong></span>
  <button id="btn-top" class="active">Top hubs</button>
  <button id="btn-all">Show all</button>
  <input id="search" type="search" placeholder="filter URL substring" />
</header>
<main>
  <svg id="graph"></svg>
  <aside>
    <div class="panel">
      <h2>Selection</h2>
      <div id="sel-url" class="url">click any node</div>
    </div>
    <div class="panel">
      <h2>Top hubs (in-degree)</h2>
      <ol id="top-hubs"></ol>
    </div>
    <div class="panel">
      <h2>Outbound</h2>
      <ul id="sel-out"></ul>
    </div>
    <div class="panel">
      <h2>Inbound</h2>
      <ul id="sel-in"></ul>
    </div>
  </aside>
</main>

<script src="https://cdn.jsdelivr.net/npm/d3@7"></script>
<script>
const DATA = __GRAPH_DATA__;

const svg = d3.select("#graph");
const tooltip = d3.select("body").append("div").attr("id", "tooltip");
const aside = {
  url: document.getElementById("sel-url"),
  out: document.getElementById("sel-out"),
  in: document.getElementById("sel-in"),
  topHubs: document.getElementById("top-hubs"),
};

document.getElementById("stat-host").textContent = DATA.host;
document.getElementById("stat-crawled").textContent = DATA.actualUniquePagesCrawled.toLocaleString();
document.getElementById("stat-discovered").textContent = DATA.totalDiscoveredUrls.toLocaleString();
document.getElementById("stat-edges").textContent = DATA.edges.length.toLocaleString();

DATA.topHubs.forEach((hub) => {
  const li = document.createElement("li");
  li.className = "url";
  li.textContent = hub.path + " (" + hub.inDegree + ")";
  li.addEventListener("click", () => focusNode(hub.path));
  aside.topHubs.appendChild(li);
});

let view = "top";
let nodes = [];
let links = [];
let simulation;
let nodeSel;
let linkSel;
let labelSel;

function buildView(mode) {
  view = mode;
  document.getElementById("btn-top").classList.toggle("active", mode === "top");
  document.getElementById("btn-all").classList.toggle("active", mode === "all");
  document.getElementById("stat-view").textContent = mode === "top" ? "top hubs" : "all";

  const allowed = mode === "top" ? new Set(DATA.topView.nodes) : null;
  const filtered = mode === "top" ? DATA.topView : DATA.allView;
  nodes = filtered.nodes.map((path) => ({
    id: path,
    inDegree: DATA.inDegree[path] || 0,
    outDegree: DATA.outDegree[path] || 0,
  }));
  const ids = new Set(nodes.map((n) => n.id));
  links = DATA.edges
    .filter((e) => ids.has(e.s) && ids.has(e.t))
    .map((e) => ({ source: e.s, target: e.t }));

  render();
}

function render() {
  svg.selectAll("*").remove();

  const width = svg.node().clientWidth;
  const height = svg.node().clientHeight;
  svg.attr("viewBox", [0, 0, width, height]);

  const g = svg.append("g");
  svg.call(
    d3.zoom().scaleExtent([0.1, 8]).on("zoom", (event) => {
      g.attr("transform", event.transform);
    })
  );

  linkSel = g.append("g")
    .selectAll("line")
    .data(links)
    .join("line")
    .attr("class", "link-line");

  nodeSel = g.append("g")
    .selectAll("circle")
    .data(nodes)
    .join("circle")
    .attr("class", "node-circle")
    .attr("r", (d) => 3 + Math.sqrt(d.inDegree))
    .attr("fill", (d) => d.inDegree > 50 ? "#f97316" : (d.inDegree > 10 ? "#38bdf8" : "#94a3b8"))
    .on("click", (_, d) => selectNode(d.id))
    .call(drag());

  labelSel = g.append("g")
    .selectAll("text")
    .data(nodes.filter((d) => d.inDegree >= 8))
    .join("text")
    .attr("class", "node-label")
    .text((d) => d.id);

  if (simulation) simulation.stop();
  simulation = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).id((d) => d.id).distance(40).strength(0.4))
    .force("charge", d3.forceManyBody().strength(-30))
    .force("center", d3.forceCenter(width / 2, height / 2))
    .force("collide", d3.forceCollide().radius((d) => 5 + Math.sqrt(d.inDegree)))
    .alpha(1)
    .alphaDecay(0.04)
    .on("tick", () => {
      linkSel
        .attr("x1", (d) => d.source.x)
        .attr("y1", (d) => d.source.y)
        .attr("x2", (d) => d.target.x)
        .attr("y2", (d) => d.target.y);
      nodeSel.attr("cx", (d) => d.x).attr("cy", (d) => d.y);
      labelSel.attr("x", (d) => d.x + 6).attr("y", (d) => d.y + 3);
    });
}

function drag() {
  return d3.drag()
    .on("start", (event, d) => {
      if (!event.active) simulation.alphaTarget(0.3).restart();
      d.fx = d.x; d.fy = d.y;
    })
    .on("drag", (event, d) => { d.fx = event.x; d.fy = event.y; })
    .on("end", (event, d) => {
      if (!event.active) simulation.alphaTarget(0);
      d.fx = null; d.fy = null;
    });
}

function selectNode(path) {
  aside.url.textContent = path;
  aside.out.innerHTML = "";
  aside.in.innerHTML = "";

  const out = DATA.outbound[path] || [];
  const inb = DATA.inbound[path] || [];
  out.forEach((p) => {
    const li = document.createElement("li");
    li.textContent = p;
    li.style.cursor = "pointer";
    li.addEventListener("click", () => focusNode(p));
    aside.out.appendChild(li);
  });
  inb.forEach((p) => {
    const li = document.createElement("li");
    li.textContent = p;
    li.style.cursor = "pointer";
    li.addEventListener("click", () => focusNode(p));
    aside.in.appendChild(li);
  });

  const neighbors = new Set([path, ...out, ...inb]);
  nodeSel.classed("dim", (d) => !neighbors.has(d.id));
  linkSel.classed("highlight", (d) => d.source.id === path || d.target.id === path)
         .classed("dim", (d) => d.source.id !== path && d.target.id !== path);
}

function focusNode(path) {
  const node = nodes.find((n) => n.id === path);
  if (!node && view === "top") {
    buildView("all");
    setTimeout(() => focusNode(path), 80);
    return;
  }
  selectNode(path);
}

document.getElementById("btn-top").addEventListener("click", () => buildView("top"));
document.getElementById("btn-all").addEventListener("click", () => buildView("all"));
document.getElementById("search").addEventListener("input", (e) => {
  const q = e.target.value.trim().toLowerCase();
  if (!q) {
    nodeSel.classed("dim", false);
    linkSel.classed("dim", false).classed("highlight", false);
    return;
  }
  nodeSel.classed("dim", (d) => !d.id.toLowerCase().includes(q));
  linkSel.classed("dim", (d) => !d.source.id.toLowerCase().includes(q) && !d.target.id.toLowerCase().includes(q));
});

buildView("top");
</script>
</body>
</html>
"""


def main() -> None:
    args = parse_args()
    summary_path = Path(args.summary)
    if not summary_path.exists():
        raise SystemExit(f"summary not found: {summary_path}")

    with summary_path.open("r", encoding="utf-8") as file:
        summary = json.load(file)

    base_url = summary.get("baseUrl") or ""
    host = urlparse(base_url).netloc or base_url

    outbound = defaultdict(list)
    inbound = defaultdict(list)
    in_degree: dict[str, int] = defaultdict(int)
    out_degree: dict[str, int] = defaultdict(int)
    edges_set: set[tuple[str, str]] = set()
    nodes: set[str] = set()

    for entry in summary.get("urls", []):
        source = entry.get("url") or ""
        source_path = urlparse(source).path or "/"
        nodes.add(source_path)

        for link in entry.get("links", []) or []:
            target_path = normalize_link(link, base_url)
            if target_path is None or target_path == source_path:
                continue
            edge = (source_path, target_path)
            if edge in edges_set:
                continue
            edges_set.add(edge)
            outbound[source_path].append(target_path)
            inbound[target_path].append(source_path)
            in_degree[target_path] += 1
            out_degree[source_path] += 1
            nodes.add(target_path)

    top_hubs_sorted = sorted(nodes, key=lambda path: in_degree.get(path, 0), reverse=True)
    top_paths = top_hubs_sorted[: args.top]
    neighborhood: set[str] = set(top_paths)

    for path in list(neighborhood):
        for neighbor in outbound.get(path, [])[: args.neighbors_per_hub]:
            neighborhood.add(neighbor)
        for neighbor in inbound.get(path, [])[: args.neighbors_per_hub]:
            neighborhood.add(neighbor)

    edges = [{"s": source, "t": target} for source, target in edges_set]

    payload = {
        "host": host,
        "actualUniquePagesCrawled": summary.get("actualUniquePagesCrawled", 0),
        "totalDiscoveredUrls": summary.get("totalDiscoveredUrls", len(nodes)),
        "topView": {
            "nodes": sorted(neighborhood),
        },
        "allView": {
            "nodes": sorted(nodes),
        },
        "edges": edges,
        "inDegree": dict(in_degree),
        "outDegree": dict(out_degree),
        "outbound": {key: sorted(set(value))[: args.list_limit] for key, value in outbound.items()},
        "inbound": {key: sorted(set(value))[: args.list_limit] for key, value in inbound.items()},
        "topHubs": [
            {"path": path, "inDegree": in_degree.get(path, 0)}
            for path in top_hubs_sorted[: args.top]
        ],
    }

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    html_path = out_dir / "link-graph.html"
    json_path = out_dir / "seo-graph-report.json"

    html_path.write_text(
        HTML_TEMPLATE.replace("__GRAPH_DATA__", json.dumps(payload)),
        encoding="utf-8",
    )

    seo_report = {
        "host": host,
        "actualUniquePagesCrawled": payload["actualUniquePagesCrawled"],
        "totalDiscoveredUrls": payload["totalDiscoveredUrls"],
        "topHubs": payload["topHubs"],
        "topInbound": [
            {"path": path, "inDegree": in_degree[path]}
            for path in top_hubs_sorted[: args.top]
        ],
        "orphanCandidates": [path for path in nodes if not in_degree.get(path)],
        "deadEndCandidates": [path for path in nodes if not out_degree.get(path)],
        "totalNodes": len(nodes),
        "totalEdges": len(edges),
    }
    json_path.write_text(json.dumps(seo_report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"html={html_path}")
    print(f"json={json_path}")
    print(f"nodes={len(nodes)} edges={len(edges)} top_view_nodes={len(neighborhood)}")


def normalize_link(link: str, base_url: str) -> str | None:
    if not isinstance(link, str) or not link:
        return None
    absolute = urljoin(base_url, link)
    parsed = urlparse(absolute)
    if base_url and parsed.netloc and parsed.netloc != urlparse(base_url).netloc:
        return None
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render an interactive internal link graph from a probe summary.")
    parser.add_argument("--summary", required=True, help="Path to seo-header-summary.json")
    parser.add_argument("--out-dir", default="reports/graph", help="Output directory. Default: reports/graph")
    parser.add_argument("--top", type=int, default=2000, help="Top hubs to keep in the default view. Default: 2000")
    parser.add_argument("--neighbors-per-hub", type=int, default=15, help="Neighbors per hub to include in the default view. Default: 15")
    parser.add_argument("--list-limit", type=int, default=200, help="Max items shown in inbound/outbound side panels per node. Default: 200")
    return parser.parse_args()


if __name__ == "__main__":
    main()
