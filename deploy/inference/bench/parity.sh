#!/usr/bin/env bash
# Quality run with the methodology of the external reference run (razona512+esquema). Uses the UNMODIFIED
# benchmark.py and proxy_esquema.py from the results ZIP, which stays outside Git, exactly as the reference: same
# corpus and system prompt (hashes checked), proxy adds response_format (json_schema strict, esquema_salida.json) to
# each request; the benchmark sends temperature 0, max_tokens 3072, timeout 180 s, 3 repetitions.
#
#   bench/parity.sh --pkg /path/to/cawbase_resultados --base-url http://127.0.0.1:18080 --key-file ./api-key \
#                   --model qwen3.6-27b-q8_0 --artifact model-record.json --out /path/outside/repo/parity-X
#
# --model is the served alias (manifest models[...].alias); /v1/models must list it before anything runs.
# --artifact is the verification record of the served file (host-facts.json "model"), copied next to the results.
# The summary reports THIS artifact's result apart from the historical 43/50, which belongs to Q4_K_M
# (Qwen3.6-27B-Q4_K_M.gguf): another artifact, never pooled with it. The server must run reference-8k-1slot.
# Output (raw outputs, metrics) goes to --out, never to Git.
set -euo pipefail

PKG=""; BASE=""; KEY_FILE=""; OUT=""; REPS=3; PORT=18767; MODEL=""; ARTIFACT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --pkg) PKG=$2; shift 2 ;;
    --base-url) BASE=$2; shift 2 ;;
    --key-file) KEY_FILE=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    --reps) REPS=$2; shift 2 ;;
    --model) MODEL=$2; shift 2 ;;
    --artifact) ARTIFACT=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$PKG" ] && [ -n "$BASE" ] && [ -n "$KEY_FILE" ] && [ -n "$OUT" ] && [ -n "$MODEL" ] \
  || { echo "missing arguments (--pkg --base-url --key-file --model --out)" >&2; exit 2; }
BENCH="$PKG/CAWbase_benchmark_v1.1"; NIGHT="$PKG/corrida_nocturna"
sha() { shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1 || sha256sum "$1" | cut -d' ' -f1; }
[ "$(sha "$BENCH/prompt_sistema.txt")" = e0720bce7966d5cc7ec63817ad6dcee540e8104fa1954d38843741bbff1f866d ] \
  || { echo "prompt_sistema.txt does not match the reference hash" >&2; exit 1; }
[ "$(sha "$NIGHT/entradas/evaluacion.jsonl")" = c0d20428bb552adf18e8b91053e25db91362360a6fbc75e35efb2ad89431c8a7 ] \
  || { echo "evaluacion.jsonl does not match the reference hash" >&2; exit 1; }
mkdir -p "$OUT"
case "$OUT" in "$(git rev-parse --show-toplevel 2>/dev/null || echo /nonexistent)"*) echo "--out must be outside the repository" >&2; exit 2 ;; esac
LOCAL_LLM_API_KEY=$(head -n1 "$KEY_FILE")
export LOCAL_LLM_API_KEY
curl -fsS -m 10 -H "Authorization: Bearer $LOCAL_LLM_API_KEY" "${BASE%/}/v1/models" > "$OUT/models.json" \
  || { echo "cannot list the served models at $BASE" >&2; exit 1; }
python3 -I -c 'import json, sys
ids = [m.get("id") for m in json.load(open(sys.argv[1])).get("data", [])]
sys.exit(0 if sys.argv[2] in ids else f"served alias mismatch: {sys.argv[2]!r} not in {ids}")' "$OUT/models.json" "$MODEL"
if [ -n "$ARTIFACT" ]; then cp "$ARTIFACT" "$OUT/artifact.json"; else echo null > "$OUT/artifact.json"; fi

python3 -I "$NIGHT/proxy_esquema.py" --puerto "$PORT" --destino "$BASE" --esquema "$NIGHT/esquema_salida.json" \
  > "$OUT/proxy.log" 2>&1 &
PROXY=$!
trap 'kill $PROXY 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do grep -q "proxy listo" "$OUT/proxy.log" && break; sleep 0.2; done

cd "$BENCH"  # benchmark.py reads prompt_sistema.txt and privado/ relative to itself
for r in $(seq 1 "$REPS"); do
  python3 -I "$BENCH/benchmark.py" ejecutar --inputs "$NIGHT/entradas/evaluacion.jsonl" \
    --base-url "http://127.0.0.1:$PORT/v1" --model "$MODEL" --output "$OUT/evaluacion-r$r-i1.jsonl" \
    --max-tokens 3072 --timeout 180
  python3 -I "$BENCH/benchmark.py" evaluar --predictions "$OUT/evaluacion-r$r-i1.jsonl" --split evaluacion \
    --output "$OUT/metricas-evaluacion-r$r.json"
done
python3 - "$OUT" "$REPS" "$MODEL" "$(sha "$BENCH/benchmark.py")" "$(sha "$NIGHT/proxy_esquema.py")" \
  "$(sha "$NIGHT/esquema_salida.json")" <<'EOF' | tee "$OUT/summary.json"
import json, sys
out, reps, model, sha_bench, sha_proxy, sha_schema = sys.argv[1], int(sys.argv[2]), *sys.argv[3:]
rows = [json.load(open(f"{out}/metricas-evaluacion-r{r}.json")) for r in range(1, reps + 1)]
correct = [round(m["casos_completamente_correctos"] * m["casos"]) for m in rows]
q4 = "qwen3.6-27b-q4km: Qwen3.6-27B-Q4_K_M.gguf sha256 f3ab00cc923590947877bad882e09ada67789f747f5d3cb3c178fdf91b9aaab9"
print(json.dumps({
    "model": model,
    "artifact": json.load(open(f"{out}/artifact.json")),
    "strict_correct_per_rep": correct,
    "cases": rows[0]["casos"],
    "methodology": {
        "corpus": "entradas/evaluacion.jsonl sha256 c0d20428bb552adf18e8b91053e25db91362360a6fbc75e35efb2ad89431c8a7",
        "prompt": "prompt_sistema.txt sha256 e0720bce7966d5cc7ec63817ad6dcee540e8104fa1954d38843741bbff1f866d",
        "benchmark_py_sha256": sha_bench, "proxy_esquema_py_sha256": sha_proxy, "esquema_salida_sha256": sha_schema,
        "temperature": 0, "max_tokens": 3072, "timeout_s": 180, "repetitions": reps, "profile": "reference-8k-1slot",
    },
    "historical_reference": {
        "artifact": q4,
        "strict_correct_per_rep": [43, 43, 43],
        "note": "external RTX 5090 run of another artifact; reported apart, never pooled with this result",
    },
}, indent=2))
EOF
