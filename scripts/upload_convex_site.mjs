// Upload the static web export to Convex storage. The served app then has no
// dependency on this computer or on a temporary tunnel.
import { readFile, readdir } from "node:fs/promises";
import { join, relative, resolve, sep } from "node:path";
import { ConvexHttpClient } from "convex/browser";
import { api } from "../convex/_generated/api.js";

const root = resolve("web/out");
const env = Object.fromEntries(
  (await readFile(".env", "utf8")).split(/\r?\n/)
    .filter(line => /^[A-Z_]+=/i.test(line))
    .map(line => {
      const at = line.indexOf("=");
      return [line.slice(0, at), line.slice(at + 1).trim().replace(/^['"]|['"]$/g, "")];
    }),
);
const url = process.env.CONVEX_URL || env.CONVEX_URL;
const token = process.env.CONVEX_BACKEND_TOKEN || env.CONVEX_BACKEND_TOKEN;
if (!url || !token) throw Error("Convex URL and worker token are required");
const client = new ConvexHttpClient(url);

const types = {
  ".html": "text/html; charset=utf-8", ".txt": "text/plain; charset=utf-8",
  ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
  ".json": "application/json", ".webmanifest": "application/manifest+json",
  ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
};
async function* files(dir) {
  for (const entry of await readdir(dir, { withFileTypes: true })) {
    const path = join(dir, entry.name);
    if (entry.isDirectory()) yield* files(path);
    else if (entry.isFile()) yield path;
  }
}
let count = 0;
for await (const file of files(root)) {
  const path = "/" + relative(root, file).split(sep).join("/");
  const ext = path.slice(path.lastIndexOf("."));
  await client.action(api.site.upload, {
    token, path, content_type: types[ext] || "application/octet-stream",
    bytes_base64: (await readFile(file)).toString("base64"),
  });
  count += 1;
}
console.log(`Uploaded ${count} static files to ${url.replace(".convex.cloud", ".convex.site")}`);
