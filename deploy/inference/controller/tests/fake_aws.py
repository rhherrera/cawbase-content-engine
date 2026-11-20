#!/usr/bin/env python3
"""A fake `aws` CLI for the GPU-validation tests: no AWS call is ever made. The "instance" is the simulated host of
`sim_host.sh` (a fake llama-server started through the real run-server.sh); SSM Run Command executes the command
locally; a port-forwarding session just sleeps. Every call is appended to $FAKE_AWS_DIR/calls.jsonl.

Environment: FAKE_AWS_DIR, FAKE_AWS_SIM_DIR, FAKE_AWS_SIM_HOST; FAKE_AWS_STOP_FAIL=1 makes stop-instances fail.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path


def opts(argv: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--"):
            nxt = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("--") else ""
            out[argv[i][2:]] = nxt
            i += 2 if nxt else 1
        else:
            i += 1
    return out


def sim(cmd: str) -> str:
    r = subprocess.run(
        [os.environ["FAKE_AWS_SIM_HOST"], os.environ["FAKE_AWS_SIM_DIR"], cmd],
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


def main(argv: list[str]) -> int:
    state_dir = Path(os.environ["FAKE_AWS_DIR"])
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "calls.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": time.time(), "argv": argv}) + "\n")
    service, op, o = argv[0], argv[1], opts(argv[2:])
    if (service, op) == ("ec2", "describe-instances"):
        print(sim("describe"))
        return 0
    if (service, op) == ("ec2", "start-instances"):
        sim("start")
        print(json.dumps({"StartingInstances": [{"InstanceId": o["instance-ids"]}]}))
        return 0
    if (service, op) == ("ec2", "stop-instances"):
        if os.environ.get("FAKE_AWS_STOP_FAIL") == "1":
            print("An error occurred (UnauthorizedOperation) when calling StopInstances (simulated)", file=sys.stderr)
            return 254
        sim("stop")
        print(json.dumps({"StoppingInstances": [{"InstanceId": o["instance-ids"]}]}))
        return 0
    if (service, op) == ("ssm", "send-command"):
        command = json.loads(o["parameters"])["commands"][0]
        r = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)
        cid = uuid.uuid4().hex
        (state_dir / f"cmd-{cid}.json").write_text(
            json.dumps({"rc": r.returncode, "out": r.stdout, "err": r.stderr, "command": command})
        )
        print(cid)
        return 0
    if (service, op) == ("ssm", "get-command-invocation"):
        rec = json.loads((state_dir / f"cmd-{o['command-id']}.json").read_text())
        field = o["query"]
        if field == "Status":
            print("Success" if rec["rc"] == 0 else "Failed")
        elif field == "StandardOutputContent":
            sys.stdout.write(rec["out"])
        else:
            sys.stdout.write(rec["err"])
        return 0
    if (service, op) == ("ssm", "start-session"):
        os.execvp("sleep", ["sleep", "3600"])  # noqa: S606 - the port-forwarding session, killed with its group
    print(f"fake aws: unsupported {service} {op}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
