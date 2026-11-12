"""Measure import cost of the INSTALLED package in fresh processes: cold (no bytecode) vs warm.

Run it with the interpreter of an environment the wheel was installed into (``measure_distribution.py``
does that for you):

    python scripts/measure_import.py [--cold-runs 5] [--warm-runs 15]

* cold  = every ``__pycache__`` under site-packages is deleted before each sample, so Python compiles
          the package and all its dependencies on that first import (what a freshly installed
          environment pays once);
* warm  = new processes with bytecode already available (the steady state).

Cold and warm are reported separately, with sample count, median, min and max. Nothing is compared
against a threshold here; the decided gate (D-33: warm median <= 300 ms, installed size <= 40 MiB) is
asserted by tests/test_distribution.py::test_installed_import_cost_meets_the_decided_references.
"""

from __future__ import annotations

import argparse
import json
import platform
import shutil
import site
import statistics
import subprocess
import sys
from importlib import metadata
from pathlib import Path

PROBE = r"""
import sys, time, json
t0 = time.perf_counter()
import cawbase_content_engine
dt = (time.perf_counter() - t0) * 1000
print(json.dumps({"ms": dt, "modules": sorted({m.split(".")[0] for m in sys.modules})}))
"""


def one_sample() -> tuple[float, list[str]]:
    out = subprocess.run([sys.executable, "-I", "-c", PROBE], capture_output=True, text=True, check=True).stdout
    data = json.loads(out)
    return data["ms"], data["modules"]


def clear_bytecode() -> int:
    removed = 0
    for root in site.getsitepackages():
        for cache in Path(root).rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)
            removed += 1
    return removed


def stats(samples: list[float]) -> dict[str, float | int]:
    return {
        "samples": len(samples),
        "median_ms": round(statistics.median(samples), 1),
        "min_ms": round(min(samples), 1),
        "max_ms": round(max(samples), 1),
        "all_ms": [round(s, 1) for s in samples],
    }


def installed_size_kib() -> int:
    total = 0
    for root in site.getsitepackages():
        total += sum(f.stat().st_size for f in Path(root).rglob("*") if f.is_file() and "__pycache__" not in f.parts)
    return round(total / 1024)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cold-runs", type=int, default=5)
    ap.add_argument("--warm-runs", type=int, default=15)
    args = ap.parse_args()
    cold, warm, modules = [], [], []
    for _ in range(args.cold_runs):
        clear_bytecode()
        ms, modules = one_sample()
        cold.append(ms)
    for _ in range(args.warm_runs):
        ms, modules = one_sample()
        warm.append(ms)
    third_party = [m for m in modules if m not in sys.stdlib_module_names and not m.startswith("_")]
    dists = sorted(f"{d.metadata['Name']}=={d.version}" for d in metadata.distributions())
    print(
        json.dumps(
            {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "cold_first_import (bytecode cleared before each sample)": stats(cold),
                "warm_import (new processes, bytecode available)": stats(warm),
                "installed_size_kib_excluding_pycache": installed_size_kib(),
                "installed_distributions": dists,
                "third_party_top_level_modules_loaded_on_import": third_party,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
