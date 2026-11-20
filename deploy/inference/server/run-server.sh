#!/usr/bin/env bash
# Start llama-server with the pinned model and a profile. Used by the systemd unit on AWS and on-premise.
#
#   run-server.sh                  # exec the server (systemd ExecStart)
#   run-server.sh --print-command  # print the exact command line (evidence; no side effects)
#
# Configuration: /etc/cce-inference/server.env (override with CCE_SERVER_ENV), which points to a profile in
# profiles/. On AWS the API key may be fetched from SSM Parameter Store at start (CCE_API_KEY_SSM_PARAM).
# --fit off and -ngl all: if the profile does not fit in VRAM the server fails explicitly instead of silently
# offloading to CPU or shrinking the context.
set -euo pipefail

ENV_FILE=${CCE_SERVER_ENV:-/etc/cce-inference/server.env}
# shellcheck disable=SC1090
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
: "${CCE_PROFILE:?CCE_PROFILE (profile env file) is required}"
# shellcheck disable=SC1090
. "$CCE_PROFILE"

: "${CCE_RUNTIME_DIR:=/opt/cce-inference/runtime}"
: "${CCE_MODEL_PATH:?CCE_MODEL_PATH is required}"
: "${CCE_MODEL_ALIAS:=qwen3.6-27b-q4km}"
: "${CCE_HOST:=0.0.0.0}"
: "${CCE_PORT:=8080}"
: "${CCE_API_KEY_FILE:=/etc/cce-inference/api-key}"
BIN=${CCE_LLAMA_SERVER_BIN:-$CCE_RUNTIME_DIR/current/llama-server}

args=(
  -m "$CCE_MODEL_PATH" --alias "$CCE_MODEL_ALIAS"
  --host "$CCE_HOST" --port "$CCE_PORT"
  -c "$CCE_CTX_TOTAL" --parallel "$CCE_PARALLEL" -ngl all --fit off
  --reasoning on --reasoning-format deepseek --reasoning-budget "$CCE_REASONING_BUDGET"
  --api-key-file "$CCE_API_KEY_FILE" --metrics --no-webui
)
if [ -n "${CCE_EXTRA_ARGS:-}" ]; then
  # shellcheck disable=SC2206
  args+=($CCE_EXTRA_ARGS)
fi

if [ "${1:-}" = "--print-command" ]; then
  printf '%q ' "$BIN" "${args[@]}"; echo
  exit 0
fi

if [ -n "${CCE_API_KEY_SSM_PARAM:-}" ]; then
  umask 077
  aws ssm get-parameter --with-decryption --name "$CCE_API_KEY_SSM_PARAM" \
    --region "${CCE_AWS_REGION:?}" --query Parameter.Value --output text > "$CCE_API_KEY_FILE.tmp"
  mv -f "$CCE_API_KEY_FILE.tmp" "$CCE_API_KEY_FILE"
fi
[ -s "$CCE_API_KEY_FILE" ] || { echo "API key file missing or empty" >&2; exit 1; }
[ -f "$CCE_MODEL_PATH" ] || { echo "model file missing" >&2; exit 1; }

export LD_LIBRARY_PATH="$CCE_RUNTIME_DIR/current:$CCE_RUNTIME_DIR/cudart${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
echo "cce-inference: profile=${CCE_PROFILE_NAME:-?} ctx_total=$CCE_CTX_TOTAL parallel=$CCE_PARALLEL" >&2
exec "$BIN" "${args[@]}"
