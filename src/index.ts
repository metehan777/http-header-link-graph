type Page = {
  path: string;
  title: string;
  description: string;
  links: string[];
};

type LinkHeaderPayload = {
  v: 1;
  url: string;
  links: string[];
};

const HEADER_NAME = "X-Internal-Links";
const HEADER_ENCODING = "json+base64url";
const MAX_HEADER_BYTES = 8 * 1024;

const PAGES: Page[] = [
  {
    path: "/",
    title: "Header Link Demo",
    description: "A controlled Cloudflare Workers demo for carrying crawl hints in response headers.",
    links: ["/pricing", "/docs", "/blog", "/contact"]
  },
  {
    path: "/pricing",
    title: "Pricing",
    description: "A pretend pricing page with links to product docs and contact.",
    links: ["/", "/docs", "/contact"]
  },
  {
    path: "/docs",
    title: "Docs",
    description: "Implementation notes for reading internal links from headers.",
    links: ["/", "/blog", "/pricing"]
  },
  {
    path: "/blog",
    title: "Blog",
    description: "A pretend blog page that points readers deeper into the site.",
    links: ["/", "/docs", "/contact"]
  },
  {
    path: "/contact",
    title: "Contact",
    description: "A pretend contact page with navigation back into the site.",
    links: ["/", "/pricing", "/docs"]
  }
];

const PAGE_BY_PATH = new Map(PAGES.map((page) => [page.path, page]));

export default {
  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/client-probe.js") {
      return new Response(CLIENT_PROBE_JS, {
        headers: {
          "content-type": "application/javascript; charset=utf-8",
          "cache-control": "no-store"
        }
      });
    }

    const page = PAGE_BY_PATH.get(normalizePath(url.pathname));
    if (!page) {
      return withLinkHeaders(
        new Response(renderNotFound(), {
          status: 404,
          headers: { "content-type": "text/html; charset=utf-8" }
        }),
        { v: 1, url: url.pathname, links: PAGES.map((item) => item.path) }
      );
    }

    const payload = { v: 1, url: page.path, links: page.links } satisfies LinkHeaderPayload;
    const blockMode = url.searchParams.get("block") === "1";

    if (blockMode) {
      return withLinkHeaders(
        new Response(renderBlocked(page), {
          status: 403,
          headers: { "content-type": "text/html; charset=utf-8" }
        }),
        payload
      );
    }

    return withLinkHeaders(
      new Response(renderPage(page), {
        headers: { "content-type": "text/html; charset=utf-8" }
      }),
      payload
    );
  }
} satisfies ExportedHandler;

function normalizePath(pathname: string): string {
  if (pathname !== "/" && pathname.endsWith("/")) {
    return pathname.slice(0, -1);
  }

  return pathname;
}

function withLinkHeaders(response: Response, payload: LinkHeaderPayload): Response {
  const headers = new Headers(response.headers);
  const encoded = encodePayload(payload);

  headers.set(HEADER_NAME, encoded.value);
  headers.set("X-Internal-Links-Encoding", HEADER_ENCODING);
  headers.set("X-Internal-Links-Bytes", String(encoded.bytes));
  headers.set("X-Internal-Links-Count", String(payload.links.length));
  headers.set("Access-Control-Expose-Headers", [
    HEADER_NAME,
    "X-Internal-Links-Encoding",
    "X-Internal-Links-Bytes",
    "X-Internal-Links-Count"
  ].join(", "));
  headers.append("Vary", "Accept");

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers
  });
}

function encodePayload(payload: LinkHeaderPayload): { value: string; bytes: number } {
  const encoder = new TextEncoder();
  let compactPayload = payload;
  let value = base64UrlEncode(encoder.encode(JSON.stringify(compactPayload)));
  let bytes = encoder.encode(value).byteLength;

  while (bytes > MAX_HEADER_BYTES && compactPayload.links.length > 0) {
    compactPayload = { ...compactPayload, links: compactPayload.links.slice(0, -1) };
    value = base64UrlEncode(encoder.encode(JSON.stringify(compactPayload)));
    bytes = encoder.encode(value).byteLength;
  }

  return { value, bytes };
}

function base64UrlEncode(bytes: Uint8Array): string {
  let binary = "";

  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }

  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/u, "");
}

