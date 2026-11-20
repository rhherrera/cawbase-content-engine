"""Pure reconciliation rules (no I/O). Times are explicit numbers: the 60-minute threshold costs nothing."""

from __future__ import annotations

from dataclasses import replace

import pytest

from cce_inference_controller.decide import (
    ControlState,
    Demand,
    Observation,
    Policy,
    Readiness,
    apply_command,
    decide,
    dispatch_allowed,
    needs_probe,
)

P = Policy()
T0 = 1_000_000.0
READY = Readiness("ready", "canary_ok")
LOADING = Readiness("loading", "health_503")
DOWN = Readiness("unreachable", "inference_unreachable")


def ready_state(at: float = T0) -> ControlState:
    return ControlState(phase="ready", phase_since=at, ready_at=at)


def test_stopped_with_pending_work_starts_once_and_records_deadline():
    d = decide(ControlState(phase_since=T0), Demand(pending=1), Observation("stopped"), P, T0)
    assert d.state.phase == "starting" and d.actions == ("start_instance",)
    assert d.state.ready_deadline == T0 + P.readiness_timeout_s
    # next tick: the instance is pending; no second start
    d2 = decide(d.state, Demand(pending=3), Observation("pending"), P, T0 + 5)
    assert d2.actions == () and d2.state.phase == "starting"


def test_no_demand_no_start_and_paused_never_autostarts():
    assert decide(ControlState(), Demand(), Observation("stopped"), P, T0).actions == ()
    paused = ControlState(mode="paused")
    d = decide(paused, Demand(pending=5), Observation("stopped"), P, T0)
    assert d.actions == () and d.state == paused
    # paused and someone started it by hand: hands-off
    assert decide(paused, Demand(), Observation("running"), P, T0).state == paused


def test_readiness_is_required_running_instance_is_not_enough():
    s = ControlState(phase="starting", phase_since=T0, ready_deadline=T0 + P.readiness_timeout_s)
    d = decide(s, Demand(pending=1), Observation("running", LOADING), P, T0 + 60)
    assert d.state.phase == "starting" and not dispatch_allowed(d.state)
    d = decide(s, Demand(pending=1), Observation("running", READY), P, T0 + 90)
    assert d.state.phase == "ready" and d.state.ready_at == T0 + 90 and dispatch_allowed(d.state)
    assert ("ready", {"waited_s": 90.0, "code": "canary_ok"}) in d.events


def test_readiness_timeout_stops_retries_with_backoff_then_blocks():
    s = ControlState(phase="starting", phase_since=T0, ready_deadline=T0 + P.readiness_timeout_s)
    now = T0 + P.readiness_timeout_s
    attempts = []
    for attempt in range(1, P.max_start_attempts + 1):
        d = decide(s, Demand(pending=1), Observation("running", LOADING), P, now)
        assert d.state.phase == "stopping" and d.actions == ("stop_instance",)
        assert d.state.start_failures == attempt
        attempts.append(d.state.retry_not_before - now)
        s = decide(d.state, Demand(pending=1), Observation("stopped"), P, now + 20).state
        assert s.phase == "stopped"
        if attempt < P.max_start_attempts:
            # backoff respected
            assert decide(s, Demand(pending=1), Observation("stopped"), P, s.retry_not_before - 1).actions == ()
            now = s.retry_not_before
            d = decide(s, Demand(pending=1), Observation("stopped"), P, now)
            assert d.actions == ("start_instance",)
            s = d.state
            now = s.ready_deadline
    assert attempts == [300.0, 600.0, 1200.0]
    assert s.blocked and s.last_error.startswith("readiness_timeout")
    late = s.retry_not_before + 10_000
    assert decide(s, Demand(pending=1), Observation("stopped"), P, late).actions == ()
    # a manual start clears the block and starts again
    s = apply_command(s, "start", P, late).state
    assert not s.blocked and s.start_requested
    assert decide(s, Demand(pending=1), Observation("stopped"), P, late).actions == ("start_instance",)


@pytest.mark.parametrize("idle", [P.idle_timeout_s - 1, P.idle_timeout_s])
def test_idle_stop_exactly_after_sixty_continuous_minutes(idle):
    d = decide(ready_state(), Demand(), Observation("running", Readiness("ready", "health_ok")), P, T0 + idle)
    if idle < P.idle_timeout_s:
        assert d.state.phase == "ready" and d.actions == ()
    else:
        assert d.state.phase == "stopping" and d.actions == ("stop_instance",) and d.state.stop_reason == "idle"


def test_idle_clock_restarts_with_work_and_ignores_health_checks():
    s = ready_state()
    health = Observation("running", Readiness("ready", "health_ok"))
    # 1000 health checks over 59 minutes do not reset or extend anything
    for i in range(1000):
        s = decide(s, Demand(), health, P, T0 + i * 3.5).state
    assert s.phase == "ready"
    assert decide(s, Demand(), health, P, T0 + 3600).state.phase == "stopping"
    # pending or active work blocks the stop regardless of time
    assert decide(s, Demand(pending=1), health, P, T0 + 99_999).state.phase == "ready"
    assert decide(s, Demand(active=1), health, P, T0 + 99_999).state.phase == "ready"
    # the last finished job resets the 60-minute window
    done = Demand(last_activity_at=T0 + 3000)
    assert decide(s, done, health, P, T0 + 3000 + 3599).state.phase == "ready"
    assert decide(s, done, health, P, T0 + 3000 + 3600).state.phase == "stopping"


