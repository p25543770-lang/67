"use strict";
/**
 * First-run Python environment bootstrap for Depth Studio.
 *
 * The application never uses a "random" system Python and never installs
 * packages globally. Every interpreter and package lives inside
 * `%LOCALAPPDATA%\DepthStudio`:
 *
 *   runtime\python\        CPython 3.11.9 (official embeddable build)
 *   runtime\python\Lib\site-packages\   NumPy + Open3D (pinned by lock file)
 *   state\environment.json installation manifest (idempotency marker)
 *   logs\setup.log         human-readable installation journal
 *
 * Two installation sources are supported:
 *   1. Bundled (preferred): the installer ships the interpreter and wheels,
 *      so the first run works offline.
 *   2. Download fallback: pinned, SHA-256 verified artifacts from python.org
 *      and PyPI. TLS verification is always enabled.
 *
 * The module is deliberately dependency-injected so that repeat-run,
 * corruption-repair and failure paths can be unit tested without Windows,
 * network access, or a real Python interpreter.
 */

const fs = require("node:fs");
const fsp = fs.promises;
const path = require("node:path");
const os = require("node:os");
const crypto = require("node:crypto");
const https = require("node:https");
const { spawn } = require("node:child_process");

const APP_FOLDER_NAME = "DepthStudio";
const LOCK_FILE_NAME = "python-lock.json";
const STATE_SCHEMA = 2;
const LOG_LIMIT_BYTES = 2 * 1024 * 1024;

const STEP_DEFINITIONS = [
  {
    id: "prepare",
    title: "Подготовка каталога приложения",
    detail: "%LOCALAPPDATA%\\DepthStudio — изолированная среда без прав администратора",
  },
  {
    id: "python",
    title: "Python 3.11 x64",
    detail: "Встроенный интерпретатор для Python-моста Depth Studio",
  },
  {
    id: "packages",
    title: "NumPy и Open3D",
    detail: "Закреплённые версии из python-lock.json",
  },
  {
    id: "verify",
    title: "Проверка среды",
    detail: "Импорт пакетов и проверка изоляции интерпретатора",
  },
  {
    id: "sensors",
    title: "Проверка датчиков (только чтение)",
    detail: "Драйверы и SDK не устанавливаются автоматически",
  },
];

const PYTHON_PROBE_SCRIPT = [
  "import json, os, platform, sys",
  "result = {",
  "  'executable': sys.executable,",
  "  'prefix': sys.prefix,",
  "  'version': '%d.%d.%d' % sys.version_info[:3],",
  "  'machine': platform.machine(),",
  "  'packages': {},",
  "}",
  "for name in ('numpy', 'open3d', 'freenect'):",
  "  entry = {'available': False, 'version': None, 'error': None}",
  "  try:",
  "    module = __import__(name)",
  "    entry['available'] = True",
  "    entry['version'] = getattr(module, '__version__', None)",
  "  except Exception as exc:",
  "    entry['error'] = '%s: %s' % (type(exc).__name__, exc)",
  "  result['packages'][name] = entry",
  "result['userSite'] = os.environ.get('PYTHONNOUSERSITE', '')",
  "print('DEPTH_STUDIO_PROBE=' + json.dumps(result))",
];

class SetupError extends Error {
  constructor(code, message, hint = "", details = "") {
    super(message);
    this.name = "SetupError";
    this.code = code;
    this.hint = hint;
    this.details = details;
  }

  toJSON() {
    return {
      code: this.code,
      message: this.message,
      hint: this.hint,
      details: this.details,
    };
  }
}

function defaultAppDataRoot(options = {}) {
  const platform = options.platform || process.platform;
  const env = options.env || process.env;
  const homedir = options.homedir || os.homedir();
  if (platform === "win32") {
    const localAppData = env.LOCALAPPDATA || path.join(homedir, "AppData", "Local");
    return path.join(localAppData, APP_FOLDER_NAME);
  }
  if (platform === "darwin") return path.join(homedir, "Library", "Application Support", APP_FOLDER_NAME);
  return path.join(env.XDG_DATA_HOME || path.join(homedir, ".local", "share"), APP_FOLDER_NAME);
}

function resolveEnvironmentPaths({ appDataRoot, platform = process.platform }) {
  const envRoot = path.resolve(appDataRoot);
  const runtimeDir = path.join(envRoot, "runtime");
  const pythonRoot = path.join(runtimeDir, "python");
  const pythonExe = path.join(pythonRoot, platform === "win32" ? "python.exe" : "bin/python3");
  const stateDir = path.join(envRoot, "state");
  return {
    envRoot,
    runtimeDir,
    pythonRoot,
    pythonExe,
    sitePackages: path.join(pythonRoot, "Lib", "site-packages"),
    stateDir,
    stateFile: path.join(stateDir, "environment.json"),
    logsDir: path.join(envRoot, "logs"),
    setupLog: path.join(envRoot, "logs", "setup.log"),
    bridgeLog: path.join(envRoot, "logs", "bridge.log"),
    cacheDir: path.join(envRoot, "cache"),
    downloadsDir: path.join(envRoot, "cache", "downloads"),
  };
}

