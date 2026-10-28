#!/usr/bin/env python3
"""Resolve the complete *Windows* dependency closure for Depth Studio.

`pip download --platform win_amd64` picks the right *wheels* but still evaluates
environment markers on the build host (Linux). Windows-only requirements such as
`colorama; sys_platform == "win32"` (ipython) or `pywin32; sys_platform == "win32"`
(jupyter-core) are therefore missing from a lock generated on Linux and the
first Windows install fails.

This script walks the metadata with a Windows marker environment, keeps the
versions already pinned in python-lock.json, resolves anything new to the newest
version that ships a CPython 3.11 win_amd64 wheel, downloads those wheels and
rewrites python-lock.json / requirements-scan.lock.txt.

Usage:
  python scripts/resolve-windows-deps.py --dest python-bundle/wheels
"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import sys
import urllib.request
from collections import defaultdict

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import Version

# pythonnet — мост к .NET: через него адаптер Kinect 360 работает с официальным
# SDK 1.8 и не трогает установленный драйвер Microsoft Kinect.
ROOT_REQUIREMENTS = ("numpy==1.26.4", "open3d==0.18.0", "pythonnet==3.2.1")

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
PYTHON_VERSION = "3.11.9"

WINDOWS_MARKERS = {
    "implementation_name": "cpython",
    "implementation_version": PYTHON_VERSION,
    "os_name": "nt",
    "platform_machine": "AMD64",
    "platform_release": "10",
    "platform_system": "Windows",
    "platform_version": "10.0.19045",
    "python_full_version": PYTHON_VERSION,
    "platform_python_implementation": "CPython",
    "python_version": "3.11",
    "sys_platform": "win32",
    "extra": "",
}


def log(message: str) -> None:
    print(f"[resolve] {message}", flush=True)


def pypi_json(name: str, version: str | None = None) -> dict:
    url = f"https://pypi.org/pypi/{name}/{version}/json" if version else f"https://pypi.org/pypi/{name}/json"
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "DepthStudio-Lock/1.0"})
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def wheel_for(name: str, version: str) -> dict | None:
    """Return the best CPython 3.11 Windows x64 wheel for a release."""
    data = pypi_json(name, version)
    requires_python = data["info"].get("requires_python") or ""
    if requires_python:
        try:
            if Version(PYTHON_VERSION) not in SpecifierSet(requires_python):
                return None
        except Exception:  # noqa: BLE001 - некорректное ограничение на PyPI
            return None
    candidates = []
    for file in data["urls"]:
        filename = file["filename"]
        if not filename.endswith(".whl") or file.get("yanked"):
            continue
        tags = wheel_tags(filename)
        if tags is None or not tags_compatible(*tags):
            continue
        candidates.append(file)
    if not candidates:
        return None
    return sorted(candidates, key=lambda file: rank_wheel(file["filename"]))[0]


def wheel_tags(filename: str) -> tuple[str, str, str] | None:
    parts = filename[: -len(".whl")].split("-")
    if len(parts) < 5:
        return None
    return parts[-3], parts[-2], parts[-1]


def compressed_tags(tag: str) -> list[str]:
    """PEP 425 allows compressed tag sets such as ``cp311.cp312.cp313-none-any``."""
    return [item for item in tag.split(".") if item]


def tags_compatible(python_tag: str, abi_tag: str, platform_tag: str) -> bool:
    """Can CPython 3.11 on Windows x64 install this wheel?"""
    platforms = compressed_tags(platform_tag)
    if not any(item in ("win_amd64", "any") for item in platforms):
        return False
    python_tags = compressed_tags(python_tag)
    for abi in compressed_tags(abi_tag):
        if abi == "none":
            # Чистые колёса: py3/py2.py3 или конкретный cp311 (в том числе сжатый набор).
            if any(item in ("py3", "py2.py3", "cp311") for item in python_tags):
                return True
        elif abi == "abi3":
            for item in python_tags:
                match = re.fullmatch(r"cp3(\d+)", item)
                if match and int(match.group(1)) <= 11:
                    return True
        elif abi == "cp311" and "cp311" in python_tags:
            return True
    return False


def rank_wheel(filename: str) -> tuple[int, str]:
    python_tag, abi_tag, platform_tag = wheel_tags(filename)
    abis = set(compressed_tags(abi_tag))
    if "cp311" in abis:
        specificity = 0
    elif "abi3" in abis:
        specificity = 1
    else:
        specificity = 2
    return specificity, filename


def compatible_wheel_hashes(name: str, version: str) -> list[str]:
    """SHA-256 of every wheel a Windows x64 / CPython 3.11 host could install.

    Keeping them all in ``allowedHashes`` lets ``pip --require-hashes`` accept a
    different-but-equivalent wheel tag on a real Windows machine.
    """
    hashes = set()
    for file in pypi_json(name, version)["urls"]:
        filename = file["filename"]
        if not filename.endswith(".whl") or file.get("yanked"):
            continue
        tags = wheel_tags(filename)
        if tags is None or not tags_compatible(*tags):
            continue
        hashes.add(file["digests"]["sha256"])
    return sorted(hashes)


def releases_newest_first(name: str) -> list[str]:
    data = pypi_json(name)
    versions = []
    for version in data["releases"]:
        try:
            parsed = Version(version)
        except Exception:  # noqa: BLE001
            continue
        if parsed.is_prerelease:
            continue
        versions.append(parsed)
    versions.sort(reverse=True)
    return [str(version) for version in versions]


def resolve(pinned: dict[str, str]) -> dict[str, str]:
    """Close the dependency graph for a Windows CPython 3.11 environment."""
    chosen: dict[str, str] = {canonicalize_name(name): version for name, version in pinned.items()}
    constraints: dict[str, list[SpecifierSet]] = defaultdict(list)
    pending: list[str] = list(ROOT_REQUIREMENTS) + [f"{name}=={version}" for name, version in pinned.items()]
    processed: set[tuple[str, str]] = set()

    while pending:
        requirement = Requirement(pending.pop())
        if requirement.marker is not None and not requirement.marker.evaluate(WINDOWS_MARKERS):
            continue
        name = canonicalize_name(requirement.name)
        specifier = requirement.specifier
        if name in chosen:
            if specifier and not specifier.contains(chosen[name], prereleases=False):
                raise SystemExit(
                    f"конфликт версий: {name}=={chosen[name]} не удовлетворяет {specifier}"
                )
            constraints[name].append(specifier)
            key = (name, chosen[name])
            if key in processed:
                continue
            processed.add(key)
            version = chosen[name]
        else:
            options = [
                version
                for version in releases_newest_first(name)
                if (not specifier or specifier.contains(version, prereleases=False))
                and wheel_for(name, version)
            ]
            if not options:
                raise SystemExit(f"не найдено совместимого колеса Windows x64 / CPython 3.11 для {name} {specifier}")
            version = options[0]
            chosen[name] = version
            constraints[name].append(specifier)
            processed.add((name, version))
            log(f"{name}=={version} (новое, требует: {specifier or 'любая версия'})")

        data = pypi_json(name, version)
        for raw in data["info"].get("requires_dist") or []:
            pending.append(raw)
    return chosen


DIRECT_REQUIREMENTS = ("numpy", "open3d", "pythonnet")


def build_lock(chosen: dict[str, str], wheels: dict[str, dict], previous: dict) -> dict:
    """Serialize the resolved Windows closure into python-lock.json."""
    packages = []
    for name, version in sorted(chosen.items()):
        file = wheels[name]
        local = file["digests"]["sha256"]
        hashes = sorted({local, *compatible_wheel_hashes(name, version)})
        packages.append(
            {
                "name": name,
                "version": version,
                "filename": file["filename"],
                "sha256": local,
                "allowedHashes": hashes,
                "direct": name in DIRECT_REQUIREMENTS,
            }
        )
    python = json.loads(json.dumps(previous.get("python") or {}))
    python.setdefault("version", PYTHON_VERSION)
    python.setdefault(
        "archive",
        {
            "url": f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-embed-amd64.zip",
            "sha256": None,
        },
    )
    python.setdefault("pthFile", "python311._pth")
    return {
        "schemaVersion": 1,
        "generatedFor": {"platform": "win_amd64", "pythonTag": "cp311"},
        "python": python,
        "bootstrapWheels": previous.get("bootstrapWheels") or [],
        "packages": packages,
        "requirements": [f"{name}=={chosen[name]}" for name in DIRECT_REQUIREMENTS if name in chosen],
    }


def write_lock(lock: dict, lock_path: pathlib.Path, txt_path: pathlib.Path) -> None:
    lock_path.write_text(f"{json.dumps(lock, indent=2, ensure_ascii=False)}\n", encoding="utf-8")
    lines = [
        "# Сгенерировано scripts/make-python-lock.mjs — не редактируйте вручную.",
        "# Полный набор зависимостей Depth Studio для Windows x64 / CPython 3.11.",
        "# Установка: python -m pip install --only-binary=:all: --require-hashes -r requirements-scan.lock.txt",
    ]
    for pkg in lock["packages"]:
        lines.append(f"{pkg['name']}=={pkg['version']} \\")
        for digest in pkg["allowedHashes"]:
            lines.append(f"    --hash=sha256:{digest}")
    body = "\n".join(lines)
    if body.endswith(" \\"):
        body = body[:-2]
    txt_path.write_text(f"{body}\n", encoding="utf-8")


def download(wheels: dict[str, dict], destination: pathlib.Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name, file in sorted(wheels.items()):
        target = destination / file["filename"]
        if target.exists():
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if digest == file["digests"]["sha256"]:
                continue
        log(f"скачивание {file['filename']}")
        request = urllib.request.Request(file["url"], headers={"User-Agent": "DepthStudio-Lock/1.0"})
        with urllib.request.urlopen(request, timeout=300) as response:
            target.write_bytes(response.read())
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != file["digests"]["sha256"]:
            raise SystemExit(f"контрольная сумма не совпала: {file['filename']}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dest", default="python-bundle/wheels")
    parser.add_argument("--skip-download", action="store_true")
    parser.add_argument(
        "--emit-lock",
        action="store_true",
        help="перезаписать python-lock.json и requirements-scan.lock.txt без скачивания колёс",
    )
    args = parser.parse_args()

    lock_path = pathlib.Path("python-lock.json")
    pinned: dict[str, str] = {}
    if lock_path.exists():
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        pinned = {canonicalize_name(pkg["name"]): pkg["version"] for pkg in lock.get("packages", [])}

    log(f"закреплено версий: {len(pinned)}")
    chosen = resolve(pinned)

    wheels: dict[str, dict] = {}
    for name, version in sorted(chosen.items()):
        file = wheel_for(name, version)
        if file is None:
            raise SystemExit(f"нет колеса Windows x64 для {name}=={version}")
        wheels[name] = file

    added = sorted(set(wheels) - set(pinned))
    log(f"всего пакетов: {len(wheels)}; добавлено для Windows: {', '.join(added) or 'ничего'}")

    if args.emit_lock:
        if not lock.get("bootstrapWheels"):
            raise SystemExit("в python-lock.json нет bootstrapWheels (pip) — замок не перезаписан")
        write_lock(build_lock(chosen, wheels, lock), lock_path, pathlib.Path("requirements-scan.lock.txt"))
        log(f"замок перезаписан: {len(chosen)} пакетов, pip {lock['bootstrapWheels'][0]['version']}")

    if not args.skip_download and not args.emit_lock:
        destination = pathlib.Path(args.dest)
        download(wheels, destination)
        log(f"колёса в {destination}")
        if not wheels.get("pip"):
            raise SystemExit("pip отсутствует в наборе — добавьте его отдельно")
    return 0


if __name__ == "__main__":
    sys.exit(main())
