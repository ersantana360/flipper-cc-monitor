#!/usr/bin/env bash
# Reports a status to the bridge. Usage: cc-state.sh <status> [detail]
# Reads the hook JSON on stdin (if any) to fill the detail with the tool name / message.
BRIDGE="${CC_BRIDGE_URL:-http://localhost:8730}"
status="${1:-working}"; detail="${2:-}"
input=$(cat 2>/dev/null || true)
if [ -z "$detail" ] && [ -n "$input" ] && command -v jq >/dev/null 2>&1; then
  detail=$(printf '%s' "$input" | jq -r '(.tool_name // .message // .notification_type // "") | tostring' 2>/dev/null | head -c 60)
fi
payload=$(printf '{"status":"%s","detail":%s}' "$status" "$(printf '%s' "$detail" | jq -Rs . 2>/dev/null || echo '""')")
curl -s --max-time 2 -X POST "$BRIDGE/state" -H 'Content-Type: application/json' -d "$payload" >/dev/null 2>&1 &
exit 0
