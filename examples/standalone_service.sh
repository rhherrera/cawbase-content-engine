#!/usr/bin/env bash
# Standalone service walkthrough (SQLite, loopback only). Reproducible:
#   uv run bash examples/standalone_service.sh
# The service validates, migrates, verifies, and only then starts listening.
set -euo pipefail

WORK="$(mktemp -d)"
PORT="${PORT:-18080}"
export CAWBASE_CONTENT_ENGINE_DATABASE_URL="sqlite+aiosqlite:///$WORK/service.db"
CLI="python -m cawbase_content_engine"
trap 'kill "${PID:-0}" 2>/dev/null || true' EXIT

# One-time operator step. The secret is written to a new 0600 file, never to a log.
$CLI migrate
$CLI bootstrap-admin --secret-file "$WORK/admin.key"
KEY="$(cat "$WORK/admin.key")"

$CLI serve --port "$PORT" >"$WORK/service.log" 2>&1 &
PID=$!
for _ in $(seq 1 100); do
  curl -fs "http://127.0.0.1:$PORT/readyz" >/dev/null 2>&1 && break
  sleep 0.2
done

AUTH="Authorization: Bearer $KEY"
curl -fs -X POST "http://127.0.0.1:$PORT/v1/admin/scopes" -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"scope_id":"demo","display_name":"Demo"}' >/dev/null
curl -fs "http://127.0.0.1:$PORT/v1/admin/scopes/demo" -H "$AUTH"
echo
curl -s -o /dev/null -w 'unauthenticated -> %{http_code}\n' "http://127.0.0.1:$PORT/v1/scopes/demo"
