#!/usr/bin/env bash
# Simulated host for the CLI test: start/stop/describe a fake llama-server launched through the real run-server.sh.
#   sim_host.sh DIR start|stop|describe
set -euo pipefail
DIR=$1; CMD=$2
PID_FILE="$DIR/server.pid"
alive() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }
case "$CMD" in
  describe) if alive; then echo running; else echo stopped; fi ;;
  start)
    if ! alive; then
      CCE_SERVER_ENV="$DIR/server.env" nohup "$(dirname "$0")/../../server/run-server.sh" > "$DIR/server.log" 2>&1 &
      echo $! > "$PID_FILE"
    fi ;;
  stop)
    if alive; then kill "$(cat "$PID_FILE")"; for _ in $(seq 1 50); do alive || break; sleep 0.1; done; fi
    rm -f "$PID_FILE" ;;
  *) exit 2 ;;
esac
