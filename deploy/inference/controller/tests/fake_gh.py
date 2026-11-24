#!/usr/bin/env python3
"""A fake `gh` CLI for the enablement tests: workflow runs of the OIDC diagnostic (with their logs), dispatch and
repository variables, kept in $FAKE_GH_STATE (JSON). No GitHub call is ever made. Every call is appended to
$FAKE_GH_STATE.calls. FAKE_GH_DISPATCH_LOG is the log of a run created by `gh workflow run`."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main(argv: list[str]) -> int:
    path = Path(os.environ["FAKE_GH_STATE"])
    with open(f"{path}.calls", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(argv) + "\n")
    st = json.loads(path.read_text()) if path.exists() else {"runs": [], "variables": {}}
    cmd = argv[:2]
    if cmd == ["run", "list"]:
        print(json.dumps([{k: v for k, v in r.items() if k != "log"} for r in st["runs"]]))
    elif cmd == ["run", "view"]:
        run = next(r for r in st["runs"] if str(r["databaseId"]) == argv[2])
        sys.stdout.write(run["log"])
    elif cmd == ["workflow", "run"]:
        n = 1000 + len(st["runs"])
        st["runs"].append(
            {
                "databaseId": n,
                "status": "completed",
                "conclusion": "success",
                "headBranch": "main",
                "event": "workflow_dispatch",
                "createdAt": f"2026-11-24T12:{len(st['runs']):02d}:00Z",
                "headSha": "f" * 40,
                "log": os.environ.get("FAKE_GH_DISPATCH_LOG", ""),
            }
        )
    elif cmd == ["variable", "set"]:
        st["variables"][argv[2]] = argv[argv.index("--body") + 1]
    elif cmd == ["variable", "list"]:
        print(json.dumps([{"name": k, "value": v} for k, v in st["variables"].items()]))
    else:
        print(f"fake gh: unsupported {argv}", file=sys.stderr)
        return 2
    path.write_text(json.dumps(st, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
