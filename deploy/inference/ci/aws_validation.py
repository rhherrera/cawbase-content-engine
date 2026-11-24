#!/usr/bin/env python3
"""A09-1 validation on AWS driven by GitHub Actions (standard library only; AWS through the `aws` CLI with the
temporary credentials of the workflow's OIDC role). Contract: docs/a09-1-aws-validation-automation.md.

A validation runs when a push to main ADDS one versioned request `deploy/inference/validation-requests/<id>.json`.
Modifying, renaming or re-running an existing request never spends again: the request id is claimed atomically in
SSM Parameter Store (`/cce/inference/validation-runs/<id>`, created without --overwrite) before anything is created.

    aws_validation.py check FILE...                      offline validation of request files (no AWS)
    aws_validation.py detect --before SHA --after SHA    the request added by a push, if any (no AWS)
    aws_validation.py oidc-claims                        safe subset of this job's OIDC claims (never the token)
    aws_validation.py run --request F --evidence DIR --workdir DIR --python PY
    aws_validation.py cleanup --request F --evidence DIR
    aws_validation.py redact DIR [--secret-file F]       remove secrets from the evidence before upload
    aws_validation.py summary DIR...                     markdown summary for $GITHUB_STEP_SUMMARY

`run`: preflight with no side effects (quota, network, AMI, configuration, spend ceiling, optional parity ZIP) ->
claim the id -> step A (stack, watchdog in AWS, bootstrap, SSM, first load, instance STOPPED) -> step B
(bench/gpu-validation.py from the runner through an SSM tunnel) -> cleanup as the request says (keep-stopped |
delete) and its verification. `cleanup` is the same cleanup, idempotent, for the workflow's separate cleanup job
(failure, timeout, cancellation).

Exit status of `run` (validation): 0 validated; 1 validation failed or incomplete (also a failed provisioning);
2 refused by the preflight (nothing claimed, nothing created); 3 request id already claimed (no new spend);
130 interrupted. Exit status of `cleanup`: 0 cleanup verified; 5 CLEANUP FAILED (resources not verified).

Tool overrides (tests only): CCE_AWS_BIN, CCE_POLL_S, CCE_CI_CONTROLLER_TEMPLATE, CCE_CI_HOST_<NAME>_CMD,
CCE_SERVER_LOG_CMD, CCE_MANIFEST. Needs Python >= 3.11 (the runner's python3; the controller environment for tests).
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import importlib.util
import ipaddress
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

CI = Path(__file__).resolve().parent
DEPLOY = CI.parent  # deploy/inference
REPO = DEPLOY.parent.parent
REQUESTS_REL = "deploy/inference/validation-requests"
REQUESTS_DIR = REPO / REQUESTS_REL
MANIFEST = DEPLOY / "manifest.json"
PROFILES_DIR = DEPLOY / "server" / "profiles"
TEMPLATE = DEPLOY / "aws" / "cloudformation.yaml"
BUILD_TEMPLATE = DEPLOY / "aws" / "build_template.py"
GPU_VALIDATION = DEPLOY / "bench" / "gpu-validation.py"
SSM_RUN = DEPLOY / "bench" / "ssm-run.sh"
CONTROLLER_EXAMPLE = DEPLOY / "controller" / "controller.aws.example.toml"

REGION = "us-east-1"
ACCESS_STACK_DEFAULT = "cce-a09-github-access"
LEDGER_PREFIX = "/cce/inference/validation-runs/"
API_KEY_PREFIX = "/cce/inference/validation/"
AMI_PARAM = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id"
AMI_NAME = "Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 24.04)"
G_QUOTA_CODE = "L-DB2E81BA"  # Running On-Demand G and VT instances (vCPU)
WATCHDOG_GRACE_MIN = 15  # the watchdog starts this long after the job's time limit
WATCHDOG_RATE_MIN = 10
WATCHDOG_WINDOW_H = 3
CLEANUP_RESERVE_S = 25 * 60  # kept out of the gpu-validation budget for the in-run cleanup
MAX_ROLE_SESSION_S = 43200
BOOT_DONE = "units installed; server started"
DEFAULT_PROFILES = ["reference-8k-1slot", "candidate-16k-1slot", "candidate-16k-2slot"]

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$")
STACK_RE = re.compile(r"^cce-inference-[a-z0-9](?:[a-z0-9-]{0,34}[a-z0-9])?$")
VPC_RE = re.compile(r"^vpc-[0-9a-f]{8,17}$")
SUBNET_RE = re.compile(r"^subnet-[0-9a-f]{8,17}$")
SG_RE = re.compile(r"^sg-[0-9a-f]{8,17}$")
AMI_RE = re.compile(r"^ami-[0-9a-f]{8,17}$")
AZ_RE = re.compile(r"^us-east-1[a-f]$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
S3_RE = re.compile(r"^s3://([a-z0-9][a-z0-9.-]{1,61}[a-z0-9])/([A-Za-z0-9!_.*'()/-]+\.zip)$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# host-side commands (run as root through SSM Run Command); each is overridable for tests: CCE_CI_HOST_<NAME>_CMD
HOST_CMDS = {
    "BOOTSTRAP": "cat /var/lib/cce-inference/bootstrap.log 2>/dev/null || true",
    "CLOUD_INIT": "cloud-init status --long 2>&1 || true",
    "SERVICE": "systemctl is-active cce-inference 2>&1 || true",
    "HEALTH": "curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1:8080/health || true",
}
SERVER_LOG_CMD = "journalctl -u cce-inference -b --no-pager -o short-iso"
# parity outputs that reproduce the private corpus content: kept on the runner, never uploaded (hash recorded)
WITHHELD = re.compile(r"(^|/)parity-[^/]+/(evaluacion-r\d+-i\d+\.jsonl|proxy\.log)$")


class RequestError(Exception):
    pass


class Refused(Exception):
    pass


class Duplicate(Exception):
    pass


class ValidationFailed(Exception):
    pass


class Interrupted(BaseException):
    pass


class AwsError(Exception):
    def __init__(self, argv: list[str], rc: int, stderr: str) -> None:
        m = re.search(r"\(([A-Za-z0-9.]+)\)", stderr)
        self.code = m.group(1) if m else ""
        self.rc = rc
        super().__init__(f"aws {' '.join(argv[:2])}: exit {rc}: {stderr.strip()[-400:]}")


def _raise_interrupted(signum: int, _frame: Any) -> None:
    raise Interrupted(signal.Signals(signum).name)


def now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def save(ev: Path, name: str, data: Any) -> None:
    ev.mkdir(parents=True, exist_ok=True)
    (ev / name).write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n", encoding="utf-8")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --- manifest and request ---------------------------------------------------------------------------------------


def manifest() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(Path(os.environ.get("CCE_MANIFEST", str(MANIFEST))).read_text(encoding="utf-8"))
    return data


def aws_target() -> dict[str, Any]:
    m = manifest()
    price = m["aws"]["on_demand_usd_per_hour"]
    return {
        "instance_type": m["aws"]["instance_type"],
        "vcpu": int(m["aws"]["vcpu"]),
        "model": m["targets"]["aws"],
        "usd_per_hour": float(price["value"]),
    }


def profile_names() -> set[str]:
    return {p.stem for p in PROFILES_DIR.glob("*.env")}


def _keys(obj: Any, where: str, required: set[str], optional: set[str], errors: list[str]) -> dict[str, Any]:
    if not isinstance(obj, dict):
        errors.append(f"{where}: must be an object")
        return {}
    missing = required - set(obj)
    unknown = set(obj) - required - optional
    if missing:
        errors.append(f"{where}: missing {sorted(missing)}")
    if unknown:
        errors.append(f"{where}: unknown keys {sorted(unknown)}")
    return obj


def _int(obj: dict[str, Any], key: str, lo: int, hi: int, where: str, errors: list[str]) -> None:
    v = obj.get(key)
    if v is not None and (not isinstance(v, int) or isinstance(v, bool) or not lo <= v <= hi):
        errors.append(f"{where}.{key}: integer between {lo} and {hi}")


def worst_case_usd(req: dict[str, Any]) -> float:
    """Instance cost if it ran until the watchdog's first stop after the job's time limit (EBS apart)."""
    minutes = int(req["limits"]["max_runtime_minutes"]) + WATCHDOG_GRACE_MIN + WATCHDOG_RATE_MIN
    return round(float(aws_target()["usd_per_hour"]) * minutes / 60, 2)


def validate_request(doc: Any, filename: str | None = None) -> list[str]:
    """Offline validation (no AWS). Every key is explicit; nothing about spend or cleanup has a silent default."""
    e: list[str] = []
    top = _keys(
        doc,
        "request",
        {"schema_version", "id", "purpose", "authorization", "aws", "model", "network", "ami", "stack", "limits"},
        {"validation", "parity"},
        e,
    )
    if not top:
        return e
    target = aws_target()
    if top.get("schema_version") != 1:
        e.append("schema_version: must be 1")
    rid = top.get("id")
    if not isinstance(rid, str) or not ID_RE.match(rid):
        e.append("id: 3-32 characters [a-z0-9-], starting and ending with a letter or digit")
    elif filename is not None and filename != f"{rid}.json":
        e.append(f"id: the file must be named {rid}.json (found {filename})")
    if not isinstance(top.get("purpose"), str) or not top["purpose"].strip():
        e.append("purpose: non-empty text")
    auth = _keys(
        top.get("authorization"),
        "authorization",
        {"spend_approved", "network_approved", "approved_by", "approved_on", "max_spend_usd"},
        set(),
        e,
    )
    if auth:
        if auth.get("spend_approved") is not True or auth.get("network_approved") is not True:
            e.append("authorization: spend_approved and network_approved must both be true")
        if not isinstance(auth.get("approved_by"), str) or not auth["approved_by"].strip():
            e.append("authorization.approved_by: non-empty text")
        if not isinstance(auth.get("approved_on"), str) or not DATE_RE.match(auth["approved_on"]):
            e.append("authorization.approved_on: YYYY-MM-DD")
        spend = auth.get("max_spend_usd")
        if not isinstance(spend, int | float) or isinstance(spend, bool) or not 0 < spend <= 100:
            e.append("authorization.max_spend_usd: number in (0, 100]")
    aws = _keys(top.get("aws"), "aws", {"region", "instance_type"}, set(), e)
    if aws:
        if aws.get("region") != REGION:
            e.append(f"aws.region: must be {REGION}")
        if aws.get("instance_type") != target["instance_type"]:
            e.append(f"aws.instance_type: must be {target['instance_type']} (manifest.json aws.instance_type)")
    if top.get("model") != target["model"]:
        e.append(f"model: must be {target['model']} (manifest.json targets.aws)")
    net = _keys(
        top.get("network"),
        "network",
        {"vpc_id", "subnet_id", "availability_zone", "assign_public_ip"},
        {"client_cidr", "client_security_group_id"},
        e,
    )
    if net:
        for key, rx in (("vpc_id", VPC_RE), ("subnet_id", SUBNET_RE), ("availability_zone", AZ_RE)):
            if not isinstance(net.get(key), str) or not rx.match(net[key]):
                e.append(f"network.{key}: invalid ({rx.pattern})")
        if not isinstance(net.get("assign_public_ip"), bool):
            e.append("network.assign_public_ip: true or false")
        cidr = net.get("client_cidr", "")
        if cidr:
            try:
                ok = isinstance(cidr, str) and ipaddress.ip_network(cidr).is_private
            except ValueError:
                ok = False
            if not ok:
                e.append("network.client_cidr: empty or a private CIDR (the endpoint is never public)")
        sg = net.get("client_security_group_id", "")
        if sg and (not isinstance(sg, str) or not SG_RE.match(sg)):
            e.append("network.client_security_group_id: empty or sg-…")
    ami = top.get("ami")
    if not (ami == "resolve-latest" or (isinstance(ami, str) and AMI_RE.match(ami))):
        e.append('ami: "resolve-latest" or a pinned ami-… id')
    stack = _keys(top.get("stack"), "stack", {"name", "mode", "after_run"}, set(), e)
    if stack:
        if not isinstance(stack.get("name"), str) or not STACK_RE.match(stack["name"]):
            e.append("stack.name: cce-inference-[a-z0-9-] (the IAM policies are scoped to that prefix), ≤ 50 chars")
        if stack.get("mode") not in ("create", "reuse"):
            e.append('stack.mode: "create" (new stack) or "reuse" (an existing stopped stack)')
        if stack.get("after_run") not in ("keep-stopped", "delete"):
            e.append('stack.after_run: "keep-stopped" (kept for A09-2) or "delete" (removed after the run)')
    limits = _keys(top.get("limits"), "limits", {"max_runtime_minutes"}, {"bootstrap_timeout_minutes"}, e)
    if limits:
        _int(limits, "max_runtime_minutes", 60, 480, "limits", e)
        _int(limits, "bootstrap_timeout_minutes", 15, 120, "limits", e)
        if not isinstance(limits.get("max_runtime_minutes"), int):
            e.append("limits.max_runtime_minutes: required")
        elif limits.get("bootstrap_timeout_minutes", 60) >= limits["max_runtime_minutes"]:
            e.append("limits.bootstrap_timeout_minutes: must be below max_runtime_minutes")
    val = _keys(
        top.get("validation", {}),
        "validation",
        set(),
        {"profiles", "server_profile", "requests", "prompt_tokens"},
        e,
    )
    known = profile_names()
    profiles = val.get("profiles", DEFAULT_PROFILES)
    if not isinstance(profiles, list) or not profiles or not all(isinstance(p, str) for p in profiles):
        e.append("validation.profiles: non-empty list of profile names (the first one is the reference)")
    else:
        bad = [p for p in profiles if p not in known] + [p for p in [val.get("server_profile")] if p and p not in known]
        if bad:
            e.append(f"validation: unknown profiles {bad} (defined: {sorted(known)})")
        if len(set(profiles)) != len(profiles):
            e.append("validation.profiles: duplicated profile")
    _int(val, "requests", 1, 200, "validation", e)
    _int(val, "prompt_tokens", 100, 6000, "validation", e)
    if "parity" in top and top["parity"] is not None:
        par = _keys(top["parity"], "parity", {"s3_uri", "sha256"}, set(), e)
        if par:
            if not isinstance(par.get("s3_uri"), str) or not S3_RE.match(par["s3_uri"]):
                e.append("parity.s3_uri: s3://bucket/key.zip")
            if not isinstance(par.get("sha256"), str) or not SHA_RE.match(par["sha256"]):
                e.append("parity.sha256: 64 lowercase hex characters")
    if not e and auth and limits:
        cost = worst_case_usd(top)
        if cost > auth["max_spend_usd"]:
            e.append(
                f"authorization.max_spend_usd: {auth['max_spend_usd']} is below the worst case of the time limit "
                f"({cost} USD of {target['instance_type']} until the watchdog stops it)"
            )
    return e


def load_request(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RequestError(f"{path.name}: not readable JSON ({exc})") from exc
    # the file name must be the id inside the requests directory (an example elsewhere may be named freely)
    in_dir = path.resolve().parent == REQUESTS_DIR.resolve()
    errors = validate_request(doc, path.name if in_dir else None)
    if errors:
        raise RequestError(f"{path.name}: " + "; ".join(errors))
    req: dict[str, Any] = doc
    req.setdefault("validation", {})
    req["limits"].setdefault("bootstrap_timeout_minutes", 60)
    req["network"].setdefault("client_cidr", "")
    req["network"].setdefault("client_security_group_id", "")
    req.setdefault("parity", None)
    return req


def check_unique_ids(directory: Path) -> list[str]:
    seen: dict[str, str] = {}
    errors: list[str] = []
    for f in sorted(directory.glob("*.json")):
        try:
            rid = json.loads(f.read_text(encoding="utf-8")).get("id")
        except (OSError, ValueError):
            continue
        if rid in seen:
            errors.append(f"duplicated id {rid!r}: {seen[rid]} and {f.name}")
        seen[str(rid)] = f.name
    return errors


# --- detect (which request a push added) ---------------------------------------------------------------------------


def detect(before: str, after: str, repo: Path = REPO) -> tuple[Path | None, list[str]]:
    """The request file ADDED between two commits (None if none), and notes about ignored changes. A push that is
    not a fast-forward of a known commit, or that adds more than one request, is refused."""
    if not re.fullmatch(r"[0-9a-f]{40}", before or "") or set(before) == {"0"}:
        raise RequestError(f"no previous commit for this push (before={before!r}); nothing is run")
    r = subprocess.run(
        ["git", "-C", str(repo), "diff", "--name-status", "--no-renames", before, after, "--", REQUESTS_REL],
        capture_output=True,
        text=True,
        check=False,
    )
    if r.returncode != 0:
        raise RequestError(f"git diff {before[:8]}..{after[:8]} failed: {r.stderr.strip()}")
    added: list[str] = []
    notes: list[str] = []
    for line in r.stdout.splitlines():
        status, path = line.split("\t", 1)
        if not re.fullmatch(re.escape(REQUESTS_REL) + r"/[^/]+\.json", path):
            continue  # README and anything else in the directory never runs
        if status == "A":
            added.append(path)
        else:
            notes.append(f"{path}: {status} (only a newly ADDED request runs; a change never runs again)")
    if len(added) > 1:
        raise RequestError(f"one request per push: {added} were added together; nothing is run")
    return (repo / added[0] if added else None), notes


def detect_outputs(req: dict[str, Any], path: Path) -> dict[str, str]:
    limit = req["limits"]["max_runtime_minutes"]
    return {
        "request": str(path.relative_to(REPO)),
        "id": req["id"],
        "stack": req["stack"]["name"],
        "after_run": req["stack"]["after_run"],
        "timeout_minutes": str(limit),
        "role_duration_seconds": str(min(MAX_ROLE_SESSION_S, (limit + 30) * 60)),
    }


# --- OIDC claims (diagnostic: never the token) --------------------------------------------------------------------

SAFE_CLAIMS = ("sub", "aud", "ref", "repository", "repository_id", "repository_owner_id", "job_workflow_ref")


def oidc_claims(audience: str = "sts.amazonaws.com") -> dict[str, Any]:
    url = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    bearer = os.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not bearer:
        raise RequestError("no OIDC token available: the job needs `permissions: id-token: write`")
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(f"{url}{sep}audience={audience}", headers={"Authorization": f"bearer {bearer}"})
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 - GitHub's own token endpoint
        token = json.loads(resp.read())["value"]
    parts = token.split(".")
    if len(parts) != 3:
        raise RequestError("malformed OIDC token")
    payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    return {k: payload[k] for k in SAFE_CLAIMS if k in payload}


# --- AWS through the CLI ---------------------------------------------------------------------------------------


class Aws:
    def __init__(self) -> None:
        self.bin = os.environ.get("CCE_AWS_BIN", "aws")
        self.env = {**os.environ, "AWS_REGION": REGION, "AWS_DEFAULT_REGION": REGION, "AWS_PAGER": ""}

    def raw(self, *args: str, timeout: float = 120) -> str:
        argv = [self.bin, *args]
        r = subprocess.run(argv, capture_output=True, text=True, env=self.env, timeout=timeout, check=False)
        if r.returncode != 0:
            raise AwsError(list(args), r.returncode, r.stderr)
        return r.stdout

    def json(self, *args: str, timeout: float = 120) -> Any:
        out = self.raw(*args, "--output", "json", timeout=timeout).strip()
        return json.loads(out) if out else {}

    # --- helpers used by run and cleanup ---

    def stack(self, name: str) -> dict[str, Any] | None:
        try:
            stacks = self.json("cloudformation", "describe-stacks", "--stack-name", name).get("Stacks", [])
        except AwsError as exc:
            if "does not exist" in str(exc):
                return None
            raise
        if not stacks or stacks[0].get("StackStatus") == "DELETE_COMPLETE":
            return None
        s: dict[str, Any] = stacks[0]
        return s

    @staticmethod
    def outputs(stack: dict[str, Any]) -> dict[str, str]:
        return {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}

    def instance_state(self, iid: str) -> str:
        try:
            res = self.json("ec2", "describe-instances", "--instance-ids", iid)
        except AwsError as exc:
            if exc.code.startswith("InvalidInstanceID"):
                return "absent"
            raise
        insts = [i for r in res.get("Reservations", []) for i in r.get("Instances", [])]
        return str(insts[0]["State"]["Name"]) if insts else "absent"

    def stack_instances(self, stack: str) -> list[dict[str, Any]]:
        res = self.json(
            "ec2",
            "describe-instances",
            "--filters",
            f"Name=tag:aws:cloudformation:stack-name,Values={stack}",
            "Name=instance-state-name,Values=pending,running,stopping,stopped,shutting-down",
        )
        return [i for r in res.get("Reservations", []) for i in r.get("Instances", [])]

    def stack_volumes(self, stack: str) -> list[dict[str, Any]]:
        res = self.json(
            "ec2", "describe-volumes", "--filters", f"Name=tag:aws:cloudformation:stack-name,Values={stack}"
        )
        vols: list[dict[str, Any]] = res.get("Volumes", [])
        return vols

    def get_param(self, name: str, decrypt: bool = False) -> str | None:
        try:
            args = ["ssm", "get-parameter", "--name", name, *(["--with-decryption"] if decrypt else [])]
            return str(self.json(*args)["Parameter"]["Value"])
        except AwsError as exc:
            if exc.code == "ParameterNotFound":
                return None
            raise

    def put_param(self, name: str, value_file: Path, *, secure: bool = False, overwrite: bool = False) -> None:
        args = ["ssm", "put-parameter", "--name", name, "--value", f"file://{value_file}"]
        args += ["--type", "SecureString" if secure else "String"]
        self.raw(*args, *(["--overwrite"] if overwrite else []))

    def delete_param(self, name: str) -> bool:
        try:
            self.raw("ssm", "delete-parameter", "--name", name)
            return True
        except AwsError as exc:
            if exc.code == "ParameterNotFound":
                return False
            raise


def poll_s() -> float:
    return float(os.environ.get("CCE_POLL_S", "15"))


def wait_for(what: str, fn: Any, timeout: float, *, deadline: float | None = None) -> Any:
    """Call fn() until it returns a truthy value; a ValidationFailed it raises stops the wait."""
    end = time.monotonic() + timeout
    if deadline is not None:
        end = min(end, time.monotonic() + max(0.0, deadline - now()))
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() >= end:
            raise ValidationFailed(f"timed out waiting for {what} ({timeout:.0f}s)")
        time.sleep(poll_s())


# --- the run ----------------------------------------------------------------------------------------------------


class Run:
    def __init__(self, req: dict[str, Any], ev: Path, workdir: Path | None = None, python: str | None = None) -> None:
        self.req = req
        self.ev = ev.resolve()
        self.workdir = (workdir or ev.parent / f"{ev.name}-work").resolve()
        self.python = python or sys.executable
        self.aws = Aws()
        self.id: str = req["id"]
        self.stack_name: str = req["stack"]["name"]
        self.ledger = LEDGER_PREFIX + self.id
        self.key_param = f"{API_KEY_PREFIX}{self.stack_name}/api-key"
        self.schedule = f"cce-a09-{self.id}"
        self.t0 = now()
        self.deadline = self.t0 + req["limits"]["max_runtime_minutes"] * 60
        self.access: dict[str, str] = {}
        self.iid: str | None = None
        self.key_file = self.workdir / "secrets" / "api-key"
        self.record: dict[str, Any] = {"id": self.id, "stack": self.stack_name, "after_run": req["stack"]["after_run"]}
        self.parity_pkg: Path | None = None

    # --- small utilities ---

    def host_cmd(self, name: str) -> str:
        return os.environ.get(f"CCE_CI_HOST_{name}_CMD", HOST_CMDS[name])

    def on_host(self, command: str, timeout: int = 120) -> str:
        assert self.iid
        r = subprocess.run(
            [str(SSM_RUN), self.iid, command, str(timeout)],
            capture_output=True,
            text=True,
            env={**self.aws.env, "CCE_AWS_BIN": self.aws.bin},
            timeout=timeout + 180,
            check=False,
        )
        if r.returncode != 0:
            raise ValidationFailed(f"SSM command failed (exit {r.returncode}): {r.stderr.strip()[-300:]}")
        return r.stdout

    def ledger_update(self, **fields: Any) -> None:
        """Best effort: the claim record says how far the run got (never blocks the cleanup)."""
        try:
            current = json.loads(self.aws.get_param(self.ledger) or "{}")
            if current.get("run_id") != self.record.get("run_id"):
                return  # another run's claim is never touched
            current.update(fields, updated_at=iso(now()))
            path = self.workdir / "ledger.json"
            path.write_text(json.dumps(current, separators=(",", ":")), encoding="utf-8")
            self.aws.put_param(self.ledger, path, overwrite=True)
        except Exception as exc:  # noqa: BLE001 - recorded only
            log(f"ledger update failed: {exc}")

    # --- 0. preflight (read-only: nothing is created) ---

    def preflight(self) -> dict[str, Any]:
        req, net, target = self.req, self.req["network"], aws_target()
        pre: dict[str, Any] = {"target": target, "checks": {}}
        checks = pre["checks"]
        repo = REPO.resolve()
        if self.ev == repo or repo in self.ev.parents or self.workdir == repo or repo in self.workdir.parents:
            raise Refused("evidence and work directories must be outside the repository checkout")
        ident = self.aws.json("sts", "get-caller-identity")
        pre["caller"] = str(ident.get("Arn", "")).split(":", 5)[-1]  # assumed-role/NAME/SESSION, no account
        if self.aws.get_param(self.ledger) is not None:
            raise Duplicate(self.ledger)  # first: a retry reports the earlier claim, whatever changed since
        stale = subprocess.run([sys.executable, str(BUILD_TEMPLATE), "--check"], capture_output=True, text=True)
        if stale.returncode != 0:
            raise Refused(f"cloudformation.yaml is stale: {stale.stderr.strip()}")
        pre["template_sha256"] = sha256_file(TEMPLATE)
        checks["template_up_to_date"] = True
        # access stack (initial enablement): roles, watchdog group and the network the roles were scoped to
        name = os.environ.get("CCE_A09_ACCESS_STACK") or ACCESS_STACK_DEFAULT
        access = self.aws.stack(name)
        if not access or not str(access.get("StackStatus", "")).endswith("_COMPLETE"):
            raise Refused(f"access stack {name} is missing or not complete: run the initial enablement first")
        self.access = self.aws.outputs(access)
        for key in ("CfnExecutionRoleArn", "WatchdogRoleArn", "WatchdogScheduleGroup", "HostPermissionsBoundaryArn"):
            if not self.access.get(key):
                raise Refused(f"access stack {name}: output {key} missing")
        allowed = {
            "vpc_id": self.access.get("AllowedVpcId"),
            "subnet_id": self.access.get("AllowedSubnetId"),
        }
        if allowed != {"vpc_id": net["vpc_id"], "subnet_id": net["subnet_id"]}:
            raise Refused(
                f"network: the request uses {net['vpc_id']}/{net['subnet_id']}; the access stack allows {allowed}"
            )
        checks["network_matches_access_stack"] = True
        # quota and capacity: one validation at a time within 4 vCPU of G/VT
        quota = float(
            self.aws.json("service-quotas", "get-service-quota", "--service-code", "ec2", "--quota-code", G_QUOTA_CODE)[
                "Quota"
            ]["Value"]
        )
        res = self.aws.json(
            "ec2",
            "describe-instances",
            "--filters",
            "Name=instance-state-name,Values=pending,running,stopping",
            "Name=instance-type,Values=g*,vt*",
        )
        used = [
            {"id": i["InstanceId"], "type": i["InstanceType"], "state": i["State"]["Name"]}
            for r in res.get("Reservations", [])
            for i in r.get("Instances", [])
        ]
        vcpu_used = sum(
            int(i.get("CpuOptions", {}).get("CoreCount", 2)) * int(i.get("CpuOptions", {}).get("ThreadsPerCore", 2))
            for r in res.get("Reservations", [])
            for i in r.get("Instances", [])
        )
        pre["quota"] = {"g_vt_vcpu_quota": quota, "vcpu_in_use": vcpu_used, "instances_in_use": used}
        if quota - vcpu_used < target["vcpu"]:
            raise Refused(
                f"quota: {quota:g} vCPU of G/VT with {vcpu_used} in use ({used}); {target['instance_type']} needs "
                f"{target['vcpu']}: wait for the other run or raise the quota"
            )
        checks["quota"] = True
        offers = self.aws.json(
            "ec2",
            "describe-instance-type-offerings",
            "--location-type",
            "availability-zone",
            "--filters",
            f"Name=instance-type,Values={target['instance_type']}",
            f"Name=location,Values={net['availability_zone']}",
        ).get("InstanceTypeOfferings", [])
        if not offers:
            raise Refused(f"{target['instance_type']} is not offered in {net['availability_zone']}")
        checks["instance_type_offered"] = True
        # network: subnet in the VPC and AZ, with an outbound route (bootstrap downloads 28.6 GB over HTTPS)
        subnets = self.aws.json("ec2", "describe-subnets", "--subnet-ids", net["subnet_id"]).get("Subnets", [])
        if not subnets:
            raise Refused(f"subnet {net['subnet_id']} not found")
        sn = subnets[0]
        if sn.get("VpcId") != net["vpc_id"] or sn.get("AvailabilityZone") != net["availability_zone"]:
            raise Refused(
                f"subnet {net['subnet_id']} is in {sn.get('VpcId')}/{sn.get('AvailabilityZone')}, the request says "
                f"{net['vpc_id']}/{net['availability_zone']}"
            )
        rts = self.aws.json(
            "ec2", "describe-route-tables", "--filters", f"Name=association.subnet-id,Values={net['subnet_id']}"
        ).get("RouteTables", [])
        if not rts:
            rts = self.aws.json(
                "ec2",
                "describe-route-tables",
                "--filters",
                f"Name=vpc-id,Values={net['vpc_id']}",
                "Name=association.main,Values=true",
            ).get("RouteTables", [])
        default = [
            r
            for t in rts
            for r in t.get("Routes", [])
            if r.get("DestinationCidrBlock") == "0.0.0.0/0" and r.get("State", "active") == "active"
        ]
        via = "igw" if any(str(r.get("GatewayId", "")).startswith("igw-") for r in default) else ""
        via = via or ("nat" if any(r.get("NatGatewayId") for r in default) else "")
        pre["network"] = {**net, "outbound": via or "none"}
        if not via:
            raise Refused(f"subnet {net['subnet_id']}: no active 0.0.0.0/0 route (bootstrap needs outbound HTTPS)")
        if via == "igw" and not net["assign_public_ip"]:
            raise Refused("subnet routes through an internet gateway: assign_public_ip must be true for egress")
        checks["network"] = True
        # AMI pinned or resolved now (recorded): the DLAMI with the NVIDIA driver
        ami = self.req["ami"]
        if ami == "resolve-latest":
            ami = self.aws.json("ssm", "get-parameter", "--name", AMI_PARAM)["Parameter"]["Value"]
        images = self.aws.json("ec2", "describe-images", "--image-ids", ami).get("Images", [])
        img = images[0] if images else {}
        pre["ami"] = {k: img.get(k) for k in ("ImageId", "Name", "OwnerAlias", "State", "Architecture", "CreationDate")}
        if not (
            img.get("State") == "available"
            and img.get("OwnerAlias") == "amazon"
            and AMI_NAME in str(img.get("Name"))
            and img.get("Architecture") == "x86_64"
        ):
            raise Refused(f"AMI {ami} is not an available Amazon '{AMI_NAME}' x86_64 image: {pre['ami']}")
        self.record["ami"] = ami
        checks["ami"] = True
        # the stack, as the request says
        stack = self.aws.stack(self.stack_name)
        mode = req["stack"]["mode"]
        if mode == "create" and stack:
            raise Refused(f"stack {self.stack_name} exists ({stack['StackStatus']}); use a new name or mode reuse")
        if mode == "reuse":
            if not stack or stack.get("StackStatus") not in ("CREATE_COMPLETE", "UPDATE_COMPLETE"):
                raise Refused(f"stack {self.stack_name} does not exist or is not complete: nothing to reuse")
            tags = {t["Key"]: t["Value"] for t in stack.get("Tags", [])}
            if tags.get("cce:template-sha256") != pre["template_sha256"]:
                raise Refused("stack was created from another cloudformation.yaml: reuse needs the same template")
            iid = self.aws.outputs(stack).get("InstanceId", "")
            state = self.aws.instance_state(iid)
            if state != "stopped":
                raise Refused(f"reused instance {iid} must be stopped (found {state})")
            if self.aws.get_param(self.key_param) is None:
                raise Refused(f"API key parameter {self.key_param} missing for the reused stack")
        checks["stack"] = mode
        # cost ceiling and the tools of the runner
        pre["worst_case_instance_usd"] = worst_case_usd(req)
        if shutil.which("session-manager-plugin") is None:
            raise Refused("session-manager-plugin is not installed on the runner (needed for the SSM tunnel)")
        boto = subprocess.run([self.python, "-c", "import boto3"], capture_output=True, text=True)
        if boto.returncode != 0:
            raise Refused(f"the controller interpreter {self.python} has no boto3 (uv sync --extra aws)")
        checks["runner_tools"] = True
        if req.get("parity"):
            self.parity_pkg = self.fetch_parity()
            pre["parity"] = {
                "status": "configured",
                "s3_uri": req["parity"]["s3_uri"],
                "sha256": req["parity"]["sha256"],
            }
        else:
            pre["parity"] = {"status": "not_configured"}
        return pre

    def fetch_parity(self) -> Path:
        """The optional results ZIP (outside Git), verified by SHA-256 and unpacked on the runner, outside the
        evidence. A mismatch refuses the run before anything is created."""
        par = self.req["parity"]
        dest = self.workdir / "parity"
        dest.mkdir(parents=True, exist_ok=True)
        zpath = dest / "results.zip"
        self.aws.raw("s3", "cp", par["s3_uri"], str(zpath), "--only-show-errors", timeout=1800)
        got = sha256_file(zpath)
        if got != par["sha256"]:
            raise Refused(f"parity ZIP sha256 {got} does not match the request")
        out = dest / "unpacked"
        with zipfile.ZipFile(zpath) as zf:
            for info in zf.infolist():
                target = (out / info.filename).resolve()
                if out.resolve() not in target.parents and target != out.resolve():
                    raise Refused(f"parity ZIP: unsafe path {info.filename!r}")
            zf.extractall(out)  # noqa: S202 - every member path was checked above
        for cand in [out, *[p for p in out.rglob("*") if p.is_dir()]]:
            if (cand / "CAWbase_benchmark_v1.1").is_dir() and (cand / "corrida_nocturna").is_dir():
                return cand
        raise Refused("parity ZIP: no directory with CAWbase_benchmark_v1.1/ and corrida_nocturna/")

    # --- claim ---

    def claim(self) -> None:
        ctx = {
            "id": self.id,
            "stack": self.stack_name,
            "repository": os.environ.get("GITHUB_REPOSITORY", ""),
            "sha": os.environ.get("GITHUB_SHA", ""),
            "run_id": os.environ.get("GITHUB_RUN_ID", f"local-{int(self.t0)}"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", "1"),
            "claimed_at": iso(self.t0),
            "status": "claimed",
        }
        self.record["run_id"] = ctx["run_id"]
        path = self.workdir / "ledger.json"
        path.write_text(json.dumps(ctx, separators=(",", ":")), encoding="utf-8")
        try:
            self.aws.put_param(self.ledger, path, overwrite=False)  # atomic: fails if the id was ever claimed
        except AwsError as exc:
            if exc.code == "ParameterAlreadyExists":
                raise Duplicate(self.ledger) from exc
            raise
        save(self.ev, "claim.json", ctx)
        log(f"request {self.id} claimed ({self.ledger})")

    # --- A. provisioning: stack, watchdog, bootstrap, first load, instance stopped ---

    def api_key(self) -> None:
        self.key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.req["stack"]["mode"] == "reuse":
            value = self.aws.get_param(self.key_param, decrypt=True) or ""
        else:
            value = secrets.token_hex(32)
        fd = os.open(self.key_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as fh:
            fh.write(value + "\n")
        if self.req["stack"]["mode"] == "create":
            self.aws.put_param(self.key_param, self.key_file, secure=True, overwrite=True)  # never printed
        self.record["api_key_parameter"] = self.key_param

    def create_stack(self, pre: dict[str, Any]) -> None:
        net = self.req["network"]
        params = {
            "VpcId": net["vpc_id"],
            "SubnetId": net["subnet_id"],
            "AvailabilityZone": net["availability_zone"],
            "AssignPublicIp": "true" if net["assign_public_ip"] else "false",
            "ClientSecurityGroupId": net["client_security_group_id"],
            "ClientCidr": net["client_cidr"],
            "ImageId": self.record["ami"],
            "InstanceType": self.req["aws"]["instance_type"],
            "ServerProfile": self.req["validation"].get("server_profile", "reference-8k-1slot"),
            "ApiKeyParameterName": self.key_param,
            "HostPermissionsBoundaryArn": self.access["HostPermissionsBoundaryArn"],
        }
        tags = {
            "cce:validation-id": self.id,
            "cce:template-sha256": pre["template_sha256"],
            "cce:after-run": self.req["stack"]["after_run"],
        }
        self.record["stack_parameters"] = params
        self.aws.raw(
            "cloudformation",
            "create-stack",
            "--stack-name",
            self.stack_name,
            "--template-body",
            f"file://{TEMPLATE}",
            "--capabilities",
            "CAPABILITY_IAM",
            "--role-arn",
            self.access["CfnExecutionRoleArn"],
            "--on-failure",
            "DELETE",
            "--parameters",
            json.dumps([{"ParameterKey": k, "ParameterValue": v} for k, v in params.items()]),
            "--tags",
            json.dumps([{"Key": k, "Value": v} for k, v in tags.items()]),
        )
        self.record["timeline"]["create_stack"] = now()
        log(f"stack {self.stack_name} requested")

    def stack_failed(self, stack: dict[str, Any] | None) -> None:
        status = str(stack.get("StackStatus")) if stack else "DELETE_COMPLETE"
        if stack is None or "ROLLBACK" in status or "FAILED" in status or status.startswith("DELETE"):
            try:
                events = self.aws.json("cloudformation", "describe-stack-events", "--stack-name", self.stack_name)
                failed = [
                    {k: e.get(k) for k in ("LogicalResourceId", "ResourceStatus", "ResourceStatusReason")}
                    for e in events.get("StackEvents", [])
                    if "FAILED" in str(e.get("ResourceStatus"))
                ]
            except AwsError:
                failed = []
            self.record["stack_failure"] = {"status": status, "failed_events": failed[:20]}
            raise ValidationFailed(f"stack {self.stack_name}: {status} {failed[:3]}")

    def host_id(self) -> str | None:
        try:
            res = self.aws.json(
                "cloudformation",
                "describe-stack-resource",
                "--stack-name",
                self.stack_name,
                "--logical-resource-id",
                "Host",
            )
        except AwsError:
            self.stack_failed(self.aws.stack(self.stack_name))
            return None
        pid = str(res.get("StackResourceDetail", {}).get("PhysicalResourceId") or "")
        return pid if pid.startswith("i-") else None

    def arm_watchdog(self) -> None:
        """A recurring stop of THIS instance, run by EventBridge Scheduler in AWS from the job's time limit on: it
        does not depend on the runner. The cleanup deletes it only after it verified the instance stopped."""
        assert self.iid
        start = self.deadline + WATCHDOG_GRACE_MIN * 60
        end = start + WATCHDOG_WINDOW_H * 3600
        target = {
            "Arn": "arn:aws:scheduler:::aws-sdk:ec2:stopInstances",
            "RoleArn": self.access["WatchdogRoleArn"],
            "Input": json.dumps({"InstanceIds": [self.iid]}),
            "RetryPolicy": {"MaximumRetryAttempts": 5, "MaximumEventAgeInSeconds": 3600},
        }
        self.aws.raw(
            "scheduler",
            "create-schedule",
            "--name",
            self.schedule,
            "--group-name",
            self.access["WatchdogScheduleGroup"],
            "--schedule-expression",
            f"rate({WATCHDOG_RATE_MIN} minutes)",
            "--schedule-expression-timezone",
            "UTC",
            "--start-date",
            iso(start),
            "--end-date",
            iso(end),
            "--flexible-time-window",
            json.dumps({"Mode": "OFF"}),
            "--target",
            json.dumps(target),
            "--action-after-completion",
            "DELETE",
        )
        wd: dict[str, Any] = {"name": self.schedule, "group": self.access["WatchdogScheduleGroup"]}
        wd |= {
            "instance_id": self.iid,
            "first_stop_at": iso(start),
            "until": iso(end),
            "every_minutes": WATCHDOG_RATE_MIN,
        }
        self.record["watchdog"] = wd
        save(self.ev, "watchdog.json", wd)
        log(f"watchdog armed in AWS: stop {self.iid} every {WATCHDOG_RATE_MIN} min from {iso(start)}")

    def bootstrap_done(self) -> bool:
        text = self.on_host(self.host_cmd("BOOTSTRAP"), timeout=60)
        (self.ev / "bootstrap.log").write_text(text, encoding="utf-8")
        if BOOT_DONE in text:
            return True
        status = self.on_host(self.host_cmd("CLOUD_INIT"), timeout=60)
        if re.search(r"status:\s*(error|degraded)", status):
            (self.ev / "cloud-init-status.txt").write_text(status, encoding="utf-8")
            raise ValidationFailed(f"bootstrap failed on the host: {status.strip()[-300:]}")
        return False

    def server_healthy(self) -> bool:
        service = self.on_host(self.host_cmd("SERVICE"), timeout=60).strip()
        if service in ("failed",):
            raise ValidationFailed("cce-inference.service failed after the bootstrap")
        return service == "active" and self.on_host(self.host_cmd("HEALTH"), timeout=60).strip() == "200"

    def provision(self, pre: dict[str, Any]) -> None:
        tl: dict[str, float] = {}
        self.record["timeline"] = tl
        boot_timeout = self.req["limits"]["bootstrap_timeout_minutes"] * 60
        self.api_key()
        if self.req["stack"]["mode"] == "reuse":
            stack = self.aws.stack(self.stack_name)
            assert stack
            self.iid = self.aws.outputs(stack)["InstanceId"]
            self.arm_watchdog()
            save(self.ev, "provision.json", self.record)
            return
        self.create_stack(pre)
        self.ledger_update(status="provisioning")
        self.iid = wait_for("the instance of the stack", self.host_id, 1200, deadline=self.deadline)
        tl["instance_created"] = now()
        self.arm_watchdog()  # before waiting for anything else on the instance

        def complete() -> bool:
            stack = self.aws.stack(self.stack_name)
            if stack and stack.get("StackStatus") == "CREATE_COMPLETE":
                self.record["outputs"] = self.aws.outputs(stack)
                return True
            if not stack or not str(stack.get("StackStatus")).endswith("_IN_PROGRESS"):
                self.stack_failed(stack)
            return False

        wait_for("CREATE_COMPLETE", complete, 1800, deadline=self.deadline)
        tl["stack_complete"] = now()

        def ssm_online() -> bool:
            info = self.aws.json(
                "ssm", "describe-instance-information", "--filters", f"Key=InstanceIds,Values={self.iid}"
            ).get("InstanceInformationList", [])
            return bool(info) and info[0].get("PingStatus") == "Online"

        wait_for("SSM agent online", ssm_online, boot_timeout, deadline=self.deadline)
        tl["ssm_online"] = now()
        wait_for("bootstrap (download, SHA-256, units)", self.bootstrap_done, boot_timeout, deadline=self.deadline)
        tl["bootstrap_done"] = now()
        wait_for("first load of the model (/health 200)", self.server_healthy, boot_timeout, deadline=self.deadline)
        tl["first_load_healthy"] = now()
        (self.ev / "cloud-init-status.txt").write_text(self.on_host(self.host_cmd("CLOUD_INIT")), encoding="utf-8")
        journal = self.on_host(os.environ.get("CCE_SERVER_LOG_CMD", SERVER_LOG_CMD), timeout=120)
        (self.ev / "bootstrap-server-journal.log").write_text(journal, encoding="utf-8")
        offload = gpu_offload(journal)
        save(self.ev, "bootstrap-gpu-offload.json", offload)
        self.record["first_load_gpu_offload"] = offload
        if not offload["all_layers_on_gpu"]:
            raise ValidationFailed(f"first load: not every layer on the GPU: {offload}")
        # the validation starts from `stopped` (gpu-validation.py refuses anything else)
        self.aws.raw("ec2", "stop-instances", "--instance-ids", self.iid)
        wait_for("instance stopped", lambda: self.aws.instance_state(str(self.iid)) == "stopped", 900)
        tl["stopped_for_validation"] = now()
        save(self.ev, "provision.json", self.record)
        log(f"step A done: {self.iid} bootstrapped, first load verified on the GPU, instance stopped")

    # --- B. validation through gpu-validation.py ---

    def controller_config(self) -> Path:
        assert self.iid
        tpl = Path(os.environ.get("CCE_CI_CONTROLLER_TEMPLATE", str(CONTROLLER_EXAMPLE))).read_text(encoding="utf-8")
        state = self.workdir / f"controller-state-{self.id}.db"  # fresh per request
        values = {
            "db_path": str(state),
            "base_url": f"http://127.0.0.1:{self.tunnel_port}",
            "api_key_file": str(self.key_file),
            "instance_id": self.iid,
            "region": REGION,
            "profile": "",  # default chain: the job's temporary OIDC credentials
        }
        out = []
        for line in tpl.splitlines():
            key = line.split("=", 1)[0].strip()
            out.append(f"{key} = {json.dumps(values[key])}" if "=" in line and key in values else line)
        cfg = self.workdir / "controller.toml"
        cfg.write_text("\n".join(out) + "\n", encoding="utf-8")
        shutil.copy(cfg, self.ev / "controller.toml")  # paths only, no secret
        return cfg

    tunnel_port = 18080

    def gpu_validation(self) -> int:
        cfg = self.controller_config()
        val = self.req["validation"]
        out = self.ev / "gpu-validation"
        argv = [
            self.python,
            str(GPU_VALIDATION),
            "--config",
            str(cfg),
            "--instance-id",
            str(self.iid),
            "--key-file",
            str(self.key_file),
            "--evidence",
            str(out),
            "--tunnel-port",
            str(self.tunnel_port),
            "--base-url",
            f"http://127.0.0.1:{self.tunnel_port}",
            "--profiles",
            " ".join(val.get("profiles", DEFAULT_PROFILES)),
        ]
        if "requests" in val:
            argv += ["--requests", str(val["requests"])]
        if "prompt_tokens" in val:
            argv += ["--prompt-tokens", str(val["prompt_tokens"])]
        if self.parity_pkg:
            argv += ["--parity-pkg", str(self.parity_pkg)]
        budget = self.deadline - now() - CLEANUP_RESERVE_S
        if budget < 600:
            raise ValidationFailed(f"no time left for the validation ({budget:.0f}s within max_runtime_minutes)")
        env = {**self.aws.env, "CCE_PYTHON": self.python, "CCE_AWS_BIN": self.aws.bin}
        log(f"step B: gpu-validation.py (budget {budget / 60:.0f} min)")
        with open(self.ev / "gpu-validation.log", "wb") as fh:
            p = subprocess.Popen(argv, stdout=fh, stderr=subprocess.STDOUT, env=env)
            try:
                return p.wait(timeout=budget)
            except subprocess.TimeoutExpired:
                log("gpu-validation.py exceeded its budget: SIGTERM (its own cleanup stops the instance)")
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(timeout=900)
                except subprocess.TimeoutExpired:
                    p.kill()
                return 124
            except Interrupted:
                p.send_signal(signal.SIGTERM)
                try:
                    p.wait(timeout=600)
                except subprocess.TimeoutExpired:
                    p.kill()
                raise

    def parity_status(self) -> dict[str, Any]:
        if not self.req.get("parity"):
            return {
                "status": "pending",
                "reason": "parity ZIP not configured in the request: Q8_0 quality with the reference methodology "
                "(AC-A09-1-19 quality, AC-A09-1-26) stays pending; the run gives the rest of the evidence",
            }
        dirs = sorted((self.ev / "gpu-validation").glob("parity-*"))
        summaries = {
            d.name: json.loads((d / "summary.json").read_text()) for d in dirs if (d / "summary.json").exists()
        }
        return {"status": "ran" if summaries else "not_run", "summaries": summaries}

    # --- the whole run ---

    def main(self) -> int:
        self.ev.mkdir(parents=True, exist_ok=True)
        self.workdir.mkdir(parents=True, exist_ok=True)
        save(self.ev, "request.json", self.req)
        outcome: dict[str, Any] = {"id": self.id, "stack": self.stack_name, "after_run": self.req["stack"]["after_run"]}
        try:
            pre = self.preflight()
        except Duplicate:
            dup = json.loads(self.aws.get_param(self.ledger) or "{}")
            save(self.ev, "duplicate.json", {"ledger": self.ledger, "claim": dup})
            outcome |= {"validation": "not_run_duplicate", "exit": 3, "claimed_by": dup}
            save(self.ev, "outcome.json", outcome)
            print(
                f"DUPLICATE: request {self.id} was already claimed by run {dup.get('run_id')} attempt "
                f"{dup.get('run_attempt')} (status {dup.get('status')}): no new spend. Submit a new id to run again.",
                file=sys.stderr,
            )
            return 3
        except (Refused, AwsError) as exc:
            save(self.ev, "preflight.json", {"refused": str(exc)})
            outcome |= {"validation": "refused", "exit": 2, "reason": str(exc)}
            save(self.ev, "outcome.json", outcome)
            print(f"REFUSED (nothing claimed or created): {exc}", file=sys.stderr)
            return 2
        save(self.ev, "preflight.json", pre)
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(sig, _raise_interrupted)
        code = 1
        try:
            self.claim()
            self.provision(pre)
            self.ledger_update(status="validating", instance_id=self.iid)
            rc = self.gpu_validation()
            code = {0: 0, 130: 130}.get(rc, 1)
            outcome["gpu_validation_exit"] = rc
            outcome["validation"] = {0: "validated", 130: "interrupted"}.get(rc, "failed")
        except Duplicate:
            outcome |= {"validation": "not_run_duplicate"}
            code = 3
        except Interrupted as exc:
            outcome["validation"] = f"interrupted: {exc}"
            code = 130
        except Exception as exc:  # noqa: BLE001 - recorded; the cleanup below still runs
            outcome["validation"] = f"failed: {exc}"
            print(f"FAILED: {exc}", file=sys.stderr)
            code = 1
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, signal.SIG_IGN)  # never abort the cleanup half-way
            save(self.ev, "provision.json", self.record)
            if code != 3:
                cleaned = Cleanup(self.req, self.ev, aws=self.aws).main(name="cleanup-run.json")
                outcome["cleanup_in_run"] = "verified" if cleaned == 0 else "FAILED"
            outcome["parity"] = self.parity_status()
            summary = self.ev / "gpu-validation" / "summary.json"
            if summary.exists():
                gv = json.loads(summary.read_text())
                outcome["gpu_validation"] = {
                    k: gv.get(k) for k in ("outcome", "exit", "verdicts", "escalation", "profile", "final")
                }
                outcome["gpu_offload"] = gv.get("steps", {}).get("gpu_offload")
            outcome["exit"] = code
            if code != 3:
                self.ledger_update(status=str(outcome.get("validation"))[:200], cleanup=outcome.get("cleanup_in_run"))
            withhold(self.ev, self.workdir / "withheld")
            save(self.ev, "outcome.json", outcome)
            redact(self.ev, [self.key_file])
        log(f"validation: {outcome.get('validation')}; in-run cleanup: {outcome.get('cleanup_in_run')}")
        return code


# --- cleanup (idempotent; also the separate cleanup job) -------------------------------------------------------------


class Cleanup:
    def __init__(self, req: dict[str, Any], ev: Path, aws: Aws | None = None) -> None:
        self.req = req
        self.ev = ev
        self.aws = aws or Aws()
        self.stack_name: str = req["stack"]["name"]
        self.after: str = req["stack"]["after_run"]
        self.key_param = f"{API_KEY_PREFIX}{self.stack_name}/api-key"
        self.schedule = f"cce-a09-{req['id']}"
        self.ledger = LEDGER_PREFIX + req["id"]

    def stop_instances(self, rec: dict[str, Any], errors: list[str]) -> bool:
        """Stop every non-terminated instance of the stack and verify it; True when none is left running."""
        instances = self.aws.stack_instances(self.stack_name)
        rec["instances"] = {i["InstanceId"]: i["State"]["Name"] for i in instances}
        for i in instances:
            iid, state = i["InstanceId"], i["State"]["Name"]
            if state in ("pending", "running"):
                try:
                    self.aws.raw("ec2", "stop-instances", "--instance-ids", iid)
                    rec.setdefault("stopped_by_cleanup", []).append(iid)
                except AwsError as exc:
                    errors.append(f"stop_instances_failed {iid}: {exc}")
        ok = True
        for i in instances:
            iid = i["InstanceId"]
            try:
                final = wait_for(
                    f"{iid} stopped",
                    lambda iid=iid: (s := self.aws.instance_state(iid)) in ("stopped", "terminated", "absent") and s,
                    900,
                )
            except ValidationFailed:
                final = self.aws.instance_state(iid)
                errors.append(f"instance_not_stopped {iid}: {final}")
                ok = False
            rec["instances"][iid] = final
        return ok

    def delete_stack(self, rec: dict[str, Any], errors: list[str]) -> bool:
        for attempt in (1, 2):
            stack = self.aws.stack(self.stack_name)
            if stack is None:
                break
            if not str(stack.get("StackStatus")).startswith("DELETE_IN_PROGRESS"):
                try:
                    self.aws.raw("cloudformation", "delete-stack", "--stack-name", self.stack_name)
                except AwsError as exc:
                    errors.append(f"delete_stack_failed: {exc}")
                    return False

            def gone() -> str | bool:
                s = self.aws.stack(self.stack_name)
                if s is None:
                    return "deleted"
                status = str(s.get("StackStatus"))
                return "failed" if status == "DELETE_FAILED" else False

            try:
                result = wait_for("stack deleted", gone, 2400)
            except ValidationFailed as exc:
                errors.append(str(exc))
                return False
            if result == "deleted":
                break
            rec.setdefault("delete_failed_attempts", []).append(attempt)
        stack = self.aws.stack(self.stack_name)
        rec["stack_status"] = stack.get("StackStatus") if stack else "DELETE_COMPLETE"
        if stack is not None:
            errors.append(f"stack_not_deleted: {rec['stack_status']}")
            return False
        return True

    def main(self, name: str = "cleanup.json") -> int:
        self.ev.mkdir(parents=True, exist_ok=True)
        rec: dict[str, Any] = {"id": self.req["id"], "stack": self.stack_name, "after_run": self.after}
        errors: list[str] = []
        try:
            stack = self.aws.stack(self.stack_name)
            rec["stack_status_before"] = stack.get("StackStatus") if stack else "absent"
            stopped = self.stop_instances(rec, errors)
            rec["instances_stopped"] = stopped
            if self.after == "delete":
                deleted = self.delete_stack(rec, errors) if stack else True
                rec["stack_deleted"] = deleted
                left = [i["InstanceId"] for i in self.aws.stack_instances(self.stack_name)]
                vols = [v["VolumeId"] for v in self.aws.stack_volumes(self.stack_name)]
                rec["instances_left"], rec["volumes_left"] = left, vols
                if left or vols:
                    errors.append(f"resources_left: instances {left}, volumes {vols}")
                if deleted:
                    rec["api_key_parameter_deleted"] = self.aws.delete_param(self.key_param)
                ok = stopped and deleted and not left and not vols
            else:
                after = self.aws.stack(self.stack_name)
                rec["stack_status"] = after.get("StackStatus") if after else "absent"
                if after and str(after.get("StackStatus")).endswith("_IN_PROGRESS"):
                    errors.append(f"stack_in_progress: {after.get('StackStatus')}")
                rec["kept"] = bool(after) and stopped
                ok = stopped and not (after and str(after.get("StackStatus")).endswith("_IN_PROGRESS"))
                if after:
                    rec["residual"] = "EBS of the stopped stack (root + weights) until it is deleted"
            # the watchdog stays armed unless the instance is verified stopped (it is the backstop)
            if ok:
                rec["watchdog"] = self.disarm()
            else:
                rec["watchdog"] = "kept_armed"
        except Exception as exc:  # noqa: BLE001 - a cleanup error is reported, never hidden
            errors.append(f"cleanup_error: {exc}")
            ok = False
        rec["errors"] = errors
        rec["outcome"] = "verified" if ok and not errors else "FAILED"
        try:
            save(self.ev, name, rec)
        except OSError:
            pass
        if rec["outcome"] != "verified":
            print(f"CLEANUP FAILED for {self.stack_name}: {errors}", file=sys.stderr)
            return 5
        log(f"cleanup verified ({self.after}): {json.dumps({k: rec.get(k) for k in ('instances', 'stack_status')})}")
        return 0

    def disarm(self) -> str:
        group = None
        try:
            access = self.aws.stack(os.environ.get("CCE_A09_ACCESS_STACK") or ACCESS_STACK_DEFAULT)
            group = self.aws.outputs(access).get("WatchdogScheduleGroup") if access else None
        except AwsError:
            group = None
        if not group:
            return "unknown_group"
        try:
            self.aws.raw("scheduler", "delete-schedule", "--name", self.schedule, "--group-name", group)
            return "disarmed"
        except AwsError as exc:
            return "absent" if exc.code == "ResourceNotFoundException" else f"disarm_failed: {exc}"

    def update_ledger(self, outcome: str) -> None:
        try:
            current = json.loads(self.aws.get_param(self.ledger) or "null")
            if not isinstance(current, dict):
                return
            current.update(cleanup_job=outcome, updated_at=iso(now()))
            tmp = self.ev / ".ledger.json"
            tmp.write_text(json.dumps(current, separators=(",", ":")), encoding="utf-8")
            self.aws.put_param(self.ledger, tmp, overwrite=True)
            tmp.unlink()
        except Exception as exc:  # noqa: BLE001
            log(f"ledger update failed: {exc}")


# --- evidence: GPU offload, withheld outputs, redaction, summary ------------------------------------------------------


def gpu_offload(text: str) -> dict[str, Any]:
    """Every model load in a server log must say `offloaded N/N layers to GPU` (one definition: gpu-validation.py)."""
    spec = importlib.util.spec_from_file_location("cce_gpu_validation", GPU_VALIDATION)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    result: dict[str, Any] = mod.gpu_offload(text)
    return result


def withhold(ev: Path, dest: Path) -> list[dict[str, str]]:
    """Move outputs that reproduce the private corpus (parity predictions, proxy log) out of the evidence; their
    SHA-256 stays in the evidence."""
    moved = []
    for f in sorted(p for p in ev.rglob("*") if p.is_file()):
        rel = f.relative_to(ev).as_posix()
        if WITHHELD.search(rel):
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            moved.append({"file": rel, "sha256": sha256_file(f)})
            shutil.move(str(f), target)
    if moved:
        save(
            ev,
            "withheld.json",
            {"reason": "parity outputs reproduce the private corpus; kept off GitHub", "files": moved},
        )
    return moved


SECRET_ENV = (
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_RUNTIME_TOKEN",
    "GITHUB_TOKEN",
)
SECRET_NAMES = re.compile(r"(^|/)(api-key[^/]*|[^/]*\.pem|credentials[^/]*)$")


def redact(ev: Path, secret_files: list[Path]) -> int:
    """Replace every known secret value in the evidence (same length in binary files), drop files whose name says
    secret, then verify nothing is left. Returns the number of replacements; raises if a secret remains."""
    values = [os.environ[k] for k in SECRET_ENV if len(os.environ.get(k, "")) >= 8]
    for f in secret_files:
        try:
            values += [ln.strip() for ln in f.read_text(encoding="utf-8").splitlines() if len(ln.strip()) >= 8]
        except OSError:
            pass
    needles = sorted({v.encode() for v in values}, key=len, reverse=True)
    count = 0
    for f in sorted(p for p in ev.rglob("*") if p.is_file()):
        if SECRET_NAMES.search(f.relative_to(ev).as_posix()):
            f.unlink()
            count += 1
            continue
        data = f.read_bytes()
        new = data
        for n in needles:
            new = new.replace(n, b"*" * len(n))
        if new != data:
            count += sum(data.count(n) for n in needles)
            f.write_bytes(new)
    for f in (p for p in ev.rglob("*") if p.is_file()):
        data = f.read_bytes()
        if any(n in data for n in needles):
            f.unlink()
            raise RuntimeError(f"secret still present in {f}; file removed from the evidence")
    return count


def summary_markdown(dirs: list[Path]) -> str:
    lines = ["### A09-1 AWS GPU validation"]
    for d in dirs:
        for name in ("outcome.json", "cleanup.json"):
            p = d / name
            if not p.exists():
                continue
            doc = json.loads(p.read_text())
            if name == "outcome.json":
                lines.append(f"- **Validation**: `{doc.get('validation')}` (exit {doc.get('exit')})")
                lines.append(f"- In-run cleanup: `{doc.get('cleanup_in_run')}`; after run: `{doc.get('after_run')}`")
                offload = doc.get("gpu_offload") or {}
                lines.append(f"- GPU offload: `{offload.get('status')}` {offload.get('offloaded')}")
                lines.append(f"- Parity: `{(doc.get('parity') or {}).get('status')}`")
                for prof, v in ((doc.get("gpu_validation") or {}).get("verdicts") or {}).items():
                    lines.append(f"  - `{prof}`: `{v.get('verdict')}` {v.get('criteria')}")
            else:
                lines.append(f"- **Cleanup job**: `{doc.get('outcome')}` errors={doc.get('errors')}")
                lines.append(f"  - instances {doc.get('instances')}; stack `{doc.get('stack_status')}`")
    return "\n".join(lines) + "\n"


# --- CLI -----------------------------------------------------------------------------------------------------------


def _write_outputs(path: str | None, values: dict[str, str]) -> None:
    text = "".join(f"{k}={v}\n" for k, v in values.items())
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)
    print(text, end="")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("files", nargs="*")
    d = sub.add_parser("detect")
    d.add_argument("--before", required=True)
    d.add_argument("--after", required=True)
    d.add_argument("--github-output")
    sub.add_parser("oidc-claims")
    r = sub.add_parser("run")
    r.add_argument("--request", required=True)
    r.add_argument("--evidence", required=True)
    r.add_argument("--workdir", required=True)
    r.add_argument("--python", default=sys.executable, help="controller interpreter (with boto3)")
    r.add_argument("--tunnel-port", type=int, default=18080)
    k = sub.add_parser("cleanup")
    k.add_argument("--request", required=True)
    k.add_argument("--evidence", required=True)
    x = sub.add_parser("redact")
    x.add_argument("evidence")
    x.add_argument("--secret-file", action="append", default=[])
    s = sub.add_parser("summary")
    s.add_argument("dirs", nargs="+")
    a = ap.parse_args(argv)

    if a.cmd == "check":
        files = [Path(f) for f in a.files] or sorted(REQUESTS_DIR.glob("*.json"))
        errors = check_unique_ids(REQUESTS_DIR)
        for f in files:
            try:
                load_request(f)
            except RequestError as exc:
                errors.append(str(exc))
        for err in errors:
            print(f"INVALID: {err}", file=sys.stderr)
        print(f"{len(files)} request(s) checked, {len(errors)} error(s)")
        return 2 if errors else 0
    if a.cmd == "detect":
        try:
            path, notes = detect(a.before, a.after)
            for n in notes:
                print(f"note: {n}", file=sys.stderr)
            if path is None:
                _write_outputs(a.github_output, {"request": ""})
                print("no new validation request in this push: nothing to run")
                return 0
            dup = check_unique_ids(REQUESTS_DIR)
            if dup:
                raise RequestError("; ".join(dup))
            req = load_request(path)
        except RequestError as exc:
            print(f"INVALID REQUEST (nothing is run): {exc}", file=sys.stderr)
            return 2
        _write_outputs(a.github_output, detect_outputs(req, path))
        return 0
    if a.cmd == "oidc-claims":
        claims = oidc_claims()
        text = json.dumps(claims, indent=2)
        print(text)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
                fh.write(f"### OIDC claims (safe subset, never the token)\n\n```json\n{text}\n```\n")
        return 0
    if a.cmd == "run":
        try:
            req = load_request(Path(a.request))
        except RequestError as exc:
            print(f"INVALID REQUEST: {exc}", file=sys.stderr)
            return 2
        run = Run(req, Path(a.evidence), Path(a.workdir), a.python)
        run.tunnel_port = a.tunnel_port
        return run.main()
    if a.cmd == "cleanup":
        req = load_request(Path(a.request))
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        cl = Cleanup(req, Path(a.evidence))
        rc = cl.main()
        cl.update_ledger("verified" if rc == 0 else "FAILED")
        redact(Path(a.evidence), [])
        return rc
    if a.cmd == "redact":
        count = redact(Path(a.evidence), [Path(f) for f in a.secret_file])
        print(f"redacted {count} occurrence(s)")
        return 0
    if a.cmd == "summary":
        text = summary_markdown([Path(x) for x in a.dirs])
        print(text)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
                fh.write(text)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
