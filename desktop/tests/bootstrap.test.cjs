"use strict";
/**
 * Tests for the first-run environment bootstrap.
 *
 * They never touch the network, the real Windows filesystem layout, or a real
 * Python installation: process execution, downloads and unpacking are injected.
 * What is covered:
 *   - первый запуск создаёт изолированную среду в %LOCALAPPDATA%\DepthStudio;
 *   - повторный запуск использует готовую среду и ничего не переустанавливает;
 *   - повреждённая среда пересоздаётся, а «Восстановить среду» чистит runtime;
 *   - ошибки установки дают понятный код, журнал и деморежим;
 *   - системный Python никогда не используется, отпечаток версий учитывается.
 */
const assert = require("node:assert/strict");
const crypto = require("node:crypto");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");

const bootstrap = require("../bootstrap.cjs");

const PYTHON_VERSION = "3.11.9";

function sha256(text) {
  return crypto.createHash("sha256").update(text).digest("hex");
}

function makeTempDir(prefix) {
  return fs.mkdtempSync(path.join(os.tmpdir(), `depth-studio-${prefix}-`));
}

/**
 * Build a fake installer resource directory: python-lock.json, a bundled
 * interpreter, one fake wheel per package and the hash-pinned requirements.
 */
function makeResources(options = {}) {
  const root = makeTempDir("resources");
  const packages = options.packages || [
    { name: "numpy", version: "1.26.4" },
    { name: "open3d", version: "0.18.0" },
  ];
  const wheels = [];

  for (const pkg of packages) {
    const filename = `${pkg.name}-${pkg.version}-cp311-cp311-win_amd64.whl`;
    const content = `wheel ${pkg.name} ${pkg.version}`;
    wheels.push({
      name: pkg.name,
      version: pkg.version,
      filename,
      sha256: sha256(content),
      allowedHashes: [sha256(content)],
      direct: pkg.name === "numpy" || pkg.name === "open3d",
      content,
    });
  }
  const pipContent = "wheel pip 24.2";
  const pipWheel = {
    name: "pip",
    version: "24.2",
    filename: "pip-24.2-py3-none-any.whl",
    sha256: sha256(pipContent),
    content: pipContent,
  };

  const lock = {
    schemaVersion: 1,
    generatedFor: { platform: "win_amd64", pythonTag: "cp311" },
    python: {
      version: PYTHON_VERSION,
      archive: {
        url: `https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-embed-amd64.zip`,
        sha256: options.pythonSha256 === undefined ? "a".repeat(64) : options.pythonSha256,
      },
      pthFile: "python311._pth",
    },
    bootstrapWheels: [pipWheel],
    packages: wheels,
    requirements: packages.map((pkg) => `${pkg.name}==${pkg.version}`),
  };
  fs.writeFileSync(path.join(root, "python-lock.json"), JSON.stringify(lock, null, 2));
  fs.writeFileSync(path.join(root, "requirements-scan.lock.txt"), "numpy==1.26.4 --hash=sha256:abc\n");

  if (options.withRuntime !== false) {
    const runtime = path.join(root, "python-runtime");
    fs.mkdirSync(path.join(runtime, "Lib", "site-packages"), { recursive: true });
    fs.writeFileSync(path.join(runtime, "python.exe"), "fake interpreter");
    fs.writeFileSync(path.join(runtime, "python311._pth"), "python311.zip\r\n.\r\n");
  }
  if (options.withWheels !== false) {
    const wheelDir = path.join(root, "python-wheels");
    fs.mkdirSync(wheelDir, { recursive: true });
    for (const wheel of [...wheels, pipWheel]) {
      fs.writeFileSync(path.join(wheelDir, wheel.filename), wheel.content);
    }
  }

  return { root, lock, wheels, pipWheel };
}

function makeProbe(envPaths, overrides = {}) {
  return {
    executable: envPaths.pythonExe,
    prefix: envPaths.pythonRoot,
    version: PYTHON_VERSION,
    machine: "AMD64",
    userSite: "1",
    packages: {
      numpy: { available: true, version: "1.26.4", error: null },
      open3d: { available: true, version: "0.18.0", error: null },
      freenect: { available: false, version: null, error: "No module named 'freenect'" },
      ...overrides,
    },
  };
}

