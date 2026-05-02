# 100-URL header probe — https://data.stateglobe.com

_Run: 2026-05-02 13:15:49 UTC_
_Elapsed: 1.88 s_
_Concurrency cache-busted (one Worker invocation per URL, no edge hits)_

## Crawl health

- URLs probed: **100**
- Status mix: `200`=100
- 5xx pages: **0** (0.0%)
- Network errors: **0** {}
- Latency (ms): min=173 | p50=274 | p95=364 | max=428

## Header coverage

- Pages exposing `X-Internal-Links` (decoded ok): **100** (100.0%)
- Pages exposing `X-Headings` (decoded ok): **100** (100.0%)
- Pages exposing **both**: **100** (100.0%)

## Payload size (bytes)

- `X-Internal-Links` header bytes: min=1375 | p50=1544 | p95=1710 | max=1967
- `X-Headings` header bytes: min=338 | p50=551 | p95=615 | max=662
- Combined (both headers, when present): min=1848 | p50=2085 | p95=2321 | max=2629
- 8 KB budget reference: **8192** B
- Pages where combined headers exceed 8 KB: **0** (0.0%)

## Link payload contents

- Pages with link list: **100**
- Links per page: min=31 | p50=31 | p95=31 | max=31
- Mean links per page: **31.0**

## Heading payload contents

- Pages with headings: **100**
- Headings per page: min=6 | p50=8 | p95=8 | max=8
- Mean headings per page: **7.9**
- Heading-level distribution: H1=100, H2=100, H3=392, H4=200

## Sample successful page

- URL: `https://data.stateglobe.com/syria/digital-ad-spending-statistics`
- HTTP status: `200`
- Latency: `271.5 ms`
- Link count: `31` (header bytes: 1455)
- Heading count: `8` (header bytes: 534)
- First few headings:
    - H1: Digital Ad Spending Statistics in Syria (2026)
    - H2: Frequently Asked Questions
    - H3: What are the main digital advertising platforms used in Syria?
    - H3: How is the digital ad market expected to evolve in Syria?
    - H4: Methodology
- First few links: `/, /blog, /articles, /analytics, /category/digital-advertising`

## Failing pages (first 10)

- None.

