"""Contract of the A09-1 AWS validation workflow, its OIDC diagnostic and the access IaC (static: no AWS, no GitHub).
The orchestration itself is tested with simulated AWS in deploy/inference/controller/tests/test_ci_validation.py."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WF_DIR = ROOT / ".github" / "workflows"
SHA = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+@[0-9a-f]{40}$")


def load_wf(name: str) -> dict[str, Any]:
    doc = yaml.safe_load((WF_DIR / name).read_text())
    doc["on"] = doc.pop(True, doc.get("on"))  # YAML 1.1 reads the key `on` as True
    return doc


class CfnLoader(yaml.SafeLoader):
    pass


CfnLoader.add_multi_constructor(
    "!",
    lambda loader, suffix, node: {
        suffix: (
            loader.construct_scalar(node)
            if isinstance(node, yaml.ScalarNode)
            else loader.construct_sequence(node, deep=True)
            if isinstance(node, yaml.SequenceNode)
            else loader.construct_mapping(node, deep=True)
        )
    },
)


def load_cfn(rel: str) -> dict[str, Any]:
    return yaml.load((ROOT / rel).read_text(), Loader=CfnLoader)  # noqa: S506 - SafeLoader subclass


WF = load_wf("a09-1-aws-gpu-validation.yml")
DIAG = load_wf("a09-1-oidc-diagnostic.yml")
ACCESS = load_cfn("deploy/inference/aws/github-validation-access.yaml")


def statements(role: str) -> list[dict[str, Any]]:
    out = []
    for pol in ACCESS["Resources"][role]["Properties"]["Policies"]:
        for st in pol["PolicyDocument"]["Statement"]:
            out.append(st["If"][1] if "If" in st else st)
    return out


def actions(st: dict[str, Any]) -> list[str]:
    a = st["Action"]
    return a if isinstance(a, list) else [a]


# --- workflow -------------------------------------------------------------------------------------------------


def test_only_a_push_to_main_that_touches_the_requests_directory_triggers_it():
    assert WF["on"] == {"push": {"branches": ["main"], "paths": ["deploy/inference/validation-requests/**"]}}
    assert "pull_request" not in WF["on"] and "pull_request_target" not in WF["on"]


def test_runs_are_serialized_and_never_cancelled():
    assert WF["concurrency"] == {"group": "a09-1-aws-gpu-validation", "cancel-in-progress": False}


def test_credentials_are_temporary_oidc_only_in_the_aws_jobs():
    assert WF["permissions"] == {"contents": "read"}
    assert "permissions" not in WF["jobs"]["detect"]  # no id-token: detection never touches AWS
    text = (WF_DIR / "a09-1-aws-gpu-validation.yml").read_text()
    assert "secrets." not in text and "AWS_SECRET_ACCESS_KEY" not in text  # no stored AWS credential
    for name in ("validate", "cleanup"):
        job = WF["jobs"][name]
        assert job["permissions"] == {"contents": "read", "id-token": "write"}, name
        creds = [s for s in job["steps"] if str(s.get("uses", "")).startswith("aws-actions/configure-aws-credentials@")]
        assert len(creds) == 1, name
        w = creds[0]["with"]
        assert w["role-to-assume"] == "${{ vars.CCE_A09_AWS_ROLE_ARN }}" and w["aws-region"] == "us-east-1"
        assert w["mask-aws-account-id"] is True and "environment" not in job
        assert "github.ref == 'refs/heads/main'" in job["if"] and "github.event_name == 'push'" in job["if"]


def test_every_action_is_pinned_to_a_full_commit_sha():
    for wf in (WF, DIAG):
        uses = [s["uses"] for job in wf["jobs"].values() for s in job["steps"] if "uses" in s]
        assert uses and all(SHA.match(u) for u in uses), uses


def test_the_time_limit_and_the_session_come_from_the_request():
    v = WF["jobs"]["validate"]
    assert "needs.detect.outputs.timeout_minutes" in str(v["timeout-minutes"])
    creds = next(s for s in v["steps"] if "configure-aws-credentials" in str(s.get("uses")))
    assert "role_duration_seconds" in str(creds["with"]["role-duration-seconds"])


def test_cleanup_always_runs_with_its_own_credentials_and_its_own_status():
    c = WF["jobs"]["cleanup"]
    assert c["needs"] == ["detect", "validate"] and c["if"].startswith("always()")
    runs = "\n".join(s.get("run", "") for s in c["steps"])
    assert "aws_validation.py cleanup --request" in runs


def test_evidence_is_redacted_and_uploaded_even_after_a_failure():
    for name in ("validate", "cleanup"):
        steps = WF["jobs"][name]["steps"]
        uploads = [i for i, s in enumerate(steps) if str(s.get("uses", "")).startswith("actions/upload-artifact@")]
        assert len(uploads) == 1 and steps[uploads[0]]["if"] == "always()", name
        assert steps[uploads[0]]["with"]["retention-days"] == 90
    v = WF["jobs"]["validate"]["steps"]
    redact = next(i for i, s in enumerate(v) if "aws_validation.py redact" in s.get("run", ""))
    upload = next(i for i, s in enumerate(v) if "upload-artifact" in str(s.get("uses")))
    assert redact < upload and v[redact]["if"] == "always()"


def test_the_oidc_diagnostic_never_touches_aws():
    text = (WF_DIR / "a09-1-oidc-diagnostic.yml").read_text()
    assert "configure-aws-credentials" not in text and "aws " not in text.replace("aws_validation", "")
    assert DIAG["on"]["push"]["branches"] == ["main"] and "workflow_dispatch" in DIAG["on"]
    assert DIAG["permissions"] == {"contents": "read"}
    assert DIAG["jobs"]["diagnose"]["permissions"] == {"contents": "read", "id-token": "write"}


# --- access IaC (initial enablement) -------------------------------------------------------------------------


def test_trust_is_the_exact_observed_subject_of_main_and_the_sts_audience():
    props = ACCESS["Resources"]["ValidationRole"]["Properties"]
    (stmt,) = props["AssumeRolePolicyDocument"]["Statement"]
    assert stmt["Action"] == "sts:AssumeRoleWithWebIdentity"
    assert stmt["Principal"] == {"Federated": {"Ref": "GitHubOidcProviderArn"}}  # reused, not created
    assert stmt["Condition"] == {
        "StringEquals": {
            "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
            "token.actions.githubusercontent.com:sub": {"Ref": "GitHubSubject"},
        }
    }
    assert props["MaxSessionDuration"] == 43200
    assert not any(r.get("Type") == "AWS::IAM::OIDCProvider" for r in ACCESS["Resources"].values())


@pytest.mark.parametrize(
    ("sub", "ok"),
    [
        ("repo:rhherrera/cawbase-content-engine:ref:refs/heads/main", True),
        ("repo:rhherrera@123/cawbase-content-engine@456:ref:refs/heads/main", True),
        ("repo:o/r:ref:refs/heads/main:job_workflow_ref:o/r/.github/workflows/a.yml@refs/heads/main", True),
        ("repo:o/r:pull_request", False),
        ("repo:o/r:pull_request:ref:refs/heads/main", False),
        ("repo:o/r:ref:refs/heads/feature", False),
        ("repo:o/r:ref:refs/heads/main*", False),
        ("repo:*:ref:refs/heads/main", False),
        ("repo:o/r:environment:prod", False),
    ],
)
def test_the_subject_parameter_rejects_wildcards_pull_requests_and_other_branches(sub: str, ok: bool):
    pattern = ACCESS["Parameters"]["GitHubSubject"]["AllowedPattern"]
    assert bool(re.fullmatch(pattern, sub)) is ok


def test_the_validation_role_cannot_update_stacks_or_manage_iam():
    acts = [a for st in statements("ValidationRole") for a in actions(st)]
    assert "cloudformation:UpdateStack" not in acts and "cloudformation:CreateStack" in acts
    assert {a for a in acts if a.startswith("iam:")} == {"iam:PassRole"}
    create = next(st for st in statements("ValidationRole") if actions(st) == ["cloudformation:CreateStack"])
    assert create["Condition"] == {"StringEquals": {"cloudformation:RoleArn": {"GetAtt": "CfnExecutionRole.Arn"}}}
    assert all("cce-inference-*" in str(st["Resource"]) for st in statements("ValidationRole")
               if any(a.startswith("cloudformation:") and a != "cloudformation:ValidateTemplate" for a in actions(st))
               and st.get("Sid") != "ReadThisAccessStack")  # fmt: skip
    ledger = next(st for st in statements("ValidationRole") if st.get("Sid") == "RequestLedger")
    assert "ssm:DeleteParameter" not in actions(ledger)  # an id is never released


def test_the_execution_role_creates_only_bounded_host_roles_and_the_authorized_instance():
    sts = {st.get("Sid"): st for st in statements("CfnExecutionRole")}
    assert sts["HostRoleOnlyWithTheBoundary"]["Condition"] == {
        "StringEquals": {"iam:PermissionsBoundary": {"Ref": "HostPermissionsBoundary"}}
    }
    acts = [a for st in statements("CfnExecutionRole") for a in actions(st)]
    assert "iam:DeleteRolePermissionsBoundary" not in acts and "iam:PutRolePermissionsBoundary" not in acts
    assert sts["RunOnlyTheAuthorizedInstanceType"]["Condition"] == {
        "StringEquals": {"ec2:InstanceType": {"Ref": "InstanceType"}}
    }
    assert "subnet/${SubnetId}" in str(sts["RunOnlyInTheAuthorizedSubnet"]["Resource"])
    assert ACCESS["Parameters"]["InstanceType"]["AllowedValues"] == ["g6e.xlarge"]


def test_the_watchdog_stops_validation_instances_only():
    role = ACCESS["Resources"]["WatchdogRole"]["Properties"]
    assert role["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] == {"Service": "scheduler.amazonaws.com"}
    (st,) = statements("WatchdogRole")
    assert st["Action"] == "ec2:StopInstances" and "cce:validation-id" in str(st["Condition"])


def test_the_host_template_accepts_the_boundary_and_stays_compatible():
    tpl = load_cfn("deploy/inference/aws/cloudformation.template.yaml")
    assert tpl["Parameters"]["HostPermissionsBoundaryArn"]["Default"] == ""
    pb = tpl["Resources"]["HostRole"]["Properties"]["PermissionsBoundary"]
    assert pb == {"If": ["HasHostBoundary", {"Ref": "HostPermissionsBoundaryArn"}, {"Ref": "AWS::NoValue"}]}
