"""End to end over real HTTP: controller (controlled clock) + fake EC2 backend that boots a fake llama-server +
real readiness prober + test executor. The model is simulated; the transport, readiness contract, slots, context
admission, timeouts and error mapping are real code paths."""

from __future__ import annotations

import socket
import threading
import time
from pathlib import Path

import pytest

from cce_inference_controller.backends import FakeBackend
from cce_inference_controller.clock import ManualClock
from cce_inference_controller.controller import Controller
from cce_inference_controller.decide import Policy
from cce_inference_controller.executor import ExecutorPolicy, JobQueue, Worker
from cce_inference_controller.llama import HttpProber, LlamaClient
from cce_inference_controller.store import Store
from conftest import JOB
from fake_llama_server import FakeLlama

KEY = "test-key-not-a-secret"
ALIAS = "qwen3.6-27b-q4km"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class Host:
    """A fake GPU host: the fake EC2 backend brings a fake llama-server up on boot and down on stop."""

    def __init__(
        self,
        tmp: Path,
        *,
        load_s: float = 0.2,
        parallel: int = 1,
        ctx_total: int = 8192,
        max_in_flight: int = 1,
        inference_timeout_s: float = 5.0,
        api_key: str = KEY,
    ) -> None:
        self.clock = ManualClock()
        self.port = free_port()
        self.load_s, self.parallel, self.ctx_total = load_s, parallel, ctx_total
        self.server: FakeLlama | None = None
        self.servers_started = 0
        self.backend = FakeBackend(self.clock, boot_s=30, stop_s=20, on_running=self._up, on_stopped=self._down)
        self.store = Store(tmp / "state.db")
        self.store.initialize(self.clock.now())
        self.policy = Policy(readiness_timeout_s=600)
        self.queue = JobQueue(
            self.store, self.clock, ExecutorPolicy(max_in_flight=max_in_flight, inference_timeout_s=inference_timeout_s)
        )
        self.client = LlamaClient(f"http://127.0.0.1:{self.port}", api_key=api_key, timeout_s=inference_timeout_s)
        self.prober = HttpProber(self.client, model_alias=ALIAS, canary_timeout_s=5)
        self.ctl = Controller(
            self.store, self.backend, self.prober, JobQueue.demand, self.policy, self.clock, owner="c"
        )

    def _up(self) -> None:
        self.server = FakeLlama(
            alias=ALIAS,
            ctx_total=self.ctx_total,
            parallel=self.parallel,
            api_key=KEY,
            load_s=self.load_s,
            port=self.port,
        ).start()
        self.servers_started += 1

    def _down(self) -> None:
        if self.server:
            self.server.stop()
            self.server = None

    def phase(self) -> str:
        return self.store.snapshot().state.phase

    def until_ready(self, real_timeout_s: float = 5.0) -> None:
        if self.phase() == "stopped" and not self.queue.counts().get("pending"):
            self.ctl.command("start")  # no demand yet: manual start
        self.ctl.tick()
        self.clock.advance(self.backend.boot_s)
        end = time.monotonic() + real_timeout_s
        while self.phase() != "ready":
            assert time.monotonic() < end, f"not ready: {self.phase()} {self.store.events(5)}"
            self.ctl.tick()
            time.sleep(0.05)

    def close(self) -> None:
        self._down()


@pytest.fixture
def host(tmp_path):
    h = Host(tmp_path)
    yield h
    h.close()


def test_full_cycle_over_http(host: Host):
    jid = host.queue.submit(JOB)
    host.ctl.tick()
    assert host.phase() == "starting"
    host.clock.advance(host.backend.boot_s)
    r = host.ctl.tick()  # running, weights still loading
    assert r.readiness.state == "loading" and host.phase() == "starting"
    host.until_ready()
    ready_event = [e for e in host.store.events(50) if e["kind"] == "ready"][-1]
    assert ready_event["code"] == "canary_ok"
    worker = Worker(host.queue, host.client, name="w1", model=ALIAS)
    assert worker.run_once() == "succeeded"
    job = host.queue.get(jid)
    assert job["status"] == "succeeded" and '"categoria": "GENERAL"' in job["result"]
    sent = host.server.requests[-1]
    assert sent["response_format"]["type"] == "json_schema" and sent["response_format"]["json_schema"]["strict"]
    assert sent["temperature"] == 0 and sent["max_tokens"] == JOB["max_tokens"]
    completions = host.server.counts["POST /v1/chat/completions"]  # canary + job
    for _ in range(60):  # one hour of liveness probes
        host.clock.advance(60)
        host.ctl.tick()
    assert host.server is None or host.server.counts["POST /v1/chat/completions"] == completions
    assert host.phase() == "stopping"
    host.clock.advance(host.backend.stop_s)
    host.ctl.tick()
    assert host.phase() == "stopped" and host.server is None
    assert host.prober.probe(full=False).state == "unreachable"


