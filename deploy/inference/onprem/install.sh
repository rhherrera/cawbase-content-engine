#!/usr/bin/env bash
# On-premise installation of the inference server: same runtime, model, profile, endpoint, API key and readiness
# contract as AWS, with local weights and no AWS service or credential.
#
#   sudo deploy/inference/onprem/install.sh --weights-from /mnt/media/Qwen3.6-27B-Q4_K_M.gguf \
#        [--weights-dir /srv/cce-inference/models] [--profile reference-8k-1slot] [--bind 10.0.0.5] [--port 8080] \
#        [--api-key-file /path/to/key]   # default: generate one (shown once, stored 0600)
#        [--model qwen3.6-27b-q4km]      # default: the manifest's on-premise target (Q4_K_M); no AWS dependency
#   (omit --weights-from to download the pinned file from Hugging Face instead)
#
# Requirements: Linux x64 (Ubuntu 22.04/24.04 tested by recipe), NVIDIA GPU with >= 24 GB for the reference
# profile, an NVIDIA driver able to run CUDA 13.5 (or --variant cuda-12.8), libgomp1, systemd, curl, sha256sum,
# python3 (reads manifest.json).
# The controller on-premise uses the `command` backend (systemctl start/stop/is-active) — see controller.onprem.toml.
set -euo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
WEIGHTS_FROM=""; WEIGHTS_DIR=/srv/cce-inference/models; PROFILE=reference-8k-1slot
BIND=127.0.0.1; PORT=8080; KEY_FROM=""; VARIANT=cuda-13.5; MODEL=""
while [ $# -gt 0 ]; do
  case "$1" in
    --weights-from) WEIGHTS_FROM=$2; shift 2 ;;
    --weights-dir) WEIGHTS_DIR=$2; shift 2 ;;
    --profile) PROFILE=$2; shift 2 ;;
    --bind) BIND=$2; shift 2 ;;
    --port) PORT=$2; shift 2 ;;
    --api-key-file) KEY_FROM=$2; shift 2 ;;
    --variant) VARIANT=$2; shift 2 ;;
    --model) MODEL=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 1; }
[ -f "$HERE/server/profiles/$PROFILE.env" ] || { echo "unknown profile $PROFILE" >&2; exit 2; }
command -v nvidia-smi >/dev/null || { echo "NVIDIA driver (nvidia-smi) not found" >&2; exit 1; }
[ -n "$MODEL" ] || MODEL=$("$HERE/server/install-model.sh" --target onprem)
MODEL_FILE=$("$HERE/server/install-model.sh" --model "$MODEL" --field file)
MODEL_ALIAS=$("$HERE/server/install-model.sh" --model "$MODEL" --field alias)

SRC=/opt/cce-inference/src
mkdir -p "$SRC" /etc/cce-inference /srv/cce-inference "$WEIGHTS_DIR"
cp -R "$HERE/manifest.json" "$HERE/server" "$HERE/bench" "$SRC/"
id cce-inference >/dev/null 2>&1 || useradd --system --home /var/lib/cce-inference --shell /usr/sbin/nologin cce-inference

ldconfig -p | grep -q libgomp.so.1 || { echo "install the libgomp1 package (OpenMP runtime) first" >&2; exit 1; }
"$SRC/server/install-runtime.sh" --prefix /opt/cce-inference --variant "$VARIANT"
if [ -n "$WEIGHTS_FROM" ]; then
  "$SRC/server/install-model.sh" --model "$MODEL" --dir "$WEIGHTS_DIR" --from "$WEIGHTS_FROM"
else
  "$SRC/server/install-model.sh" --model "$MODEL" --dir "$WEIGHTS_DIR"
fi

umask 077
if [ -n "$KEY_FROM" ]; then
  install -m 0600 "$KEY_FROM" /etc/cce-inference/api-key
elif [ ! -s /etc/cce-inference/api-key ]; then
  head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > /etc/cce-inference/api-key
  echo "generated /etc/cce-inference/api-key (copy it to the controller/workers host; it is not printed)"
fi
cat > /etc/cce-inference/server.env <<EOF
CCE_RUNTIME_DIR=/opt/cce-inference/runtime
CCE_MODEL_ID=$MODEL
CCE_MODEL_PATH=$WEIGHTS_DIR/$MODEL_FILE
CCE_MODEL_ALIAS=$MODEL_ALIAS
CCE_PROFILE=$SRC/server/profiles/$PROFILE.env
CCE_HOST=$BIND
CCE_PORT=$PORT
CCE_API_KEY_FILE=/etc/cce-inference/api-key
EOF
chown -R cce-inference:cce-inference /etc/cce-inference "$WEIGHTS_DIR"
chmod 0750 /etc/cce-inference
"$SRC/server/run-server.sh" --check  # the unit runs the same check before every start
sed "s#/srv/cce-inference/models#$WEIGHTS_DIR#; s#RequiresMountsFor=/srv/cce-inference#RequiresMountsFor=$WEIGHTS_DIR#" \
  "$SRC/server/cce-inference.service" > /etc/systemd/system/cce-inference.service
systemctl daemon-reload
systemctl enable --now cce-inference.service
"$SRC/bench/host-facts.sh" > /var/lib/cce-inference/host-facts.json 2>/dev/null || true
echo "installed $MODEL ($MODEL_FILE, alias $MODEL_ALIAS); endpoint http://$BIND:$PORT (readiness: GET /health then the controller's structured canary)"
echo "no idle guard on-premise: the controller's command backend stops the service after the idle threshold"
