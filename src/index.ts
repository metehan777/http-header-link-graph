import { attachStructuralHeaders, type HeadingEntry } from "./headers";

type Page = {
  path: string;
  title: string;
  description: string;
  links: string[];
  headings: HeadingEntry[];
};

const PAGES: Page[] = [
  {
    path: "/",
    title: "Header Link Demo",
    description: "Cloudflare Workers demo for carrying crawl hints in response headers.",
    links: ["/pricing", "/docs", "/blog", "/contact"],
    headings: [
      { l: 1, t: "Header Link Demo" },
      { l: 2, t: "What this demo proves" },
      { l: 2, t: "Who this is for" },
      { l: 3, t: "Site owners" },
      { l: 3, t: "SEO and AEO tooling" },
    ],
  },
  {
    path: "/pricing",
    title: "Pricing",
    description: "A pretend pricing page with links to product docs and contact.",
    links: ["/", "/docs", "/contact"],
    headings: [
      { l: 1, t: "Pricing" },
      { l: 2, t: "Plans" },
      { l: 3, t: "Free" },
      { l: 3, t: "Team" },
      { l: 3, t: "Enterprise" },
    ],
  },
  {
    path: "/docs",
    title: "Docs",
    description: "Implementation notes for reading internal links from headers.",
    links: ["/", "/blog", "/pricing"],
    headings: [
      { l: 1, t: "Docs" },
      { l: 2, t: "Quick start" },
      { l: 2, t: "Header schema" },
      { l: 3, t: "X-Internal-Links" },
      { l: 3, t: "X-Headings" },
      { l: 2, t: "Production warnings" },
    ],
  },
  {
    path: "/blog",
    title: "Blog",
    description: "A pretend blog page that points readers deeper into the site.",
    links: ["/", "/docs", "/contact"],
    headings: [
      { l: 1, t: "Blog" },
      { l: 2, t: "Latest posts" },
    ],
  },
  {
    path: "/contact",
    title: "Contact",
    description: "A pretend contact page with navigation back into the site.",
    links: ["/", "/pricing", "/docs"],
    headings: [
      { l: 1, t: "Contact" },
      { l: 2, t: "Email" },
      { l: 2, t: "Office" },
    ],
  },
];

const PAGE_BY_PATH = new Map(PAGES.map((page) => [page.path, page]));

export default {
  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);

    if (url.pathname === "/client-probe.js") {
      return new Response(CLIENT_PROBE_JS, {
        headers: {
          "content-type": "application/javascript; charset=utf-8",
          "cache-control": "no-store",
        },
      });
    }

    const page = PAGE_BY_PATH.get(normalizePath(url.pathname));
    if (!page) {
      return attachStructuralHeaders(
        new Response(renderNotFound(), {
          status: 404,
          headers: { "content-type": "text/html; charset=utf-8" },
        }),
        {
          url: url.pathname,
          links: PAGES.map((item) => item.path),
          headings: [{ l: 1, t: "Not Found" }],
        }
      );
    }

    const blockMode = url.searchParams.get("block") === "1";
    const stress = url.searchParams.get("stress") === "1";

    const links = stress ? makeStressLinks() : page.links;
    const headings = stress ? makeStressHeadings() : page.headings;

    if (blockMode) {
      return attachStructuralHeaders(
        new Response(renderBlocked(page), {
          status: 403,
          headers: { "content-type": "text/html; charset=utf-8" },
        }),
        { url: page.path, links, headings }
      );
    }

    return attachStructuralHeaders(
      new Response(renderPage(page), {
        headers: { "content-type": "text/html; charset=utf-8" },
      }),
      { url: page.path, links, headings }
    );
  },
} satisfies ExportedHandler;

function normalizePath(pathname: string): string {
  if (pathname !== "/" && pathname.endsWith("/")) {
    return pathname.slice(0, -1);
  }
  return pathname;
}

/**
 * Pathological inputs to verify the budget cap works:
 * /?stress=1 should NEVER 500, no matter how big these get.
 */
function makeStressLinks(): string[] {
  const out: string[] = [];
  for (let i = 0; i < 5000; i++) {
    out.push(`/very-long-pathname-segment-${i}-for-stress-testing-budget-cap`);
  }
  return out;
}

function makeStressHeadings(): HeadingEntry[] {
  const out: HeadingEntry[] = [];
  for (let i = 0; i < 5000; i++) {
    out.push({
      l: ((i % 5) + 2) as 2 | 3 | 4 | 5 | 6,
      t: `Stress heading ${i} with extra padding to push the size higher than any reasonable header budget`,
    });
  }
  return out;
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
              This page returns internal crawl hints in the <code>X-Internal-Links</code>
              and <code>X-Headings</code> response headers. The same headers are also
              attached to a simulated blocked response. Try
              <a href="${page.path}?stress=1">${page.path}?stress=1</a> to see budget
              caps prevent a 500.
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
            <pre id="probe-output">Click "Read 403 headers with JS" to fetch ${page.path}?block=1 and decode the headers.</pre>
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
            <p>The body is intentionally blocked, but this response still includes <code>X-Internal-Links</code> and <code>X-Headings</code>.</p>
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
            <p>Unknown page. The response headers still point at the known internal pages.</p>
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
  if (path === "/") return "Home";
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
  const links = decodePayload(response.headers.get("X-Internal-Links"));
  const headings = decodePayload(response.headers.get("X-Headings"));
  output.textContent = JSON.stringify({
    status: response.status,
    linksBytes: response.headers.get("X-Internal-Links-Bytes"),
    linksCount: response.headers.get("X-Internal-Links-Count"),
    linksTruncated: response.headers.get("X-Internal-Links-Truncated"),
    headingsBytes: response.headers.get("X-Headings-Bytes"),
    headingsCount: response.headers.get("X-Headings-Count"),
    headingsTruncated: response.headers.get("X-Headings-Truncated"),
    links,
    headings,
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
