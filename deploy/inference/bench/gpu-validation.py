#!/usr/bin/env python3
"""A09-1 validation on the real L4, run from the operator machine (standard library only). Reproducible sequence:

  0. preflight, with no side effects: the instance is `stopped`, the controller state is fresh (auto, no work), the
     evidence directory is outside the repository;
  1. job-driven cycle: tunnel keeper + controller `serve` + one worker in the background, then ONE durable job is
     submitted; the evidence must show the start requested by demand (no manual start), the EC2 start, the verified
     readiness and the job succeeded with a structured result;
  2. host evidence through SSM (host facts, boot timeline of this cold start);
  3. profile campaign while the instance is up (controller stopped so it cannot idle-stop under the measurements):
     per profile, `load` (switch + restart, memory sampled ON the host), `limits`, `run` (memory on the host) and the
     verdict; the campaign stops at the first profile that does not pass (D-A09-05: escalate to the user);
  4. final stop through the controller (`stop --wait`: pause, drain, stop);
  always (finally, also on failure, Ctrl-C, SIGTERM or SIGHUP): stop the background processes, restore the original
  profile in server.env, stop the instance if it is not stopped, verify `stopped` and write `final-state.json`.

Exit status: 0 validated; 1 validation failed or incomplete (instance verified stopped); 2 refused by the preflight
(nothing touched); 130 interrupted (instance verified stopped); 4 CLEANUP FAILED: the instance is NOT verified
stopped (printed on stderr; stop it by hand).

    export AWS_PROFILE=... AWS_REGION=us-east-1
    python3 deploy/inference/bench/gpu-validation.py --config "$EV/controller.toml" --instance-id "$IID" \\
        --key-file "$EV/api-key" --evidence "$EV/run-1"

Tool overrides (tests only): CCE_AWS_BIN, CCE_HOST_SRC, CCE_HOST_SUDO, CCE_POLL_S.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

BENCH = Path(__file__).resolve().parent
DEPLOY = BENCH.parent
CLI = DEPLOY / "bin" / "cce-inference"
MEASURE = BENCH / "measure.py"
SSM_RUN = BENCH / "ssm-run.sh"
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
        self.env = {**os.environ, "CCE_INFERENCE_CONFIG": str(Path(a.config).resolve())}
        self.bg: dict[str, subprocess.Popen[bytes]] = {}
        self.summary: dict[str, Any] = {"instance_id": a.instance_id, "steps": {}, "verdicts": {}}
        self.original_profile: str | None = None
        self.profile_changed = False

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
        return self.run([sys.executable, str(MEASURE), *args], timeout=timeout, check=False)

    # --- 0. preflight (no side effects) --------------------------------------------------------------------------

    def preflight(self) -> None:
        repo = repo_root()
        if self.ev == repo or repo in self.ev.parents:
            raise Refused(f"the evidence directory must be outside the repository: {self.ev}")
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
        self.save("preflight.json", {"instance_state": state, "controller": st})

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
        (self.ev / "host-facts.json").write_text(self.on_host("bench/host-facts.sh", timeout=120), encoding="utf-8")
        (self.ev / "boot-timings.json").write_text(self.on_host("bench/boot-timings.sh", timeout=120), encoding="utf-8")
        self.summary["steps"]["host_evidence"] = "collected"

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
        self.summary["original_profile"] = self.original_profile
        common = ["--base-url", self.a.base_url, "--key-file", self.a.key_file]
        for i, prof in enumerate(self.a.profiles.split()):
            self.log(f"profile {prof}: load, limits, run")
            self.profile_changed = True
            files = []
            out = self.ev / f"load-{prof}.json"
            r = self.measure(
                "load",
                *common,
                "--label",
                prof,
                "--timeout",
                str(self.a.load_timeout),
                "--restart-cmd",
                self.ssm_cmd("server/switch-profile.sh", prof),
                *self.memory_hooks(f"load-{prof}"),
                "--out",
                str(out),
            )
            if r.returncode != 0:
                raise ValidationFailed(f"load {prof}: exit {r.returncode}: {r.stderr[-400:]}")
            files.append(out)
            slots = int(json.loads(out.read_text(encoding="utf-8")).get("total_slots") or 1)
            out = self.ev / f"limits-{prof}.json"
            if self.measure("limits", *common, "--label", prof, "--out", str(out)).returncode == 0:
                files.append(out)
            runs = [("c1", self.a.requests, 1)]
            if slots > 1:
                runs.append((f"c{slots}", self.a.requests, slots))
            if i == 0:
                runs.append(("overload", max(3, self.a.requests // 3), 3))  # queue beyond the slot
            for tag, n, conc in runs:
                out = self.ev / f"run-{prof}-{tag}.json"
                args = ["--requests", str(n), "--concurrency", str(conc), "--prompt-tokens", str(self.a.prompt_tokens)]
                label = f"run-{prof}-{tag}"
                if (
                    self.measure(
                        "run", *common, *args, "--label", label, *self.memory_hooks(label), "--out", str(out)
                    ).returncode
                    == 0
                ):
                    files.append(out)
            if i == 0 and self.a.parity_pkg:
                self.run(
                    [
                        str(BENCH / "parity.sh"),
                        "--pkg",
                        self.a.parity_pkg,
                        "--base-url",
                        self.a.base_url,
                        "--key-file",
                        self.a.key_file,
                        "--out",
                        str(self.ev / f"parity-{prof}"),
                    ],
                    timeout=4 * 3600,
                    check=False,
                )
            vout = self.ev / f"verdict-{prof}.json"
            self.measure("verdict", "--profile", prof, "--out", str(vout), *map(str, files))
            verdict = json.loads(vout.read_text(encoding="utf-8"))
            self.summary["verdicts"][prof] = verdict
            self.log(f"profile {prof}: {verdict['verdict']} {verdict['criteria']}")
            if verdict["verdict"] != "pass":
                self.summary["escalation"] = f"{prof} did not pass ({verdict['criteria']}): D-A09-05, ask the user"
                break
        if not self.summary["verdicts"] or any(v["verdict"] != "pass" for v in self.summary["verdicts"].values()):
            raise ValidationFailed(self.summary.get("escalation", "no profile verdict"))

    # --- 4. final stop and cleanup ----------------------------------------------------------------------------------

    def final_stop(self) -> None:
        r = self.cli("stop", "--wait", str(self.a.stop_timeout), check=False, timeout=self.a.stop_timeout + 60)
        self.summary["steps"]["controller_stop"] = {"exit": r.returncode, "stdout": r.stdout.strip()}
        if r.returncode != 0 or "phase=stopped" not in r.stdout:
            raise ValidationFailed(f"controller stop --wait: {r.stdout.strip()} {r.stderr.strip()}")

    def cleanup(self) -> bool:
        """Always runs. Returns True only when the instance is verified `stopped`."""
        errors: list[str] = []
        self.stop_bg()
        if self.profile_changed and self.original_profile:
            try:  # persist the original profile for the next boot (no restart: the host is about to stop)
                self.on_host("server/switch-profile.sh", self.original_profile, "--no-restart", timeout=60)
            except Exception as exc:  # noqa: BLE001 - reported, never hides the stop below
                errors.append(f"profile_restore_failed: {exc}")
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
        record = {"instance_id": self.a.instance_id, "state": final, "fallback_stop": fallback, "errors": errors}
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
        except Refused as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
        for sig in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, _raise_interrupted)
        code = 0
        try:
            self.job_cycle()
            self.host_evidence()
            self.campaign()
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
        self.log(f"outcome: {self.summary['outcome']}; final state: {self.summary['final']['state']}")
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
