#!/usr/bin/env bash
# Print the boot timeline of the current boot as JSON epoch seconds (run on the GPU host after a cold start):
# kernel boot, cce-inference unit start, llama-server "loading model" / "model loaded" / "listening" log lines.
# Combine with the operator-side StartInstances and `running` times to separate EC2 start, OS boot, weight load
# and time to readiness (the canary time comes from `cce-inference probe --full` or measure.py).
set -uo pipefail
UNIT=${1:-cce-inference.service}
btime=$(awk '/^btime/{print $2}' /proc/stat)
unit_start=$(systemctl show "$UNIT" -p ExecMainStartTimestampMonotonic --value 2>/dev/null)
first() { journalctl -b -u "$UNIT" -o short-unix --no-pager 2>/dev/null | grep -m1 -- "$1" | cut -d' ' -f1; }
unit_epoch=""
if [ -n "$unit_start" ] && [ "$unit_start" != 0 ]; then
  unit_epoch=$(awk -v b="$btime" -v m="$unit_start" 'BEGIN{printf "%.3f", b + m/1e6}')
fi
cat <<EOF
{
  "kernel_boot_epoch": ${btime:-null},
  "unit_start_epoch": ${unit_epoch:-null},
  "loading_model_epoch": $(first "loading model" | sed 's/^$/null/'),
  "model_loaded_epoch": $(first "model loaded" | sed 's/^$/null/'),
  "listening_epoch": $(first "listening on" | sed 's/^$/null/')
}
EOF
