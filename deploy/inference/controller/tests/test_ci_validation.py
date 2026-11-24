"""The GitHub Actions AWS validation (deploy/inference/ci/aws_validation.py) end to end with SIMULATED AWS: the fake
`aws` CLI keeps an account state (access stack, quota, network, AMIs, SSM parameters, CloudFormation stacks,
EventBridge schedules), the "instance" is the simulated host of sim_host.sh (fake llama-server through the real
run-server.sh, which fetches its API key from the fake SSM like the real host), Run Command runs locally, and the real
bench/gpu-validation.py, controller and measure.py run the step B. No AWS resource is ever touched.

Cases: request detection on real git history (normal merge, new request, modified, two at once, renamed); a new
request runs end to end (delete and keep-stopped); a retry with the same id spends nothing and only re-verifies the
cleanup; a second request reuses the kept stack; every preflight refusal creates nothing; validation failure and
cleanup failure are reported apart; SIGTERM; the runner killed with SIGKILL while the AWS-side watchdog still stops
the instance; secrets never reach the evidence."""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from conftest import DEPLOY, Q8
from test_gpu_validation import SIM_HOST, Sim, wait_until

CI_SCRIPT = DEPLOY / "ci" / "aws_validation.py"
REPO = DEPLOY.parent.parent
VPC, SUBNET, AZ, AMI = "vpc-0aaaaaaaaaaaaaaaa", "subnet-0bbbbbbbbbbbbbbbb", "us-east-1a", "ami-0ccccccccccccccc0"
AMI_PARAM = "/aws/service/deeplearning/ami/x86_64/base-oss-nvidia-driver-gpu-ubuntu-24.04/latest/ami-id"
ACCESS = {
    "ValidationRoleArn": "arn:aws:iam::000000000000:role/cce-a09-github-validation",
    "CfnExecutionRoleArn": "arn:aws:iam::000000000000:role/cce-a09-cfn-execution",
    "HostPermissionsBoundaryArn": "arn:aws:iam::000000000000:policy/cce-a09-host-boundary",
    "WatchdogRoleArn": "arn:aws:iam::000000000000:role/cce-a09-watchdog",
    "WatchdogScheduleGroup": "cce-inference-watchdog",
    "AllowedVpcId": VPC,
    "AllowedSubnetId": SUBNET,
}


