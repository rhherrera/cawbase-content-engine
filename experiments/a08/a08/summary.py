"""Counts for the report: floor (protocol §11) per environment, official vs informative, Z1-only vs Z2, failures by stage.

    .venv/bin/python -m a08.summary <env> [<env> ...]
"""

from __future__ import annotations

import json
import sys

from .manifest import ROOT

FAMILIES = "WGLCM"


def summarize(env: str) -> dict:
    d = json.loads((ROOT / "outputs" / env / "results.json").read_text())
    res = d["results"]
    by_case = {}
    for r in res:
        cid = r["case_id"]
        st = r.get("stages", {})
        last = list(st.items())[-1] if st else ("export", {"result": "missing"})
        z2 = any(ln["kind"] == "Z2" for ln in r.get("plan", []))
        by_case[cid] = {"family": cid[0], "success": bool(r.get("success")), "success_rt": bool(r.get("success_rt")),
                        "type0": bool(r.get("type0_cid_subset")), "z2": z2,
                        "factors": [ln["f"] for ln in r.get("plan", []) if ln["kind"] == "Z2"],
                        "attempts": sorted({s["attempt"] for ln in r.get("plan", []) for s in ln["sels"]}),
                        "stage": last[0], "reason": last[1].get("reason") or last[1].get("check"),
                        "baseline": (r.get("baseline") or {}).get("reason") or (r.get("baseline") or {}).get("open_gate"),
                        "source_map_deterministic": r.get("source_map_deterministic"), "source_map_sha256": r.get("source_map_sha256")}

    def floor(key: str) -> dict:
        tot = sum(1 for c in by_case.values() if c[key])
        fam = {f: sum(1 for c in by_case.values() if c["family"] == f and c[key]) for f in FAMILIES}
        t0 = any(c[key] and c["type0"] for c in by_case.values())
        return {"total": f"{tot}/20", "per_family": {f: f"{n}/4" for f, n in fam.items()}, "type0_native": t0,
                "floor_met": tot >= 16 and all(n >= 3 for n in fam.values()) and t0,
                "z1_only": sorted(c for c, v in by_case.items() if v[key] and not v["z2"]),
                "with_z2": sorted(c for c, v in by_case.items() if v[key] and v["z2"])}
    return {"env": env, "rules": d["env"].get("rules"), "cases": by_case, "strict": floor("success"), "with_rt": floor("success_rt")}


def main(envs: list[str]) -> None:
    for env in envs:
        s = summarize(env)
        (ROOT / "outputs" / env / "summary.json").write_text(json.dumps(s, indent=1, ensure_ascii=False) + "\n")
        print(env, "strict", s["strict"]["total"], s["strict"]["per_family"], "type0", s["strict"]["type0_native"], "| RT", s["with_rt"]["total"], s["with_rt"]["per_family"])
        for c, v in s["cases"].items():
            print("  ", c, "OK" if v["success"] else ("RT" if v["success_rt"] else "--"), v["stage"], v["reason"], "z2" if v["z2"] else "", v["factors"], "base:", v["baseline"])


if __name__ == "__main__":
    main(sys.argv[1:])