function readLock(resourcesRoot) {
  const candidates = [
    path.join(resourcesRoot, LOCK_FILE_NAME),
    path.join(resourcesRoot, "..", LOCK_FILE_NAME),
    path.join(process.cwd(), LOCK_FILE_NAME),
  ];
  for (const candidate of candidates) {
    try {
      const raw = fs.readFileSync(candidate, "utf8");
      const lock = JSON.parse(raw);
      if (lock.schemaVersion !== 1 || !lock.python || !Array.isArray(lock.packages)) {
        throw new Error("python-lock.json имеет неподдерживаемый формат");
      }
      return lock;
    } catch (error) {
      if (error.code === "ENOENT") continue;
      throw new SetupError(
        "lock_invalid",
        "Не удалось прочитать файл закреплённых версий python-lock.json.",
        "Переустановите Depth Studio: файл входит в состав приложения.",
        String(error.message || error),
      );
    }
  }
  throw new SetupError(
    "lock_missing",
    "Файл python-lock.json не найден в установке Depth Studio.",
    "Переустановите приложение или запустите `npm run prepare:python` в исходниках.",
  );
}

function lockFingerprint(lock) {
  const payload = {
    schema: lock.schemaVersion,
    python: lock.python?.version,
    pth: lock.python?.pthFile,
    bootstrap: (lock.bootstrapWheels || []).map((wheel) => `${wheel.name}==${wheel.version}:${wheel.sha256}`),
    packages: (lock.packages || []).map(
      (pkg) => `${pkg.name}==${pkg.version}:${pkg.sha256}:${[...(pkg.allowedHashes || [])].sort().join(",")}`,
    ),
  };
  return crypto.createHash("sha256").update(JSON.stringify(payload)).digest("hex");
}

function sha256File(file) {
  return new Promise((resolve, reject) => {
    const hash = crypto.createHash("sha256");
    const stream = fs.createReadStream(file);
    stream.on("error", reject);
    stream.on("data", (chunk) => hash.update(chunk));
    stream.on("end", () => resolve(hash.digest("hex")));
  });
}

async function pathExists(target) {
  try {
    await fsp.access(target);
    return true;
  } catch {
    return false;
  }
}

async function removePath(target) {
  await fsp.rm(target, { recursive: true, force: true, maxRetries: 3 });
}

function createLogger(paths, emit) {
  const listeners = [];
  let buffer = "";
  const write = (level, message) => {
    const line = `[${new Date().toISOString()}] ${level} ${message}`;
    buffer += `${line}\n`;
    for (const listener of listeners) listener(line);
    emit({ type: "log", level, line });
  };
  const logger = {
    info: (message) => write("INFO", message),
    warn: (message) => write("WARN", message),
    error: (message) => write("ERROR", message),
    line: (message) => write("INFO", message),
    tail: (count = 200) => buffer.split("\n").filter(Boolean).slice(-count),
    buffer: () => buffer,
    onLine: (listener) => listeners.push(listener),
    flush: async () => {
      try {
        await fsp.mkdir(paths.logsDir, { recursive: true });
        try {
          const stat = await fsp.stat(paths.setupLog);
          if (stat.size > LOG_LIMIT_BYTES) await fsp.rename(paths.setupLog, `${paths.setupLog}.1`);
        } catch {
          /* first run */
        }
        await fsp.appendFile(paths.setupLog, buffer, "utf8");
        buffer = "";
      } catch (error) {
        emit({ type: "log-write-failed", error: String(error.message || error) });
      }
    },
    emit,
  };
  return logger;
}

/* ------------------------------------------------------------------------- */
/* Нативные помощники: запуск процессов и загрузка файлов по HTTPS.          */
/* ------------------------------------------------------------------------- */

function runStreaming(command, args, options = {}) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(command, args, {
        cwd: options.cwd,
        env: options.env,
        windowsHide: true,
        stdio: ["ignore", "pipe", "pipe"],
      });
    } catch (error) {
      resolve({ code: -1, stdout: "", stderr: String(error.message || error), spawnError: true });
      return;
    }
    let stdout = "";
    let stderr = "";
    let settled = false;
    const timeoutMs = options.timeoutMs || 30 * 60 * 1000;
    const timer = setTimeout(() => {
      if (!settled) child.kill();
    }, timeoutMs);
    const consume = (chunk, isError) => {
      const text = chunk.toString("utf8");
      if (isError) stderr += text;
      else stdout += text;
      if (options.onLine) {
        for (const line of text.split(/\r?\n/)) {
          if (line.trim()) options.onLine(line, isError);
        }
      }
    };
    child.stdout?.on("data", (chunk) => consume(chunk, false));
    child.stderr?.on("data", (chunk) => consume(chunk, true));
    child.once("error", (error) => {
      settled = true;
      clearTimeout(timer);
      resolve({ code: -1, stdout, stderr: String(error.message || error), spawnError: true });
    });
    child.once("close", (code) => {
      settled = true;
      clearTimeout(timer);
      resolve({ code: code ?? -1, stdout, stderr, spawnError: false });
    });
  });
}

function downloadFile(url, destination, options = {}) {
  const fetchImpl = options.fetch || httpsFetchBufferToFile;
  return fetchImpl(url, destination, options);
}

