"""Regressions for the A09-1 review (finding 1): dispatch is open only on verified readiness.

- start → stop → start while the model loads must not reach `ready` without a successful canary;
- the first failed probe while `ready` closes dispatch; only a verified (full) probe reopens it; the threshold of
  failed probes still decides when to go back to waiting readiness;
- a canary that finishes after the readiness deadline does not open dispatch for that expired attempt;
- a probe taken for one start attempt is not applied to a newer one;
- active work keeps its lease and completes while dispatch is closed.
"""

from __future__ import annotations

from dataclasses import dataclass

from cce_inference_controller.controller import Controller, status
from cce_inference_controller.decide import Readiness, dispatch_allowed
from cce_inference_controller.executor import JobQueue
from conftest import JOB, Rig

READY = Readiness("ready", "canary_ok", verified=True)
LOADING = Readiness("loading", "health_503")
DOWN = Readiness("unreachable", "inference_unreachable")


def bring_up(rig: Rig, ctl: Controller) -> None:
    ctl.tick()
    rig.clock.advance(rig.backend.boot_s)
    rig.prober.state = READY
    ctl.tick()
    assert rig.phase() == "ready"


def gate_open(rig: Rig) -> bool:
    return dispatch_allowed(rig.store.snapshot().state)


def test_start_stop_start_during_load_requires_a_canary(rig: Rig):
    ctl = rig.controller()
    rig.queue.submit(JOB)
    ctl.tick()  # starting
    rig.clock.advance(rig.backend.boot_s)
    rig.prober.state = LOADING
    ctl.tick()
    assert rig.phase() == "starting"
    ctl.command("stop")
    assert rig.phase() == "draining"
    ctl.command("start")  # resume before the next tick: the model was never verified
    assert rig.phase() == "starting" and not gate_open(rig)
    assert rig.queue.claim("w1") is None
    full_before = rig.prober.full_calls
    ctl.tick()  # still loading: no readiness
    assert rig.phase() == "starting" and rig.queue.claim("w1") is None
    assert rig.prober.full_calls == full_before + 1
    rig.prober.state = READY
    ctl.tick()
    assert rig.phase() == "ready" and rig.queue.claim("w1") is not None


def test_resume_during_drain_with_active_work_revalidates_before_dispatch(rig: Rig):
    ctl = rig.controller()
    rig.queue.submit(JOB)
    bring_up(rig, ctl)
    active = rig.queue.claim("w1")
    assert active is not None
    ctl.command("stop")
    assert rig.phase() == "draining"
    second = rig.queue.submit(JOB)
    ctl.command("start")
    assert rig.phase() == "starting" and not gate_open(rig)
    rig.prober.state = LOADING  # e.g. the only slot is busy with the active job
    ctl.tick()
    assert rig.phase() == "starting" and rig.backend.stop_calls == 0  # the host keeps serving the active job
    assert rig.queue.claim("w2") is None
    assert rig.queue.complete(active, {"output": {}})  # the active job finishes under its lease
    rig.prober.state = READY
    ctl.tick()
    assert rig.phase() == "ready"
    job = rig.queue.claim("w2")
    assert job is not None and job.job_id == second


def test_first_failed_probe_closes_dispatch_and_only_a_verified_probe_reopens(rig: Rig):
    ctl = rig.controller()
    rig.queue.submit(JOB)
    rig.queue.submit(JOB)
    bring_up(rig, ctl)
    active = rig.queue.claim("w1")
    assert active is not None
    assert rig.queue.complete(active, {"output": {}})
    rig.prober.state = DOWN
    ctl.tick()
    assert rig.phase() == "ready" and not gate_open(rig)
    assert rig.queue.claim("w1") is None
    st = status(rig.store, rig.backend, None, JobQueue.demand, rig.policy, rig.clock)
    assert st.dispatch_allowed is False and st.failed_probes == 1
    # /health answers again: a liveness probe alone does not reopen dispatch; the next probe is a full one
    rig.prober.state = READY
    full_before = rig.prober.full_calls
    rig.prober.liveness_only = True
    ctl.tick()
    assert rig.prober.full_calls == full_before + 1  # the controller asked for verified readiness
    assert not gate_open(rig) and rig.queue.claim("w1") is None
    rig.prober.liveness_only = False
    ctl.tick()
    assert gate_open(rig) and rig.queue.claim("w1") is not None
    kinds = [e["kind"] for e in rig.store.events(50)]
    assert "dispatch_closed" in kinds and "dispatch_reopened" in kinds


