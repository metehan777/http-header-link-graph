---
title: "I crawled 65,000 pages of my own site without parsing a single line of HTML"
description: "A weekend experiment where I shoved my entire internal link graph into HTTP response headers, crawled the whole site in 99 seconds, and accidentally discovered a new way to think about SEO, AEO, and GEO."
date: 2026-05-02
author: Metehan Yesilyurt
tags: [seo, aeo, geo, crawling, cloudflare, http-headers, technical-seo]
---

# I crawled 65,000 pages of my own site without parsing a single line of HTML

I have been thinking about a stupid question for months.

> What is the smallest, fastest, most boring thing a website can do to make itself perfectly understood by every crawler, scraper, and LLM that visits it?

Robots.txt? Sitemaps? `llms.txt`? Schema.org? They all help. They all also assume the same thing: that a crawler is going to download your HTML, render it, parse it, and then politely figure out what you meant.

That's a lot of trust to place in a stranger's parser.

So this weekend I tried something different. I took my entire internal link graph — every page, every connection — encoded it as compact JSON, base64url-ed it, and stuffed it directly into an HTTP response header on every single page of my site.

Not in the body. Not in a sidecar file. **In the headers.**

Then I crawled 65,000 pages in **99 seconds** without parsing one byte of HTML.

This post is the story of that experiment, what I learned, and why I now think this is a genuinely new lens on SEO, AEO, and GEO that nobody is talking about.

---

## The idea in one sentence

> HTTP responses can carry **8 KB to 32 KB** of headers depending on the server config. That is more than enough room to ship structured site metadata next to every page — for free, on every request, before a single byte of body is sent.

Once that clicks, a lot of things change.

A WAF blocks the body? The headers still went out.  
A page returns 403? The headers still went out.  
A page returns 500? The headers still went out.  
The page is JavaScript-rendered? The headers still went out before the JS even loaded.

Headers travel light, headers travel first, and headers travel even when everything else falls apart. That makes them a beautiful place to publish things you actually want crawlers to see.

---

## What I actually built

A toy. On purpose.

A small Cloudflare Worker that serves a real site, but on every response it adds:

```http
X-Internal-Links: eyJ2IjoxLCJ1cmwiOiIvIiwibGlua3MiOlsiL3ByaWNpbmciLCIvZG9jcyIsIi9ibG9nIiwiL2NvbnRhY3QiXX0
X-Internal-Links-Encoding: base64url-json
X-Internal-Links-Bytes: 4604
X-Internal-Links-Count: 230
Access-Control-Expose-Headers: X-Internal-Links, X-Internal-Links-Encoding, X-Internal-Links-Count, X-Internal-Links-Bytes
```

That string in the first header is just `{"v":1,"url":"/","links":["/pricing","/docs","/blog","/contact"]}` after base64url encoding. Decode it, and you have the full list of internal links from that page. No DOM, no Readability, no Playwright, no Cheerio. Check on https://www.base64decode.org/

Then I deployed it to a real-ish site I own (`data.stateglobe.com`, 65k+ pages) and asked one question:

> If I act like a scraper, with no HTML parsing at all, can I rebuild the entire internal link graph using only the response headers?

Spoiler: yes. And it turned out to be the fastest, cleanest, most surprising crawl I have ever run.

---

## The first crawl: a brutal honesty check

The first run was painful, in the way that good experiments always are.

I wrote a Rust crawler with `tokio` and `reqwest` (because once you taste 1,000 RPS you cannot go back), pointed it at the live site with 800 concurrent connections, and watched it grind away.

```
elapsed_human   = 14m 14s
rps             = 76
unique_pages    = 65,292
```

76 requests per second. For a static-feeling site. Ouch.

The bottleneck was obvious once I looked: every request was hitting my Worker, executing logic, and serializing the link payload from scratch. Cloudflare's edge cache was sitting there, completely empty, watching me re-render the same HTML for the millionth time.

So I did the smallest possible fix. I taught the Worker to use `caches.default.put` and added a sane `Cache-Control: public, s-maxage=7200, max-age=300` to every response. Then I purged the edge cache once to flush the stale stuff.

Then I ran it again.

```
elapsed_human   = 1m 39s
rps             = 660 (peaks at 969)
unique_pages    = 65,292
```

**8.6× speedup.** Same site, same crawler, same 65,000 pages — now in under two minutes. Once Cloudflare's edge had a copy of every response with my custom headers attached, the Worker barely had to lift a finger.

That alone was a lesson worth the weekend. But the real fun was about to start.

---

## What happens when you don't need HTML to crawl

This is where the experiment stopped being a benchmark and started being a worldview.

When you crawl by parsing HTML, you are at the mercy of:

