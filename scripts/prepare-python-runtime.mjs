#!/usr/bin/env node
/**
 * Build the self-contained Python runtime that ships inside the Windows
 * installer:
 *
 *   python-bundle/runtime/   CPython 3.11.9 embeddable (official python.org zip)
 *   python-bundle/wheels/    pinned NumPy/Open3D wheels + pip (SHA-256 checked)
 *   python-bundle/manifest.json
 *
 * The result makes the first launch work offline, without administrator rights
 * and without any system Python. Every artifact is verified against
 * python-lock.json before it is placed into the bundle.
 *
 * Usage:
 *   node scripts/prepare-python-runtime.mjs            # build the bundle
 *   node scripts/prepare-python-runtime.mjs --check    # verify an existing bundle
 *   node scripts/prepare-python-runtime.mjs --write-lock  # pin the python.org SHA-256
 */
import { spawn } from "node:child_process";
import { createHash } from "node:crypto";
import { createWriteStream, existsSync, promises as fsp, readFileSync } from "node:fs";
import https from "node:https";
import os from "node:os";
import path from "node:path";

const BUNDLE_DIR = path.resolve("python-bundle");
const RUNTIME_DIR = path.join(BUNDLE_DIR, "runtime");
const WHEELS_DIR = path.join(BUNDLE_DIR, "wheels");
const CACHE_DIR = path.join(BUNDLE_DIR, "cache");
const LOCK_PATH = path.resolve("python-lock.json");
const PTH_CONTENT = ["python311.zip", ".", "Lib\\site-packages", "import site", ""].join("\r\n");

const argv = process.argv.slice(2);
const CHECK_ONLY = argv.includes("--check");
const WRITE_LOCK = argv.includes("--write-lock");

function log(message) {
  process.stdout.write(`[python-runtime] ${message}\n`);
}

function readLock() {
  return JSON.parse(readFileSync(LOCK_PATH, "utf8"));
}

function sha256(buffer) {
  return createHash("sha256").update(buffer).digest("hex");
}

function sha256File(file) {
  return sha256(readFileSync(file));
}

function run(command, args, options = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(command, args, { stdio: ["ignore", "pipe", "pipe"], windowsHide: true, ...options });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk) => {
      stdout += chunk;
      if (options.echo) process.stdout.write(chunk);
    });
    child.stderr.on("data", (chunk) => {
      stderr += chunk;
      if (options.echo) process.stderr.write(chunk);
    });
    child.once("error", reject);
    child.once("close", (code) => resolve({ code, stdout, stderr }));
  });
}

function pythonCommand() {
  if (process.env.DEPTH_STUDIO_BUILD_PYTHON) return [process.env.DEPTH_STUDIO_BUILD_PYTHON, []];
  if (process.platform === "win32") return ["python", []];
  return ["python3", []];
}

function download(url, destination) {
  return new Promise((resolve, reject) => {
    const follow = (target) => {
      https
        .get(target, { headers: { "User-Agent": "DepthStudio-Build/1.0" } }, (response) => {
          if ([301, 302, 303, 307, 308].includes(response.statusCode) && response.headers.location) {
            response.resume();
            follow(new URL(response.headers.location, target).toString());
            return;
          }
          if (response.statusCode !== 200) {
            response.resume();
            reject(new Error(`HTTP ${response.statusCode} при загрузке ${target}`));
            return;
          }
          const total = Number(response.headers["content-length"] || 0);
          let received = 0;
          let lastReported = 0;
          const file = createWriteStream(destination);
          response.on("data", (chunk) => {
            received += chunk.length;
            if (total && received - lastReported > 8 * 1024 * 1024) {
              lastReported = received;
              log(`  ${(received / 1048576).toFixed(1)} / ${(total / 1048576).toFixed(1)} МБ`);
            }
          });
          response.pipe(file);
          file.once("finish", () => file.close(() => resolve({ bytes: received })));
          file.once("error", reject);
        })
        .on("error", reject);
    };
    follow(url);
  });
}

async function extract(archive, destination) {
  await fsp.rm(destination, { recursive: true, force: true });
  await fsp.mkdir(destination, { recursive: true });
  if (process.platform === "win32") {
    const result = await run("powershell.exe", [
      "-NoProfile",
      "-NonInteractive",
      "-Command",
      `Expand-Archive -LiteralPath '${archive.replace(/'/g, "''")}' -DestinationPath '${destination.replace(/'/g, "''")}' -Force`,
    ]);
    if (result.code !== 0) throw new Error(`Expand-Archive завершился с кодом ${result.code}: ${result.stderr}`);
    return;
  }
  const result = await run("unzip", ["-q", "-o", archive, "-d", destination]);
  if (result.code !== 0) throw new Error(`unzip завершился с кодом ${result.code}: ${result.stderr}`);
}

