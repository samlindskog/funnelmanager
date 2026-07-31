#!/usr/bin/env bash
# drive-canary setup — (re)write the Playwright MCP config to COOKIE mode and verify
# both MCP servers (grafana + playwright) are reachable for the agent-driven E2E loop.
#
# COOKIE mode = headless chromium, NO extraHTTPHeaders. The old `x-fm-canary` header
# is now stripped by the gateway AND broke Keycloak token refresh via CORS; the canary
# is reached instead by seeding a host-only `fm_canary` cookie at RUNTIME (the
# drive-canary loop, step 2 — a persistent-profile cookie can't be set from this static
# config). This script only writes the config + verifies wiring; it drives nothing.
#
# NOTE: config changes take effect only on an MCP/session RESTART.
#
# Usage: .claude/skills/drive-canary/setup.sh [config|verify|all]
#   all     (default) write the config, then verify both MCPs
#   config  (re)write ~/.config/fm-e2e/playwright-mcp.json in cookie mode
#   verify  check creds.env is present and both MCP servers are Connected

set -uo pipefail   # NOT -e: a failed check must not abort the report

CFG_DIR="$HOME/.config/fm-e2e"
MCP_CFG="$CFG_DIR/playwright-mcp.json"
CREDS="$CFG_DIR/creds.env"
OK="✅"; WARN="⚠️"; BAD="❌"

write_config() {
  echo "== write Playwright MCP config (cookie mode) =="
  mkdir -p "$CFG_DIR"
  # Headless chromium, NO extraHTTPHeaders — the canary is gated by the runtime-seeded
  # fm_canary cookie, not by a header (stripped at the gateway + broke KC CORS refresh).
  cat > "$MCP_CFG" <<'JSON'
{
  "browser": {
    "browserName": "chromium",
    "launchOptions": { "headless": true }
  }
}
JSON
  chmod 600 "$MCP_CFG"
  echo "$OK wrote $MCP_CFG (headless chromium, no extraHTTPHeaders)"
  echo "$WARN takes effect only after an MCP/session RESTART"
}

verify() {
  echo "== verify wiring =="
  # creds.env must exist (holds e2e-canary user/pass, fm_canary token, URLs).
  if [ -f "$CREDS" ]; then
    echo "$OK creds present: $CREDS"
  else
    echo "$BAD missing $CREDS (e2e-canary user/pass, FM_CANARY_TOKEN, hub/kc/grafana URLs)"
  fi

  # Both MCP servers must be Connected for the loop (playwright drives, grafana reads).
  local mcp
  mcp=$(claude mcp list 2>&1)
  for s in grafana playwright; do
    if echo "$mcp" | grep -iq "^$s:.*Connected\|$s:.*✔"; then
      echo "$OK MCP '$s' connected"
    elif echo "$mcp" | grep -iq "$s"; then
      echo "$WARN MCP '$s' listed but not Connected — check 'claude mcp list'"
    else
      echo "$BAD MCP '$s' not found — 'claude mcp add $s …' then restart"
    fi
  done
  echo "$WARN if you just (re)wrote the config or added an MCP, RESTART the session"
}

case "${1:-all}" in
  config) write_config ;;
  verify) verify ;;
  all)
    echo "############ drive-canary setup — $(date -u '+%Y-%m-%dT%H:%M:%SZ') ############"
    echo; write_config
    echo; verify
    echo; echo "############ end — restart the MCP/session for changes to apply ############"
    ;;
  *) sed -n '11,15p' "$0"; exit 1 ;;
esac
