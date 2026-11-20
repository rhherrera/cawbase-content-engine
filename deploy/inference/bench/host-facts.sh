#!/usr/bin/env bash
# Print the facts of the GPU host as JSON (evidence): GPU, driver, CUDA, OS, kernel, instance identity (on EC2),
# installed runtime and model verification record. Read-only.
set -uo pipefail
IMDS=${CCE_IMDS_BASE:-http://169.254.169.254}  # override for tests only

q() { nvidia-smi --query-gpu="$1" --format=csv,noheader,nounits 2>/dev/null | head -n1 | sed 's/^ *//;s/ *$//'; }
imds() {
  local tok
  tok=$(curl -fsS -m 2 -X PUT "$IMDS/latest/api/token" -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null) || return 0
  curl -fsS -m 2 -H "X-aws-ec2-metadata-token: $tok" "$IMDS/latest/meta-data/$1" 2>/dev/null
}
cuda=$(nvidia-smi 2>/dev/null | sed -n 's/.*CUDA Version: *\([0-9.]*\).*/\1/p' | head -n1)
runtime=$(cat /opt/cce-inference/runtime/runtime.json 2>/dev/null || echo null)
model=$(cat /srv/cce-inference/models/*.verified.json 2>/dev/null | head -n1)
. /etc/os-release 2>/dev/null || true
cat <<EOF
{
  "collected_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "gpu_name": "$(q name)",
  "gpu_memory_total_mib": "$(q memory.total)",
  "driver_version": "$(q driver_version)",
  "cuda_driver_api": "${cuda}",
  "os": "${PRETTY_NAME:-unknown}",
  "kernel": "$(uname -r)",
  "cpu_count": "$(nproc 2>/dev/null)",
  "mem_total_kib": "$(awk '/MemTotal/{print $2}' /proc/meminfo 2>/dev/null)",
  "instance_type": "$(imds instance-type)",
  "ami_id": "$(imds ami-id)",
  "availability_zone": "$(imds placement/availability-zone)",
  "runtime": ${runtime:-null},
  "model": ${model:-null}
}
EOF