def test_input_over_the_admitted_context_is_rejected_explicitly(host: Host, monkeypatch):
    big = dict(JOB, messages=[{"role": "user", "content": "palabra " * 6000}])  # ~12k fake tokens > 8192
    tight = dict(JOB, max_tokens=8190)  # small prompt, but prompt + output budget > context per slot
    ids = [host.queue.submit(big), host.queue.submit(tight)]
    host.until_ready()
    worker = Worker(host.queue, host.client, name="w", model=ALIAS)
    before = host.server.counts.get("POST /v1/chat/completions", 0)
    assert worker.run_once() == "failed" and worker.run_once() == "failed"
    for jid in ids:
        job = host.queue.get(jid)
        assert job["status"] == "failed" and job["error_code"] == "input_exceeds_context" and job["attempts"] == 1
    assert '"n_ctx_slot": 8192' in host.queue.get(ids[0])["error_detail"]
    assert host.server.counts.get("POST /v1/chat/completions", 0) == before  # never sent to the model
    # the server-side rejection maps to the same explicit, non-retryable code
    monkeypatch.setattr(host.client, "count_prompt_tokens", lambda messages: 1)
    jid = host.queue.submit(big)
    assert worker.run_once() == "failed"
    assert host.queue.get(jid)["error_code"] == "input_exceeds_context"


def test_work_beyond_capacity_waits_durably_in_the_queue(tmp_path):
    host = Host(tmp_path, parallel=1, max_in_flight=1)
    try:
        host.until_ready()
        host.server.gen_s = 0.15
        ids = [host.queue.submit(JOB) for _ in range(4)]
        outcomes: list[str | None] = []

        def run(name: str) -> None:
            w = Worker(host.queue, host.client, name=name, model=ALIAS)
            while True:
                o = w.run_once()
                if o is None and all(host.queue.get(i)["status"] == "succeeded" for i in ids):
                    return
                if o is not None:
                    outcomes.append(o)
                time.sleep(0.01)

        threads = [threading.Thread(target=run, args=(f"w{i}",)) for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=20)
        assert outcomes.count("succeeded") == 4
        assert host.server.max_in_flight == 1  # the excess never reached the server
    finally:
        host.close()


def test_two_slots_admit_two_in_flight(tmp_path):
    host = Host(tmp_path, parallel=2, ctx_total=16384, max_in_flight=2)
    try:
        host.until_ready()
        assert host.client.slot_context() == 8192  # effective context per slot = total / slots
        host.server.gen_s = 0.3
        for _ in range(4):
            host.queue.submit(JOB)
        threads = [threading.Thread(target=Worker(host.queue, host.client, name=f"w{i}").run_once) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        assert host.server.max_in_flight == 2 and host.queue.counts() == {"succeeded": 2, "pending": 2}
    finally:
        host.close()


def test_inference_timeout_is_retried_and_does_not_change_readiness(tmp_path):
    host = Host(tmp_path, inference_timeout_s=0.5)
    try:
        jid = host.queue.submit(JOB)
        host.until_ready()
        host.server.hang.set()
        w = Worker(host.queue, host.client, name="w", model=ALIAS)
        assert w.run_once() == "retry_wait"
        job = host.queue.get(jid)
        assert job["status"] == "pending" and job["error_code"] == "inference_timeout"
        host.ctl.tick()
        assert host.phase() == "ready"  # the inference timeout is not a readiness failure
        host.server.hang.clear()
        assert w.run_once() is None  # backoff: not yet available
        host.clock.advance(30)
        assert w.run_once() == "succeeded" and host.queue.get(jid)["attempts"] == 2
    finally:
        host.close()


def test_readiness_never_reached_stops_the_host_and_keeps_work(tmp_path):
    host = Host(tmp_path, load_s=3600)  # the weights never finish loading during the test
    try:
        jid = host.queue.submit(JOB)
        host.ctl.tick()
        host.clock.advance(host.backend.boot_s)
        host.ctl.tick()
        assert host.phase() == "starting"
        host.clock.advance(host.policy.readiness_timeout_s)
        host.ctl.tick()
        assert host.phase() == "stopping"
        ev = [e for e in host.store.events(20) if e["kind"] == "readiness_timeout"][-1]
        assert ev["code"] == "health_503" and ev["attempt"] == 1
        host.clock.advance(host.backend.stop_s)
        host.ctl.tick()
        assert host.phase() == "stopped" and host.queue.get(jid)["status"] == "pending"
        host.clock.advance(host.policy.start_retry_backoff_s)
        host.ctl.tick()
        assert host.phase() == "starting" and host.backend.effective_starts == 2  # retry
    finally:
        host.close()


def test_wrong_api_key_is_not_ready(tmp_path):
    host = Host(tmp_path, api_key="wrong")
    try:
        host.queue.submit(JOB)
        host.ctl.tick()
        host.clock.advance(host.backend.boot_s)
        host.ctl.tick()
        time.sleep(0.3)
        r = host.ctl.tick()
        assert r.readiness.state == "unhealthy" and r.readiness.code == "canary:completion_http_401"
        assert host.phase() == "starting"
    finally:
        host.close()
