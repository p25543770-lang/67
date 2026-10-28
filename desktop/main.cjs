"use strict";
/**
 * Electron main process for Depth Studio.
 *
 * Startup order is deliberate:
 *   1. A fast filesystem check decides whether the isolated Python environment
 *      is already prepared.
 *   2. If it is not, the first-run window is shown with steps and progress.
 *      The scanning window is *not* opened as if everything were ready.
 *   3. The Python bridge always runs from %LOCALAPPDATA%\DepthStudio and never
 *      from a system Python found on PATH.
 *   4. Any failure keeps the demo mode available and explains the next step.
 *
 * `DepthStudio.exe --depth-studio-selftest` runs the whole setup headlessly and
 * is used by CI to verify the installer in a clean Windows environment.
 */
const { app, BrowserWindow, ipcMain, shell } = require("electron");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const crypto = require("node:crypto");
const { spawn } = require("node:child_process");
const bootstrap = require("./bootstrap.cjs");
const packageInfo = require("../package.json");

const IS_PACKAGED = app.isPackaged;
const RESOURCES_ROOT = IS_PACKAGED ? process.resourcesPath : path.join(__dirname, "..");
const DIST_ROOT = path.join(app.getAppPath(), "dist");
const BUNDLE_ROOT = IS_PACKAGED ? process.resourcesPath : path.join(__dirname, "..", "python-bundle");
const APP_DATA_ROOT = bootstrap.defaultAppDataRoot({ platform: process.platform, env: process.env });
const ENV_PATHS = bootstrap.resolveEnvironmentPaths({
  appDataRoot: APP_DATA_ROOT,
  platform: process.platform,
});
const PREFERRED_PORT = Number(process.env.DEPTH_STUDIO_PORT || 8765);
const BRIDGE_INSTANCE = crypto.randomBytes(8).toString("hex");
// Тестовые хуки используются только CI-проверками и включаются явно.
const TEST_HOOKS =
  process.env.DEPTH_STUDIO_ALLOW_TEST_HOOKS === "1"
    ? { failStep: process.env.DEPTH_STUDIO_TEST_FAIL_STEP || null }
    : {};

let bridge;
let bridgePort = PREFERRED_PORT;
let bridgeStatus = { running: false, ready: false, error: null, python: null, instance: BRIDGE_INSTANCE };
let web;
let mainWindow = null;
let setupWindow = null;
let setupPromise = null;
let shuttingDown = false;
let quitAfterSetup = false;

const setup = {
  phase: "idle",
  progress: 0,
  steps: bootstrap.STEP_DEFINITIONS.map((step) => ({ ...step, status: "pending", detailText: "" })),
  error: null,
  warnings: [],
  sensorNotes: [],
  envDir: ENV_PATHS.envRoot,
  logFile: ENV_PATHS.setupLog,
  bridgeLog: ENV_PATHS.bridgeLog,
  python: null,
  packages: null,
  cached: false,
  startedAt: null,
  finishedAt: null,
  appVersion: packageInfo.version || "",
};

/* ------------------------------------------------------------------------- */
/* Журналирование и состояние                                                */
/* ------------------------------------------------------------------------- */

function setupPublicState() {
  return {
    phase: setup.phase,
    progress: setup.progress,
    steps: setup.steps,
    error: setup.error,
    warnings: setup.warnings,
    sensorNotes: setup.sensorNotes,
    envDir: setup.envDir,
    logFile: setup.logFile,
    bridgeLog: setup.bridgeLog,
    python: setup.python,
    packages: setup.packages,
    cached: setup.cached,
    startedAt: setup.startedAt,
    finishedAt: setup.finishedAt,
    appVersion: setup.appVersion,
    bridge: {
      running: bridgeStatus.running,
      ready: bridgeStatus.ready,
      error: bridgeStatus.error,
      port: bridgePort,
    },
    demoAvailable: true,
    platform: process.platform,
    arch: process.arch,
  };
}

function broadcast(event) {
  if (setupWindow && !setupWindow.isDestroyed()) {
    setupWindow.webContents.send("setup:event", event);
  }
}

