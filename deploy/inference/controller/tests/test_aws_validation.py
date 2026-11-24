"""The LOCAL AWS validation (deploy/inference/validation/aws_validation.py) end to end with SIMULATED AWS: the fake
`aws` CLI keeps an account state (base stack, quota, network, AMIs, SSM parameters, CloudFormation stacks, EventBridge
schedules, identities by access key), a fake STS endpoint answers boto3 (the identity probe runs the REAL boto3
credential chain against it), the "instance" is the simulated host of sim_host.sh (fake llama-server through the real
run-server.sh), Run Command runs locally, and the real bench/gpu-validation.py, controller and measure.py run step B.
No AWS resource is ever touched, and neither GitHub nor OIDC is available: a `gh` trap fails the test if called.

Cases: the local flow end to end (delete and keep-stopped) with a named profile and with environment credentials;
identity checks before any write (wrong account, CLI and boto3 disagreeing, no credentials, profile + environment
keys); plan and invalid requests write nothing; the base stack setup (create, idempotent, revoking a former CI/OIDC
role, refusing to run while it is there); a retry with the same id spends nothing; reuse of a kept stack; every
preflight refusal creates nothing; validation failure and cleanup failure reported apart; SIGTERM and SIGINT; the
process killed with SIGKILL while the AWS-side watchdog still stops the instance; secrets never reach the evidence."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from conftest import DEPLOY, Q8
from test_gpu_validation import SIM_HOST, Sim, wait_until

CI_SCRIPT = DEPLOY / "validation" / "aws_validation.py"
WRAPPER = DEPLOY / "bin" / "a09-aws-validation"
REPO = DEPLOY.parent.parent
ACCOUNT = "000000000000"
VPC, SUBNET, AZ, AMI = "vpc-0aaaaaaaaaaaaaaaa", "subnet-0bbbbbbbbbbbbbbbb", "us-east-1a", "ami-0ccccccccccccccc0"
AMI_PARAM = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id"
BASE = "cce-a09-validation-base"
PROFILE_KEY, ENV_KEY, OTHER_KEY = "AKIASIMPROFILE000001", "ASIASIMSESSION000002", "AKIASIMOTHERACCT0003"
OPERATOR = f"arn:aws:iam::{ACCOUNT}:user/sim-operator"
BASE_OUT = {
    "CfnExecutionRoleArn": f"arn:aws:iam::{ACCOUNT}:role/cce-a09-cfn-execution",
    "HostPermissionsBoundaryArn": f"arn:aws:iam::{ACCOUNT}:policy/cce-a09-host-boundary",
    "WatchdogRoleArn": f"arn:aws:iam::{ACCOUNT}:role/cce-a09-watchdog",
    "WatchdogScheduleGroup": "cce-inference-watchdog",
}
ACCESS = BASE_OUT  # the outputs the validation stacks use
READ_ONLY = re.compile(r"^(describe-|get-|list-|validate-)")


def load_ci() -> Any:
    spec = importlib.util.spec_from_file_location("aws_validation", CI_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["aws_validation"] = mod
    spec.loader.exec_module(mod)
    return mod


ci = load_ci()


def request(rid: str = "a09-1-sim-001", **over: Any) -> dict[str, Any]:
    doc = json.loads((DEPLOY / "validation" / "request.example.json").read_text())
    doc["id"] = rid
    doc["aws"] = {"account_id": ACCOUNT, "region": "us-east-1", "instance_type": "g6e.xlarge", "base_stack": BASE}
    doc["network"] = {"vpc_id": VPC, "subnet_id": SUBNET, "availability_zone": AZ, "assign_public_ip": True}
    doc["ami"] = "resolve-latest"
    doc["stack"] = {"name": f"cce-inference-{rid}", "mode": "create", "after_run": "delete"}
    doc["limits"] = {"max_runtime_minutes": 60, "bootstrap_timeout_minutes": 15}
    doc["validation"] = {"profiles": ["reference-8k-1slot"], "requests": 2, "prompt_tokens": 200}
    for key, value in over.items():
        doc[key] = value
    return doc


class FakeSts:
    """STS GetCallerIdentity for boto3 (AWS_ENDPOINT_URL_STS): the identity of the access key in the SigV4 header,
    from the same account state as the fake CLI. `override` makes boto3 see someone else."""

    def __init__(self, state_file: Path) -> None:
        self.state_file = state_file
        self.override: dict[str, str] | None = None
        self.calls: list[dict[str, str]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                m = re.search(r"Credential=([^/]+)/\d+/([^/]+)/sts/", self.headers.get("Authorization", ""))
                key, region = (m.group(1), m.group(2)) if m else ("", "")
                outer.calls.append({"key": key, "region": region})
                ids = json.loads(outer.state_file.read_text()).get("identities", {})
                ident = outer.override or ids.get(key)
                if ident is None:
                    body = b"<ErrorResponse><Error><Code>InvalidClientTokenId</Code><Message>x</Message></Error></ErrorResponse>"
                    self.send_response(403)
                else:
                    body = (
                        '<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">'
                        f"<GetCallerIdentityResult><Arn>{ident['Arn']}</Arn><UserId>AIDASIM</UserId>"
                        f"<Account>{ident['Account']}</Account></GetCallerIdentityResult>"
                        "<ResponseMetadata><RequestId>sim</RequestId></ResponseMetadata></GetCallerIdentityResponse>"
                    ).encode()
                    self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


class CiSim(Sim):
    """Sim (test_gpu_validation) plus a seeded fake account, a fake STS, local AWS profiles and the overrides."""

    def __init__(self, tmp: Path, fake_model: dict[str, str]) -> None:
        super().__init__(tmp, fake_model)
        self.fake = tmp / "fake-aws"
        self.fake.mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        (self.bin / "aws").symlink_to(tmp / "aws")  # the host's run-server.sh calls `aws` from PATH
        plugin = self.bin / "session-manager-plugin"
        plugin.write_text("#!/bin/sh\necho 1.2.0-sim\n")
        plugin.chmod(0o755)
        self.gh_trap = tmp / "gh-called"
        trap = self.bin / "gh"
        trap.write_text(f'#!/bin/sh\necho "$@" >> {self.gh_trap}\nexit 1\n')
        trap.chmod(0o755)
        self.seed()
        self.sts = FakeSts(self.fake / "state.json")
        creds = tmp / "aws-credentials"
        creds.write_text(
            f"[sim-operator]\naws_access_key_id = {PROFILE_KEY}\naws_secret_access_key = simsecretprofile0001\n"
            f"[sim-other]\naws_access_key_id = {OTHER_KEY}\naws_secret_access_key = simsecretother000003\n"
        )
        (tmp / "aws-config").write_text("[profile sim-operator]\n[profile sim-other]\n")
        self.cievidence = tmp / "ev"
        self.work = tmp / "work"
        port = self.port
        # nothing of the developer's AWS or GitHub session leaks into the simulation
        self.env = {
            k: v
            for k, v in self.env.items()
            if not k.startswith(("AWS_", "GITHUB_", "ACTIONS_", "GH_")) and k != "CCE_A09_ACCESS_STACK"
        }
        self.env.update(
            {
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "AWS_SHARED_CREDENTIALS_FILE": str(creds),
                "AWS_CONFIG_FILE": str(tmp / "aws-config"),
                "AWS_ENDPOINT_URL_STS": self.sts.url,
                "AWS_EC2_METADATA_DISABLED": "true",
                "CCE_CI_CONTROLLER_TEMPLATE": str(self.cfg),
                "CCE_CI_HOST_BOOTSTRAP_CMD": f"cat {tmp / 'bootstrap.log'}",
                "CCE_CI_HOST_CLOUD_INIT_CMD": f"cat {tmp / 'cloud-init-status'}",
                "CCE_CI_HOST_SERVICE_CMD": f"{SIM_HOST} {tmp} describe | sed s/running/active/",
                "CCE_CI_HOST_HEALTH_CMD": f"curl -s -o /dev/null -w '%{{http_code}}' -m 5 "
                f"http://127.0.0.1:{port}/health || true",
            }
        )
        self.profile: str | None = "sim-operator"

    def seed(self, **over: Any) -> None:
        body = ci.BASE_TEMPLATE.read_text()
        base = {
            "id": f"arn:aws:cloudformation:us-east-1:{ACCOUNT}:stack/{BASE}/seeded",
            "status": "CREATE_COMPLETE",
            "params": {"VpcId": VPC, "SubnetId": SUBNET, "InstanceType": "g6e.xlarge"},
            "outputs": {**BASE_OUT, "AllowedVpcId": VPC, "AllowedSubnetId": SUBNET},
            "resources": {
                "CfnExecutionRole": {"type": "AWS::IAM::Role", "physical": "cce-a09-cfn-execution"},
                "HostPermissionsBoundary": {
                    "type": "AWS::IAM::ManagedPolicy",
                    "physical": BASE_OUT["HostPermissionsBoundaryArn"],
                },
                "WatchdogRole": {"type": "AWS::IAM::Role", "physical": "cce-a09-watchdog"},
                "WatchdogScheduleGroup": {
                    "type": "AWS::Scheduler::ScheduleGroup",
                    "physical": "cce-inference-watchdog",
                },
            },
            "template": body,
        }

        def svc_role(name: str, service: str) -> dict[str, Any]:
            stmt = {"Effect": "Allow", "Principal": {"Service": service}, "Action": "sts:AssumeRole"}
            return {"Arn": f"arn:aws:iam::{ACCOUNT}:role/{name}", "AssumeRolePolicyDocument": {"Statement": [stmt]}}

        st: dict[str, Any] = {
            "identities": {
                PROFILE_KEY: {"Account": ACCOUNT, "Arn": OPERATOR},
                ENV_KEY: {"Account": ACCOUNT, "Arn": f"arn:aws:sts::{ACCOUNT}:assumed-role/SimSSO/operator"},
                OTHER_KEY: {"Account": "111111111111", "Arn": "arn:aws:iam::111111111111:user/someone"},
            },
            "roles": {
                "cce-a09-cfn-execution": svc_role("cce-a09-cfn-execution", "cloudformation.amazonaws.com"),
                "cce-a09-watchdog": svc_role("cce-a09-watchdog", "scheduler.amazonaws.com"),
            },
            "quota": 4.0,
            "offered_azs": [AZ],
            "subnets": [{"SubnetId": SUBNET, "VpcId": VPC, "AvailabilityZone": AZ, "State": "available"}],
            "route_tables": [
                {
                    "subnet": SUBNET,
                    "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-0sim", "State": "active"}],
                }
            ],
            "images": [
                {
                    "ImageId": AMI,
                    "Name": "Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 24.04) 20261119",
                    "ImageOwnerAlias": "amazon",  # the DescribeImages field name (real response shape)
                    "OwnerId": "898082745236",
                    "Public": True,
                    "State": "available",
                    "Architecture": "x86_64",
                }
            ],
            "params": {AMI_PARAM: {"Value": AMI, "Type": "String"}},
            "stacks": {BASE: base},
            "instances": {},
            "volumes": {},
            "schedules": {},
        }
        st.update(over)
        (self.fake / "state.json").write_text(json.dumps(st, indent=1))

    def state(self) -> dict[str, Any]:
        return json.loads((self.fake / "state.json").read_text())

    def patch_state(self, fn: Any) -> None:
        st = self.state()
        fn(st)
        (self.fake / "state.json").write_text(json.dumps(st, indent=1))

    def host_reads_key_from_ssm(self, stack: str) -> None:
        """server.env as bootstrap.sh writes it on AWS: the API key comes from SSM at every server start."""
        with self.env_file.open("a") as fh:
            fh.write(f"CCE_API_KEY_FILE={self.tmp / 'host-api-key'}\n")
            fh.write(f"CCE_API_KEY_SSM_PARAM=/cce/inference/validation/{stack}/api-key\nCCE_AWS_REGION=us-east-1\n")

    def write_request(self, doc: dict[str, Any]) -> Path:
        path = self.tmp / "requests" / f"{doc['id']}.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(doc))
        return path

    def ci(
        self, *args: str, env: dict[str, str] | None = None, timeout: float = 900
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(CI_SCRIPT), *args],
            capture_output=True,
            text=True,
            env={**self.env, **(env or {})},
            timeout=timeout,
            check=False,
        )

    def common(self, req: Path, ev: Path) -> list[str]:
        return [
            "--request",
            str(req),
            "--evidence",
            str(ev),
            "--python",
            sys.executable,
            *(["--profile", self.profile] if self.profile else []),
        ]

    def run_args(self, req: Path, ev: Path | None = None, cmd: str = "run") -> list[str]:
        ev = ev or self.cievidence
        return [cmd, *self.common(req, ev), "--workdir", str(self.work), "--tunnel-port", str(self.port)]

    def run_ci(self, req: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return self.ci(*self.run_args(req), env=env)

    def cleanup(self, req: Path, ev: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return self.ci("cleanup", *self.common(req, ev), env=env)

    def evj(self, name: str, base: Path | None = None) -> Any:
        return json.loads(((base or self.cievidence) / name).read_text())

    def calls(self) -> list[dict[str, Any]]:
        path = self.fake / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def calls_of(self, *words: str) -> int:
        return sum(1 for c in self.calls() if all(w in c["argv"] for w in words))

    def writes(self) -> list[list[str]]:
        """Every AWS call that is not a read (the fake's own `fake ...` hooks apart)."""
        return [
            c["argv"]
            for c in self.calls()
            if len(c["argv"]) > 1
            and c["argv"][0] != "fake"
            and not READ_ONLY.match(c["argv"][1])
            and c["argv"][:2] != ["sts", "get-caller-identity"]
        ]


@pytest.fixture
def cs(tmp_path: Path, fake_model: dict[str, str]):
    s = CiSim(tmp_path, fake_model)
    yield s
    s.sts.server.shutdown()
    subprocess.run([str(SIM_HOST), str(tmp_path), "stop"], check=False)


def all_evidence_bytes(*dirs: Path) -> bytes:
    return b"".join(p.read_bytes() for d in dirs if d.exists() for p in d.rglob("*") if p.is_file())


# --- request schema (offline) -----------------------------------------------------------------------------------


def test_the_example_request_is_valid_fictitious_and_within_its_spend_ceiling():
    req = ci.load_request(ci.EXAMPLE_REQUEST, local=False)
    assert req["stack"]["after_run"] in ("keep-stopped", "delete") and req["model"] == Q8
    assert ci.worst_case_usd(req) <= req["authorization"]["max_spend_usd"]
    assert req["aws"]["account_id"] == "123456789012" and req["network"]["vpc_id"] == "vpc-0123456789abcdef0"
    assert req["parity"] is None
    r = subprocess.run([sys.executable, str(CI_SCRIPT), "check"], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda d: d["stack"].pop("after_run"), "stack: missing ['after_run']"),
        (lambda d: d["stack"].update(after_run="keep"), "stack.after_run"),
        (lambda d: d["authorization"].update(spend_approved=False), "spend_approved and network_approved"),
        (lambda d: d["authorization"].update(max_spend_usd=1), "below the worst case"),
        (lambda d: d["aws"].update(region="us-west-2"), "aws.region"),
        (lambda d: d["aws"].update(instance_type="g6.xlarge"), "aws.instance_type"),
        (lambda d: d["aws"].pop("account_id"), "aws: missing ['account_id']"),
        (lambda d: d["aws"].update(account_id=123456789012), "aws.account_id"),
        (lambda d: d["aws"].update(base_stack="bad name"), "aws.base_stack"),
        (lambda d: d.update(schema_version=1), "schema_version: must be 2"),
        (lambda d: d.update(model="qwen3.6-27b-q4km"), "model: must be qwen3.6-27b-q8_0"),
        (lambda d: d.update(extra=1), "unknown keys ['extra']"),
        (lambda d: d["limits"].update(max_runtime_minutes=600), "limits.max_runtime_minutes"),
        (lambda d: d["limits"].pop("max_runtime_minutes"), "limits: missing"),
        (lambda d: d["network"].update(client_cidr="8.8.8.0/24"), "network.client_cidr"),
        (lambda d: d["validation"].update(profiles=["nope"]), "unknown profiles"),
        (lambda d: d["stack"].update(name="other-stack"), "stack.name"),
        (lambda d: d.update(id="Bad_Id"), "id:"),
        (lambda d: d.update(parity={"s3_uri": "s3://b/k.zip", "sha256": "0" * 64}), "parity: missing ['zip_path']"),
        (lambda d: d.update(parity={"zip_path": "relative/results.zip"}), "parity.zip_path"),
    ],
)
def test_invalid_requests_are_refused_offline(mutate: Any, error: str):
    doc = request()
    mutate(doc)
    errors = ci.validate_request(doc)
    assert any(error in e for e in errors), errors


