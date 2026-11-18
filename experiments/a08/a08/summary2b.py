"""Stage 2B floor (docs/a08-stage2b-protocol.md §5) per environment: >= 8/10 complete cycles, >= 1 per family, >= 1
native subset Type0/CID whose selected text completes the cycle, zero improperly accepted negatives, zero blockers.
RT is a diagnostic and never counts.

    .venv/bin/python -m a08.summary2b <env> [<env> ...]
"""

from __future__ import annotations

import json
import sys

from .manifest import ROOT

FAMILIES = "WGLCM"


def summarize(env: str) -> dict:
    d = json.loads((ROOT / "outputs" / env / "results.json").read_text())
    ctl_path = ROOT / "outputs" / env / "controls.json"
    ctl = json.loads(ctl_path.read_text()) if ctl_path.exists() else None
    cv_path = ROOT / "outputs" / env / "cv.json"
    cv = json.loads(cv_path.read_text()) if cv_path.exists() else None
    cases = {}
    for r in d["results"]:
        st = r.get("stages", {})
        failed = [kv for kv in st.items() if kv[1].get("result") in ("failed", "missing")]
        first = failed[0] if failed else (list(st.items())[-1] if st else ("export", {"result": "missing"}))
        plan = r.get("plan", [])
        cases[r["case_id"]] = {
            "family": r["family"], "template": r["template"], "success": bool(r.get("success")),
            "missing": first[0] == "export", "rt_diagnostic_only": bool(r.get("rt_diagnostic_only")),
            "blocker": r.get("blocker"), "stage": None if r.get("success") else first[0],
            "reason": None if r.get("success") else (first[1].get("reason") or first[1].get("check")),
            "detail": None if r.get("success") else str(first[1].get("detail", ""))[:160],
            "zones": [{"kind": ln["kind"], "f": ln["f"], "sels": [s["sid"] for s in ln["sels"]], "attempts": sorted({s["attempt"] for s in ln["sels"]})} for ln in plan],
            "type0_selected_cycle": bool(r.get("type0_selected_cycle")),
            "type0_cid_subset_in_pdf": r.get("type0_cid_subset_in_pdf"),
            "source_map_deterministic": r.get("source_map_deterministic"), "source_map_sha256": r.get("source_map_sha256"),
            "baseline": (r.get("baseline") or {}).get("reason") or (r.get("baseline") or {}).get("open_gate"),
            "seconds": r.get("seconds"),
        }
    ok = [c for c, v in cases.items() if v["success"]]
    fam = {f: sum(1 for c in ok if cases[c]["family"] == f) for f in FAMILIES}
    blockers = sorted(c for c, v in cases.items() if v["blocker"])
    accepted_neg = (ctl or {}).get("summary", {}).get("accepted_indebidamente", []) if ctl else None
    cv_missed = sorted(k for k, v in (cv or {}).items() if not v.get("detected")) if cv else None
    floor = {
        "total": f"{len(ok)}/10", "per_family": {f: n for f, n in fam.items()},
        "type0_selected_cycle": any(cases[c]["type0_selected_cycle"] for c in ok),
        "blockers": blockers, "negatives_accepted": accepted_neg, "cv_not_detected": cv_missed,
        "missing": sorted(c for c, v in cases.items() if v["missing"]),
        "rt_diagnostic_only_not_counted": sorted(c for c, v in cases.items() if v["rt_diagnostic_only"]),
    }
    floor["floor_met"] = (len(ok) >= 8 and all(n >= 1 for n in fam.values()) and floor["type0_selected_cycle"] and not blockers
                          and accepted_neg == [] and cv_missed == [])
    return {"env": env, "env_info": d["env"], "cases": cases, "floor": floor}


def main(envs: list[str]) -> None:
    for env in envs:
        s = summarize(env)
        (ROOT / "outputs" / env / "summary.json").write_text(json.dumps(s, indent=1, ensure_ascii=False) + "\n")
        f = s["floor"]
        print(env, f["total"], f["per_family"], "type0:", f["type0_selected_cycle"], "blockers:", f["blockers"], "neg accepted:", f["negatives_accepted"],
              "cv missed:", f["cv_not_detected"], "missing:", f["missing"], "RT-only:", f["rt_diagnostic_only_not_counted"], "FLOOR MET" if f["floor_met"] else "floor not met")
        for c, v in s["cases"].items():
            print("  ", c, "OK" if v["success"] else ("--" if not v["missing"] else "missing"), v["stage"] or "", v["reason"] or "",
                  [(z["kind"], z["f"]) for z in v["zones"]], "RT-diag" if v["rt_diagnostic_only"] else "", "BLOCKER" if v["blocker"] else "")


if __name__ == "__main__":
    main(sys.argv[1:])
