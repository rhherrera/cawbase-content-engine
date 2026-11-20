#!/usr/bin/env bash
# Instance-side backstop (AWS only): power the host off if the inference server has done no work for
# CCE_GUARD_IDLE_MIN minutes (default 120, deliberately longer than the controller's 60-minute rule plus the
# readiness limit). It protects against a dead controller leaving the GPU on. Activity = a change in the server's
# token counters or a request in progress; /health probes never decode, so they never count. With the instance's
# shutdown behaviour set to "stop", poweroff stops (not terminates) the instance; the controller then sees it
# stopped and reconciles.
#
# Run by cce-inference-idle-guard.timer every 5 minutes. State: /var/lib/cce-inference/idle-guard.state
set -euo pipefail

IDLE_MIN=${CCE_GUARD_IDLE_MIN:-120}
URL=${CCE_GUARD_METRICS_URL:-http://127.0.0.1:8080/metrics}
STATE=${CCE_GUARD_STATE:-/var/lib/cce-inference/idle-guard.state}
KEY_FILE=${CCE_API_KEY_FILE:-/etc/cce-inference/api-key}
NOW=$(date +%s)
BOOT=$(( NOW - $(cut -d. -f1 /proc/uptime) ))

metrics=""
if [ -s "$KEY_FILE" ]; then
  metrics=$(curl -fsS -m 5 -H "Authorization: Bearer $(head -n1 "$KEY_FILE")" "$URL" 2>/dev/null || true)
fi
sig=$(printf '%s\n' "$metrics" | grep -E '^llamacpp:(tokens_predicted_total|prompt_tokens_total) ' | sort | tr '\n' ' ' || true)
busy=$(printf '%s\n' "$metrics" | awk '/^llamacpp:requests_(processing|deferred) /{s+=$2} END{print s+0}')

last_sig=""; last_change=$BOOT; last_boot=$BOOT
if [ -f "$STATE" ]; then
  # shellcheck disable=SC1090
  . "$STATE"
fi
if [ "$last_boot" != "$BOOT" ]; then last_sig=""; last_change=$BOOT; fi  # new boot: reset
if [ "$busy" != "0" ] || { [ -n "$sig" ] && [ "$sig" != "$last_sig" ]; }; then
  last_change=$NOW
fi
mkdir -p "$(dirname "$STATE")"
printf 'last_sig=%q\nlast_change=%q\nlast_boot=%q\n' "$sig" "$last_change" "$BOOT" > "$STATE"

idle=$(( (NOW - last_change) / 60 ))
echo "idle-guard: idle=${idle}min limit=${IDLE_MIN}min busy=$busy"
if [ "$idle" -ge "$IDLE_MIN" ]; then
  echo "idle-guard: no inference activity for ${idle} min; powering off" >&2
  if [ "${CCE_GUARD_DRY_RUN:-0}" = 1 ]; then echo "DRY RUN: poweroff"; exit 0; fi
  systemctl poweroff
fi