function renderPage(page: Page): string {
  return html`
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>${escapeHtml(page.title)}</title>
        <style>${STYLE}</style>
      </head>
      <body>
        <main>
          ${renderNav()}
          <section class="card">
            <p class="eyebrow">Cloudflare Workers demo</p>
            <h1>${escapeHtml(page.title)}</h1>
            <p>${escapeHtml(page.description)}</p>
            <p>
              This page returns internal crawl hints in the <code>${HEADER_NAME}</code>
              response header. The same header is also attached to a simulated blocked response.
            </p>
            <div class="actions">
              <a href="${page.path}?block=1">Open simulated 403</a>
              <button id="probe" type="button">Read 403 headers with JS</button>
            </div>
          </section>
          <section class="card">
            <h2>Visible links</h2>
            <ul>
              ${page.links.map((link) => `<li><a href="${link}">${escapeHtml(labelFor(link))}</a></li>`).join("")}
            </ul>
          </section>
          <section class="card">
            <h2>Header probe</h2>
            <pre id="probe-output">Click "Read 403 headers with JS" to fetch ${page.path}?block=1 and decode the header.</pre>
          </section>
        </main>
        <script src="/client-probe.js"></script>
      </body>
    </html>
  `;
}

function renderBlocked(page: Page): string {
  return html`
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>403 - ${escapeHtml(page.title)}</title>
        <style>${STYLE}</style>
      </head>
      <body>
        <main>
          <section class="card blocked">
            <p class="eyebrow">Simulated block</p>
            <h1>403</h1>
            <p>The body is intentionally blocked, but this response still includes <code>${HEADER_NAME}</code>.</p>
          </section>
        </main>
      </body>
    </html>
  `;
}

function renderNotFound(): string {
  return html`
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8" />
        <meta name="viewport" content="width=device-width, initial-scale=1" />
        <title>404</title>
        <style>${STYLE}</style>
      </head>
      <body>
        <main>
          <section class="card">
            <h1>404</h1>
            <p>Unknown page. The response header still points at the known internal pages.</p>
          </section>
        </main>
      </body>
    </html>
  `;
}

function renderNav(): string {
  return html`
    <nav>
      ${PAGES.map((page) => `<a href="${page.path}">${escapeHtml(labelFor(page.path))}</a>`).join("")}
    </nav>
  `;
}

function labelFor(path: string): string {
  if (path === "/") {
    return "Home";
  }

  const page = PAGE_BY_PATH.get(path);
  return page?.title ?? path;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function html(strings: TemplateStringsArray, ...values: string[]): string {
  return String.raw({ raw: strings }, ...values);
}

const CLIENT_PROBE_JS = `
const button = document.querySelector("#probe");
const output = document.querySelector("#probe-output");

function decodePayload(value) {
  if (!value) return null;
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized.padEnd(normalized.length + ((4 - (normalized.length % 4)) % 4), "=");
  return JSON.parse(new TextDecoder().decode(Uint8Array.from(atob(padded), (char) => char.charCodeAt(0))));
}

button?.addEventListener("click", async () => {
  output.textContent = "Fetching blocked response...";
  const response = await fetch(location.pathname + "?block=1", { cache: "no-store" });
  const header = response.headers.get("${HEADER_NAME}");
  const payload = decodePayload(header);

  output.textContent = JSON.stringify({
    status: response.status,
    headerBytes: response.headers.get("X-Internal-Links-Bytes"),
    decodedHeader: payload
  }, null, 2);
});
`;

const STYLE = `
  :root {
    color-scheme: light dark;
    font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    line-height: 1.5;
  }

  body {
    margin: 0;
    background: #0f172a;
    color: #e2e8f0;
  }

  main {
    width: min(880px, calc(100% - 32px));
    margin: 40px auto;
  }

  nav {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    margin-bottom: 24px;
  }

  a,
  button {
    border: 1px solid #475569;
    border-radius: 999px;
    background: #1e293b;
    color: #f8fafc;
    display: inline-flex;
    font: inherit;
    padding: 10px 14px;
    text-decoration: none;
  }

  button {
    cursor: pointer;
  }

  .actions {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
  }

  .card {
    background: #111827;
    border: 1px solid #334155;
    border-radius: 24px;
    margin: 16px 0;
    padding: 24px;
  }

  .blocked {
    border-color: #f97316;
  }

  .eyebrow {
    color: #38bdf8;
    font-size: 0.8rem;
    font-weight: 700;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }

  code,
  pre {
    background: #020617;
    border: 1px solid #1e293b;
    border-radius: 12px;
    color: #bae6fd;
  }

  code {
    padding: 2px 6px;
  }

  pre {
    overflow: auto;
    padding: 16px;
  }
`;
