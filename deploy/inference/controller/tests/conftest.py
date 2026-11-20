from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from cce_inference_controller.backends import FakeBackend  # noqa: E402
from cce_inference_controller.clock import ManualClock  # noqa: E402
from cce_inference_controller.controller import Controller  # noqa: E402
from cce_inference_controller.decide import Policy, Readiness  # noqa: E402
from cce_inference_controller.executor import ExecutorPolicy, JobQueue  # noqa: E402
from cce_inference_controller.store import Store  # noqa: E402

JOB = {
    "messages": [{"role": "system", "content": "Classify."}, {"role": "user", "content": "Synthetic text."}],
    "schema": {
        "type": "object",
        "properties": {"categoria": {"type": "string", "enum": ["GENERAL"]}},
        "required": ["categoria"],
    },
    "max_tokens": 64,
}


@dataclass
class ScriptedProber:
    """Readiness controlled by the test. `calls` counts probes (none of them is inference activity)."""

    state: Readiness = field(default_factory=lambda: Readiness("loading", "health_503"))
    calls: int = 0
    full_calls: int = 0
    liveness_only: bool = False  # when True, a full probe cannot verify the model (e.g. canary not answered yet)

    def probe(self, *, full: bool) -> Readiness:
        self.calls += 1
        self.full_calls += int(full)
        if self.state.state == "ready" and (not full or self.liveness_only):
            return Readiness("ready", "health_ok")  # like HttpProber: liveness answers, nothing verified
        return self.state


@dataclass
class Rig:
    clock: ManualClock
    store: Store
    backend: FakeBackend
    prober: ScriptedProber
    queue: JobQueue
    policy: Policy

    def controller(self, owner: str = "ctl-a", lease_ttl_s: float = 60.0) -> Controller:
        return Controller(
            self.store,
            self.backend,
            self.prober,
            JobQueue.demand,
            self.policy,
            self.clock,
            owner=owner,
            lease_ttl_s=lease_ttl_s,
        )

    def phase(self) -> str:
        return self.store.snapshot().state.phase


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    clock = ManualClock()
    store = Store(tmp_path / "state.db")
    store.initialize(clock.now())
    policy = Policy()
    return Rig(
        clock=clock,
        store=store,
        backend=FakeBackend(clock, boot_s=30, stop_s=20),
        prober=ScriptedProber(),
        queue=JobQueue(store, clock, ExecutorPolicy(max_in_flight=1)),
        policy=policy,
    )