function createRunner(options = {}) {
  const calls = [];
  const envPaths = options.envPaths;
  const probe = options.probe || makeProbe(envPaths);
  const runner = async (command, args, runOptions = {}) => {
    calls.push({ command, args, env: runOptions.env, timeoutMs: runOptions.timeoutMs });
    if (args[0] === "-c" && String(args[1]).startsWith("import sys;print")) {
      return { code: 0, stdout: `${PYTHON_VERSION}\n`, stderr: "" };
    }
    if (args[0] === "-c" && String(args[1]).includes("DEPTH_STUDIO_PROBE")) {
      const data = typeof probe === "function" ? probe() : probe;
      return { code: 0, stdout: `DEPTH_STUDIO_PROBE=${JSON.stringify(data)}\n`, stderr: "" };
    }
    if (String(args[0]).endsWith("/pip") && args.includes("install")) {
      if (options.failPipViaWheel) return { code: 1, stdout: "", stderr: "Unable to execute pip from the wheel" };
      for (const name of ["numpy", "open3d", "pip"]) {
        fs.mkdirSync(path.join(envPaths.sitePackages, name), { recursive: true });
      }
      return { code: 0, stdout: "Successfully installed pip\n", stderr: "" };
    }
    if (args[0] === "-m" && args[1] === "pip" && args.includes("install")) {
      if (options.failPip) {
        return {
          code: 1,
          stdout: "Collecting numpy==1.26.4\n",
          stderr: "ERROR: Could not find a version that satisfies the requirement numpy==1.26.4",
        };
      }
      for (const name of ["numpy", "open3d", "pip"]) {
        fs.mkdirSync(path.join(envPaths.sitePackages, name), { recursive: true });
      }
      return { code: 0, stdout: "Successfully installed\n", stderr: "" };
    }
    if (args.includes("pip") && args.includes("install")) {
      if (options.failPip) {
        return {
          code: 1,
          stdout: "Collecting numpy==1.26.4\n",
          stderr: "ERROR: Could not find a version that satisfies the requirement numpy==1.26.4",
        };
      }
      // Настоящий pip создаёт каталоги пакетов — повторяем это для проверок среды.
      for (const name of ["numpy", "open3d", "pip"]) {
        fs.mkdirSync(path.join(envPaths.sitePackages, name), { recursive: true });
      }
      return { code: 0, stdout: "Successfully installed numpy open3d\n", stderr: "" };
    }
    return { code: 0, stdout: "", stderr: "" };
  };
  return { runner, calls };
}

function makeUnpack(envPaths, options = {}) {
  const calls = [];
  const unpack = async (archive, destination) => {
    calls.push({ archive, destination });
    if (options.fail) throw new bootstrap.SetupError("unpack_failed", "Не удалось распаковать архив.");
    fs.mkdirSync(path.join(destination, "Lib", "site-packages"), { recursive: true });
    fs.writeFileSync(path.join(destination, "python.exe"), "downloaded interpreter");
    fs.writeFileSync(path.join(destination, "python311._pth"), "python311.zip\r\n.\r\n");
  };
  return { unpack, calls };
}

function setupOptions(overrides = {}) {
  const envRoot = overrides.appDataRoot || makeTempDir("env");
  const resources = overrides.resources || makeResources();
  const envPaths = bootstrap.resolveEnvironmentPaths({ appDataRoot: envRoot, platform: "win32" });
  const { runner, calls } = createRunner({ envPaths, ...overrides.runner });
  const { unpack, calls: unpackCalls } = makeUnpack(envPaths, overrides.unpack);
  return {
    envRoot,
    resources,
    envPaths,
    calls,
    unpackCalls,
    options: {
      appDataRoot: envRoot,
      resourcesRoot: resources.root,
      bundleRoot: resources.root,
      platform: "win32",
      arch: "x64",
      runCommand: runner,
      unpack,
      download: async () => {
        throw new bootstrap.SetupError("download_failed", "Сеть не должна использоваться в этом тесте.");
      },
      emit: () => {},
      ...overrides.options,
    },
  };
}