def test_a_request_inside_the_checkout_is_refused_before_any_aws_call(cs: CiSim):
    inside = REPO / "deploy" / "inference" / "validation" / "request.example.json"
    for cmd in ("plan", "setup", "run", "cleanup"):
        r = cs.ci(cmd, "--request", str(inside), "--evidence", str(cs.tmp / f"ev-{cmd}"))
        assert r.returncode == 2 and "OUTSIDE the checkout" in r.stderr, r.stderr
    assert cs.calls() == []


def test_an_invalid_request_reads_and_writes_nothing_in_aws(cs: CiSim):
    doc = request()
    doc["authorization"]["network_approved"] = False
    req = cs.write_request(doc)
    for cmd in ("plan", "setup", "run", "cleanup"):
        r = cs.ci(cmd, *cs.common(req, cs.tmp / f"ev-{cmd}"))
        assert r.returncode == 2 and "INVALID REQUEST" in r.stderr, r.stderr
    assert cs.calls() == [] and cs.sts.calls == []


# --- end to end with simulated AWS --------------------------------------------------------------------------------


def test_new_request_runs_end_to_end_and_deletes_with_verification(cs: CiSim):
    doc = request()
    cs.host_reads_key_from_ssm(doc["stack"]["name"])
    req = cs.write_request(doc)
    r = cs.run_ci(req)
    assert r.returncode == 0, r.stdout + r.stderr + (cs.cievidence / "gpu-validation.log").read_text()
    out = cs.evj("outcome.json")
    assert out["validation"] == "validated" and out["exit"] == 0 and out["cleanup_in_run"] == "verified"
    # preflight checked quota, network, AMI, template and recorded them; the id was claimed before anything else
    pre = cs.evj("preflight.json")
    assert pre["quota"]["g_vt_vcpu_quota"] == 4.0 and pre["network"]["outbound"] == "igw"
    assert pre["ami"]["ImageId"] == AMI and pre["checks"]["template_up_to_date"] is True
    assert pre["parity"]["status"] == "pending"
    # identity: the same operator for the CLI and for boto3, in us-east-1 and the request's account, checked first
    assert pre["identity"]["arn"] == OPERATOR == pre["identity"]["boto3_arn"]
    assert pre["identity"]["account"] == ACCOUNT and pre["identity"]["boto3_region"] == "us-east-1"
    calls = cs.calls()
    first_write = next(i for i, c in enumerate(calls) if c["argv"] in cs.writes())
    assert calls[0]["argv"][:2] == ["sts", "get-caller-identity"] and first_write > 0
    # every AWS call, the children's included (gpu-validation.py, ssm-run.sh, the controller), used the same
    # credentials and region; boto3 resolved the same key through its own chain
    assert {c["key"] for c in calls} == {PROFILE_KEY} and {c["region"] for c in calls} == {"us-east-1"}
    assert cs.sts.calls and {c["key"] for c in cs.sts.calls} == {PROFILE_KEY}
    assert {c["region"] for c in cs.sts.calls} == {"us-east-1"}
    # no GitHub, no OIDC: the gh trap was never called and no web-identity call was made
    assert not cs.gh_trap.exists() and not any("web-identity" in " ".join(c["argv"]) for c in calls)
    claim0 = cs.evj("claim.json")
    assert claim0["operator"] == OPERATOR and claim0["run_id"].startswith("local-") and claim0["checkout"]["commit"]
    st = cs.state()
    claim = json.loads(st["params"][f"/cce/inference/validation-runs/{doc['id']}"]["Value"])
    assert claim["id"] == doc["id"] and claim["status"] == "validated" and claim["cleanup"] == "verified"
    # step A: the stack with the request's parameters, through the execution role, the watchdog armed in AWS
    stack = st["stacks"][doc["stack"]["name"]]
    assert stack["role"] == ACCESS["CfnExecutionRoleArn"]
    p = stack["params"]
    assert (p["InstanceType"], p["ImageId"], p["SubnetId"], p["AvailabilityZone"]) == ("g6e.xlarge", AMI, SUBNET, AZ)
    assert p["HostPermissionsBoundaryArn"] == ACCESS["HostPermissionsBoundaryArn"] and p["ClientCidr"] == ""
    assert stack["tags"]["cce:validation-id"] == doc["id"] and stack["tags"]["cce:after-run"] == "delete"
    # the AWS-side stop is a resource of the stack (created by CloudFormation with the instance), bound to the limit
    wd = cs.evj("watchdog.json")
    assert wd["kind"] == "in_stack" and wd["targets"] == ["i-0sim"] and wd["every_minutes"] == 10
    claimed = ci.dt.datetime.fromisoformat(claim["claimed_at"].replace("Z", "+00:00")).timestamp()
    start = ci.dt.datetime.fromisoformat(p["WatchdogStart"].replace("Z", "+00:00")).timestamp()
    assert abs(start - (claimed + (60 + ci.WATCHDOG_GRACE_MIN) * 60)) <= 5  # spend limit: max_runtime + grace
    assert p["WatchdogRoleArn"] == ACCESS["WatchdogRoleArn"] and stack["tags"]["cce:claim-token"] == claim["token"]
    prov = cs.evj("provision.json")
    tl = prov["timeline"]
    assert tl["create_stack"] <= tl["instance_created"] <= tl["bootstrap_done"] <= tl["stopped_for_validation"]
    assert prov["first_load_gpu_offload"]["status"] == "verified"
    assert "units installed; server started" in (cs.cievidence / "bootstrap.log").read_text()
    # step B: gpu-validation.py from stopped, job-driven start, GPU offload log, verdicts, verified stop
    gv = out["gpu_validation"]
    assert gv["outcome"] == "validated" and gv["verdicts"]["reference-8k-1slot"]["verdict"] == "pass"
    assert out["gpu_offload"]["job-cycle"]["status"] == "verified"
    assert (
        "offloaded 65/65 layers to GPU"
        in (cs.cievidence / "gpu-validation" / "server-journal-job-cycle.log").read_text()
    )
    assert cs.evj("gpu-validation/final-state.json")["instance_stopped"] is True
    assert out["parity"]["status"] == "pending" and "not configured" in out["parity"]["reason"]
    # cleanup (delete): stack gone, instance terminated, no volume, API key parameter deleted, watchdog disarmed
    clean = cs.evj("cleanup-run.json")
    assert clean["outcome"] == "verified" and clean["stack_deleted"] is True and clean["volumes_left"] == []
    assert clean["api_key_parameter_deleted"] is True and clean["watchdog"] == "no_runner_schedule"
    assert clean["ownership"] == "owned" and clean["owner_record"] == "deleted"
    st = cs.state()
    assert not any(k.startswith(ci.OWNER_PREFIX) for k in st["params"])
    assert doc["stack"]["name"] not in [n for n, s in st["stacks"].items() if s["status"] != "DELETE_COMPLETE"]
    assert st["instances"]["i-0sim"].get("terminated") and st["schedules"] == {} and st["volumes"] == {}
    # a separate `cleanup` (idempotent) verifies the same
    r2 = cs.cleanup(req, cs.tmp / "ev-cleanup")
    assert r2.returncode == 0, r2.stderr
    assert cs.evj("cleanup.json", cs.tmp / "ev-cleanup")["outcome"] == "verified"
    # never a secret in the evidence: the endpoint key (from the fake SSM) and no file named like a key
    key = st["params"].get(f"/cce/inference/validation/{doc['stack']['name']}/api-key")
    assert key is None  # deleted with the stack
    assert not list(cs.cievidence.rglob("api-key*"))
    assert cs.calls_of("ec2", "start-instances") == 0  # the controller started it (command backend), by demand
    # like the real host, every server start read the key from SSM (run-server.sh), never from the user data
    assert cs.calls_of("ssm", "get-parameter", "--with-decryption", "Parameter.Value") >= 2


