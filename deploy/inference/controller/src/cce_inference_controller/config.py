"""Controller configuration (TOML). Example: deploy/inference/controller/controller.example.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from .decide import Policy
from .executor import ExecutorPolicy


@dataclass(frozen=True)
class EndpointConfig:
    base_url: str
    model_alias: str
    api_key_file: str | None = None
    canary: bool = True
    canary_timeout_s: float = 180.0
    canary_max_tokens: int = 1024


@dataclass(frozen=True)
class BackendConfig:
    kind: str  # aws | command
    instance_id: str | None = None
    region: str | None = None
    profile: str | None = None
    describe: str | None = None
    start: str | None = None
    stop: str | None = None


@dataclass(frozen=True)
class Config:
    db_path: str
    tick_interval_s: float
    lease_ttl_s: float
    policy: Policy
    executor: ExecutorPolicy
    endpoint: EndpointConfig
    backend: BackendConfig


def _pick[T](cls: type[T], data: dict[str, Any], section: str) -> T:
    names = {f.name for f in fields(cls)}  # type: ignore[arg-type]
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"[{section}] unknown keys: {sorted(unknown)}")
    return cls(**data)


def load(path: str | Path) -> Config:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    unknown = set(data) - {"controller", "policy", "executor", "endpoint", "backend"}
    if unknown:
        raise ValueError(f"unknown sections: {sorted(unknown)}")
    ctl = dict(data.get("controller", {}))
    backend = _pick(BackendConfig, dict(data.get("backend", {})), "backend")
    if backend.kind == "aws" and not (backend.instance_id and backend.region):
        raise ValueError("[backend] kind=aws needs instance_id and region")
    if backend.kind == "command" and not (backend.describe and backend.start and backend.stop):
        raise ValueError("[backend] kind=command needs describe, start and stop")
    if backend.kind not in ("aws", "command"):
        raise ValueError("[backend] kind must be aws or command")
    cfg = Config(
        db_path=str(ctl.pop("db_path")),
        tick_interval_s=float(ctl.pop("tick_interval_s", 10.0)),
        lease_ttl_s=float(ctl.pop("lease_ttl_s", 60.0)),
        policy=_pick(Policy, dict(data.get("policy", {})), "policy"),
        executor=_pick(ExecutorPolicy, dict(data.get("executor", {})), "executor"),
        endpoint=_pick(EndpointConfig, dict(data["endpoint"]), "endpoint"),
        backend=backend,
    )
    if ctl:
        raise ValueError(f"[controller] unknown keys: {sorted(ctl)}")
    if cfg.lease_ttl_s <= cfg.tick_interval_s:
        raise ValueError("lease_ttl_s must be greater than tick_interval_s")
    return cfg
