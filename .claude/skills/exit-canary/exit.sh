#!/usr/bin/env bash
# Leave the canary: clear the host-only fm_canary cookie in your default browser
# and stop the enter-canary launcher. The counterpart to enter-canary.
#
# /canary/off needs NO secret — clearing the cookie only pushes you to STABLE
# (the safe default), so there's nothing to gate. It just opens the gateway
# clear-cookie endpoint in your browser (302 -> Set-Cookie Max-Age=0 -> /).
#
# This exits YOUR BROWSER's canary routing. It does NOT scale down the canary
# WORKLOADS — for that use the `canary` skill:  canary retire <svc>.
#
# Usage:  .claude/skills/exit-canary/exit.sh
set -uo pipefail

HUB="${FM_HUB_ORIGIN:-https://x9bc433.win}"

echo "clearing the fm_canary cookie via ${HUB}/canary/off ..."
if command -v open >/dev/null 2>&1; then
  open "${HUB}/canary/off"
elif command -v xdg-open >/dev/null 2>&1; then
  xdg-open "${HUB}/canary/off"
else
  echo "  (couldn't auto-open a browser; visit ${HUB}/canary/off manually)"
fi

echo "stopping the enter-canary launcher (if running) ..."
if pkill -f "enter-canary/launcher.py" 2>/dev/null; then
  echo "  launcher stopped"
else
  echo "  launcher not running"
fi

echo "done — you're back on stable prod."
echo "  (canary WORKLOADS are still running; scale them down with:  canary retire <svc>)"