test("первый запуск создаёт изолированную среду в каталоге приложения", async () => {
  const context = setupOptions();
  const result = await bootstrap.runSetup(context.options);

  assert.equal(result.ok, true, JSON.stringify(result.error));
  assert.equal(result.cached, false);
  assert.equal(result.envDir, context.envRoot);
  assert.deepEqual(
    result.steps.map((step) => step.status),
    ["done", "done", "done", "done", "done"],
  );
  assert.ok(fs.existsSync(path.join(context.envPaths.pythonRoot, "python.exe")));
  assert.ok(fs.existsSync(context.envPaths.stateFile));
  assert.equal(result.sources.python, "bundled");
  assert.equal(result.sources.packages, "bundled");
  assert.equal(result.warnings.some((warning) => warning.code === "sensor_driver_missing"), true);

  // Каждый запуск процесса выполнен именно интерпретатором Depth Studio.
  assert.ok(context.calls.length >= 3);
  for (const call of context.calls) {
    assert.equal(call.command, context.envPaths.pythonExe);
    assert.ok(call.command.startsWith(context.envRoot), "интерпретатор вне каталога Depth Studio");
  }
  const envOf = context.calls[0].env;
  assert.equal(envOf.PYTHONNOUSERSITE, "1");
  assert.equal(envOf.PYTHONPATH, undefined);
  assert.equal(envOf.PYTHONHOME, undefined);
});

test("site-packages включён для встроенного Python", async () => {
  const context = setupOptions();
  await bootstrap.runSetup(context.options);
  const pth = fs.readFileSync(path.join(context.envPaths.pythonRoot, "python311._pth"), "utf8");
  assert.match(pth, /Lib\\site-packages/);
  assert.match(pth, /^import site$/m);
});

test("повторный запуск использует готовую среду и ничего не переустанавливает", async () => {
  const context = setupOptions();
  const first = await bootstrap.runSetup(context.options);
  assert.equal(first.ok, true);

  const secondRunner = {
    runner: async () => {
      throw new Error("Повторный запуск не должен вызывать процессы установки");
    },
  };
  const second = await bootstrap.runSetup({
    ...context.options,
    runCommand: secondRunner.runner,
    emit: () => {},
  });

  assert.equal(second.ok, true);
  assert.equal(second.cached, true);
  assert.equal(second.steps.every((step) => step.status === "cached"), true);
});

test("quickStatus сообщает о проблемах до установки", async () => {
  const context = setupOptions();
  const before = await bootstrap.quickStatus({
    appDataRoot: context.envRoot,
    platform: "win32",
    resourcesRoot: context.resources.root,
  });
  assert.equal(before.ready, false);
  assert.ok(before.problems.some((problem) => problem.includes("манифест")));

  await bootstrap.runSetup(context.options);
  const after = await bootstrap.quickStatus({
    appDataRoot: context.envRoot,
    platform: "win32",
    resourcesRoot: context.resources.root,
  });
  assert.equal(after.ready, true);
  assert.equal(after.python?.version, PYTHON_VERSION);
});

test("удалённый интерпретатор приводит к переустановке, а не к ошибке", async () => {
  const context = setupOptions();
  await bootstrap.runSetup(context.options);
  fs.rmSync(path.join(context.envPaths.pythonRoot, "python.exe"));

  const repaired = await bootstrap.runSetup(context.options);
  assert.equal(repaired.ok, true);
  assert.equal(repaired.cached, false);
  assert.ok(fs.existsSync(path.join(context.envPaths.pythonRoot, "python.exe")));
  assert.ok(context.calls.length > 0);
});

test("«Восстановить среду» удаляет runtime и создаёт его заново", async () => {
  const context = setupOptions();
  await bootstrap.runSetup(context.options);
  const marker = path.join(context.envPaths.pythonRoot, "python.exe");
  assert.ok(fs.existsSync(marker));

  const repaired = await bootstrap.runSetup({ ...context.options, force: true });
  assert.equal(repaired.ok, true);
  assert.equal(repaired.cached, false);
  assert.ok(fs.existsSync(marker));
});

