#!/usr/bin/env bash
# Start the bridge in the background (idempotent). Logs to ~/.claude/cc-monitor/bridge.log
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
LOGDIR="$HOME/.claude/cc-monitor"; mkdir -p "$LOGDIR"
if curl -s --max-time 1 localhost:8730/health >/dev/null 2>&1; then
  echo "bridge already running on :8730"; exit 0
fi
setsid nohup python3 "$DIR/main.py" >> "$LOGDIR/bridge.log" 2>&1 < /dev/null &
for i in 1 2 3 4 5 6; do sleep 0.5; curl -s --max-time 2 localhost:8730/health >/dev/null 2>&1 && { echo "bridge started (log: $LOGDIR/bridge.log)"; exit 0; }; done
echo "bridge failed to start, see $LOGDIR/bridge.log" >&2; exit 1
