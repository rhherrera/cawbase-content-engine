from __future__ import annotations

import hashlib
import json
import os
import socket
import stat
import subprocess
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

HERE = Path(__file__).parent
DEPLOY = HERE.parent.parent  # deploy/inference
SERVER = DEPLOY / "server"
MANIFEST = DEPLOY / "manifest.json"
Q8 = "qwen3.6-27b-q8_0"  # the AWS target model of manifest.json
Q4 = "qwen3.6-27b-q4km"  # the on-premise target model of manifest.json

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


def make_test_manifest(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    """manifest.json for the tests: the real one (ids, targets, aliases, file names), with every model's bytes,
    SHA-256 and URL replaced by a tiny local file. Written to TMP/manifest.json, where sim_host.sh finds it."""
    m = json.loads(MANIFEST.read_text(encoding="utf-8"))
    sources: dict[str, Path] = {}
    for model_id, entry in m["models"].items():
        src = tmp_path / f"src-{model_id}.gguf"
        src.write_bytes(b"GGUF" + model_id.encode() + os.urandom(4096))
        entry.update(bytes=src.stat().st_size, sha256=hashlib.sha256(src.read_bytes()).hexdigest(), url=src.as_uri())
        sources[model_id] = src
    out = tmp_path / "manifest.json"
    out.write_text(json.dumps(m, indent=2), encoding="utf-8")
    return out, sources


def install_model(manifest: Path, model_id: str, models_dir: Path, src: Path | None = None) -> Path:
    """Install a model of the test manifest with the real install-model.sh (verification record included)."""
    argv = [str(SERVER / "install-model.sh"), "--model", model_id, "--dir", str(models_dir)]
    if src is not None:
        argv += ["--from", str(src)]
    subprocess.run(argv, check=True, capture_output=True, env={**os.environ, "CCE_MANIFEST": str(manifest)})
    file = json.loads(manifest.read_text())["models"][model_id]["file"]
    return models_dir / file


@pytest.fixture
def fake_model(tmp_path: Path) -> dict[str, str]:
    """The AWS target (Q8_0 id, file name and alias) installed from a tiny file through the test manifest."""
    manifest, sources = make_test_manifest(tmp_path)
    path = install_model(manifest, Q8, tmp_path / "models")
    return {"path": str(path), "manifest": str(manifest), "model_id": Q8, "alias": Q8, "q4_src": str(sources[Q4])}


def server_env(
    tmp_path: Path,
    model: str,
    port: int,
    profile: str = "reference-8k-1slot",
    *,
    model_id: str | None = Q8,
    alias: str = Q8,
) -> Path:
    """server.env as bootstrap.sh / onprem/install.sh write it; model_id=None is a pre-selection (legacy) file."""
    key = tmp_path / "api-key"
    key.write_text("cli-test-key\n")
    key.chmod(0o600)
    fake_bin = tmp_path / "fake-llama-server"
    fake_bin.write_text(f'#!/bin/sh\nexec {sys.executable} {HERE / "fake_llama_server.py"} "$@"\n')
    fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IEXEC)
    env = tmp_path / "server.env"
    env.write_text(
        "\n".join(
            [
                *([f"CCE_MODEL_ID={model_id}"] if model_id else []),
                f"CCE_MODEL_PATH={model}",
                f"CCE_MODEL_ALIAS={alias}",
                f"CCE_PROFILE={SERVER / 'profiles' / (profile + '.env')}",
                "CCE_HOST=127.0.0.1",
                f"CCE_PORT={port}",
                f"CCE_API_KEY_FILE={key}",
                f"CCE_LLAMA_SERVER_BIN={fake_bin}",
            ]
        )
        + "\n"
    )
    return env


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
