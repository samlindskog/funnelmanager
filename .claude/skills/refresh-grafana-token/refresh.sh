#!/usr/bin/env bash
# Re-mint the Grafana MCP service-account token after a grafana pod roll.
#
# WHY THIS EXISTS. Grafana runs on EPHEMERAL storage (emptyDir, no PVC), so every
# time the grafana pod rolls its SQLite DB — including all service accounts and
# their tokens — is wiped. The token baked into the Grafana MCP config
# (~/.claude.json, `GRAFANA_SERVICE_ACCOUNT_TOKEN=glsa_…`) then starts returning
# 401 and every mcp__grafana__* call fails. This script mints a fresh SA token
# from the grafana-admin creds (in-cluster) and rewrites the MCP config in place.
#
# You MUST RESTART the Claude session (or the grafana MCP) afterward — the running
# MCP holds the OLD token in its docker process env; the new one only loads on
# MCP startup.
#
# Usage: .claude/skills/refresh-grafana-token/refresh.sh [--force]
#   (no arg)  mint a new token ONLY if the current one is dead (fast no-op if live)
#   --force   rotate even if the current token still works
#
# Prereqs: the gitignored ops env (FM_CP_HOST — see _lib/ops-env.example), ssh to
# the control plane, the `grafana-admin` secret in the `monitoring` namespace, and
# the public Grafana URL reachable. Reads NOTHING from git.
set -uo pipefail

# Shared ops config + SSH/K/FLUX prefixes (FM_CP_HOST, FM_KUBECONFIG).
. "$(cd "$(dirname "$0")/../_lib" && pwd)/common.sh"

GRAFANA_URL="${FM_GRAFANA_URL:-https://grafana.x9bc433.win}"  # PLACEHOLDERS.md (Grafana host)
SA_NAME="${FM_GRAFANA_SA:-claude-mcp}"
CLAUDE_JSON="${CLAUDE_CONFIG:-$HOME/.claude.json}"

die() { echo "error: $*" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || die "python3 required (local)"
[ -f "$CLAUDE_JSON" ] || die "no Claude config at $CLAUDE_JSON"

# --- current token in the MCP config (for the liveness check + replacement) ----
CUR=$(grep -oE 'GRAFANA_SERVICE_ACCOUNT_TOKEN=glsa_[A-Za-z0-9_]+' "$CLAUDE_JSON" 2>/dev/null | head -1 | sed 's/^[^=]*=//')
if [ -z "$CUR" ]; then
  echo "note: no existing GRAFANA_SERVICE_ACCOUNT_TOKEN found in $CLAUDE_JSON."
  echo "      is the grafana MCP configured there? Proceeding to mint anyway (nothing to replace = no-op write)."
fi

# --- liveness check (skip the mint if the current token still works) -----------
force="${1:-}"
if [ -n "$CUR" ] && [ "$force" != "--force" ]; then
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 \
    -H "Authorization: Bearer $CUR" "$GRAFANA_URL/api/datasources" 2>/dev/null || echo 000)
  if [ "$code" = 200 ]; then
    echo "current Grafana MCP token is still valid (HTTP 200) — nothing to do."
    echo "(use --force to rotate anyway.)"
    exit 0
  fi
  echo "current token -> HTTP $code (dead). Minting a fresh one…"
fi

# --- mint a fresh SA token from the grafana-admin creds (kept in-cluster) -------
# Runs on the control plane: read the admin creds, create/reuse the SA, mint a
# token, print ONLY the token key. Admin creds + the token never touch git.
NEW=$($SSH "FM_KUBECONFIG='$FM_KUBECONFIG' GRAFANA_URL='$GRAFANA_URL' SA_NAME='$SA_NAME' bash -s" <<'REMOTE' 2>/dev/null
set -e
KC="sudo -n env KUBECONFIG=$FM_KUBECONFIG kubectl"
U=$($KC -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-user}' | base64 -d)
PW=$($KC -n monitoring get secret grafana-admin -o jsonpath='{.data.admin-password}' | base64 -d)
auth="$U:$PW"
# Create the SA (Admin role — the MCP runs --disable-write, so it stays read-only
# in practice). If it already exists (grafana did NOT roll), reuse it by search.
ID=$(curl -s -u "$auth" -H 'Content-Type: application/json' \
      -d "{\"name\":\"$SA_NAME\",\"role\":\"Admin\",\"isDisabled\":false}" \
      "$GRAFANA_URL/api/serviceaccounts" \
     | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("id") or "")' 2>/dev/null || true)
if [ -z "$ID" ]; then
  ID=$(curl -s -u "$auth" "$GRAFANA_URL/api/serviceaccounts/search?query=$SA_NAME" \
       | python3 -c 'import json,sys; a=json.load(sys.stdin).get("serviceAccounts") or []; print(a[0]["id"] if a else "")')
fi
[ -n "$ID" ] || { echo "could not create or find the $SA_NAME service account" >&2; exit 1; }
# Mint the token (name carries a hint; grafana requires token names unique per SA,
# so include the SA id to avoid a collision on repeated --force without a roll).
curl -s -u "$auth" -H 'Content-Type: application/json' \
     -d "{\"name\":\"claude-mcp-$ID-$(date +%s 2>/dev/null || echo t)\"}" \
     "$GRAFANA_URL/api/serviceaccounts/$ID/tokens" \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["key"]) if d.get("key") else (sys.stderr.write(json.dumps(d)) or sys.exit(1))'
REMOTE
)

case "$NEW" in
  glsa_*) : ;;
  *) die "failed to mint a token (got: ${NEW:-<empty>}). Check ssh to $FM_CP_HOST + the grafana-admin secret." ;;
esac

# --- verify the new token BEFORE writing it (never persist a broken token) ------
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 12 \
  -H "Authorization: Bearer $NEW" "$GRAFANA_URL/api/datasources" 2>/dev/null || echo 000)
[ "$code" = 200 ] || die "the new token did not authenticate (HTTP $code) — config NOT changed."

# --- rewrite the MCP config in place (string-replace preserves formatting) ------
python3 - "$CLAUDE_JSON" "$NEW" <<'PY'
import json, re, shutil, sys
f, new = sys.argv[1], sys.argv[2]
t = open(f).read()
t2, n = re.subn(r'(GRAFANA_SERVICE_ACCOUNT_TOKEN=)glsa_[A-Za-z0-9_]+', r'\g<1>' + new, t)
if n == 0:
    sys.stderr.write("no GRAFANA_SERVICE_ACCOUNT_TOKEN=glsa_… found to replace — "
                     "add the grafana MCP to your config, or set it manually.\n")
    sys.exit(2)
json.loads(t2)  # must stay valid JSON
shutil.copy(f, f + ".bak-grafana-token")
open(f, "w").write(t2)
print(f"  updated {n} token reference(s); backup at {f}.bak-grafana-token")
PY
rc=$?
[ "$rc" = 0 ] || die "config not updated (python exit $rc). The token WAS minted and verified — set it by hand if needed."

echo
echo "✅ Grafana MCP token refreshed and verified (reads datasources)."
echo "⚠️  RESTART this Claude session (or the grafana MCP) so it reconnects with the new token —"
echo "    the running MCP still holds the old one in its process env."
