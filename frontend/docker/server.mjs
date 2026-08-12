/**
 * Minimal Node entry for the Docker/Render deployment.
 * Serves the built client assets and delegates everything else to the
 * TanStack Start SSR handler produced by `vite build`.
 */
import { createServer } from "node:http";
import { createReadStream, existsSync, statSync } from "node:fs";
import { extname, join, normalize } from "node:path";

const port = Number(process.env.PORT ?? 3000);
const clientDir = join(process.cwd(), "dist", "client");
const handler = (await import("./../dist/server/index.mjs")).default;

const mime = {
  ".js": "text/javascript",
  ".mjs": "text/javascript",
  ".css": "text/css",
  ".html": "text/html",
  ".json": "application/json",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".woff2": "font/woff2",
  ".txt": "text/plain",
};

function serveStatic(req, res) {
  const url = new URL(req.url ?? "/", "http://localhost");
  const filePath = join(clientDir, normalize(decodeURIComponent(url.pathname)));
  if (!filePath.startsWith(clientDir) || !existsSync(filePath) || !statSync(filePath).isFile()) {
    return false;
  }
  res.writeHead(200, {
    "Content-Type": mime[extname(filePath)] ?? "application/octet-stream",
    "Cache-Control": url.pathname.startsWith("/assets/")
      ? "public, max-age=31536000, immutable"
      : "public, max-age=3600",
  });
  createReadStream(filePath).pipe(res);
  return true;
}

createServer(async (req, res) => {
  try {
    if (serveStatic(req, res)) return;

    const chunks = [];
    if (req.method !== "GET" && req.method !== "HEAD") {
      for await (const chunk of req) chunks.push(chunk);
    }

    const request = new Request(`http://${req.headers.host ?? "localhost"}${req.url}`, {
      method: req.method,
      headers: req.headers,
      ...(chunks.length ? { body: Buffer.concat(chunks) } : {}),
    });

    // The build targets a fetch-style runtime; static assets are handled above.
    const env = { ASSETS: { fetch: async () => new Response(null, { status: 404 }) } };
    const ctx = { waitUntil() {}, passThroughOnException() {} };
    const response = await handler.fetch(request, env, ctx);
    res.writeHead(response.status, Object.fromEntries(response.headers));
    res.end(response.body ? Buffer.from(await response.arrayBuffer()) : undefined);
  } catch (error) {
    console.error(error);
    res.writeHead(500, { "Content-Type": "text/plain" });
    res.end("Internal Server Error");
  }
}).listen(port, "0.0.0.0", () => {
  console.log(`Myra frontend listening on http://0.0.0.0:${port}`);
});
