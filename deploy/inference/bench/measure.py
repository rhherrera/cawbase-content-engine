#!/usr/bin/env python3
"""A09-1 measurements on real hardware (standard library only; boto3 only for `ec2-cycle`). Writes JSON evidence.
Synthetic, deterministic inputs only: no benchmark cases, no real data.

    # on the GPU host (sudo for load): cold restart of the server, time to HTTP, to weights loaded, to canary
    sudo python3 measure.py load --base-url http://127.0.0.1:8080 --key-file /etc/cce-inference/api-key --out load.json
    # latency/throughput at a concurrency, with VRAM/RAM sampling (prompt size in tokens, approx.)
    python3 measure.py run --base-url URL --key-file F --requests 20 --concurrency 1 --prompt-tokens 1800 --out r.json
    # context admission: largest admitted prompt succeeds, one token more is rejected before sending, server 400
    python3 measure.py limits --base-url ... --key-file ... --max-tokens 3072 --out limits.json
    # from the operator machine: EC2 start -> running -> HTTP up -> weights loaded -> canary, then stop -> stopped
    python3 measure.py ec2-cycle --instance-id i-... --region us-east-1 --profile AWS_PROFILE \
        --base-url http://127.0.0.1:18080 --key-file ./api-key --out cycle.json
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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


class Http:
    def __init__(self, base_url: str, key: str | None) -> None:
        self.base = base_url.rstrip("/")
        self.key = key

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
        body = {
            "temperature": 0,
            "max_tokens": max_tokens,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "salida", "strict": True, "schema": schema},
            },
        }
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


class Sampler(threading.Thread):
    """Samples GPU memory (nvidia-smi) and host memory every `period` seconds; keeps the peaks."""

    def __init__(self, period: float = 0.5) -> None:
        super().__init__(daemon=True)
        self.period, self.stop_evt = period, threading.Event()
        self.vram_peak_mib = 0
        self.vram_samples: list[int] = []
        self.ram_used_peak_kib = 0

    def run(self) -> None:
        while not self.stop_evt.is_set():
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                ).stdout.split()
                if out:
                    v = int(out[0])
                    self.vram_samples.append(v)
                    self.vram_peak_mib = max(self.vram_peak_mib, v)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                pass
            try:
                mem = dict(line.split(":", 1) for line in open("/proc/meminfo", encoding="ascii"))
                used = int(mem["MemTotal"].split()[0]) - int(mem["MemAvailable"].split()[0])
                self.ram_used_peak_kib = max(self.ram_used_peak_kib, used)
            except (OSError, KeyError, ValueError):
                pass
            self.stop_evt.wait(self.period)

    def result(self) -> dict[str, Any]:
        self.stop_evt.set()
        return {
            "vram_peak_mib": self.vram_peak_mib,
            "vram_last_mib": self.vram_samples[-1] if self.vram_samples else None,
            "ram_used_peak_kib": self.ram_used_peak_kib,
        }


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
    if "health_ok_s" in marks:
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
    s = Sampler()
    s.start()
    t0 = time.monotonic()
    subprocess.run(["systemctl", "restart", a.unit], check=True)
    marks = wait_ready(http, t0, a.timeout)
    res = {"kind": "load", "unit": a.unit, **marks, **s.result(), **props(http)}
    res["weights_load_s"] = round(marks["health_ok_s"] - marks["http_up_s"], 3) if "health_ok_s" in marks else None
    return res


def cmd_run(a: argparse.Namespace, http: Http) -> dict[str, Any]:
    msgs = [text_of_tokens(http, a.prompt_tokens, seed=i) for i in range(a.requests)]
    s = Sampler()
    s.start()
    t0 = time.monotonic()
    with ThreadPoolExecutor(a.concurrency) as pool:
        results = list(pool.map(lambda m: http.chat(m, a.max_tokens), msgs))
    wall = time.monotonic() - t0
    ok = [r for r in results if r["status"] == 200]
    lat = [r["latency_s"] for r in ok]
    gen = sum((r.get("usage") or {}).get("completion_tokens", 0) for r in ok)
    tps = [r["timings"].get("predicted_per_second") for r in ok if r.get("timings")]
    return {
        "kind": "run",
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
        **s.result(),
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("load", "run", "limits", "ec2-cycle"):
        p = sub.add_parser(name)
        p.add_argument("--base-url", required=True)
        p.add_argument("--key-file")
        p.add_argument("--out", required=True)
        p.add_argument("--label", default="")
        if name in ("load", "ec2-cycle"):
            p.add_argument("--timeout", type=float, default=1800)
        if name == "load":
            p.add_argument("--unit", default="cce-inference.service")
        if name in ("run", "limits"):
            p.add_argument("--max-tokens", type=int, default=3072)
        if name == "run":
            p.add_argument("--requests", type=int, default=10)
            p.add_argument("--concurrency", type=int, default=1)
            p.add_argument("--prompt-tokens", type=int, default=1800)
        if name == "ec2-cycle":
            p.add_argument("--instance-id", required=True)
            p.add_argument("--region", required=True)
            p.add_argument("--profile")
    a = ap.parse_args()
    http = Http(a.base_url, key_from(a.key_file))
    fn = {"load": cmd_load, "run": cmd_run, "limits": cmd_limits, "ec2-cycle": cmd_ec2_cycle}[a.cmd]
    result = {"label": a.label, "measured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fn(a, http)}
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2, ensure_ascii=False)
    print(json.dumps({k: v for k, v in result.items() if k != "per_request"}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