function httpsFetchBufferToFile(url, destination, options = {}) {
  const timeoutMs = options.timeoutMs || 5 * 60 * 1000;
  return new Promise((resolve, reject) => {
    const request = https.get(url, { headers: { "User-Agent": "DepthStudio-Setup/1.0" } }, (response) => {
      if ([301, 302, 303, 307, 308].includes(response.statusCode) && response.headers.location) {
        response.resume();
        const next = new URL(response.headers.location, url).toString();
        httpsFetchBufferToFile(next, destination, options).then(resolve, reject);
        return;
      }
      if (response.statusCode !== 200) {
        response.resume();
        reject(
          new SetupError(
            "download_failed",
            `Не удалось скачать компонент (HTTP ${response.statusCode}).`,
            "Проверьте подключение к интернету и повторите установку.",
            url,
          ),
        );
        return;
      }
      const total = Number(response.headers["content-length"] || 0);
      let received = 0;
      const file = fs.createWriteStream(destination);
      response.on("data", (chunk) => {
        received += chunk.length;
        if (options.onProgress) options.onProgress(total ? received / total : null, received, total);
      });
      response.on("error", (error) => {
        file.destroy();
        reject(new SetupError("download_failed", "Соединение прервалось во время загрузки.", error.message, url));
      });
      file.on("error", (error) => {
        reject(new SetupError("download_failed", "Не удалось записать загруженный файл.", error.message, url));
      });
      file.on("finish", () => file.close(() => resolve({ bytes: received })));
      response.pipe(file);
    });
    request.setTimeout(timeoutMs, () => {
      request.destroy(
        new SetupError(
          "download_timeout",
          "Истекло время ожидания загрузки.",
          "Проверьте подключение и повторите установку.",
          url,
        ),
      );
    });
    request.on("error", (error) => {
      if (error instanceof SetupError) reject(error);
      else
        reject(
          new SetupError(
            "download_failed",
            "Не удалось соединиться с сервером загрузки.",
            "Проверьте интернет-соединение, прокси или антивирус.",
            String(error.message || error),
          ),
        );
    });
  });
}

function childEnvironment(env = process.env) {
  const result = { ...env };
  for (const key of ["PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PIP_REQUIRE_VIRTUALENV"]) delete result[key];
  result.PYTHONNOUSERSITE = "1";
  result.PYTHONUTF8 = "1";
  result.PYTHONIOENCODING = "utf-8";
  result.PIP_DISABLE_PIP_VERSION_CHECK = "1";
  result.PIP_NO_INPUT = "1";
  result.PIP_PROGRESS_BAR = "off";
  return result;
}

/* ------------------------------------------------------------------------- */
/* Основной сценарий.                                                        */
/* ------------------------------------------------------------------------- */

async function readState(paths) {
  try {
    return JSON.parse(await fsp.readFile(paths.stateFile, "utf8"));
  } catch {
    return null;
  }
}

async function writeState(paths, state) {
  await fsp.mkdir(paths.stateDir, { recursive: true });
  const temp = `${paths.stateFile}.tmp`;
  await fsp.writeFile(temp, `${JSON.stringify(state, null, 2)}\n`, "utf8");
  await fsp.rename(temp, paths.stateFile);
}

/**
 * Fast, filesystem-only readiness check used on every application start.
 * It never spawns a process, so the window opens immediately; the bridge
 * performs the authoritative import check when it starts.
 */
async function quickStatus(options) {
  const paths = resolveEnvironmentPaths(options);
  const lock = options.lock || readLock(options.resourcesRoot || path.join(__dirname, ".."));
  const fingerprint = lockFingerprint(lock);
  const state = await readState(paths);
  const problems = [];
  if (!state) problems.push("нет манифеста установки");
  else if (state.fingerprint !== fingerprint) problems.push("версии зависимостей изменились");
  if (!(await pathExists(paths.pythonExe))) problems.push("нет интерпретатора Depth Studio");
  if (!(await pathExists(path.join(paths.sitePackages, "numpy")))) problems.push("нет NumPy в среде Depth Studio");
  const open3dPresent = await pathExists(path.join(paths.sitePackages, "open3d"));
  if (state && state.packages?.open3d?.available === false && open3dPresent) {
    // Recorded as unavailable by the last verify step; keep the reason visible.
    problems.push("Open3D не импортируется");
  }
  return {
    ready: problems.length === 0,
    problems,
    paths,
    lock,
    fingerprint,
    state,
    python: state?.python || null,
    packages: state?.packages || null,
    envDir: paths.envRoot,
    logFile: paths.setupLog,
  };
}

function createEmitter(onEvent) {
  const emit = typeof onEvent === "function" ? onEvent : () => {};
  return (event) => {
    try {
      emit(event);
    } catch {
      /* никогда не ломаем установку из-за UI */
    }
  };
}

function normalizeSetupOptions(options = {}) {
  const platform = options.platform || process.platform;
  const appDataRoot = options.appDataRoot || defaultAppDataRoot({ platform, env: options.env });
  const resourcesRoot = options.resourcesRoot || path.join(__dirname, "..");
  return {
    platform,
    arch: options.arch || process.arch,
    env: options.env || process.env,
    appDataRoot,
    resourcesRoot,
    bundleRoot: options.bundleRoot || resourcesRoot,
    force: Boolean(options.force),
    allowNetwork: options.allowNetwork !== false,
    emit: createEmitter(options.emit),
    runCommand: options.runCommand || runStreaming,
    download: options.download || downloadFile,
    unpack: options.unpack || defaultUnpack,
    confirmSensorComponents: Boolean(options.confirmSensorComponents),
    testHooks: options.testHooks || {},
  };
}

function bundledRuntimeInfo(bundleRoot) {
  const pythonDir = path.join(bundleRoot, "python-runtime");
  const wheelsDir = path.join(bundleRoot, "python-wheels");
  return { pythonDir, wheelsDir };
}

