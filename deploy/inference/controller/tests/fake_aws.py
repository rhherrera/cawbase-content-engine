#!/usr/bin/env python3
"""A fake `aws` CLI for the GPU-validation and CI-orchestration tests: no AWS call is ever made. The "instance" is
the simulated host of `sim_host.sh` (a fake llama-server started through the real run-server.sh); SSM Run Command
executes the command locally; a port-forwarding session just sleeps. Every call is appended to
$FAKE_AWS_DIR/calls.jsonl.

Like the real service, Run Command is rejected unless the instance is `running` (a stopped instance has no SSM
agent online). Environment: FAKE_AWS_DIR, FAKE_AWS_SIM_DIR, FAKE_AWS_SIM_HOST; FAKE_AWS_STOP_FAIL=1 makes
stop-instances fail; FAKE_AWS_RUN_FAIL=SUBSTRING makes Run Command fail for the commands that contain SUBSTRING.

Account state for the local orchestration (deploy/inference/validation/aws_validation.py) lives in
$FAKE_AWS_DIR/state.json, seeded by the test: identities by access key (the standard chain: environment keys, then
AWS_PROFILE in AWS_SHARED_CREDENTIALS_FILE), base stack, quota, subnets, route tables, AMIs, SSM parameters.
Without it the fake behaves as for the GPU-validation tests (one simulated instance, text queries). Faults:
FAKE_AWS_DEPLOY_FAIL=1 (`cloudformation deploy` of the base stack fails), FAKE_AWS_CREATE_FAIL=1 (stack
creation fails and is deleted), FAKE_AWS_BOOTSTRAP=fail|hang, FAKE_AWS_DELETE_FAIL=1 (DELETE_FAILED, the volume
stays), FAKE_AWS_OTHER_GPU=ID:TYPE:STATE (another G instance in the account), FAKE_AWS_CREATE_DELAY_S=N (CloudFormation
finishes the creation N seconds later in a detached process, like AWS, whether or not the caller is alive),
FAKE_AWS_KILL_CALLER_AFTER=create-stack (the caller, i.e. the runner, is SIGKILLed right after AWS accepted
CreateStack and before it receives the StackId). `fake fire-schedules` does what EventBridge Scheduler would do with
every armed schedule (the AWS-side watchdog), including the one CloudFormation creates with the instance.
"""

from __future__ import annotations

import configparser
import fcntl
import fnmatch
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

IID = "i-0sim"
VOL = "vol-0simweights"


