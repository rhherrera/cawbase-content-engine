#!/usr/bin/env bash
# Operator side: run one shell command on the instance through SSM Run Command (no SSH) and print its stdout.
# Exit status is non-zero when the command failed, timed out or could not be delivered.
#   ssm-run.sh INSTANCE_ID 'command' [TIMEOUT_S]
set -euo pipefail
AWS=${CCE_AWS_BIN:-aws}
POLL=${CCE_SSM_POLL_S:-2}
iid=${1:?instance id}; command=${2:?command}; timeout=${3:-600}
params=$(python3 -c 'import json, sys; print(json.dumps({"commands": [sys.argv[1]], "executionTimeout": [sys.argv[2]]}))' \
  "$command" "$timeout")
cid=$("$AWS" ssm send-command --instance-ids "$iid" --document-name AWS-RunShellScript --parameters "$params" \
  --query Command.CommandId --output text)
inv() { "$AWS" ssm get-command-invocation --command-id "$cid" --instance-id "$iid" --query "$1" --output text; }
deadline=$((SECONDS + timeout + 60))
while :; do
  status=$(inv Status 2>/dev/null || echo Pending)
  case $status in Success | Failed | Cancelled | TimedOut | Cancelling) break ;; esac
  [ "$SECONDS" -lt "$deadline" ] || { echo "ssm-run: timed out waiting for $cid" >&2; exit 124; }
  sleep "$POLL"
done
inv StandardOutputContent
if [ "$status" != Success ]; then
  inv StandardErrorContent >&2 || true
  echo "ssm-run: $cid $status" >&2
  exit 1
fi
