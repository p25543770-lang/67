const test = require("node:test");
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const listen = (server) => new Promise((resolve, reject) => {
  server.once("error", reject);
  server.listen(0, "127.0.0.1", () => resolve(server.address().port));
});
const close = (server) => new Promise((resolve) => server.close(resolve));

function send(port) {
  return new Promise((resolve, reject) => {
    const req = http.request({ hostname: "127.0.0.1", port, path: "/api/devices/tilt", method: "POST",
      headers: { Host: "depth.local:8080", Origin: "http://depth.local:8080",
        "Content-Type": "application/json", "X-Depth-Studio-Tilt": "1", Connection: "close" } }, (res) => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => { body += chunk; });
      res.on("end", () => resolve({ status: res.statusCode, body: JSON.parse(body) }));
    });
    req.on("error", reject);
    req.setTimeout(5000, () => req.destroy(new Error("proxy test request timed out")));
    req.end('{"source":"kinect_v1_sdk","angle":3}');
  });
}
function echoBridge() {
  return http.createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => { body += chunk; });
    req.on("end", () => {
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ host: req.headers.host, origin: req.headers.origin,
        motorHeader: req.headers["x-depth-studio-tilt"], contentType: req.headers["content-type"], body }));
    });
  });
}
function assertPreserved(result) {
  assert.equal(result.status, 200);
  assert.deepEqual(result.body, { host: "depth.local:8080", origin: "http://depth.local:8080", motorHeader: "1",
    contentType: "application/json", body: '{"source":"kinect_v1_sdk","angle":3}' });
}

// Vite is a development-only proxy; its live HTTP forwarding is exercised on
// Linux CI. Avoid starting its watcher/esbuild subprocess in Windows packaging
// tests, where teardown can hang; the actual Electron proxy is tested below.
test("Vite proxy preserves origin, host and explicit motor header", {
  skip: process.platform === "win32" ? "Vite proxy exercised in Linux CI; Windows uses Electron proxy" : false,
  timeout: 15000,
}, async () => {
  const bridge = echoBridge();
  const port = await listen(bridge);
  const previous = process.env.DEPTH_STUDIO_PORT;
  process.env.DEPTH_STUDIO_PORT = String(port);
  let vite;
  try {
    const { createServer } = await import("vite");
    vite = await createServer({ configFile: path.resolve(__dirname, "../../vite.config.js"),
      server: { host: "127.0.0.1", port: 0, strictPort: true }, logLevel: "silent" });
    await vite.listen();
    assertPreserved(await send(vite.httpServer.address().port));
  } finally {
    if (vite) {
      vite.httpServer?.closeAllConnections();
      await vite.close();
    }
    if (previous === undefined) delete process.env.DEPTH_STUDIO_PORT;
    else process.env.DEPTH_STUDIO_PORT = previous;
    await close(bridge);
  }
});

test("packaged Electron proxy preserves the same headers", { timeout: 15000 }, async () => {
  const bridge = echoBridge();
  const bridgePort = await listen(bridge);
  // Exercise the production function, with Electron's unrelated lifecycle
  // excluded. This makes the actual request forwarding testable in Node CI.
  const source = fs.readFileSync(path.resolve(__dirname, "../main.cjs"), "utf8");
  const start = source.indexOf("function proxyApi(req, res) {");
  const end = source.indexOf("\n\nfunction startWebServer()", start);
  assert.ok(start >= 0 && end > start, "production proxy function was found");
  const proxyApi = vm.runInNewContext(`(${source.slice(start, end)})`, {
    http, URL, bridgePort, bridgeStatus: { running: true },
    sendJson: (res, status, value) => { res.writeHead(status); res.end(JSON.stringify(value)); },
  });
  const proxy = http.createServer(proxyApi);
  try {
    const port = await listen(proxy);
    assertPreserved(await send(port));
  } finally {
    proxy.closeAllConnections();
    bridge.closeAllConnections();
    await close(proxy);
    await close(bridge);
  }
});
