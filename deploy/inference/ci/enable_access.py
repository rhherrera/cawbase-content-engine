#!/usr/bin/env python3
"""Initial enablement of the A09-1 AWS validation, as ONE idempotent operation for an authorized agent (standard
library only; AWS through the `aws` CLI with the profile `rodrigo`, GitHub through `gh`). It never spends: it creates
or updates the access stack (roles, permissions boundary, watchdog role and schedule group) and the repository
variable. Contract: docs/a09-1-aws-validation-automation.md §5.

    enable_access.py plan  --repo OWNER/REPO --vpc-id vpc-… --subnet-id subnet-… --availability-zone us-east-1a \
                           --out DIR [--dispatch] [--diagnostic-run ID] [--parity-object-arn arn:aws:s3:::B/K.zip]
    enable_access.py apply (same arguments)

Both read, with no side effect: the caller identity (profile rodrigo only; never fede), the REAL OIDC subject from a
successful run of .github/workflows/a09-1-oidc-diagnostic.yml on main (its `CCE_OIDC_CLAIMS=` log line; with
--dispatch, that workflow is dispatched on main if there is no run yet), the claims (repository, ref main, audience
sts.amazonaws.com, subject pattern, no custom job_workflow_ref template), the account's GitHub OIDC provider (reused;
audience sts.amazonaws.com), the chosen network (VPC, subnet in that VPC and AZ, g6e.xlarge offered there, default
route) and the current access stack. `plan` stops there and writes the plan. `apply` then deploys the stack
(`cloudformation deploy`, no-op when nothing changed), VERIFIES the role's trust (exactly aud + the observed sub on
the reused provider) and the network the stack was scoped to, and only then sets the repository variable
CCE_A09_AWS_ROLE_ARN (and CCE_A09_ACCESS_STACK if the stack name is not the default) and reads it back.

Exit status: 0 done and verified (or plan written); 2 refused before any change; 1 verification failed after the
deploy (the repository variable is NOT set). Overrides (tests only): CCE_AWS_BIN, CCE_GH_BIN, CCE_POLL_S.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import aws_validation as av  # noqa: E402

PROFILE = "rodrigo"  # the only identity allowed to enable (never `fede`)
TEMPLATE = av.DEPLOY / "aws" / "github-validation-access.yaml"
DIAGNOSTIC = "a09-1-oidc-diagnostic.yml"
ROLE_NAME = "cce-a09-github-validation"
PROVIDER_HOST = "token.actions.githubusercontent.com"
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class Refused(Exception):
    pass


class VerificationFailed(Exception):
    pass


def aws_env() -> dict[str, str]:
    """The profile `rodrigo`, with no static or session credentials from the environment taking precedence."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("AWS_ACCESS_KEY", "AWS_SECRET", "AWS_SESSION"))}
    env.update(AWS_PROFILE=PROFILE, AWS_REGION=av.REGION, AWS_DEFAULT_REGION=av.REGION, AWS_PAGER="")
    env.pop("AWS_DEFAULT_PROFILE", None)
    return env


def gh(*args: str, timeout: float = 120) -> str:
    r = subprocess.run(
        [os.environ.get("CCE_GH_BIN", "gh"), *args], capture_output=True, text=True, timeout=timeout, check=False
    )
    if r.returncode != 0:
        raise Refused(f"gh {' '.join(args[:2])}: exit {r.returncode}: {r.stderr.strip()[-300:]}")
    return r.stdout


