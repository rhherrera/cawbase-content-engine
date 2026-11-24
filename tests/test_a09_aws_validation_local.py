"""Contract of the LOCAL A09-1 AWS validation that can be checked without AWS: no GitHub workflow touches AWS, the base
IaC trusts no federated (CI/OIDC) principal, its roles stay least-privilege, and the versioned tree carries no real
account or network identifier. The orchestration itself is tested with simulated AWS in
deploy/inference/controller/tests/test_aws_validation.py."""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent
WF_DIR = ROOT / ".github" / "workflows"


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


BASE = load_cfn("deploy/inference/aws/validation-base.yaml")


def statements(role: str) -> list[dict[str, Any]]:
    out = []
    for pol in BASE["Resources"][role]["Properties"]["Policies"]:
        for st in pol["PolicyDocument"]["Statement"]:
            out.append(st["If"][1] if "If" in st else st)
    return out


def actions(st: dict[str, Any]) -> list[str]:
    a = st["Action"]
    return a if isinstance(a, list) else [a]


def orchestrator() -> Any:
    path = ROOT / "deploy" / "inference" / "validation" / "aws_validation.py"
    spec = importlib.util.spec_from_file_location("av_static", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- GitHub: no access to AWS ------------------------------------------------------------------------------------


def test_no_workflow_can_reach_aws_or_request_an_oidc_token():
    for wf in sorted(WF_DIR.glob("*.y*ml")):
        text = wf.read_text()
        assert "id-token" not in text, wf.name
        assert "configure-aws-credentials" not in text and "CCE_A09_AWS_ROLE_ARN" not in text, wf.name
        assert "aws_validation.py" not in text and "validation-requests" not in text, wf.name
    assert not (WF_DIR / "a09-1-aws-gpu-validation.yml").exists()
    assert not (WF_DIR / "a09-1-oidc-diagnostic.yml").exists()
    assert not (ROOT / "deploy" / "inference" / "validation-requests").exists()  # merging never spends


# --- base IaC (setup): no federated trust, least privilege -------------------------------------------------------


def test_the_base_stack_trusts_no_federated_principal_and_has_no_ci_parameters():
    assert not any(k.startswith("GitHub") or k == "ParityObjectArn" for k in BASE["Parameters"])
    assert "ValidationRole" not in BASE["Resources"] and "ValidationRoleArn" not in BASE["Outputs"]
    assert not any(r["Type"] == "AWS::IAM::OIDCProvider" for r in BASE["Resources"].values())
    for name, res in BASE["Resources"].items():
        if res["Type"] != "AWS::IAM::Role":
            continue
        (stmt,) = res["Properties"]["AssumeRolePolicyDocument"]["Statement"]
        assert set(stmt["Principal"]) == {"Service"}, name  # an AWS service of this account only
        assert stmt["Action"] == "sts:AssumeRole", name
        assert stmt["Condition"] == {"StringEquals": {"aws:SourceAccount": {"Ref": "AWS::AccountId"}}}, name
    text = (ROOT / "deploy" / "inference" / "aws" / "validation-base.yaml").read_text()
    assert "githubusercontent" not in text and "AssumeRoleWithWebIdentity" not in text


def test_the_base_stack_keeps_the_logical_ids_and_names_of_the_roles_in_use():
    """An update of an existing base stack (setup --apply) keeps these resources: same logical ids, same names."""
    res = BASE["Resources"]
    assert res["CfnExecutionRole"]["Properties"]["RoleName"] == "cce-a09-cfn-execution"
    assert res["WatchdogRole"]["Properties"]["RoleName"] == "cce-a09-watchdog"
    assert res["HostPermissionsBoundary"]["Properties"]["ManagedPolicyName"] == "cce-a09-host-boundary"
    assert res["WatchdogScheduleGroup"]["Properties"]["Name"] == "cce-inference-watchdog"
    av = orchestrator()
    assert set(av.BASE_OUTPUTS) <= set(BASE["Outputs"]) and {"AllowedVpcId", "AllowedSubnetId"} <= set(BASE["Outputs"])
    assert av.template_resources(av.BASE_TEMPLATE.read_text()) == {k: v["Type"] for k, v in res.items()}


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
    assert BASE["Parameters"]["InstanceType"]["AllowedValues"] == ["g6e.xlarge"]


def test_the_watchdog_stops_validation_instances_only():
    role = BASE["Resources"]["WatchdogRole"]["Properties"]
    assert role["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] == {"Service": "scheduler.amazonaws.com"}
    (st,) = statements("WatchdogRole")
    assert st["Action"] == "ec2:StopInstances" and "cce:validation-id" in str(st["Condition"])


def test_the_host_template_accepts_the_boundary_and_stays_compatible():
    tpl = load_cfn("deploy/inference/aws/cloudformation.template.yaml")
    assert tpl["Parameters"]["HostPermissionsBoundaryArn"]["Default"] == ""
    pb = tpl["Resources"]["HostRole"]["Properties"]["PermissionsBoundary"]
    assert pb == {"If": ["HasHostBoundary", {"Ref": "HostPermissionsBoundaryArn"}, {"Ref": "AWS::NoValue"}]}


def test_the_aws_side_stop_is_created_by_cloudformation_with_the_instance():
    tpl = load_cfn("deploy/inference/aws/cloudformation.template.yaml")
    wd = tpl["Resources"]["Watchdog"]
    assert wd["Type"] == "AWS::Scheduler::Schedule" and wd["Condition"] == "HasWatchdog"
    props = wd["Properties"]
    assert props["Target"]["Arn"] == "arn:aws:scheduler:::aws-sdk:ec2:stopInstances"
    assert props["Target"]["Input"] == {"Sub": '{"InstanceIds": ["${Host}"]}'}  # the instance of this stack
    assert props["StartDate"] == {"Ref": "WatchdogStart"} and props["State"] == "ENABLED"
    assert tpl["Parameters"]["WatchdogStart"]["Default"] == ""  # manual deployments: no schedule
    sts = {st.get("Sid"): st for st in statements("CfnExecutionRole")}
    assert "scheduler:CreateSchedule" in actions(sts["InStackWatchdog"])
    assert sts["PassTheWatchdogRoleToScheduler"]["Resource"] == {"GetAtt": "WatchdogRole.Arn"}


# --- no real account or network data in the versioned tree ----------------------------------------------------------

FICTITIOUS_ACCOUNTS = {"000000000000", "123456789012", "111111111111"}
ACCOUNT_ARN = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]+:[a-z0-9-]*:(\d{12}):")
AWS_IDS = re.compile(r"\b(vpc|subnet|sg|igw|nat|i|eni|rtb|vol)-[0-9a-f]{8,17}\b")
GITHUB_SUB = re.compile(r"repo:[A-Za-z0-9_.-]+@\d+/")  # the immutable-id OIDC subject of a real repository


def fictitious(identifier: str) -> bool:
    hexpart = identifier.split("-", 1)[1]
    return hexpart.startswith("0123456789abcdef") or len(set(hexpart.lstrip("0"))) <= 1


def test_the_versioned_tree_has_no_real_account_or_network_identifier():
    """The request (account, VPC, subnet) lives outside the checkout. Fictitious identifiers only: the example values
    (123456789012, vpc-0123456789abcdef0) and the simulated ones (000000000000, vpc-0aaaaaaaaaaaaaaaa)."""
    files = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True)
    found = []
    for rel in files.stdout.splitlines():
        if rel.startswith("experiments/") or rel.endswith((".lock", ".pdf", ".docx", ".png", ".zip")):
            continue
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        found += [f"{rel}: account {m}" for m in ACCOUNT_ARN.findall(text) if m not in FICTITIOUS_ACCOUNTS]
        found += [f"{rel}: {m.group(0)}" for m in AWS_IDS.finditer(text) if not fictitious(m.group(0))]
        found += [f"{rel}: {m.group(0)}" for m in GITHUB_SUB.finditer(text)]
    assert found == []


def test_the_example_request_is_fictitious():
    doc = json.loads((ROOT / "deploy" / "inference" / "validation" / "request.example.json").read_text())
    assert doc["aws"]["account_id"] == "123456789012" and doc["parity"] is None
    assert fictitious(doc["network"]["vpc_id"]) and fictitious(doc["network"]["subnet_id"])