def load_ci() -> Any:
    spec = importlib.util.spec_from_file_location("aws_validation", CI_SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["aws_validation"] = mod
    spec.loader.exec_module(mod)
    return mod


ci = load_ci()


def request(rid: str = "a09-1-sim-001", **over: Any) -> dict[str, Any]:
    doc = json.loads((DEPLOY / "ci" / "request.example.json").read_text())
    doc["id"] = rid
    doc["network"] = {"vpc_id": VPC, "subnet_id": SUBNET, "availability_zone": AZ, "assign_public_ip": True}
    doc["ami"] = "resolve-latest"
    doc["stack"] = {"name": f"cce-inference-{rid}", "mode": "create", "after_run": "delete"}
    doc["limits"] = {"max_runtime_minutes": 60, "bootstrap_timeout_minutes": 15}
    doc["validation"] = {"profiles": ["reference-8k-1slot"], "requests": 2, "prompt_tokens": 200}
    for key, value in over.items():
        doc[key] = value
    return doc


class CiSim(Sim):
    """Sim (test_gpu_validation) plus a seeded fake account and the orchestrator's overrides."""

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
        self.seed()
        self.cievidence = tmp / "ev"
        self.work = tmp / "work"
        port = self.port
        self.env.update(
            {
                "PATH": f"{self.bin}:{os.environ['PATH']}",
                "CCE_CI_CONTROLLER_TEMPLATE": str(self.cfg),
                "CCE_CI_HOST_BOOTSTRAP_CMD": f"cat {tmp / 'bootstrap.log'}",
                "CCE_CI_HOST_CLOUD_INIT_CMD": f"cat {tmp / 'cloud-init-status'}",
                "CCE_CI_HOST_SERVICE_CMD": f"{SIM_HOST} {tmp} describe | sed s/running/active/",
                "CCE_CI_HOST_HEALTH_CMD": f"curl -s -o /dev/null -w '%{{http_code}}' -m 5 "
                f"http://127.0.0.1:{port}/health || true",
            }
        )
        for k in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_SHA", "CCE_A09_ACCESS_STACK"):
            self.env.pop(k, None)

    def seed(self, **over: Any) -> None:
        st: dict[str, Any] = {
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
                    "OwnerAlias": "amazon",
                    "State": "available",
                    "Architecture": "x86_64",
                }
            ],
            "params": {AMI_PARAM: {"Value": AMI, "Type": "String"}},
            "stacks": {"cce-a09-github-access": {"status": "CREATE_COMPLETE", "outputs": dict(ACCESS)}},
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

    def run_args(self, req: Path, ev: Path | None = None) -> list[str]:
        return [
            "run",
            "--request",
            str(req),
            "--evidence",
            str(ev or self.cievidence),
            "--workdir",
            str(self.work),
            "--python",
            sys.executable,
            "--tunnel-port",
            str(self.port),
        ]

    def run_ci(self, req: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return self.ci(*self.run_args(req), env=env)

    def cleanup(self, req: Path, ev: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return self.ci("cleanup", "--request", str(req), "--evidence", str(ev), env=env)

    def evj(self, name: str, base: Path | None = None) -> Any:
        return json.loads(((base or self.cievidence) / name).read_text())

    def calls_of(self, *words: str) -> int:
        path = self.fake / "calls.jsonl"
        if not path.exists():
            return 0
        return sum(1 for line in path.read_text().splitlines() if all(w in json.loads(line)["argv"] for w in words))


@pytest.fixture
def cs(tmp_path: Path, fake_model: dict[str, str]):
    s = CiSim(tmp_path, fake_model)
    yield s
    subprocess.run([str(SIM_HOST), str(tmp_path), "stop"], check=False)


def all_evidence_bytes(*dirs: Path) -> bytes:
    return b"".join(p.read_bytes() for d in dirs if d.exists() for p in d.rglob("*") if p.is_file())


# --- request schema (offline) -----------------------------------------------------------------------------------


def test_the_example_request_is_valid_and_within_its_spend_ceiling():
    req = ci.load_request(DEPLOY / "ci" / "request.example.json")
    assert req["stack"]["after_run"] in ("keep-stopped", "delete") and req["model"] == Q8
    assert ci.worst_case_usd(req) <= req["authorization"]["max_spend_usd"]
    # the example lives outside the watched directory: merging it never runs anything
    assert (DEPLOY / "ci" / "request.example.json").parent != ci.REQUESTS_DIR


@pytest.mark.parametrize(
    ("mutate", "error"),
    [
        (lambda d: d["stack"].pop("after_run"), "stack: missing ['after_run']"),
        (lambda d: d["stack"].update(after_run="keep"), "stack.after_run"),
        (lambda d: d["authorization"].update(spend_approved=False), "spend_approved and network_approved"),
        (lambda d: d["authorization"].update(max_spend_usd=1), "below the worst case"),
        (lambda d: d["aws"].update(region="us-west-2"), "aws.region"),
        (lambda d: d["aws"].update(instance_type="g6.xlarge"), "aws.instance_type"),
        (lambda d: d.update(model="qwen3.6-27b-q4km"), "model: must be qwen3.6-27b-q8_0"),
        (lambda d: d.update(extra=1), "unknown keys ['extra']"),
        (lambda d: d["limits"].update(max_runtime_minutes=600), "limits.max_runtime_minutes"),
        (lambda d: d["limits"].pop("max_runtime_minutes"), "limits: missing"),
        (lambda d: d["network"].update(client_cidr="8.8.8.0/24"), "network.client_cidr"),
        (lambda d: d["validation"].update(profiles=["nope"]), "unknown profiles"),
        (lambda d: d["stack"].update(name="other-stack"), "stack.name"),
        (lambda d: d.update(id="Bad_Id"), "id:"),
        (lambda d: d.update(parity={"s3_uri": "https://x/y.zip", "sha256": "0" * 64}), "parity.s3_uri"),
    ],
)
def test_invalid_requests_are_refused_offline(mutate: Any, error: str):
    doc = request()
    mutate(doc)
    errors = ci.validate_request(doc)
    assert any(error in e for e in errors), errors


def test_the_file_name_must_be_the_id_in_the_requests_directory():
    assert any("must be named" in e for e in ci.validate_request(request("a09-1-x-001"), "other.json"))
    assert ci.validate_request(request("a09-1-x-001"), "a09-1-x-001.json") == []


def test_the_versioned_requests_directory_is_valid():
    r = subprocess.run([sys.executable, str(CI_SCRIPT), "check"], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr


# --- detection on real git history (what the workflow's detect job does) -------------------------------------------


def git(repo: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t"}
    env["GIT_COMMITTER_EMAIL"] = "t@t"
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env).stdout


def commit(repo: Path, files: dict[str, str | None], msg: str) -> str:
    for rel, text in files.items():
        p = repo / rel
        if text is None:
            p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", msg)
    return git(repo, "rev-parse", "HEAD").strip()


def test_detection_normal_merge_new_request_change_and_rename(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    d = ci.REQUESTS_REL
    c0 = commit(repo, {f"{d}/README.md": "x", "src/a.py": "1"}, "base")
    # a normal merge (no request) and a README change never run anything
    c1 = commit(repo, {"src/a.py": "2"}, "normal merge")
    assert ci.detect(c0, c1, repo) == (None, [])
    c2 = commit(repo, {f"{d}/README.md": "y"}, "readme")
    assert ci.detect(c1, c2, repo) == (None, [])
    # a new request runs
    c3 = commit(repo, {f"{d}/a09-1-x-001.json": json.dumps(request("a09-1-x-001"))}, "new request")
    path, notes = ci.detect(c2, c3, repo)
    assert path == repo / d / "a09-1-x-001.json" and notes == []
    # modifying it never runs it again (and a squash/merge of several commits is one range)
    c4 = commit(repo, {f"{d}/a09-1-x-001.json": json.dumps(request("a09-1-x-001", purpose="edited"))}, "edit")
    path, notes = ci.detect(c3, c4, repo)
    assert path is None and "M" in notes[0]
    # a rename shows as a new file: detection passes it on, and the SSM claim (same id) refuses the spend
    c5 = commit(repo, {f"{d}/a09-1-x-001.json": None, f"{d}/renamed.json": json.dumps(request("a09-1-x-001"))}, "mv")
    path, _ = ci.detect(c4, c5, repo)
    assert path == repo / d / "renamed.json"
    # two new requests in one push: refused, nothing runs
    c6 = commit(
        repo,
        {f"{d}/a09-1-x-002.json": json.dumps(request("a09-1-x-002")), f"{d}/a09-1-x-003.json": "{}"},
        "two",
    )
    with pytest.raises(ci.RequestError, match="one request per push"):
        ci.detect(c5, c6, repo)
    with pytest.raises(ci.RequestError, match="no previous commit"):
        ci.detect("0" * 40, c6, repo)


def test_detect_outputs_drive_the_job_time_limit_and_credentials():
    req = ci.load_request(DEPLOY / "ci" / "request.example.json")
    out = ci.detect_outputs(req, ci.REQUESTS_DIR / "a09-1-g6e-q8-001.json")
    assert out["timeout_minutes"] == "240" and out["role_duration_seconds"] == str((240 + 30) * 60)
    assert out["request"] == "deploy/inference/validation-requests/a09-1-g6e-q8-001.json"
    assert out["after_run"] == "keep-stopped"


# --- OIDC claims: never the token ----------------------------------------------------------------------------------


def test_oidc_claims_print_the_subject_and_never_the_token(tmp_path: Path):
    def b64(d: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    payload = {
        "sub": "repo:owner/repo:ref:refs/heads/main",
        "aud": "sts.amazonaws.com",
        "ref": "refs/heads/main",
        "job_workflow_ref": "owner/repo/.github/workflows/a09-1-aws-gpu-validation.yml@refs/heads/main",
        "actor": "someone",
        "run_id": "1",
    }
    token = f"{b64({'alg': 'RS256'})}.{b64(payload)}.SIGNATURE-SECRET"
    seen: dict[str, str] = {}

    class H(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            seen["auth"], seen["path"] = self.headers["Authorization"], self.path
            body = json.dumps({"value": token}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a: Any) -> None:
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        env = {
            **os.environ,
            "ACTIONS_ID_TOKEN_REQUEST_URL": f"http://127.0.0.1:{srv.server_port}/token?api-version=2.0",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "request-bearer",
            "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        }
        r = subprocess.run([sys.executable, str(CI_SCRIPT), "oidc-claims"], capture_output=True, text=True, env=env)
    finally:
        srv.shutdown()
    assert r.returncode == 0, r.stderr
    claims = json.loads(r.stdout.split(ci.CLAIMS_MARK, 1)[1])
    assert json.loads(r.stdout.split(ci.CLAIMS_MARK, 1)[0]) == claims  # human-readable and machine line agree
    assert claims["sub"] == payload["sub"] and claims["job_workflow_ref"].endswith("@refs/heads/main")
    assert "actor" not in claims and "run_id" not in claims
    assert "SIGNATURE-SECRET" not in r.stdout + r.stderr and token not in (tmp_path / "summary.md").read_text()
    assert seen["auth"] == "bearer request-bearer" and "audience=sts.amazonaws.com" in seen["path"]


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
    assert pre["parity"] == {"status": "not_configured"}
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
    # the separate cleanup job (idempotent) verifies the same
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
    assert key.encode() not in all_evidence_bytes(cs.cievidence)  # used by the host and the runner, never saved
    creates = cs.calls_of("cloudformation", "create-stack")

    # retry of the same request (re-run of the workflow, or the id re-added): no new spend, cleanup re-verified
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
    assert cs.evj("watchdog.json", ev2)["kind"] == "runner"  # armed before anything could start the instance
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
        ("access_subnet_differs", "the access stack allows"),
        ("no_access_stack", "access stack cce-a09-github-access is missing"),
        ("bad_ami", "is not an available Amazon"),
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
    elif case == "access_subnet_differs":
        cs.patch_state(lambda s: s["stacks"]["cce-a09-github-access"]["outputs"].update(AllowedSubnetId="subnet-0ff"))
    elif case == "no_access_stack":
        cs.patch_state(lambda s: s["stacks"].pop("cce-a09-github-access"))
    elif case == "bad_ami":
        cs.patch_state(lambda s: s["images"][0].update(OwnerAlias="someone"))
    elif case == "no_plugin":
        (cs.bin / "session-manager-plugin").unlink()
        env["PATH"] = f"{cs.bin}:/usr/bin:/bin"
    elif case == "parity_sha_mismatch":
        s3 = cs.tmp / "s3" / "bucket"
        s3.mkdir(parents=True)
        (s3 / "results.zip").write_bytes(b"PK not really")
        env["FAKE_AWS_S3_DIR"] = str(cs.tmp / "s3")
        doc["parity"] = {"s3_uri": "s3://bucket/results.zip", "sha256": "0" * 64}
    req = cs.write_request(doc)
    r = cs.run_ci(req, env=env)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "REFUSED (nothing claimed or created)" in r.stderr and expect in r.stderr, r.stderr
    st = cs.state()
    assert not any(k.startswith("/cce/inference/validation-runs/") for k in st["params"])  # id not burned
    assert cs.calls_of("cloudformation", "create-stack") == 0 and cs.calls_of("put-parameter") == 0
    assert cs.evj("outcome.json")["validation"] == "refused"


def test_parity_zip_is_fetched_verified_and_its_corpus_outputs_never_uploaded(cs: CiSim, tmp_path: Path):
    """A configured ZIP is verified before anything is created; parity outputs that reproduce the corpus are kept
    off the evidence (their hashes are recorded). parity.sh itself is exercised against real data only on AWS."""
    import hashlib
    import zipfile

    zpath = tmp_path / "s3" / "bucket" / "a09" / "results.zip"
    zpath.parent.mkdir(parents=True)
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("cawbase_resultados/CAWbase_benchmark_v1.1/benchmark.py", "print(1)\n")
        z.writestr("cawbase_resultados/corrida_nocturna/entradas/evaluacion.jsonl", "{}\n")
    doc = request(
        parity={"s3_uri": "s3://bucket/a09/results.zip", "sha256": hashlib.sha256(zpath.read_bytes()).hexdigest()}
    )
    run = ci.Run(ci.load_request(cs.write_request(doc)), cs.cievidence, cs.work)
    run.aws.bin = cs.env["CCE_AWS_BIN"]
    run.aws.env.update(cs.env, FAKE_AWS_S3_DIR=str(tmp_path / "s3"))
    pkg = run.fetch_parity()
    assert (pkg / "CAWbase_benchmark_v1.1").is_dir() and cs.cievidence not in pkg.parents
    par = cs.cievidence / "gpu-validation" / "parity-reference-8k-1slot"
    par.mkdir(parents=True)
    (par / "evaluacion-r1-i1.jsonl").write_text('{"salida": "corpus content"}\n')
    (par / "summary.json").write_text('{"strict_correct_per_rep": [40]}')
    moved = ci.withhold(cs.cievidence, cs.work / "withheld")
    assert [m["file"] for m in moved] == ["gpu-validation/parity-reference-8k-1slot/evaluacion-r1-i1.jsonl"]
    assert not (par / "evaluacion-r1-i1.jsonl").exists() and (par / "summary.json").exists()
    assert json.loads((cs.cievidence / "withheld.json").read_text())["files"][0]["sha256"]


def test_validation_failure_and_cleanup_are_reported_apart(cs: CiSim):
    """The measurement fails (validation failed, exit 1) while the cleanup is verified (exit 0 of the cleanup job)."""
    doc = request()
    cs.host_reads_key_from_ssm(doc["stack"]["name"])
    req = cs.write_request(doc)
    r = cs.run_ci(req, env={"FAKE_MEASURE_FAIL": "run:run-reference-8k-1slot-c1"})
    assert r.returncode == 1, r.stdout + r.stderr
    out = cs.evj("outcome.json")
    assert out["validation"] == "failed" and out["gpu_validation_exit"] == 1 and out["cleanup_in_run"] == "verified"
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
    """Bootstrap fails (validation failed) AND the stack deletion fails: the cleanup job fails on its own (exit 5),
    the instance is still verified stopped and the AWS-side watchdog stays armed."""
    doc = request()
    req = cs.write_request(doc)
    env = {"FAKE_AWS_BOOTSTRAP": "fail", "FAKE_AWS_DELETE_FAIL": "1"}
    r = cs.run_ci(req, env=env)
    assert r.returncode == 1, r.stdout + r.stderr
    out = cs.evj("outcome.json")
    assert "bootstrap failed on the host" in out["validation"] and out["cleanup_in_run"] == "FAILED"
    r2 = cs.cleanup(req, cs.tmp / "ev-cleanup", env=env)
    assert r2.returncode == 5 and "CLEANUP FAILED" in r2.stderr
    clean = cs.evj("cleanup.json", cs.tmp / "ev-cleanup")
    assert clean["outcome"] == "FAILED" and clean["volumes_left"] == ["vol-0simweights"]
    assert clean["watchdog"] == "kept_armed" and any("stack_not_deleted" in e for e in clean["errors"])
    assert cs.state()["schedules"]  # still armed in AWS
    claim = json.loads(cs.state()["params"][f"/cce/inference/validation-runs/{doc['id']}"]["Value"])
    assert claim["cleanup_job"] == "FAILED"


def test_sigterm_during_bootstrap_cleans_up_and_exits_130(cs: CiSim):
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
        p.send_signal(signal.SIGTERM)  # what GitHub sends on cancellation / time limit
        out, err = p.communicate(timeout=300)
    finally:
        if p.poll() is None:
            p.kill()
    assert p.returncode == 130, out + err
    assert cs.evj("outcome.json")["validation"].startswith("interrupted")
    clean = cs.evj("cleanup-run.json")
    assert clean["outcome"] == "verified" and clean["stack_deleted"] is True
    assert clean["watchdog"] == "no_runner_schedule" and cs.state()["schedules"] == {}  # in-stack: gone with it
    assert cs.host_state() == "stopped"


def test_runner_killed_the_aws_side_watchdog_still_stops_the_instance(cs: CiSim):
    """SIGKILL: no finally, no in-run cleanup (a dead runner). The schedule armed in AWS stops the instance on its
    own; the cleanup job, when it runs, finishes the deletion."""
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
    subprocess.run([str(cs.tmp / "aws"), "fake", "fire-schedules"], env=cs.env, check=True)  # EventBridge, no runner
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


def test_summary_tells_validation_and_cleanup_apart(tmp_path: Path):
    a, b = tmp_path / "a", tmp_path / "b"
    ci.save(a, "outcome.json", {"validation": "failed: x", "exit": 1, "cleanup_in_run": "verified"})
    ci.save(b, "cleanup.json", {"outcome": "FAILED", "errors": ["stack_not_deleted"]})
    text = ci.summary_markdown([a, b])
    assert "**Validation**: `failed: x`" in text and "**Cleanup job**: `FAILED`" in text


def test_the_workflow_and_the_orchestrator_agree_on_the_contract():
    wf = (REPO / ".github" / "workflows" / "a09-1-aws-gpu-validation.yml").read_text()
    for sub in ("check", "detect", "oidc-claims", "run --request", "cleanup --request", "redact", "summary"):
        assert f"aws_validation.py {sub}" in wf, sub
    assert f'paths: ["{ci.REQUESTS_REL}/**"]' in wf


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
    """Reproduction of the defect of 09c9429: a create+delete request naming an existing stack is refused by the
    preflight (nothing claimed), and the always-run cleanup job then deleted that stack. Now it touches nothing."""
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


# --- the AWS-side stop exists from the creation of the instance, without the runner --------------------------


def test_runner_lost_right_after_create_stack_the_instance_is_stopped_without_runner_or_cleanup(cs: CiSim):
    """AWS accepts CreateStack and the runner dies before it receives the StackId (SIGKILL from the fake, at that
    exact point). CloudFormation keeps creating: the instance appears later, together with the in-stack schedule
    bound to the spend limit, and EventBridge stops it. No runner, no cleanup job."""
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
    assert owner["owner_id"] == doc["id"] and owner["stack_id"] is None  # the runner never learned the id
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
    assert cs.calls_of("stop-instances") == 0 and cs.calls_of("delete-stack") == 0  # neither runner nor cleanup
    # a later cleanup job still finds it as this request's (claim token tag) and deletes it by StackId
    r = cs.cleanup(req, cs.tmp / "ev-cleanup")
    assert r.returncode == 0, r.stderr
    clean = cs.evj("cleanup.json", cs.tmp / "ev-cleanup")
    assert clean["ownership"] == "owned" and clean["stack_deleted"] is True and clean["stack_id"] == stack["id"]