- The DOM rendering correctly
- JavaScript executing in the right order
- Anti-bot challenges
- Cloudflare interstitials
- Lazy-loaded content
- Dynamic links inside iframes
- That one guy on the team who hid the nav inside a `<canvas>` for fun

When you crawl by reading headers, none of that matters. You make a request. You read 4 KB of headers. You move on.

My Rust crawler didn't even need to download the full body. It is literally streaming response headers, decoding a base64 string, and skipping the rest. That's why 1 KB of header outperforms 50 KB of HTML — because the crawler never asked for the 50 KB.

It also means a few wild things become possible:

1. **You can fully describe a page from a 403 or 500.** If your WAF blocks abusive bots from reading the body, fine — the headers can still ship the link list to your own SEO tooling, your own monitoring, or trusted partners. (To be very clear: I am only suggesting this for sites *you own*. Don't go ramming this into someone else's WAF and call it a day.)
2. **You can publish crawl metadata that the body cannot easily express.** Want to expose canonical, language alternates, content checksum, last-edited timestamp, model-readable summary? Headers are a clean place for that.
3. **You can A/B-test crawler behavior without touching content.** Add a header. Remove a header. Watch what the bots do. The body never changes.

Once you accept that headers are a first-class publishing surface, you start asking better questions.

---

## Then I built SEO insights from headers alone

Here is the part I did not expect.

After the second crawl finished, I had 65,000 JSON payloads sitting in a file. Each one was just `{ url, links: [...] }`. That's it. No HTML.

Out of curiosity, I wrote a small Python script that did one thing: build the full directed graph from those headers and compute SEO metrics on it.

What came back floored me:

- **27,372 pages (41.9%) had zero inbound internal links.** Pure orphans, only reachable through the sitemap. On a site that was supposed to be tightly cross-linked.
- **27,659 pages were unreachable by walking from `/`.** Same root cause.
- **Click-depth distribution:** 54% of pages were 4+ clicks deep from the homepage. 53% sat at depth 6.
- **Gini coefficient on inbound links: 0.918.** That's near-monopolistic. The top 10 pages were hoovering up 17% of all internal links.
- **Cluster inequality.** The country sub-folders had identical structure — 301 pages each — but median inbound count was **199 for some countries** and **1 for others**. Same template, same code, wildly different link distribution.

I have used Screaming Frog, Sitebulb, Ahrefs, OnCrawl, JetOctopus. They are all great. But this hit different. Because it took 99 seconds, cost approximately nothing, and surfaced a structural problem that no amount of "let's audit the homepage" would have caught.

The crawl wasn't the product. The crawl was just the *delivery vehicle* for a much cleaner SEO insight pipeline.

---

## Why this matters for SEO, AEO, and GEO

This is the part that got me genuinely excited, because it generalizes.

I started with internal links. But the header is an envelope. You can put almost anything structured inside it.

### For traditional SEO

- **Internal link graph** (what I tested): orphans, dead-ends, click-depth, hub identification, Gini concentration.
- **Canonical hints, hreflang, content hash**: ride along with every response, no extra request.
- **Last-edited timestamp**: helps crawlers decide whether to revisit. Cheaper than `Last-Modified` games.
- **Page tier signal**: tell crawlers "this is a tier-1 hub" or "this is a tier-3 long-tail page" so they can budget accordingly.

### For AEO (Answer Engine Optimization)

This is where it gets fun. AI engines like Perplexity, ChatGPT, Gemini, and the new wave of search agents are extremely sensitive to *clarity of structure*. They reward sites where the topic, the headings, and the link relationships are obvious.

Imagine a header that ships:

```http
X-Page-Headings: base64url-json (a flat list of H1/H2/H3 with anchor IDs)
X-Page-Topic: "ecommerce conversion benchmarks Bangladesh 2026"
X-Page-Entities: ["Bangladesh", "ecommerce", "conversion rate", "2026"]
X-Page-Summary: base64url(280-char abstract)
```

Now any answer engine that fetches your URL gets a perfect, parser-free abstract before the body even arrives. You are not begging the crawler to "understand" your page. You are *handing* it the understanding.

### For GEO (Generative Engine Optimization)

GEO is about being the source that LLMs choose to cite. Citation rate is correlated with how easily the model can extract a high-confidence factual snippet. Headers are perfect for that:

```http
X-Cite-Snippet: base64url("In 2026, mobile commerce in Bangladesh reached 47.2% of total online retail.")
X-Cite-Source-Of: "internal-research-q1-2026"
X-Cite-License: "CC-BY-4.0"
```