function stepProgress(index, fraction) {
  const total = STEP_DEFINITIONS.length;
  const value = Math.max(0, Math.min(1, (index + Math.min(1, Math.max(0, fraction))) / total));
  return Math.round(value * 100);
}

async function runSetup(rawOptions = {}) {
  const opts = normalizeSetupOptions(rawOptions);
  const paths = resolveEnvironmentPaths({ appDataRoot: opts.appDataRoot, platform: opts.platform });
  const logger = createLogger(paths, opts.emit);
  const emit = opts.emit;
  const steps = STEP_DEFINITIONS.map((step) => ({ ...step, status: "pending", detailText: "" }));
  const warnings = [];
  let currentIndex = 0;
  let result = null;

  const updateStep = (id, patch) => {
    const step = steps.find((item) => item.id === id);
    if (!step) return;
    Object.assign(step, patch);
    emit({ type: "step", step: { ...step }, progress: stepProgress(currentIndex, 0) });
  };
  const currentStepFraction = (fraction, detailText) => {
    const step = steps[currentIndex];
    emit({ type: "step", step: { ...step, status: "running", detailText }, progress: stepProgress(currentIndex, fraction) });
  };
  const fail = async (stepId, error) => {
    const setupError = error instanceof SetupError ? error : new SetupError("unexpected", String(error?.message || error));
    updateStep(stepId, { status: "error", detailText: setupError.message });
    logger.error(`${stepId}: ${setupError.message}${setupError.details ? ` | ${setupError.details}` : ""}`);
    await logger.flush();
    const failure = {
      ok: false,
      cached: false,
      envDir: paths.envRoot,
      logFile: paths.setupLog,
      steps,
      warnings,
      error: setupError.toJSON(),
      demoAvailable: true,
    };
    emit({ type: "error", error: failure.error, result: failure });
    return failure;
  };

  try {
    if (opts.force) {
      logger.warn("Запрошено восстановление среды: каталог runtime\\python будет пересоздан.");
      await removePath(paths.runtimeDir);
      await removePath(paths.stateFile);
    }

    await fsp.mkdir(paths.envRoot, { recursive: true });
    const lock = readLock(opts.resourcesRoot);
    const fingerprint = lockFingerprint(lock);
    logger.info(
      `Depth Studio setup: env=${paths.envRoot} python=${lock.python.version} packages=${lock.packages.length} force=${opts.force}`,
    );

    if (!opts.force && !opts.testHooks.skipCache) {
      const status = await quickStatus({
        appDataRoot: opts.appDataRoot,
        platform: opts.platform,
        resourcesRoot: opts.resourcesRoot,
        lock,
      });
      if (status.ready) {
        // Идемпотентность: готовую среду не переустанавливаем.
        for (const step of steps) {
          step.status = "cached";
          step.detailText = "среда уже готова";
        }
        steps[0].detailText = paths.envRoot;
        if (status.state?.packages) {
          steps[3].detailText = "проверено при предыдущем запуске";
        }
        logger.info("Среда Depth Studio уже настроена — повторная установка не требуется.");
        await logger.flush();
        const cachedResult = {
          ok: true,
          cached: true,
          envDir: paths.envRoot,
          pythonExe: paths.pythonExe,
          logFile: paths.setupLog,
          steps,
          python: status.state?.python || null,
          packages: status.state?.packages || null,
          warnings: [],
          sensorNotes: [],
          sources: status.state?.sources || null,
          demoAvailable: true,
        };
        emit({ type: "done", result: cachedResult });
        return cachedResult;
      }
      logger.info(`Среда будет создана заново: ${status.problems.join("; ")}`);
    }

    if (opts.platform !== "win32" && !opts.testHooks.allowForeignPlatform) {
      return await fail(
        "prepare",
        new SetupError(
          "unsupported_platform",
          "Автоматическая настройка Python поддерживается только в Windows x64.",
          "Запустите интерфейс через `npm run dev` — деморежим работает без Python.",
          `${opts.platform}/${opts.arch}`,
        ),
      );
    }
    if (opts.arch !== "x64" && !opts.testHooks.allowForeignPlatform) {
      return await fail(
        "prepare",
        new SetupError(
          "unsupported_arch",
          `Текущая архитектура — ${opts.arch}. Depth Studio поставляет 64-битный Python для x64.`,
          "Установите 64-битную версию приложения на Windows x64.",
        ),
      );
    }

    /* ------------------------------- 1. prepare ------------------------------- */
    currentIndex = 0;
    updateStep("prepare", { status: "running" });
    await fsp.mkdir(paths.logsDir, { recursive: true });
    await fsp.mkdir(paths.stateDir, { recursive: true });
    await fsp.mkdir(paths.cacheDir, { recursive: true });
    await fsp.mkdir(paths.downloadsDir, { recursive: true });
    await fsp.mkdir(paths.runtimeDir, { recursive: true });
    updateStep("prepare", { status: "done", detailText: paths.envRoot });
    logger.info(`Каталоги готовы: ${paths.envRoot}`);

    /* -------------------------------- 2. python ------------------------------- */
    currentIndex = 1;
    updateStep("python", { status: "running" });
    let pythonSource = "existing";
    let pythonVersion = lock.python.version;
    if (!(await pathExists(paths.pythonExe))) {
      const bundle = bundledRuntimeInfo(opts.bundleRoot);
      const bundleReady = await pathExists(path.join(bundle.pythonDir, "python.exe"));
      if (bundleReady) {
        pythonSource = "bundled";
        logger.info(`Копирование встроенного Python из ${bundle.pythonDir}`);
        currentStepFraction(0.2, "копирование встроенного Python");
        await fsp.mkdir(paths.runtimeDir, { recursive: true });
        await removePath(paths.pythonRoot);
        await fsp.cp(bundle.pythonDir, paths.pythonRoot, { recursive: true });
      } else {
        pythonSource = "download";
        const archive = lock.python.archive || {};
        if (!archive.sha256) {
          return await fail(
            "python",
            new SetupError(
              "python_hash_missing",
              "В установке нет встроенного Python, а SHA-256 официального архива не закреплён.",
              "Переустановите Depth Studio из официального установщика или запустите `npm run prepare:python` в исходниках.",
              "python-lock.json → python.archive.sha256 == null",
            ),
          );
        }
        if (!opts.allowNetwork) {
          return await fail(
            "python",
            new SetupError(
              "network_disabled",
              "Требуется загрузка Python, но сеть отключена.",
              "Подключите интернет и нажмите «Повторить».",
            ),
          );
        }
        const archivePath = path.join(paths.downloadsDir, path.basename(new URL(archive.url).pathname));
        logger.info(`Загрузка Python: ${archive.url}`);
        currentStepFraction(0.05, "загрузка Python 3.11 с python.org");
        if (!(await pathExists(archivePath)) || (await sha256File(archivePath)) !== archive.sha256) {
          await removePath(archivePath);
          await opts.download(archive.url, archivePath, {
            onProgress: (fraction, received, total) => {
              currentStepFraction(0.05 + (fraction || 0) * 0.55, "загрузка Python 3.11 с python.org");
              logger.info(`python.org: ${received} из ${total || "?"} байт`);
            },
          });
        }
        const digest = await sha256File(archivePath);
        if (digest !== archive.sha256) {
          await removePath(archivePath);
          return await fail(
            "python",
            new SetupError(
              "python_checksum_mismatch",
              "Контрольная сумма загруженного Python не совпала с закреплённой.",
              "Повторите установку. Если ошибка повторяется, скачайте установщик заново: файл мог быть повреждён.",
              `ожидалось ${archive.sha256}, получено ${digest}`,
            ),
          );
        }
        currentStepFraction(0.65, "распаковка Python");
        await removePath(paths.pythonRoot);
        await opts.unpack(archivePath, paths.pythonRoot, { platform: opts.platform });
        logger.info("Python распакован.");
      }
      // The embeddable distribution ignores site-packages until "import site"
      // is enabled in the ._pth file.
      await enableSitePackages(paths, lock, logger);
    } else {
      logger.info("Интерпретатор уже установлен, повторная распаковка не требуется.");
    }

    const versionCheck = await opts.runCommand(paths.pythonExe, ["-c", "import sys;print(sys.version.split()[0])"], {
      env: childEnvironment(opts.env),
      timeoutMs: 60_000,
    });
    if (versionCheck.code !== 0) {
      return await fail(
        "python",
        new SetupError(
          "python_unusable",
          "Интерпретатор Depth Studio не запускается.",
          "Нажмите «Восстановить среду», чтобы пересоздать изолированную среду.",
          (versionCheck.stderr || versionCheck.stdout || "").slice(-800),
        ),
      );
    }
    pythonVersion = versionCheck.stdout.trim().split(/\r?\n/).pop() || lock.python.version;
    logger.info(`Python в среде Depth Studio: ${pythonVersion} (${paths.pythonExe})`);
    updateStep("python", { status: "done", detailText: `Python ${pythonVersion}` });

    /* ------------------------------- 3. packages ------------------------------ */
    currentIndex = 2;
    updateStep("packages", { status: "running" });
    const bundle = bundledRuntimeInfo(opts.bundleRoot);
    const bundleWheels = await collectBundledWheels(bundle.wheelsDir, lock, logger, warnings);
    const pipInfo = (lock.bootstrapWheels || [])[0];
    if (!pipInfo) {
      return await fail(
        "packages",
        new SetupError("lock_invalid", "В python-lock.json нет bootstrap-пакета pip.", "Переустановите Depth Studio."),
      );
    }

    currentStepFraction(0.02, "проверка pip");
    const pipReady = await pathExists(path.join(paths.sitePackages, "pip"));
    if (!pipReady) {
      let pipWheelPath = bundleWheels.get(pipInfo.filename) || null;
      if (!pipWheelPath) {
        if (!opts.allowNetwork) {
          return await fail(
            "packages",
            new SetupError(
              "network_disabled",
              "В установке нет колеса pip, а сеть отключена.",
              "Подключите интернет и нажмите «Повторить».",
            ),
          );
        }
        pipWheelPath = path.join(paths.downloadsDir, pipInfo.filename);
        if (!(await pathExists(pipWheelPath)) || (await sha256File(pipWheelPath)) !== pipInfo.sha256) {
          await downloadFromPyPI(pipInfo, pipWheelPath, opts);
        }
      }
      const pipInstall = await opts.runCommand(
        paths.pythonExe,
        [pipWheelPath + path.sep + "pip", "install", "--no-index", "--no-warn-script-location", pipWheelPath],
        { env: childEnvironment(opts.env), timeoutMs: 5 * 60 * 1000, onLine: (line) => logger.line(`pip: ${line}`) },
      );
      if (pipInstall.code !== 0) {
        return await fail(
          "packages",
          new SetupError(
            "pip_install_failed",
            "Не удалось установить pip в изолированную среду.",
            "Нажмите «Восстановить среду». Если не помогает, приложите журнал установки.",
            tailText(pipInstall),
          ),
        );
      }
      logger.info("pip установлен в среду Depth Studio.");
    }

    let installSource = "bundled";
    const requiredPackages = lock.packages.map((pkg) => `${pkg.name}==${pkg.version}`);
    currentStepFraction(0.15, "установка NumPy и Open3D");
    const useBundle = lock.packages.every((pkg) => bundleWheels.has(pkg.filename));
    let installResult;
    if (useBundle) {
      installResult = await opts.runCommand(
        paths.pythonExe,
        [
          "-m",
          "pip",
          "install",
          "--no-index",
          "--find-links",
          bundle.wheelsDir,
          "--only-binary=:all:",
          "--no-warn-script-location",
          "--disable-pip-version-check",
          ...requiredPackages,
        ],
        {
          env: childEnvironment(opts.env),
          timeoutMs: 30 * 60 * 1000,
          onLine: (line) => logger.line(`pip: ${line}`),
        },
      );
    } else {
      installSource = "download";
      if (!opts.allowNetwork) {
        return await fail(
          "packages",
          new SetupError(
            "network_disabled",
            "В установке нет всех колёс NumPy/Open3D, а сеть отключена.",
            "Используйте официальный установщик Depth Studio (среда включена) или подключите интернет.",
          ),
        );
      }
      warnings.push({
        code: "downloading_packages",
        message: "Встроенных колёс нет — пакеты загружаются с PyPI по закреплённым версиям.",
        hint: "Прогресс отображается на этом экране; установка может занять несколько минут.",
      });
      logger.warn("Колёса не найдены в установке, используется загрузка с PyPI.");
      const lockFile = await fsp
        .readFile(path.join(opts.resourcesRoot, "requirements-scan.lock.txt"), "utf8")
        .catch(async () => fsp.readFile(path.join(opts.resourcesRoot, "..", "requirements-scan.lock.txt"), "utf8"));
      const lockPath = path.join(paths.downloadsDir, "requirements-scan.lock.txt");
      await fsp.writeFile(lockPath, lockFile, "utf8");
      installResult = await opts.runCommand(
        paths.pythonExe,
        [
          "-m",
          "pip",
          "install",
          "--only-binary=:all:",
          "--require-hashes",
          "--no-warn-script-location",
          "--disable-pip-version-check",
          "-r",
          lockPath,
        ],
        {
          env: childEnvironment(opts.env),
          timeoutMs: 60 * 60 * 1000,
          onLine: (line) => {
            logger.line(`pip: ${line}`);
            const progressMatch = /Collecting |Downloading |Installing collected/i.test(line);
            if (progressMatch) currentStepFraction(0.3, line.slice(0, 120));
          },
        },
      );
    }

    if (opts.testHooks.failStep === "packages") {
      installResult = { code: 1, stdout: "", stderr: "Смоделированный сбой установки пакетов." };
    }
    if (installResult.code !== 0) {
      return await fail(
        "packages",
        new SetupError(
          "packages_failed",
          "Не удалось установить NumPy/Open3D в изолированную среду.",
          "Нажмите «Повторить». Если ошибка повторяется, проверьте интернет, прокси и антивирус, затем откройте журнал установки.",
          tailText(installResult),
        ),
      );
    }
    logger.info(`Зависимости установлены (источник: ${installSource}).`);
    updateStep("packages", {
      status: "done",
      detailText: `NumPy + Open3D · ${installSource === "bundled" ? "из установщика" : "загружено с PyPI"}`,
    });

    /* -------------------------------- 4. verify ------------------------------- */
    currentIndex = 3;
    updateStep("verify", { status: "running" });
    currentStepFraction(0.2, "проверка импорта");
    const probe = await opts.runCommand(paths.pythonExe, ["-c", PYTHON_PROBE_SCRIPT.join("\n")], {
      env: childEnvironment(opts.env),
      timeoutMs: 5 * 60 * 1000,
    });
    const probeData = parseProbe(probe.stdout);
    if (probe.code !== 0 || !probeData) {
      return await fail(
        "verify",
        new SetupError(
          "verify_failed",
          "Среда Depth Studio создана, но проверка Python не прошла.",
          "Нажмите «Восстановить среду», чтобы пересоздать её.",
          tailText(probe),
        ),
      );
    }
    const isolated = isInside(paths.envRoot, probeData.executable) && isInside(paths.envRoot, probeData.prefix);
    if (!isolated) {
      return await fail(
        "verify",
        new SetupError(
          "env_not_isolated",
          "Интерпретатор оказался вне каталога Depth Studio — среда не изолирована.",
          "Нажмите «Восстановить среду». Приложение не использует системный Python.",
          `${probeData.executable} / ${probeData.prefix}`,
        ),
      );
    }
    if (!probeData.packages?.numpy?.available) {
      return await fail(
        "verify",
        new SetupError(
          "numpy_missing",
          "NumPy не импортируется в среде Depth Studio.",
          "Нажмите «Восстановить среду».",
          probeData.packages?.numpy?.error || "",
        ),
      );
    }
    if (!probeData.packages?.open3d?.available) {
      warnings.push({
        code: "open3d_unavailable",
        message:
          "Open3D не импортируется: сканирование с датчиком недоступно, деморежим работает. NumPy и экспорт доступны.",
        hint:
          "Чаще всего не хватает системного компонента Microsoft Visual C++ 2015–2022 Redistributable (x64). " +
          "Установите его вручную с сайта Microsoft, если хотите использовать датчик; Depth Studio не устанавливает системные компоненты автоматически.",
      });
      logger.warn(`Open3D недоступен: ${probeData.packages.open3d.error}`);
    }
    const packagesState = {};
    for (const [name, entry] of Object.entries(probeData.packages)) {
      packagesState[name] = { available: Boolean(entry.available), version: entry.version || null, error: entry.error || null };
    }
    updateStep("verify", {
      status: "done",
      detailText: packagesState.open3d.available
        ? `NumPy ${packagesState.numpy.version} · Open3D ${packagesState.open3d.version}`
        : `NumPy ${packagesState.numpy.version} · Open3D недоступен`,
    });
    logger.info(`Проверка импорта: ${JSON.stringify(packagesState)}`);

    /* -------------------------------- 5. sensors ------------------------------ */
    currentIndex = 4;
    updateStep("sensors", { status: "running" });
    const sensorNotes = [];
    if (!packagesState.freenect?.available) {
      sensorNotes.push(
        "Модуль freenect (libfreenect) не найден. Depth Studio не устанавливает USB-драйверы, SDK и системные компоненты датчиков автоматически: " +
          "для экспериментального Kinect 360 подготовьте совместимый драйвер вручную (см. README).",
      );
      warnings.push({
        code: "sensor_driver_missing",
        message: "USB-драйвер датчика глубины не настроен — доступен только деморежим.",
        hint: "Kinect 360 требует libfreenect с Python-привязками и совместимый USB-драйвер. Приложение не ставит их без вашего согласия.",
      });
    }
    if (opts.confirmSensorComponents) {
      sensorNotes.push("Пользователь разрешил установку компонентов датчиков — этот релиз таких установок не выполняет.");
    }
    updateStep("sensors", {
      status: "done",
      detailText: packagesState.freenect?.available ? "freenect найден" : "датчики не настроены · деморежим",
    });

    const state = {
      schemaVersion: STATE_SCHEMA,
      fingerprint,
      envDir: paths.envRoot,
      pythonExe: paths.pythonExe,
      python: { version: probeData.version, executable: probeData.executable, prefix: probeData.prefix },
      packages: packagesState,
      sources: { python: pythonSource, packages: installSource },
      warnings: warnings.map((warning) => warning.code),
      completedAt: new Date().toISOString(),
      appVersion: opts.appVersion || null,
    };
    await writeState(paths, state);

    logger.info("Среда Depth Studio готова.");
    await logger.flush();
    result = {
      ok: true,
      cached: false,
      envDir: paths.envRoot,
      pythonExe: paths.pythonExe,
      logFile: paths.setupLog,
      steps: steps.map((step) => ({ ...step, status: step.status === "pending" ? "skipped" : step.status })),
      python: state.python,
      packages: packagesState,
      warnings,
      sensorNotes,
      sources: state.sources,
      demoAvailable: true,
    };
    emit({ type: "done", result });
    return result;
  } catch (error) {
    const stepId = STEP_DEFINITIONS[currentIndex]?.id || "prepare";
    if (error instanceof SetupError) return await fail(stepId, error);
    return await fail(
      stepId,
      new SetupError(
        "unexpected",
        "Неожиданная ошибка при настройке среды Depth Studio.",
        "Откройте журнал установки и нажмите «Восстановить среду».",
        String(error?.stack || error),
      ),
    );
  }
}

