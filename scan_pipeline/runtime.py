"""Conservative CPU thread selection, before importing NumPy/Open3D.

Do not bind threads to cores: Windows' scheduler knows more about hybrid P/E
cores than this application. An explicit environment setting always wins.
"""
from __future__ import annotations

import os


def available_logical_cpus() -> int:
    count = max(1, os.cpu_count() or 1)
    try:
        # Honour restricted affinity where available (not exposed on Windows).
        count = min(count, max(1, len(os.sched_getaffinity(0))))
    except (AttributeError, OSError):
        pass
    return count


def default_openmp_threads(logical_cpus: int) -> int:
    """Leave capacity for capture, the UI, and the OS; cap small ICP jobs.

    An i5-13400 normally exposes 16 logical processors, so defaults to four
    OpenMP threads rather than the old blanket limit of two. This is a starting
    point, not a measured optimum for every CPU or scene.
    """
    if logical_cpus >= 12:
        return 4
    if logical_cpus >= 6:
        return 3
    return 2 if logical_cpus >= 2 else 1


def configure_threads(env=None, logical_cpus: int | None = None) -> dict:
    target = os.environ if env is None else env
    count = available_logical_cpus() if logical_cpus is None else max(1, int(logical_cpus))
    target.setdefault("OMP_NUM_THREADS", str(default_openmp_threads(count)))
    # Small BLAS matrices do not benefit from a second thread pool.
    target.setdefault("OPENBLAS_NUM_THREADS", "1")
    target.setdefault("MKL_NUM_THREADS", "1")
    return {"logicalCpus": count, "openmpThreads": target.get("OMP_NUM_THREADS"),
            "openblasThreads": target.get("OPENBLAS_NUM_THREADS"), "mklThreads": target.get("MKL_NUM_THREADS")}


def thread_info() -> dict:
    """Report requested settings; do not imply they are observed thread counts."""
    return {"logicalCpus": available_logical_cpus(), "openmpThreads": os.environ.get("OMP_NUM_THREADS"),
            "openblasThreads": os.environ.get("OPENBLAS_NUM_THREADS"),
            "mklThreads": os.environ.get("MKL_NUM_THREADS")}
