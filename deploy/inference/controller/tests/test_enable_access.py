"""The initial enablement as one idempotent operation (deploy/inference/ci/enable_access.py) with SIMULATED AWS and
GitHub: the fake `aws` keeps the account (OIDC provider, network, access stack, the role's trust policy) and the fake
`gh` the diagnostic runs, their logs and the repository variables. No AWS or GitHub call is ever made."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from conftest import DEPLOY, HERE

SCRIPT = DEPLOY / "ci" / "enable_access.py"
REPO = "owner/cawbase-content-engine"
SUB = f"repo:{REPO}:ref:refs/heads/main"
VPC, SUBNET, AZ = "vpc-0aaaaaaaaaaaaaaaa", "subnet-0bbbbbbbbbbbbbbbb", "us-east-1a"
PROVIDER = "arn:aws:iam::000000000000:oidc-provider/token.actions.githubusercontent.com"


def claims_log(**over: Any) -> str:
    claims = {"sub": SUB, "aud": "sts.amazonaws.com", "ref": "refs/heads/main", "repository": REPO, **over}
    line = "CCE_OIDC_CLAIMS=" + json.dumps(claims, separators=(",", ":"))
    return f"diagnose\tPrint the safe claims\t2026-11-24T12:00:00.0000000Z {line}\n"


class Env:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.fake = tmp / "fake-aws"
        self.fake.mkdir()
        aws, ghw = tmp / "aws", tmp / "gh"
        aws.write_text(f'#!/bin/sh\nexec {sys.executable} {HERE / "fake_aws.py"} "$@"\n')
        ghw.write_text(f'#!/bin/sh\nexec {sys.executable} {HERE / "fake_gh.py"} "$@"\n')
        aws.chmod(0o755)
        ghw.chmod(0o755)
        self.gh_state = tmp / "gh.json"
        self.out = tmp / "report"
        self.account()
        self.diagnostic(claims_log())
        self.env = {
            **os.environ,
            "CCE_AWS_BIN": str(aws),
            "CCE_GH_BIN": str(ghw),
            "FAKE_AWS_DIR": str(self.fake),
            "FAKE_AWS_SIM_DIR": str(tmp),
            "FAKE_AWS_SIM_HOST": "/bin/false",
            "FAKE_GH_STATE": str(self.gh_state),
            "CCE_POLL_S": "0.05",
            "AWS_ACCESS_KEY_ID": "AKIASHOULDNOTBEUSED",  # static credentials must not take precedence
            "AWS_PROFILE": "fede",  # nor another profile
        }

    def account(self, **over: Any) -> None:
        st: dict[str, Any] = {
            "offered_azs": [AZ],
            "subnets": [{"SubnetId": SUBNET, "VpcId": VPC, "AvailabilityZone": AZ, "State": "available"}],
            "route_tables": [
                {"subnet": SUBNET, "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "NatGatewayId": "nat-0s"}]}
            ],
            "oidc_providers": {
                PROVIDER: {"Url": "token.actions.githubusercontent.com", "ClientIDList": ["sts.amazonaws.com"]}
            },
            "stacks": {},
            "params": {},
        }
        st.update(over)
        (self.fake / "state.json").write_text(json.dumps(st))

    def diagnostic(self, log: str | None, conclusion: str = "success", branch: str = "main") -> None:
        runs = []
        if log is not None:
            runs.append(
                {
                    "databaseId": 7,
                    "status": "completed",
                    "conclusion": conclusion,
                    "headBranch": branch,
                    "event": "push",
                    "createdAt": "2026-11-24T11:00:00Z",
                    "headSha": "a" * 40,
                    "log": log,
                }
            )
        self.gh_state.write_text(json.dumps({"runs": runs, "variables": {}}))

    def run(self, cmd: str, *extra: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        argv = [sys.executable, str(SCRIPT), cmd, "--repo", REPO, "--vpc-id", VPC, "--subnet-id", SUBNET]
        argv += ["--availability-zone", AZ, "--out", str(self.out), *extra]
        return subprocess.run(argv, capture_output=True, text=True, env={**self.env, **(env or {})}, timeout=120)

    def report(self) -> dict[str, Any]:
        return json.loads((self.out / "enablement.json").read_text())

    def aws_calls(self, word: str) -> int:
        path = self.fake / "calls.jsonl"
        return sum(1 for ln in path.read_text().splitlines() if word in json.loads(ln)["argv"]) if path.exists() else 0

    def gh_calls(self, *words: str) -> int:
        path = Path(f"{self.gh_state}.calls")
        if not path.exists():
            return 0
        return sum(1 for ln in path.read_text().splitlines() if all(w in json.loads(ln) for w in words))

    def variables(self) -> dict[str, str]:
        return dict(json.loads(self.gh_state.read_text())["variables"])

    def state(self) -> dict[str, Any]:
        return dict(json.loads((self.fake / "state.json").read_text()))


@pytest.fixture
def e(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_plan_reads_everything_and_changes_nothing(e: Env):
    r = e.run("plan")
    assert r.returncode == 0, r.stderr
    rep = e.report()
    assert rep["checks"]["oidc_subject"] == SUB and rep["checks"]["oidc_provider"] == PROVIDER
    assert rep["checks"]["network"]["outbound"] == "nat" and rep["plan"]["changes"] == ["create"]
    assert rep["plan"]["parameters"]["GitHubSubject"] == SUB and rep["plan"]["parameters"]["SubnetId"] == SUBNET
    assert e.aws_calls("deploy") == 0 and e.gh_calls("variable", "set") == 0 and e.variables() == {}


def test_apply_is_verified_and_idempotent(e: Env):
    r = e.run("apply")
    assert r.returncode == 0, r.stderr
    rep = e.report()
    assert rep["outcome"] == "enabled_and_verified" and rep["deploy"] == "created_or_updated"
    assert rep["checks"]["trust_verified"] is True
    role = "arn:aws:iam::000000000000:role/cce-a09-github-validation"
    assert e.variables() == {"CCE_A09_AWS_ROLE_ARN": role}
    assert rep["variables"]["CCE_A09_AWS_ROLE_ARN"]["changed"] is True
    stack = e.state()["stacks"]["cce-a09-github-access"]
    assert stack["params"]["GitHubOidcProviderArn"] == PROVIDER and stack["params"]["VpcId"] == VPC
    # every AWS call used the profile rodrigo, never the static key or the other profile from the environment
    calls = [json.loads(ln) for ln in (e.fake / "calls.jsonl").read_text().splitlines()]
    assert {c["profile"] for c in calls} == {"rodrigo"} and not any(c["env_static_key"] for c in calls)
    # again: nothing changes, nothing is re-set, everything is verified again
    r = e.run("apply")
    assert r.returncode == 0, r.stderr
    rep = e.report()
    assert rep["deploy"] == "no_changes" and rep["plan"]["changes"] == []
    assert rep["variables"]["CCE_A09_AWS_ROLE_ARN"]["changed"] is False and rep["checks"]["trust_verified"] is True
    assert e.gh_calls("variable", "set") == 1


def test_a_non_default_stack_name_also_sets_its_variable(e: Env):
    r = e.run("apply", "--stack-name", "cce-a09-github-access-2")
    assert r.returncode == 0, r.stderr
    assert e.variables()["CCE_A09_ACCESS_STACK"] == "cce-a09-github-access-2"


def test_dispatches_the_diagnostic_when_it_never_ran(e: Env):
    e.diagnostic(None)
    r = e.run("plan")
    assert r.returncode == 2 and "pass --dispatch" in r.stderr
    r = e.run("plan", "--dispatch", env={"FAKE_GH_DISPATCH_LOG": claims_log()})
    assert r.returncode == 0, r.stderr
    assert e.gh_calls("workflow", "run") == 1 and e.report()["diagnostic_run"]["event"] == "workflow_dispatch"


@pytest.mark.parametrize(
    ("setup", "expect"),
    [
        ({"profile": "fede"}, "only with the profile 'rodrigo'"),
        ({"log": claims_log(repository="other/repo")}, "repository 'other/repo'"),
        ({"log": claims_log(ref="refs/heads/feature")}, "ref 'refs/heads/feature'"),
        ({"log": claims_log(sub=f"repo:{REPO}:pull_request")}, "does not match"),
        ({"log": claims_log(aud="other")}, "aud 'other'"),
        ({"log": claims_log(sub=SUB + ":job_workflow_ref:x/y/.github/workflows/a.yml@refs/heads/main")}, "template"),
        ({"log": "no claims here\n"}, "no CCE_OIDC_CLAIMS= line"),
        ({"conclusion": "failure"}, "no successful"),
        ({"subnets_az": "us-east-1b"}, "network: subnet"),
        ({"no_route": True}, "no active 0.0.0.0/0 route"),
        ({"no_provider": True}, "OIDC provider to reuse"),
        ({"no_offer": True}, "is not offered"),
    ],
)
def test_refusals_change_nothing(e: Env, setup: dict[str, Any], expect: str):
    extra: list[str] = []
    if "profile" in setup:
        extra = ["--profile", setup["profile"]]
    if "log" in setup or "conclusion" in setup:
        e.diagnostic(setup.get("log", claims_log()), conclusion=setup.get("conclusion", "success"))
    if "subnets_az" in setup:
        e.account(subnets=[{"SubnetId": SUBNET, "VpcId": VPC, "AvailabilityZone": setup["subnets_az"]}])
    if setup.get("no_route"):
        e.account(route_tables=[])
    if setup.get("no_provider"):
        e.account(oidc_providers={})
    if setup.get("no_offer"):
        e.account(offered_azs=[])
    r = e.run("apply", *extra)
    assert r.returncode == 2, r.stdout + r.stderr
    assert "REFUSED (nothing changed)" in r.stderr and expect in r.stderr, r.stderr
    assert e.aws_calls("deploy") == 0 and e.gh_calls("variable", "set") == 0 and e.variables() == {}


def test_a_trust_that_does_not_match_the_observed_subject_never_sets_the_variable(e: Env):
    r = e.run("apply", env={"FAKE_AWS_TRUST_SUB_OVERRIDE": "repo:owner/cawbase-content-engine:ref:refs/heads/dev"})
    assert r.returncode == 1, r.stdout + r.stderr
    assert "ENABLEMENT NOT VERIFIED" in r.stderr and "trust policy is not exactly" in r.stderr
    assert e.report()["checks"]["trust_verified"] is False and e.variables() == {}


def test_a_network_change_updates_the_stack_and_is_verified(e: Env):
    assert e.run("apply").returncode == 0
    other = "subnet-0cccccccccccccccc"
    e.account(
        subnets=[
            {"SubnetId": SUBNET, "VpcId": VPC, "AvailabilityZone": AZ},
            {"SubnetId": other, "VpcId": VPC, "AvailabilityZone": AZ},
        ],
        route_tables=[{"subnet": other, "Routes": [{"DestinationCidrBlock": "0.0.0.0/0", "GatewayId": "igw-0s"}]}],
        stacks=e.state()["stacks"],
        roles=e.state()["roles"],
    )
    r = e.run("apply", "--subnet-id", other)
    assert r.returncode == 0, r.stderr
    rep = e.report()
    assert rep["plan"]["changes"] == ["SubnetId"] and rep["deploy"] == "created_or_updated"
    assert rep["outputs"]["AllowedSubnetId"] == other


def test_the_report_must_stay_outside_the_repository(e: Env):
    r = e.run("plan", "--out", str(DEPLOY))
    assert r.returncode == 2 and "outside the repository" in r.stderr
