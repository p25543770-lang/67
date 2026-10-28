const { app, BrowserWindow, shell } = require("electron");
const http = require("http");
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");

let bridge;
let web;
const bridgePort = Number(process.env.DEPTH_STUDIO_PORT || 8765);
const root = path.join(app.getAppPath(), "dist");

function startPythonBridge(startIndex = 0) {
  const script = path.join(process.resourcesPath, "server.py");
  if (!fs.existsSync(script)) return Promise.resolve(false);

  const candidates =
    process.platform === "win32"
      ? [
          ["py", ["-3.11", script]],
          ["py", ["-3", script]],
          ["python", [script]],
        ]
      : [
          ["python3", [script]],
          ["python", [script]],
        ];

  return new Promise((resolve) => {
    let index = startIndex;
    const tryNext = () => {
      if (index >= candidates.length) {
        resolve(false);
        return;
      }
      const [command, args] = candidates[index++];
      let settled = false;
      let child;
      try {
        child = spawn(command, args, {
          cwd: process.resourcesPath,
          windowsHide: true,
          stdio: "ignore",
        });
      } catch {
        tryNext();
        return;
      }
      child.once("spawn", () => {
        settled = true;
        bridge = child;
        resolve(true);
      });
      child.once("error", () => {
        if (!settled) tryNext();
      });
      child.once("exit", () => {
        if (bridge === child) bridge = null;
      });
    };
    tryNext();
  });
}

function startWeb() {
  web = http.createServer((req, res) => {
    const requestUrl = new URL(req.url, "http://127.0.0.1");
    if (requestUrl.pathname.startsWith("/api/")) {
      const proxy = http.request(
        {
          hostname: "127.0.0.1",
          port: bridgePort,
          path: req.url,
          method: req.method,
          headers: req.headers,
        },
        (upstream) => {
          res.writeHead(upstream.statusCode || 502, upstream.headers);
          upstream.pipe(res);
        },
      );
      proxy.on("error", () => {
        if (!res.headersSent) {
          res.writeHead(503, {
            "Content-Type": "application/json; charset=utf-8",
            "Cache-Control": "no-store",
          });
          res.end(
            JSON.stringify({
              error:
                "Локальный мост не запущен. Демо работает без Python; для датчика установите Python и зависимости из README.",
            }),
          );
        }
      });
      req.pipe(proxy);
      return;
    }

    let urlPath;
    try {
      urlPath = decodeURIComponent(requestUrl.pathname);
    } catch {
      res.writeHead(400);
      res.end("Bad request");
      return;
    }
    const relativePath = urlPath === "/" ? "/index.html" : urlPath;
    const file = path.resolve(root, `.${relativePath}`);
    if (
      !file.startsWith(`${root}${path.sep}`) &&
      file !== path.join(root, "index.html")
    ) {
      res.writeHead(403);
      res.end("Forbidden");
      return;
    }
    const types = {
      ".html": "text/html; charset=utf-8",
      ".js": "application/javascript; charset=utf-8",
      ".css": "text/css; charset=utf-8",
      ".svg": "image/svg+xml",
      ".png": "image/png",
    };
    fs.readFile(file, (error, data) => {
      if (error) {
        res.writeHead(404);
        res.end("Not found");
        return;
      }
      res.writeHead(200, {
        "Content-Type": types[path.extname(file)] || "application/octet-stream",
      });
      res.end(data);
    });
  });
  return new Promise((resolve, reject) => {
    web.once("error", reject);
    web.listen(0, "127.0.0.1", () => {
      web.removeListener("error", reject);
      resolve();
    });
  });
}

function waitForBridge(timeoutMs = 2200) {
  return new Promise((resolve) => {
    const started = Date.now();
    const attempt = () => {
      const request = http.get(
        `http://127.0.0.1:${bridgePort}/api/health`,
        (response) => {
          response.resume();
          resolve(response.statusCode === 200);
        },
      );
      request.setTimeout(350, () => request.destroy());
      request.on("error", () => {
        if (Date.now() - started >= timeoutMs) resolve(false);
        else setTimeout(attempt, 120);
      });
    };
    attempt();
  });
}

app.whenReady().then(async () => {
  await startPythonBridge();
  let bridgeReady = await waitForBridge();
  if (!bridgeReady && process.platform === "win32") {
    if (bridge) bridge.kill();
    bridge = null;
    await startPythonBridge(1); // Fall back if the Python 3.11 launcher is unavailable.
    await waitForBridge();
  }
  // If Python/SDK is absent, the UI still opens in demo mode.
  await startWeb();

  const win = new BrowserWindow({
    width: 1440,
    height: 940,
    minWidth: 760,
    minHeight: 620,
    title: "Depth Studio",
    backgroundColor: "#f3f6f2",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("https://")) shell.openExternal(url);
    return { action: "deny" };
  });
  win.loadURL(`http://127.0.0.1:${web.address().port}/`);
});

app.on("window-all-closed", () => app.quit());
app.on("before-quit", () => {
  if (bridge) bridge.kill();
  if (web) web.close();
});
