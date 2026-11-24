#!/usr/bin/env python3
"""A09-1 validation on AWS run LOCALLY by an operator with their own AWS identity (standard library only; AWS through
the `aws` CLI, plus a boto3 identity probe in the controller interpreter). No GitHub, no OIDC, no CI identity.
Contract: docs/a09-1-aws-validation-automation.md. Entry point: deploy/inference/bin/a09-aws-validation.

    a09-aws-validation check [FILE...]                     offline validation of request files (no AWS)
    a09-aws-validation plan    --request F [--profile P]   read-only: identity, base stack, preflight of the run
    a09-aws-validation setup   --request F [--profile P] [--apply]
                                                           base stack (execution role, host boundary, watchdog):
                                                           plan by default; --apply creates or updates it
    a09-aws-validation run     --request F [--profile P] [--parity-zip ZIP]
                                                           the whole campaign: preflight, claim, step A, step B, cleanup
    a09-aws-validation cleanup --request F [--profile P]   the same cleanup, idempotent (after a lost process)

The request is a LOCAL file outside the checkout (it holds the account, the network and the budget); the evidence and
the work directory are local too (default: $XDG_STATE_HOME or ~/.local/state, `cce-inference/a09-validation/<id>/`).
AWS credentials come from the standard chain (environment, AWS_PROFILE, SSO, temporary credentials) or `--profile`;
the same identity and region (us-east-1) are passed to the AWS CLI, to boto3 and to every child process. Before any
write, STS must report the same identity through the CLI and through boto3, in the request's `aws.account_id`.

`run`: preflight with no side effects (identity and account, base stack, quota, network, AMI, configuration, spend
ceiling, optional parity ZIP) -> claim the id -> step A (stack with its AWS-side watchdog, bootstrap, SSM, first load
with every layer on the GPU, instance STOPPED) -> step B (bench/gpu-validation.py through an SSM tunnel) -> cleanup as
the request says (keep-stopped | delete) and its verification, also after a failure or an interruption.

Exit status of `run`: 0 validated; 1 validation failed or incomplete (also a failed provisioning); 2 refused before any
write (invalid request, identity, account, preflight: nothing claimed, nothing created); 3 request id already claimed
(no new spend); 130 interrupted; 5 CLEANUP FAILED (whatever the validation said: resources not verified). The
validation result and the cleanup result are printed and recorded apart (`outcome.json`, `cleanup-run.json`).
`cleanup`: 0 cleanup verified; 5 CLEANUP FAILED. `plan`/`setup`: 0 ready (or plan written); 2 refused; 1 setup not
verified.

Tool overrides (tests only): CCE_AWS_BIN, CCE_POLL_S, CCE_CI_CONTROLLER_TEMPLATE, CCE_CI_HOST_<NAME>_CMD,
CCE_SERVER_LOG_CMD, CCE_MANIFEST. Needs Python >= 3.11; the wrapper runs it in the controller environment (boto3).
"""

from __future__ import annotations

import argparse
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
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
DEPLOY = HERE.parent  # deploy/inference
REPO = DEPLOY.parent.parent
MANIFEST = DEPLOY / "manifest.json"
PROFILES_DIR = DEPLOY / "server" / "profiles"
TEMPLATE = DEPLOY / "aws" / "cloudformation.yaml"
BASE_TEMPLATE = DEPLOY / "aws" / "validation-base.yaml"
BUILD_TEMPLATE = DEPLOY / "aws" / "build_template.py"
GPU_VALIDATION = DEPLOY / "bench" / "gpu-validation.py"
SSM_RUN = DEPLOY / "bench" / "ssm-run.sh"
CONTROLLER_EXAMPLE = DEPLOY / "controller" / "controller.aws.example.toml"
EXAMPLE_REQUEST = HERE / "request.example.json"