def test_keep_stopped_then_retry_spends_nothing_then_a_new_request_reuses_the_stack(cs: CiSim):
    doc = request(
        "a09-1-sim-keep", stack={"name": "cce-inference-a09-keep", "mode": "create", "after_run": "keep-stopped"}
    )
    cs.host_reads_key_from_ssm(doc["stack"]["name"])
    req = cs.write_request(doc)
    r = cs.run_ci(req)
    assert r.returncode == 0, r.stdout + r.stderr
    clean = cs.evj("cleanup-run.json")
    assert clean["outcome"] == "verified" and clean["kept"] is True
    assert clean["instance"] == {"id": "i-0sim", "state": "stopped"} and clean["ownership"] == "owned"
    assert "EBS" in clean["residual"] and clean["watchdog"] == "no_runner_schedule"
    st = cs.state()
    (sched,) = st["schedules"].values()  # the in-stack watchdog stays with the kept stack, inert after its end date
    assert sched["created_by"] == "cloudformation" and sched["end-date"] > sched["start-date"]
    owner = json.loads(st["params"][ci.OWNER_PREFIX + doc["stack"]["name"]]["Value"])
    assert owner["owner_id"] == doc["id"] and owner["instance_id"] == "i-0sim" and owner["stack_id"]
    assert st["stacks"][doc["stack"]["name"]]["status"] == "CREATE_COMPLETE" and st["volumes"]
    key_param = f"/cce/inference/validation/{doc['stack']['name']}/api-key"
    key = st["params"][key_param]["Value"]
    assert st["params"][key_param]["Type"] == "SecureString" and len(key) == 64
    assert key.encode() not in all_evidence_bytes(cs.cievidence)  # used by the host and this process, never saved
    creates = cs.calls_of("cloudformation", "create-stack")

    # retry of the same request (the same id run again): no new spend, cleanup re-verified
    retry_ev = cs.tmp / "ev-retry"
    r = cs.ci(*cs.run_args(req, retry_ev))
    assert r.returncode == 3, r.stdout + r.stderr
    assert "DUPLICATE" in r.stderr and "no new spend" in r.stderr
    dup = cs.evj("duplicate.json", retry_ev)
    assert dup["claim"]["id"] == doc["id"] and dup["claim"]["status"] == "validated"
    assert cs.calls_of("cloudformation", "create-stack") == creates and cs.host_state() == "stopped"
    assert not (retry_ev / "claim.json").exists()
    r = cs.cleanup(req, cs.tmp / "ev-retry-cleanup")
    assert r.returncode == 0 and cs.evj("cleanup.json", cs.tmp / "ev-retry-cleanup")["kept"] is True

    # a NEW request reuses the kept stack (no bootstrap), then deletes it
    doc2 = request("a09-1-sim-reuse", stack={"name": doc["stack"]["name"], "mode": "reuse", "after_run": "delete"})
    req2 = cs.write_request(doc2)
    ev2 = cs.tmp / "ev-reuse"
    r = cs.ci(*cs.run_args(req2, ev2))
    assert r.returncode == 0, r.stdout + r.stderr
    assert cs.calls_of("cloudformation", "create-stack") == creates  # reused, not created
    assert cs.evj("outcome.json", ev2)["validation"] == "validated"
    assert cs.evj("provision.json", ev2)["owner"]["adopted_from"] == doc["id"]  # explicit adoption
    assert cs.evj("watchdog.json", ev2)["kind"] == "runner"  # armed by this process before any start
    clean2 = cs.evj("cleanup-run.json", ev2)
    assert clean2["stack_deleted"] is True and cs.state()["volumes"] == {}
    assert clean2["ownership"] == f"owned (adopted from {doc['id']})"
    assert clean2["watchdog"] == "runner_schedule_disarmed"


