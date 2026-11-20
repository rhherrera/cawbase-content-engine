#!/usr/bin/env python3
"""A09-1 validation on the real AWS target (g6e.xlarge, NVIDIA L40S, Qwen3.6-27B Q8_0, all weights on the GPU), run
from the operator machine (standard library only). Reproducible sequence:

  0. preflight, with no side effects: the instance is `stopped`, the controller state is fresh (auto, no work), the
     evidence directory is outside the repository, every profile is defined in this checkout, the controller's
     `model_alias` is the alias of the manifest's AWS target model, and the interpreter is the controller
     environment's (it also runs every child: `cce-inference` through CCE_PYTHON and measure.py);
  1. job-driven cycle: tunnel keeper + controller `serve` + one worker in the background, then ONE durable job is
     submitted; the evidence must show the start requested by demand (no manual start), the EC2 start, the verified
     readiness and the job succeeded with a structured result;
  2. host evidence through SSM (host facts, boot timeline of this cold start); the served artifact must be the
     manifest's AWS target (model id, file, alias, size and SHA-256 of its verification record), or the run fails;
  3. profile campaign while the instance is up (controller stopped so it cannot idle-stop under the measurements):
     per profile, `load` (switch + restart, memory sampled ON the host), `limits`, every expected run (`c1`; `cN`
     for N slots; `overload` for the first, reference profile; memory on the host) and the verdict over ALL of them:
     a measurement that ran and failed leaves a `status: failed` record, never a gap; the campaign stops at the first
     profile that does not pass (D-A09-05: escalate to the user);
  4. while the instance is still `running`: restore the original profile in server.env through Run Command and
     verify it (`switch-profile.sh --current`); it takes effect at the next start;
  5. final stop through the controller (`stop --wait`: pause, drain, stop);
  always (finally, also on failure, Ctrl-C, SIGTERM or SIGHUP): stop the background processes; if the profile is
  not restored yet and the instance is `running`, restore and verify it (a failure is recorded, never blocks the
  stop); stop the instance if it is not stopped, verify `stopped` and write `final-state.json`, which records apart
  whether the profile was restored and whether EC2 is stopped.

Exit status: 0 validated (every profile passed, profile restored, instance verified stopped); 1 validation failed or
incomplete, or the profile was not restored (instance verified stopped); 2 refused by the preflight (nothing
touched); 130 interrupted (instance verified stopped); 4 CLEANUP FAILED: the instance is NOT verified stopped
(printed on stderr; stop it by hand).

    export AWS_PROFILE=... AWS_REGION=us-east-1
    "$CCE_PYTHON" deploy/inference/bench/gpu-validation.py --config "$EV/controller.toml" --instance-id "$IID" \\
        --key-file "$EV/api-key" --evidence "$EV/run-1"

Every measurement sends and requires the served alias (`--model`); parity.sh gets it and the artifact record.

Tool overrides (tests only): CCE_AWS_BIN, CCE_HOST_SRC, CCE_HOST_SUDO, CCE_POLL_S, CCE_MEASURE_SCRIPT, CCE_MANIFEST.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shlex
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

BENCH = Path(__file__).resolve().parent
DEPLOY = BENCH.parent
CLI = DEPLOY / "bin" / "cce-inference"
MEASURE = BENCH / "measure.py"
SSM_RUN = BENCH / "ssm-run.sh"
PROFILES_DIR = DEPLOY / "server" / "profiles"
MANIFEST = DEPLOY / "manifest.json"
DEFAULT_PROFILES = "reference-8k-1slot candidate-16k-1slot candidate-16k-2slot"


class Interrupted(BaseException):
    pass


class Refused(Exception):
    pass


class ValidationFailed(Exception):
    pass


def repo_root() -> Path:
    """The Git work tree that contains this checkout (evidence must stay outside it); the deploy directory when the
    files are not in a Git work tree."""
    r = subprocess.run(
        ["git", "-C", str(DEPLOY), "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False
    )
    return Path(r.stdout.strip()).resolve() if r.returncode == 0 and r.stdout.strip() else DEPLOY


def _load_measure() -> Any:
    """measure.py as a module: one source of truth for the expected runs of each profile."""
    spec = importlib.util.spec_from_file_location("cce_measure", MEASURE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def profile_slots(name: str) -> int:
    """Slots of a profile from its definition in this checkout (the same file the host runs)."""
    path = PROFILES_DIR / f"{name}.env"
    if not path.is_file():
        raise Refused(f"unknown profile {name!r}: no {path.name} in {PROFILES_DIR}")
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("CCE_PARALLEL="):
            return int(line.split("=", 1)[1].strip().strip('"'))
    raise Refused(f"profile {name!r}: CCE_PARALLEL is not defined")


def aws_target_model() -> tuple[str, dict[str, Any]]:
    """The manifest's AWS target: (model id, model entry)."""
    m = json.loads(Path(os.environ.get("CCE_MANIFEST", str(MANIFEST))).read_text(encoding="utf-8"))
    model_id = m["targets"]["aws"]
    return model_id, m["models"][model_id]


