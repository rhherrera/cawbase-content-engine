#!/usr/bin/env python3
"""A09-1 measurements on real hardware (standard library only; boto3 only for `ec2-cycle`). Writes JSON evidence.
Synthetic, deterministic inputs only: no benchmark cases, no real data.

Memory (VRAM/RAM) is sampled **on the measured host** by `mem-sample.sh`, started right before the load and stopped
right after it. From the operator machine the two hooks run it on EC2 through SSM (`ssm-run.sh`); on the host itself
`--memory-local` runs it directly. The result records the measured host, the sample counts and the sampling window,
and is `unavailable` (never a fictitious 0) when there are no samples, the window does not cover the load or the host
is not the expected one.

    # from the operator, through the tunnel, with memory sampled on EC2 during the same load
    python3 measure.py run --base-url http://127.0.0.1:18080 --key-file F --requests 20 --concurrency 1 \
        --prompt-tokens 1800 --expect-instance-id i-... --out r.json \
        --memory-start-cmd "bench/ssm-run.sh i-... 'sudo /opt/cce-inference/src/bench/mem-sample.sh start r1'" \
        --memory-stop-cmd  "bench/ssm-run.sh i-... 'sudo /opt/cce-inference/src/bench/mem-sample.sh stop r1'"
    # on the GPU host itself (on-premise): same, with the local sampler
    python3 measure.py run --base-url http://127.0.0.1:8080 --key-file F --memory-local --out r.json
    # restart of the server (time to HTTP, weights loaded, canary), memory as above
    python3 measure.py load --restart-cmd "..." --base-url ... --key-file ... --out load.json
    # context admission: largest admitted prompt succeeds, one token more is rejected before sending, server 400
    python3 measure.py limits --base-url ... --key-file ... --max-tokens 3072 --out limits.json
    # from the operator machine: EC2 start -> running -> HTTP up -> weights loaded -> canary, then stop -> stopped
    python3 measure.py ec2-cycle --instance-id i-... --region us-east-1 --profile AWS_PROFILE \
        --base-url http://127.0.0.1:18080 --key-file ./api-key --out cycle.json
    # profile verdict from the evidence files (criteria fixed before measuring): pass | fail | incomplete. Every
    # expected run must be evidenced (c1; cN for N slots; overload for the reference profile): a missing one is
    # `incomplete`, one that ran and failed (`status: failed`) is `fail`
    python3 measure.py verdict --profile reference-8k-1slot --slots 1 --reference --out verdict.json \
        load.json limits.json run-c1.json run-overload.json
"""

from __future__ import annotations

import argparse
import json
import random
import shlex
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