def opts(argv: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    i = 0
    while i < len(argv):
        if argv[i].startswith("--"):
            nxt = argv[i + 1] if i + 1 < len(argv) and not argv[i + 1].startswith("--") else ""
            out[argv[i][2:]] = nxt
            i += 2 if nxt else 1
        else:
            i += 1
    return out


def multi(argv: list[str], flag: str) -> list[str]:
    """All values after FLAG up to the next --option (e.g. several --filters)."""
    if flag not in argv:
        return []
    out = []
    for a in argv[argv.index(flag) + 1 :]:
        if a.startswith("--"):
            break
        out.append(a)
    return out


def sim(cmd: str) -> str:
    r = subprocess.run(
        [os.environ["FAKE_AWS_SIM_HOST"], os.environ["FAKE_AWS_SIM_DIR"], cmd],
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


def fail(code: str, msg: str, op: str = "operation") -> int:
    print(f"An error occurred ({code}) when calling the {op} operation: {msg} (simulated)", file=sys.stderr)
    return 254


@contextmanager
def state() -> Iterator[dict[str, Any] | None]:
    d = Path(os.environ["FAKE_AWS_DIR"])
    path = d / "state.json"
    if not path.exists():
        yield None
        return
    with open(d / "state.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        st = json.loads(path.read_text())
        before = json.dumps(st, sort_keys=True)
        yield st
        if json.dumps(st, sort_keys=True) != before:
            path.write_text(json.dumps(st, indent=1))


def inst_state(st: dict[str, Any] | None, iid: str) -> str:
    if st is not None:
        inst = st.get("instances", {}).get(iid)
        if inst and inst.get("terminated"):
            return "terminated"
    return sim("describe")


def inst_json(st: dict[str, Any], iid: str) -> dict[str, Any]:
    inst = st["instances"][iid]
    return {
        "InstanceId": iid,
        "InstanceType": inst["type"],
        "State": {"Name": inst_state(st, iid)},
        "CpuOptions": {"CoreCount": 2, "ThreadsPerCore": 2},
        "Tags": [{"Key": k, "Value": v} for k, v in inst.get("tags", {}).items()],
    }


def matches(inst: dict[str, Any], filters: list[str]) -> bool:
    tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
    for f in filters:
        name, values = f.split(",", 1)[0].split("=", 1)[1], f.split("Values=", 1)[1].split(",")
        if name == "instance-state-name":
            ok = inst["State"]["Name"] in values
        elif name == "instance-type":
            ok = any(fnmatch.fnmatch(inst["InstanceType"], v) for v in values)
        elif name.startswith("tag:"):
            ok = tags.get(name[4:]) in values
        else:
            ok = True
        if not ok:
            return False
    return True


def describe_instances(argv: list[str], o: dict[str, str]) -> int:
    with state() as st:
        if "query" in o or st is None:  # text form used by gpu-validation.py and the tunnel keeper
            print(inst_state(st, o.get("instance-ids", IID)))
            return 0
        found = [inst_json(st, i) for i in st.get("instances", {})]
        for other in filter(None, os.environ.get("FAKE_AWS_OTHER_GPU", "").split(";")):
            oid, otype, ostate = other.split(":")
            found.append({"InstanceId": oid, "InstanceType": otype, "State": {"Name": ostate}, "Tags": []})
        if "instance-ids" in o:
            found = [i for i in found if i["InstanceId"] == o["instance-ids"]]
            if not found:
                return fail("InvalidInstanceID.NotFound", f"{o['instance-ids']} does not exist", "DescribeInstances")
        found = [i for i in found if matches(i, multi(argv, "--filters"))]
        print(json.dumps({"Reservations": [{"Instances": found}] if found else []}))
    return 0


def outputs(stack: dict[str, Any]) -> list[dict[str, str]]:
    return [{"OutputKey": k, "OutputValue": v} for k, v in stack.get("outputs", {}).items()]


def complete_creation(st: dict[str, Any], name: str) -> None:
    """What CloudFormation does after accepting CreateStack: the instance, the volume, the in-stack watchdog."""
    sim_dir = Path(os.environ["FAKE_AWS_SIM_DIR"])
    stack = st["stacks"][name]
    if stack["status"] != "CREATE_IN_PROGRESS":
        return
    if os.environ.get("FAKE_AWS_CREATE_FAIL") == "1":
        stack["status"] = "DELETE_IN_PROGRESS"
        stack["events"] = [{"LogicalResourceId": "Host", "ResourceStatus": "CREATE_FAILED",
                            "ResourceStatusReason": "simulated: insufficient capacity"}]  # fmt: skip
        return
    params, tags, sid = stack["params"], stack["tags"], stack["id"]
    cfn = {"aws:cloudformation:stack-name": name, "aws:cloudformation:stack-id": sid}
    st.setdefault("instances", {})[IID] = {
        "type": params["InstanceType"],
        "tags": {**tags, "cce:role": "inference", **cfn},
    }
    st.setdefault("volumes", {})[VOL] = {"stack": name, "stack_id": sid}
    stack["outputs"] = {
        "InstanceId": IID,
        "EndpointUrl": "http://10.0.0.10:8080",
        "WeightsVolumeId": VOL,
        "EndpointSecurityGroupId": "sg-0sim",
        "ControllerPolicyArn": "arn:aws:iam::000000000000:policy/x",
    }
    if params.get("WatchdogStart"):  # AWS::Scheduler::Schedule of the template, created with the instance
        stack["outputs"]["WatchdogScheduleName"] = f"{name}-watchdog"
        st.setdefault("schedules", {})[f"{params['WatchdogScheduleGroup']}/{name}-watchdog"] = {
            "schedule-expression": "rate(10 minutes)",
            "start-date": params["WatchdogStart"],
            "end-date": params["WatchdogEnd"],
            "created_by": "cloudformation",
            "stack_id": sid,
            "target": {
                "Arn": "arn:aws:scheduler:::aws-sdk:ec2:stopInstances",
                "RoleArn": params["WatchdogRoleArn"],
                "Input": json.dumps({"InstanceIds": [IID]}),
            },
        }
    sim("start")  # a new instance boots and runs its bootstrap (cloud-init)
    mode = os.environ.get("FAKE_AWS_BOOTSTRAP", "ok")
    marks = ["bootstrap start", "model selected: qwen3.6-27b-q8_0", "weights volume mounted"]
    if mode == "ok":
        marks += ["runtime installed", "model installed and verified", "units installed; server started"]
    (sim_dir / "bootstrap.log").write_text("".join(f"2026-11-24T00:00:00.000Z {m}\n" for m in marks))
    status = {"ok": "status: done", "fail": "status: error\nerrors: bootstrap.sh exit 1"}.get(mode, "status: running")
    (sim_dir / "cloud-init-status").write_text(status + "\n")
    stack["status"] = "CREATE_COMPLETE"


def find_stack(st: dict[str, Any], name_or_id: str) -> tuple[str, dict[str, Any] | None]:
    if name_or_id.startswith("arn:"):
        for name, stack in st.get("stacks", {}).items():
            if stack.get("id") == name_or_id:
                return name, stack
        return "", None
    return name_or_id, st.get("stacks", {}).get(name_or_id)


ROLE_NAMES = {
    "CfnExecutionRole": ("cce-a09-cfn-execution", "cloudformation.amazonaws.com"),
    "WatchdogRole": ("cce-a09-watchdog", "scheduler.amazonaws.com"),
    "ValidationRole": ("cce-a09-github-validation", None),  # the former CI role: trusted by GitHub's OIDC provider
}
ACCT = "arn:aws:iam::000000000000"


def base_resources(body: str) -> dict[str, str]:
    part = body.split("\nResources:\n", 1)[1].split("\nOutputs:\n", 1)[0]
    return dict(re.findall(r"^  ([A-Za-z0-9]+):\n    Type: (\S+)", part, flags=re.M))


def role_doc(logical: str) -> dict[str, Any]:
    name, service = ROLE_NAMES[logical]
    principal: dict[str, str] = (
        {"Service": service} if service else {"Federated": f"{ACCT}:oidc-provider/token.actions.githubusercontent.com"}
    )
    stmt: dict[str, Any] = {"Effect": "Allow", "Principal": principal}
    stmt["Action"] = "sts:AssumeRole" if service else "sts:AssumeRoleWithWebIdentity"
    return {"Arn": f"{ACCT}:role/{name}", "AssumeRolePolicyDocument": {"Version": "2012-10-17", "Statement": [stmt]}}


def deployed_base(
    st: dict[str, Any], name: str, body: str, params: dict[str, str], old: dict[str, Any] | None
) -> dict[str, Any]:
    """The base stack after a create/update: exactly the template's resources; roles that left the template are
    deleted (as CloudFormation does on update), the others keep their names."""
    kept = {**(old or {}).get("params", {}), **params} if old else dict(params)
    resources: dict[str, dict[str, str]] = {}
    roles = st.setdefault("roles", {})
    for logical, rtype in base_resources(body).items():
        physical = ROLE_NAMES[logical][0] if logical in ROLE_NAMES else f"{name}-{logical}"
        if logical == "HostPermissionsBoundary":
            physical = f"{ACCT}:policy/cce-a09-host-boundary"
        if logical == "WatchdogScheduleGroup":
            physical = "cce-inference-watchdog"
        resources[logical] = {"type": rtype, "physical": physical}
        if rtype == "AWS::IAM::Role":
            roles[physical] = role_doc(logical)
    for logical, res in (old or {}).get("resources", {}).items():
        if logical not in resources and res["type"] == "AWS::IAM::Role":
            roles.pop(res["physical"], None)
    outputs = {
        "CfnExecutionRoleArn": f"{ACCT}:role/cce-a09-cfn-execution",
        "HostPermissionsBoundaryArn": f"{ACCT}:policy/cce-a09-host-boundary",
        "WatchdogRoleArn": f"{ACCT}:role/cce-a09-watchdog",
        "WatchdogScheduleGroup": "cce-inference-watchdog",
        "AllowedVpcId": kept["VpcId"],
        "AllowedSubnetId": kept["SubnetId"],
    }
    if "ValidationRole" in resources:
        outputs["ValidationRoleArn"] = f"{ACCT}:role/cce-a09-github-validation"
    return {
        "id": (old or {}).get("id") or f"arn:aws:cloudformation:us-east-1:000000000000:stack/{name}/{uuid.uuid4()}",
        "status": "UPDATE_COMPLETE" if old else "CREATE_COMPLETE",
        "params": {k: v for k, v in kept.items() if k in ("VpcId", "SubnetId", "InstanceType")}
        | ({k: v for k, v in kept.items() if k.startswith("GitHub")} if "ValidationRole" in resources else {}),
        "outputs": outputs,
        "resources": resources,
        "template": body,
    }


def caller_key() -> str | None:
    """The access key the standard chain would use: environment credentials first, then AWS_PROFILE's."""
    if os.environ.get("AWS_ACCESS_KEY_ID"):
        return os.environ["AWS_ACCESS_KEY_ID"]
    profile = os.environ.get("AWS_PROFILE")
    creds = os.environ.get("AWS_SHARED_CREDENTIALS_FILE")
    if profile and creds and Path(creds).is_file():
        cp = configparser.ConfigParser()
        cp.read(creds)
        if cp.has_section(profile):
            return cp.get(profile, "aws_access_key_id", fallback=None)
    return None


def cloudformation(op: str, o: dict[str, str]) -> int:
    kill_caller = False
    with state() as st:
        assert st is not None, "cloudformation needs a seeded state.json"
        stacks = st.setdefault("stacks", {})
        name, stack = find_stack(st, o.get("stack-name", ""))
        if op == "describe-stacks":
            if stack and stack["status"] == "DELETE_IN_PROGRESS" and not stack.get("delete_failed"):
                stack["status"] = "DELETE_COMPLETE"  # the deletion completes between two describes
            if not stack or stack["status"] == "DELETE_COMPLETE":
                return fail("ValidationError", f"Stack with id {o.get('stack-name')} does not exist", "DescribeStacks")
            out = {
                "StackName": name,
                "StackId": stack.get("id", f"arn:aws:cloudformation:us-east-1:000000000000:stack/{name}/seeded"),
                "StackStatus": stack["status"],
                "Outputs": outputs(stack),
                "Tags": [{"Key": k, "Value": v} for k, v in stack.get("tags", {}).items()],
                "Parameters": [{"ParameterKey": k, "ParameterValue": v} for k, v in stack.get("params", {}).items()],
            }
            print(json.dumps({"Stacks": [out]}))
            return 0
        if op == "create-stack":
            if stack and stack["status"] != "DELETE_COMPLETE":
                return fail("AlreadyExistsException", f"Stack [{name}] already exists", "CreateStack")
            tpl = o["template-body"].removeprefix("file://")
            assert Path(tpl).is_file() and o.get("role-arn") and o.get("on-failure") == "DELETE", o
            params = {p["ParameterKey"]: p["ParameterValue"] for p in json.loads(o["parameters"])}
            tags = {t["Key"]: t["Value"] for t in json.loads(o["tags"])}
            sid = f"arn:aws:cloudformation:us-east-1:000000000000:stack/{name}/{uuid.uuid4()}"
            stacks[name] = {
                "id": sid,
                "status": "CREATE_IN_PROGRESS",
                "params": params,
                "tags": tags,
                "role": o["role-arn"],
            }
            delay = float(os.environ.get("FAKE_AWS_CREATE_DELAY_S", "0"))
            if delay > 0:  # CloudFormation keeps going on its own, whatever happens to the caller
                subprocess.Popen(  # noqa: S603
                    [sys.executable, __file__, "fake", "complete-stack", name, str(delay)],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                complete_creation(st, name)
            kill_caller = os.environ.get("FAKE_AWS_KILL_CALLER_AFTER") == "create-stack"
            if not kill_caller:
                print(json.dumps({"StackId": sid}))
        elif op == "describe-stack-resource":
            if not stack or "outputs" not in stack or stack["status"].startswith("DELETE"):
                return fail(
                    "ValidationError", f"Resource Host does not exist for stack {name}", "DescribeStackResource"
                )
            print(json.dumps({"StackResourceDetail": {"PhysicalResourceId": IID, "ResourceStatus": "CREATE_COMPLETE"}}))
            return 0
        elif op == "deploy":  # the base stack (validation-base.yaml), as `cloudformation deploy` does it
            assert Path(o["template-file"]).is_file() and "--no-fail-on-empty-changeset" in sys.argv
            assert "CAPABILITY_NAMED_IAM" in sys.argv
            body = Path(o["template-file"]).read_text()
            params = dict(x.split("=", 1) for x in multi(sys.argv[1:], "--parameter-overrides"))
            same = stack and stack.get("params") == params and stack.get("template") == body
            if same and stack["status"].endswith("_COMPLETE"):
                print(f"\nNo changes to deploy. Stack {name} is up to date")
                return 0
            if os.environ.get("FAKE_AWS_DEPLOY_FAIL") == "1":
                return fail("ValidationError", "simulated deploy failure", "CreateChangeSet")
            stacks[name] = deployed_base(st, name, body, params, stack)
            print(f"\nSuccessfully created/updated stack - {name}")
            return 0
        elif op == "get-template":
            if not stack or stack["status"] == "DELETE_COMPLETE":
                return fail("ValidationError", f"Stack with id {name} does not exist", "GetTemplate")
            print(json.dumps({"TemplateBody": stack.get("template", "")}))
            return 0
        elif op == "list-stack-resources":
            if not stack or stack["status"] == "DELETE_COMPLETE":
                return fail("ValidationError", f"Stack with id {name} does not exist", "ListStackResources")
            res = [
                {
                    "LogicalResourceId": k,
                    "ResourceType": v["type"],
                    "PhysicalResourceId": v["physical"],
                    "ResourceStatus": "CREATE_COMPLETE",
                }
                for k, v in stack.get("resources", {}).items()
            ]
            print(json.dumps({"StackResourceSummaries": res}))
            return 0
        elif op == "describe-stack-events":
            print(json.dumps({"StackEvents": (stack or {}).get("events", [])}))
            return 0
        elif op == "delete-stack":
            if not stack:
                return 0
            if os.environ.get("FAKE_AWS_DELETE_FAIL") == "1":
                stack["status"] = "DELETE_FAILED"
                stack["delete_failed"] = True
                if IID in st.get("instances", {}):
                    sim("stop")
                    st["instances"][IID]["terminated"] = True  # the instance went; the volume did not
                return 0
            stack["status"] = "DELETE_IN_PROGRESS"
            inst = st.get("instances", {}).get(IID)
            if inst and inst["tags"].get("aws:cloudformation:stack-id") == stack.get("id"):
                sim("stop")
                inst["terminated"] = True
            if st.get("volumes", {}).get(VOL, {}).get("stack_id") == stack.get("id"):
                st["volumes"].pop(VOL)
            for key in [k for k, v in st.get("schedules", {}).items() if v.get("stack_id") == stack.get("id")]:
                st["schedules"].pop(key)  # the in-stack watchdog goes with its stack
            return 0
        else:
            return fail("InvalidAction", op)
    if kill_caller:
        os.kill(os.getppid(), signal.SIGKILL)  # the runner is lost before it learns the StackId
        return 0
    return 0


def ssm(op: str, o: dict[str, str]) -> int:
    state_dir = Path(os.environ["FAKE_AWS_DIR"])
    if op == "send-command":
        command = json.loads(o["parameters"])["commands"][0]
        with state() as st:
            current = inst_state(st, o["instance-ids"])
        if current != "running":
            return fail("InvalidInstanceId", "Instances not in a valid state for account", "SendCommand")
        bad = os.environ.get("FAKE_AWS_RUN_FAIL")
        if bad and bad in command:
            return fail("InternalServerError", "Run Command failed", "SendCommand")
        r = subprocess.run(["bash", "-c", command], capture_output=True, text=True, check=False)
        cid = uuid.uuid4().hex
        (state_dir / f"cmd-{cid}.json").write_text(
            json.dumps({"rc": r.returncode, "out": r.stdout, "err": r.stderr, "command": command})
        )
        print(cid)
        return 0
    if op == "get-command-invocation":
        rec = json.loads((state_dir / f"cmd-{o['command-id']}.json").read_text())
        field = o["query"]
        if field == "Status":
            print("Success" if rec["rc"] == 0 else "Failed")
        elif field == "StandardOutputContent":
            sys.stdout.write(rec["out"])
        else:
            sys.stdout.write(rec["err"])
        return 0
    if op == "start-session":
        os.execvp("sleep", ["sleep", "3600"])  # noqa: S606 - the port-forwarding session, killed with its group
    with state() as st:
        assert st is not None, f"ssm {op} needs a seeded state.json"
        params = st.setdefault("params", {})
        name = o.get("name", "")
        if op == "describe-instance-information":
            online = IID in st.get("instances", {}) and inst_state(st, IID) == "running"
            print(
                json.dumps({"InstanceInformationList": [{"InstanceId": IID, "PingStatus": "Online"}] if online else []})
            )
            return 0
        if op == "get-parameter":
            if name not in params:
                return fail("ParameterNotFound", name, "GetParameter")
            if o.get("query") == "Parameter.Value":  # run-server.sh on the host: --query ... --output text
                print(params[name]["Value"])
            else:
                print(json.dumps({"Parameter": {"Name": name, **params[name]}}))
            return 0
        if op == "put-parameter":
            if name in params and "overwrite" not in o:
                return fail("ParameterAlreadyExists", f"The parameter {name} already exists.", "PutParameter")
            value = o["value"]
            if value.startswith("file://"):
                value = Path(value.removeprefix("file://")).read_text().rstrip("\n")
            params[name] = {"Value": value, "Type": o.get("type", "String")}
            print(json.dumps({"Version": 1}))
            return 0
        if op == "delete-parameter":
            if params.pop(name, None) is None:
                return fail("ParameterNotFound", name, "DeleteParameter")
            return 0
    return fail("InvalidAction", op)


def scheduler(op: str, o: dict[str, str]) -> int:
    with state() as st:
        assert st is not None
        schedules = st.setdefault("schedules", {})
        key = f"{o.get('group-name', 'default')}/{o.get('name')}"
        if op == "create-schedule":
            if key in schedules:
                return fail("ConflictException", key, "CreateSchedule")
            schedules[key] = {k: o.get(k) for k in ("schedule-expression", "start-date", "end-date")}
            schedules[key]["target"] = json.loads(o["target"])
            print(json.dumps({"ScheduleArn": f"arn:aws:scheduler:us-east-1:000000000000:schedule/{key}"}))
            return 0
        if op == "get-schedule":
            if key not in schedules:
                return fail("ResourceNotFoundException", key, "GetSchedule")
            sc = schedules[key]
            print(json.dumps({"Name": o.get("name"), "GroupName": o.get("group-name"), "Target": sc["target"],
                              "StartDate": sc["start-date"], "EndDate": sc["end-date"],
                              "ScheduleExpression": sc["schedule-expression"], "State": "ENABLED"}))  # fmt: skip
            return 0
        if op == "delete-schedule":
            if schedules.pop(key, None) is None:
                return fail("ResourceNotFoundException", key, "DeleteSchedule")
            return 0
    return fail("InvalidAction", op)


def fire_schedules() -> int:
    """What EventBridge Scheduler does at the schedule's time: call the target API with its input."""
    with state() as st:
        assert st is not None
        for sched in st.get("schedules", {}).values():
            target = sched["target"]
            assert target["Arn"] == "arn:aws:scheduler:::aws-sdk:ec2:stopInstances", target
            for iid in json.loads(target["Input"])["InstanceIds"]:
                if iid == IID and inst_state(st, iid) in ("pending", "running"):
                    sim("stop")
            sched["fired"] = sched.get("fired", 0) + 1
    return 0


def seeded(key: str, o: dict[str, str], flag: str, field: str) -> int:
    with state() as st:
        assert st is not None
        items = [x for x in st.get(key, []) if x.get(field) == o.get(flag)]
        out_key = {"subnets": "Subnets", "images": "Images"}[key]
        print(json.dumps({out_key: items}))
    return 0


def main(argv: list[str]) -> int:
    state_dir = Path(os.environ["FAKE_AWS_DIR"])
    state_dir.mkdir(parents=True, exist_ok=True)
    with open(state_dir / "calls.jsonl", "a", encoding="utf-8") as fh:
        rec = {"ts": time.time(), "argv": argv, "profile": os.environ.get("AWS_PROFILE")}
        rec["region"] = os.environ.get("AWS_REGION")
        rec["key"] = caller_key()
        rec["env_static_key"] = bool(os.environ.get("AWS_ACCESS_KEY_ID"))
        fh.write(json.dumps(rec) + "\n")
    service, op = argv[0], argv[1]
    o = opts(argv[2:])
    if (service, op) == ("fake", "fire-schedules"):
        return fire_schedules()
    if (service, op) == ("fake", "complete-stack"):  # detached: CloudFormation finishing a creation on its own
        time.sleep(float(argv[3]))
        with state() as st:
            assert st is not None
            complete_creation(st, argv[2])
        return 0
    if (service, op) == ("ec2", "describe-instances"):
        return describe_instances(argv, o)
    if (service, op) == ("ec2", "start-instances"):
        sim("start")
        print(json.dumps({"StartingInstances": [{"InstanceId": o["instance-ids"]}]}))
        return 0
    if (service, op) == ("ec2", "stop-instances"):
        if os.environ.get("FAKE_AWS_STOP_FAIL") == "1":
            print("An error occurred (UnauthorizedOperation) when calling StopInstances (simulated)", file=sys.stderr)
            return 254
        sim("stop")
        print(json.dumps({"StoppingInstances": [{"InstanceId": o["instance-ids"]}]}))
        return 0
    if service == "ssm":
        return ssm(op, o)
    if service == "cloudformation":
        return cloudformation(op, o)
    if service == "scheduler":
        return scheduler(op, o)
    if service == "iam":
        with state() as st:
            assert st is not None
            if op == "list-open-id-connect-providers":
                print(json.dumps({"OpenIDConnectProviderList": [{"Arn": a} for a in st.get("oidc_providers", {})]}))
                return 0
            if op == "get-open-id-connect-provider":
                prov = st.get("oidc_providers", {}).get(o["open-id-connect-provider-arn"])
                if prov is None:
                    return fail("NoSuchEntity", "provider", "GetOpenIDConnectProvider")
                print(json.dumps(prov))
                return 0
            if op == "get-role":
                role = st.get("roles", {}).get(o["role-name"])
                if role is None:
                    return fail("NoSuchEntity", o["role-name"], "GetRole")
                print(json.dumps({"Role": {"RoleName": o["role-name"], **role}}))
                return 0
    if (service, op) == ("sts", "get-caller-identity"):
        with state() as st:
            identities = (st or {}).get("identities")
        if not identities:  # GPU-validation tests: any identity
            print(json.dumps({"Account": "000000000000", "Arn": f"{ACCT}:user/sim"}))
            return 0
        ident = identities.get(caller_key() or "")
        if ident is None:
            return fail(
                "InvalidClientTokenId", "The security token included in the request is invalid", "GetCallerIdentity"
            )
        print(json.dumps({"Account": ident["Account"], "Arn": ident["Arn"], "UserId": "AIDASIM"}))
        return 0
    if (service, op) == ("service-quotas", "get-service-quota"):
        with state() as st:
            assert st is not None
            print(json.dumps({"Quota": {"QuotaCode": o["quota-code"], "Value": st["quota"]}}))
        return 0
    if (service, op) == ("ec2", "describe-instance-type-offerings"):
        loc = multi(argv, "--filters")[-1].split("Values=", 1)[1]
        with state() as st:
            assert st is not None
            offers = [{"InstanceType": "g6e.xlarge", "Location": loc}] if loc in st["offered_azs"] else []
            print(json.dumps({"InstanceTypeOfferings": offers}))
        return 0
    if (service, op) == ("ec2", "describe-vpcs"):
        with state() as st:
            assert st is not None
            vpcs = {s["VpcId"] for s in st.get("subnets", [])} | set(st.get("vpcs", []))
            print(json.dumps({"Vpcs": [{"VpcId": o["vpc-ids"]}] if o["vpc-ids"] in vpcs else []}))
        return 0
    if (service, op) == ("ec2", "describe-subnets"):
        return seeded("subnets", o, "subnet-ids", "SubnetId")
    if (service, op) == ("ec2", "describe-images"):
        return seeded("images", o, "image-ids", "ImageId")
    if (service, op) == ("ec2", "describe-route-tables"):
        filters = multi(argv, "--filters")
        with state() as st:
            assert st is not None
            subnet = next((f.split("Values=")[1] for f in filters if "association.subnet-id" in f), None)
            vpc = next((f.split("Values=")[1] for f in filters if "Name=vpc-id" in f), None)
            tables = [
                t
                for t in st.get("route_tables", [])
                if (subnet and t.get("subnet") == subnet) or (vpc and t.get("main") and t.get("vpc") == vpc)
            ]
            print(json.dumps({"RouteTables": tables}))
        return 0
    if (service, op) == ("ec2", "describe-volumes"):
        flt = multi(argv, "--filters")[0]
        value = flt.split("Values=", 1)[1]
        field = "stack_id" if "stack-id" in flt else "stack"
        with state() as st:
            assert st is not None
            vols = [
                {"VolumeId": v, "State": "available"} for v, d in st.get("volumes", {}).items() if d.get(field) == value
            ]
            print(json.dumps({"Volumes": vols}))
        return 0
    if (service, op) == ("s3", "cp"):
        src = argv[2].removeprefix("s3://")
        shutil.copy(Path(os.environ["FAKE_AWS_S3_DIR"]) / src, argv[3])
        return 0
    print(f"fake aws: unsupported {service} {op}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
