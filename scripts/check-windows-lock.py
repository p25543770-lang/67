#!/usr/bin/env python3
"""Verify that python-lock.json is a complete, installable Windows closure.

The lock must satisfy two properties:

1. Every runtime requirement of every pinned package, evaluated with a *Windows*
   marker environment, is present in the lock and version-compatible. This is the
   check that catches `colorama; sys_platform == "win32"`-style gaps when the
   lock was generated on Linux.
2. Every locked package has the expected Windows x64 / CPython 3.11 wheel with the
   recorded SHA-256, and requirements-scan.lock.txt contains its hash.

Requires network access to pypi.org; used by CI.
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import sys

from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name

RESOLVER = pathlib.Path(__file__).resolve().parent / "resolve-windows-deps.py"
spec = importlib.util.spec_from_file_location("resolve_windows_deps", RESOLVER)
resolver = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(resolver)
WINDOWS_MARKERS = resolver.WINDOWS_MARKERS
pypi_json = resolver.pypi_json
wheel_for = resolver.wheel_for

LOCK = pathlib.Path("python-lock.json")
LOCK_TXT = pathlib.Path("requirements-scan.lock.txt")
GITHUB_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"

# Консоль Windows по умолчанию не в UTF-8 — русские сообщения не должны ронять проверку.
for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def main() -> int:
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    pinned = {canonicalize_name(pkg["name"]): pkg for pkg in lock["packages"]}
    problems: list[str] = []

    for name, pkg in sorted(pinned.items()):
        data = pypi_json(name, pkg["version"])
        for raw in data["info"].get("requires_dist") or []:
            try:
                requirement = Requirement(raw)
            except Exception as error:  # noqa: BLE001
                problems.append(f"{name}: не удалось разобрать зависимость {raw!r}: {error}")
                continue
            if requirement.marker is not None and not requirement.marker.evaluate(WINDOWS_MARKERS):
                continue
            dependency = canonicalize_name(requirement.name)
            if dependency not in pinned:
                problems.append(
                    f"{name}=={pkg['version']} требует {raw}, но {dependency} отсутствует в python-lock.json"
                )
            elif requirement.specifier and not requirement.specifier.contains(
                pinned[dependency]["version"], prereleases=False
            ):
                problems.append(
                    f"{name} требует {raw}, а закреплено {dependency}=={pinned[dependency]['version']}"
                )

    for name, pkg in sorted(pinned.items()):
        file = wheel_for(name, pkg["version"])
        if file is None:
            problems.append(f"{name}=={pkg['version']}: нет колеса для Windows x64 / CPython 3.11")
            continue
        if file["filename"] != pkg["filename"]:
            problems.append(
                f"{name}: в замке {pkg['filename']}, а совместимое колесо — {file['filename']}"
            )
        if file["digests"]["sha256"] != pkg["sha256"]:
            problems.append(f"{name}: SHA-256 в замке не совпадает с PyPI")

    lock_lines = LOCK_TXT.read_text(encoding="utf-8")
    for name, pkg in sorted(pinned.items()):
        if f"{name}=={pkg['version']}" not in lock_lines:
            problems.append(f"{name}=={pkg['version']} отсутствует в requirements-scan.lock.txt")
        if pkg["sha256"] not in lock_lines:
            problems.append(f"SHA-256 пакета {name} отсутствует в requirements-scan.lock.txt")

    if problems:
        print("Проверка замка зависимостей не пройдена:", file=sys.stderr)
        for problem in problems:
            print(f"::error::{problem}" if GITHUB_ACTIONS else f"  - {problem}", file=sys.stderr)
        return 1

    print(f"Замок зависимостей согласован: {len(pinned)} пакетов, Windows x64 / CPython 3.11.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