def test_health_probe_is_light_while_ready():
    assert needs_probe(ready_state(), "running") and not needs_probe(ready_state(), "stopping")
    assert not needs_probe(ControlState(), "running")


def test_unhealthy_server_while_ready_goes_back_to_waiting_readiness():
    s = ready_state()
    for _ in range(P.unhealthy_threshold - 1):
        s = decide(s, Demand(pending=1), Observation("running", DOWN), P, T0 + 10).state
        assert s.phase == "ready"
    d = decide(s, Demand(pending=1), Observation("running", DOWN), P, T0 + 20)
    assert d.state.phase == "starting" and not dispatch_allowed(d.state)
    assert d.state.ready_deadline == T0 + 20 + P.readiness_timeout_s


def test_manual_stop_drains_then_stops_and_keeps_pending():
    s = apply_command(ready_state(), "stop", P, T0).state
    assert s.mode == "paused" and s.phase == "draining" and not dispatch_allowed(s)
    d = decide(s, Demand(pending=4, active=1), Observation("running"), P, T0 + 10)
    assert d.state.phase == "draining" and d.actions == ()
    d = decide(d.state, Demand(pending=4, active=0), Observation("running"), P, T0 + 20)
    assert d.state.phase == "stopping" and d.actions == ("stop_instance",)
    d = decide(d.state, Demand(pending=4), Observation("stopped"), P, T0 + 40)
    assert d.state.phase == "stopped"
    # pending work stays, but auto-start is paused
    assert decide(d.state, Demand(pending=4), Observation("stopped"), P, T0 + 99).actions == ()


def test_drain_timeout_stops_anyway():
    s = apply_command(ready_state(), "stop", P, T0).state
    d = decide(s, Demand(active=2), Observation("running"), P, T0 + P.drain_timeout_s)
    assert d.state.phase == "stopping" and ("drain_timeout", {"active": 2}) in d.events


def test_manual_start_resumes_a_drain_and_starts_a_stopped_host():
    s = apply_command(ready_state(), "stop", P, T0).state
    s = apply_command(s, "start", P, T0 + 5).state
    assert s.phase == "ready" and s.mode == "auto" and s.ready_at == T0 + 5
    stopped = ControlState(mode="paused")
    s = apply_command(stopped, "start", P, T0).state
    d = decide(s, Demand(), Observation("stopped"), P, T0)
    assert d.actions == ("start_instance",) and ("start_requested", {"reason": "manual"}) in d.events


def test_stop_on_stopped_host_ensures_it_is_stopped():
    s = apply_command(ControlState(), "stop", P, T0).state
    assert s.phase == "stopping"
    assert decide(s, Demand(), Observation("running"), P, T0).actions == ("stop_instance",)
    assert decide(s, Demand(), Observation("stopped"), P, T0).state.phase == "stopped"


def test_arrival_while_stopping_restarts_after_the_stop_completes():
    s = replace(ready_state(), phase="stopping", stop_reason="idle")
    d = decide(s, Demand(pending=1), Observation("stopping"), P, T0)
    assert d.actions == () and d.state.phase == "stopping"
    s = decide(d.state, Demand(pending=1), Observation("stopped"), P, T0 + 20).state
    assert s.phase == "stopped"
    assert decide(s, Demand(pending=1), Observation("stopped"), P, T0 + 30).actions == ("start_instance",)


def test_external_changes_are_reconciled():
    # stopped externally (console, instance-side idle guard) while ready
    d = decide(ready_state(), Demand(), Observation("stopped"), P, T0)
    assert d.state.phase == "stopped" and d.state.stop_reason == "external"
    # started externally in auto mode: adopted, so it is gated and idle-stopped
    d = decide(ControlState(), Demand(), Observation("running"), P, T0)
    assert d.state.phase == "starting" and d.actions == ()
    # lost start: the instance is still stopped while starting -> re-issued
    s = ControlState(phase="starting", phase_since=T0, ready_deadline=T0 + 999)
    assert decide(s, Demand(pending=1), Observation("stopped"), P, T0 + 1).actions == ("start_instance",)
    # lost stop
    s = ControlState(phase="stopping", phase_since=T0)
    assert decide(s, Demand(), Observation("running"), P, T0 + 1).actions == ("stop_instance",)


def test_unknown_or_terminated_instance():
    s = ready_state()
    assert decide(s, Demand(), Observation(None), P, T0 + 99_999).state == s
    d = decide(s, Demand(pending=1), Observation("terminated"), P, T0)
    assert d.state.phase == "stopped" and d.state.blocked and d.state.last_error == "instance_terminated"


def test_state_round_trips_and_policy_validates():
    s = ControlState(phase="draining", drain_deadline=12.5, last_error="x")
    assert ControlState.from_json(s.to_json()) == s
    with pytest.raises(ValueError):
        Policy(idle_timeout_s=0)
    with pytest.raises(ValueError):
        Policy(max_start_attempts=0)
