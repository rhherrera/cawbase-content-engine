"""Measure import cost and loaded third-party modules in FRESH processes (not evidence by itself).

Usage: python scripts/measure_import.py [--runs N]
Prints a JSON object with platform, versions and timings. Thresholds are NOT enforced here: the
provisional references in docs/installation-and-operations.md must be confirmed from recorded runs.
"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import subprocess
import sys
from importlib import metadata

PROBE = r"""
import sys, time, json
t0 = time.perf_counter()
import cawbase_content_engine
dt = (time.perf_counter() - t0) * 1000
print(json.dumps({"ms": dt, "modules": sorted({m.split(".")[0] for m in sys.modules})}))
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=15)
    args = ap.parse_args()
    samples, modules = [], []
    for _ in range(args.runs):
        out = subprocess.run([sys.executable, "-I", "-c", PROBE], capture_output=True, text=True, check=True).stdout
        data = json.loads(out)
        samples.append(data["ms"])
        modules = data["modules"]
    versions = {}
    for dist in (
        "cawbase-content-engine",
        "sqlalchemy",
        "pydantic",
        "asyncpg",
        "aiosqlite",
        "alembic",
        "fastapi",
        "uvicorn",
    ):
        try:
            versions[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            versions[dist] = None
    print(
        json.dumps(
            {
                "platform": platform.platform(),
                "machine": platform.machine(),
                "python": platform.python_version(),
                "runs": args.runs,
                "import_ms_median": round(statistics.median(samples), 1),
                "import_ms_min": round(min(samples), 1),
                "import_ms_max": round(max(samples), 1),
                "third_party_top_level_modules": [
                    m for m in modules if m not in sys.stdlib_module_names and not m.startswith("_")
                ],
                "versions": versions,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