class Enable:
    def __init__(self, a: argparse.Namespace) -> None:
        self.a = a
        self.aws = av.Aws()
        self.aws.env = aws_env()
        self.report: dict[str, Any] = {"mode": a.cmd, "repo": a.repo, "stack": a.stack_name, "checks": {}}
        self.out = Path(a.out).resolve()

    # --- reads (plan and apply) ---

    def identity(self) -> None:
        if self.a.profile != PROFILE:
            raise Refused(f"the enablement runs only with the profile {PROFILE!r} (got {self.a.profile!r})")
        ident = self.aws.json("sts", "get-caller-identity")
        self.report["caller"] = str(ident.get("Arn", ""))
        self.report["account"] = str(ident.get("Account", ""))

    def diagnostic_run(self) -> dict[str, Any]:
        def runs() -> list[dict[str, Any]]:
            fields = "databaseId,status,conclusion,headBranch,event,createdAt,headSha"
            out = gh("run", "list", "--repo", self.a.repo, "--workflow", DIAGNOSTIC, "--branch", "main", "--limit",
                     "20", "--json", fields)  # fmt: skip
            found: list[dict[str, Any]] = json.loads(out or "[]")
            return found

        if self.a.diagnostic_run:
            match = [r for r in runs() if str(r["databaseId"]) == str(self.a.diagnostic_run)]
            if not match:
                raise Refused(f"diagnostic run {self.a.diagnostic_run} not found on main")
            run = match[0]
        else:
            ok = [r for r in runs() if r.get("conclusion") == "success" and r.get("headBranch") == "main"]
            if not ok and self.a.dispatch:
                before = {r["databaseId"] for r in runs()}
                gh("workflow", "run", DIAGNOSTIC, "--repo", self.a.repo, "--ref", "main")

                def done() -> list[dict[str, Any]]:
                    return [r for r in runs() if r["databaseId"] not in before and r.get("status") == "completed"]

                ok = av.wait_for("the dispatched OIDC diagnostic run", done, 900)
            if not ok:
                raise Refused(
                    f"no successful {DIAGNOSTIC} run on main: merge the automation first (it runs on its own) or "
                    "pass --dispatch"
                )
            run = sorted(ok, key=lambda r: str(r.get("createdAt")))[-1]
        if run.get("conclusion") != "success" or run.get("headBranch") != "main":
            raise Refused(f"diagnostic run {run['databaseId']} is not a successful run on main: {run}")
        return run

    def claims(self) -> dict[str, Any]:
        run = self.diagnostic_run()
        log = gh("run", "view", str(run["databaseId"]), "--repo", self.a.repo, "--log")
        m = re.search(re.escape(av.CLAIMS_MARK) + r"(\{.*\})", log)
        if not m:
            raise Refused(f"diagnostic run {run['databaseId']}: no {av.CLAIMS_MARK} line in its log")
        claims: dict[str, Any] = json.loads(m.group(1))
        self.report["diagnostic_run"] = {k: run.get(k) for k in ("databaseId", "event", "headSha", "createdAt")}
        self.report["claims"] = claims
        sub = str(claims.get("sub", ""))
        problems = []
        if str(claims.get("repository", "")).lower() != self.a.repo.lower():
            problems.append(f"repository {claims.get('repository')!r} is not {self.a.repo!r}")
        if claims.get("ref") != "refs/heads/main":
            problems.append(f"ref {claims.get('ref')!r} is not refs/heads/main")
        if claims.get("aud") != "sts.amazonaws.com":
            problems.append(f"aud {claims.get('aud')!r} is not sts.amazonaws.com")
        if not re.fullmatch(av.SUB_PATTERN, sub):
            problems.append(f"sub {sub!r} does not match the subjects the role may trust")
        if ":job_workflow_ref:" in sub:
            problems.append(
                "the repository's subject template includes job_workflow_ref: the diagnostic's sub names the "
                "diagnostic workflow, not the validation workflow (P-A09-06 needs its own observation)"
            )
        if problems:
            raise Refused("OIDC claims: " + "; ".join(problems))
        self.report["checks"]["oidc_subject"] = sub
        return claims

    def provider(self) -> str:
        arns = [
            p["Arn"]
            for p in self.aws.json("iam", "list-open-id-connect-providers").get("OpenIDConnectProviderList", [])
            if str(p.get("Arn", "")).endswith(f"/{PROVIDER_HOST}")
        ]
        if len(arns) != 1:
            raise Refused(f"expected exactly one existing {PROVIDER_HOST} OIDC provider to reuse, found {arns}")
        info = self.aws.json("iam", "get-open-id-connect-provider", "--open-id-connect-provider-arn", arns[0])
        if "sts.amazonaws.com" not in info.get("ClientIDList", []):
            raise Refused(f"provider {arns[0]} does not accept the audience sts.amazonaws.com")
        self.report["checks"]["oidc_provider"] = arns[0]
        return str(arns[0])

    def network(self) -> None:
        a = self.a
        try:
            itype = av.aws_target()["instance_type"]
            net = av.check_network(self.aws, a.vpc_id, a.subnet_id, a.availability_zone, itype)
        except av.Refused as exc:
            raise Refused(f"network: {exc}") from exc
        self.report["checks"]["network"] = net

    def parameters(self, provider: str, sub: str) -> dict[str, str]:
        return {
            "GitHubOidcProviderArn": provider,
            "GitHubSubject": sub,
            "VpcId": self.a.vpc_id,
            "SubnetId": self.a.subnet_id,
            "InstanceType": av.aws_target()["instance_type"],
            "ParityObjectArn": self.a.parity_object_arn or "",
        }

    # --- apply ---

    def deploy(self, params: dict[str, str]) -> dict[str, str]:
        overrides = [f"{k}={v}" for k, v in params.items()]
        out = self.aws.raw(
            "cloudformation",
            "deploy",
            "--stack-name",
            self.a.stack_name,
            "--template-file",
            str(TEMPLATE),
            "--capabilities",
            "CAPABILITY_NAMED_IAM",
            "--no-fail-on-empty-changeset",
            "--parameter-overrides",
            *overrides,
            timeout=1800,
        )
        self.report["deploy"] = "no_changes" if "No changes to deploy" in out else "created_or_updated"
        stack = self.aws.stack(self.a.stack_name)
        if not stack or not str(stack.get("StackStatus")).endswith("_COMPLETE"):
            raise VerificationFailed(f"access stack {self.a.stack_name}: {stack and stack.get('StackStatus')}")
        outputs = self.aws.outputs(stack)
        self.report["outputs"] = outputs
        return outputs

    def verify(self, outputs: dict[str, str], provider: str, sub: str) -> None:
        role = self.aws.json("iam", "get-role", "--role-name", ROLE_NAME)["Role"]
        doc = role["AssumeRolePolicyDocument"]
        if isinstance(doc, str):
            doc = json.loads(urllib.parse.unquote(doc))
        expected = [
            {
                "Effect": "Allow",
                "Principal": {"Federated": provider},
                "Action": "sts:AssumeRoleWithWebIdentity",
                "Condition": {
                    "StringEquals": {
                        f"{PROVIDER_HOST}:aud": "sts.amazonaws.com",
                        f"{PROVIDER_HOST}:sub": sub,
                    }
                },
            }
        ]
        problems = []
        if doc.get("Statement") != expected:
            problems.append(f"trust policy is not exactly aud + the observed sub: {doc.get('Statement')}")
        if role.get("Arn") != outputs.get("ValidationRoleArn"):
            problems.append(f"role {role.get('Arn')} is not the stack output {outputs.get('ValidationRoleArn')}")
        if int(role.get("MaxSessionDuration", 0)) > av.MAX_ROLE_SESSION_S:
            problems.append(f"MaxSessionDuration {role.get('MaxSessionDuration')} > {av.MAX_ROLE_SESSION_S}")
        if outputs.get("GitHubSubject") != sub:
            problems.append(f"stack trusts {outputs.get('GitHubSubject')!r}, observed {sub!r}")
        net = {"vpc": outputs.get("AllowedVpcId"), "subnet": outputs.get("AllowedSubnetId")}
        if net != {"vpc": self.a.vpc_id, "subnet": self.a.subnet_id}:
            problems.append(f"stack network {net} is not the chosen {self.a.vpc_id}/{self.a.subnet_id}")
        for key in ("CfnExecutionRoleArn", "WatchdogRoleArn", "WatchdogScheduleGroup", "HostPermissionsBoundaryArn"):
            if not outputs.get(key):
                problems.append(f"output {key} missing")
        self.report["checks"]["trust_verified"] = not problems
        if problems:
            raise VerificationFailed("; ".join(problems))

    def variables(self, outputs: dict[str, str]) -> None:
        wanted = {"CCE_A09_AWS_ROLE_ARN": outputs["ValidationRoleArn"]}
        if self.a.stack_name != av.ACCESS_STACK_DEFAULT:
            wanted["CCE_A09_ACCESS_STACK"] = self.a.stack_name

        def current() -> dict[str, str]:
            rows = json.loads(gh("variable", "list", "--repo", self.a.repo, "--json", "name,value") or "[]")
            return {r["name"]: r["value"] for r in rows}

        before = current()
        for name, value in wanted.items():
            if before.get(name) != value:
                gh("variable", "set", name, "--repo", self.a.repo, "--body", value)
        after = current()
        if any(after.get(k) != v for k, v in wanted.items()):
            raise VerificationFailed(f"repository variables not set as expected: {after}")
        self.report["variables"] = {k: {"value": v, "changed": before.get(k) != v} for k, v in wanted.items()}

    def main(self) -> int:
        repo = av.REPO.resolve()
        if self.out == repo or repo in self.out.parents:
            print("REFUSED: --out must be outside the repository", file=sys.stderr)
            return 2
        self.out.mkdir(parents=True, exist_ok=True)
        try:
            if not REPO_RE.match(self.a.repo):
                raise Refused(f"--repo must be OWNER/REPO (got {self.a.repo!r})")
            self.identity()
            sub = str(self.claims()["sub"])
            provider = self.provider()
            self.network()
            params = self.parameters(provider, sub)
            stack = self.aws.stack(self.a.stack_name)
            current = {p["ParameterKey"]: p["ParameterValue"] for p in (stack or {}).get("Parameters", [])}
            self.report["plan"] = {
                "parameters": params,
                "stack_status": stack.get("StackStatus") if stack else "absent",
                "changes": sorted(k for k, v in params.items() if current.get(k) != v) if stack else ["create"],
            }
        except (Refused, av.AwsError) as exc:
            self.report["refused"] = str(exc)
            av.save(self.out, "enablement.json", self.report)
            print(f"REFUSED (nothing changed): {exc}", file=sys.stderr)
            return 2
        if self.a.cmd == "plan":
            av.save(self.out, "enablement.json", self.report)
            print(json.dumps(self.report["plan"], indent=2))
            return 0
        try:
            outputs = self.deploy(params)
            self.verify(outputs, provider, sub)
            self.variables(outputs)
        except (VerificationFailed, Refused, av.AwsError) as exc:
            self.report["failed"] = str(exc)
            av.save(self.out, "enablement.json", self.report)
            print(f"ENABLEMENT NOT VERIFIED (repository variable not set): {exc}", file=sys.stderr)
            return 1
        self.report["outcome"] = "enabled_and_verified"
        av.save(self.out, "enablement.json", self.report)
        print(f"enabled and verified: {outputs['ValidationRoleArn']} trusts {sub}; variable set on {self.a.repo}")
        return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["plan", "apply"])
    ap.add_argument("--repo", required=True, help="OWNER/REPO of this repository on GitHub")
    ap.add_argument("--vpc-id", required=True)
    ap.add_argument("--subnet-id", required=True)
    ap.add_argument("--availability-zone", required=True)
    ap.add_argument("--out", required=True, help="report directory, outside the repository")
    ap.add_argument("--profile", default=PROFILE)
    ap.add_argument("--stack-name", default=av.ACCESS_STACK_DEFAULT)
    ap.add_argument("--parity-object-arn", default="")
    ap.add_argument("--diagnostic-run", help="a specific successful diagnostic run id (default: the latest on main)")
    ap.add_argument("--dispatch", action="store_true", help="dispatch the diagnostic on main if it never ran")
    return Enable(ap.parse_args(argv)).main()


if __name__ == "__main__":
    sys.exit(main())