@pytest.mark.parametrize(
    ("case", "expect"),
    [
        ("quota_zero", "quota: 0 vCPU"),
        ("other_gpu_running", "with 4 in use"),
        ("az_mismatch", "the request says"),
        ("no_route", "no active 0.0.0.0/0 route"),
        ("igw_without_public_ip", "assign_public_ip must be true"),
        ("not_offered", "is not offered"),
        ("stack_exists", "exists"),
        ("base_subnet_differs", f"base stack {BASE} is not ready (network differs"),
        ("no_base_stack", f"base stack {BASE} is not ready (it does not exist"),
        ("base_keeps_a_ci_role", "roles trusted by a federated principal"),
        ("bad_ami", "is not an available Amazon"),
        ("ami_without_owner_alias", "is not an available Amazon"),
        ("no_plugin", "session-manager-plugin is not installed"),
        ("parity_sha_mismatch", "parity ZIP sha256"),
    ],
)
def test_preflight_refusals_create_nothing_and_claim_nothing(cs: CiSim, case: str, expect: str):
    doc = request()
    env: dict[str, str] = {}
    if case == "quota_zero":
        cs.patch_state(lambda s: s.update(quota=0.0))
    elif case == "other_gpu_running":
        env["FAKE_AWS_OTHER_GPU"] = "i-0other:g6e.xlarge:running"
    elif case == "az_mismatch":
        cs.patch_state(lambda s: s["subnets"][0].update(AvailabilityZone="us-east-1b"))
    elif case == "no_route":
        cs.patch_state(lambda s: s.update(route_tables=[]))
    elif case == "igw_without_public_ip":
        doc["network"]["assign_public_ip"] = False
    elif case == "not_offered":
        cs.patch_state(lambda s: s.update(offered_azs=["us-east-1c"]))
    elif case == "stack_exists":
        cs.patch_state(lambda s: s["stacks"].update({doc["stack"]["name"]: {"status": "CREATE_COMPLETE"}}))
    elif case == "base_subnet_differs":
        cs.patch_state(lambda s: s["stacks"][BASE]["outputs"].update(AllowedSubnetId="subnet-0ff"))
    elif case == "no_base_stack":
        cs.patch_state(lambda s: s["stacks"].pop(BASE))
    elif case == "base_keeps_a_ci_role":
        seed_legacy_ci_role(cs)
    elif case == "bad_ami":
        cs.patch_state(lambda s: s["images"][0].update(ImageOwnerAlias="someone"))
    elif case == "ami_without_owner_alias":  # e.g. a copy in another account: no `ImageOwnerAlias`
        cs.patch_state(lambda s: s["images"][0].pop("ImageOwnerAlias"))
    elif case == "no_plugin":
        (cs.bin / "session-manager-plugin").unlink()
        env["PATH"] = f"{cs.bin}:/usr/bin:/bin"
    elif case == "parity_sha_mismatch":
        zpath = parity_zip(cs.tmp / "local" / "results.zip")
        doc["parity"] = {"zip_path": str(zpath), "sha256": "0" * 64}
    req = cs.write_request(doc)
    r = cs.run_ci(req, env=env)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "REFUSED (nothing claimed or created)" in r.stderr and expect in r.stderr, r.stderr
    st = cs.state()
    assert not any(k.startswith("/cce/inference/validation-runs/") for k in st["params"])  # id not burned
    assert cs.writes() == [] and cs.calls_of("cloudformation", "create-stack") == 0
    assert cs.evj("outcome.json")["validation"] == "refused"
    assert "VALIDATION (tests): refused" in r.stdout and "CLEANUP:            not_needed" in r.stdout


