#!/usr/bin/env python3
"""Wraps bench/measure.py for the GPU-validation tests (CCE_MEASURE_SCRIPT): runs the real measurement unless the
environment injects a fault for `CMD:LABEL` (e.g. `run:run-candidate-16k-2slot-c2`, `limits:candidate-16k-2slot`):

  FAKE_MEASURE_FAIL=CMD:LABEL[,...]  exit 1 without writing evidence (a measurement that ran and failed)
  FAKE_MEASURE_HANG=CMD:LABEL        touch $FAKE_MEASURE_DIR/hang, then sleep (to interrupt the campaign there)
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

MEASURE = Path(__file__).resolve().parents[2] / "bench" / "measure.py"


def main(argv: list[str]) -> int:
    label = argv[argv.index("--label") + 1] if "--label" in argv else ""
    key = f"{argv[0]}:{label}"
    if key in os.environ.get("FAKE_MEASURE_FAIL", "").split(","):
        print(f"simulated measurement failure: {key}", file=sys.stderr)
        return 1
    if key == os.environ.get("FAKE_MEASURE_HANG"):
        (Path(os.environ["FAKE_MEASURE_DIR"]) / "hang").write_text(key)
        time.sleep(600)
        return 1
    os.execv(sys.executable, [sys.executable, str(MEASURE), *argv])  # noqa: S606 - the real measurement


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