test("ошибка установки пакетов даёт понятный код, журнал и деморежим", async () => {
  const context = setupOptions({ runner: { failPip: true } });
  const result = await bootstrap.runSetup(context.options);

  assert.equal(result.ok, false);
  assert.equal(result.demoAvailable, true);
  assert.equal(result.error.code, "packages_failed");
  assert.match(result.error.message, /NumPy\/Open3D/);
  assert.ok(result.error.hint.length > 0);
  assert.match(result.error.details, /Could not find a version/);
  assert.equal(result.steps.find((step) => step.id === "packages").status, "error");
  assert.equal(fs.existsSync(context.envPaths.stateFile), false);

  const log = fs.readFileSync(context.envPaths.setupLog, "utf8");
  assert.match(log, /ERROR/);
  assert.match(log, /packages/);
});

test("pip ставится из колеса, а при сбое — резервным способом через PYTHONPATH", async () => {
  const context = setupOptions({ runner: { failPipViaWheel: true } });
  const result = await bootstrap.runSetup(context.options);
  assert.equal(result.ok, true, JSON.stringify(result.error));

  const pipCalls = context.calls.filter((call) => call.args.includes("install"));
  const wheelCall = pipCalls.find((call) => String(call.args[0]).endsWith("/pip"));
  const moduleCall = pipCalls.find((call) => call.args[0] === "-m" && call.args[1] === "pip");
  assert.ok(wheelCall, "первая попытка должна запускать pip из колеса");
  assert.match(String(wheelCall.args[0]), /\/pip$/);
  assert.ok(moduleCall, "должна быть резервная попытка через -m pip");
  assert.ok(moduleCall.env.PYTHONPATH.endsWith(".whl"), "PYTHONPATH должен указывать на колесо pip");
  assert.equal(moduleCall.command, context.envPaths.pythonExe);
});

test("без закреплённого SHA-256 архив Python не загружается", async () => {
  const resources = makeResources({ withRuntime: false, pythonSha256: null });
  const context = setupOptions({ resources });
  const result = await bootstrap.runSetup(context.options);

  assert.equal(result.ok, false);
  assert.equal(result.error.code, "python_hash_missing");
  assert.equal(result.demoAvailable, true);
});

test("неверная контрольная сумма архива Python отбрасывается", async () => {
  const resources = makeResources({ withRuntime: false, pythonSha256: "b".repeat(64) });
  const context = setupOptions({ resources });
  const downloaded = [];
  const result = await bootstrap.runSetup({
    ...context.options,
    download: async (url, destination) => {
      downloaded.push(destination);
      fs.mkdirSync(path.dirname(destination), { recursive: true });
      fs.writeFileSync(destination, "подменённый архив");
    },
    unpack: async () => {
      throw new Error("распаковка не должна выполняться");
    },
  });

  assert.equal(result.ok, false);
  assert.equal(result.error.code, "python_checksum_mismatch");
  assert.match(result.error.details, /ожидалось/);
  assert.equal(downloaded.length, 1);
  assert.equal(fs.existsSync(downloaded[0]), false, "повреждённый архив должен быть удалён");
  assert.equal(context.unpackCalls.length, 0);
});

