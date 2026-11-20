#!/usr/bin/env bash
# Switch the server profile in server.env (persists across reboots) and restart the server. Used by the GPU
# validation campaign, which restores the original profile at the end.
#   switch-profile.sh --current | NAME [--no-restart]
set -euo pipefail
ENV_FILE=${CCE_SERVER_ENV:-/etc/cce-inference/server.env}
PROFILES=${CCE_PROFILES_DIR:-$(cd "$(dirname "$0")" && pwd)/profiles}
RESTART=${CCE_RESTART_CMD:-systemctl restart cce-inference.service}
current=$(sed -n 's/^CCE_PROFILE=//p' "$ENV_FILE" | tail -n1)
name=${1:?usage: switch-profile.sh --current | NAME [--no-restart]}
if [ "$name" = --current ]; then basename "$current" .env; exit 0; fi
case $name in *[!A-Za-z0-9._-]*) echo "invalid profile name" >&2; exit 2 ;; esac
[ -f "$PROFILES/$name.env" ] || { echo "unknown profile: $name" >&2; exit 2; }
body=$(grep -v '^CCE_PROFILE=' "$ENV_FILE" || true)
printf '%s\nCCE_PROFILE=%s\n' "$body" "$PROFILES/$name.env" > "$ENV_FILE"  # in place: owner and mode are kept
echo "profile: $(basename "$current" .env) -> $name"
[ "${2:-}" = --no-restart ] || bash -c "$RESTART"
