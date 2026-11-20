#!/usr/bin/env bash
# First-boot provisioning of the GPU host (run once by cloud-init user data, as root). Idempotent: safe to re-run.
#
#   bootstrap.sh --volume-id vol-... --region us-east-1 --api-key-param /cce/inference/api-key [--profile reference-8k-1slot]
#
# 1. format (first time only) and mount the persistent weights EBS volume at /srv/cce-inference;
# 2. install the pinned llama.cpp runtime (SHA-256 verified);
# 3. install the pinned GGUF on the weights volume (SHA-256 verified; skipped when already verified);
# 4. install server.env, the systemd unit (starts the server at every boot) and the idle guard timer.
# Every phase writes a timestamp to /var/lib/cce-inference/bootstrap.log (evidence for boot timing).
set -euo pipefail

SRC=/opt/cce-inference/src
VOLUME_ID=""; REGION=""; KEY_PARAM=""; PROFILE=reference-8k-1slot; GUARD_MIN=120
while [ $# -gt 0 ]; do
  case "$1" in
    --volume-id) VOLUME_ID=$2; shift 2 ;;
    --region) REGION=$2; shift 2 ;;
    --api-key-param) KEY_PARAM=$2; shift 2 ;;
    --profile) PROFILE=$2; shift 2 ;;
    --guard-idle-min) GUARD_MIN=$2; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[ -n "$VOLUME_ID" ] && [ -n "$REGION" ] && [ -n "$KEY_PARAM" ] || { echo "missing arguments" >&2; exit 2; }
[ -f "$SRC/server/profiles/$PROFILE.env" ] || { echo "unknown profile $PROFILE" >&2; exit 2; }

mkdir -p /var/lib/cce-inference
LOG=/var/lib/cce-inference/bootstrap.log
mark() { echo "$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ) $*" | tee -a "$LOG"; }
mark "bootstrap start"

# 1. weights volume (NVMe name derived from the EBS volume id)
DEV=/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_${VOLUME_ID/-/}
for _ in $(seq 1 150); do [ -e "$DEV" ] && break; sleep 2; done
[ -e "$DEV" ] || { mark "weights volume $VOLUME_ID not attached"; exit 1; }
if ! blkid "$DEV" >/dev/null 2>&1; then
  mkfs.ext4 -q -L cce-weights "$DEV"
  mark "weights volume formatted"
fi
UUID=$(blkid -s UUID -o value "$DEV")
mkdir -p /srv/cce-inference
grep -q "$UUID" /etc/fstab || echo "UUID=$UUID /srv/cce-inference ext4 defaults,nofail,noatime 0 2" >> /etc/fstab
mountpoint -q /srv/cce-inference || mount /srv/cce-inference
mark "weights volume mounted"

id cce-inference >/dev/null 2>&1 || useradd --system --home /var/lib/cce-inference --shell /usr/sbin/nologin cce-inference
mkdir -p /srv/cce-inference/models /etc/cce-inference
chown -R cce-inference:cce-inference /srv/cce-inference /etc/cce-inference
chmod 0750 /etc/cce-inference

# 2. runtime (the release needs the OpenMP runtime; the DLAMI driver provides libcuda)
if ! ldconfig -p | grep -q libgomp.so.1; then
  DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq libgomp1
fi
"$SRC/server/install-runtime.sh" --prefix /opt/cce-inference
mark "runtime installed"

# 3. model (download once to the persistent volume; later boots only re-verify quickly)
if ! "$SRC/server/install-model.sh" --dir /srv/cce-inference/models --verify --quick >/dev/null 2>&1; then
  sudo -u cce-inference "$SRC/server/install-model.sh" --dir /srv/cce-inference/models
fi
mark "model installed and verified"

# 4. configuration and units
cat > /etc/cce-inference/server.env <<EOF
CCE_RUNTIME_DIR=/opt/cce-inference/runtime
CCE_MODEL_PATH=/srv/cce-inference/models/Qwen3.6-27B-Q4_K_M.gguf
CCE_MODEL_ALIAS=qwen3.6-27b-q4km
CCE_PROFILE=$SRC/server/profiles/$PROFILE.env
CCE_HOST=0.0.0.0
CCE_PORT=8080
CCE_API_KEY_FILE=/etc/cce-inference/api-key
CCE_API_KEY_SSM_PARAM=$KEY_PARAM
CCE_AWS_REGION=$REGION
EOF
echo "CCE_GUARD_IDLE_MIN=$GUARD_MIN" > /etc/cce-inference/idle-guard.env
chown cce-inference:cce-inference /etc/cce-inference/server.env
install -m 0644 "$SRC/server/cce-inference.service" /etc/systemd/system/cce-inference.service
install -m 0644 "$SRC/server/cce-inference-idle-guard.service" /etc/systemd/system/cce-inference-idle-guard.service
install -m 0644 "$SRC/server/cce-inference-idle-guard.timer" /etc/systemd/system/cce-inference-idle-guard.timer
systemctl daemon-reload
systemctl enable cce-inference.service cce-inference-idle-guard.timer
systemctl restart cce-inference.service
systemctl start cce-inference-idle-guard.timer
"$SRC/bench/host-facts.sh" > /var/lib/cce-inference/host-facts.json 2>/dev/null || true
mark "units installed; server started"