You're publishing a machine-readable "if you quote this page, here is the canonical snippet to use, and here is the license." That is way more useful for an LLM than scraping a paragraph and hoping it picked the right sentence.

### For technical SEO operations

- **Crawl prioritization at the edge.** Inject `X-Crawl-Priority: high` on hub pages. A friendly crawler can use it to budget its requests.
- **Crawl change detection.** A hash of the page's link graph in the header lets you detect navigation drift between deploys without comparing HTML.
- **Independence from rendering.** Edge headers are added by your platform, not your CMS. So even if marketing forgets to add canonical tags, the platform still publishes the truth.

---

## The architecture, in three boring sentences

1. Generate a small JSON payload per page describing its links/headings/topic. Cache it.
2. On every response, attach the payload as a base64url-encoded HTTP header. Cap the size to keep total headers under your origin's limit (8–32 KB).
3. Cache the response at the edge so the second crawl is essentially free.

That's it. There is no machine learning, no fancy infra, no protocol change. It is the kind of thing a single dev can ship in an afternoon.

---

## What I learned the hard way

A few honest notes for anyone who wants to try this:

- **Workers don't cache by default.** I had to explicitly opt in with `caches.default.put` and a real `Cache-Control` header. Without that, my first run was a 76 RPS crawl. With it, I peaked at 969 RPS.
- **Edge caches are sticky.** After deploying the cache logic, I still saw missing headers from old cached responses until I manually purged. Always purge once after a header-shape change.
- **HEAD requests are not your friend.** I tried switching to HEAD to skip body bytes. Cloudflare and many origins respond differently to HEAD, and I lost the headers. Stick with GET; the body is cheap to drop on the client side.
- **Header size is finite.** I capped my payload at 8 KB and clip the link list if a page has too many links. My p95 ended up at 1.7 KB and max at 4.6 KB, well under budget. But for huge nav trees, you will need a "next-payload" pointer header.
- **Treat missing headers as crawl-incomplete, not as data.** When my first insights pass tagged 256 pages as "dead ends," they were actually pages where the header was missing on that fetch. Re-crawl those before drawing conclusions, otherwise you will mis-diagnose your own site.
- **Scope this to your own domains.** This entire approach assumes you own the site and intentionally want to publish crawl metadata. It is not a bypass tool, it is a publishing tool.

---

## The bigger picture: headers as a publishing surface

Most of SEO has trained us to think of "the content" as the thing on the page. The body. The DOM. The words.

But every HTTP response is actually two things stacked on top of each other:

```
Headers   ← machine-readable, fast, structured, almost free
Body      ← human-readable, slow, unstructured, expensive
```

For 30 years, we shoved everything into the body and asked machines to figure it out. Crawlers got stronger, parsers got smarter, and we kept paying the cost of that translation, every single request, forever.

Meanwhile, the headers above the body — the most efficient part of every HTTP response on the planet — were sitting there carrying `Content-Type: text/html` and not much else.

I think that is going to change. As LLMs and answer engines become the dominant consumers of the web, they will reward sites that publish *structured intent*. And there is no faster, cheaper, more universal place to publish structured intent than HTTP headers.

You don't need a new protocol. You don't need a new standard. You don't need WebSub or ActivityPub or some W3C committee. You just need to put the JSON in the header and serve it.

The crawler does the rest in milliseconds.

---

## Where I'm taking this next

A few directions I'm exploring this week:

1. **Heading graph in headers.** Same technique, but ship a flat list of H1–H3 with their anchor IDs. Now you have a deep-link map per page, perfect for AEO.
2. **Topic embeddings as a header.** A 256-dim quantized vector base64'd. LLMs can compare pages without reading them.
3. **A crawl-budget protocol.** A header that says "I have 65,000 pages, 240 hubs, last full re-index was 14 hours ago." Let crawlers use that to decide how aggressively to revisit.
4. **A public "header crawler" CLI.** Take any site I own, pass it a base URL, and watch a 99-second full audit happen. The Rust crawler I wrote already does this internally. I want to clean it up and open-source it.

If any of this excites you, ping me. This is the kind of weekend rabbit hole that is genuinely more fun with collaborators.

---

## Closing thought

I went into this experiment thinking I would solve a crawling problem.

I came out thinking I had stumbled onto a different way of looking at the entire SEO/AEO/GEO stack: **the header is the most under-used publishing surface on the web, and it is going to matter more, not less, as machines do more of the reading.**

65,000 pages. 99 seconds. No HTML parsed. A 41% orphan rate I would have never caught with traditional tools. And about a hundred new ideas for what to put in a header next.

If you have a site you own and an afternoon to spare, try it. The first thing you will notice is how *small* the change is. The second thing you will notice is how much it changes how you think about your site.

— Metehan