async function enableSitePackages(paths, lock, logger) {
  const pthFile = path.join(paths.pythonRoot, lock.python.pthFile || "python311._pth");
  let content = "";
  try {
    content = await fsp.readFile(pthFile, "utf8");
  } catch {
    content = `python311.zip\n.\n`;
  }
  const lines = content.split(/\r?\n/);
  const additions = [];
  if (!lines.some((line) => line.trim().toLowerCase() === "lib\\site-packages")) additions.push("Lib\\site-packages");
  const enabled = lines.some((line) => line.trim() === "import site");
  const next = [...lines.filter((line) => line.trim() !== "" || true)];
  const merged = [...next];
  for (const line of additions) merged.push(line);
  if (!enabled) merged.push("import site");
  const payload = `${merged.filter((line, index, array) => !(line === "" && index === array.length - 1)).join("\r\n")}\r\n`;
  if (payload !== content) {
    await fsp.writeFile(pthFile, payload, "utf8");
    logger.info("Включён импорт site-packages для встроенного Python.");
  }
  await fsp.mkdir(paths.sitePackages, { recursive: true });
}

async function collectBundledWheels(wheelsDir, lock, logger, warnings) {
  const found = new Map();
  if (!(await pathExists(wheelsDir))) return found;
  const entries = await fsp.readdir(wheelsDir).catch(() => []);
  for (const pkg of [...lock.packages, ...(lock.bootstrapWheels || [])]) {
    if (!entries.includes(pkg.filename)) continue;
    const file = path.join(wheelsDir, pkg.filename);
    const digest = await sha256File(file);
    if (digest !== pkg.sha256) {
      logger.warn(`Пропущено колесо с неверной контрольной суммой: ${pkg.filename}`);
      warnings.push({
        code: "wheel_checksum_mismatch",
        message: `Колесо ${pkg.filename} повреждено и не будет использовано.`,
        hint: "Пакет будет загружен заново с PyPI по закреплённой версии.",
      });
      continue;
    }
    found.set(pkg.filename, file);
  }
  logger.info(`Проверено встроенных колёс: ${found.size}`);
  return found;
}