def test_threshold_still_decides_the_restart_and_active_work_survives(rig: Rig):
    ctl = rig.controller()
    rig.queue.submit(JOB)
    second = rig.queue.submit(JOB)
    bring_up(rig, ctl)
    active = rig.queue.claim("w1")
    assert active is not None
    rig.prober.state = DOWN
    for _ in range(rig.policy.unhealthy_threshold - 1):
        rig.clock.advance(10)
        ctl.tick()
        assert rig.phase() == "ready" and not gate_open(rig)
    rig.clock.advance(10)
    ctl.tick()
    assert rig.phase() == "starting"  # threshold reached: back to waiting readiness, host not stopped
    assert rig.backend.stop_calls == 0
    assert rig.queue.complete(active, {"output": {}})  # the active job was never cut off
    assert rig.queue.claim("w2") is None
    rig.prober.state = READY
    ctl.tick()
    job = rig.queue.claim("w2")
    assert rig.phase() == "ready" and job is not None and job.job_id == second


def test_idle_stop_is_not_taken_while_dispatch_is_closed_with_active_work(rig: Rig):
    ctl = rig.controller()
    rig.queue.submit(JOB)
    bring_up(rig, ctl)
    active = rig.queue.claim("w1")
    assert active is not None
    rig.prober.state = DOWN
    ctl.tick()
    rig.clock.advance(rig.policy.idle_timeout_s + 1)
    rig.prober.state = READY
    ctl.tick()  # verified again; work is active (lease still valid): no idle stop
    assert rig.phase() == "ready" and rig.backend.stop_calls == 0


@dataclass
class SlowCanary:
    """A full probe that takes `seconds` on the controlled clock before answering `result`."""

    rig: Rig
    seconds: float
    result: Readiness
    calls: int = 0
    full_calls: int = 0
    liveness_only: bool = False

    def probe(self, *, full: bool) -> Readiness:
        self.calls += 1
        self.full_calls += int(full)
        if full:
            self.rig.clock.advance(self.seconds)
        return self.result


def test_canary_that_finishes_after_the_deadline_does_not_open_dispatch(rig: Rig):
    rig.queue.submit(JOB)
    ctl = rig.controller()
    ctl.tick()  # starting, deadline = now + readiness_timeout
    deadline = rig.store.snapshot().state.ready_deadline
    assert deadline is not None
    rig.clock.advance(rig.backend.boot_s)
    remaining = deadline - rig.clock.now()
    ctl.prober = SlowCanary(rig, remaining + 5, READY)  # the canary succeeds 5 s after the deadline
    ctl.tick()
    snap = rig.store.snapshot().state
    assert snap.phase == "stopping" and snap.start_failures == 1
    assert snap.last_error is not None and snap.last_error.startswith("readiness_timeout")
    assert rig.queue.claim("w1") is None
    assert not any(e["kind"] == "ready" for e in rig.store.events(50))


@dataclass
class ProbeDuringCommands:
    """While the (slow) probe of attempt N runs, an operator issues stop + start: the result is stale."""

    rig: Rig
    ctl: Controller
    calls: int = 0
    full_calls: int = 0
    liveness_only: bool = False

    def probe(self, *, full: bool) -> Readiness:
        self.calls += 1
        self.full_calls += int(full)
        if self.calls == 1:
            self.ctl.command("stop")
            self.ctl.command("start")
        return READY


def test_probe_of_a_previous_attempt_is_discarded(rig: Rig):
    rig.queue.submit(JOB)
    ctl = rig.controller()
    ctl.tick()
    rig.clock.advance(rig.backend.boot_s)
    prober = ProbeDuringCommands(rig, ctl)
    ctl.prober = prober
    ctl.tick()
    assert rig.phase() == "starting" and rig.queue.claim("w1") is None
    assert any(e["kind"] == "stale_probe_discarded" for e in rig.store.events(50))
    ctl.tick()  # a fresh probe for the current attempt
    assert rig.phase() == "ready" and rig.queue.claim("w1") is not None