REGION = "us-east-1"
BASE_STACK_DEFAULT = "cce-a09-validation-base"
BASE_OUTPUTS = ("CfnExecutionRoleArn", "WatchdogRoleArn", "WatchdogScheduleGroup", "HostPermissionsBoundaryArn")
LEDGER_PREFIX = "/cce/inference/validation-runs/"
API_KEY_PREFIX = "/cce/inference/validation/"
OWNER_PREFIX = "/cce/inference/validation-stacks/"  # which request owns each stack (created it, or adopted it by reuse)
AMI_PARAM = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id"
AMI_NAME = "Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 24.04)"
G_QUOTA_CODE = "L-DB2E81BA"  # Running On-Demand G and VT instances (vCPU)
WATCHDOG_GRACE_MIN = 15  # the watchdog starts this long after the run's time limit
WATCHDOG_RATE_MIN = 10
WATCHDOG_WINDOW_H = 3
CLEANUP_RESERVE_S = 25 * 60  # kept out of the gpu-validation budget for the in-run cleanup
BOOT_DONE = "units installed; server started"
DEFAULT_PROFILES = ["reference-8k-1slot", "candidate-16k-1slot", "candidate-16k-2slot"]
CREDENTIAL_ENV = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")

ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,30}[a-z0-9]$")
STACK_RE = re.compile(r"^cce-inference-[a-z0-9](?:[a-z0-9-]{0,34}[a-z0-9])?$")
VPC_RE = re.compile(r"^vpc-[0-9a-f]{8,17}$")
SUBNET_RE = re.compile(r"^subnet-[0-9a-f]{8,17}$")
SG_RE = re.compile(r"^sg-[0-9a-f]{8,17}$")
AMI_RE = re.compile(r"^ami-[0-9a-f]{8,17}$")
AZ_RE = re.compile(r"^us-east-1[a-f]$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ACCOUNT_RE = re.compile(r"^[0-9]{12}$")
BASE_STACK_RE = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# host-side commands (run as root through SSM Run Command); each is overridable for tests: CCE_CI_HOST_<NAME>_CMD
HOST_CMDS = {
    "BOOTSTRAP": "cat /var/lib/cce-inference/bootstrap.log 2>/dev/null || true",
    "CLOUD_INIT": "cloud-init status --long 2>&1 || true",
    "SERVICE": "systemctl is-active cce-inference 2>&1 || true",
    "HEALTH": "curl -s -o /dev/null -w '%{http_code}' -m 5 http://127.0.0.1:8080/health || true",
}
SERVER_LOG_CMD = "journalctl -u cce-inference -b --no-pager -o short-iso"
# parity outputs that reproduce the private corpus content: moved out of the evidence (hash recorded)
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
    """Instance cost if it ran until the watchdog's first stop after the run's time limit (EBS apart)."""
    minutes = int(req["limits"]["max_runtime_minutes"]) + WATCHDOG_GRACE_MIN + WATCHDOG_RATE_MIN
    return round(float(aws_target()["usd_per_hour"]) * minutes / 60, 2)


def validate_request(doc: Any) -> list[str]:
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
    if top.get("schema_version") != 2:
        e.append("schema_version: must be 2 (local request: aws.account_id, parity.zip_path)")
    rid = top.get("id")
    if not isinstance(rid, str) or not ID_RE.match(rid):
        e.append("id: 3-32 characters [a-z0-9-], starting and ending with a letter or digit")
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
    aws = _keys(top.get("aws"), "aws", {"account_id", "region", "instance_type"}, {"base_stack"}, e)
    if aws:
        if not isinstance(aws.get("account_id"), str) or not ACCOUNT_RE.match(aws["account_id"]):
            e.append(
                "aws.account_id: the 12 digits of the expected account, as text (checked with STS before any write)"
            )
        base = aws.get("base_stack", BASE_STACK_DEFAULT)
        if not isinstance(base, str) or not BASE_STACK_RE.match(base):
            e.append("aws.base_stack: a CloudFormation stack name")
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
        par = _keys(top["parity"], "parity", {"zip_path"}, {"sha256"}, e)
        if par:
            zp = par.get("zip_path")
            if not isinstance(zp, str) or not zp.endswith(".zip") or not Path(zp).expanduser().is_absolute():
                e.append("parity.zip_path: absolute local path of the results ZIP (outside Git)")
            sha = par.get("sha256")
            if sha is not None and (not isinstance(sha, str) or not SHA_RE.match(sha)):
                e.append("parity.sha256: 64 lowercase hex characters (optional)")
    if not e and auth and limits:
        cost = worst_case_usd(top)
        if cost > auth["max_spend_usd"]:
            e.append(
                f"authorization.max_spend_usd: {auth['max_spend_usd']} is below the worst case of the time limit "
                f"({cost} USD of {target['instance_type']} until the watchdog stops it)"
            )
    return e


def inside_checkout(path: Path) -> bool:
    repo = REPO.resolve()
    p = path.expanduser().resolve()
    return p == repo or repo in p.parents


def load_request(path: Path, *, local: bool = True) -> dict[str, Any]:
    """A request file. `local`: it must live outside the checkout (it names the account and the network); only the
    fictitious example inside the repository is checked with local=False."""
    if local and inside_checkout(path):
        raise RequestError(
            f"{path}: requests hold the account, network and budget: keep them OUTSIDE the checkout "
            "(e.g. ~/.config/cce-inference/a09-validation/<id>.json)"
        )
    try:
        doc = json.loads(path.expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RequestError(f"{path.name}: not readable JSON ({exc})") from exc
    errors = validate_request(doc)
    if errors:
        raise RequestError(f"{path.name}: " + "; ".join(errors))
    req: dict[str, Any] = doc
    req.setdefault("validation", {})
    req["aws"].setdefault("base_stack", BASE_STACK_DEFAULT)
    req["limits"].setdefault("bootstrap_timeout_minutes", 60)
    req["network"].setdefault("client_cidr", "")
    req["network"].setdefault("client_security_group_id", "")
    req.setdefault("parity", None)
    return req


# --- AWS through the CLI ---------------------------------------------------------------------------------------


def aws_env(profile: str | None, base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment of every AWS call and child process: the standard credential chain (environment credentials,
    AWS_PROFILE, SSO, temporary credentials), or the named `profile`, always in us-east-1. A profile together with
    credentials in the environment is ambiguous (the CLI's --profile and boto3's profile_name would disagree with
    the plain chain): refused instead of guessing."""
    env = dict(os.environ if base is None else base)
    if profile:
        present = [k for k in CREDENTIAL_ENV if env.get(k)]
        if present:
            raise Refused(f"--profile {profile} with {present} in the environment: ambiguous identity; unset one")
        env["AWS_PROFILE"] = profile
        env.pop("AWS_DEFAULT_PROFILE", None)
    env.update(AWS_REGION=REGION, AWS_DEFAULT_REGION=REGION, AWS_PAGER="")
    return env


class Aws:
    def __init__(self, profile: str | None = None) -> None:
        self.bin = os.environ.get("CCE_AWS_BIN", "aws")
        self.profile = profile or None
        self.env = aws_env(self.profile)

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


def boto3_identity() -> dict[str, Any]:
    """Run inside the controller interpreter (hidden subcommand): who boto3 is, with this process's environment."""
    import boto3  # only the controller environment has it

    session = boto3.Session()
    ident = session.client("sts", region_name=session.region_name).get_caller_identity()
    return {"account": ident["Account"], "arn": ident["Arn"], "region": session.region_name}


def verify_identity(aws: Aws, python: str, expected_account: str) -> dict[str, Any]:
    """Read-only, BEFORE any write: the AWS CLI and boto3 (the controller's interpreter, as the children will run it)
    must resolve the same identity, in us-east-1, in the request's account. Raises Refused otherwise."""
    try:
        cli = aws.json("sts", "get-caller-identity")
    except AwsError as exc:
        raise Refused(f"no usable AWS identity for the CLI (log in first, e.g. `aws sso login`): {exc}") from exc
    probe = subprocess.run(
        [python, str(Path(__file__).resolve()), "_boto3-identity"],
        capture_output=True,
        text=True,
        env=aws.env,
        timeout=120,
        check=False,
    )
    if probe.returncode != 0:
        raise Refused(f"boto3 in {python} cannot resolve an identity: {probe.stderr.strip()[-300:]}")
    b3 = json.loads(probe.stdout)
    ident = {
        "account": str(cli.get("Account", "")),
        "arn": str(cli.get("Arn", "")),
        "boto3_arn": b3.get("arn"),
        "boto3_region": b3.get("region"),
        "profile": aws.profile or os.environ.get("AWS_PROFILE") or "(default chain)",
        "region": REGION,
    }
    if b3.get("arn") != ident["arn"] or b3.get("account") != ident["account"]:
        raise Refused(f"the AWS CLI is {ident['arn']} but boto3 is {b3.get('arn')}: different identities")
    if b3.get("region") != REGION:
        raise Refused(f"boto3 resolves region {b3.get('region')!r}, not {REGION}")
    if ident["account"] != expected_account:
        raise Refused(
            f"the active identity is in account {ident['account']}, the request expects {expected_account}: "
            "nothing was written"
        )
    return ident


# --- base stack (execution role, host boundary, watchdog): read-only state and its setup ---------------------------


def template_resources(text: str) -> dict[str, str]:
    """Logical id -> type of the top-level Resources of a YAML template (two-space indentation, as ours)."""
    body = text.split("\nResources:\n", 1)[1].split("\nOutputs:\n", 1)[0]
    found = re.findall(r"^  ([A-Za-z0-9]+):\n    Type: (\S+)", body, flags=re.M)
    return dict(found)


def base_parameters(req: dict[str, Any]) -> dict[str, str]:
    net = req["network"]
    return {"VpcId": net["vpc_id"], "SubnetId": net["subnet_id"], "InstanceType": req["aws"]["instance_type"]}


def federated_trust(doc: Any) -> list[str]:
    if isinstance(doc, str):
        doc = json.loads(urllib.parse.unquote(doc))
    out = []
    for st in doc.get("Statement", []) if isinstance(doc, dict) else []:
        principal = st.get("Principal", {})
        if isinstance(principal, dict) and principal.get("Federated"):
            out.append(str(principal["Federated"]))
        if "AssumeRoleWithWebIdentity" in json.dumps(st.get("Action", "")):
            out.append("sts:AssumeRoleWithWebIdentity")
    return out


def base_stack_state(aws: Aws, req: dict[str, Any]) -> dict[str, Any]:
    """Read-only. `ready` only if the base stack is complete, has exactly the resources of validation-base.yaml (so no
    federated/CI role is left), no role of it trusts a federated principal, its outputs are there and its network is
    the request's. The template body is compared too (a difference is fixed by `setup --apply`)."""
    name = req["aws"]["base_stack"]
    want = template_resources(BASE_TEMPLATE.read_text(encoding="utf-8"))
    state: dict[str, Any] = {"name": name, "parameters_wanted": base_parameters(req)}
    stack = aws.stack(name)
    if stack is None:
        return state | {"status": "absent", "ready": False, "action": "create", "to_add": sorted(want)}
    status = str(stack.get("StackStatus"))
    outputs = aws.outputs(stack)
    current = {p["ParameterKey"]: p["ParameterValue"] for p in stack.get("Parameters", [])}
    summaries = aws.json("cloudformation", "list-stack-resources", "--stack-name", name).get(
        "StackResourceSummaries", []
    )
    have = {r["LogicalResourceId"]: r for r in summaries if r.get("ResourceStatus") != "DELETE_COMPLETE"}
    federated = {}
    for logical, r in have.items():
        if r.get("ResourceType") == "AWS::IAM::Role" and r.get("PhysicalResourceId"):
            role = aws.json("iam", "get-role", "--role-name", r["PhysicalResourceId"])["Role"]
            trust = federated_trust(role.get("AssumeRolePolicyDocument", {}))
            if trust:
                federated[logical] = {"role": r["PhysicalResourceId"], "trust": sorted(set(trust))}
    try:
        body = aws.json("cloudformation", "get-template", "--stack-name", name, "--template-stage", "Original")
        tbody = body.get("TemplateBody")
        template_matches = isinstance(tbody, str) and tbody.strip() == BASE_TEMPLATE.read_text(encoding="utf-8").strip()
    except AwsError:
        template_matches = False
    params_changed = sorted(k for k, v in state["parameters_wanted"].items() if current.get(k) != v)
    extra_params = sorted(set(current) - set(state["parameters_wanted"]))
    to_add = sorted(set(want) - set(have))
    to_remove = sorted(
        f"{lid} ({have[lid].get('ResourceType')} {have[lid].get('PhysicalResourceId')})"
        for lid in set(have) - set(want)
    )
    missing_outputs = [k for k in BASE_OUTPUTS if not outputs.get(k)]
    network = {"vpc_id": outputs.get("AllowedVpcId"), "subnet_id": outputs.get("AllowedSubnetId")}
    complete = status.endswith("_COMPLETE") and "ROLLBACK" not in status and not status.startswith("DELETE")
    ready = (
        complete
        and not to_add
        and not to_remove
        and not federated
        and not missing_outputs
        and not params_changed
        and network == {"vpc_id": req["network"]["vpc_id"], "subnet_id": req["network"]["subnet_id"]}
    )
    changes = bool(to_add or to_remove or params_changed or extra_params or not template_matches)
    return state | {
        "status": status,
        "ready": ready,
        "action": "update" if changes else "none",
        "template_matches": template_matches,
        "parameters_changed": params_changed,
        "parameters_dropped": extra_params,
        "to_add": to_add,
        "to_remove": to_remove,
        "federated_trust": federated,
        "missing_outputs": missing_outputs,
        "network": network,
        "outputs": {k: outputs.get(k) for k in BASE_OUTPUTS},
    }


def base_not_ready(state: dict[str, Any]) -> str:
    why = []
    if state.get("status") == "absent":
        why.append("it does not exist")
    for key in ("to_add", "to_remove", "parameters_changed", "missing_outputs"):
        if state.get(key):
            why.append(f"{key}={state[key]}")
    if state.get("federated_trust"):
        why.append(f"roles trusted by a federated principal (CI/OIDC) {state['federated_trust']}")
    if state.get("status") not in (None, "absent") and not str(state["status"]).endswith("_COMPLETE"):
        why.append(f"status {state['status']}")
    return (
        f"base stack {state['name']} is not ready ({'; '.join(why) or 'network differs: ' + str(state.get('network'))})"
        ": review `setup` and apply it with `setup --apply`"
    )


def check_network(aws: Aws, vpc_id: str, subnet_id: str, az: str, instance_type: str) -> dict[str, Any]:
    """Read-only: the VPC exists, the subnet is in it and in the AZ, the instance type is offered there and the subnet
    has an active default route (the bootstrap downloads 28.6 GB over HTTPS). Raises Refused otherwise."""
    if not aws.json("ec2", "describe-vpcs", "--vpc-ids", vpc_id).get("Vpcs"):
        raise Refused(f"VPC {vpc_id} not found")
    subnets = aws.json("ec2", "describe-subnets", "--subnet-ids", subnet_id).get("Subnets", [])
    if not subnets:
        raise Refused(f"subnet {subnet_id} not found")
    sn = subnets[0]
    if sn.get("VpcId") != vpc_id or sn.get("AvailabilityZone") != az:
        raise Refused(
            f"subnet {subnet_id} is in {sn.get('VpcId')}/{sn.get('AvailabilityZone')}, the request says {vpc_id}/{az}"
        )
    offers = aws.json(
        "ec2",
        "describe-instance-type-offerings",
        "--location-type",
        "availability-zone",
        "--filters",
        f"Name=instance-type,Values={instance_type}",
        f"Name=location,Values={az}",
    ).get("InstanceTypeOfferings", [])
    if not offers:
        raise Refused(f"{instance_type} is not offered in {az}")
    rts = aws.json("ec2", "describe-route-tables", "--filters", f"Name=association.subnet-id,Values={subnet_id}").get(
        "RouteTables", []
    )
    if not rts:
        rts = aws.json(
            "ec2",
            "describe-route-tables",
            "--filters",
            f"Name=vpc-id,Values={vpc_id}",
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
    if not via:
        raise Refused(f"subnet {subnet_id}: no active 0.0.0.0/0 route (bootstrap needs outbound HTTPS)")
    return {"vpc_id": vpc_id, "subnet_id": subnet_id, "availability_zone": az, "outbound": via}


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


# --- local inputs: parity ZIP, tools, checkout ---------------------------------------------------------------------


def check_parity_zip(par: dict[str, Any]) -> dict[str, Any]:
    """Read-only: the local results ZIP exists, matches its SHA-256 (if the request pins one), has only safe member
    paths and contains CAWbase_benchmark_v1.1/ and corrida_nocturna/. Raises Refused otherwise."""
    zpath = Path(par["zip_path"]).expanduser()
    if not zpath.is_file():
        raise Refused(f"parity ZIP not found: {zpath}")
    if inside_checkout(zpath):
        raise Refused("parity ZIP inside the checkout: keep it outside Git")
    got = sha256_file(zpath)
    if par.get("sha256") and got != par["sha256"]:
        raise Refused(f"parity ZIP sha256 {got} does not match the request")
    try:
        with zipfile.ZipFile(zpath) as zf:
            names = zf.namelist()
    except zipfile.BadZipFile as exc:
        raise Refused(f"parity ZIP is not a ZIP: {exc}") from exc
    for name in names:
        parts = Path(name).parts
        if name.startswith("/") or ".." in parts:
            raise Refused(f"parity ZIP: unsafe path {name!r}")
    dirs = {str(Path(n).parent) if not n.endswith("/") else n.rstrip("/") for n in names}
    candidates = {""} | {str(Path(d).parent) for d in dirs if Path(d).name == "CAWbase_benchmark_v1.1"}
    for cand in sorted(candidates, key=len):
        prefix = f"{cand}/" if cand and cand != "." else ""
        if all(
            any(n.startswith(f"{prefix}{sub}/") for n in names)
            for sub in ("CAWbase_benchmark_v1.1", "corrida_nocturna")
        ):
            return {"file": zpath.name, "sha256": got, "package_dir": prefix.rstrip("/")}
    raise Refused("parity ZIP: no directory with CAWbase_benchmark_v1.1/ and corrida_nocturna/")


def local_tools(aws: Aws, python: str) -> dict[str, str]:
    def version(argv: list[str]) -> str:
        try:
            r = subprocess.run(argv, capture_output=True, text=True, env=aws.env, timeout=60, check=False)
            return (
                (r.stdout or r.stderr).strip().splitlines()[0]
                if (r.stdout or r.stderr).strip()
                else f"exit {r.returncode}"
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"missing: {exc}"

    return {
        "aws": version([aws.bin, "--version"]),
        "session-manager-plugin": version(["session-manager-plugin", "--version"]),
        "python": version([python, "--version"]),
    }


def checkout_commit() -> dict[str, Any]:
    def git(*args: str) -> str:
        r = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, check=False)
        return r.stdout.strip() if r.returncode == 0 else ""

    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain", "--untracked-files=no"))}


# --- the run ----------------------------------------------------------------------------------------------------


class Run:
    def __init__(
        self,
        req: dict[str, Any],
        ev: Path,
        workdir: Path | None = None,
        python: str | None = None,
        profile: str | None = None,
    ) -> None:
        self.req = req
        self.ev = ev.resolve()
        self.workdir = (workdir or ev.parent / f"{ev.name}-work").resolve()
        self.python = python or sys.executable
        self.aws = Aws(profile)
        self.id: str = req["id"]
        self.stack_name: str = req["stack"]["name"]
        self.ledger = LEDGER_PREFIX + self.id
        self.key_param = f"{API_KEY_PREFIX}{self.stack_name}/api-key"
        self.schedule = f"cce-a09-{self.id}"
        self.t0 = now()
        self.deadline = self.t0 + req["limits"]["max_runtime_minutes"] * 60
        self.access: dict[str, str] = {}  # outputs of the base stack
        self.identity: dict[str, Any] = {}
        self.iid: str | None = None
        self.key_file = self.workdir / "secrets" / "api-key"
        self.record: dict[str, Any] = {"id": self.id, "stack": self.stack_name, "after_run": req["stack"]["after_run"]}
        self.parity_pkg: Path | None = None
        self.token = secrets.token_hex(16)

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
        if inside_checkout(self.ev) or inside_checkout(self.workdir):
            raise Refused("evidence and work directories must be outside the repository checkout")
        # who we are, before anything else touches AWS: CLI and boto3 agree, us-east-1, the expected account
        self.identity = pre["identity"] = verify_identity(self.aws, self.python, req["aws"]["account_id"])
        checks["identity"] = True
        if self.aws.get_param(self.ledger) is not None:
            raise Duplicate(self.ledger)  # first: a retry reports the earlier claim, whatever changed since
        stale = subprocess.run([sys.executable, str(BUILD_TEMPLATE), "--check"], capture_output=True, text=True)
        if stale.returncode != 0:
            raise Refused(f"cloudformation.yaml is stale: {stale.stderr.strip()}")
        pre["template_sha256"] = sha256_file(TEMPLATE)
        checks["template_up_to_date"] = True
        # base stack (setup): execution role, host boundary, watchdog and the network the roles were scoped to
        base = pre["base_stack"] = base_stack_state(self.aws, req)
        if not base["ready"]:
            raise Refused(base_not_ready(base))
        self.access = {k: str(v) for k, v in base["outputs"].items()}
        checks["base_stack"] = True
        checks["network_matches_base_stack"] = True
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
        pre["network"] = check_network(
            self.aws, net["vpc_id"], net["subnet_id"], net["availability_zone"], target["instance_type"]
        )
        if pre["network"]["outbound"] == "igw" and not net["assign_public_ip"]:
            raise Refused("subnet routes through an internet gateway: assign_public_ip must be true for egress")
        checks["instance_type_offered"] = checks["network"] = True
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
            owner = owner_record(self.aws, self.stack_name)
            iid = self.aws.outputs(stack).get("InstanceId", "")
            if not owner or owner.get("stack_id") != stack.get("StackId") or owner.get("instance_id") != iid:
                raise Refused(
                    f"stack {self.stack_name} has no matching ownership record ({OWNER_PREFIX}{self.stack_name}): "
                    "only a stack created by a validation request can be adopted with mode reuse"
                )
            pre["adopt_from"] = {k: owner.get(k) for k in ("owner_id", "stack_id", "instance_id")}
            state = self.aws.instance_state(iid)
            if state != "stopped":
                raise Refused(f"reused instance {iid} must be stopped (found {state})")
            if self.aws.get_param(self.key_param) is None:
                raise Refused(f"API key parameter {self.key_param} missing for the reused stack")
        checks["stack"] = mode
        # cost ceiling and the local tools
        pre["worst_case_instance_usd"] = worst_case_usd(req)
        pre["tools"] = local_tools(self.aws, self.python)
        if shutil.which("session-manager-plugin") is None:
            raise Refused("session-manager-plugin is not installed on this machine (needed for the SSM tunnel)")
        checks["local_tools"] = True
        if req.get("parity"):
            pre["parity"] = {"status": "configured", **check_parity_zip(req["parity"])}
        else:
            pre["parity"] = {"status": "pending", "reason": "no parity ZIP given (parity.zip_path or --parity-zip)"}
        return pre

    def unpack_parity(self) -> Path:
        """The optional results ZIP (outside Git), verified again and unpacked in the work directory, outside the
        evidence. Done after the preflight and before the claim: a mismatch refuses the run before anything exists."""
        info = check_parity_zip(self.req["parity"])
        zpath = Path(self.req["parity"]["zip_path"]).expanduser()
        out = self.workdir / "parity" / "unpacked"
        with zipfile.ZipFile(zpath) as zf:
            zf.extractall(out)  # noqa: S202 - every member path was checked by check_parity_zip
        pkg = out / info["package_dir"] if info["package_dir"] else out
        return pkg

    # --- claim ---

    def claim(self) -> None:
        ctx = {
            "id": self.id,
            "stack": self.stack_name,
            "run_id": f"local-{int(self.t0)}-{os.getpid()}",
            "operator": self.identity.get("arn", ""),
            "checkout": checkout_commit(),
            "claimed_at": iso(self.t0),
            "status": "claimed",
            "mode": self.req["stack"]["mode"],
            "token": self.token,  # tags what this claim creates; ownership is checked against it
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

    # --- ownership ---

    def write_owner(self, **fields: Any) -> None:
        """The stack's ownership record: this claim (id + token) owns it. Written BEFORE CreateStack (create) or at
        adoption (reuse, overwriting the previous owner), then completed with the stack and instance ids."""
        current = owner_record(self.aws, self.stack_name) or {}
        if current.get("owner_id") != self.id or current.get("token") != self.token:
            current = {"stack_name": self.stack_name, "stack_id": None, "instance_id": None}
        current.update(fields, owner_id=self.id, token=self.token, updated_at=iso(now()))
        path = self.workdir / "owner.json"
        path.write_text(json.dumps(current, separators=(",", ":")), encoding="utf-8")
        self.aws.put_param(OWNER_PREFIX + self.stack_name, path, overwrite=True)
        self.record["owner"] = {k: v for k, v in current.items() if k != "token"}

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
            # the AWS-side stop is created by CloudFormation with the instance: no local process in between
            "WatchdogStart": iso(self.deadline + WATCHDOG_GRACE_MIN * 60),
            "WatchdogEnd": iso(self.deadline + WATCHDOG_GRACE_MIN * 60 + WATCHDOG_WINDOW_H * 3600),
            "WatchdogRoleArn": self.access["WatchdogRoleArn"],
            "WatchdogScheduleGroup": self.access["WatchdogScheduleGroup"],
        }
        tags = {
            "cce:claim-token": self.token,
            "cce:validation-id": self.id,
            "cce:template-sha256": pre["template_sha256"],
            "cce:after-run": self.req["stack"]["after_run"],
        }
        self.record["stack_parameters"] = params
        wd = {
            "kind": "in_stack",
            "name": f"{self.stack_name}-watchdog",
            "group": params["WatchdogScheduleGroup"],
            "first_stop_at": params["WatchdogStart"],
            "until": params["WatchdogEnd"],
            "every_minutes": WATCHDOG_RATE_MIN,
        }
        self.record["watchdog"] = wd
        save(self.ev, "watchdog.json", wd)
        self.write_owner(created_by=self.id)  # before CreateStack: a lost process leaves a record to clean by
        out = self.aws.json(
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
        self.write_owner(stack_id=out.get("StackId"))
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
        """A recurring stop of THIS instance, run by EventBridge Scheduler in AWS from the run's time limit on: it
        does not depend on this process. The cleanup deletes it only after it verified the instance stopped."""
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
        wd: dict[str, Any] = {"kind": "runner", "name": self.schedule, "group": self.access["WatchdogScheduleGroup"]}
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
            prev = owner_record(self.aws, self.stack_name) or {}
            # explicit adoption: from now on this claim owns the stack; an older request's retry preserves it
            self.write_owner(stack_id=stack["StackId"], instance_id=self.iid, adopted_from=prev.get("owner_id"))
            self.arm_watchdog()  # the instance is stopped: armed before anything can start it
            save(self.ev, "provision.json", self.record)
            return
        self.create_stack(pre)
        self.ledger_update(status="provisioning")
        self.iid = wait_for("the instance of the stack", self.host_id, 1200, deadline=self.deadline)
        tl["instance_created"] = now()
        self.write_owner(instance_id=self.iid)

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
        # evidence: the schedule CloudFormation created with the instance targets exactly this instance
        wd = self.record["watchdog"]
        sched = self.aws.json("scheduler", "get-schedule", "--name", wd["name"], "--group-name", wd["group"])
        target = json.loads(sched.get("Target", sched.get("target", {})).get("Input", "{}"))
        wd["targets"] = target.get("InstanceIds")
        if wd["targets"] != [self.iid]:
            raise ValidationFailed(f"in-stack watchdog does not target {self.iid}: {sched}")
        save(self.ev, "watchdog.json", wd)

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
            "profile": "",  # the standard chain of the environment this process passes on (AWS_PROFILE, SSO, keys)
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
            if self.req.get("parity"):
                self.parity_pkg = self.unpack_parity()  # local only; before the claim
        except Duplicate:
            dup = json.loads(self.aws.get_param(self.ledger) or "{}")
            save(self.ev, "duplicate.json", {"ledger": self.ledger, "claim": dup})
            outcome |= {"validation": "not_run_duplicate", "exit": 3, "claimed_by": dup, "cleanup_in_run": "not_run"}
            save(self.ev, "outcome.json", outcome)
            print(
                f"DUPLICATE: request {self.id} was already claimed by {dup.get('run_id')} "
                f"(status {dup.get('status')}): no new spend. Use a new id to run again; "
                "`cleanup` re-verifies the old run's resources.",
                file=sys.stderr,
            )
            report(self.ev, outcome)
            return 3
        except (Refused, AwsError) as exc:
            save(self.ev, "preflight.json", {"refused": str(exc), "identity": self.identity or None})
            outcome |= {"validation": "refused", "exit": 2, "reason": str(exc), "cleanup_in_run": "not_needed"}
            save(self.ev, "outcome.json", outcome)
            print(f"REFUSED (nothing claimed or created): {exc}", file=sys.stderr)
            report(self.ev, outcome)
            return 2
        except KeyboardInterrupt:
            outcome |= {"validation": "interrupted before any write", "exit": 130, "cleanup_in_run": "not_needed"}
            save(self.ev, "outcome.json", outcome)
            report(self.ev, outcome)
            return 130
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
        report(self.ev, outcome)
        # the cleanup decides the exit status when it failed: resources may still be there
        return 5 if outcome.get("cleanup_in_run") == "FAILED" else code


def report(ev: Path, outcome: dict[str, Any]) -> None:
    """The two results, apart: the validation (tests) and the cleanup."""
    clean_file = ev / "cleanup-run.json"
    clean = json.loads(clean_file.read_text()) if clean_file.exists() else {}
    lines = [
        f"== A09-1 AWS validation {outcome.get('id')} "
        f"(stack {outcome.get('stack')}, after_run {outcome.get('after_run')})",
        f"   VALIDATION (tests): {outcome.get('validation')} (exit {outcome.get('exit')})",
        f"   CLEANUP:            {outcome.get('cleanup_in_run')}"
        + (f" instance={clean.get('instance')} stack={clean.get('stack_status')}" if clean else ""),
        f"   PARITY:             {(outcome.get('parity') or {}).get('status', 'pending')}",
        f"   evidence:           {ev}",
    ]
    if outcome.get("cleanup_in_run") == "FAILED":
        lines.append("   CLEANUP FAILED: repeat `a09-aws-validation cleanup --request …` (the AWS-side watchdog")
        lines.append("   keeps stopping the instance meanwhile); see cleanup-run.json for what is left")
    print("\n".join(lines), flush=True)


# --- cleanup (idempotent; also `cleanup` after a lost process) ---------------------------------------------------


def owner_record(aws: Aws, stack_name: str) -> dict[str, Any] | None:
    raw = aws.get_param(OWNER_PREFIX + stack_name)
    try:
        doc = json.loads(raw) if raw else None
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def tags_of(obj: dict[str, Any]) -> dict[str, str]:
    return {t["Key"]: t["Value"] for t in obj.get("Tags", [])}


class Cleanup:
    """Stops and, if the request says delete, deletes ONLY what this request owns: the request must be claimed, the
    stack's ownership record must name this claim (id and token: it created the stack or adopted it with reuse), the
    stack must be that one (StackId, or the claim token tag if the process was lost before learning the id), and only
    the concrete instance of that stack is touched. Anything else is preserved and reported."""

    def __init__(self, req: dict[str, Any], ev: Path, aws: Aws | None = None) -> None:
        self.req = req
        self.ev = ev
        self.aws = aws or Aws()
        self.base_stack: str = req["aws"]["base_stack"]
        self.id: str = req["id"]
        self.stack_name: str = req["stack"]["name"]
        self.after: str = req["stack"]["after_run"]
        self.key_param = f"{API_KEY_PREFIX}{self.stack_name}/api-key"
        self.schedule = f"cce-a09-{req['id']}"
        self.ledger = LEDGER_PREFIX + req["id"]

    def ownership(self, rec: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """(owner record, owned stack or None) when this claim owns the stack; (None, None) when nothing here is
        this request's (the reason is in rec['ownership'])."""
        raw = self.aws.get_param(self.ledger)
        claim = json.loads(raw) if raw else None
        if not isinstance(claim, dict) or not claim.get("token"):
            rec["ownership"] = "not_claimed: this request never reserved its id; nothing is stopped or deleted"
            return None, None
        owner = owner_record(self.aws, self.stack_name)
        rec["owner_record"] = {k: v for k, v in (owner or {}).items() if k != "token"}
        if not owner or owner.get("owner_id") != self.id or owner.get("token") != claim["token"]:
            who = (owner or {}).get("owner_id")
            rec["ownership"] = (
                f"not_owned: stack owned by {who!r} (adopted or created by another request)"
                if who
                else "not_owned: this request created or adopted nothing (or its cleanup already finished)"
            )
            return None, None
        stack = self.aws.stack(owner["stack_id"] or self.stack_name)
        if stack is not None:
            same = (
                stack.get("StackId") == owner["stack_id"]
                if owner.get("stack_id")
                else tags_of(stack).get("cce:claim-token") == claim["token"]
            )
            if not same:
                rec["ownership"] = f"not_owned: {self.stack_name} is another stack ({stack.get('StackId')})"
                return None, None
            if not owner.get("stack_id"):  # the process was lost before it learned the id: recover it from the tag
                owner["stack_id"] = stack["StackId"]
        rec["ownership"] = "owned" + (f" (adopted from {owner['adopted_from']})" if owner.get("adopted_from") else "")
        if stack is None and owner.get("stack_id"):
            other = self.aws.stack(self.stack_name)
            if other is not None and other.get("StackId") != owner["stack_id"]:
                rec["ownership"] += f"; the owned stack is gone and {self.stack_name} is another stack: preserved"
        return owner, stack

    def instance_of(self, owner: dict[str, Any], stack: dict[str, Any] | None) -> str | None:
        iid = owner.get("instance_id")
        if not iid and stack is not None:
            try:
                res = self.aws.json(
                    "cloudformation",
                    "describe-stack-resource",
                    "--stack-name",
                    stack["StackId"],
                    "--logical-resource-id",
                    "Host",
                )
                iid = res.get("StackResourceDetail", {}).get("PhysicalResourceId") or None
            except AwsError:
                iid = None
        return str(iid) if iid else None

    def stop_owned(self, iid: str, stack_id: str, rec: dict[str, Any], errors: list[str]) -> bool:
        res = self.aws.json("ec2", "describe-instances", "--instance-ids", iid) if iid else {}
        insts = [i for r in res.get("Reservations", []) for i in r.get("Instances", [])]
        if not insts:
            rec["instance"] = {"id": iid, "state": "absent"}
            return True
        inst = insts[0]
        if tags_of(inst).get("aws:cloudformation:stack-id") not in (None, stack_id):
            rec["instance"] = {"id": iid, "state": inst["State"]["Name"], "preserved": "belongs to another stack"}
            return True  # not ours: neither stopped nor counted as left over
        state = inst["State"]["Name"]
        if state in ("pending", "running"):
            try:
                self.aws.raw("ec2", "stop-instances", "--instance-ids", iid)
                rec["stopped_by_cleanup"] = iid
            except AwsError as exc:
                errors.append(f"stop_instances_failed {iid}: {exc}")
        try:
            final = wait_for(
                f"{iid} stopped",
                lambda: (st := self.aws.instance_state(iid)) in ("stopped", "terminated", "absent") and st,
                900,
            )
        except ValidationFailed:
            final = self.aws.instance_state(iid)
            errors.append(f"instance_not_stopped {iid}: {final}")
        rec["instance"] = {"id": iid, "state": final}
        return final in ("stopped", "terminated", "absent")

    def delete_stack(self, stack_id: str, rec: dict[str, Any], errors: list[str]) -> bool:
        """Delete by StackId (never by name: a stack recreated with the same name is not this one)."""
        for attempt in (1, 2):
            stack = self.aws.stack(stack_id)
            if stack is None:
                break
            if not str(stack.get("StackStatus")).startswith("DELETE_IN_PROGRESS"):
                try:
                    self.aws.raw("cloudformation", "delete-stack", "--stack-name", stack_id)
                except AwsError as exc:
                    errors.append(f"delete_stack_failed: {exc}")
                    return False

            def gone() -> str | bool:
                s = self.aws.stack(stack_id)
                if s is None:
                    return "deleted"
                return "failed" if str(s.get("StackStatus")) == "DELETE_FAILED" else False

            try:
                result = wait_for("stack deleted", gone, 2400)
            except ValidationFailed as exc:
                errors.append(str(exc))
                return False
            if result == "deleted":
                break
            rec.setdefault("delete_failed_attempts", []).append(attempt)
        stack = self.aws.stack(stack_id)
        rec["stack_status"] = stack.get("StackStatus") if stack else "DELETE_COMPLETE"
        if stack is not None:
            errors.append(f"stack_not_deleted: {rec['stack_status']}")
            return False
        return True

    def volumes_left(self, stack_id: str) -> list[str]:
        res = self.aws.json(
            "ec2", "describe-volumes", "--filters", f"Name=tag:aws:cloudformation:stack-id,Values={stack_id}"
        )
        return [v["VolumeId"] for v in res.get("Volumes", [])]

    def main(self, name: str = "cleanup.json") -> int:
        self.ev.mkdir(parents=True, exist_ok=True)
        rec: dict[str, Any] = {"id": self.id, "stack": self.stack_name, "after_run": self.after}
        errors: list[str] = []
        ok = True
        try:
            owner, stack = self.ownership(rec)
            if owner is None:
                rec["watchdog"] = "untouched"
                rec["outcome"] = "verified"
                rec["errors"] = []
                save(self.ev, name, rec)
                log(f"cleanup: nothing of {self.id} to clean ({rec['ownership']}); resources preserved")
                return 0
            stack_id = str(owner["stack_id"] or "")
            rec["stack_id"] = stack_id
            rec["stack_status_before"] = stack.get("StackStatus") if stack else "absent"
            iid = self.instance_of(owner, stack)
            stopped = self.stop_owned(iid, stack_id, rec, errors) if iid else True
            rec["instances_stopped"] = stopped
            if self.after == "delete":
                deleted = self.delete_stack(stack_id, rec, errors) if (stack is not None and stack_id) else True
                rec["stack_deleted"] = deleted
                vols = self.volumes_left(stack_id) if stack_id else []
                ours = bool(iid) and "preserved" not in rec.get("instance", {})
                final = self.aws.instance_state(str(iid)) if ours else "absent"
                rec["volumes_left"], rec["instance_final"] = vols, final
                if vols or final not in ("terminated", "absent"):
                    errors.append(f"resources_left: instance {iid} {final}, volumes {vols}")
                ok = stopped and deleted and not vols and final in ("terminated", "absent")
                if ok and self.aws.stack(self.stack_name) is None:  # parameters are per NAME: only if it is free
                    rec["api_key_parameter_deleted"] = self.aws.delete_param(self.key_param)
                    self.aws.delete_param(OWNER_PREFIX + self.stack_name)  # nothing left to own
                    rec["owner_record"] = "deleted"
                elif ok:
                    rec["api_key_parameter_deleted"] = False  # the name now belongs to another stack: kept
            else:
                after = self.aws.stack(stack_id) if stack_id else None
                rec["stack_status"] = after.get("StackStatus") if after else "absent"
                in_progress = after is not None and str(after.get("StackStatus")).endswith("_IN_PROGRESS")
                if in_progress:
                    errors.append(f"stack_in_progress: {rec['stack_status']}")
                rec["kept"] = bool(after) and stopped
                ok = stopped and not in_progress
                if after:
                    rec["residual"] = "EBS of the stopped stack (root + weights) until it is deleted"
            # runner watchdog (reused stacks) removed only once the instance is verified stopped; the in-stack one
            # goes with the stack (delete) or ends at its EndDate (keep-stopped)
            rec["watchdog"] = self.disarm() if ok else "kept_armed"
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
        log(f"cleanup verified ({self.after}): {json.dumps({k: rec.get(k) for k in ('instance', 'stack_status')})}")
        return 0

    def disarm(self) -> str:
        group = None
        try:
            access = self.aws.stack(self.base_stack)
            group = self.aws.outputs(access).get("WatchdogScheduleGroup") if access else None
        except AwsError:
            group = None
        if not group:
            return "unknown_group"
        try:
            self.aws.raw("scheduler", "delete-schedule", "--name", self.schedule, "--group-name", group)
            return "runner_schedule_disarmed"
        except AwsError as exc:
            return "no_runner_schedule" if exc.code == "ResourceNotFoundException" else f"disarm_failed: {exc}"

    def update_ledger(self, outcome: str) -> None:
        try:
            current = json.loads(self.aws.get_param(self.ledger) or "null")
            if not isinstance(current, dict):
                return  # never create a claim from the cleanup
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
            {"reason": "parity outputs reproduce the private corpus; kept out of the evidence", "files": moved},
        )
    return moved


SECRET_ENV = ("AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_ACCESS_KEY_ID")
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


# --- setup and plan --------------------------------------------------------------------------------------------


def setup(aws: Aws, req: dict[str, Any], python: str, out: Path, apply: bool) -> int:
    """The base stack, idempotent: plan (read-only) or, with apply, `cloudformation deploy` of validation-base.yaml and
    its verification (ready: exactly the template's resources, no role trusted by a federated principal, outputs,
    network). Identity and account are verified before anything is read or written."""
    rec: dict[str, Any] = {"mode": "apply" if apply else "plan", "base_stack": req["aws"]["base_stack"]}
    try:
        rec["identity"] = verify_identity(aws, python, req["aws"]["account_id"])
        net = req["network"]
        rec["network"] = check_network(
            aws, net["vpc_id"], net["subnet_id"], net["availability_zone"], req["aws"]["instance_type"]
        )
        rec["before"] = base_stack_state(aws, req)
    except (Refused, AwsError) as exc:
        rec["refused"] = str(exc)
        save(out, "setup.json", rec)
        print(f"REFUSED (nothing changed): {exc}", file=sys.stderr)
        return 2
    before = rec["before"]
    print(json.dumps({k: before.get(k) for k in SETUP_PLAN_KEYS if k in before}, indent=2))
    if not apply:
        save(out, "setup.json", rec)
        print(
            "base stack ready: nothing to do"
            if before["ready"] and before["action"] == "none"
            else "plan only: apply it with `setup --apply`"
        )
        return 0
    overrides = [f"{k}={v}" for k, v in base_parameters(req).items()]
    try:
        out_text = aws.raw(
            "cloudformation",
            "deploy",
            "--stack-name",
            req["aws"]["base_stack"],
            "--template-file",
            str(BASE_TEMPLATE),
            "--capabilities",
            "CAPABILITY_NAMED_IAM",
            "--no-fail-on-empty-changeset",
            "--parameter-overrides",
            *overrides,
            timeout=1800,
        )
        rec["deploy"] = "no_changes" if "No changes to deploy" in out_text else "created_or_updated"
        after = rec["after"] = base_stack_state(aws, req)
    except AwsError as exc:
        rec["failed"] = str(exc)
        save(out, "setup.json", rec)
        print(f"SETUP NOT VERIFIED: {exc}", file=sys.stderr)
        return 1
    save(out, "setup.json", rec)
    if not after["ready"]:
        print(f"SETUP NOT VERIFIED: {base_not_ready(after)}", file=sys.stderr)
        return 1
    print(f"base stack {after['name']} ready ({rec['deploy']}): no federated trust, outputs and network verified")
    return 0


SETUP_PLAN_KEYS = (
    "name",
    "status",
    "ready",
    "action",
    "to_add",
    "to_remove",
    "federated_trust",
    "parameters_changed",
    "parameters_dropped",
    "template_matches",
    "network",
)


def plan(run: Run) -> int:
    """Read-only: the identity, the base stack and the whole preflight of the run (nothing claimed or created)."""
    run.ev.mkdir(parents=True, exist_ok=True)
    try:
        pre = run.preflight()
        pre["plan"] = "ready: `run` would claim the id and create the stack"
        code = 0
    except Duplicate:
        pre = {"plan": f"duplicate: {run.ledger} is already claimed (no new spend; use a new id)"}
        code = 3
    except (Refused, AwsError) as exc:
        pre = {"plan": f"refused: {exc}", "identity": run.identity or None}
        code = 2
    save(run.ev, "plan.json", pre)
    print(
        json.dumps(
            {
                k: pre.get(k)
                for k in (
                    "plan",
                    "identity",
                    "quota",
                    "network",
                    "ami",
                    "parity",
                    "worst_case_instance_usd",
                    "base_stack",
                )
                if k in pre
            },
            indent=2,
            default=str,
        )
    )
    return code


# --- CLI -----------------------------------------------------------------------------------------------------------


def state_dir(rid: str) -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return base / "cce-inference" / "a09-validation" / rid


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="offline validation of request files (default: the fictitious example)")
    c.add_argument("files", nargs="*")
    for name in ("plan", "setup", "run", "cleanup"):
        p = sub.add_parser(name)
        p.add_argument("--request", required=True, help="local request file, outside the checkout")
        p.add_argument("--profile", help="named AWS profile (default: the standard credential chain)")
        p.add_argument("--evidence", help="evidence directory (default: the local state directory)")
        p.add_argument("--python", default=sys.executable, help="controller interpreter (with boto3)")
        if name in ("plan", "run"):
            p.add_argument("--workdir", help="work directory (default: next to the evidence)")
            p.add_argument("--parity-zip", help="local results ZIP for the parity run (optional)")
            p.add_argument("--tunnel-port", type=int, default=18080)
        if name == "setup":
            p.add_argument("--apply", action="store_true", help="create or update the base stack (default: plan)")
    sub.add_parser("_boto3-identity")
    a = ap.parse_args(argv)

    if a.cmd == "_boto3-identity":
        print(json.dumps(boto3_identity()))
        return 0
    if a.cmd == "check":
        files = [Path(f) for f in a.files] or [EXAMPLE_REQUEST]
        errors = []
        for f in files:
            try:
                load_request(f, local=f.resolve() != EXAMPLE_REQUEST)
            except RequestError as exc:
                errors.append(str(exc))
        for err in errors:
            print(f"INVALID: {err}", file=sys.stderr)
        print(f"{len(files)} request(s) checked, {len(errors)} error(s)")
        return 2 if errors else 0
    try:
        req = load_request(Path(a.request))
        if getattr(a, "parity_zip", None):
            zp = str(Path(a.parity_zip).expanduser().resolve())
            pinned = (req.get("parity") or {}).get("sha256")
            req["parity"] = {"zip_path": zp, **({"sha256": pinned} if pinned else {})}
            errors = validate_request(req)
            if errors:
                raise RequestError("; ".join(errors))
        aws = Aws(a.profile)
    except (RequestError, Refused) as exc:
        print(f"INVALID REQUEST (nothing read or written in AWS): {exc}", file=sys.stderr)
        return 2
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    ev = Path(a.evidence).expanduser() if a.evidence else state_dir(req["id"]) / f"{a.cmd}-{stamp}"
    if inside_checkout(ev):
        print("REFUSED: the evidence directory must be outside the checkout", file=sys.stderr)
        return 2
    if a.cmd == "setup":
        return setup(aws, req, a.python, ev, a.apply)
    if a.cmd == "cleanup":
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        try:
            ident = verify_identity(aws, a.python, req["aws"]["account_id"])
        except (Refused, AwsError) as exc:
            print(f"REFUSED (nothing changed): {exc}", file=sys.stderr)
            return 2
        save(ev, "identity.json", ident)
        cl = Cleanup(req, ev, aws=aws)
        rc = cl.main()
        cl.update_ledger("verified" if rc == 0 else "FAILED")
        redact(ev, [])
        print(f"   CLEANUP: {'verified' if rc == 0 else 'FAILED'} ({req['stack']['after_run']}); evidence: {ev}")
        return rc
    workdir = Path(a.workdir).expanduser() if a.workdir else ev.parent / f"{ev.name}-work"
    run = Run(req, ev, workdir, a.python, a.profile)
    run.aws = aws
    run.tunnel_port = a.tunnel_port
    if a.cmd == "plan":
        return plan(run)
    return run.main()


if __name__ == "__main__":
    sys.exit(main())
