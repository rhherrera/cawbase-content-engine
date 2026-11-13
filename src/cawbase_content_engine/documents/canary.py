"""Canary run INSIDE the isolation: every probe must be blocked (docs 10.5). Reports; never raises."""

from __future__ import annotations

import os
import socket
import threading
from typing import Any


def run_canary() -> dict[str, Any]:
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
        for _ in range(512):
            threading.Thread(target=ev.wait, daemon=True).start()
            started += 1
        blocked["processes"] = False
    except RuntimeError:
        blocked["processes"] = True
    ev.set()
    return {"canary": blocked, "all_blocked": all(blocked.values())}