WORDS = (
    "el la los las un una de del en con por para contrato factura cliente proveedor servicio pago plazo informe "
    "proyecto equipo reunion acuerdo documento revision entrega fecha monto cuenta empresa area gestion propuesta "
    "analisis riesgo control proceso solicitud respuesta registro version anexo clausula condicion"
).split()
SCHEMA = {
    "type": "object",
    "properties": {
        "categoria": {"type": "string", "enum": ["COMERCIAL", "LEGAL", "FINANZAS", "GENERAL"]},
        "entidades": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"tipo": {"type": "string"}, "valor": {"type": "string", "minLength": 1}},
                "required": ["tipo", "valor"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["categoria", "entidades"],
    "additionalProperties": False,
}
SYSTEM = "Clasificá el texto en una categoría y listá entidades sensibles. Respondé solo con JSON válido."
MEM_SAMPLER = Path(__file__).resolve().parent / "mem-sample.sh"
MIN_FREE_VRAM_MIB = 1024  # criterion fixed before measuring (report §4)
WINDOW_TOLERANCE_S = 2.0  # clock granularity of the host sampler (whole seconds) and operator/host skew


class Http:
    def __init__(self, base_url: str, key: str | None, model: str | None = None) -> None:
        self.base = base_url.rstrip("/")
        self.key = key
        self.model = model  # the served alias (manifest models[...].alias); sent in every request

    def model_ids(self) -> list[str]:
        _, res = self.call("GET", "/v1/models", timeout=10)
        data = res.get("data") if isinstance(res, dict) else None
        return [str(m.get("id")) for m in data if isinstance(m, dict)] if isinstance(data, list) else []

    def call(self, method: str, path: str, body: Any = None, timeout: float = 600) -> tuple[int, Any]:
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw, status = r.read(), r.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        except (urllib.error.URLError, OSError):
            return 0, None
        try:
            return status, json.loads(raw) if raw else None
        except ValueError:
            return status, raw.decode(errors="replace")

    def tokens(self, messages: list[dict[str, str]]) -> int:
        _, tpl = self.call("POST", "/apply-template", {"messages": messages}, 60)
        _, tok = self.call(
            "POST", "/tokenize", {"content": tpl["prompt"], "add_special": True, "parse_special": True}, 60
        )
        return len(tok["tokens"])

    def chat(self, messages: list[dict[str, str]], max_tokens: int, schema: dict[str, Any] = SCHEMA) -> dict[str, Any]:
        body: dict[str, Any] = {
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "salida", "strict": True, "schema": schema},
            },
        }
        if self.model:
            body["model"] = self.model
        t = time.monotonic()
        status, res = self.call("POST", "/v1/chat/completions", body)
        out: dict[str, Any] = {"status": status, "latency_s": round(time.monotonic() - t, 3)}
        if status == 200:
            out["usage"] = res.get("usage")
            out["timings"] = res.get("timings")
            out["finish_reason"] = res["choices"][0].get("finish_reason")
            try:
                json.loads(res["choices"][0]["message"]["content"])
                out["json_ok"] = True
            except (TypeError, ValueError):
                out["json_ok"] = False
        else:
            out["error_type"] = (res or {}).get("error", {}).get("type") if isinstance(res, dict) else None
        return out


def key_from(path: str | None) -> str | None:
    if not path:
        return None
    return next(line.strip() for line in open(path, encoding="utf-8") if line.strip() and not line.startswith("#"))


def text_of_tokens(http: Http, target: int, seed: int) -> list[dict[str, str]]:
    rnd = random.Random(seed)
    words = [rnd.choice(WORDS) for _ in range(target * 2)]
    lo, hi = 1, len(words)
    best = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": words[0]}]
    while lo <= hi:
        mid = (lo + hi) // 2
        msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": " ".join(words[:mid])}]
        if http.tokens(msgs) <= target:
            best, lo = msgs, mid + 1
        else:
            hi = mid - 1
    return best


class MemoryProbe:
    """Runs the host sampler around a load: `start()` before, `finish(window)` after. The commands run on the
    operator machine; their effect is on the measured host (SSM) or on this host (`--memory-local`)."""

    def __init__(self, start_cmd: str | None, stop_cmd: str | None, expect_instance_id: str | None) -> None:
        self.start_cmd, self.stop_cmd, self.expect = start_cmd, stop_cmd, expect_instance_id
        self.start_error: str | None = None

    @classmethod
    def from_args(cls, a: argparse.Namespace) -> MemoryProbe:
        start, stop = a.memory_start_cmd, a.memory_stop_cmd
        if a.memory_local:
            label = f"measure-{int(time.time())}"
            start, stop = f"{MEM_SAMPLER} start {label}", f"{MEM_SAMPLER} stop {label}"
        return cls(start, stop, a.expect_instance_id)

    def start(self) -> None:
        if not self.start_cmd:
            return
        r = subprocess.run(shlex.split(self.start_cmd), capture_output=True, text=True, timeout=180, check=False)
        if r.returncode != 0:
            self.start_error = f"start_cmd_exit_{r.returncode}"

    def finish(self, window: dict[str, float]) -> dict[str, Any]:
        if not (self.start_cmd and self.stop_cmd):
            return memory_evidence(None, window, self.expect, reason="not_requested")
        r = subprocess.run(shlex.split(self.stop_cmd), capture_output=True, text=True, timeout=180, check=False)
        if self.start_error:
            return memory_evidence(None, window, self.expect, reason=self.start_error)
        if r.returncode != 0:
            return memory_evidence(None, window, self.expect, reason=f"stop_cmd_exit_{r.returncode}")
        return memory_evidence(r.stdout, window, self.expect)