test("загрузка Python идёт с python.org и только с проверкой TLS по умолчанию", async () => {
  const resources = makeResources({ withRuntime: false, pythonSha256: "c".repeat(64) });
  const context = setupOptions({ resources });
  const urls = [];
  // Скачиваем «правильный» архив: хеш в замке подменяем на вычисленный заранее.
  const archiveBody = "officially signed python archive";
  const digest = sha256(archiveBody);
  const lockPath = path.join(resources.root, "python-lock.json");
  const lock = JSON.parse(fs.readFileSync(lockPath, "utf8"));
  lock.python.archive.sha256 = digest;
  fs.writeFileSync(lockPath, JSON.stringify(lock, null, 2));

  const result = await bootstrap.runSetup({
    ...context.options,
    download: async (url, destination) => {
      urls.push(url);
      fs.mkdirSync(path.dirname(destination), { recursive: true });
      fs.writeFileSync(destination, archiveBody);
    },
  });

  assert.equal(result.ok, true, JSON.stringify(result.error));
  assert.equal(urls.length, 1);
  assert.match(urls[0], /^https:\/\/www\.python\.org\/ftp\/python\//);
  assert.equal(result.sources.python, "download");
});

test("интерпретатор вне каталога Depth Studio отклоняется", async () => {
  const envRoot = makeTempDir("env");
  const envPaths = bootstrap.resolveEnvironmentPaths({ appDataRoot: envRoot, platform: "win32" });
  const resources = makeResources();
  const context = setupOptions({
    resources,
    appDataRoot: envRoot,
    runner: {
      probe: () => ({
        ...makeProbe(envPaths),
        executable: "C:\\Python311\\python.exe",
        prefix: "C:\\Python311",
      }),
    },
  });
  const result = await bootstrap.runSetup(context.options);

  assert.equal(result.ok, false);
  assert.equal(result.error.code, "env_not_isolated");
  assert.equal(fs.existsSync(envPaths.stateFile), false);
});

test("недоступный Open3D не блокирует запуск: среда остаётся, деморежим работает", async () => {
  const envRoot = makeTempDir("env");
  const envPaths = bootstrap.resolveEnvironmentPaths({ appDataRoot: envRoot, platform: "win32" });
  const context = setupOptions({
    appDataRoot: envRoot,
    runner: {
      probe: makeProbe(envPaths, {
        open3d: { available: false, version: null, error: "ImportError: DLL load failed" },
      }),
    },
  });
  const result = await bootstrap.runSetup(context.options);

  assert.equal(result.ok, true);
  assert.equal(result.packages.open3d.available, false);
  assert.ok(result.warnings.some((warning) => warning.code === "open3d_unavailable"));
  assert.match(
    result.warnings.find((warning) => warning.code === "open3d_unavailable").hint,
    /Visual C\+\+/,
  );
  assert.equal(fs.existsSync(envPaths.stateFile), true);
});

test("изменение закреплённых версий требует переустановки", async () => {
  const resources = makeResources();
  const context = setupOptions({ resources });
  await bootstrap.runSetup(context.options);

  const lockPath = path.join(resources.root, "python-lock.json");
  const lock = JSON.parse(fs.readFileSync(lockPath, "utf8"));
  lock.packages[0].version = "1.27.0";
  lock.packages[0].filename = "numpy-1.27.0-cp311-cp311-win_amd64.whl";
  fs.writeFileSync(lockPath, JSON.stringify(lock, null, 2));

  const after = await bootstrap.runSetup(context.options);
  assert.equal(after.ok, true);
  assert.equal(after.cached, false, "смена версий должна запускать установку заново");
});

test("неподдерживаемая платформа объясняется без падения", async () => {
  const context = setupOptions();
  const result = await bootstrap.runSetup({ ...context.options, platform: "linux" });
  assert.equal(result.ok, false);
  assert.equal(result.error.code, "unsupported_platform");
  assert.match(result.error.hint, /npm run dev/);
});

test("каталог по умолчанию — %LOCALAPPDATA%\\DepthStudio", () => {
  const root = bootstrap.defaultAppDataRoot({
    platform: "win32",
    env: { LOCALAPPDATA: "C:\\Users\\user\\AppData\\Local" },
    homedir: "C:\\Users\\user",
  });
  assert.equal(root, path.join("C:\\Users\\user\\AppData\\Local", "DepthStudio"));
  assert.equal(bootstrap.APP_FOLDER_NAME, "DepthStudio");
});

test("parseProbe устойчив к мусору в выводе", () => {
  assert.equal(bootstrap.parseProbe(""), null);
  assert.equal(bootstrap.parseProbe("DEPTH_STUDIO_PROBE={bad json"), null);
  const parsed = bootstrap.parseProbe('лог\nDEPTH_STUDIO_PROBE={"version":"3.11.9"}\n');
  assert.equal(parsed.version, "3.11.9");
});

test("isInside не считает соседние каталоги частью среды", () => {
  assert.equal(bootstrap.isInside("C:\\Local\\DepthStudio", "C:\\Local\\DepthStudio\\runtime\\python\\python.exe"), true);
  assert.equal(bootstrap.isInside("C:\\Local\\DepthStudio", "C:\\Local\\DepthStudio2\\python.exe"), false);
});
