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
const zlib = require("node:zlib");
const { spawn } = require("node:child_process");

const APP_FOLDER_NAME = "DepthStudio";
const LOCK_FILE_NAME = "python-lock.json";
const STATE_SCHEMA = 2;
const LOG_LIMIT_BYTES = 2 * 1024 * 1024;
// pip распаковывается, а NumPy/Open3D затем устанавливаются в site-packages.
// Запас нужен до распаковки, иначе первый запуск падает посреди записи файлов.
const SITE_PACKAGES_REQUIRED_BYTES = 600 * 1024 * 1024;
const PIP_INSTALL_ATTEMPTS = 2;

// Диагностика неудачной установки pip: что интерпретатор видит в sys.path и
// импортируется ли модуль. Без этого сообщение «pip недоступен» ничего не даёт.
const PIP_DIAGNOSTIC_SCRIPT = [
  "import json, sys",
  "result = {'executable': sys.executable, 'prefix': sys.prefix, 'path': list(sys.path), 'pip': None, 'error': None}",
  "try:",
  "    import pip",
  "    result['pip'] = getattr(pip, '__file__', None)",
  "except Exception as exc:",
  "    result['error'] = '%s: %s' % (type(exc).__name__, exc)",
  "print('DEPTH_STUDIO_PIP_DIAG=' + json.dumps(result))",
];

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
  "for name in ('numpy', 'open3d', 'pythonnet', 'freenect'):",
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
  /**
   * @param {string} code machine-readable code shown as «Код: …»
   * @param {string} message one-line explanation for the banner
   * @param {string} hint what the user can do next
   * @param {string} details technical tail (pip output, errno) for the expandable block
   * @param {{cause?: string, remedies?: Array<{id: string, title: string, text: string, relevant?: boolean}>}} [options]
   */
  constructor(code, message, hint = "", details = "", options = {}) {
    super(message);
    this.name = "SetupError";
    this.code = code;
    this.hint = hint;
    this.details = details;
    // cause — уточнение внутри кода (disk_full, access_denied, file_locked…),
    // remedies — типовые причины, которые интерфейс показывает отдельными пунктами.
    this.cause = options.cause || null;
    this.remedies = Array.isArray(options.remedies) ? options.remedies : [];
  }

  toJSON() {
    return {
      code: this.code,
      message: this.message,
      hint: this.hint,
      details: this.details,
      cause: this.cause,
      remedies: this.remedies,
    };
  }
}

/**
 * Типовые причины сбоя установки. Пользователь видит их отдельными пунктами,
 * поэтому диагноз не сводится к одной фразе «не удалось установить pip».
 */
const REMEDY_CATALOG = {
  antivirus: {
    id: "antivirus",
    title: "Антивирус или защита в реальном времени",
    text:
      "Сторонние антивирусы и «Защита от вирусов и угроз» Windows иногда блокируют запись файлов .pyd и .dll в профиль пользователя. " +
      "Добавьте %LOCALAPPDATA%\\DepthStudio в исключения антивируса, повторите установку и верните исключение после успешного запуска.",
  },
  disk_space: {
    id: "disk_space",
    title: "Свободное место на диске",
    text:
      "Изолированной среде нужно около 1 ГБ на диске с профилем пользователя: распаковка Python, pip, NumPy и Open3D идёт в %LOCALAPPDATA%\\DepthStudio. " +
      "Освободите место на системном диске и нажмите «Повторить».",
  },
  permissions: {
    id: "permissions",
    title: "Права на каталог",
    text:
      "Приложению нужна запись в %LOCALAPPDATA%\\DepthStudio. Проверьте, что каталог принадлежит вашему пользователю и не защищён политикой " +
      "контролируемого доступа к папкам (Controlled folder access). Права администратора при этом не требуются.",
  },
  network: {
    id: "network",
    title: "Сеть, прокси или профиль сети",
    text:
      "Если встроенных колёс в установщике нет, компоненты скачиваются с python.org и pypi.org по HTTPS с проверкой TLS. " +
      "Проверьте прокси, VPN, «общедоступный» сетевой профиль и корпоративный фильтр — отключение проверки TLS не требуется и не поддерживается.",
  },
  locked_files: {
    id: "locked_files",
    title: "Файлы заняты другим процессом",
    text:
      "Другой экземпляр Depth Studio, индексатор или сканер могли удерживать файлы среды. Закройте Depth Studio и повторите установку.",
  },
};

function remedyById(id) {
  const item = REMEDY_CATALOG[id];
  return item ? { ...item } : null;
}

