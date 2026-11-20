"""Instance backends: how the controller observes, starts and stops the GPU host.

- `Ec2Backend` (AWS): one EC2 instance by id; needs `ec2:DescribeInstances`, `ec2:StartInstances`,
  `ec2:StopInstances` on that instance (policy in deploy/inference/aws). boto3 is imported lazily.
- `CommandBackend` (on-premise or simulation): three shell commands; `describe` prints a state name. On-premise it
  maps to the systemd unit of the server (`systemctl is-active`), with no AWS service or credential.
- `FakeBackend` (tests): in-memory EC2-like state machine on a controlled clock, with fault injection.

States use the EC2 vocabulary: pending, running, stopping, stopped, shutting-down, terminated.
`start`/`stop` must be idempotent; the controller re-issues them while reconciling.
"""

from __future__ import annotations

import shlex
import subprocess
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from .clock import Clock

EC2_STATES = ("pending", "running", "stopping", "stopped", "shutting-down", "terminated")


class BackendError(Exception):
    """The backend could not observe or act this time (transient)."""


class InstanceBackend(Protocol):
    def describe(self) -> str: ...

    def start(self) -> None: ...

    def stop(self) -> None: ...


class Ec2Backend:
    def __init__(self, instance_id: str, *, region: str, profile: str | None = None, client: Any = None) -> None:
        if not instance_id.startswith("i-"):
            raise ValueError("instance_id must be an EC2 instance id")
        self.instance_id = instance_id
        if client is None:
            import boto3  # lazy: only the AWS backend needs it

            session = boto3.Session(profile_name=profile or None, region_name=region)
            client = session.client("ec2")
        self._ec2 = client

    def describe(self) -> str:
        try:
            res = self._ec2.describe_instances(InstanceIds=[self.instance_id])
            state = res["Reservations"][0]["Instances"][0]["State"]["Name"]
        except Exception as exc:  # botocore errors, throttling, network
            raise BackendError(type(exc).__name__) from exc
        if state not in EC2_STATES:
            raise BackendError("unknown_state")
        return str(state)

    def start(self) -> None:
        try:
            self._ec2.start_instances(InstanceIds=[self.instance_id])
        except Exception as exc:
            raise BackendError(type(exc).__name__) from exc

    def stop(self) -> None:
        try:
            self._ec2.stop_instances(InstanceIds=[self.instance_id])
        except Exception as exc:
            raise BackendError(type(exc).__name__) from exc


class CommandBackend:
    """`describe` prints one of the EC2 state names (or `active`/`activating` → running, `inactive`/`failed` →
    stopped, `deactivating` → stopping, as printed by `systemctl is-active`)."""

    SYSTEMD = {
        "active": "running",
        "activating": "pending",
        "reloading": "running",
        "deactivating": "stopping",
        "inactive": "stopped",
        "failed": "stopped",
    }

    def __init__(self, describe: str, start: str, stop: str, *, timeout_s: float = 60.0) -> None:
        self._cmds = {"describe": describe, "start": start, "stop": stop}
        self._timeout_s = timeout_s

    def _run(self, name: str) -> str:
        try:
            proc = subprocess.run(
                shlex.split(self._cmds[name]), capture_output=True, text=True, timeout=self._timeout_s, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackendError(f"{name}_failed") from exc
        if name != "describe" and proc.returncode != 0:
            raise BackendError(f"{name}_exit_{proc.returncode}")
        return proc.stdout.strip()

    def describe(self) -> str:
        out = self._run("describe").splitlines()
        word = out[-1].strip() if out else ""
        state = self.SYSTEMD.get(word, word)
        if state not in EC2_STATES:
            raise BackendError("unknown_state")
        return state

    def start(self) -> None:
        self._run("start")

    def stop(self) -> None:
        self._run("stop")


@dataclass
class FakeBackend:
    """EC2-like fake on a controlled clock. `boot_s` / `stop_s` model the state transitions; `on_running` and
    `on_stopped` let tests bring a fake model server up and down; the `fail_*` flags inject API failures."""

    clock: Clock
    boot_s: float = 30.0
    stop_s: float = 20.0
    state: str = "stopped"
    on_running: Callable[[], None] | None = None
    on_stopped: Callable[[], None] | None = None
    fail_describe: bool = False
    fail_start: bool = False
    fail_stop: bool = False
    start_calls: int = 0
    stop_calls: int = 0
    effective_starts: int = 0
    _since: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _advance(self) -> None:
        now = self.clock.now()
        if self.state == "pending" and now - self._since >= self.boot_s:
            self.state, self._since = "running", now
            if self.on_running:
                self.on_running()
        elif self.state == "stopping" and now - self._since >= self.stop_s:
            self.state, self._since = "stopped", now
            if self.on_stopped:
                self.on_stopped()

    def describe(self) -> str:
        with self._lock:
            if self.fail_describe:
                raise BackendError("describe_failed")
            self._advance()
            return self.state

    def start(self) -> None:
        with self._lock:
            self.start_calls += 1
            if self.fail_start:
                raise BackendError("start_failed")
            self._advance()
            if self.state == "stopped":
                self.state, self._since = "pending", self.clock.now()
                self.effective_starts += 1

    def stop(self) -> None:
        with self._lock:
            self.stop_calls += 1
            if self.fail_stop:
                raise BackendError("stop_failed")
            self._advance()
            if self.state in ("pending", "running"):
                self.state, self._since = "stopping", self.clock.now()

    def external(self, state: str) -> None:
        """Simulate a change made outside the controller (console, instance-side guard, crash)."""
        with self._lock:
            self.state, self._since = state, self.clock.now()
