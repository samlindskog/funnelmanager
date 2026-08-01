#!/usr/bin/env bash
# drive-canary setup — (re)write the Playwright MCP config to COOKIE mode, verify
# both MCP servers (grafana + playwright), and emit the target-specific loop wiring
# for the agent-driven E2E loop.
#
# COOKIE mode = headless chromium, NO extraHTTPHeaders. The old `x-fm-canary` header
# is now stripped by the gateway AND broke Keycloak token refresh via CORS; a debug
# session is reached instead by seeding the host-only `fm_debug` cookie (+ the
# `fm_route=canary` selector for a canary target) at RUNTIME (the drive-canary loop,
# step 2 — a persistent-profile cookie can't be set from this static config). This
# script only writes the config + verifies wiring + prints the shim; it drives
# nothing.
#
# --target prod | canary  (default: canary)
#   canary  set fm_debug + fm_route=canary via /debug/canary/on -> requests route
#           to <svc>-canary; the CANARY bundle's own Faro gives full RUM + browser
#           traces. No shim needed.
#   prod    set fm_debug ONLY via /debug/on -> requests route to STABLE/prod pods,
#           but fm_debug PERMITS forced tracing. Prod SPA bundles ship NO Faro
#           (tree-shaken), so there is NO browser RUM on prod — inject the
#           traceparent SHIM (below) so /api/* requests carry a sampled trace the
#           BACKEND honors. LIMITATION: backend spans only, no browser/RUM spans.
#
# NOTE: config changes take effect only on an MCP/session RESTART.
#
# Usage: .claude/skills/drive-canary/setup.sh [config|verify|shim|all] [--target prod|canary]
#   all     (default) write the config, verify both MCPs, print target guidance
#   config  (re)write ~/.config/fm-e2e/playwright-mcp.json in cookie mode
#   verify  check creds.env is present and both MCP servers are Connected
#   shim    print the exact prod-tracing browser_evaluate shim (for --target prod)

set -uo pipefail   # NOT -e: a failed check must not abort the report

CFG_DIR="$HOME/.config/fm-e2e"
MCP_CFG="$CFG_DIR/playwright-mcp.json"
CREDS="$CFG_DIR/creds.env"
OK="✅"; WARN="⚠️"; BAD="❌"

# ---- arg parse: a subcommand plus an optional --target ---------------------
CMD="all"
TARGET="canary"
while [ $# -gt 0 ]; do
  case "$1" in
    config|verify|shim|all) CMD="$1" ;;
    --target) shift; TARGET="${1:-}" ;;
    --target=*) TARGET="${1#*=}" ;;
    *) echo "$BAD unknown arg '$1'"; sed -n '26,30p' "$0"; exit 1 ;;
  esac
  shift
done
case "$TARGET" in prod|canary) ;; *) echo "$BAD --target must be prod|canary"; exit 1 ;; esac

write_config() {
  echo "== write Playwright MCP config (cookie mode) =="
  mkdir -p "$CFG_DIR"
  # Headless chromium, NO extraHTTPHeaders — the debug session is gated by the
  # runtime-seeded fm_debug cookie, not by a header (stripped at the gateway +
  # broke KC CORS refresh).
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

# The prod-tracing shim, printed for injection via the Playwright MCP
# `browser_evaluate` AFTER navigation/login. It wraps window.fetch to stamp a
# fresh W3C traceparent (sampled) on SAME-ORIGIN /api/* requests ONLY — never
# Keycloak / cross-origin (a custom header there triggers a CORS preflight that
# breaks token refresh: the x-fm-canary lesson). It stashes the last trace id on
# window.__fm_last_trace_id for hand-off to observe-grafana. Idempotent.
print_shim() {
  cat <<'JS'
// Paste as the argument to mcp__playwright__browser_evaluate, AFTER login+settle.
// (--target prod only; a canary target already emits full Faro RUM+traces.)
() => {
  if (window.__fm_trace_shim) return 'already installed';
  const hex = (n) => {
    const b = new Uint8Array(n);
    crypto.getRandomValues(b);
    return Array.from(b, x => x.toString(16).padStart(2, '0')).join('');
  };
  const origin = window.location.origin;
  const orig = window.fetch.bind(window);
  window.fetch = (input, init) => {
    try {
      const raw = (typeof input === 'string' || input instanceof URL) ? input : input.url;
      const url = new URL(raw, origin);
      // SAME-ORIGIN /api/* ONLY — never Keycloak / cross-origin (CORS preflight
      // on a custom header breaks token refresh).
      if (url.origin === origin && url.pathname.startsWith('/api/')) {
        const traceId = hex(16);           // 16 bytes -> 32 lowercase hex
        const spanId  = hex(8);            //  8 bytes -> 16 lowercase hex
        const tp = `00-${traceId}-${spanId}-01`;  // sampled (-01)
        window.__fm_last_trace_id = traceId;
        init = init ? Object.assign({}, init) : {};
        const h = new Headers(init.headers ||
          (input instanceof Request ? input.headers : undefined));
        h.set('traceparent', tp);
        init.headers = h;
      }
    } catch (e) { /* never break fetch */ }
    return orig(input, init);
  };
  window.__fm_trace_shim = true;
  return 'installed';
}
JS
}

verify() {
  echo "== verify wiring =="
  # creds.env must exist (holds e2e-canary user/pass, FM_DEBUG_TOKEN, URLs).
  if [ -f "$CREDS" ]; then
    echo "$OK creds present: $CREDS"
    if grep -q '^FM_DEBUG_TOKEN=' "$CREDS"; then
      echo "$OK FM_DEBUG_TOKEN set in creds.env"
    else
      echo "$BAD FM_DEBUG_TOKEN missing in $CREDS (was FM_CANARY_TOKEN — rename it)"
    fi
  else
    echo "$BAD missing $CREDS (e2e-canary user/pass, FM_DEBUG_TOKEN, hub/kc/grafana URLs)"
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

target_guidance() {
  echo "== target: $TARGET =="
  if [ "$TARGET" = canary ]; then
    echo "$OK CANARY target — full RUM + traces from the canary bundle's own Faro."
    echo "   Seed cookies:   browser_navigate  https://x9bc433.win/debug/canary/on?t=<FM_DEBUG_TOKEN>"
    echo "   (sets fm_debug HttpOnly + fm_route=canary; /api/* routes to <svc>-canary)"
    echo "   No shim needed — capture traceparent from browser_network_requests as usual."
  else
    echo "$OK PROD target — fm_debug ONLY (routes to STABLE prod pods; permits forced tracing)."
    echo "   Seed cookie:    browser_navigate  https://x9bc433.win/debug/on?t=<FM_DEBUG_TOKEN>"
    echo "   (sets fm_debug HttpOnly; NO fm_route -> stable). Prod ships NO Faro, so:"
    echo "   Inject the SHIM via browser_evaluate AFTER login+settle:"
    echo "       .claude/skills/drive-canary/setup.sh shim"
    echo "   $WARN LIMITATION: backend spans ONLY, no browser/RUM spans on prod."
    echo "   Hand window.__fm_last_trace_id (or the /api/* request's traceparent) to observe-grafana."
  fi
}

case "$CMD" in
  config) write_config ;;
  verify) verify ;;
  shim)   print_shim ;;
  all)
    echo "############ drive-canary setup ($TARGET) — $(date -u '+%Y-%m-%dT%H:%M:%SZ') ############"
    echo; write_config
    echo; verify
    echo; target_guidance
    echo; echo "############ end — restart the MCP/session for changes to apply ############"
    ;;
  *) sed -n '26,30p' "$0"; exit 1 ;;
esac
