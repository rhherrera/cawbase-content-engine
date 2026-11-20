#!/usr/bin/env bash
# Parity check against the external reference run (razona512+esquema, 43/50 strict on RTX 5090).
# Uses the UNMODIFIED benchmark.py and proxy_esquema.py from the results ZIP, which stays outside Git, exactly as
# the reference: proxy adds response_format (json_schema strict, esquema_salida.json) to each request; the
# benchmark sends the fixed system prompt, temperature 0, max_tokens 3072, timeout 180 s, 3 repetitions.
#
#   bench/parity.sh --pkg /path/to/cawbase_resultados --base-url http://127.0.0.1:18080 \
#                   --key-file ./api-key --out /path/outside/repo/parity-YYYYMMDD
#
# The server must run the reference-8k-1slot profile. Output (raw outputs, metrics) goes to --out, never to Git.
set -euo pipefail

PKG=""; BASE=""; KEY_FILE=""; OUT=""; REPS=3; PORT=18767
while [ $# -gt 0 ]; do
  case "$1" in
    --pkg) PKG=$2; shift 2 ;;
    --base-url) BASE=$2; shift 2 ;;
    --key-file) KEY_FILE=$2; shift 2 ;;
    --out) OUT=$2; shift 2 ;;
    --reps) REPS=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$PKG" ] && [ -n "$BASE" ] && [ -n "$KEY_FILE" ] && [ -n "$OUT" ] || { echo "missing arguments" >&2; exit 2; }
BENCH="$PKG/CAWbase_benchmark_v1.1"; NIGHT="$PKG/corrida_nocturna"
sha() { shasum -a 256 "$1" 2>/dev/null | cut -d' ' -f1 || sha256sum "$1" | cut -d' ' -f1; }
[ "$(sha "$BENCH/prompt_sistema.txt")" = e0720bce7966d5cc7ec63817ad6dcee540e8104fa1954d38843741bbff1f866d ] \
  || { echo "prompt_sistema.txt does not match the reference hash" >&2; exit 1; }
[ "$(sha "$NIGHT/entradas/evaluacion.jsonl")" = c0d20428bb552adf18e8b91053e25db91362360a6fbc75e35efb2ad89431c8a7 ] \
  || { echo "evaluacion.jsonl does not match the reference hash" >&2; exit 1; }
mkdir -p "$OUT"
case "$OUT" in "$(git rev-parse --show-toplevel 2>/dev/null || echo /nonexistent)"*) echo "--out must be outside the repository" >&2; exit 2 ;; esac

python3 -I "$NIGHT/proxy_esquema.py" --puerto "$PORT" --destino "$BASE" --esquema "$NIGHT/esquema_salida.json" \
  > "$OUT/proxy.log" 2>&1 &
PROXY=$!
trap 'kill $PROXY 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do grep -q "proxy listo" "$OUT/proxy.log" && break; sleep 0.2; done

LOCAL_LLM_API_KEY=$(head -n1 "$KEY_FILE")
export LOCAL_LLM_API_KEY
cd "$BENCH"  # benchmark.py reads prompt_sistema.txt and privado/ relative to itself
for r in $(seq 1 "$REPS"); do
  python3 -I "$BENCH/benchmark.py" ejecutar --inputs "$NIGHT/entradas/evaluacion.jsonl" \
    --base-url "http://127.0.0.1:$PORT/v1" --model qwen3.6-27b-q4km --output "$OUT/evaluacion-r$r-i1.jsonl" \
    --max-tokens 3072 --timeout 180
  python3 -I "$BENCH/benchmark.py" evaluar --predictions "$OUT/evaluacion-r$r-i1.jsonl" --split evaluacion \
    --output "$OUT/metricas-evaluacion-r$r.json"
done
python3 - "$OUT" "$REPS" <<'EOF'
import json, sys
out, reps = sys.argv[1], int(sys.argv[2])
rows = [json.load(open(f"{out}/metricas-evaluacion-r{r}.json")) for r in range(1, reps + 1)]
correct = [round(m["casos_completamente_correctos"] * m["casos"]) for m in rows]
print(json.dumps({"strict_correct_per_rep": correct, "reference": [43, 43, 43]}, indent=2))
EOF
