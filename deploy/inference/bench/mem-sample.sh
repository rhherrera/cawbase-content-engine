#!/usr/bin/env bash
# Memory evidence taken ON the measured host while the operator drives the load (through the tunnel):
#   mem-sample.sh start LABEL [PERIOD_S]   # background sampler (nvidia-smi + /proc/meminfo); returns at once
#   mem-sample.sh stop LABEL               # stops it and prints one JSON summary: host identity, sample counts,
#                                          # peaks, sampling window; status "unavailable" when there are no samples
# A missing sample is never reported as 0. Overrides (tests only): CCE_BENCH_DIR, CCE_NVIDIA_SMI, CCE_MEMINFO,
# CCE_DMI_DIR, CCE_IMDS_BASE.
set -uo pipefail
DIR=${CCE_BENCH_DIR:-/var/lib/cce-inference/bench}
SMI=${CCE_NVIDIA_SMI:-nvidia-smi}
MEMINFO=${CCE_MEMINFO:-/proc/meminfo}
DMI=${CCE_DMI_DIR:-/sys/devices/virtual/dmi/id}
IMDS=${CCE_IMDS_BASE:-http://169.254.169.254}
cmd=${1:?usage: mem-sample.sh start|stop LABEL}; label=${2:?label}
case $label in *[!A-Za-z0-9._-]*) echo "invalid label" >&2; exit 2 ;; esac
csv="$DIR/$label.csv"; pidf="$DIR/$label.pid"; stopf="$DIR/$label.stop"

imds() {
  local tok
  tok=$(curl -fsS -m 2 -X PUT "$IMDS/latest/api/token" -H 'X-aws-ec2-metadata-token-ttl-seconds: 60' 2>/dev/null) || return 0
  curl -fsS -m 2 -H "X-aws-ec2-metadata-token: $tok" "$IMDS/latest/meta-data/$1" 2>/dev/null
}

case "$cmd" in
  start)
    mkdir -p "$DIR"
    if [ -f "$pidf" ] && kill -0 "$(cat "$pidf")" 2>/dev/null; then echo "already sampling: $label" >&2; exit 1; fi
    rm -f "$csv" "$stopf"
    date +%s > "$DIR/$label.started"
    detach=(); command -v setsid > /dev/null && detach=(setsid)  # survive the end of the SSM command
    ${detach[@]+"${detach[@]}"} nohup "$0" _loop "$label" "${3:-0.5}" > /dev/null 2>&1 &
    echo $! > "$pidf"
    echo "sampling $label pid=$(cat "$pidf")" ;;
  _loop)
    while [ ! -f "$stopf" ]; do
      gpu=$("$SMI" --query-gpu=memory.used,memory.total --format=csv,noheader,nounits 2>/dev/null | head -n1 | tr -d ' ')
      ram=$(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{if (t && a) print t - a}' "$MEMINFO" 2>/dev/null)
      echo "$(date +%s),${gpu:-,},${ram}" >> "$csv"
      sleep "${3:-0.5}"
    done ;;
  stop)
    touch "$stopf"
    if [ -f "$pidf" ]; then
      pid=$(cat "$pidf")
      for _ in $(seq 1 50); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
      kill "$pid" 2>/dev/null || true
    fi
    stopped=$(date +%s); started=$(cat "$DIR/$label.started" 2>/dev/null || echo null)
    iid=$(imds instance-id); [ -n "$iid" ] || iid=$(cat "$DMI/board_asset_tag" 2>/dev/null)
    itype=$(imds instance-type)
    gpu=$("$SMI" --query-gpu=name --format=csv,noheader 2>/dev/null | head -n1)
    stats=$(awk -F, '
      $2 != "" { nv++; if ($2 > pv) pv = $2; tv = $3 }
      $4 != "" { nr++; if ($4 > pr) pr = $4 }
      END {
        printf "\"vram_samples\": %d, \"ram_samples\": %d, ", nv, nr
        printf "\"vram_peak_mib\": %s, \"vram_total_mib\": %s, ", (nv ? pv : "null"), (nv && tv != "" ? tv : "null")
        printf "\"ram_used_peak_kib\": %s, \"status\": \"%s\"", (nr ? pr : "null"), (nv && nr ? "ok" : "unavailable")
      }' "$csv" 2>/dev/null) || stats=""
    [ -n "$stats" ] || stats='"vram_samples": 0, "ram_samples": 0, "vram_peak_mib": null, "vram_total_mib": null, "ram_used_peak_kib": null, "status": "unavailable"'
    rm -f "$pidf" "$stopf"
    cat <<EOF
{"label": "$label", "host": {"hostname": "$(hostname)", "instance_id": "${iid}", "instance_type": "${itype}", "gpu_name": "${gpu}"}, "started_epoch": ${started}, "stopped_epoch": ${stopped}, ${stats}}
EOF
    ;;
  *) echo "usage: mem-sample.sh start|stop LABEL" >&2; exit 2 ;;
esac
