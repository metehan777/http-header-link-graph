/**
 * Safe, framework-agnostic helper for attaching the
 * X-Internal-Links and X-Headings response headers without ever
 * busting the origin's response-header size budget.
 *
 * Drop this file into any TypeScript Worker, Next.js middleware,
 * edge function, or Node HTTP server. There are NO runtime deps —
 * `TextEncoder` and `btoa` are available in every modern JS runtime
 * (Workers, Deno, Bun, Node 18+, browsers).
 *
 * Why this exists: if you naively stuff the full link list and
 * heading map into headers, large hub pages can blow past the
 * combined response-header limit (Cloudflare ~16 KB, many origins
 * 8 KB), which makes the platform return HTTP 500 to real users.
 * Don't be me. Use this.
 */

export type LinkEntry = string;

export type HeadingEntry = {
  /** Heading level, 1-6. */
  l: number;
  /** Heading text. */
  t: string;
};

export type AttachOptions = {
  /**
   * Per-header soft cap. When a single header value (after base64url
   * encoding) exceeds this, that header's payload is truncated.
   * Default: 6 KB. Recommend keeping <= 7 KB.
   */
  perHeaderBudgetBytes?: number;

  /**
   * Combined hard cap across BOTH custom headers (after encoding).
   * If both fit, both are emitted. If they don't, headings are
   * truncated first, then links, until the total fits.
   * Default: 12 KB. The remaining 4 KB of a typical 16 KB origin
   * limit is reserved for the rest of the response headers.
   */
  combinedBudgetBytes?: number;

  /** Header names; override only if you have to. */
  headerNames?: {
    links?: string;
    headings?: string;
  };
};

const DEFAULTS = {
  perHeaderBudgetBytes: 6 * 1024,
  combinedBudgetBytes: 12 * 1024,
  headerNames: {
    links: "X-Internal-Links",
    headings: "X-Headings",
  },
} as const;

const ENCODER = new TextEncoder();

/**
 * Attach link and heading payload headers to a Response under a
 * strict combined byte budget. Returns a NEW Response.
 *
 * Truncation strategy:
 *   1. Each individual payload is shrunk to fit `perHeaderBudgetBytes`.
 *   2. If both fit individually but their sum exceeds
 *      `combinedBudgetBytes`, the LARGER header is truncated until
 *      the combined fits, with headings preferred for shrinkage
 *      (link-graph signal is more valuable to crawlers than the full
 *      heading list).
 *   3. If a payload truncates to empty, that header is omitted
 *      entirely instead of emitting an empty value.
 *
 * Truncation status is reported via these headers, so monitoring
 * can alert when budgets are being hit:
 *   X-Internal-Links-Truncated: 1   (only if links list was clipped)
 *   X-Headings-Truncated:       1   (only if heading list was clipped)
 *   X-Internal-Links-Count:     N   (links emitted, original count if not)
 *   X-Internal-Links-Original:  M   (only if truncated)
 *   X-Headings-Count:           N
 *   X-Headings-Original:        M   (only if truncated)
 *   X-Internal-Links-Bytes:     N
 *   X-Headings-Bytes:           N
 */