def parity_zip(path: Path, *, prefix: str = "cawbase_resultados/") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{prefix}CAWbase_benchmark_v1.1/benchmark.py", "print(1)\n")
        z.writestr(f"{prefix}corrida_nocturna/entradas/evaluacion.jsonl", "{}\n")
    return path


def test_parity_zip_is_read_from_a_local_path_verified_and_its_corpus_outputs_kept_out(cs: CiSim, tmp_path: Path):
    """A local ZIP is verified (SHA-256, safe paths, layout) before anything is created and unpacked in the work
    directory; parity outputs that reproduce the corpus are moved out of the evidence (their hashes are recorded).
    parity.sh itself is exercised against real data only on AWS. Without a ZIP, parity is recorded as pending."""
    zpath = parity_zip(tmp_path / "local" / "results.zip")
    sha = hashlib.sha256(zpath.read_bytes()).hexdigest()
    info = ci.check_parity_zip({"zip_path": str(zpath), "sha256": sha})
    assert info == {"file": "results.zip", "sha256": sha, "package_dir": "cawbase_resultados"}
    assert ci.check_parity_zip({"zip_path": str(zpath)})["sha256"] == sha  # sha256 optional: recorded
    run = ci.Run(request(parity={"zip_path": str(zpath), "sha256": sha}), cs.cievidence, cs.work)
    pkg = run.unpack_parity()
    assert (pkg / "CAWbase_benchmark_v1.1").is_dir() and cs.cievidence not in pkg.parents
    with zipfile.ZipFile(tmp_path / "evil.zip", "w") as z:
        z.writestr("../escape.txt", "x")
    with pytest.raises(ci.Refused, match="unsafe path"):
        ci.check_parity_zip({"zip_path": str(tmp_path / "evil.zip")})
    with pytest.raises(ci.Refused, match="not found"):
        ci.check_parity_zip({"zip_path": str(tmp_path / "missing.zip")})
    # --parity-zip on the command line: verified by the plan (read-only)
    req = cs.write_request(request())
    r = cs.ci(*cs.run_args(req, cs.tmp / "ev-plan", cmd="plan"), "--parity-zip", str(zpath))
    assert r.returncode == 0, r.stdout + r.stderr
    assert cs.evj("plan.json", cs.tmp / "ev-plan")["parity"]["status"] == "configured" and cs.writes() == []
    par = cs.cievidence / "gpu-validation" / "parity-reference-8k-1slot"
    par.mkdir(parents=True)
    (par / "evaluacion-r1-i1.jsonl").write_text('{"salida": "corpus content"}\n')
    (par / "summary.json").write_text('{"strict_correct_per_rep": [40]}')
    moved = ci.withhold(cs.cievidence, cs.work / "withheld")
    assert [m["file"] for m in moved] == ["gpu-validation/parity-reference-8k-1slot/evaluacion-r1-i1.jsonl"]
    assert not (par / "evaluacion-r1-i1.jsonl").exists() and (par / "summary.json").exists()
    assert json.loads((cs.cievidence / "withheld.json").read_text())["files"][0]["sha256"]


def test_validation_failure_and_cleanup_are_reported_apart(cs: CiSim):
    """The measurement fails (validation failed, exit 1) while the cleanup is verified (and `cleanup` says so)."""
    doc = request()
    cs.host_reads_key_from_ssm(doc["stack"]["name"])
    req = cs.write_request(doc)
    r = cs.run_ci(req, env={"FAKE_MEASURE_FAIL": "run:run-reference-8k-1slot-c1"})
    assert r.returncode == 1, r.stdout + r.stderr
    out = cs.evj("outcome.json")
    assert out["validation"] == "failed" and out["gpu_validation_exit"] == 1 and out["cleanup_in_run"] == "verified"
    assert "VALIDATION (tests): failed (exit 1)" in r.stdout and "CLEANUP:            verified" in r.stdout
    assert out["gpu_validation"]["verdicts"]["reference-8k-1slot"]["verdict"] == "fail"
    r2 = cs.cleanup(req, cs.tmp / "ev-cleanup")
    assert r2.returncode == 0 and cs.evj("cleanup.json", cs.tmp / "ev-cleanup")["outcome"] == "verified"
    claim = json.loads(cs.state()["params"][f"/cce/inference/validation-runs/{doc['id']}"]["Value"])
    assert claim["status"] == "failed" and claim["cleanup"] == "verified" and claim["cleanup_job"] == "verified"