function applySetupEvent(event) {
  if (!event) return;
  if (event.type === "step") {
    const index = setup.steps.findIndex((step) => step.id === event.step.id);
    if (index >= 0) setup.steps[index] = { ...setup.steps[index], ...event.step };
    else setup.steps.push(event.step);
    setup.phase = "running";
    if (typeof event.progress === "number") setup.progress = event.progress;
  } else if (event.type === "done") {
    const result = event.result || {};
    setup.phase = "ready";
    setup.progress = 100;
    if (result.steps?.length) setup.steps = result.steps;
    setup.warnings = result.warnings || [];
    setup.sensorNotes = result.sensorNotes || [];
    setup.python = result.python || null;
    setup.packages = result.packages || null;
    setup.cached = Boolean(result.cached);
    setup.error = null;
    setup.finishedAt = new Date().toISOString();
  } else if (event.type === "error") {
    setup.phase = "failed";
    setup.error = event.error;
    setup.finishedAt = new Date().toISOString();
    if (event.result?.steps?.length) setup.steps = event.result.steps;
    if (event.result?.warnings?.length) setup.warnings = event.result.warnings;
  }
}

function readTail(file, limit = 400) {
  try {
    const content = fs.readFileSync(file, "utf8");
    return content.split(/\r?\n/).filter(Boolean).slice(-limit);
  } catch {
    return [];
  }
}

/* ------------------------------------------------------------------------- */
/* Окна                                                                      */
/* ------------------------------------------------------------------------- */

function createSetupWindow() {
  if (setupWindow && !setupWindow.isDestroyed()) {
    setupWindow.focus();
    return setupWindow;
  }
  setupWindow = new BrowserWindow({
    width: 900,
    height: 780,
    minWidth: 620,
    minHeight: 620,
    title: "Depth Studio — настройка среды",
    backgroundColor: "#f3f6f2",
    show: false,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "setup", "preload.cjs"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  setupWindow.once("ready-to-show", () => setupWindow?.show());
  setupWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("https://")) shell.openExternal(url);
    return { action: "deny" };
  });
  setupWindow.on("closed", () => {
    setupWindow = null;
  });
  setupWindow.loadFile(path.join(__dirname, "setup", "index.html"));
  return setupWindow;
}

async function openMainWindow() {
  await ensureWebServer();
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.focus();
    return mainWindow;
  }
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 940,
    minWidth: 760,
    minHeight: 620,
    title: "Depth Studio",
    backgroundColor: "#f3f6f2",
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow.once("ready-to-show", () => mainWindow?.show());
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith("https://")) shell.openExternal(url);
    return { action: "deny" };
  });
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
  await mainWindow.loadURL(`http://127.0.0.1:${web.address().port}/`);
  void startBridge();
  return mainWindow;
}

async function closeSetupWindow() {
  if (setupWindow && !setupWindow.isDestroyed()) {
    setupWindow.destroy();
    setupWindow = null;
  }
}

/* ------------------------------------------------------------------------- */
/* Настройка среды                                                           */
/* ------------------------------------------------------------------------- */

function runEnvironmentSetup(mode = "retry") {
  if (setupPromise) return setupPromise;
  const force = mode === "repair";
  setup.phase = "running";
  setup.error = null;
  setup.progress = 0;
  setup.startedAt = new Date().toISOString();
  setup.finishedAt = null;
  setup.warnings = [];
  setup.sensorNotes = [];
  setup.steps = bootstrap.STEP_DEFINITIONS.map((step) => ({ ...step, status: "pending", detailText: "" }));
  broadcast({ type: "reset", state: setupPublicState() });

  setupPromise = bootstrap
    .runSetup({
      appDataRoot: APP_DATA_ROOT,
      resourcesRoot: RESOURCES_ROOT,
      bundleRoot: BUNDLE_ROOT,
      platform: process.platform,
      arch: process.arch,
      force,
      appVersion: setup.appVersion,
      testHooks: TEST_HOOKS,
      emit: (event) => {
        applySetupEvent(event);
        broadcast(event);
      },
    })
    .then(async (result) => {
      setupPromise = null;
      if (result.ok) {
        await openMainWindow();
        // Среда могла быть восстановлена уже после открытия окна: мост нужно
        // поднять заново, иначе интерфейс остался бы в деморежиме навсегда.
        if (!bridgeStatus.ready) await startBridge();
        setTimeout(() => {
          void closeSetupWindow();
        }, 1200);
      }
      return result;
    })
    .catch((error) => {
      setupPromise = null;
      applySetupEvent({
        type: "error",
        error: {
          code: "unexpected",
          message: "Неожиданная ошибка при настройке среды.",
          hint: "Откройте журнал установки и нажмите «Восстановить среду».",
          details: String(error?.stack || error),
        },
      });
      broadcast({ type: "error", error: setup.error });
      return null;
    });

  return setupPromise;
}