def _raise_interrupted(signum: int, _frame: Any) -> None:
    raise Interrupted(signal.Signals(signum).name)


class Validation:
    def __init__(self, a: argparse.Namespace) -> None:
        self.a = a
        self.ev = Path(a.evidence).resolve()
        self.aws_bin = os.environ.get("CCE_AWS_BIN", "aws")
        self.host_src = os.environ.get("CCE_HOST_SRC", "/opt/cce-inference/src")
        self.sudo = os.environ.get("CCE_HOST_SUDO", "sudo")
        self.poll = float(os.environ.get("CCE_POLL_S", "5"))
        # every child runs on this interpreter (the controller environment's): cce-inference and measure.py
        self.env = {**os.environ, "CCE_INFERENCE_CONFIG": str(Path(a.config).resolve()), "CCE_PYTHON": sys.executable}
        self.measure_script = os.environ.get("CCE_MEASURE_SCRIPT", str(MEASURE))
        self.bg: dict[str, subprocess.Popen[bytes]] = {}
        self.summary: dict[str, Any] = {"instance_id": a.instance_id, "steps": {}, "verdicts": {}}
        self.profiles = a.profiles.split()
        self.model_id, self.model = aws_target_model()
        self.alias: str = self.model["alias"]
        self.slots: dict[str, int] = {}
        self.original_profile: str | None = None
        self.profile_changed = False
        # recorded apart from the instance state: not_changed | restored | not_restored
        self.profile: dict[str, Any] = {"status": "not_changed", "original": None, "current": None, "attempts": []}

    # --- tools -----------------------------------------------------------------------------------------------

    def log(self, msg: str) -> None:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    def run(self, argv: list[str], *, timeout: float = 900, check: bool = True) -> subprocess.CompletedProcess[str]:
        r = subprocess.run(argv, capture_output=True, text=True, env=self.env, timeout=timeout, check=False)
        if check and r.returncode != 0:
            raise ValidationFailed(
                f"{Path(argv[0]).name} {' '.join(argv[1:3])}: exit {r.returncode}: {r.stderr[-400:]}"
            )
        return r

    def aws(self, *args: str, check: bool = True) -> str:
        return self.run([self.aws_bin, *args], timeout=120, check=check).stdout.strip()

    def ec2_state(self) -> str:
        r = self.run(
            [
                self.aws_bin,
                "ec2",
                "describe-instances",
                "--instance-ids",
                self.a.instance_id,
                "--query",
                "Reservations[0].Instances[0].State.Name",
                "--output",
                "text",
            ],
            timeout=60,
            check=False,
        )
        return r.stdout.strip() if r.returncode == 0 and r.stdout.strip() else "unknown"

    def host_cmd(self, script: str, *args: str) -> str:
        parts = [self.sudo] if self.sudo else []
        return " ".join(shlex.quote(p) for p in [*parts, f"{self.host_src}/{script}", *args])

    def on_host(self, script: str, *args: str, timeout: float = 600) -> str:
        cmd = self.host_cmd(script, *args)
        return self.run([str(SSM_RUN), self.a.instance_id, cmd, str(int(timeout))], timeout=timeout + 120).stdout

    def ssm_cmd(self, script: str, *args: str) -> str:
        """The operator-side command line that runs `script` on the host (for measure.py hooks)."""
        return shlex.join([str(SSM_RUN), self.a.instance_id, self.host_cmd(script, *args)])

    def cli(self, *args: str, check: bool = True, timeout: float = 120) -> subprocess.CompletedProcess[str]:
        return self.run([str(CLI), *args], check=check, timeout=timeout)

    def start_bg(self, name: str, argv: list[str]) -> None:
        log = open(self.ev / f"{name}.log", "ab")  # noqa: SIM115 - closed with the process
        self.bg[name] = subprocess.Popen(
            argv, stdout=log, stderr=subprocess.STDOUT, env=self.env, start_new_session=True
        )

    def stop_bg(self, *names: str) -> None:
        for name in names or tuple(self.bg):
            p = self.bg.pop(name, None)
            if p is None:
                continue
            try:
                os.killpg(p.pid, signal.SIGTERM)
                p.wait(timeout=15)
            except (ProcessLookupError, PermissionError):
                pass
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)

    def save(self, name: str, data: Any) -> None:
        (self.ev / name).write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    def measure(self, *args: str, timeout: float = 3600) -> subprocess.CompletedProcess[str]:
        return self.run([sys.executable, self.measure_script, *args], timeout=timeout, check=False)

    def measured(self, kind: str, out: Path, args: list[str], *, label: str, tag: str | None = None) -> dict[str, Any]:
        """One measurement whose evidence file always exists afterwards: the measurement itself, or, when it ran and
        failed (non-zero exit, timeout, no readable output), a `status: failed` record. Never omitted."""
        out.unlink(missing_ok=True)  # a file from an earlier run is never taken for this one
        timeout = self.a.load_timeout + 600 if kind == "load" else 3600
        try:
            r = self.measure(kind, *args, "--label", label, "--out", str(out), timeout=timeout)
            code: int | str = r.returncode
            err = r.stderr.strip()[-400:]
        except subprocess.TimeoutExpired:
            code, err = "timeout", f"no result after {timeout:.0f}s"
        try:
            doc = json.loads(out.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            doc = None
        if code != 0 or not isinstance(doc, dict):
            doc = {"kind": kind, "tag": tag, "label": label, "status": "failed", "exit": code, "error": err}
            self.save(out.name, doc)
            self.log(f"{label}: measurement FAILED (exit {code}); recorded in {out.name}")
        return doc

    # --- 0. preflight (no side effects) --------------------------------------------------------------------------

    def preflight(self) -> None:
        repo = repo_root()
        if self.ev == repo or repo in self.ev.parents:
            raise Refused(f"the evidence directory must be outside the repository: {self.ev}")
        given = os.environ.get("CCE_PYTHON")
        if given and os.path.abspath(given) != os.path.abspath(sys.executable):
            raise Refused(f"run this script with the controller interpreter CCE_PYTHON={given} (not {sys.executable})")
        if sys.version_info < (3, 13):  # noqa: UP036 - the operator may launch it with an older python3
            raise Refused(f"the controller needs Python >= 3.13 (this is {sys.version.split()[0]})")
        if not self.profiles:
            raise Refused("no profile to validate")
        configured = tomllib.loads(Path(self.a.config).read_text(encoding="utf-8"))["endpoint"]["model_alias"]
        if configured != self.alias:
            raise Refused(
                f"artifact mismatch: the controller model_alias is {configured!r}, the AWS target "
                f"{self.model_id} serves {self.alias!r} (manifest.json)"
            )
        self.slots = {p: profile_slots(p) for p in self.profiles}
        if not Path(self.a.key_file).is_file():
            raise Refused("missing --key-file")
        state = self.ec2_state()
        if state != "stopped":
            raise Refused(f"the instance must be stopped before the validation (found {state}); nothing was changed")
        st = json.loads(self.cli("status", "--json").stdout)
        fresh = (
            st["phase"] == "stopped"
            and not st["autostart_paused"]
            and not st["blocked"]
            and st["pending"] == 0
            and st["active"] == 0
        )
        if not fresh:
            raise Refused("the controller state must be fresh (auto, stopped, no work): use a new db_path")
        self.ev.mkdir(parents=True, exist_ok=True)
        python = {"executable": sys.executable, "version": sys.version.split()[0]}
        target = {"model_id": self.model_id, **{k: self.model[k] for k in ("alias", "file", "bytes", "sha256")}}
        self.save(
            "preflight.json",
            {"instance_state": state, "controller": st, "python": python, "slots": self.slots, "target": target},
        )

    # --- 1. job-driven cycle ---------------------------------------------------------------------------------------

    def tunnel_keeper(self) -> list[str]:
        params = json.dumps({"portNumber": ["8080"], "localPortNumber": [str(self.a.tunnel_port)]})
        session = shlex.join(
            [
                self.aws_bin,
                "ssm",
                "start-session",
                "--target",
                self.a.instance_id,
                "--document-name",
                "AWS-StartPortForwardingSession",
                "--parameters",
                params,
            ]
        )
        state = shlex.join(
            [
                self.aws_bin,
                "ec2",
                "describe-instances",
                "--instance-ids",
                self.a.instance_id,
                "--query",
                "Reservations[0].Instances[0].State.Name",
                "--output",
                "text",
            ]
        )
        loop = f'while :; do if [ "$({state})" = running ]; then {session}; fi; sleep {self.poll}; done'
        return ["bash", "-c", loop]

    def job_cycle(self) -> None:
        if not self.a.no_tunnel:
            self.start_bg("tunnel", self.tunnel_keeper())
        self.start_bg("controller", [str(CLI), "serve"])
        self.start_bg("worker", [str(CLI), "work"])
        job = self.ev / "job.json"
        payload = {
            "messages": [
                {"role": "system", "content": "Clasificá el texto y listá entidades sensibles. Respondé solo JSON."},
                {"role": "user", "content": "Texto sintético de validación: reunión de equipo sin datos reales."},
            ],
            "schema": {
                "type": "object",
                "properties": {
                    "categoria": {"type": "string", "enum": ["COMERCIAL", "LEGAL", "FINANZAS", "GENERAL"]},
                    "entidades": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["categoria", "entidades"],
            },
            "max_tokens": 3072,
        }
        job.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        t_submit = time.time()
        jid = self.cli("submit", str(job)).stdout.strip()
        self.log(f"job {jid} submitted; waiting for the job-driven start (no manual start)")
        marks: dict[str, float] = {"submitted": t_submit}
        deadline = time.monotonic() + self.a.job_timeout
        record: dict[str, Any] = {}
        while time.monotonic() < deadline:
            state = self.ec2_state()
            if state in ("pending", "running"):
                marks.setdefault(f"ec2_{state}_observed", time.time())
            record = json.loads(self.cli("job", jid).stdout)
            if record["status"] in ("succeeded", "failed", "cancelled"):
                break
            time.sleep(self.poll)
        events = [json.loads(line) for line in self.cli("events", "--limit", "1000").stdout.splitlines()][::-1]
        self.save("events.json", events)
        self.save("job-result.json", record)
        demand = [e for e in events if e["kind"] == "start_requested" and e["ts"] >= t_submit - 1]
        ready = [e for e in events if e["kind"] == "ready"]
        result = json.loads(record["result"]) if isinstance(record.get("result"), str) else record.get("result")
        checks = {
            "start_requested_by_demand": bool(demand) and demand[0].get("reason") == "demand",
            "no_manual_start": not any(e["kind"] == "manual_start" for e in events),
            "ec2_started": "ec2_running_observed" in marks,
            "verified_ready": bool(ready) and ready[0].get("code") in ("canary_ok", "models_ok"),
            "job_succeeded": record.get("status") == "succeeded",
            "structured_result": isinstance(result, dict) and isinstance(result.get("output"), dict),
        }
        timeline = {
            **marks,
            "start_requested": demand[0]["ts"] if demand else None,
            "ready": ready[0]["ts"] if ready else None,
            "ready_waited_s": ready[0].get("waited_s") if ready else None,
            "job_started": record.get("started_at"),
            "job_finished": record.get("finished_at"),
        }
        self.summary["steps"]["job_cycle"] = {"job_id": jid, "checks": checks, "timeline": timeline}
        self.save("job-cycle.json", self.summary["steps"]["job_cycle"])
        if not all(checks.values()):
            raise ValidationFailed(f"job-driven cycle: {checks}")
        self.log(f"job-driven cycle verified: {checks}")

    # --- 2. host evidence -------------------------------------------------------------------------------------------

    def host_evidence(self) -> None:
        facts_raw = self.on_host("bench/host-facts.sh", timeout=120)
        (self.ev / "host-facts.json").write_text(facts_raw, encoding="utf-8")
        (self.ev / "boot-timings.json").write_text(self.on_host("bench/boot-timings.sh", timeout=120), encoding="utf-8")
        try:
            facts = json.loads(facts_raw)
        except ValueError as exc:
            raise ValidationFailed(f"host facts are not JSON: {exc}") from exc
        record = facts.get("model") if isinstance(facts.get("model"), dict) else {}
        served = {
            "model_id": facts.get("model_id"),
            "file": Path(str(facts.get("model_path") or "")).name,
            "alias": facts.get("model_alias"),
            "record_file": record.get("file"),
            "record_bytes": record.get("bytes"),
            "record_sha256": record.get("sha256"),
        }
        expected = {
            "model_id": self.model_id,
            "file": self.model["file"],
            "alias": self.alias,
            "record_file": self.model["file"],
            "record_bytes": self.model["bytes"],
            "record_sha256": self.model["sha256"],
        }
        self.save("model-record.json", record or None)
        artifact = {"expected": expected, "served": served, "match": served == expected}
        self.summary["steps"]["host_evidence"] = {"artifact": artifact}
        if served != expected:
            raise ValidationFailed(f"artifact mismatch on the host: served {served}, expected {expected}")
        self.log(f"served artifact verified: {self.model_id} ({self.model['file']}, sha256 {self.model['sha256']})")

    # --- 3. profile campaign ----------------------------------------------------------------------------------------

    def memory_hooks(self, label: str) -> list[str]:
        return [
            "--memory-start-cmd",
            self.ssm_cmd("bench/mem-sample.sh", "start", label),
            "--memory-stop-cmd",
            self.ssm_cmd("bench/mem-sample.sh", "stop", label),
            "--expect-instance-id",
            self.a.instance_id,
        ]

    def campaign(self) -> None:
        self.stop_bg("controller", "worker")  # the controller must not idle-stop under the measurements
        self.original_profile = self.on_host("server/switch-profile.sh", "--current", timeout=60).strip()
        self.profile["original"] = self.summary["original_profile"] = self.original_profile
        expected_runs = _load_measure().expected_runs
        common = ["--base-url", self.a.base_url, "--key-file", self.a.key_file, "--model", self.alias]
        for i, prof in enumerate(self.profiles):
            reference, slots = i == 0, self.slots[prof]
            self.log(f"profile {prof} ({slots} slot(s)): load, limits, runs")
            self.profile_changed = True
            files = [self.ev / f"load-{prof}.json"]
            load = self.measured(
                "load",
                files[0],
                [
                    *common,
                    "--timeout",
                    str(self.a.load_timeout),
                    "--restart-cmd",
                    self.ssm_cmd("server/switch-profile.sh", prof),
                    *self.memory_hooks(f"load-{prof}"),
                ],
                label=prof,
            )
            if load.get("status") != "failed":  # without the profile loaded, any further number would be another's
                files.append(self.ev / f"limits-{prof}.json")
                self.measured("limits", files[-1], common, label=prof)
                for tag, conc in expected_runs(slots, reference).items():
                    n = max(3, self.a.requests // 3) if tag == "overload" else self.a.requests
                    args = [
                        "--tag",
                        tag,
                        "--requests",
                        str(n),
                        "--concurrency",
                        str(conc),
                        "--prompt-tokens",
                        str(self.a.prompt_tokens),
                    ]
                    label = f"run-{prof}-{tag}"
                    files.append(self.ev / f"{label}.json")
                    self.measured("run", files[-1], [*common, *args, *self.memory_hooks(label)], label=label, tag=tag)
                if reference and self.a.parity_pkg:
                    self.run(
                        [
                            str(BENCH / "parity.sh"),
                            "--pkg",
                            self.a.parity_pkg,
                            "--base-url",
                            self.a.base_url,
                            "--key-file",
                            self.a.key_file,
                            "--model",
                            self.alias,
                            "--artifact",
                            str(self.ev / "model-record.json"),
                            "--out",
                            str(self.ev / f"parity-{prof}"),
                        ],
                        timeout=4 * 3600,
                        check=False,
                    )
            vout = self.ev / f"verdict-{prof}.json"
            vout.unlink(missing_ok=True)
            flags = ["--reference"] if reference else []
            vargs = ["--profile", prof, "--slots", str(slots), "--model", self.alias, *flags, "--out", str(vout)]
            self.measure("verdict", *vargs, *map(str, files))
            try:
                verdict = json.loads(vout.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValidationFailed(f"verdict {prof}: no verdict written ({exc})") from exc
            self.summary["verdicts"][prof] = verdict
            self.log(f"profile {prof}: {verdict['verdict']} {verdict['criteria']} runs={verdict['runs']}")
            if verdict["verdict"] != "pass":
                self.summary["escalation"] = (
                    f"{prof} did not pass ({verdict['criteria']}, runs {verdict['runs']}, failures "
                    f"{verdict['failures']}): D-A09-05, ask the user"
                )
                break
        if not self.summary["verdicts"] or any(v["verdict"] != "pass" for v in self.summary["verdicts"].values()):
            raise ValidationFailed(self.summary.get("escalation", "no profile verdict"))

    # --- 4. restore the original profile while the instance is running ---------------------------------------------

    def restore_profile(self) -> None:
        """Persist the original profile in server.env through Run Command and verify it, while the instance is still
        running (Run Command needs the SSM agent online). No restart: it takes effect at the next start."""
        if not (self.profile_changed and self.original_profile) or self.profile["status"] == "restored":
            return
        attempt: dict[str, Any] = {"ts": time.time()}
        self.profile["attempts"].append(attempt)
        self.profile["status"] = "not_restored"
        try:
            self.on_host("server/switch-profile.sh", self.original_profile, "--no-restart", timeout=60)
            current = self.on_host("server/switch-profile.sh", "--current", timeout=60).strip()
        except Exception as exc:
            attempt["error"] = str(exc)[-400:]
            raise ValidationFailed(f"profile restore: {exc}") from exc
        self.profile["current"] = attempt["current"] = current
        if current != self.original_profile:
            attempt["error"] = f"server.env has {current!r}, expected {self.original_profile!r}"
            raise ValidationFailed(f"profile restore: {attempt['error']}")
        self.profile["status"] = "restored"
        self.summary["steps"]["profile_restore"] = "restored"
        self.log(f"profile {self.original_profile} restored and verified in server.env")

    # --- 5. final stop and cleanup ----------------------------------------------------------------------------------

    def final_stop(self) -> None:
        r = self.cli("stop", "--wait", str(self.a.stop_timeout), check=False, timeout=self.a.stop_timeout + 60)
        self.summary["steps"]["controller_stop"] = {"exit": r.returncode, "stdout": r.stdout.strip()}
        if r.returncode != 0 or "phase=stopped" not in r.stdout:
            raise ValidationFailed(f"controller stop --wait: {r.stdout.strip()} {r.stderr.strip()}")

    def cleanup(self) -> bool:
        """Always runs. Returns True only when the instance is verified `stopped`."""
        errors: list[str] = []
        self.stop_bg()
        if self.profile_changed and self.original_profile and self.profile["status"] != "restored":
            # before the stop: Run Command only reaches a running instance; a failure never skips the stop below
            state = self.ec2_state()
            if state == "running":
                try:
                    self.restore_profile()
                except Exception as exc:  # noqa: BLE001 - recorded, never hides the stop below
                    errors.append(f"profile_restore_failed: {exc}")
            else:
                self.profile["status"] = "not_restored"
                errors.append(f"profile_restore_skipped: instance {state}, Run Command needs it running")
        state = self.ec2_state()
        fallback = False
        if state in ("pending", "running", "stopping"):
            if state != "stopping":
                fallback = True
                r = self.run(
                    [self.aws_bin, "ec2", "stop-instances", "--instance-ids", self.a.instance_id],
                    timeout=120,
                    check=False,
                )
                if r.returncode != 0:
                    errors.append(f"stop_instances_failed: {r.stderr.strip()[-300:]}")
            deadline = time.monotonic() + self.a.stop_timeout
            while time.monotonic() < deadline and (state := self.ec2_state()) != "stopped":
                time.sleep(self.poll)
        final = self.ec2_state()
        record = {
            "instance_id": self.a.instance_id,
            "state": final,
            "instance_stopped": final == "stopped",
            "fallback_stop": fallback,
            "profile": self.profile,
            "errors": errors,
        }
        self.summary["final"] = record
        try:
            self.save("final-state.json", record)
        except OSError as exc:
            errors.append(f"evidence_write_failed: {exc}")
        for e in errors:
            print(f"WARNING cleanup: {e}", file=sys.stderr)
        return final == "stopped"

    def main(self) -> int:
        try:
            self.preflight()
        except (Refused, ValidationFailed) as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, _raise_interrupted)
        code = 0
        try:
            self.job_cycle()
            self.host_evidence()
            self.campaign()
            self.restore_profile()
            self.final_stop()
        except (KeyboardInterrupt, Interrupted) as exc:
            self.summary["outcome"] = f"interrupted: {exc or 'SIGINT'}"
            code = 130
        except Exception as exc:  # noqa: BLE001 - recorded; the stop below still runs
            self.summary["outcome"] = f"failed: {exc}"
            print(f"FAILED: {exc}", file=sys.stderr)
            code = 1
        else:
            self.summary["outcome"] = "validated"
        finally:
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, signal.SIG_IGN)  # never abort the cleanup half-way
            self.log("cleanup: stopping background processes and verifying the instance is stopped")
            stopped = self.cleanup()
            if code == 0 and self.profile["status"] not in ("restored", "not_changed"):
                self.summary["outcome"] = "failed: original profile not restored"  # never declared validated
                code = 1
            self.summary["profile"] = self.profile
            self.summary["exit"] = code if stopped else 4
            try:
                self.save("summary.json", self.summary)
            except OSError:
                pass
            if not stopped:
                print(
                    f"CLEANUP FAILED: instance {self.a.instance_id} is {self.summary['final']['state']}, NOT stopped. "
                    f"Stop it by hand: aws ec2 stop-instances --instance-ids {self.a.instance_id}",
                    file=sys.stderr,
                )
        self.log(
            f"outcome: {self.summary['outcome']}; profile: {self.profile['status']}; "
            f"final state: {self.summary['final']['state']}"
        )
        return int(self.summary["exit"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", required=True, help="controller TOML (fresh db_path, base_url through the tunnel)")
    ap.add_argument("--instance-id", required=True)
    ap.add_argument("--key-file", required=True)
    ap.add_argument("--evidence", required=True, help="output directory, outside the repository")
    ap.add_argument("--base-url", default="http://127.0.0.1:18080")
    ap.add_argument("--tunnel-port", type=int, default=18080)
    ap.add_argument("--no-tunnel", action="store_true", help="the operator reaches the endpoint directly (VPC)")
    ap.add_argument("--profiles", default=DEFAULT_PROFILES, help="in order; the first is the reference")
    ap.add_argument("--requests", type=int, default=20)
    ap.add_argument("--prompt-tokens", type=int, default=1800)
    ap.add_argument("--parity-pkg", help="unpacked external results package (outside Git) for parity.sh")
    ap.add_argument("--job-timeout", type=float, default=2400)
    ap.add_argument("--load-timeout", type=float, default=1800)
    ap.add_argument("--stop-timeout", type=float, default=900)
    return Validation(ap.parse_args()).main()


if __name__ == "__main__":
    sys.exit(main())