def _unavailable(reason: str, **extra: Any) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason, "criterion": "unavailable", **extra}


def memory_evidence(
    raw: str | None, window: dict[str, float], expect_instance_id: str | None, *, reason: str | None = None
) -> dict[str, Any]:
    """Validate the host sampler summary against the load window. Only a summary with samples that covers the
    window, from the expected host, yields numbers and a pass/fail criterion; anything else is `unavailable`."""
    if raw is None:
        return _unavailable(reason or "no_summary")
    try:
        summary = json.loads(raw.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return _unavailable("invalid_summary")
    host = summary.get("host") or {}
    counts = {"vram_samples": summary.get("vram_samples", 0), "ram_samples": summary.get("ram_samples", 0)}
    window_seen = [summary.get("started_epoch"), summary.get("stopped_epoch")]
    base = {"host": host, "samples": counts, "sampler_window": window_seen}
    if summary.get("status") != "ok" or not counts["vram_samples"] or not counts["ram_samples"]:
        return _unavailable("no_samples", **base)
    if expect_instance_id and host.get("instance_id") != expect_instance_id:
        return _unavailable("host_mismatch", expected_instance_id=expect_instance_id, **base)
    started, stopped = summary.get("started_epoch"), summary.get("stopped_epoch")
    if not isinstance(started, (int, float)) or not isinstance(stopped, (int, float)):
        return _unavailable("window_unknown", **base)
    if started > window["start_epoch"] + WINDOW_TOLERANCE_S or stopped < window["end_epoch"] - WINDOW_TOLERANCE_S:
        return _unavailable("window_not_covered", load_window=[window["start_epoch"], window["end_epoch"]], **base)
    peak, total = summary.get("vram_peak_mib"), summary.get("vram_total_mib")
    free = total - peak if isinstance(total, (int, float)) and isinstance(peak, (int, float)) else None
    return {
        "status": "measured",
        **base,
        "vram_peak_mib": peak,
        "vram_total_mib": total,
        "vram_free_min_mib": free,
        "ram_used_peak_kib": summary.get("ram_used_peak_kib"),
        "criterion": "unavailable" if free is None else ("pass" if free >= MIN_FREE_VRAM_MIB else "fail"),
    }


def _window(t_wall: float) -> dict[str, float]:
    return {"start_epoch": round(t_wall, 3), "end_epoch": round(time.time(), 3)}


def props(http: Http) -> dict[str, Any]:
    _, p = http.call("GET", "/props", timeout=10)
    if not isinstance(p, dict):
        return {}
    return {"n_ctx_slot": p.get("default_generation_settings", {}).get("n_ctx"), "total_slots": p.get("total_slots")}


def wait_ready(http: Http, t0: float, timeout: float) -> dict[str, Any]:
    marks: dict[str, Any] = {}
    while time.monotonic() - t0 < timeout:
        status, _ = http.call("GET", "/health", timeout=5)
        if status and "http_up_s" not in marks:
            marks["http_up_s"] = round(time.monotonic() - t0, 3)
        if status == 200:
            marks["health_ok_s"] = round(time.monotonic() - t0, 3)
            break
        time.sleep(0.25)
    if "health_ok_s" in marks and http.model:
        listed = http.model_ids()  # the served alias must be the expected one (manifest), not only "some model"
        marks["models_listed"], marks["model_ok"] = listed, http.model in listed
    if "health_ok_s" in marks and marks.get("model_ok", True):
        c = http.chat(
            [{"role": "user", "content": 'Reply with {"ok": true}.'}],
            1024,
            {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
        )
        marks["canary"] = c
        if c.get("json_ok"):
            marks["ready_s"] = round(time.monotonic() - t0, 3)
    return marks


def cmd_load(a: argparse.Namespace, http: Http) -> dict[str, Any]:
    mem = MemoryProbe.from_args(a)
    mem.start()
    t_wall, t0 = time.time(), time.monotonic()
    subprocess.run(shlex.split(a.restart_cmd), check=True)
    marks = wait_ready(http, t0, a.timeout)
    window = _window(t_wall)
    res = {"kind": "load", "restart_cmd": a.restart_cmd, **marks, "memory": mem.finish(window), **props(http)}
    res["weights_load_s"] = round(marks["health_ok_s"] - marks["http_up_s"], 3) if "health_ok_s" in marks else None
    return res


def cmd_run(a: argparse.Namespace, http: Http) -> dict[str, Any]:
    msgs = [text_of_tokens(http, a.prompt_tokens, seed=i) for i in range(a.requests)]
    mem = MemoryProbe.from_args(a)
    mem.start()
    t_wall, t0 = time.time(), time.monotonic()
    with ThreadPoolExecutor(a.concurrency) as pool:
        results = list(pool.map(lambda m: http.chat(m, a.max_tokens), msgs))
    wall = time.monotonic() - t0
    window = _window(t_wall)
    ok = [r for r in results if r["status"] == 200]
    lat = [r["latency_s"] for r in ok]
    gen = sum((r.get("usage") or {}).get("completion_tokens", 0) for r in ok)
    tps = [r["timings"].get("predicted_per_second") for r in ok if r.get("timings")]
    return {
        "kind": "run",
        "tag": a.tag or f"c{a.concurrency}",
        "requests": a.requests,
        "concurrency": a.concurrency,
        "prompt_tokens_target": a.prompt_tokens,
        "max_tokens": a.max_tokens,
        "ok": len(ok),
        "json_ok": sum(1 for r in ok if r.get("json_ok")),
        "status_counts": {str(k): sum(1 for r in results if r["status"] == k) for k in {r["status"] for r in results}},
        "latency_median_s": statistics.median(lat) if lat else None,
        "latency_p95_s": sorted(lat)[max(0, int(len(lat) * 0.95) - 1)] if lat else None,
        "wall_s": round(wall, 3),
        "completion_tokens_total": gen,
        "throughput_tokens_per_s": round(gen / wall, 2) if wall else None,
        "decode_tps_median": statistics.median(tps) if tps else None,
        "finish_reason_length": sum(1 for r in ok if r.get("finish_reason") == "length"),
        "load_window": window,
        "memory": mem.finish(window),
        **props(http),
        "per_request": results,
    }


def cmd_limits(a: argparse.Namespace, http: Http) -> dict[str, Any]:
    p = props(http)
    n_ctx = int(p["n_ctx_slot"])
    admitted = n_ctx - a.max_tokens
    fit = text_of_tokens(http, admitted, seed=1)
    over = text_of_tokens(http, n_ctx + 64, seed=2)
    n_fit, n_over = http.tokens(fit), http.tokens(over)
    return {
        "kind": "limits",
        **p,
        "max_tokens": a.max_tokens,
        "admitted_prompt_tokens": admitted,
        "fit": {
            "prompt_tokens": n_fit,
            "admitted": n_fit + a.max_tokens <= n_ctx,
            "result": http.chat(fit, a.max_tokens),
        },
        "over_by_one": {"prompt_tokens": admitted + 1, "admitted": False, "note": "rejected by the client pre-check"},
        "over_context": {"prompt_tokens": n_over, "server_result": http.chat(over, 16)},
    }


def cmd_ec2_cycle(a: argparse.Namespace, http: Http) -> dict[str, Any]:
    import boto3  # operator machine only

    ec2 = boto3.Session(profile_name=a.profile, region_name=a.region).client("ec2")

    def state() -> str:
        return ec2.describe_instances(InstanceIds=[a.instance_id])["Reservations"][0]["Instances"][0]["State"]["Name"]

    assert state() == "stopped", "the instance must be stopped before the cycle"
    t0 = time.monotonic()
    ec2.start_instances(InstanceIds=[a.instance_id])
    while state() != "running":
        time.sleep(1)
    running_s = round(time.monotonic() - t0, 3)
    marks = wait_ready(http, t0, a.timeout)
    t1 = time.monotonic()
    ec2.stop_instances(InstanceIds=[a.instance_id])
    while state() != "stopped":
        time.sleep(2)
    res = {
        "kind": "ec2-cycle",
        "start_to_running_s": running_s,
        **marks,
        "stop_to_stopped_s": round(time.monotonic() - t1, 3),
        "final_state": state(),
    }
    if "http_up_s" in marks:
        res["running_to_http_up_s"] = round(marks["http_up_s"] - running_s, 3)
    if "health_ok_s" in marks:
        res["weights_load_s"] = round(marks["health_ok_s"] - marks["http_up_s"], 3)
    return res


def expected_runs(slots: int, reference: bool) -> dict[str, int]:
    """The runs every profile must evidence (tag -> concurrency), fixed before measuring (report §4): `c1`; `cN` at
    the slot count when the profile has N > 1 slots; `overload` (two requests queued beyond the slots) for the
    reference profile."""
    runs = {"c1": 1}
    if slots > 1:
        runs[f"c{slots}"] = slots
    if reference:
        runs["overload"] = slots + 2
    return runs


def profile_verdict(
    profile: str, docs: list[dict[str, Any]], *, slots: int, reference: bool = False, model: str | None = None
) -> dict[str, Any]:
    """Criteria fixed before measuring (report §4). `pass` only when every criterion passed with evidence of every
    expected run; an expected run without evidence, or a criterion without evidence (for example memory not sampled
    on the host), makes the verdict `incomplete`; a measurement that ran and failed (`status: failed`) is `fail`."""
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for d in docs:
        by_kind.setdefault(d.get("kind", "?"), []).append(d)

    def failed(d: dict[str, Any]) -> bool:
        return d.get("status") == "failed"

    crit: dict[str, str] = {}
    loads = by_kind.get("load", [])
    crit["loaded"] = (
        "unavailable" if not loads else ("pass" if all(d.get("ready_s") and not failed(d) for d in loads) else "fail")
    )
    if model is not None:  # every load must have served exactly this alias
        served = all(d.get("model") == model and d.get("model_ok") is True and not failed(d) for d in loads)
        crit["model"] = "unavailable" if not loads else "pass" if served else "fail"
    seen_slots = {d.get("total_slots") for d in loads if not failed(d)}
    crit["slots"] = (
        "unavailable" if not seen_slots or None in seen_slots else "pass" if seen_slots == {slots} else "fail"
    )
    expected = expected_runs(slots, reference)
    runs = by_kind.get("run", [])
    by_tag: dict[str, list[dict[str, Any]]] = {}
    for d in runs:
        by_tag.setdefault(str(d.get("tag") or f"c{d.get('concurrency')}"), []).append(d)

    def run_ok(d: dict[str, Any], conc: int | None) -> bool:
        if failed(d) or (conc is not None and d.get("concurrency") != conc):
            return False
        return d.get("ok") == d.get("requests") == d.get("json_ok") and not d.get("finish_reason_length")

    run_status: dict[str, str] = {}
    for tag in sorted(set(expected) | set(by_tag)):
        got = by_tag.get(tag, [])
        if not got:
            run_status[tag] = "missing"
        else:
            run_status[tag] = "pass" if all(run_ok(d, expected.get(tag)) for d in got) else "fail"
    states = set(run_status.values())
    crit["run"] = "fail" if "fail" in states else "unavailable" if "missing" in states else "pass"
    missing_mem = [_unavailable("missing_run") for tag in expected if tag not in by_tag]
    mems = [d.get("memory") or _unavailable("absent") for d in loads + runs] + missing_mem
    mstates = {m.get("criterion", "unavailable") for m in mems}
    crit["memory"] = (
        "unavailable" if not mems else "fail" if "fail" in mstates else "pass" if mstates == {"pass"} else "unavailable"
    )
    lims = by_kind.get("limits", [])

    def limits_ok(d: dict[str, Any]) -> bool:
        fit = (d.get("fit") or {}).get("result") or {}
        over = (d.get("over_context") or {}).get("server_result") or {}
        rejected = over.get("error_type") == "exceed_context_size_error"
        return not failed(d) and fit.get("status") == 200 and bool(fit.get("json_ok")) and rejected

    crit["limits"] = "unavailable" if not lims else ("pass" if all(limits_ok(d) for d in lims) else "fail")
    values = set(crit.values())
    verdict = "fail" if "fail" in values else "pass" if values == {"pass"} else "incomplete"
    failures = [
        {k: d.get(k) for k in ("kind", "tag", "label", "exit", "error") if d.get(k) is not None}
        for d in docs
        if failed(d)
    ]
    return {
        "kind": "verdict",
        "profile": profile,
        "expected_slots": slots,
        "expected_model": model,
        "reference": reference,
        "expected_runs": expected,
        "runs": run_status,
        "failures": failures,
        "criteria": crit,
        "verdict": verdict,
    }


def read_evidence(paths: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Evidence files for `verdict`; a file that does not exist or is not a JSON object is listed as unreadable and
    counts as missing evidence (never as a pass)."""
    docs, unreadable = [], []
    for f in paths:
        try:
            d = json.loads(Path(f).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            unreadable.append(f)
            continue
        if isinstance(d, dict):
            docs.append(d)
        else:
            unreadable.append(f)
    return docs, unreadable


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("verdict")
    v.add_argument("--profile", required=True)
    v.add_argument("--out", required=True)
    v.add_argument("--slots", type=int, required=True, help="slots of the profile definition (expected runs)")
    v.add_argument("--reference", action="store_true", help="the reference profile (also requires `overload`)")
    v.add_argument("--model", help="served alias every load must have listed (manifest models[...].alias)")
    v.add_argument("evidence", nargs="+")
    for name in ("load", "run", "limits", "ec2-cycle"):
        p = sub.add_parser(name)
        p.add_argument("--base-url", required=True)
        p.add_argument("--key-file")
        p.add_argument("--out", required=True)
        p.add_argument("--label", default="")
        p.add_argument("--model", help="served alias to require and send (manifest models[...].alias)")
        if name in ("load", "ec2-cycle"):
            p.add_argument("--timeout", type=float, default=1800)
        if name in ("load", "run"):
            p.add_argument("--memory-start-cmd", help="starts mem-sample.sh on the measured host (e.g. via ssm-run.sh)")
            p.add_argument("--memory-stop-cmd", help="stops it; its stdout must be the sampler JSON summary")
            p.add_argument("--memory-local", action="store_true", help="run mem-sample.sh here (measuring this host)")
            p.add_argument("--expect-instance-id", help="the summary must come from this host")
        if name == "load":
            p.add_argument(
                "--restart-cmd",
                default="systemctl restart cce-inference.service",
                help="command that restarts the server (e.g. an `aws ssm send-command` from the operator machine)",
            )
        if name in ("run", "limits"):
            p.add_argument("--max-tokens", type=int, default=3072)
        if name == "run":
            p.add_argument("--tag", help="run identity for the verdict (default: c<concurrency>)")
            p.add_argument("--requests", type=int, default=10)
            p.add_argument("--concurrency", type=int, default=1)
            p.add_argument("--prompt-tokens", type=int, default=1800)
        if name == "ec2-cycle":
            p.add_argument("--instance-id", required=True)
            p.add_argument("--region", required=True)
            p.add_argument("--profile")
    a = ap.parse_args()
    if a.cmd == "verdict":
        docs, unreadable = read_evidence(a.evidence)
        verdict = profile_verdict(a.profile, docs, slots=a.slots, reference=a.reference, model=a.model)
        res = {**verdict, "unreadable": unreadable}
        if unreadable and res["verdict"] == "pass":
            res["verdict"] = "incomplete"  # evidence that cannot be read is never a pass
        Path(a.out).write_text(json.dumps(res, indent=2), encoding="utf-8")
        print(json.dumps(res))
        return {"pass": 0, "fail": 1}.get(res["verdict"], 3)
    http = Http(a.base_url, key_from(a.key_file), a.model)
    fn = {"load": cmd_load, "run": cmd_run, "limits": cmd_limits, "ec2-cycle": cmd_ec2_cycle}[a.cmd]
    measured_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    result = {"label": a.label, "measured_at": measured_at, "model": a.model, **fn(a, http)}
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)
    print(json.dumps({k: v for k, v in result.items() if k != "per_request"}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
