"""Controller + durable store + fake instance backend, on a controlled clock."""

from __future__ import annotations

import random
import threading

from cce_inference_controller.controller import status
from cce_inference_controller.decide import Readiness
from cce_inference_controller.executor import JobQueue
from conftest import JOB, Rig

READY = Readiness("ready", "canary_ok", verified=True)


def bring_up(rig: Rig, ctl) -> None:
    ctl.tick()  # start
    rig.clock.advance(rig.backend.boot_s)
    rig.prober.state = READY
    ctl.tick()
    assert rig.phase() == "ready"


def test_submit_is_durable_and_never_touches_the_cloud(rig: Rig):
    jid = rig.queue.submit(JOB)
    assert rig.backend.start_calls == 0 and rig.queue.get(jid)["status"] == "pending"
    assert rig.queue.claim("w1") is None  # not ready: nothing is dispatched


def test_concurrent_requests_and_controllers_coordinate_one_start(rig: Rig):
    controllers = [rig.controller(owner=f"ctl-{i}") for i in range(4)]
    barrier = threading.Barrier(24)
    errors: list[BaseException] = []

    def submitter() -> None:
        barrier.wait()
        rig.queue.submit(JOB)

    def ticker(ctl) -> None:
        barrier.wait()
        for _ in range(5):
            try:
                ctl.tick()
            except BaseException as exc:  # pragma: no cover - reported below
                errors.append(exc)

    threads = [threading.Thread(target=submitter) for _ in range(20)]
    threads += [threading.Thread(target=ticker, args=(c,)) for c in controllers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    for c in controllers:  # a final pass in case the submissions landed after the last ticks
        c.tick()
    assert rig.queue.counts() == {"pending": 20}
    assert rig.backend.effective_starts == 1 and rig.backend.start_calls == 1
    assert rig.phase() == "starting"
    leaders = {e["kind"] for e in rig.store.events(500)}
    assert "start_requested" in leaders
    assert sum(1 for e in rig.store.events(500) if e["kind"] == "start_requested") == 1


def test_full_cycle_with_sixty_minute_idle_stop(rig: Rig):
    ctl = rig.controller()
    jid = rig.queue.submit(JOB)
    bring_up(rig, ctl)
    assert rig.prober.full_calls >= 1
    job = rig.queue.claim("w1")
    assert job is not None and rig.queue.complete(job, {"output": {}})
    full_before = rig.prober.full_calls
    # 59 min 59 s of health checks only: still up
    for _ in range(359):
        rig.clock.advance(10)
        ctl.tick()
    rig.clock.advance(9)
    ctl.tick()
    assert rig.phase() == "ready" and rig.prober.full_calls == full_before  # liveness is health-only
    rig.clock.advance(1)
    ctl.tick()
    assert rig.phase() == "stopping"
    rig.clock.advance(rig.backend.stop_s)
    ctl.tick()
    assert rig.phase() == "stopped" and rig.backend.state == "stopped"
    assert rig.queue.get(jid)["status"] == "succeeded"
    kinds = [e["kind"] for e in rig.store.events(100)]
    assert kinds.index("idle_stop") < kinds.index("stopped")


def test_arrival_during_shutdown_is_kept_and_restarts(rig: Rig):
    ctl = rig.controller()
    rig.queue.submit(JOB)
    bring_up(rig, ctl)
    j = rig.queue.claim("w")
    rig.queue.complete(j, {})
    rig.clock.advance(3600)
    ctl.tick()
    assert rig.phase() == "stopping"
    late = rig.queue.submit(JOB)  # arrives while stopping
    assert rig.queue.claim("w") is None  # never dispatched to a stopping host
    ctl.tick()
    assert rig.phase() == "stopping"
    rig.clock.advance(rig.backend.stop_s)
    ctl.tick()  # stopped
    ctl.tick()  # pending demand -> start again
    assert rig.phase() == "starting" and rig.backend.effective_starts == 2
    rig.clock.advance(rig.backend.boot_s)
    ctl.tick()
    assert rig.phase() == "ready"
    assert rig.queue.claim("w").job_id == late


def test_controller_crash_and_restart_resume_the_cycle(rig: Rig):
    a = rig.controller(owner="ctl-a", lease_ttl_s=60)
    b = rig.controller(owner="ctl-b", lease_ttl_s=60)
    rig.queue.submit(JOB)
    rig.backend.fail_start = True  # the start call is lost (API error, or A dies right after committing)
    r = a.tick()
    assert r.leader and r.action_errors and rig.phase() == "starting" and rig.backend.state == "stopped"
    # A is gone. B cannot act while A's lease is valid ...
    assert not b.tick().leader
    rig.backend.fail_start = False
    rig.clock.advance(61)
    # ... and takes over after it expires, re-issuing the start from the durable phase
    r = b.tick()
    assert r.leader and r.actions == ("start_instance",) and rig.backend.state == "pending"
    rig.clock.advance(rig.backend.boot_s)
    rig.prober.state = READY
    b.tick()
    assert rig.phase() == "ready"
    # A comes back: it is not the leader any more and cannot act
    assert not a.tick().leader


def test_restart_in_the_middle_of_a_stop(rig: Rig):
    a = rig.controller(owner="ctl-a")
    rig.queue.submit(JOB)
    bring_up(rig, a)
    rig.queue.complete(rig.queue.claim("w"), {})
    rig.clock.advance(3600)
    rig.backend.fail_stop = True
    a.tick()
    assert rig.phase() == "stopping" and rig.backend.state == "running"
    rig.backend.fail_stop = False
    rig.clock.advance(61)
    b = rig.controller(owner="ctl-b")
    assert b.tick().actions == ("stop_instance",)
    rig.clock.advance(rig.backend.stop_s)
    b.tick()
    assert rig.phase() == "stopped"


def test_dispatch_gate_and_capacity_queue(rig: Rig):
    ctl = rig.controller()
    ids = [rig.queue.submit(JOB) for _ in range(3)]
    ctl.tick()
    assert rig.queue.claim("w") is None  # starting
    rig.clock.advance(rig.backend.boot_s)
    rig.prober.state = READY
    ctl.tick()
    first = rig.queue.claim("w1")
    assert first is not None and rig.queue.claim("w2") is None  # max_in_flight = server slots = 1
    assert rig.queue.complete(first, {})
    second = rig.queue.claim("w2")
    assert second is not None and second.job_id == ids[1]


def test_expired_worker_lease_returns_work_and_does_not_hold_the_host(rig: Rig):
    ctl = rig.controller()
    rig.queue.submit(JOB)
    bring_up(rig, ctl)
    job = rig.queue.claim("w-dies")
    assert JobQueue.demand(rig.store.connect(), rig.clock.now()).active == 1
    rig.clock.advance(rig.queue.policy.lease_ttl_s)
    dem = JobQueue.demand(rig.store.connect(), rig.clock.now())
    assert dem.active == 0 and dem.pending == 1
    assert not rig.queue.complete(job, {})  # fenced: the dead worker cannot publish
    again = rig.queue.claim("w2")
    assert again is not None and again.attempts == 2


def test_manual_stop_drains_active_work_and_keeps_pending(rig: Rig):
    ctl = rig.controller()
    for _ in range(3):
        rig.queue.submit(JOB)
    bring_up(rig, ctl)
    running = rig.queue.claim("w1")
    ctl.command("stop")
    st = status(rig.store, rig.backend, rig.prober, JobQueue.demand, rig.policy, rig.clock)
    assert st.autostart_paused and st.phase == "draining" and not st.dispatch_allowed
    assert rig.queue.claim("w2") is None  # no new dispatch while draining
    ctl.tick()
    assert rig.phase() == "draining"  # one job still active
    rig.queue.complete(running, {})
    ctl.tick()
    assert rig.phase() == "stopping"
    rig.clock.advance(rig.backend.stop_s)
    ctl.tick()
    ctl.tick()
    assert rig.phase() == "stopped" and rig.backend.effective_starts == 1
    assert rig.queue.counts() == {"pending": 2, "succeeded": 1}
    # start resumes: the pending work is dispatched after readiness
    ctl.command("start")
    ctl.tick()
    rig.clock.advance(rig.backend.boot_s)
    ctl.tick()
    assert rig.phase() == "ready" and rig.queue.claim("w3") is not None


def test_status_reports_instance_readiness_pause_and_idle_timer(rig: Rig):
    ctl = rig.controller()
    st = status(rig.store, rig.backend, rig.prober, JobQueue.demand, rig.policy, rig.clock)
    assert (st.instance, st.phase, st.autostart_paused, st.readiness) == ("stopped", "stopped", False, None)
    rig.queue.submit(JOB)
    bring_up(rig, ctl)
    rig.queue.complete(rig.queue.claim("w"), {})
    rig.clock.advance(600)
    st = status(rig.store, rig.backend, rig.prober, JobQueue.demand, rig.policy, rig.clock)
    assert st.readiness.state == "ready" and st.idle_for_s == 600 and st.idle_stop_in_s == 3000
    rig.backend.fail_describe = True
    st = status(rig.store, rig.backend, rig.prober, JobQueue.demand, rig.policy, rig.clock)
    assert st.instance is None and st.instance_error == "describe_failed"


def test_stop_decision_and_claims_are_serialized(rig: Rig):
    """Randomized race between submissions, claims/completions and idle-stop ticks: a host is never stopped (or
    stopping) by the idle rule while a job is running, and no job is ever claimed outside `ready`."""
    ctl = rig.controller()
    rig.queue.submit(JOB)
    bring_up(rig, ctl)
    rig.queue.complete(rig.queue.claim("w"), {})
    rnd = random.Random(7)
    violations: list[str] = []
    stop = threading.Event()

    def check() -> None:
        with rig.store.tx() as conn:
            phase = rig.store.read(conn).state.phase
            running = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'running'").fetchone()[0]
            if phase in ("stopping", "stopped") and running:
                violations.append(f"{phase} with {running} running")

    def worker() -> None:
        while not stop.is_set():
            j = rig.queue.claim(f"w{threading.get_ident()}")
            if j is not None:
                check()
                rig.queue.complete(j, {})

    def submitter() -> None:
        for _ in range(40):
            if rnd.random() < 0.5:
                rig.queue.submit(JOB)
            check()

    def ticker() -> None:
        for _ in range(200):
            rig.clock.advance(rnd.choice([0, 0, 30, 3600]))
            ctl.tick()
            check()

    threads = [threading.Thread(target=f) for f in (worker, worker, submitter, ticker)]
    for t in threads:
        t.start()
    for t in threads[2:]:
        t.join()
    stop.set()
    for t in threads[:2]:
        t.join()
    assert not violations