async function downloadFromPyPI(pkg, destination, opts) {
  const jsonUrl = `https://pypi.org/pypi/${pkg.name}/${pkg.version}/json`;
  const payload = await new Promise((resolve, reject) => {
    https
      .get(jsonUrl, { headers: { "User-Agent": "DepthStudio-Setup/1.0", Accept: "application/json" } }, (response) => {
        if (response.statusCode !== 200) {
          response.resume();
          reject(
            new SetupError(
              "download_failed",
              `Не удалось получить сведения о ${pkg.name} с pypi.org (HTTP ${response.statusCode}).`,
              "Проверьте интернет-соединение и повторите.",
              jsonUrl,
            ),
          );
          return;
        }
        let body = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => {
          body += chunk;
        });
        response.on("end", () => {
          try {
            resolve(JSON.parse(body));
          } catch (error) {
            reject(new SetupError("download_failed", "Некорректный ответ pypi.org.", "", String(error)));
          }
        });
      })
      .on("error", (error) =>
        reject(
          new SetupError(
            "download_failed",
            "Не удалось соединиться с pypi.org.",
            "Проверьте интернет-соединение, прокси или антивирус.",
            String(error.message || error),
          ),
        ),
      );
  });
  const file = (payload.urls || []).find((entry) => entry.filename === pkg.filename);
  if (!file) {
    throw new SetupError(
      "wheel_not_found",
      `PyPI не содержит файла ${pkg.filename}.`,
      "Проверьте закреплённые версии через `npm run prepare:python`.",
    );
  }
  await opts.download(file.url, destination, {});
  const digest = await sha256File(destination);
  if (digest !== pkg.sha256) {
    await removePath(destination);
    throw new SetupError(
      "wheel_checksum_mismatch",
      `Контрольная сумма ${pkg.filename} не совпала с закреплённой.`,
      "Повторите установку; при повторении ошибки проверьте прокси или антивирус.",
      `ожидалось ${pkg.sha256}, получено ${digest}`,
    );
  }
}

