"""Pure reconciliation rules of the controller (no I/O, no clock reads).

The durable `ControlState` is the desired state; each tick observes the instance state (EC2 or equivalent), the
model readiness and the durable demand, and `decide` returns the next state plus idempotent actions
(`start_instance` / `stop_instance`). Manual commands go through `apply_command`. The same inputs always give the
same decision, which is what the tests exercise with a controlled clock.

Phases:

    stopped ──demand/start──► starting ──real readiness──► ready ──60 min idle──► stopping ──► stopped
                                 │  ▲                        │  └─ manual stop ─► draining ─┘
                                 │  └── unhealthy (N probes)─┘
                                 └── readiness deadline ─► stopping (retry with backoff; blocked after N attempts)

Dispatch of inference work is allowed only in `ready`.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, replace
from typing import Any, Literal

Phase = Literal["stopped", "starting", "ready", "draining", "stopping"]
Mode = Literal["auto", "paused"]
Action = Literal["start_instance", "stop_instance"]
Command = Literal["start", "stop"]
ReadinessState = Literal["ready", "loading", "unreachable", "unhealthy"]

PHASES: tuple[str, ...] = ("stopped", "starting", "ready", "draining", "stopping")
RUNNING_LIKE = ("pending", "running")
GONE = ("shutting-down", "terminated")


@dataclass(frozen=True)
class Policy:
    """Timing policy. Defaults are the A09-1 values; the idle threshold (60 min) is fixed by the user."""

    idle_timeout_s: float = 3600.0
    readiness_timeout_s: float = 1200.0
    drain_timeout_s: float = 1800.0
    max_start_attempts: int = 3
    start_retry_backoff_s: float = 300.0
    start_retry_backoff_max_s: float = 3600.0
    unhealthy_threshold: int = 3

    def __post_init__(self) -> None:
        positive = ("idle_timeout_s", "readiness_timeout_s", "drain_timeout_s", "start_retry_backoff_s")
        for name in positive:
            if not getattr(self, name) > 0:
                raise ValueError(f"{name} must be > 0")
        if self.max_start_attempts < 1 or self.unhealthy_threshold < 1:
            raise ValueError("max_start_attempts and unhealthy_threshold must be >= 1")
        if self.start_retry_backoff_max_s < self.start_retry_backoff_s:
            raise ValueError("start_retry_backoff_max_s must be >= start_retry_backoff_s")


@dataclass(frozen=True)
class Demand:
    """Durable inference work seen by the controller. `pending` includes work waiting for a retry and work whose
    worker lease expired; `active` is work running under a live lease; `last_activity_at` is the latest moment any
    inference work finished (health checks never change it)."""

    pending: int = 0
    active: int = 0
    last_activity_at: float | None = None


@dataclass(frozen=True)
class Readiness:
    state: ReadinessState
    code: str


@dataclass(frozen=True)
class Observation:
    instance: str | None  # EC2 state name, or None when it could not be observed this tick
    readiness: Readiness | None = None  # None when not probed


@dataclass(frozen=True)
class ControlState:
    mode: Mode = "auto"
    phase: Phase = "stopped"
    phase_since: float = 0.0
    start_requested: bool = False
    ready_deadline: float | None = None
    ready_at: float | None = None
    start_failures: int = 0
    retry_not_before: float | None = None
    blocked: bool = False
    stop_reason: str | None = None
    drain_deadline: float | None = None
    unhealthy_count: int = 0
    last_error: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> ControlState:
        data = json.loads(raw)
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


Event = tuple[str, dict[str, Any]]


@dataclass(frozen=True)
class Decision:
    state: ControlState
    actions: tuple[Action, ...] = ()
    events: tuple[Event, ...] = ()


@dataclass
class _Builder:
    state: ControlState
    now: float
    actions: list[Action] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)

    def go(self, phase: Phase, **changes: Any) -> None:
        if phase != self.state.phase:
            changes["phase_since"] = self.now
        self.state = replace(self.state, phase=phase, **changes)

    def set(self, **changes: Any) -> None:
        self.state = replace(self.state, **changes)

    def act(self, action: Action) -> None:
        self.actions.append(action)

    def event(self, kind: str, **data: Any) -> None:
        self.events.append((kind, data))

    def done(self) -> Decision:
        return Decision(self.state, tuple(self.actions), tuple(self.events))


def needs_probe(state: ControlState, instance: str | None) -> bool:
    """Readiness is probed only while starting or ready on a running instance. A probe never counts as activity."""
    return state.phase in ("starting", "ready") and instance == "running"


def dispatch_allowed(state: ControlState) -> bool:
    return state.phase == "ready"


def idle_reference(state: ControlState, demand: Demand) -> float:
    base = state.ready_at if state.ready_at is not None else state.phase_since
    return max(base, demand.last_activity_at or base)


def _backoff(policy: Policy, failures: int) -> float:
    return float(min(policy.start_retry_backoff_s * 2 ** max(failures - 1, 0), policy.start_retry_backoff_max_s))


def _begin_start(b: _Builder, policy: Policy, reason: str) -> None:
    b.go("starting", ready_deadline=b.now + policy.readiness_timeout_s, stop_reason=None, unhealthy_count=0)
    b.act("start_instance")
    b.event("start_requested", reason=reason)


def decide(state: ControlState, demand: Demand, obs: Observation, policy: Policy, now: float) -> Decision:
    b = _Builder(state, now)
    inst = obs.instance
    if inst is None:  # cloud state unknown this tick: hold, decide nothing
        return b.done()
    if inst in GONE:
        if state.phase != "stopped" or not state.blocked:
            b.go("stopped", blocked=True, last_error="instance_terminated", ready_deadline=None, drain_deadline=None)
            b.event("instance_gone", instance=inst)
        return b.done()

    if state.phase == "stopped":
        _stopped(b, demand, inst, policy)
    elif state.phase == "starting":
        _starting(b, inst, obs.readiness, policy)
    elif state.phase == "ready":
        _ready(b, demand, inst, obs.readiness, policy)
    elif state.phase == "draining":
        _draining(b, demand, inst)
    elif state.phase == "stopping":
        _stopping(b, inst)
    return b.done()


def _stopped(b: _Builder, demand: Demand, inst: str, policy: Policy) -> None:
    s = b.state
    if s.mode != "auto":
        return  # paused: hands-off (an operator may have started the host for maintenance)
    if inst in RUNNING_LIKE:
        # Running outside our control (or a start we lost track of): adopt it so it is gated and idle-stopped.
        b.go("starting", ready_deadline=b.now + policy.readiness_timeout_s, stop_reason=None, unhealthy_count=0)
        b.event("adopted_running_instance", instance=inst)
        return
    if inst != "stopped":
        return  # stopping: wait
    if not (demand.pending > 0 or s.start_requested):
        return
    if s.blocked:
        return  # needs a manual start after repeated readiness failures
    if s.retry_not_before is not None and b.now < s.retry_not_before:
        return
    _begin_start(b, policy, "manual" if s.start_requested else "demand")


def _starting(b: _Builder, inst: str, readiness: Readiness | None, policy: Policy) -> None:
    s = b.state
    if inst == "running" and readiness is not None and readiness.state == "ready":
        waited = b.now - s.phase_since
        b.go(
            "ready",
            ready_at=b.now,
            ready_deadline=None,
            start_failures=0,
            retry_not_before=None,
            start_requested=False,
            unhealthy_count=0,
            last_error=None,
        )
        b.event("ready", waited_s=round(waited, 3), code=readiness.code)
        return
    if s.ready_deadline is not None and b.now >= s.ready_deadline:
        failures = s.start_failures + 1
        blocked = failures >= policy.max_start_attempts
        code = readiness.code if readiness is not None else f"instance_{inst}"
        b.go(
            "stopping",
            stop_reason="readiness_failed",
            ready_deadline=None,
            start_failures=failures,
            retry_not_before=b.now + _backoff(policy, failures),
            blocked=blocked,
            last_error=f"readiness_timeout:{code}",
        )
        b.act("stop_instance")
        b.event("readiness_timeout", attempt=failures, blocked=blocked, code=code)
        return
    if inst == "stopped":
        b.act("start_instance")  # idempotent re-issue (lost call, controller restart, external stop)
        b.event("start_reissued")


def _ready(b: _Builder, demand: Demand, inst: str, readiness: Readiness | None, policy: Policy) -> None:
    s = b.state
    if inst != "running":
        if inst == "pending":
            b.go("starting", ready_deadline=b.now + policy.readiness_timeout_s, unhealthy_count=0)
        else:
            b.go("stopping" if inst == "stopping" else "stopped", stop_reason="external", ready_deadline=None)
        b.event("instance_left_running", instance=inst)
        return
    if readiness is not None and readiness.state != "ready":
        count = s.unhealthy_count + 1
        if count >= policy.unhealthy_threshold:
            b.go("starting", ready_deadline=b.now + policy.readiness_timeout_s, unhealthy_count=0)
            b.event("unhealthy", code=readiness.code, probes=count)
        else:
            b.set(unhealthy_count=count)
        return
    if readiness is not None and s.unhealthy_count:
        b.set(unhealthy_count=0)
    if s.mode != "auto" or demand.pending > 0 or demand.active > 0:
        return
    idle_for = b.now - idle_reference(s, demand)
    if idle_for >= policy.idle_timeout_s:
        b.go("stopping", stop_reason="idle", ready_deadline=None)
        b.act("stop_instance")
        b.event("idle_stop", idle_s=round(idle_for, 3))


def _draining(b: _Builder, demand: Demand, inst: str) -> None:
    s = b.state
    if inst == "stopped":
        b.go("stopped", drain_deadline=None)
        b.event("stopped", reason=s.stop_reason)
        return
    if demand.active == 0:
        b.go("stopping", drain_deadline=None)
        b.act("stop_instance")
        b.event("drain_completed")
    elif s.drain_deadline is not None and b.now >= s.drain_deadline:
        b.go("stopping", drain_deadline=None)
        b.act("stop_instance")
        b.event("drain_timeout", active=demand.active)


def _stopping(b: _Builder, inst: str) -> None:
    s = b.state
    if inst == "stopped":
        b.go("stopped", drain_deadline=None)
        b.event("stopped", reason=s.stop_reason, stopping_s=round(b.now - s.phase_since, 3))
    elif inst in RUNNING_LIKE:
        b.act("stop_instance")  # idempotent re-issue


def apply_command(state: ControlState, command: Command, policy: Policy, now: float) -> Decision:
    """Manual control. `start` resumes auto-start and asks for capacity now; `stop` pauses auto-start, drains active
    work and stops the host. Pending work stays durable in both cases. Actions are taken by the next tick."""
    b = _Builder(state, now)
    if command == "start":
        b.set(mode="auto", blocked=False, start_failures=0, retry_not_before=None, last_error=None)
        if state.phase == "draining":
            b.go("ready", ready_at=now, drain_deadline=None, stop_reason=None, start_requested=False)
        elif state.phase in ("ready", "starting"):
            b.set(start_requested=False)
        else:
            b.set(start_requested=True)
        b.event("manual_start", phase=state.phase)
    elif command == "stop":
        b.set(mode="paused", start_requested=False)
        if state.phase in ("ready", "starting"):
            b.go("draining", drain_deadline=now + policy.drain_timeout_s, stop_reason="manual", ready_deadline=None)
        elif state.phase == "stopped":
            b.go("stopping", stop_reason="manual")
        b.event("manual_stop", phase=state.phase)
    else:  # pragma: no cover - guarded by the Literal type and the CLI
        raise ValueError(f"unknown command {command!r}")
    return b.done()