async function ensureWheels(lock) {
  await fsp.mkdir(WHEELS_DIR, { recursive: true });
  const pins = [...lock.packages, ...(lock.bootstrapWheels || [])].map((pkg) => `${pkg.name}==${pkg.version}`);
  const missing = pins.filter((pin) => {
    const [name, version] = pin.split("==");
    const pkg = [...lock.packages, ...(lock.bootstrapWheels || [])].find(
      (entry) => entry.name === name && entry.version === version,
    );
    return !pkg || !existsSync(path.join(WHEELS_DIR, pkg.filename));
  });

  if (missing.length && !CHECK_ONLY) {
    const [command, prefix] = pythonCommand();
    log(`Загрузка ${missing.length} колёс через pip download…`);
    const flags = [
      "--only-binary=:all:",
      "--no-deps",
      "--disable-pip-version-check",
      "--platform",
      "win_amd64",
      "--python-version",
      "311",
      "--implementation",
      "cp",
      "--abi",
      "cp311",
    ];
    const result = await run(
      command,
      [...prefix, "-m", "pip", "download", ...flags, "--dest", WHEELS_DIR, ...missing],
      { echo: true },
    );
    if (result.code !== 0) {
      throw new Error(
        `pip download завершился с кодом ${result.code}. Проверьте, что сборка выполняется с Python 3.11 и доступом к pypi.org.`,
      );
    }
  }

  const problems = [];
  for (const pkg of [...lock.packages, ...(lock.bootstrapWheels || [])]) {
    const file = path.join(WHEELS_DIR, pkg.filename);
    if (!existsSync(file)) {
      problems.push(`отсутствует ${pkg.filename}`);
      continue;
    }
    if (sha256File(file) !== pkg.sha256) problems.push(`неверная контрольная сумма ${pkg.filename}`);
  }
  if (problems.length) {
    throw new Error(`Набор колёс не совпадает с python-lock.json:\n  ${problems.join("\n  ")}`);
  }
  log(`Колёс проверено: ${lock.packages.length + (lock.bootstrapWheels || []).length}`);
}

async function ensureRuntime(lock) {
  const archive = lock.python.archive;
  const archiveName = path.basename(new URL(archive.url).pathname);
  const cached = path.join(CACHE_DIR, archiveName);
  await fsp.mkdir(CACHE_DIR, { recursive: true });

  const alreadyBuilt = existsSync(path.join(RUNTIME_DIR, "python.exe"));
  if (alreadyBuilt && CHECK_ONLY) {
    log(`Встроенный Python уже собран: ${RUNTIME_DIR}`);
    return { sha256: archive.sha256, archiveName, reused: true };
  }
  if (alreadyBuilt && archive.sha256) {
    const manifestPath = path.join(BUNDLE_DIR, "manifest.json");
    if (existsSync(manifestPath)) {
      const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
      if (manifest.python?.archiveSha256 === archive.sha256) {
        log("Встроенный Python уже собран из проверенного архива — пропускаю распаковку.");
        return { sha256: archive.sha256, archiveName, reused: true };
      }
    }
  }

  if (!existsSync(cached)) {
    if (CHECK_ONLY) throw new Error(`Нет кэша архива Python: ${cached}`);
    log(`Загрузка ${archive.url}`);
    await download(archive.url, cached);
  }
  const digest = sha256File(cached);
  if (archive.sha256 && digest !== archive.sha256) {
    throw new Error(
      `Контрольная сумма архива Python не совпала.\n  ожидалось: ${archive.sha256}\n  получено: ${digest}\n` +
        "Удалите python-bundle/cache и повторите сборку.",
    );
  }
  if (!archive.sha256) {
    log(`::notice::python-archive-sha256=${digest} (закрепите значение в python-lock.json)`);
    log(`SHA-256 архива python.org: ${digest}`);
    if (WRITE_LOCK) {
      lock.python.archive.sha256 = digest;
      await fsp.writeFile(LOCK_PATH, `${JSON.stringify(lock, null, 2)}\n`, "utf8");
      log("Хеш записан в python-lock.json.");
    }
  }

  log("Распаковка официального embeddable-пакета Python…");
  await extract(cached, RUNTIME_DIR);
  const pthFile = path.join(RUNTIME_DIR, lock.python.pthFile || "python311._pth");
  await fsp.writeFile(pthFile, PTH_CONTENT, "utf8");
  await fsp.mkdir(path.join(RUNTIME_DIR, "Lib", "site-packages"), { recursive: true });
  return { sha256: digest, archiveName, reused: false };
}

async function main() {
  const lock = readLock();
  const { sha256: pythonSha, archiveName, reused } = await ensureRuntime(lock);
  await ensureWheels(lock);

  if (!CHECK_ONLY) {
    const manifest = {
      builtAt: new Date().toISOString(),
      builtOn: `${process.platform}-${process.arch}`,
      host: os.hostname(),
      python: {
        version: lock.python.version,
        archive: lock.python.archive.url,
        archiveFile: archiveName,
        archiveSha256: pythonSha,
        pinned: Boolean(lock.python.archive.sha256),
        runtimePath: "runtime",
        reused,
      },
      wheels: {
        count: lock.packages.length + (lock.bootstrapWheels || []).length,
        requirements: lock.requirements,
        pip: (lock.bootstrapWheels || [])[0]?.version || null,
        directory: "wheels",
      },
    };
    await fsp.writeFile(path.join(BUNDLE_DIR, "manifest.json"), `${JSON.stringify(manifest, null, 2)}\n`, "utf8");
    log(`Готово: ${path.relative(process.cwd(), RUNTIME_DIR)} + ${manifest.wheels.count} колёс.`);
  } else {
    log("Проверка встроенной среды пройдена.");
  }
}

main().catch((error) => {
  process.stderr.write(`[python-runtime] ОШИБКА: ${error.message}\n`);
  process.exitCode = 1;
});
