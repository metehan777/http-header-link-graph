const baseUrl = new URL(process.env.BASE_URL ?? "http://127.0.0.1:8787");
const headerName = "x-internal-links";

const visited = new Set();
const queue = ["/"];

while (queue.length > 0) {
  const path = queue.shift();

  if (!path || visited.has(path)) {
    continue;
  }

  visited.add(path);

  const normalResponse = await fetchUrl(path);
  const blockedResponse = await fetchUrl(`${path}?block=1`);
  const blockedPayload = decodeHeader(blockedResponse.headers.get(headerName));

  console.log([
    `${path}`,
    `normal=${normalResponse.status}`,
    `blocked=${blockedResponse.status}`,
    `headerBytes=${blockedResponse.headers.get("x-internal-links-bytes")}`,
    `links=${blockedPayload.links.join(",")}`
  ].join(" "));

  for (const link of blockedPayload.links) {
    if (!visited.has(link)) {
      queue.push(link);
    }
  }
}

console.log(`crawled=${visited.size} pages`);

async function fetchUrl(path) {
  const url = new URL(path, baseUrl);
  return fetch(url, {
    redirect: "manual",
    headers: {
      "user-agent": "header-link-demo-crawler/0.1"
    }
  });
}

function decodeHeader(value) {
  if (!value) {
    throw new Error(`Missing ${headerName} header`);
  }

  const normalized = value.replace(/-/g, "+").replace(/_/g, "/");
  const padded = normalized.padEnd(normalized.length + ((4 - (normalized.length % 4)) % 4), "=");

  return JSON.parse(Buffer.from(padded, "base64").toString("utf8"));
}
