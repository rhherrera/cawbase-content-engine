"""Canary run INSIDE the isolation: every probe must be blocked (docs 10.5). Reports; never raises."""

from __future__ import annotations

import os
import socket
import threading
from typing import Any


def run_canary(params: dict[str, Any] | None = None) -> dict[str, Any]:
    blocked: dict[str, bool] = {}
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(1)
        s.connect(("93.184.216.34", 53))
        s.send(b"x")
        s.close()
        blocked["network"] = False
    except OSError:
        blocked["network"] = True
    for name, path in (
        ("write_etc", "/etc/cce-canary"),
        ("write_src", "/opt/cce/src/cce-canary"),
        ("write_root", "/cce-canary"),
    ):
        try:
            with open(path, "w") as fh:
                fh.write("x")
            os.unlink(path)
            blocked[name] = False
        except OSError:
            blocked[name] = True
    sensitive = [
        k for k in os.environ if any(w in k.upper() for w in ("DATABASE", "SECRET", "TOKEN", "PASSWORD", "CAWBASE"))
    ]
    blocked["environment"] = not sensitive
    started = 0
    ev = threading.Event()
    try:
        for _ in range(int((params or {}).get("expected_pids", 496)) + 16):
            threading.Thread(target=ev.wait, daemon=True).start()
            started += 1
        blocked["processes"] = False
    except RuntimeError:
        blocked["processes"] = True
    ev.set()
    return {
        "canary": blocked,
        "all_blocked": all(blocked.values()),
        "memory_limit_bytes": _cgroup_memory_limit(),
    }


def _cgroup_memory_limit() -> int | None:
    """The memory limit the container really has (cgroup v2, then v1); ``None`` when not visible."""
    for path in ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            with open(path) as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        if raw.isdigit() and int(raw) < 1 << 60:
            return int(raw)
        if raw == "max":
            return 1 << 62  # unlimited
    return None


def probe_memory(nbytes: int) -> None:
    """Allocates and touches ``nbytes``. Inside a correctly limited container this never returns (OOM kill)."""
    buf = bytearray(nbytes)
    for i in range(0, nbytes, 4096):
        buf[i] = 1