export function attachStructuralHeaders(
  response: Response,
  payload: { url: string; links: LinkEntry[]; headings?: HeadingEntry[] },
  options: AttachOptions = {}
): Response {
  const opts = {
    perHeaderBudgetBytes:
      options.perHeaderBudgetBytes ?? DEFAULTS.perHeaderBudgetBytes,
    combinedBudgetBytes:
      options.combinedBudgetBytes ?? DEFAULTS.combinedBudgetBytes,
    headerNames: {
      links: options.headerNames?.links ?? DEFAULTS.headerNames.links,
      headings: options.headerNames?.headings ?? DEFAULTS.headerNames.headings,
    },
  };

  const links = payload.links ?? [];
  const headings = payload.headings ?? [];

  const linksFit = fitListUnderBudget(
    links,
    (xs) => encodeLinks(xs),
    opts.perHeaderBudgetBytes
  );
  const headingsFit = fitListUnderBudget(
    headings,
    (xs) => encodeHeadings(xs),
    opts.perHeaderBudgetBytes
  );

  const balanced = balanceCombined(
    linksFit,
    headingsFit,
    opts.combinedBudgetBytes
  );

  const headers = new Headers(response.headers);
  const expose: string[] = [];

  if (balanced.links.encoded.length > 0 && balanced.links.kept.length > 0) {
    headers.set(opts.headerNames.links, balanced.links.encoded);
    headers.set(`${opts.headerNames.links}-Encoding`, "json+base64url");
    headers.set(`${opts.headerNames.links}-Bytes`, String(balanced.links.bytes));
    headers.set(`${opts.headerNames.links}-Count`, String(balanced.links.kept.length));
    if (balanced.links.kept.length < links.length) {
      headers.set(`${opts.headerNames.links}-Truncated`, "1");
      headers.set(`${opts.headerNames.links}-Original`, String(links.length));
    }
    expose.push(
      opts.headerNames.links,
      `${opts.headerNames.links}-Encoding`,
      `${opts.headerNames.links}-Bytes`,
      `${opts.headerNames.links}-Count`,
      `${opts.headerNames.links}-Truncated`,
      `${opts.headerNames.links}-Original`
    );
  }

  if (
    balanced.headings.encoded.length > 0 &&
    balanced.headings.kept.length > 0
  ) {
    headers.set(opts.headerNames.headings, balanced.headings.encoded);
    headers.set(`${opts.headerNames.headings}-Encoding`, "json+base64url");
    headers.set(`${opts.headerNames.headings}-Schema`, "[{l:1-6,t:string}]");
    headers.set(
      `${opts.headerNames.headings}-Bytes`,
      String(balanced.headings.bytes)
    );
    headers.set(
      `${opts.headerNames.headings}-Count`,
      String(balanced.headings.kept.length)
    );
    if (balanced.headings.kept.length < headings.length) {
      headers.set(`${opts.headerNames.headings}-Truncated`, "1");
      headers.set(
        `${opts.headerNames.headings}-Original`,
        String(headings.length)
      );
    }
    expose.push(
      opts.headerNames.headings,
      `${opts.headerNames.headings}-Encoding`,
      `${opts.headerNames.headings}-Schema`,
      `${opts.headerNames.headings}-Bytes`,
      `${opts.headerNames.headings}-Count`,
      `${opts.headerNames.headings}-Truncated`,
      `${opts.headerNames.headings}-Original`
    );
  }

  if (expose.length > 0) {
    const existingExpose = headers.get("Access-Control-Expose-Headers");
    const next = existingExpose
      ? `${existingExpose}, ${expose.join(", ")}`
      : expose.join(", ");
    headers.set("Access-Control-Expose-Headers", next);
  }

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers,
  });
}

type FitResult<T> = {
  kept: T[];
  encoded: string;
  bytes: number;
};

function fitListUnderBudget<T>(
  items: T[],
  encode: (xs: T[]) => string,
  budgetBytes: number
): FitResult<T> {
  if (items.length === 0) {
    return { kept: [], encoded: "", bytes: 0 };
  }
  let kept = items;
  let encoded = encode(kept);
  let bytes = ENCODER.encode(encoded).byteLength;
  while (bytes > budgetBytes && kept.length > 0) {
    const dropCount = Math.max(1, Math.ceil(kept.length * 0.1));
    kept = kept.slice(0, kept.length - dropCount);
    encoded = encode(kept);
    bytes = ENCODER.encode(encoded).byteLength;
  }
  return { kept, encoded, bytes };
}

function balanceCombined<L, H>(
  links: FitResult<L>,
  headings: FitResult<H>,
  combinedBudgetBytes: number
): { links: FitResult<L>; headings: FitResult<H> } {
  let combined = links.bytes + headings.bytes;
  if (combined <= combinedBudgetBytes) {
    return { links, headings };
  }
  let h = headings;
  while (links.bytes + h.bytes > combinedBudgetBytes && h.kept.length > 0) {
    const drop = Math.max(1, Math.ceil(h.kept.length * 0.2));
    const newKept = h.kept.slice(0, h.kept.length - drop);
    const newEncoded =
      (encodeHeadings as unknown as (xs: H[]) => string)(newKept);
    const newBytes = ENCODER.encode(newEncoded).byteLength;
    h = { kept: newKept, encoded: newEncoded, bytes: newBytes };
  }
  if (h.kept.length === 0) {
    h = { kept: [], encoded: "", bytes: 0 };
  }
  let l = links;
  while (l.bytes + h.bytes > combinedBudgetBytes && l.kept.length > 0) {
    const drop = Math.max(1, Math.ceil(l.kept.length * 0.2));
    const newKept = l.kept.slice(0, l.kept.length - drop);
    const newEncoded =
      (encodeLinks as unknown as (xs: L[]) => string)(newKept);
    const newBytes = ENCODER.encode(newEncoded).byteLength;
    l = { kept: newKept, encoded: newEncoded, bytes: newBytes };
  }
  return { links: l, headings: h };
}

function encodeLinks(items: LinkEntry[]): string {
  return base64UrlEncode(ENCODER.encode(JSON.stringify(items)));
}

function encodeHeadings(items: HeadingEntry[]): string {
  return base64UrlEncode(ENCODER.encode(JSON.stringify(items)));
}

function base64UrlEncode(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/u, "");
}