def test_partial_gpu_offload_at_first_load_fails_before_step_b(cs: CiSim):
    doc = request()
    cs.host_reads_key_from_ssm(doc["stack"]["name"])
    r = cs.run_ci(cs.write_request(doc), env={"FAKE_LLAMA_OFFLOAD": "40/65"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert cs.evj("bootstrap-gpu-offload.json")["status"] == "partial"
    out = cs.evj("outcome.json")
    assert "not every layer on the GPU" in out["validation"] and "gpu_validation_exit" not in out
    assert cs.evj("cleanup-run.json")["outcome"] == "verified"


def test_cleanup_failure_is_its_own_failure_and_keeps_the_watchdog(cs: CiSim):
    """Bootstrap fails (validation failed) AND the stack deletion fails: the cleanup fails on its own (exit 5),
    the instance is still verified stopped and the AWS-side watchdog stays armed."""
    doc = request()
    req = cs.write_request(doc)
    env = {"FAKE_AWS_BOOTSTRAP": "fail", "FAKE_AWS_DELETE_FAIL": "1"}
    r = cs.run_ci(req, env=env)
    assert r.returncode == 5, r.stdout + r.stderr  # the cleanup decides the exit status when it failed
    out = cs.evj("outcome.json")
    assert "bootstrap failed on the host" in out["validation"] and out["cleanup_in_run"] == "FAILED"
    assert out["exit"] == 1  # the validation's own result is kept apart
    assert "VALIDATION (tests): failed: bootstrap failed" in r.stdout and "CLEANUP:            FAILED" in r.stdout
    assert "CLEANUP FAILED: repeat" in r.stdout
    r2 = cs.cleanup(req, cs.tmp / "ev-cleanup", env=env)
    assert r2.returncode == 5 and "CLEANUP FAILED" in r2.stderr
    clean = cs.evj("cleanup.json", cs.tmp / "ev-cleanup")
    assert clean["outcome"] == "FAILED" and clean["volumes_left"] == ["vol-0simweights"]
    assert clean["watchdog"] == "kept_armed" and any("stack_not_deleted" in e for e in clean["errors"])
    assert cs.state()["schedules"]  # still armed in AWS
    claim = json.loads(cs.state()["params"][f"/cce/inference/validation-runs/{doc['id']}"]["Value"])
    assert claim["cleanup_job"] == "FAILED"


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_an_interruption_during_bootstrap_cleans_up_and_exits_130(cs: CiSim, sig: signal.Signals):
    """Ctrl-C (SIGINT) or a terminated terminal/session (SIGTERM): the cleanup still runs and is verified."""
    req = cs.write_request(request())
    p = subprocess.Popen(
        [sys.executable, str(CI_SCRIPT), *cs.run_args(req)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**cs.env, "FAKE_AWS_BOOTSTRAP": "hang"},
    )
    try:
        wait_until(lambda: (cs.cievidence / "bootstrap.log").exists(), timeout=120)
        time.sleep(1)
        p.send_signal(sig)
        out, err = p.communicate(timeout=300)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 130, out + err
    assert cs.evj("outcome.json")["validation"].startswith("interrupted")
    assert "CLEANUP:            verified" in out
    clean = cs.evj("cleanup-run.json")
    assert clean["outcome"] == "verified" and clean["stack_deleted"] is True
    assert clean["watchdog"] == "no_runner_schedule" and cs.state()["schedules"] == {}  # in-stack: gone with it
    assert cs.host_state() == "stopped"


def test_process_killed_the_aws_side_watchdog_still_stops_the_instance(cs: CiSim):
    """SIGKILL (or the laptop gone): no finally, no in-run cleanup. The schedule armed in AWS stops the instance on
    its own; `cleanup`, when it runs, finishes the deletion."""
    req = cs.write_request(request())
    p = subprocess.Popen(
        [sys.executable, str(CI_SCRIPT), *cs.run_args(req)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**cs.env, "FAKE_AWS_BOOTSTRAP": "hang"},
    )
    try:
        wait_until(lambda: (cs.cievidence / "bootstrap.log").exists(), timeout=120)
        p.kill()
        p.wait(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert cs.host_state() == "running" and not (cs.cievidence / "cleanup-run.json").exists()
    sched = next(iter(cs.state()["schedules"].values()))
    assert json.loads(sched["target"]["Input"]) == {"InstanceIds": ["i-0sim"]}
    assert sched["created_by"] == "cloudformation" and sched["schedule-expression"] == "rate(10 minutes)"
    assert sched["target"]["RoleArn"] == ACCESS["WatchdogRoleArn"]
    subprocess.run(
        [str(cs.tmp / "aws"), "fake", "fire-schedules"], env=cs.env, check=True
    )  # EventBridge, no local process
    assert cs.host_state() == "stopped"
    r = cs.cleanup(req, cs.tmp / "ev-cleanup")
    assert r.returncode == 0, r.stderr
    assert cs.evj("cleanup.json", cs.tmp / "ev-cleanup")["stack_deleted"] is True and cs.state()["schedules"] == {}


# --- secrets never reach the evidence -------------------------------------------------------------------------


def test_redaction_removes_every_secret_and_files_named_like_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ev = tmp_path / "ev"
    (ev / "sub").mkdir(parents=True)
    key = "k" * 64
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "SECRETAWSVALUE123")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "SESSIONTOKENVALUE456")
    keyfile = tmp_path / "api-key"
    keyfile.write_text(key + "\n")
    (ev / "log.txt").write_text(f"Authorization: Bearer {key} and SECRETAWSVALUE123\n")
    (ev / "sub" / "state.db").write_bytes(b"\x00\x01" + key.encode() + b"\x00SESSIONTOKENVALUE456")
    (ev / "sub" / "api-key").write_text(key)
    (ev / "ok.json").write_text('{"fine": true}')
    n = ci.redact(ev, [keyfile])
    assert n >= 4
    data = all_evidence_bytes(ev)
    assert key.encode() not in data and b"SECRETAWSVALUE123" not in data and b"SESSIONTOKENVALUE456" not in data
    assert not (ev / "sub" / "api-key").exists() and (ev / "ok.json").read_text() == '{"fine": true}'
    assert len((ev / "sub" / "state.db").read_bytes()) == 2 + 64 + 1 + 20  # binary keeps its length


# --- ownership: the cleanup touches only what the request created or explicitly adopted ------------------------


def seed_foreign_stack(cs: CiSim, name: str, *, running: bool = True) -> str:
    """A stack that this request did not create (manual, or another run's), with its instance."""
    sid = f"arn:aws:cloudformation:us-east-1:000000000000:stack/{name}/foreign"

    def pre(s: dict[str, Any]) -> None:
        s["stacks"][name] = {"id": sid, "status": "CREATE_COMPLETE", "outputs": {"InstanceId": "i-0sim"}, "tags": {}}
        tags = {"cce:role": "inference", "aws:cloudformation:stack-name": name, "aws:cloudformation:stack-id": sid}
        s["instances"]["i-0sim"] = {"type": "g6e.xlarge", "tags": tags}
        s["volumes"]["vol-0simweights"] = {"stack": name, "stack_id": sid}

    cs.patch_state(pre)
    if running:
        subprocess.run([str(SIM_HOST), str(cs.tmp), "start"], check=True, env=cs.env)
    return sid


def assert_untouched(cs: CiSim, name: str, sid: str) -> None:
    st = cs.state()
    assert st["stacks"][name]["status"] == "CREATE_COMPLETE" and st["stacks"][name]["id"] == sid
    assert cs.host_state() == "running" and "vol-0simweights" in st["volumes"]
    assert cs.calls_of("delete-stack") == 0 and cs.calls_of("stop-instances") == 0


def test_an_unclaimed_request_never_stops_or_deletes_anything(cs: CiSim):
    """A create+delete request naming an existing stack is refused by the preflight (nothing claimed); a later
    `cleanup` of that request must not touch the stack (defect of 09c9429)."""
    doc = request()
    sid = seed_foreign_stack(cs, doc["stack"]["name"])
    req = cs.write_request(doc)
    r = cs.run_ci(req)
    assert r.returncode == 2, r.stdout + r.stderr
    r2 = cs.cleanup(req, cs.tmp / "ev-cleanup")
    assert r2.returncode == 0, r2.stderr
    clean = cs.evj("cleanup.json", cs.tmp / "ev-cleanup")
    assert clean["ownership"].startswith("not_claimed") and clean["watchdog"] == "untouched"
    assert_untouched(cs, doc["stack"]["name"], sid)


def claim_and_owner(cs: CiSim, doc: dict[str, Any], *, owner_id: str, stack_id: str | None) -> None:
    def pre(s: dict[str, Any]) -> None:
        s["params"][ci.LEDGER_PREFIX + doc["id"]] = {
            "Type": "String",
            "Value": json.dumps({"id": doc["id"], "token": "t-" + doc["id"], "run_id": "1", "status": "failed"}),
        }
        s["params"][ci.OWNER_PREFIX + doc["stack"]["name"]] = {
            "Type": "String",
            "Value": json.dumps(
                {"owner_id": owner_id, "token": "t-" + owner_id, "stack_id": stack_id, "instance_id": "i-0sim"}
            ),
        }

    cs.patch_state(pre)


def test_a_claimed_request_preserves_a_stack_recreated_under_its_name(cs: CiSim):
    """The request owned a stack that is gone; another stack now has the same name: its StackId differs."""
    doc = request()
    name = doc["stack"]["name"]
    sid = seed_foreign_stack(cs, name)
    old = f"arn:aws:cloudformation:us-east-1:000000000000:stack/{name}/old"
    claim_and_owner(cs, doc, owner_id=doc["id"], stack_id=old)
    r = cs.cleanup(cs.write_request(doc), cs.tmp / "ev-cleanup")
    assert r.returncode == 0, r.stderr
    clean = cs.evj("cleanup.json", cs.tmp / "ev-cleanup")
    assert "is another stack: preserved" in clean["ownership"] and clean["instance"]["preserved"]
    assert_untouched(cs, name, sid)


def test_a_claimed_request_preserves_a_stack_adopted_by_another(cs: CiSim):
    doc = request()
    name = doc["stack"]["name"]
    sid = seed_foreign_stack(cs, name)
    claim_and_owner(cs, doc, owner_id="a09-1-later-002", stack_id=sid)
    r = cs.cleanup(cs.write_request(doc), cs.tmp / "ev-cleanup")
    assert r.returncode == 0, r.stderr
    assert "owned by 'a09-1-later-002'" in cs.evj("cleanup.json", cs.tmp / "ev-cleanup")["ownership"]
    assert_untouched(cs, name, sid)


def test_retry_of_an_old_request_preserves_resources_adopted_later(cs: CiSim):
    """id1 creates and keeps the stack; id2 adopts it with reuse and keeps it; the instance is then in use (A09-2).
    Re-running id1 (its validation and its cleanup, keep-stopped as versioned or even a delete variant) must not
    stop or delete it; the current owner still can."""
    name = "cce-inference-a09-shared"
    doc1 = request("a09-1-sim-old", stack={"name": name, "mode": "create", "after_run": "keep-stopped"})
    cs.host_reads_key_from_ssm(name)
    req1 = cs.write_request(doc1)
    r = cs.run_ci(req1)
    assert r.returncode == 0, r.stdout + r.stderr
    doc2 = request("a09-1-sim-new", stack={"name": name, "mode": "reuse", "after_run": "keep-stopped"})
    r = cs.ci(*cs.run_args(cs.write_request(doc2), cs.tmp / "ev2"))
    assert r.returncode == 0, r.stdout + r.stderr
    owner = json.loads(cs.state()["params"][ci.OWNER_PREFIX + name]["Value"])
    assert owner["owner_id"] == doc2["id"] and owner["adopted_from"] == doc1["id"]
    subprocess.run([str(SIM_HOST), str(cs.tmp), "start"], check=True, env=cs.env)  # in use after the validation
    stops, deletes = cs.calls_of("stop-instances"), cs.calls_of("delete-stack")
    assert cs.ci(*cs.run_args(req1, cs.tmp / "ev-retry")).returncode == 3  # no spend
    for variant, after in (("keep", "keep-stopped"), ("del", "delete")):
        doc1v = {**doc1, "stack": {**doc1["stack"], "after_run": after}}
        r = cs.cleanup(cs.write_request(doc1v), cs.tmp / f"ev-old-{variant}")
        assert r.returncode == 0, r.stderr
        clean = cs.evj("cleanup.json", cs.tmp / f"ev-old-{variant}")
        assert clean["ownership"].startswith("not_owned") and doc2["id"] in clean["ownership"]
    assert cs.host_state() == "running" and cs.state()["stacks"][name]["status"] == "CREATE_COMPLETE"
    assert cs.calls_of("stop-instances") == stops and cs.calls_of("delete-stack") == deletes
    doc2d = {**doc2, "stack": {**doc2["stack"], "after_run": "delete"}}
    r = cs.cleanup(cs.write_request(doc2d), cs.tmp / "ev-2")
    assert r.returncode == 0 and cs.evj("cleanup.json", cs.tmp / "ev-2")["stack_deleted"] is True


def test_reuse_refuses_a_stack_without_a_matching_ownership_record(cs: CiSim):
    name = "cce-inference-manual"
    doc = request(stack={"name": name, "mode": "reuse", "after_run": "delete"})
    sid = seed_foreign_stack(cs, name, running=False)
    sha = ci.sha256_file(ci.TEMPLATE)
    cs.patch_state(lambda s: s["stacks"][name]["tags"].update({"cce:template-sha256": sha}))
    r = cs.run_ci(cs.write_request(doc))
    assert r.returncode == 2 and "no matching ownership record" in r.stderr, r.stderr
    assert cs.state()["stacks"][name]["id"] == sid and cs.calls_of("delete-stack") == 0


# --- the AWS-side stop exists from the creation of the instance, without this process --------------------------


def test_process_lost_right_after_create_stack_the_instance_is_stopped_without_it_or_cleanup(cs: CiSim):
    """AWS accepts CreateStack and the process dies before it receives the StackId (SIGKILL from the fake, at that
    exact point). CloudFormation keeps creating: the instance appears later, together with the in-stack schedule
    bound to the spend limit, and EventBridge stops it. No local process, no cleanup."""
    doc = request()
    req = cs.write_request(doc)
    p = subprocess.Popen(
        [sys.executable, str(CI_SCRIPT), *cs.run_args(req)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**cs.env, "FAKE_AWS_CREATE_DELAY_S": "1.5", "FAKE_AWS_KILL_CALLER_AFTER": "create-stack"},
    )
    assert p.wait(timeout=120) == -signal.SIGKILL
    st = cs.state()
    stack = st["stacks"][doc["stack"]["name"]]
    assert stack["status"] == "CREATE_IN_PROGRESS" and "i-0sim" not in st["instances"]  # accepted, nothing yet
    owner = json.loads(st["params"][ci.OWNER_PREFIX + doc["stack"]["name"]]["Value"])
    assert owner["owner_id"] == doc["id"] and owner["stack_id"] is None  # the process never learned the id
    assert not (cs.cievidence / "provision.json").exists() and not (cs.cievidence / "cleanup-run.json").exists()
    # later, on its own, CloudFormation creates the instance and the schedule that targets it
    wait_until(lambda: cs.state()["stacks"][doc["stack"]["name"]]["status"] == "CREATE_COMPLETE", timeout=60)
    assert cs.host_state() == "running"
    (sched,) = cs.state()["schedules"].values()
    assert sched["created_by"] == "cloudformation"
    assert json.loads(sched["target"]["Input"]) == {"InstanceIds": ["i-0sim"]}
    claim = json.loads(cs.state()["params"][ci.LEDGER_PREFIX + doc["id"]]["Value"])
    claimed = ci.dt.datetime.fromisoformat(claim["claimed_at"].replace("Z", "+00:00")).timestamp()
    start = ci.dt.datetime.fromisoformat(sched["start-date"].replace("Z", "+00:00")).timestamp()
    assert start <= claimed + (60 + ci.WATCHDOG_GRACE_MIN) * 60 + 5  # within the request's spend limit
    subprocess.run([str(cs.tmp / "aws"), "fake", "fire-schedules"], env=cs.env, check=True)  # EventBridge only
    assert cs.host_state() == "stopped"
    assert cs.calls_of("stop-instances") == 0 and cs.calls_of("delete-stack") == 0  # neither this process nor a cleanup
    # a later `cleanup` still finds it as this request's (claim token tag) and deletes it by StackId
    r = cs.cleanup(req, cs.tmp / "ev-cleanup")
    assert r.returncode == 0, r.stderr
    clean = cs.evj("cleanup.json", cs.tmp / "ev-cleanup")
    assert clean["ownership"] == "owned" and clean["stack_deleted"] is True and clean["stack_id"] == stack["id"]


# --- identity: the same operator everywhere, the expected account, checked before any write ------------------------


def seed_legacy_ci_role(cs: CiSim) -> None:
    """The base stack as the former GitHub enablement left it: an extra role trusted by GitHub's OIDC provider."""

    def pre(s: dict[str, Any]) -> None:
        base = s["stacks"][BASE]
        base["resources"]["ValidationRole"] = {"type": "AWS::IAM::Role", "physical": "cce-a09-github-validation"}
        base["params"].update(GitHubSubject="repo:example-owner/example-repo:ref:refs/heads/main")
        base["template"] = "legacy template with ValidationRole"
        base["outputs"]["ValidationRoleArn"] = f"arn:aws:iam::{ACCOUNT}:role/cce-a09-github-validation"
        stmt = {
            "Effect": "Allow",
            "Principal": {"Federated": f"arn:aws:iam::{ACCOUNT}:oidc-provider/token.actions.githubusercontent.com"},
            "Action": "sts:AssumeRoleWithWebIdentity",
        }
        s["roles"]["cce-a09-github-validation"] = {
            "Arn": f"arn:aws:iam::{ACCOUNT}:role/cce-a09-github-validation",
            "AssumeRolePolicyDocument": {"Statement": [stmt]},
        }

    cs.patch_state(pre)


@pytest.mark.parametrize("cmd", ["plan", "setup", "run", "cleanup"])
def test_a_wrong_account_is_refused_before_any_write(cs: CiSim, cmd: str):
    cs.profile = "sim-other"  # a valid session, but in another account
    req = cs.write_request(request())
    args = cs.run_args(req, cs.tmp / "ev", cmd=cmd) if cmd in ("plan", "run") else [cmd, *cs.common(req, cs.tmp / "ev")]
    if cmd == "setup":
        args.append("--apply")
    r = cs.ci(*args)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "in account 111111111111, the request expects 000000000000" in r.stdout + r.stderr
    assert [c["argv"][:2] for c in cs.calls()] == [["sts", "get-caller-identity"]]  # nothing else, not even reads
    assert cs.writes() == [] and not any(k.startswith("/cce/") for k in cs.state()["params"])


def test_the_cli_and_boto3_must_resolve_the_same_identity(cs: CiSim):
    cs.sts.override = {"Account": ACCOUNT, "Arn": f"arn:aws:iam::{ACCOUNT}:user/someone-else"}
    r = cs.run_ci(cs.write_request(request()))
    assert r.returncode == 2 and "different identities" in r.stderr, r.stderr
    assert cs.writes() == []


def test_no_credentials_is_refused_with_a_hint(cs: CiSim):
    cs.profile = None  # no profile, no environment credentials: nothing in the chain
    r = cs.run_ci(cs.write_request(request()))
    assert r.returncode == 2 and "no usable AWS identity" in r.stderr and "sso login" in r.stderr, r.stderr
    assert cs.writes() == []


def test_a_profile_with_credentials_in_the_environment_is_ambiguous(cs: CiSim):
    env = {"AWS_ACCESS_KEY_ID": ENV_KEY, "AWS_SECRET_ACCESS_KEY": "simsecretenv00000002"}
    r = cs.run_ci(cs.write_request(request()), env=env)
    assert r.returncode == 2 and "ambiguous identity" in r.stderr, r.stderr
    assert cs.calls() == []


def test_temporary_environment_credentials_without_a_profile(cs: CiSim):
    """The active local session (SSO export, assumed role): AWS_ACCESS_KEY_ID + AWS_SESSION_TOKEN, no profile. The
    CLI, boto3 and the children all use it."""
    cs.profile = None
    env = {
        "AWS_ACCESS_KEY_ID": ENV_KEY,
        "AWS_SECRET_ACCESS_KEY": "simsecretenv00000002",
        "AWS_SESSION_TOKEN": "simsessiontoken000000002",
        "AWS_PROFILE": "sim-other",  # ignored by the standard chain when keys are in the environment
    }
    r = cs.ci(*cs.run_args(cs.write_request(request()), cs.tmp / "ev-plan", cmd="plan"), env=env)
    assert r.returncode == 0, r.stdout + r.stderr
    ident = cs.evj("plan.json", cs.tmp / "ev-plan")["identity"]
    assert ident["arn"] == ident["boto3_arn"] == f"arn:aws:sts::{ACCOUNT}:assumed-role/SimSSO/operator"
    assert {c["key"] for c in cs.calls()} == {ENV_KEY} and {c["key"] for c in cs.sts.calls} == {ENV_KEY}


# --- plan: read-only ----------------------------------------------------------------------------------------------


def test_plan_reads_only_and_reports_the_whole_preflight(cs: CiSim):
    req = cs.write_request(request())
    r = cs.ci(*cs.run_args(req, cs.tmp / "ev-plan", cmd="plan"))
    assert r.returncode == 0, r.stdout + r.stderr
    plan = cs.evj("plan.json", cs.tmp / "ev-plan")
    assert plan["plan"].startswith("ready") and plan["base_stack"]["ready"] is True
    assert plan["quota"]["g_vt_vcpu_quota"] == 4.0 and plan["ami"]["ImageId"] == AMI
    assert plan["parity"]["status"] == "pending" and plan["worst_case_instance_usd"] > 0
    assert cs.writes() == [] and cs.state()["stacks"].keys() == {BASE}
    assert not any(k.startswith("/cce/") for k in cs.state()["params"])
    assert not cs.work.exists() or not any(cs.work.rglob("api-key*"))
    # a plan never claims: the run afterwards still can
    r = cs.run_ci(req, env={"FAKE_AWS_BOOTSTRAP": "fail"})
    assert r.returncode == 1 and "claimed" in r.stdout, r.stdout + r.stderr


def test_plan_of_a_claimed_id_reports_the_duplicate_without_writing(cs: CiSim):
    doc = request()
    cs.patch_state(
        lambda s: s["params"].update(
            {ci.LEDGER_PREFIX + doc["id"]: {"Type": "String", "Value": json.dumps({"id": doc["id"], "token": "t"})}}
        )
    )
    r = cs.ci(*cs.run_args(cs.write_request(doc), cs.tmp / "ev-plan", cmd="plan"))
    assert r.returncode == 3 and cs.writes() == []
    assert cs.evj("plan.json", cs.tmp / "ev-plan")["plan"].startswith("duplicate")


# --- base stack setup (idempotent; revokes a former CI/OIDC role) ----------------------------------------------------


def test_setup_creates_the_base_stack_idempotently_and_only_with_apply(cs: CiSim):
    cs.patch_state(lambda s: (s["stacks"].pop(BASE), s.update(roles={})))
    req = cs.write_request(request())
    r = cs.ci("setup", *cs.common(req, cs.tmp / "ev-s1"))
    assert r.returncode == 0 and "plan only" in r.stdout, r.stdout + r.stderr
    before = cs.evj("setup.json", cs.tmp / "ev-s1")["before"]
    assert before["status"] == "absent" and before["action"] == "create" and cs.writes() == []
    r = cs.ci("setup", *cs.common(req, cs.tmp / "ev-s2"), "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    rec = cs.evj("setup.json", cs.tmp / "ev-s2")
    assert rec["deploy"] == "created_or_updated" and rec["after"]["ready"] is True
    assert rec["after"]["federated_trust"] == {} and rec["after"]["template_matches"] is True
    assert [w[:2] for w in cs.writes()] == [["cloudformation", "deploy"]]
    deploy = next(c for c in cs.calls() if c["argv"][:2] == ["cloudformation", "deploy"])
    assert f"VpcId={VPC}" in deploy["argv"] and f"SubnetId={SUBNET}" in deploy["argv"]
    r = cs.ci("setup", *cs.common(req, cs.tmp / "ev-s3"), "--apply")
    assert r.returncode == 0 and cs.evj("setup.json", cs.tmp / "ev-s3")["deploy"] == "no_changes"
    assert cs.ci(*cs.run_args(req, cs.tmp / "ev-plan", cmd="plan")).returncode == 0


def test_setup_revokes_a_former_ci_role_and_the_run_refuses_until_then(cs: CiSim):
    seed_legacy_ci_role(cs)
    req = cs.write_request(request())
    r = cs.run_ci(req)
    assert r.returncode == 2 and "roles trusted by a federated principal" in r.stderr, r.stderr
    r = cs.ci("setup", *cs.common(req, cs.tmp / "ev-plan"))
    assert r.returncode == 0 and cs.writes() == []
    before = cs.evj("setup.json", cs.tmp / "ev-plan")["before"]
    assert before["ready"] is False and before["action"] == "update"
    assert before["to_remove"] == ["ValidationRole (AWS::IAM::Role cce-a09-github-validation)"] and not before["to_add"]
    assert before["federated_trust"]["ValidationRole"]["role"] == "cce-a09-github-validation"
    assert before["parameters_dropped"] == ["GitHubSubject"] and before["template_matches"] is False
    r = cs.ci("setup", *cs.common(req, cs.tmp / "ev-apply"), "--apply")
    assert r.returncode == 0, r.stdout + r.stderr
    after = cs.evj("setup.json", cs.tmp / "ev-apply")["after"]
    assert after["ready"] is True and after["to_remove"] == [] and after["federated_trust"] == {}
    st = cs.state()
    assert "cce-a09-github-validation" not in st["roles"]  # the OIDC trust is gone with its role
    assert {"cce-a09-cfn-execution", "cce-a09-watchdog"} <= set(st["roles"])  # the roles still in use are kept
    assert "ValidationRoleArn" not in st["stacks"][BASE]["outputs"]
    assert cs.ci(*cs.run_args(req, cs.tmp / "ev-plan2", cmd="plan")).returncode == 0


def test_setup_refuses_an_unusable_network_before_writing(cs: CiSim):
    cs.patch_state(lambda s: s.update(route_tables=[]))
    r = cs.ci("setup", *cs.common(cs.write_request(request()), cs.tmp / "ev"), "--apply")
    assert r.returncode == 2 and "no active 0.0.0.0/0 route" in r.stderr and cs.writes() == []


def test_a_failed_setup_is_reported_not_verified(cs: CiSim):
    seed_legacy_ci_role(cs)
    r = cs.ci(
        "setup", *cs.common(cs.write_request(request()), cs.tmp / "ev"), "--apply", env={"FAKE_AWS_DEPLOY_FAIL": "1"}
    )
    assert r.returncode == 1 and "SETUP NOT VERIFIED" in r.stderr
    assert "cce-a09-github-validation" in cs.state()["roles"]  # nothing claimed it was revoked


def test_the_wrapper_runs_the_orchestrator_in_the_controller_environment():
    text = WRAPPER.read_text()
    assert os.access(WRAPPER, os.X_OK) and "validation/aws_validation.py" in text
    assert "--extra aws" in text and "--frozen" in text and "gh " not in text