/* ------------------------------------------------------------------------- */
/* Python-мост                                                               */
/* ------------------------------------------------------------------------- */

function findFreePort(start) {
  return new Promise((resolve) => {
    const attempt = (port, remaining) => {
      const probe = http.createServer();
      probe.once("error", () => {
        if (remaining <= 0) resolve(start);
        else attempt(port + 1, remaining - 1);
      });
      probe.listen(port, "127.0.0.1", () => {
        const address = probe.address();
        probe.close(() => resolve(address?.port || port));
      });
    };
    attempt(start, 12);
  });
}

function waitForBridgeHealth(token, timeoutMs = 45_000) {
  return new Promise((resolve) => {
    const started = Date.now();
    const attempt = () => {
      const request = http.get(`http://127.0.0.1:${bridgePort}/api/health`, (response) => {
        let body = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => {
          body += chunk;
        });
        response.on("end", () => {
          let payload = null;
          try {
            payload = JSON.parse(body);
          } catch {
            payload = null;
          }
          if (response.statusCode === 200 && payload?.instance === token) resolve(payload);
          else retry();
        });
      });
      request.setTimeout(1200, () => request.destroy());
      request.on("error", retry);
    };
    const retry = () => {
      if (Date.now() - started >= timeoutMs) resolve(null);
      else setTimeout(attempt, 350);
    };
    attempt();
  });
}

/**
 * The embeddable interpreter uses a ._pth file (isolated sys.path), so the
 * application folder must be listed there for `import sensor_drivers` to work.
 * It is refreshed on every launch: the install directory may have changed.
 */
async function prepareInterpreterPaths() {
  const lock = bootstrap.readLock(RESOURCES_ROOT);
  const paths = bootstrap.resolveEnvironmentPaths({
    appDataRoot: APP_DATA_ROOT,
    platform: process.platform,
  });
  return bootstrap.syncInterpreterPaths(paths, lock, [RESOURCES_ROOT]);
}