/** Список типовых причин: первая — вероятная для текущего сбоя. */
function buildRemedies(causeId, extraIds = []) {
  const priority = [causeId, ...extraIds, "antivirus", "disk_space", "permissions", "network"].filter(Boolean);
  const seen = new Set();
  const result = [];
  for (const id of priority) {
    if (seen.has(id)) continue;
    const item = remedyById(id);
    if (!item) continue;
    seen.add(id);
    result.push({ ...item, relevant: result.length === 0 });
  }
  return result;
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
    // Отдельный журнал попыток установки pip: он остаётся, даже если setup.log
    // не удалось записать, и содержит вывод интерпретатора и sys.path.
    pipBootstrapLog: path.join(envRoot, "logs", "pip-bootstrap.log"),
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

/** Free space of the volume that holds `target`, or null when unavailable. */
async function freeSpaceBytes(target, statfs = fsp.statfs) {
  try {
    const stats = await statfs(target);
    const blockSize = Number(stats.bsize || stats.frsize || 0);
    const available = Number(stats.bavail ?? stats.bfree ?? 0);
    if (!blockSize || !available) return null;
    return { free: available * blockSize, total: Number(stats.blocks || 0) * blockSize };
  } catch {
    return null;
  }
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 МБ";
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} КБ`;
  const megabytes = bytes / (1024 * 1024);
  if (megabytes < 1024) return `${megabytes.toFixed(0)} МБ`;
  return `${(megabytes / 1024).toFixed(1)} ГБ`;
}

/**
 * Translate a filesystem errno into an actionable SetupError.
 *
 * ENOSPC/EACCES/EPERM/EBUSY are the failures users actually hit on Windows:
 * antivirus interception, full system drive and folder permissions.
 */
function mapFileSystemError(error, context = {}) {
  const errno = String(error?.code || "");
  const target = context.target ? ` (${context.target})` : "";
  const detail = [`${errno || error?.name || "ERROR"}: ${error?.message || error}`, context.action || ""]
    .filter(Boolean)
    .join(" | ");

  if (errno === "ENOSPC") {
    return new SetupError(
      "pip_disk_full",
      "На диске закончилось свободное место во время подготовки среды Depth Studio.",
      "Освободите около 1 ГБ на диске с профилем пользователя и нажмите «Повторить». Права администратора не нужны.",
      detail,
      { cause: "disk_full", remedies: buildRemedies("disk_space", ["antivirus"]) },
    );
  }
  if (errno === "EACCES" || errno === "EPERM" || errno === "EBUSY" || errno === "ETXTBSY") {
    const locked =
      errno === "EBUSY" || errno === "ETXTBSY" || /EBUSY|ETXTBSY|being used|занят|locked/i.test(String(error?.message || ""));
    return new SetupError(
      locked ? "pip_file_locked" : "pip_access_denied",
      locked
        ? "Файлы среды Depth Studio заняты другим процессом."
        : "Нет прав на запись в каталог среды Depth Studio.",
      locked
        ? "Закройте другие экземпляры Depth Studio и повторите установку."
        : "Проверьте права на %LOCALAPPDATA%\\DepthStudio и исключения антивируса, затем нажмите «Повторить».",
      detail,
      {
        cause: locked ? "file_locked" : "access_denied",
        remedies: buildRemedies(locked ? "locked_files" : "permissions", ["antivirus"]),
      },
    );
  }
  return new SetupError(
    context.code || "unpack_failed",
    context.message || `Не удалось записать файлы среды Depth Studio${target}.`,
    context.hint || "Нажмите «Повторить». Если ошибка повторяется, откройте журнал установки.",
    detail,
    { cause: "io_error", remedies: buildRemedies("antivirus", ["permissions"]) },
  );
}

/**
 * Verify that site-packages can actually be written *before* unpacking pip.
 *
 * A directory can exist and still be read-only (folder permissions, Controlled
 * folder access, antivirus). Checking early turns a mid-install crash into a
 * specific, fixable message.
 */
async function ensureDirectoryWritable(directory, options = {}) {
  const fileSystem = options.fileSystem || fsp;
  const probeDir = directory;
  try {
    await fileSystem.mkdir(probeDir, { recursive: true });
  } catch (error) {
    throw mapFileSystemError(error, {
      target: probeDir,
      action: "mkdir",
      code: "site_packages_not_writable",
      message: "Не удалось создать каталог site-packages среды Depth Studio.",
      hint: "Проверьте права на %LOCALAPPDATA%\\DepthStudio и повторите установку.",
    });
  }
  const probe = path.join(probeDir, `.depth-studio-write-probe-${process.pid}-${Date.now()}`);
  try {
    await fileSystem.writeFile(probe, "depth-studio-write-probe", "utf8");
    const echo = await fileSystem.readFile(probe, "utf8");
    if (echo !== "depth-studio-write-probe") {
      throw new SetupError(
        "site_packages_not_writable",
        "Каталог site-packages среды Depth Studio не сохраняет записанные файлы.",
        "Проверьте антивирус и права на %LOCALAPPDATA%\\DepthStudio, затем повторите установку.",
        `проверочный файл прочитан как ${JSON.stringify(echo.slice(0, 80))}`,
        { cause: "access_denied", remedies: buildRemedies("antivirus", ["permissions"]) },
      );
    }
  } catch (error) {
    if (error instanceof SetupError) throw error;
    throw mapFileSystemError(error, {
      target: probe,
      action: "write probe",
      code: "site_packages_not_writable",
      message: "Нет прав на запись в site-packages среды Depth Studio.",
      hint: "Проверьте антивирус и права на %LOCALAPPDATA%\\DepthStudio, затем повторите установку.",
    });
  } finally {
    await Promise.resolve(fileSystem.rm(probe, { force: true })).catch(() => {});
  }
}

/**
 * Disk-space and write-access preflight for the managed site-packages.
 * @returns {{freeBytes: number|null}} diagnostics for the setup log
 */
async function prepareSitePackages(paths, opts, logger = null) {
  const statfs = opts?.statfs || fsp.statfs;
  await ensureDirectoryWritable(paths.sitePackages, { fileSystem: opts?.fileSystem });
  const space = await freeSpaceBytes(paths.envRoot, statfs);
  const required = opts?.requiredBytes || SITE_PACKAGES_REQUIRED_BYTES;
  if (space && space.free < required) {
    logger?.warn?.(
      `Свободного места ${formatBytes(space.free)} при рекомендуемых ${formatBytes(required)} — распаковка может не удаться.`,
    );
    throw new SetupError(
      "pip_disk_full",
      `Недостаточно свободного места: нужно около ${formatBytes(required)}, доступно ${formatBytes(space.free)}.`,
      "Освободите место на диске с профилем пользователя и нажмите «Повторить». Права администратора не нужны.",
      `свободно ${space.free} байт, требуется ${required} байт (${paths.envRoot})`,
      { cause: "disk_full", remedies: buildRemedies("disk_space", ["antivirus"]) },
    );
  }
  if (!space) {
    logger?.info?.("Свободное место проверить не удалось — продолжаю с проверкой записи.");
  } else {
    logger?.info?.(`Свободно на диске среды: ${formatBytes(space.free)} (требуется ~${formatBytes(required)}).`);
  }
  return { freeBytes: space?.free ?? null };
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
/* Распаковка ZIP без внешних утилит.                                        */
/* Встроенный Python и pip — обычные zip-архивы, поэтому распаковка не должна */
/* зависеть от PowerShell (её могут блокировать политики предприятия).       */
/* ------------------------------------------------------------------------- */

const ZIP_EOCD_SIGNATURE = 0x06054b50;
const ZIP_CENTRAL_SIGNATURE = 0x02014b50;
const ZIP_LOCAL_SIGNATURE = 0x04034b50;

function findEndOfCentralDirectory(buffer) {
  const minOffset = Math.max(0, buffer.length - 66_000);
  for (let offset = buffer.length - 22; offset >= minOffset; offset -= 1) {
    if (buffer.readUInt32LE(offset) === ZIP_EOCD_SIGNATURE) return offset;
  }
  return -1;
}

/** List ZIP entries: name, sizes, compression method and data offset. */
function listZipEntries(buffer) {
  const eocd = findEndOfCentralDirectory(buffer);
  if (eocd < 0) throw new SetupError("unpack_failed", "Архив повреждён: не найден заголовок ZIP.");
  const total = buffer.readUInt16LE(eocd + 10);
  let offset = buffer.readUInt32LE(eocd + 16);
  if (offset === 0xffffffff) {
    throw new SetupError("unpack_failed", "Архивы ZIP64 не поддерживаются встроенным распаковщиком.");
  }
  const entries = [];
  for (let index = 0; index < total; index += 1) {
    if (buffer.readUInt32LE(offset) !== ZIP_CENTRAL_SIGNATURE) {
      throw new SetupError("unpack_failed", "Архив повреждён: неверный заголовок каталога.");
    }
    const method = buffer.readUInt16LE(offset + 10);
    const compressedSize = buffer.readUInt32LE(offset + 20);
    const uncompressedSize = buffer.readUInt32LE(offset + 24);
    const nameLength = buffer.readUInt16LE(offset + 28);
    const extraLength = buffer.readUInt16LE(offset + 30);
    const commentLength = buffer.readUInt16LE(offset + 32);
    const localOffset = buffer.readUInt32LE(offset + 42);
    const name = buffer.toString("utf8", offset + 46, offset + 46 + nameLength);
    entries.push({ name, method, compressedSize, uncompressedSize, localOffset });
    offset += 46 + nameLength + extraLength + commentLength;
  }
  return entries;
}

function readZipEntryData(buffer, entry) {
  const local = entry.localOffset;
  if (buffer.readUInt32LE(local) !== ZIP_LOCAL_SIGNATURE) {
    throw new SetupError("unpack_failed", `Архив повреждён: запись ${entry.name}.`);
  }
  const nameLength = buffer.readUInt16LE(local + 26);
  const extraLength = buffer.readUInt16LE(local + 28);
  const start = local + 30 + nameLength + extraLength;
  const raw = buffer.subarray(start, start + entry.compressedSize);
  if (entry.method === 0) return raw;
  if (entry.method === 8) {
    try {
      return zlib.inflateRawSync(raw);
    } catch (error) {
      throw new SetupError("unpack_failed", `Не удалось распаковать ${entry.name}.`, "", String(error.message || error));
    }
  }
  throw new SetupError("unpack_failed", `Неподдерживаемый метод сжатия ${entry.method} (${entry.name}).`);
}

/**
 * Extract a ZIP archive into `destination` (created if needed).
 * Path traversal entries are rejected; no external tools are used.
 */
async function extractZip(archivePath, destination, options = {}) {
  const buffer = await fsp.readFile(archivePath);
  const entries = listZipEntries(buffer);
  try {
    await fsp.mkdir(destination, { recursive: true });
  } catch (error) {
    throw mapFileSystemError(error, { target: destination, action: "mkdir destination" });
  }
  let written = 0;
  for (const entry of entries) {
    const isDirectory = entry.name.endsWith("/") || entry.name.endsWith("\\");
    const target = path.resolve(destination, entry.name);
    if (!isInside(destination, target)) {
      throw new SetupError("unpack_failed", `Архив содержит недопустимый путь: ${entry.name}`);
    }
    if (isDirectory) {
      try {
        await fsp.mkdir(target, { recursive: true });
      } catch (error) {
        throw mapFileSystemError(error, { target, action: `mkdir ${entry.name}` });
      }
      continue;
    }
    const data = readZipEntryData(buffer, entry);
    try {
      await fsp.mkdir(path.dirname(target), { recursive: true });
      await fsp.writeFile(target, data);
    } catch (error) {
      throw mapFileSystemError(error, { target, action: `write ${entry.name}` });
    }
    written += 1;
    if (options.onEntry && written % 50 === 0) options.onEntry(written, entries.length);
  }
  return { files: written, entries: entries.length };
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
  // Недоступный Open3D (например, из-за отсутствующего Visual C++ Runtime) не
  // делает среду «неготовой»: переустановка это не исправит, а интерфейс честно
  // сообщает об ограничении и остаётся в деморежиме.
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
    statfs: options.statfs || fsp.statfs,
    fileSystem: options.fileSystem || fsp,
    extractWheel: options.extractWheel || extractZip,
    confirmSensorComponents: Boolean(options.confirmSensorComponents),
    testHooks: options.testHooks || {},
  };
}

function bundledRuntimeInfo(bundleRoot) {
  const pythonDir = path.join(bundleRoot, "python-runtime");
  const wheelsDir = path.join(bundleRoot, "python-wheels");
  return { pythonDir, wheelsDir };
}

/**
 * Where Kinect for Windows SDK 1.8 puts Microsoft.Kinect.dll.
 * Read-only discovery: Depth Studio never installs or modifies SDK components.
 */
const KINECT_SDK_RELATIVE_PATH = path.join("Microsoft SDKs", "Kinect", "v1.8", "Assemblies", "Microsoft.Kinect.dll");

function kinectSdkCandidates(env = process.env) {
  const roots = [env.ProgramW6432, env.ProgramFiles, env["ProgramFiles(x86)"]].filter(Boolean);
  return [...new Set(roots.map((root) => path.join(root, KINECT_SDK_RELATIVE_PATH)))];
}

async function detectKinectSdk(env = process.env) {
  const candidates = kinectSdkCandidates(env);
  for (const candidate of candidates) {
    if (await pathExists(candidate)) return { available: true, assembly: candidate, checked: candidates };
  }
  return { available: false, assembly: null, checked: candidates };
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
          sensors: status.state?.sensors || null,
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
      await enableSitePackages(paths, lock, logger, [opts.appRuntimeDir || opts.resourcesRoot]);
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

    currentStepFraction(0.02, "проверка записи и свободного места");
    try {
      await prepareSitePackages(paths, opts, logger);
    } catch (error) {
      return await fail(
        "packages",
        error instanceof SetupError
          ? error
          : new SetupError("packages_failed", "Не удалось подготовить site-packages.", "", String(error)),
      );
    }

    currentStepFraction(0.04, "проверка pip");
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
      const pipBootstrap = await installPip(paths, pipWheelPath, opts, logger);
      if (!pipBootstrap.ok) {
        // Отдельный журнал именно для pip: он нужен, даже если setup.log недоступен.
        try {
          await fsp.writeFile(
            path.join(paths.logsDir, "pip-bootstrap.log"),
            `[${new Date().toISOString()}] попытки установки pip\n${(pipBootstrap.attempts || []).join("\n")}\n\n${tailText(
              pipBootstrap.result,
              4000,
            )}\n`,
            "utf8",
          );
        } catch {
          /* журнал не критичен для сообщения об ошибке */
        }
        return await fail("packages", pipBootstrapFailure(pipBootstrap, paths));
      }
      logger.info(`pip установлен в среду Depth Studio (${pipBootstrap.method}, попыток: ${pipBootstrap.attemptsCount}).`);
    }

    let installSource = "bundled";
    const requiredPackages = lock.packages.map((pkg) => `${pkg.name}==${pkg.version}`);
    const pipLines = [];
    const collectPipLine = (line) => {
      pipLines.push(line);
      if (pipLines.length > 4000) pipLines.shift();
      logger.line(`pip: ${line}`);
    };
    let remoteLockPath = null;
    if (lock.packages.every((pkg) => bundleWheels.has(pkg.filename))) {
      installSource = "bundled";
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
      remoteLockPath = path.join(paths.downloadsDir, "requirements-scan.lock.txt");
      await fsp.writeFile(remoteLockPath, lockFile, "utf8");
    }

    currentStepFraction(0.3, installSource === "bundled" ? "установка NumPy и Open3D" : "загрузка NumPy и Open3D с PyPI");
    const runInstall = (extraArgs = []) => {
      const args =
        installSource === "bundled"
          ? [
              "-m",
              "pip",
              "install",
              ...extraArgs,
              "--no-index",
              "--find-links",
              bundle.wheelsDir,
              "--only-binary=:all:",
              "--no-warn-script-location",
              "--no-cache-dir",
              "--disable-pip-version-check",
              ...requiredPackages,
            ]
          : [
              "-m",
              "pip",
              "install",
              ...extraArgs,
              "--only-binary=:all:",
              "--require-hashes",
              "--no-warn-script-location",
              "--no-cache-dir",
              "--disable-pip-version-check",
              "-r",
              remoteLockPath,
            ];
      return opts.runCommand(paths.pythonExe, args, {
        env: childEnvironment(opts.env),
        timeoutMs: 60 * 60 * 1000,
        onLine: (line) => {
          collectPipLine(line);
          if (/Collecting |Downloading |Installing collected/i.test(line)) {
            currentStepFraction(0.3, line.slice(0, 120));
          }
        },
      });
    };

    let installResult = opts.testHooks.failStep === "packages"
      ? { code: 1, stdout: "", stderr: "Смоделированный сбой установки пакетов." }
      : await runInstall();

    if (installResult.code === 0 && !(await pathExists(path.join(paths.sitePackages, "numpy")))) {
      // pip считает пакет установленным, если остался только dist-info, —
      // при повреждённой среде помогает только принудительная переустановка.
      logger.warn("NumPy отсутствует после установки: повтор с --force-reinstall.");
      warnings.push({
        code: "packages_force_reinstalled",
        message: "Среда была повреждена: пакеты установлены заново с --force-reinstall.",
        hint: "Это нормально после ручного удаления файлов среды или сбоя установки.",
      });
      installResult = await runInstall(["--force-reinstall"]);
    }
    if (installResult.code !== 0) {
      const pipLog = path.join(paths.logsDir, "pip-install.log");
      try {
        await fsp.writeFile(pipLog, `${pipLines.join("\n")}\n`, "utf8");
      } catch {
        /* журнал не критичен для сообщения об ошибке */
      }
      const details = summarizePipFailure(pipLines, installResult);
      const diagnosis = diagnosePipOutput(`${details}\n${pipLines.join("\n")}`);
      const specificHint = {
        disk_full: "Освободите около 1 ГБ на диске с профилем пользователя и нажмите «Повторить».",
        access_denied:
          "Проверьте права на %LOCALAPPDATA%\\DepthStudio и исключения антивируса, затем нажмите «Повторить».",
        file_locked: "Закройте другие экземпляры Depth Studio и повторите установку.",
        network:
          "Проверьте интернет, прокси, VPN и сетевой профиль, затем нажмите «Повторить». Отключение проверки TLS не поддерживается.",
      }[diagnosis.cause];
      return await fail(
        "packages",
        new SetupError(
          "packages_failed",
          diagnosis.cause === "disk_full"
            ? "Не удалось установить NumPy/Open3D: на диске закончилось свободное место."
            : "Не удалось установить NumPy/Open3D в изолированную среду.",
          specificHint ||
            "Нажмите «Повторить». Если ошибка повторяется, проверьте интернет, прокси, антивирус и свободное место, затем откройте журнал установки.",
          details,
          { cause: diagnosis.cause, remedies: diagnosis.remedies },
        ),
      );
    }
    if (!(await pathExists(path.join(paths.sitePackages, "numpy")))) {
      return await fail(
        "packages",
        new SetupError(
          "numpy_missing_after_install",
          "NumPy не появился в среде после установки, хотя pip сообщил об успехе.",
          "Нажмите «Восстановить среду». Если ошибка повторяется, проверьте антивирус: он может блокировать установку файлов .pyd.",
          tailText(installResult),
          { cause: "access_denied", remedies: buildRemedies("antivirus", ["permissions", "disk_space"]) },
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
    // Требования разделены намеренно: «драйвер устройства» ставится пользователем,
    // «компоненты Python» приходят вместе с установщиком Depth Studio.
    const kinectSdk = await detectKinectSdk(opts.env);
    const pythonComponents = [
      { id: "numpy", title: "NumPy", available: Boolean(packagesState.numpy?.available), version: packagesState.numpy?.version || null },
      { id: "open3d", title: "Open3D", available: Boolean(packagesState.open3d?.available), version: packagesState.open3d?.version || null },
      {
        id: "pythonnet",
        title: "pythonnet",
        available: Boolean(packagesState.pythonnet?.available),
        version: packagesState.pythonnet?.version || null,
      },
      {
        id: "freenect",
        title: "freenect (libfreenect)",
        available: Boolean(packagesState.freenect?.available),
        version: packagesState.freenect?.version || null,
      },
    ];

    sensorNotes.push(
      kinectSdk.available
        ? `Драйвер устройства: Kinect for Windows SDK 1.8 найден (${kinectSdk.assembly}).`
        : "Драйвер устройства: Kinect for Windows SDK 1.8 не найден. Этот SDK нужен адаптеру Microsoft Kinect SDK и не устанавливается приложением автоматически.",
    );
    const missingPython = pythonComponents.filter((item) => !item.available).map((item) => item.title);
    sensorNotes.push(
      missingPython.length
        ? `Компоненты Python: отсутствуют ${missingPython.join(", ")}.`
        : "Компоненты Python: NumPy, Open3D и pythonnet доступны.",
    );

    if (opts.platform === "win32" && !kinectSdk.available) {
      warnings.push({
        code: "kinect_sdk_missing",
        message: "Драйвер устройства: не установлен Kinect for Windows SDK 1.8.",
        hint:
          "Адаптер «Kinect 360 · Microsoft Kinect SDK» требует установленный Kinect for Windows SDK 1.8 (runtime) с файлом " +
          "%ProgramFiles%\\Microsoft SDKs\\Kinect\\v1.8\\Assemblies\\Microsoft.Kinect.dll. Установите SDK вручную с сайта Microsoft — " +
          "Depth Studio не скачивает и не устанавливает драйверы и SDK датчиков. Деморежим работает без них.",
      });
    }
    if (!packagesState.pythonnet?.available) {
      warnings.push({
        code: "sensor_python_component_missing",
        message: "Компонент Python: pythonnet не импортируется в среде Depth Studio.",
        hint:
          "pythonnet входит в установщик Depth Studio. Нажмите «Восстановить среду», чтобы установить пакеты заново; " +
          "драйверы датчика это не затрагивает.",
      });
    }
    sensorNotes.push(
      "Альтернатива libfreenect не включена: она требует отдельный USB-драйвер libusbK/WinUSB, который заменяет драйвер Microsoft Kinect и может сломать другие Kinect-приложения.",
    );
    if (opts.confirmSensorComponents) {
      sensorNotes.push("Пользователь разрешил установку компонентов датчиков — этот релиз таких установок не выполняет.");
    }
    updateStep("sensors", {
      status: "done",
      detailText: kinectSdk.available
        ? "драйвер Kinect SDK 1.8 найден · деморежим доступен"
        : "драйвер Kinect SDK 1.8 не найден · деморежим",
    });

    const state = {
      schemaVersion: STATE_SCHEMA,
      fingerprint,
      envDir: paths.envRoot,
      pythonExe: paths.pythonExe,
      python: { version: probeData.version, executable: probeData.executable, prefix: probeData.prefix },
      packages: packagesState,
      sources: { python: pythonSource, packages: installSource },
      sensors: { kinectSdk, pythonComponents },
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
      sensors: state.sensors,
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


/**
 * Remove a partially unpacked pip so a retry starts from a clean state.
 * Only pip's own files are touched — nothing else in site-packages.
 */
async function removePartialPip(paths, logger = null) {
  const removed = [];
  const entries = await fsp.readdir(paths.sitePackages).catch(() => []);
  for (const name of entries) {
    if (name === "pip" || /^pip-[0-9][^\\/]*\.(dist-info|egg-info)$/i.test(name)) {
      await removePath(path.join(paths.sitePackages, name));
      removed.push(name);
    }
  }
  if (removed.length) logger?.warn?.(`Удалены неполные файлы pip перед повтором: ${removed.join(", ")}`);
  return removed;
}

async function checkPipAvailable(paths, opts, logger = null) {
  try {
    return await opts.runCommand(paths.pythonExe, ["-m", "pip", "--version"], {
      env: childEnvironment(opts.env),
      timeoutMs: 120_000,
      onLine: (line) => logger?.line?.(`pip: ${line}`),
    });
  } catch (error) {
    return { code: -1, stdout: "", stderr: String(error?.message || error) };
  }
}

/**
 * Install pip into the embeddable interpreter.
 *
 * The embeddable distribution ships without pip, and pip refuses to modify itself
 * from a zip archive, so the wheel is unpacked straight into Lib\site-packages.
 * A partially written wheel (antivirus interception, interrupted write) is
 * removed and unpacked once more before giving up. Returns structured attempts so
 * the setup screen can show what exactly was tried and why it failed.
 */
async function installPip(paths, pipWheelPath, opts, logger) {
  const attempts = [];
  let lastResult = null;
  let lastError = null;

  for (let attempt = 1; attempt <= PIP_INSTALL_ATTEMPTS; attempt += 1) {
    if (attempt > 1) await removePartialPip(paths, logger);
    const note = `распаковка колеса pip (попытка ${attempt} из ${PIP_INSTALL_ATTEMPTS})`;
    logger?.info?.(`pip: ${note}`);
    try {
      const extracted = await (opts.extractWheel || extractZip)(pipWheelPath, paths.sitePackages);
      attempts.push(`${note}: распаковано файлов ${extracted?.files ?? "?"}`);
    } catch (error) {
      const setupError =
        error instanceof SetupError
          ? error
          : new SetupError("pip_install_failed", "Не удалось распаковать колесо pip.", "", String(error?.message || error));
      // Нехватка места и отказ доступа не исчезнут от повтора — сразу объясняем причину.
      if (setupError.cause === "disk_full" || setupError.cause === "access_denied") {
        attempts.push(`${note}: ${setupError.message}`);
        return { ok: false, result: { code: -1, stdout: "", stderr: setupError.details }, error: setupError, method: null, attempts };
      }
      attempts.push(`${note}: ${setupError.message}`);
      logger?.warn(`Распаковка колеса pip не удалась: ${setupError.message}`);
      lastError = setupError;
      lastResult = { code: -1, stdout: "", stderr: setupError.details || setupError.message };
      continue;
    }

    const check = await checkPipAvailable(paths, opts, logger);
    if (check.code === 0) {
      logger?.info?.(`pip доступен (распаковка колеса, попытка ${attempt}): ${check.stdout.trim()}`);
      return { ok: true, result: check, method: "распаковка колеса pip", attempts, attemptsCount: attempt };
    }
    attempts.push(
      `${note}: python -m pip --version не сработал (${check.code}): ${tailText(check, 300) || "без вывода"}`,
    );
    logger?.warn(`После распаковки pip не запускается (код ${check.code}).`);
    lastResult = check;
    lastError = new SetupError(
      "pip_install_failed",
      "Колесо pip распаковано, но интерпретатор не видит модуль pip.",
      "Нажмите «Повторить»; если ошибка повторяется, проверьте антивирус и журнал установки.",
      tailText(check, 800),
    );
  }

  // Последняя попытка: запуск pip напрямую из колеса (на случай нестандартной распаковки).
  const note = "запуск pip из колеса";
  logger?.info?.(`pip: ${note}`);
  try {
    lastResult = await opts.runCommand(
      paths.pythonExe,
      [`${pipWheelPath}/pip`, "install", "--no-index", "--no-warn-script-location", pipWheelPath],
      {
        env: childEnvironment(opts.env),
        timeoutMs: 5 * 60 * 1000,
        onLine: (line) => logger?.line?.(`pip: ${line}`),
      },
    );
    attempts.push(`${note}: код ${lastResult?.code ?? -1} ${tailText(lastResult, 300)}`.trim());
  } catch (error) {
    lastResult = { code: -1, stdout: "", stderr: String(error?.message || error) };
    attempts.push(`${note}: ${String(error?.message || error)}`);
  }
  const check = await checkPipAvailable(paths, opts, logger);
  if (check.code === 0) {
    logger?.info?.(`pip доступен (${note}): ${check.stdout.trim()}`);
    return { ok: true, result: check, method: note, attempts, attemptsCount: PIP_INSTALL_ATTEMPTS + 1 };
  }
  attempts.push(`${note}: python -m pip --version не сработал (${check.code})`);
  logger?.warn(`Способ «${note}» не дал рабочего pip.`);
  try {
    const diagnostics = await opts.runCommand(paths.pythonExe, ["-c", PIP_DIAGNOSTIC_SCRIPT.join("\n")], {
      env: childEnvironment(opts.env),
      timeoutMs: 120_000,
    });
    const line = String(diagnostics.stdout || "")
      .split(/\r?\n/)
      .find((entry) => entry.startsWith("DEPTH_STUDIO_PIP_DIAG="));
    if (line) {
      const data = JSON.parse(line.slice("DEPTH_STUDIO_PIP_DIAG=".length));
      attempts.push(
        `диагностика: pip=${data.pip || "не импортируется"}${data.error ? ` (${data.error})` : ""}; ` +
          `sys.path=${(data.path || []).slice(0, 6).join(" | ")}`,
      );
    } else {
      attempts.push(`диагностика: ${tailText(diagnostics, 400) || "интерпретатор не ответил"}`);
    }
  } catch (error) {
    attempts.push(`диагностика не удалась: ${String(error?.message || error)}`);
  }
  return {
    ok: false,
    result: check,
    error: lastError,
    method: null,
    attempts,
  };
}

/**
 * Turn pip output into a specific, actionable diagnosis instead of one vague line.
 * Windows-specific errno and antivirus messages are recognised explicitly.
 */
function diagnosePipOutput(text) {
  const value = String(text || "");
  const checks = [
    {
      cause: "disk_full",
      relevant: "disk_space",
      pattern: /ENOSPC|Errno 28|No space left on device|недостаточно места|There is not enough space/i,
    },
    {
      cause: "file_locked",
      relevant: "locked_files",
      pattern: /EBUSY|WinError 32|being used by another process|файл занят/i,
    },
    {
      cause: "access_denied",
      relevant: "permissions",
      pattern: /EACCES|EPERM|Errno 13|Permission denied|Access is denied|WinError 5\b|отказано в доступе/i,
    },
    {
      cause: "access_denied",
      relevant: "antivirus",
      pattern: /0x80070005|blocked by|заблокирован|antivirus|антивирус|Controlled folder access/i,
    },
    {
      cause: "network",
      relevant: "network",
      pattern:
        /TLS|SSL:|CERTIFICATE_VERIFY_FAILED|proxy|ProxyError|timed out|ReadTimeout|NewConnectionError|Max retries exceeded|HTTP 5\d\d|HTTP 4\d\d|Temporary failure in name resolution|Name or service not known/i,
    },
  ];
  const matched = checks.find((entry) => entry.pattern.test(value)) || null;
  return {
    cause: matched?.cause || "unknown",
    remedies: buildRemedies(matched?.relevant || null, ["antivirus", "disk_space", "permissions", "network"]),
  };
}

function pipBootstrapFailure(pipBootstrap, paths) {
  const detailLines = [...(pipBootstrap.attempts || [])];
  const tail = tailText(pipBootstrap.result, 1200);
  if (tail) detailLines.push(tail);
  const details = detailLines.filter(Boolean).join("\n").slice(-2400) || "нет вывода";
  const cause = pipBootstrap.error?.cause || diagnosePipOutput(details).cause;
  const remedies = pipBootstrap.error?.remedies?.length
    ? pipBootstrap.error.remedies
    : diagnosePipOutput(details).remedies;
  const code = pipBootstrap.error?.code && pipBootstrap.error.code !== "unpack_failed"
    ? pipBootstrap.error.code
    : "pip_install_failed";
  const messages = {
    disk_full: {
      message: "Не удалось установить pip: на диске закончилось свободное место.",
      hint: "Освободите около 1 ГБ на диске с профилем пользователя и нажмите «Повторить».",
    },
    access_denied: {
      message: "Не удалось установить pip: нет прав на запись в среду Depth Studio.",
      hint:
        "Проверьте права на " + paths.sitePackages + " и исключения антивируса, затем нажмите «Повторить».",
    },
    file_locked: {
      message: "Не удалось установить pip: файлы среды заняты другим процессом.",
      hint: "Закройте другие экземпляры Depth Studio и повторите установку.",
    },
    network: {
      message: "Не удалось установить pip: сетевое соединение прервалось или заблокировано.",
      hint: "Проверьте прокси, VPN и сетевой профиль, затем нажмите «Повторить». Отключение проверки TLS не поддерживается.",
    },
  };
  const specific = messages[cause];
  return new SetupError(
    code,
    specific?.message || "Не удалось установить pip в изолированную среду.",
    specific?.hint ||
      "Нажмите «Восстановить среду». Если не помогает, откройте журнал установки и приложите файл %LOCALAPPDATA%\\DepthStudio\\logs\\setup.log.",
    details,
    { cause, remedies },
  );
}

/**
 * (Re)write the embeddable interpreter's python311._pth.
 *
 * A ._pth file puts CPython into isolated mode: sys.path contains exactly the
 * listed entries and the script directory is *not* added. Depth Studio therefore
 * adds its own application directory (the folder with server.py and
 * sensor_drivers) explicitly. The file is refreshed on every launch because the
 * installation directory may differ from the one used during setup.
 */
async function syncInterpreterPaths(paths, lock, extraDirs = [], logger = null) {
  const pthFile = path.join(paths.pythonRoot, lock?.python?.pthFile || "python311._pth");
  let content = "";
  try {
    content = await fsp.readFile(pthFile, "utf8");
  } catch {
    content = "";
  }
  const existing = content
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  const zipLine = existing.find((line) => line.toLowerCase().endsWith(".zip")) || "python311.zip";
  const lines = [];
  const seen = new Set();
  const push = (line) => {
    const key = line.toLowerCase().replace(/[\\/]+$/, "");
    if (!key || seen.has(key)) return;
    seen.add(key);
    lines.push(line);
  };
  push(zipLine);
  push(".");
  push("Lib\\site-packages");
  for (const dir of extraDirs) {
    if (dir) push(path.resolve(dir));
  }
  const next = `${lines.join("\r\n")}\r\nimport site\r\n`;
  if (next !== content) {
    await fsp.mkdir(paths.sitePackages, { recursive: true });
    await fsp.writeFile(pthFile, next, "utf8");
    logger?.info?.(`Пути интерпретатора обновлены: ${lines.join(" | ")}`);
  }
  await fsp.mkdir(paths.sitePackages, { recursive: true });
  return { pthFile, entries: lines };
}

async function enableSitePackages(paths, lock, logger, extraDirs = []) {
  return syncInterpreterPaths(paths, lock, extraDirs, logger);
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

/** Extract the actionable part of pip output instead of a wall of "Processing" lines. */
function summarizePipFailure(lines, result, limit = 1600) {
  const interesting = (lines || []).filter((line) =>
    /error|could not|conflict|no matching|not in '|denied|errno|Traceback|Failed/i.test(line),
  );
  const text = (interesting.length ? interesting : (lines || []).slice(-8)).join("\n").trim();
  if (text) return text.slice(-limit);
  return tailText(result, limit);
}

function tailText(result, limit = 1200) {
  const text = `${result?.stderr || ""}\n${result?.stdout || ""}`.trim();
  return text.length > limit ? text.slice(-limit) : text;
}

async function defaultUnpack(archivePath, destination, options = {}) {
  return extractZip(archivePath, destination, {
    onEntry: options.onEntry,
  });
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
  syncInterpreterPaths,
  bundledRuntimeInfo,
  parseProbe,
  readState,
  summarizePipFailure,
  diagnosePipOutput,
  buildRemedies,
  REMEDY_CATALOG,
  freeSpaceBytes,
  formatBytes,
  mapFileSystemError,
  ensureDirectoryWritable,
  prepareSitePackages,
  removePartialPip,
  detectKinectSdk,
  kinectSdkCandidates,
  isInside,
  sha256File,
  childEnvironment,
  runStreaming,
  httpsFetchBufferToFile,
  downloadFromPyPI,
  collectBundledWheels,
  defaultUnpack,
  extractZip,
  listZipEntries,
  installPip,
};
