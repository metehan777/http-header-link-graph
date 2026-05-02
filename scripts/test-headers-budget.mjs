#!/usr/bin/env node
/**
 * Verifies that attachStructuralHeaders never lets the combined
 * X-Internal-Links + X-Headings header bytes exceed the configured
 * budget, even with pathological inputs.
 *
 * Run with:
 *   npx tsx scripts/test-headers-budget.mjs
 *   (or compile src/headers.ts first, see comments below)
 *
 * If you don't have tsx installed, install with:
 *   npm install --no-save tsx
 */

import { attachStructuralHeaders } from "../src/headers.ts";

const TESTS = [
  {
    name: "small page (well under budget)",
    links: ["/", "/blog", "/about"],
    headings: [
      { l: 1, t: "Hello" },
      { l: 2, t: "World" },
    ],
    expectTruncated: false,
  },
  {
    name: "moderate page (~30 links, ~10 headings)",
    links: Array.from({ length: 30 }, (_, i) => `/page-${i}`),
    headings: Array.from({ length: 10 }, (_, i) => ({ l: ((i % 5) + 1), t: `H${i}` })),
    expectTruncated: false,
  },
  {
    name: "huge link list (5000 links) — must truncate, must not overflow",
    links: Array.from({ length: 5000 }, (_, i) => `/very-long-pathname-segment-${i}-padding-to-make-each-link-meaningful`),
    headings: Array.from({ length: 50 }, (_, i) => ({ l: ((i % 5) + 1), t: `Heading ${i}` })),
    expectTruncated: true,
  },
  {
    name: "huge heading list (5000 headings) — must truncate, must not overflow",
    links: ["/", "/blog"],
    headings: Array.from({ length: 5000 }, (_, i) => ({ l: ((i % 5) + 1), t: `Some long heading text number ${i} with extra padding` })),
    expectTruncated: true,
  },
  {
    name: "both lists massive — combined cap kicks in",
    links: Array.from({ length: 5000 }, (_, i) => `/path-${i}-with-padding-to-make-it-meaningful`),
    headings: Array.from({ length: 5000 }, (_, i) => ({ l: ((i % 5) + 1), t: `Heading ${i} with padding` })),
    expectTruncated: true,
  },
];

const COMBINED_BUDGET = 12 * 1024;
const PER_HEADER_BUDGET = 6 * 1024;

let passed = 0;
let failed = 0;

for (const test of TESTS) {
  const baseResp = new Response("body", { status: 200 });
  const out = attachStructuralHeaders(baseResp, {
    url: "/",
    links: test.links,
    headings: test.headings,
  });

  const linksHeader = out.headers.get("X-Internal-Links") ?? "";
  const headingsHeader = out.headers.get("X-Headings") ?? "";
  const linksBytes = new TextEncoder().encode(linksHeader).byteLength;
  const headingsBytes = new TextEncoder().encode(headingsHeader).byteLength;
  const combined = linksBytes + headingsBytes;

  const linksTruncated = out.headers.get("X-Internal-Links-Truncated") === "1";
  const headingsTruncated = out.headers.get("X-Headings-Truncated") === "1";
  const wasTruncated = linksTruncated || headingsTruncated;

  const failures = [];
  if (linksBytes > PER_HEADER_BUDGET) {
    failures.push(`links per-header budget exceeded: ${linksBytes} > ${PER_HEADER_BUDGET}`);
  }
  if (headingsBytes > PER_HEADER_BUDGET) {
    failures.push(`headings per-header budget exceeded: ${headingsBytes} > ${PER_HEADER_BUDGET}`);
  }
  if (combined > COMBINED_BUDGET) {
    failures.push(`combined budget exceeded: ${combined} > ${COMBINED_BUDGET}`);
  }
  if (test.expectTruncated && !wasTruncated) {
    failures.push(`expected truncation, got none`);
  }

  if (failures.length === 0) {
    passed++;
    console.log(
      `  ✓ ${test.name}\n    links=${linksBytes}B, headings=${headingsBytes}B, combined=${combined}B, truncated=${wasTruncated}`
    );
  } else {
    failed++;
    console.log(`  ✗ ${test.name}`);
    for (const f of failures) console.log(`    - ${f}`);
    console.log(
      `    links=${linksBytes}B, headings=${headingsBytes}B, combined=${combined}B, truncated=${wasTruncated}`
    );
  }
}

console.log();
console.log(`${passed} passed, ${failed} failed`);
process.exit(failed === 0 ? 0 : 1);