async function startBridge() {
  if (bridgeStatus.running) return bridgeStatus;
  if (!fs.existsSync(ENV_PATHS.pythonExe)) {
    bridgeStatus = {
      running: false,
      ready: false,
      error:
        "Среда Depth Studio не готова: интерпретатор Python отсутствует. Приложение работает в деморежиме — повторите настройку среды.",
      python: null,
      instance: BRIDGE_INSTANCE,
    };
    return bridgeStatus;
  }
  try {
    await prepareInterpreterPaths();
  } catch (error) {
    bridgeStatus = {
      running: false,
      ready: false,
      error: `Не удалось настроить пути интерпретатора Depth Studio: ${error?.message || error}`,
      python: null,
      instance: BRIDGE_INSTANCE,
    };
    return bridgeStatus;
  }
  const script = path.join(RESOURCES_ROOT, "server.py");
  if (!fs.existsSync(script)) {
    bridgeStatus = {
      running: false,
      ready: false,
      error: "Python-мост не найден в установке (server.py).",
      python: null,
      instance: BRIDGE_INSTANCE,
    };
    return bridgeStatus;
  }

  bridgePort = await findFreePort(PREFERRED_PORT);
  fs.mkdirSync(ENV_PATHS.logsDir, { recursive: true });
  const logStream = fs.createWriteStream(ENV_PATHS.bridgeLog, { flags: "a" });
  logStream.write(
    `\n=== ${new Date().toISOString()} запуск моста из ${ENV_PATHS.pythonExe} на порту ${bridgePort} ===\n`,
  );
  let child;
  try {
    child = spawn(ENV_PATHS.pythonExe, [script], {
      cwd: RESOURCES_ROOT,
      windowsHide: true,
      env: {
        ...bootstrap.childEnvironment(process.env),
        DEPTH_STUDIO_PORT: String(bridgePort),
        DEPTH_STUDIO_ENV_ROOT: ENV_PATHS.envRoot,
        DEPTH_STUDIO_STATE: ENV_PATHS.stateFile,
        DEPTH_STUDIO_REQUIRE_APP_ENV: "1",
        DEPTH_STUDIO_INSTANCE: BRIDGE_INSTANCE,
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
  } catch (error) {
    bridgeStatus = {
      running: false,
      ready: false,
      error: `Не удалось запустить Python-мост из среды Depth Studio: ${error.message}`,
      python: null,
      instance: BRIDGE_INSTANCE,
    };
    return bridgeStatus;
  }

  bridge = child;
  child.stdout.pipe(logStream);
  child.stderr.pipe(logStream);
  bridgeStatus = { running: true, ready: false, error: null, python: null, instance: BRIDGE_INSTANCE };

  child.once("exit", (code) => {
    if (bridge === child) bridge = null;
    if (shuttingDown) return;
    const tail = readTail(ENV_PATHS.bridgeLog, 40).join(" ");
    bridgeStatus = {
      running: false,
      ready: false,
      error: `Python-мост завершился с кодом ${code}. Проверьте журнал моста. ${
        code === 2 ? "Запуск из чужого интерпретатора заблокирован." : ""
      }`,
      python: null,
      instance: BRIDGE_INSTANCE,
      logTail: tail,
    };
  });

  const health = await waitForBridgeHealth(BRIDGE_INSTANCE);
  if (health) {
    bridgeStatus = {
      running: true,
      ready: true,
      error: null,
      python: health.python || null,
      environment: health.environment || null,
      modules: health.modules || null,
      instance: BRIDGE_INSTANCE,
    };
  } else if (bridgeStatus.running) {
    bridgeStatus = {
      ...bridgeStatus,
      ready: false,
      error:
        "Python-мост не отвечает. Деморежим доступен; откройте журнал моста или повторите настройку среды.",
    };
  }
  return bridgeStatus;
}

/* ------------------------------------------------------------------------- */
/* Локальный веб-сервер                                                       */
/* ------------------------------------------------------------------------- */

function readRequestBody(req, limit = 64 * 1024) {
  return new Promise((resolve) => {
    let body = "";
    req.on("data", (chunk) => {
      body += chunk;
      if (body.length > limit) body = body.slice(0, limit);
    });
    req.on("end", () => {
      try {
        resolve(body ? JSON.parse(body) : {});
      } catch {
        resolve({});
      }
    });
    req.on("error", () => resolve({}));
  });
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

function serveStatic(req, res, root, requestUrl) {
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
  if (!file.startsWith(`${root}${path.sep}`) && file !== path.join(root, "index.html")) {
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
    ".woff2": "font/woff2",
    ".json": "application/json; charset=utf-8",
  };
  fs.readFile(file, (error, data) => {
    if (error) {
      res.writeHead(404);
      res.end("Not found");
      return;
    }
    res.writeHead(200, {
      "Content-Type": types[path.extname(file)] || "application/octet-stream",
      "Cache-Control": "no-store",
    });
    res.end(data);
  });
}

function proxyApi(req, res) {
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
    if (res.headersSent) return;
    sendJson(res, 503, {
      error:
        bridgeStatus.error ||
        "Локальный мост не запущен. Деморежим работает без Python; для датчика завершите настройку среды Depth Studio.",
    });
  });
  req.pipe(proxy);
}

function startWebServer() {
  web = http.createServer(async (req, res) => {
    const requestUrl = new URL(req.url, "http://127.0.0.1");

    if (requestUrl.pathname === "/app/setup" || requestUrl.pathname === "/app/setup/") {
      sendJson(res, 200, setupPublicState());
      return;
    }
    if (requestUrl.pathname === "/app/setup/run" && req.method === "POST") {
      const payload = await readRequestBody(req);
      const mode = payload.mode === "repair" ? "repair" : "retry";
      if (setupPromise) {
        sendJson(res, 202, { started: false, reason: "Установка уже выполняется.", state: setupPublicState() });
        return;
      }
      if (!mainWindow && !setupWindow) {
        createSetupWindow();
      }
      void runEnvironmentSetup(mode);
      sendJson(res, 202, { started: true, mode, state: setupPublicState() });
      return;
    }
    if (requestUrl.pathname === "/app/setup/log") {
      sendJson(res, 200, {
        setup: readTail(ENV_PATHS.setupLog, 400),
        bridge: readTail(ENV_PATHS.bridgeLog, 200),
        logFile: ENV_PATHS.setupLog,
        bridgeLog: ENV_PATHS.bridgeLog,
      });
      return;
    }
    if (requestUrl.pathname === "/app/setup/reveal" && req.method === "POST") {
      await revealLogFolder();
      sendJson(res, 200, { ok: true, logDir: ENV_PATHS.logsDir });
      return;
    }
    if (requestUrl.pathname.startsWith("/api/")) {
      proxyApi(req, res);
      return;
    }
    serveStatic(req, res, DIST_ROOT, requestUrl);
  });

  return new Promise((resolve, reject) => {
    web.once("error", reject);
    web.listen(0, "127.0.0.1", () => {
      web.removeListener("error", reject);
      resolve(web.address());
    });
  });
}

async function ensureWebServer() {
  if (web) return web.address();
  return startWebServer();
}

async function revealLogFolder() {
  const target = fs.existsSync(ENV_PATHS.setupLog) ? ENV_PATHS.setupLog : ENV_PATHS.logsDir;
  try {
    fs.mkdirSync(ENV_PATHS.logsDir, { recursive: true });
    if (target.endsWith("setup.log")) shell.showItemInFolder(target);
    else await shell.openPath(target);
  } catch {
    /* журнал — не критичный путь */
  }
}

/* ------------------------------------------------------------------------- */
/* IPC для экрана настройки                                                  */
/* ------------------------------------------------------------------------- */

ipcMain.handle("setup:state", () => ({
  ...setupPublicState(),
  log: readTail(ENV_PATHS.setupLog, 200),
}));

ipcMain.handle("setup:run", async (_event, payload = {}) => {
  if (setupPromise) return { started: false, reason: "Установка уже выполняется." };
  void runEnvironmentSetup(payload.mode === "repair" ? "repair" : "retry");
  return { started: true };
});

ipcMain.handle("setup:reveal-log", async () => {
  await revealLogFolder();
  return { ok: true };
});

ipcMain.handle("setup:read-log", () => ({
  setup: readTail(ENV_PATHS.setupLog, 400),
  bridge: readTail(ENV_PATHS.bridgeLog, 200),
}));

ipcMain.handle("setup:open-app", async () => {
  await openMainWindow();
  return { ok: true };
});

ipcMain.handle("setup:continue-demo", async () => {
  await openMainWindow();
  await closeSetupWindow();
  return { ok: true };
});

ipcMain.handle("setup:quit", () => {
  app.quit();
  return { ok: true };
});

/* ------------------------------------------------------------------------- */
/* Самопроверка установки (CI / диагностика)                                 */
/* ------------------------------------------------------------------------- */

/** GUI-приложение на Windows может не иметь консоли: вывод не должен ронять процесс. */
function writeStdout(text) {
  try {
    if (process.stdout?.writable) process.stdout.write(text);
  } catch {
    /* отчёт всё равно записан в файл */
  }
}

function argValue(argv, name) {
  const index = argv.indexOf(name);
  return index >= 0 ? argv[index + 1] : null;
}

function directorySize(root) {
  let total = 0;
  const walk = (current) => {
    let entries;
    try {
      entries = fs.readdirSync(current, { withFileTypes: true });
    } catch {
      return;
    }
    for (const entry of entries) {
      const target = path.join(current, entry.name);
      if (entry.isDirectory()) walk(target);
      else {
        try {
          total += fs.statSync(target).size;
        } catch {
          /* пропускаем недоступные файлы */
        }
      }
    }
  };
  walk(root);
  return total;
}

async function runSelfTest(argv) {
  const reportPath = argValue(argv, "--report");
  const repair = argv.includes("--repair");
  const skipBridge = argv.includes("--no-bridge");
  const report = {
    app: "Depth Studio",
    version: setup.appVersion,
    platform: process.platform,
    arch: process.arch,
    packaged: IS_PACKAGED,
    startedAt: new Date().toISOString(),
    envDir: ENV_PATHS.envRoot,
    pythonExe: ENV_PATHS.pythonExe,
    systemPathPython:
      (process.env.PATH || "")
        .split(path.delimiter)
        .filter((entry) => /python/i.test(entry)) || [],
  };
  let result;
  try {
    result = await bootstrap.runSetup({
      appDataRoot: APP_DATA_ROOT,
      resourcesRoot: RESOURCES_ROOT,
      bundleRoot: BUNDLE_ROOT,
      platform: process.platform,
      arch: process.arch,
      force: repair,
      appVersion: setup.appVersion,
      testHooks: TEST_HOOKS,
      emit: () => {},
    });
  } catch (error) {
    result = {
      ok: false,
      error: { code: "unexpected", message: String(error?.message || error), hint: "", details: "" },
      steps: [],
      warnings: [],
    };
  }
  report.demoAvailable = true;
  report.setup = {
    ok: Boolean(result.ok),
    cached: Boolean(result.cached),
    error: result.error || null,
    steps: (result.steps || []).map((step) => ({ id: step.id, status: step.status, detail: step.detailText })),
    warnings: result.warnings || [],
    sources: result.sources || null,
  };
  report.environment = {
    python: result.python || null,
    packages: result.packages || null,
    logFile: result.logFile || ENV_PATHS.setupLog,
    envSizeBytes: directorySize(ENV_PATHS.envRoot),
  };

  if (result.ok && !skipBridge) {
    const status = await startBridge();
    report.bridge = {
      ok: Boolean(status.ready),
      error: status.error,
      python: status.python,
      environment: status.environment || null,
      modules: status.modules || null,
      port: bridgePort,
      logTail: readTail(ENV_PATHS.bridgeLog, 40),
    };
  } else {
    report.bridge = { ok: false, skipped: true, reason: result.ok ? "--no-bridge" : "setup failed" };
  }

  report.ok = Boolean(result.ok) && (skipBridge || Boolean(report.bridge.ok));
  report.finishedAt = new Date().toISOString();
  const destination = reportPath || path.join(ENV_PATHS.logsDir, "selftest-report.json");
  try {
    fs.mkdirSync(path.dirname(destination), { recursive: true });
    fs.writeFileSync(destination, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  } catch (error) {
    report.reportWriteError = String(error?.message || error);
  }
  writeStdout(`${JSON.stringify(report, null, 2)}\n`);
  return report.ok ? 0 : 2;
}

/* ------------------------------------------------------------------------- */
/* Жизненный цикл приложения                                                 */
/* ------------------------------------------------------------------------- */

const argv = process.argv.slice(1);
const isSelfTest = argv.some((argument) => argument === "--depth-studio-selftest");

if (!isSelfTest) {
  const gotLock = app.requestSingleInstanceLock();
  if (!gotLock) {
    app.quit();
  } else {
    app.on("second-instance", () => {
      if (mainWindow && !mainWindow.isDestroyed()) {
        if (mainWindow.isMinimized()) mainWindow.restore();
        mainWindow.focus();
      }
      if (setupWindow && !setupWindow.isDestroyed()) setupWindow.focus();
    });
  }
}

app.whenReady().then(async () => {
  if (isSelfTest) {
    const code = await runSelfTest(process.argv);
    shuttingDown = true;
    if (bridge) bridge.kill();
    app.exit(code);
    return;
  }

  let quick = { ready: false, problems: [] };
  try {
    quick = await bootstrap.quickStatus({
      appDataRoot: APP_DATA_ROOT,
      resourcesRoot: RESOURCES_ROOT,
      platform: process.platform,
    });
  } catch (error) {
    setup.error = {
      code: error?.code || "lock_invalid",
      message: error?.message || "Не удалось проверить среду Depth Studio.",
      hint: error?.hint || "Переустановите приложение.",
      details: error?.details || "",
    };
  }
  setup.phase = quick.ready ? "ready" : "idle";
  setup.python = quick.state?.python || null;
  setup.packages = quick.state?.packages || null;

  if (quick.ready) {
    // Повторный запуск: среда уже готова, установка не повторяется.
    await openMainWindow();
    return;
  }

  // Первый запуск (или повреждённая среда): сначала экран настройки,
  // и только после него — окно сканирования.
  createSetupWindow();
  await runEnvironmentSetup("retry");
});

app.on("window-all-closed", () => {
  if (isSelfTest) return;
  app.quit();
});

app.on("before-quit", () => {
  shuttingDown = true;
  if (bridge) {
    bridge.kill();
    bridge = null;
  }
  if (web) {
    web.close();
    web = null;
  }
});

process.stdout?.on("error", () => {});
process.stderr?.on("error", () => {});

process.on("uncaughtException", (error) => {
  try {
    fs.appendFileSync(
      path.join(ENV_PATHS.logsDir, "main.log"),
      `[${new Date().toISOString()}] ${error?.stack || error}\n`,
    );
  } catch {
    /* ignore */
  }
  if (isSelfTest) {
    writeStdout(`${JSON.stringify({ ok: false, error: String(error?.message || error) })}\n`);
    app.exit(3);
  }
});
