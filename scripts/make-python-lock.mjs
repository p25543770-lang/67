#!/usr/bin/env node
/**
 * Build python-lock.json + requirements-scan.lock.txt from a directory of
 * Windows wheels that were resolved by pip.
 *
 * The lock is the single source of truth for:
 *  - the Python build that ships in the installer (python-3.11.9-embed-amd64.zip),
 *  - the pip bootstrap wheel,
 *  - every runtime package (exact version + SHA-256).
 *
 * Usage:
 *   node scripts/make-python-lock.mjs --wheels <dir> --pip-wheel <file.whl>
 *   node scripts/make-python-lock.mjs --wheels <dir> --pip-wheel <file> --python-sha256 <hash>
 */
import { createHash } from "node:crypto";
import { readdirSync, readFileSync, writeFileSync } from "node:fs";
import path from "node:path";

const DIRECT_REQUIREMENTS = ["numpy", "open3d", "opencv-python", "pythonnet"];
const PYTHON_VERSION = "3.11.9";
const PYTHON_ARCHIVE_URL = `https://www.python.org/ftp/python/${PYTHON_VERSION}/python-${PYTHON_VERSION}-embed-amd64.zip`;

function parseArgs(argv) {
  const args = { wheels: null, pipWheel: null, pythonSha256: undefined };
  for (let index = 0; index < argv.length; index += 1) {
    const key = argv[index];
    const value = argv[index + 1];
    if (key === "--wheels") {
      args.wheels = value;
      index += 1;
    } else if (key === "--pip-wheel") {
      args.pipWheel = value;
      index += 1;
    } else if (key === "--python-sha256") {
      args.pythonSha256 = value;
      index += 1;
    }
  }
  if (!args.wheels || !args.pipWheel) {
    throw new Error("Usage: make-python-lock.mjs --wheels <dir> --pip-wheel <file.whl> [--python-sha256 <hash>]");
  }
  return args;
}

/** Split a wheel filename into normalized name, version, and build/tag tail. */
function parseWheelFilename(filename) {
  const match = /^(?<name>.+?)-(?<version>[^-]+)(?:-(?<build>[^-]+))?-(?<python>[^-]+)-(?<abi>[^-]+)-(?<platform>[^-]+)\.whl$/.exec(
    filename,
  );
  if (!match?.groups) throw new Error(`Unrecognised wheel filename: ${filename}`);
  return {
    name: match.groups.name.replace(/_/g, "-").toLowerCase(),
    version: match.groups.version,
    filename,
  };
}

function sha256File(file) {
  return createHash("sha256").update(readFileSync(file)).digest("hex");
}

async function pypiHashes(name, version) {
  const url = `https://pypi.org/pypi/${name}/${version}/json`;
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  if (!response.ok) throw new Error(`PyPI lookup failed for ${name}==${version}: HTTP ${response.status}`);
  const payload = await response.json();
  const hashes = new Set();
  for (const file of payload.urls || []) {
    if (!file.filename.endsWith(".whl")) continue;
    // Only wheel tags that a Windows x64 / CPython 3.11 interpreter can install.
    if (!file.filename.endsWith("-none-any.whl") && !file.filename.endsWith("-win_amd64.whl")) continue;
    if (file.yanked) continue;
    hashes.add(file.digests.sha256);
  }
  return [...hashes].sort();
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const files = readdirSync(args.wheels).filter((file) => file.endsWith(".whl"));
  const packages = files.map((filename) => {
    const meta = parseWheelFilename(filename);
    return { ...meta, sha256: sha256File(path.join(args.wheels, filename)) };
  });

  const byName = new Map();
  for (const pkg of packages) {
    const existing = byName.get(pkg.name);
    if (existing && existing.version !== pkg.version) {
      throw new Error(`Multiple versions resolved for ${pkg.name}: ${existing.version} / ${pkg.version}`);
    }
    byName.set(pkg.name, pkg);
  }

  const locked = [];
  for (const name of [...byName.keys()].sort()) {
    const pkg = byName.get(name);
    // Extra hashes for alternative Windows-compatible wheels of the same
    // version, so `pip --require-hashes` also succeeds if pip prefers a
    // different (but equivalent) wheel tag on a real Windows host.
    let extra = [];
    try {
      extra = await pypiHashes(name, pkg.version);
    } catch (error) {
      console.warn(`warning: ${error.message} (using local hash only)`);
    }
    const hashes = [...new Set([pkg.sha256, ...extra])].sort();
    locked.push({
      name,
      version: pkg.version,
      filename: pkg.filename,
      sha256: pkg.sha256,
      allowedHashes: hashes,
      direct: DIRECT_REQUIREMENTS.includes(name),
    });
  }

  const pipWheelName = path.basename(args.pipWheel);
  const pipMeta = parseWheelFilename(pipWheelName);
  const pipWheel = {
    name: pipMeta.name,
    version: pipMeta.version,
    filename: pipWheelName,
    sha256: sha256File(args.pipWheel),
  };

  // pip ставится распаковкой колеса, а не через `pip install pip`, поэтому он
  // не должен попадать в список runtime-пакетов.
  const runtimePackages = locked.filter((pkg) => pkg.name !== pipMeta.name);
  if (runtimePackages.length === locked.length) {
    console.warn("warning: колесо pip не найдено в наборе — проверьте --pip-wheel");
  }

  const direct = runtimePackages.filter((pkg) => pkg.direct);
  for (const name of DIRECT_REQUIREMENTS) {
    if (!direct.some((pkg) => pkg.name === name)) throw new Error(`Missing direct requirement ${name} in resolved wheel set`);
  }

  let previous = {};
  try {
    previous = JSON.parse(readFileSync("python-lock.json", "utf8"));
  } catch {
    previous = {};
  }
  const pythonSha256 = args.pythonSha256 ?? previous?.python?.archive?.sha256 ?? null;

  const lock = {
    schemaVersion: 1,
    generatedFor: { platform: "win_amd64", pythonTag: "cp311" },
    python: {
      version: PYTHON_VERSION,
      archive: { url: PYTHON_ARCHIVE_URL, sha256: pythonSha256 },
      pthFile: "python311._pth",
      requires: "Windows x64, официальная сборка python.org (embeddable package)",
    },
    bootstrapWheels: [pipWheel],
    packages: runtimePackages,
    requirements: DIRECT_REQUIREMENTS.map((name) => `${name}==${runtimePackages.find((pkg) => pkg.name === name).version}`),
  };

  writeFileSync("python-lock.json", `${JSON.stringify(lock, null, 2)}\n`);

  const lines = [
    "# Сгенерировано scripts/make-python-lock.mjs — не редактируйте вручную.",
    "# Полный набор зависимостей Depth Studio для Windows x64 / CPython 3.11.",
    "# Установка: python -m pip install --only-binary=:all: --require-hashes -r requirements-scan.lock.txt",
  ];
  for (const pkg of runtimePackages) {
    lines.push(`${pkg.name}==${pkg.version} \\`);
    for (const hash of pkg.allowedHashes) lines.push(`    --hash=sha256:${hash}`);
  }
  writeFileSync("requirements-scan.lock.txt", `${lines.join("\n").replace(/ \\\n$/, "")}\n`);

  console.log(
    `Locked ${runtimePackages.length} runtime packages (${runtimePackages.filter((pkg) => pkg.direct).length} direct) from ${files.length} wheels; pip ${pipWheel.version} in bootstrapWheels.`,
  );
  console.log(`python ${PYTHON_VERSION}: ${pythonSha256 || "<sha256 not pinned yet>"}`);
}

main().catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
