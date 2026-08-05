#!/usr/bin/env bash
# e2e-access — unified, least-privilege escalation/deescalation for the e2e-canary
# test identity. ONE mechanism serves both driving modes; the TOKEN always comes
# from the normal browser PKCE flow (Playwright), never a special grant. See SKILL.md.
#
# Model:
#   * e2e-canary is DORMANT by default (no access roles).
#   * A dedicated service account `e2e-escalator` (standing, scoped `manage-users`)
#     is the ONLY identity used to add/remove e2e-canary's role-group membership —
#     not the master admin. escalate/deescalate run AS e2e-escalator.
#   * ESCALATE adds e2e-canary to an existing role group (standard|power|admins);
#     the agent then logs in as e2e-canary via Playwright (normal OIDC) to obtain a
#     token — used to drive the browser UI OR captured to curl the backend API.
#   * DEESCALATE removes the group membership — always, even on failure.
#
# Verbs:  init | escalate <group> | deescalate | status
# (No token verb — the token is minted by the browser login, per SKILL.md.)
set -euo pipefail

VERB="${1:-status}"; GROUP="${2:-}"
[ -f "$HOME/.config/fm-ops/env" ] && . "$HOME/.config/fm-ops/env" || true
HOST="${FM_CP_HOST:-usfr4}"

ssh -o BatchMode=yes -o ConnectTimeout=30 "$HOST" "VERB='$VERB' GROUP='$GROUP' bash -s" <<'REMOTE'
set -euo pipefail
REALM=funnelmanager
ESC=e2e-escalator          # the standing scoped escalation identity (service account)
E2E_USER=e2e-canary
NS=identity
SECRET=fm-e2e-escalator    # in-cluster Secret holding the escalator client secret

KP=$(sudo -n kubectl -n "$NS" get pod -l app.kubernetes.io/name=keycloak -o jsonpath='{.items[0].metadata.name}')
# kcadm as MASTER admin (bootstrap only: init).
kcadm_master() { sudo -n kubectl -n "$NS" exec "$KP" -- bash -c "/opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:8080 --realm master --user \"\$KC_BOOTSTRAP_ADMIN_USERNAME\" --password \"\$KC_BOOTSTRAP_ADMIN_PASSWORD\" >/dev/null 2>&1; /opt/keycloak/bin/kcadm.sh $1"; }
# kcadm as the SCOPED escalator (routine escalate/deescalate). Reads the secret from k8s.
kcadm_esc() {
  local sec; sec=$(sudo -n kubectl -n "$NS" get secret "$SECRET" -o jsonpath='{.data.client_secret}' 2>/dev/null | base64 -d)
  [ -n "$sec" ] || { echo "escalator secret missing (run init)"; return 1; }
  sudo -n kubectl -n "$NS" exec "$KP" -- bash -c "/opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:8080 --realm $REALM --client $ESC --secret '$sec' >/dev/null 2>&1; /opt/keycloak/bin/kcadm.sh $1"
}
uuid() { grep -oE '[0-9a-f]{8}-[0-9a-f-]{27,}' | head -1; }
group_id() { kcadm_master "get groups -r $REALM --fields id,name" | tr -d ' \n' | grep -oE "\"id\":\"[0-9a-f-]{36}\",\"name\":\"$1\"" | grep -oE '[0-9a-f-]{36}' | head -1; }
user_id()  { kcadm_master "get users -r $REALM -q username=$1 --fields id" | uuid; }

case "$VERB" in
  init)
    echo "== e2e-access init =="
    CID=$(kcadm_master "get clients -r $REALM -q clientId=$ESC --fields id" | uuid || true)
    if [ -z "${CID:-}" ]; then
      echo "creating scoped escalator service account: $ESC"
      kcadm_master "create clients -r $REALM -s clientId=$ESC -s protocol=openid-connect -s enabled=true -s publicClient=false -s standardFlowEnabled=false -s implicitFlowEnabled=false -s directAccessGrantsEnabled=false -s serviceAccountsEnabled=true -s 'redirectUris=[]' -s 'webOrigins=[]'"
      CID=$(kcadm_master "get clients -r $REALM -q clientId=$ESC --fields id" | uuid)
      # grant ONLY realm-management:manage-users (can toggle group membership; not full admin)
      kcadm_master "add-roles -r $REALM --uusername service-account-$ESC --cclientid realm-management --rolename manage-users" && echo "  + manage-users"
    else
      echo "escalator $ESC already exists ($CID)"
    fi
    CS=$(kcadm_master "get clients/$CID/client-secret -r $REALM" | grep -oE '"value"[^,}]*' | grep -oE '[^"]+$' | tail -1)
    sudo -n kubectl -n "$NS" create secret generic "$SECRET" --from-literal=client_secret="$CS" --dry-run=client -o yaml | sudo -n kubectl apply -f - >/dev/null
    echo "stored escalator client_secret in Secret $NS/$SECRET"
    echo "de-privileging $E2E_USER to dormant (removing standing access roles)"
    for r in search-access agents-access jobs-access mail-access admin; do
      kcadm_master "remove-roles -r $REALM --uusername $E2E_USER --rolename $r" >/dev/null 2>&1 && echo "  - $r" || true
    done
    echo "init done. e2e-canary is dormant; escalate via '$ESC'."
    ;;

  escalate)
    [ -n "$GROUP" ] || { echo "usage: escalate <standard|power|admins>"; exit 1; }
    GID=$(group_id "$GROUP"); EID=$(user_id "$E2E_USER")
    [ -n "$GID" ] && [ -n "$EID" ] || { echo "group/user not found"; exit 1; }
    kcadm_esc "update users/$EID/groups/$GID -r $REALM -n" >/dev/null && echo "ESCALATED (as $ESC): $E2E_USER + group '$GROUP'"
    echo "-> now log in as $E2E_USER via Playwright to obtain the token (see SKILL.md); DEESCALATE when done."
    ;;

  deescalate)
    EID=$(user_id "$E2E_USER")
    for g in standard power admins; do
      GID=$(group_id "$g"); [ -n "$GID" ] && kcadm_esc "delete users/$EID/groups/$GID -r $REALM" >/dev/null 2>&1 && echo "removed from group '$g'" || true
    done
    echo "DEESCALATED: $E2E_USER back to dormant."
    ;;

  status)
    EID=$(user_id "$E2E_USER")
    echo -n "$E2E_USER roles:  "; kcadm_master "get users/$EID/role-mappings/realm -r $REALM --fields name" | grep name | tr -d ' \n'; echo
    echo -n "$E2E_USER groups: "; kcadm_master "get users/$EID/groups -r $REALM --fields name" | grep name | tr -d ' \n'; echo
    CID=$(kcadm_master "get clients -r $REALM -q clientId=$ESC --fields id" | uuid || true)
    [ -n "${CID:-}" ] && echo "escalator $ESC: present" || echo "escalator $ESC: NOT created (run init)"
    ;;
  *) echo "unknown verb: $VERB (init|escalate <group>|deescalate|status)"; exit 1;;
esac
REMOTE
