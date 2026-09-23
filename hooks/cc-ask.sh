#!/usr/bin/env bash
# PreToolUse hook: ask the Flipper before a tool runs.
# exit 0 = let the tool run, exit 2 = block it (stderr goes back to Claude).
# If the bridge is down, or no Flipper is polling it, the tool is allowed.
BRIDGE="${CC_BRIDGE_URL:-http://localhost:8730}"
input=$(cat)
tool=$(printf '%s' "$input" | jq -r '.tool_name // "tool"' 2>/dev/null)
detail=$(printf '%s' "$input" | jq -r '.tool_input | (.command // .file_path // .description // .pattern // "") | tostring' 2>/dev/null | head -c 120)
payload=$(jq -cn --arg t "$tool" --arg d "$detail" '{tool:$t, detail:$d}' 2>/dev/null || printf '{"tool":"%s"}' "$tool")
answer=$(curl -s --max-time 100 -X POST "$BRIDGE/ask" -H 'Content-Type: application/json' -d "$payload" 2>/dev/null)
case "$answer" in
  allow|passthrough|"") exit 0 ;;
  *) echo "Denied from the Flipper: $tool was not approved on the device." >&2; exit 2 ;;
esac
