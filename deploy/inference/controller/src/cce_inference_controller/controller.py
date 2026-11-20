"""The reconciler. One leader (lease in the store) runs `tick()` periodically:

1. take/renew the lease (a second controller waits; a dead leader is replaced after the lease TTL);
2. observe the instance (`describe`) and, while starting or ready, the readiness (outside any transaction);
3. in one serialized transaction: re-check the lease, read the state and the durable demand, `decide`, persist the
   new state and events;
4. after commit, run the idempotent actions (`start_instance` / `stop_instance`).

Recovery after a crash or restart needs nothing special: the phase is durable and every tick reconciles the real
instance state toward it (re-issuing a lost start or stop). Manual commands change the durable state; the next tick
acts on them.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from .backends import BackendError, InstanceBackend
from .clock import Clock
from .decide import (
    Command,
    ControlState,
    Decision,
    Demand,
    Observation,
    Policy,
    Readiness,
    apply_command,
    decide,
    dispatch_allowed,
    idle_reference,
    needs_probe,
)
from .store import Store


class Prober(Protocol):
    def probe(self, *, full: bool) -> Readiness: ...


DemandFn = Callable[[sqlite3.Connection, float], Demand]


@dataclass
class TickReport:
    leader: bool
    instance: str | None = None
    readiness: Readiness | None = None
    phase: str | None = None
    actions: tuple[str, ...] = ()
    action_errors: tuple[str, ...] = ()
    events: tuple[str, ...] = field(default_factory=tuple)


class Controller:
    def __init__(
        self,
        store: Store,
        backend: InstanceBackend,
        prober: Prober,
        demand: DemandFn,
        policy: Policy,
        clock: Clock,
        *,
        owner: str,
        lease_ttl_s: float = 60.0,
    ) -> None:
        self.store = store
        self.backend = backend
        self.prober = prober
        self.demand = demand
        self.policy = policy
        self.clock = clock
        self.owner = owner
        self.lease_ttl_s = lease_ttl_s

    def tick(self) -> TickReport:
        now = self.clock.now()
        with self.store.tx() as conn:
            if not self.store.take_lease(conn, self.owner, now, self.lease_ttl_s):
                return TickReport(leader=False)
            before = self.store.read(conn).state
        try:
            instance: str | None = self.backend.describe()
        except BackendError:
            instance = None
        readiness = None
        if needs_probe(before, instance):
            readiness = self.prober.probe(full=before.phase == "starting")

        now = self.clock.now()
        with self.store.tx() as conn:
            if not self.store.take_lease(conn, self.owner, now, self.lease_ttl_s):
                return TickReport(leader=False)  # another controller took over while we were observing
            row = self.store.read(conn)
            demand = self.demand(conn, now)
            decision = decide(row.state, demand, Observation(instance, readiness), self.policy, now)
            self._persist(conn, row.state, row.generation, decision, now)

        errors = self._act(decision)
        return TickReport(
            leader=True,
            instance=instance,
            readiness=readiness,
            phase=decision.state.phase,
            actions=decision.actions,
            action_errors=errors,
            events=tuple(k for k, _ in decision.events),
        )

    def command(self, command: Command) -> ControlState:
        """Apply a manual command durably (any process may call it; the leader acts on it)."""
        now = self.clock.now()
        with self.store.tx() as conn:
            row = self.store.read(conn)
            decision = apply_command(row.state, command, self.policy, now)
            self._persist(conn, row.state, row.generation, decision, now)
            return decision.state

    def _persist(self, conn: sqlite3.Connection, old: ControlState, gen: int, d: Decision, now: float) -> None:
        if d.state != old:
            gen = self.store.write(conn, d.state, gen)
        events = d.events + tuple(("action", {"action": a}) for a in d.actions)
        self.store.append_events(conn, now, gen, events)

    def _act(self, decision: Decision) -> tuple[str, ...]:
        errors: list[str] = []
        for action in decision.actions:
            try:
                if action == "start_instance":
                    self.backend.start()
                else:
                    self.backend.stop()
            except BackendError as exc:
                errors.append(f"{action}:{exc}")
        if errors:
            with self.store.tx() as conn:
                gen = self.store.read(conn).generation
                self.store.append_events(
                    conn, self.clock.now(), gen, tuple(("action_failed", {"error": e}) for e in errors)
                )
        return tuple(errors)

    def run(self, stop: threading.Event, *, interval_s: float) -> None:
        while not stop.is_set():
            self.tick()
            stop.wait(interval_s)


@dataclass(frozen=True)
class Status:
    instance: str | None
    instance_error: str | None
    readiness: Readiness | None
    autostart_paused: bool
    phase: str
    dispatch_allowed: bool
    blocked: bool
    start_failures: int
    pending: int
    active: int
    idle_for_s: float | None
    idle_stop_in_s: float | None
    last_error: str | None
    leader: str | None
    leader_lease_valid: bool


def status(
    store: Store, backend: InstanceBackend, prober: Prober | None, demand: DemandFn, policy: Policy, clock: Clock
) -> Status:
    """Read-only status. Never changes state; the readiness probe here is health-only (no decoding)."""
    now = clock.now()
    conn = store.connect()
    try:
        row = store.read(conn)
        dem = demand(conn, now)
    finally:
        conn.close()
    instance, err = None, None
    try:
        instance = backend.describe()
    except BackendError as exc:
        err = str(exc)
    readiness = prober.probe(full=False) if prober is not None and instance == "running" else None
    s = row.state
    idle_for = idle_in = None
    if s.phase == "ready" and dem.pending == 0 and dem.active == 0:
        idle_for = now - idle_reference(s, dem)
        idle_in = max(policy.idle_timeout_s - idle_for, 0.0) if s.mode == "auto" else None
    return Status(
        instance=instance,
        instance_error=err,
        readiness=readiness,
        autostart_paused=s.mode == "paused",
        phase=s.phase,
        dispatch_allowed=dispatch_allowed(s),
        blocked=s.blocked,
        start_failures=s.start_failures,
        pending=dem.pending,
        active=dem.active,
        idle_for_s=idle_for,
        idle_stop_in_s=idle_in,
        last_error=s.last_error,
        leader=row.lease_owner,
        leader_lease_valid=row.lease_expires_at is not None and row.lease_expires_at > now,
    )
