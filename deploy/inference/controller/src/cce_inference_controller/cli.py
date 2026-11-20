"""`cce-inference` — operate the inference host.

    cce-inference --config controller.toml status [--json]
    cce-inference --config controller.toml start [--wait SECONDS]
    cce-inference --config controller.toml stop  [--wait SECONDS]
    cce-inference --config controller.toml serve            # the controller loop (leader election by lease)
    cce-inference --config controller.toml tick             # one reconciliation pass
    cce-inference --config controller.toml probe [--full]   # readiness only
    cce-inference --config controller.toml events [--limit N]
    cce-inference --config controller.toml submit FILE.json # test executor: enqueue one durable job
    cce-inference --config controller.toml work [--once]    # test executor: worker loop
    cce-inference --config controller.toml job JOB_ID

`stop` pauses auto-start, drains active work and stops the host; pending work stays durable. `start` resumes
auto-start and starts (or keeps) the host. `status` reports the instance state, readiness and whether auto-start is
paused. With `--wait`, the command also runs reconciliation passes itself when no other controller holds the lease.
Exit codes: 0 ok, 1 error, 2 usage, 3 `--wait` timed out.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
from dataclasses import asdict
from typing import Any

from .backends import CommandBackend, Ec2Backend, InstanceBackend
from .clock import SystemClock
from .config import Config, load
from .controller import Controller, status
from .executor import JobQueue, Worker
from .llama import HttpProber, LlamaClient, read_api_key
from .store import Store


def build(cfg: Config) -> tuple[Store, Controller, JobQueue, HttpProber, LlamaClient]:
    clock = SystemClock()
    store = Store(cfg.db_path)
    store.initialize(clock.now())
    backend: InstanceBackend
    if cfg.backend.kind == "aws":
        assert cfg.backend.instance_id and cfg.backend.region
        backend = Ec2Backend(cfg.backend.instance_id, region=cfg.backend.region, profile=cfg.backend.profile)
    else:
        assert cfg.backend.describe and cfg.backend.start and cfg.backend.stop
        backend = CommandBackend(cfg.backend.describe, cfg.backend.start, cfg.backend.stop)
    client = LlamaClient(
        cfg.endpoint.base_url,
        api_key=read_api_key(cfg.endpoint.api_key_file),
        timeout_s=cfg.executor.inference_timeout_s,
    )
    prober = HttpProber(
        client,
        model_alias=cfg.endpoint.model_alias,
        canary=cfg.endpoint.canary,
        canary_timeout_s=cfg.endpoint.canary_timeout_s,
        canary_max_tokens=cfg.endpoint.canary_max_tokens,
    )
    queue = JobQueue(store, clock, cfg.executor)
    owner = f"{socket.gethostname()}:{os.getpid()}"
    ctl = Controller(
        store, backend, prober, JobQueue.demand, cfg.policy, clock, owner=owner, lease_ttl_s=cfg.lease_ttl_s
    )
    return store, ctl, queue, prober, client


def _print(obj: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, sort_keys=True, default=str))
    else:
        for k, v in obj.items():
            print(f"{k:22} {v}")


def _status_dict(cfg: Config, store: Store, ctl: Controller, prober: HttpProber) -> dict[str, Any]:
    st = status(store, ctl.backend, prober, JobQueue.demand, cfg.policy, ctl.clock)
    d = asdict(st)
    d["readiness"] = f"{st.readiness.state} ({st.readiness.code})" if st.readiness else "not probed"
    return d


def _wait(cfg: Config, store: Store, ctl: Controller, target: str, seconds: float) -> bool:
    deadline = ctl.clock.now() + seconds
    while ctl.clock.now() < deadline:
        ctl.tick()  # no-op when another controller is the leader
        if store.snapshot().state.phase == target:
            return True
        ctl.clock.sleep(cfg.tick_interval_s)
    return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="cce-inference", description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    ap.add_argument("--config", default=os.environ.get("CCE_INFERENCE_CONFIG"), help="controller TOML")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("status")
    p.add_argument("--json", action="store_true")
    for name in ("start", "stop"):
        p = sub.add_parser(name)
        p.add_argument("--wait", type=float, default=0.0, metavar="SECONDS")
    sub.add_parser("serve")
    sub.add_parser("tick")
    p = sub.add_parser("probe")
    p.add_argument("--full", action="store_true", help="also check the alias and run the structured canary")
    p = sub.add_parser("events")
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("submit")
    p.add_argument("file")
    p = sub.add_parser("work")
    p.add_argument("--once", action="store_true")
    p.add_argument("--name", default=f"{socket.gethostname()}:{os.getpid()}")
    p = sub.add_parser("job")
    p.add_argument("job_id")
    args = ap.parse_args(argv)
    if not args.config:
        ap.error("--config (or CCE_INFERENCE_CONFIG) is required")
    cfg = load(args.config)
    store, ctl, queue, prober, client = build(cfg)

    if args.cmd == "status":
        _print(_status_dict(cfg, store, ctl, prober), args.json)
        return 0
    if args.cmd in ("start", "stop"):
        state = ctl.command(args.cmd)
        print(f"{args.cmd}: mode={state.mode} phase={state.phase}")
        if args.wait > 0:
            target = "ready" if args.cmd == "start" else "stopped"
            if not _wait(cfg, store, ctl, target, args.wait):
                print(f"timed out waiting for phase={target}", file=sys.stderr)
                return 3
            print(f"phase={target}")
        return 0
    if args.cmd == "tick":
        r = ctl.tick()
        print(json.dumps(asdict(r), default=str))
        return 0
    if args.cmd == "serve":
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        signal.signal(signal.SIGINT, lambda *_: stop.set())
        try:
            ctl.run(stop, interval_s=cfg.tick_interval_s)
        finally:
            store.release_lease(ctl.owner)
        return 0
    if args.cmd == "probe":
        rd = prober.probe(full=args.full)
        print(f"{rd.state} {rd.code}")
        return 0 if rd.state == "ready" else 1
    if args.cmd == "events":
        for e in store.events(args.limit):
            print(json.dumps(e, sort_keys=True))
        return 0
    if args.cmd == "submit":
        with open(args.file, encoding="utf-8") as fh:
            payload = json.load(fh)
        print(queue.submit(payload))
        return 0
    if args.cmd == "work":
        worker = Worker(queue, client, name=args.name, model=cfg.endpoint.model_alias)
        stop = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stop.set())
        while not stop.is_set():
            outcome = worker.run_once()
            if outcome is not None:
                print(outcome, flush=True)
            if args.once:
                return 0
            if outcome is None:
                stop.wait(cfg.tick_interval_s)
        return 0
    if args.cmd == "job":
        job = queue.get(args.job_id)
        if job is None:
            print("not found", file=sys.stderr)
            return 1
        _print(job, True)
        return 0
    return 2  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
