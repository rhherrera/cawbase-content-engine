"""Build the wheel, install it into a CLEAN venv (outside the source tree) and measure its import cost.

    python scripts/measure_distribution.py [--wheel PATH] [--cold-runs 5] [--warm-runs 15] [--extras ""]

Needs ``uv``. Prints the JSON produced by ``measure_import.py`` run with the new venv's interpreter.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wheel")
    ap.add_argument("--extras", default="", help="e.g. postgres,migrations (default: base only)")
    ap.add_argument("--cold-runs", default="5")
    ap.add_argument("--warm-runs", default="15")
    args = ap.parse_args()
    uv = shutil.which("uv")
    assert uv, "uv is required"
    work = Path(tempfile.mkdtemp(prefix="cce-measure-"))
    wheel = args.wheel
    if wheel is None:
        subprocess.run(
            [uv, "build", "--wheel", "--out-dir", str(work / "dist"), str(ROOT)], check=True, capture_output=True
        )
        wheel = str(next((work / "dist").glob("*.whl")))
    venv = work / "venv"
    subprocess.run([uv, "venv", "--python", "3.13", str(venv), "-q"], check=True)
    spec = f"cawbase-content-engine[{args.extras}] @ {Path(wheel).resolve().as_uri()}" if args.extras else wheel
    subprocess.run([uv, "pip", "install", "--python", str(venv / "bin" / "python"), "-q", spec], check=True)
    script = work / "measure_import.py"
    shutil.copy(ROOT / "scripts" / "measure_import.py", script)
    subprocess.run(
        [str(venv / "bin" / "python"), str(script), "--cold-runs", args.cold_runs, "--warm-runs", args.warm_runs],
        check=True,
        cwd=work,
    )
    shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