function parseProbe(stdout) {
  const line = String(stdout || "")
    .split(/\r?\n/)
    .reverse()
    .find((entry) => entry.startsWith("DEPTH_STUDIO_PROBE="));
  if (!line) return null;
  try {
    return JSON.parse(line.slice("DEPTH_STUDIO_PROBE=".length));
  } catch {
    return null;
  }
}

function isInside(parent, child) {
  if (!parent || !child) return false;
  const normalize = (value) => {
    const unified = String(value).trim().replace(/\\/g, "/").replace(/\/+$/, "");
    // Windows paths are case-insensitive; POSIX paths are not.
    return /^[a-zA-Z]:\//.test(unified) || unified.startsWith("//") ? unified.toLowerCase() : unified;
  };
  const root = normalize(parent);
  const target = normalize(child);
  return root === target || target.startsWith(`${root}/`);
}

function tailText(result, limit = 1200) {
  const text = `${result?.stderr || ""}\n${result?.stdout || ""}`.trim();
  return text.length > limit ? text.slice(-limit) : text;
}

async function defaultUnpack(archivePath, destination, options = {}) {
  if ((options.platform || process.platform) === "win32") {
    const result = await runStreaming(
      "powershell.exe",
      [
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        `Expand-Archive -LiteralPath '${archivePath.replace(/'/g, "''")}' -DestinationPath '${destination.replace(/'/g, "''")}' -Force`,
      ],
      { timeoutMs: 5 * 60 * 1000, env: process.env },
    );
    if (result.code !== 0) {
      throw new SetupError(
        "unpack_failed",
        "Не удалось распаковать архив Python.",
        "Проверьте свободное место и права на каталог %LOCALAPPDATA%\\DepthStudio.",
        tailText(result),
      );
    }
    return;
  }
  const result = await runStreaming("unzip", ["-q", "-o", archivePath, "-d", destination], {
    timeoutMs: 5 * 60 * 1000,
  });
  if (result.code !== 0) throw new SetupError("unpack_failed", "Не удалось распаковать архив Python.", "", tailText(result));
}

/** Remove the managed environment. Used by repair and uninstall flows. */
async function resetEnvironment(rawOptions = {}) {
  const opts = normalizeSetupOptions(rawOptions);
  const paths = resolveEnvironmentPaths({ appDataRoot: opts.appDataRoot, platform: opts.platform });
  await removePath(paths.runtimeDir);
  await removePath(paths.stateFile);
  return paths;
}

module.exports = {
  APP_FOLDER_NAME,
  STEP_DEFINITIONS,
  PYTHON_PROBE_SCRIPT,
  SetupError,
  defaultAppDataRoot,
  resolveEnvironmentPaths,
  readLock,
  lockFingerprint,
  quickStatus,
  runSetup,
  resetEnvironment,
  enableSitePackages,
  bundledRuntimeInfo,
  parseProbe,
  isInside,
  sha256File,
  childEnvironment,
  runStreaming,
  httpsFetchBufferToFile,
  downloadFromPyPI,
  collectBundledWheels,
  defaultUnpack,
};
