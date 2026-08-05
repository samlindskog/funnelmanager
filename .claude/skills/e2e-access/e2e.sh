#!/usr/bin/env bash
# e2e-access — sanctioned, least-privilege e2e-canary elevation + token minting for
# driving BACKEND API tests directly (before a service's UI exists / when the
# browser can't hand us a raw token). See SKILL.md for the doctrine.
#
# Model:
#   * e2e-canary is DORMANT by default (no access roles).
#   * A test ELEVATES it into an existing role group (standard|power|admins) AND
#     opens a token-mint window (enables directAccessGrants on the dedicated
#     confidential `e2e-driver` client), MINTS a short-lived token, runs, then
#     REVOKES both — always.
#   * All privileged work runs on the control-plane host via kcadm inside the
#     keycloak pod; secrets live in the in-cluster Secret `identity/fm-e2e-driver`
#     and never touch this repo or local disk.
#
# Verbs:  init | elevate <group> | mint | revoke | status
# Usage is agent-driven: the agent PRESENTS the exact group it needs, the human
# CONFIRMS, then the agent runs `elevate <group>` -> `mint` -> test -> `revoke`.
set -euo pipefail

VERB="${1:-status}"; GROUP="${2:-}"
HOST="${FM_CP_HOST:-usfr4}"
[ -f "$HOME/.config/fm-ops/env" ] && . "$HOME/.config/fm-ops/env" || true
HOST="${FM_CP_HOST:-usfr4}"

# For `init` only: seed the in-cluster Secret from the local e2e creds (the only
# time the e2e-canary password leaves creds.env; afterwards it lives in k8s).
SEED_PW=""
if [ "$VERB" = "init" ]; then
  CREDS="$HOME/.config/fm-e2e/creds.env"
  [ -f "$CREDS" ] || { echo "missing $CREDS (need FM_E2E_PASS to seed)"; exit 1; }
  SEED_PW="$(grep -oE '^FM_E2E_PASS=.*' "$CREDS" | cut -d= -f2-)"
  [ -n "$SEED_PW" ] || { echo "FM_E2E_PASS not set in $CREDS"; exit 1; }
fi

# Remote logic (runs on $HOST). Reads the verb + optional group + optional seed pw.
ssh -o BatchMode=yes -o ConnectTimeout=30 "$HOST" "VERB='$VERB' GROUP='$GROUP' SEED_PW='$SEED_PW' bash -s" <<'REMOTE'
set -euo pipefail
REALM=funnelmanager
DRIVER=e2e-driver
E2E_USER=e2e-canary
NS=identity
SECRET=fm-e2e-driver
ISSUER="https://kc.x9bc433.win/realms/${REALM}"
TOKEN_URL="${ISSUER}/protocol/openid-connect/token"

KP=$(sudo -n kubectl -n "$NS" get pod -l app.kubernetes.io/name=keycloak -o jsonpath='{.items[0].metadata.name}')
# kc "<kcadm args>" — runs kcadm inside the keycloak pod (localhost admin).
kc() { sudo -n kubectl -n "$NS" exec "$KP" -- bash -c "/opt/keycloak/bin/kcadm.sh config credentials --server http://localhost:8080 --realm master --user \"\$KC_BOOTSTRAP_ADMIN_USERNAME\" --password \"\$KC_BOOTSTRAP_ADMIN_PASSWORD\" >/dev/null 2>&1; /opt/keycloak/bin/kcadm.sh $1"; }
uuid() { grep -oE '[0-9a-f]{8}-[0-9a-f-]{27,}' | head -1; }

client_id() { kc "get clients -r $REALM -q clientId=$1 --fields id" | uuid; }
group_id()  { kc "get groups  -r $REALM --fields id,name" | tr -d ' \n' | grep -oE "\"id\":\"[0-9a-f-]{36}\",\"name\":\"$1\"" | grep -oE '[0-9a-f-]{36}' | head -1; }
user_id()   { kc "get users -r $REALM -q username=$1 --fields id" | uuid; }

secret_get() { sudo -n kubectl -n "$NS" get secret "$SECRET" -o jsonpath="{.data.$1}" 2>/dev/null | base64 -d; }

case "$VERB" in
  init)
    echo "== e2e-access init =="
    DID=$(client_id "$DRIVER" || true)
    if [ -z "${DID:-}" ]; then
      echo "creating confidential client $DRIVER (direct-grants OFF by default)"
      kc "create clients -r $REALM -s clientId=$DRIVER -s protocol=openid-connect -s enabled=true -s publicClient=false -s standardFlowEnabled=false -s implicitFlowEnabled=false -s serviceAccountsEnabled=false -s directAccessGrantsEnabled=false -s 'redirectUris=[]' -s 'webOrigins=[]'"
      DID=$(client_id "$DRIVER")
      # audience mappers so e2e-driver tokens name the services under test
      for svc in agents search jobs mail; do
        kc "create clients/$DID/protocol-mappers/models -r $REALM -s name=aud-$svc -s protocol=openid-connect -s protocolMapper=oidc-audience-mapper -s 'config.\"included.client.audience\"=$svc' -s 'config.\"id.token.claim\"=false' -s 'config.\"access.token.claim\"=true'" >/dev/null && echo "  + aud-$svc"
      done
    else
      echo "client $DRIVER already exists ($DID)"
    fi
    CS=$(kc "get clients/$DID/client-secret -r $REALM" | grep -oE '"value"[^,]*' | grep -oE '[^"]+$' | tail -1)
    sudo -n kubectl -n "$NS" create secret generic "$SECRET" \
      --from-literal=client_secret="$CS" --from-literal=e2e_password="$SEED_PW" \
      --dry-run=client -o yaml | sudo -n kubectl apply -f - >/dev/null
    echo "stored client_secret + e2e_password in Secret $NS/$SECRET"
    echo "de-privileging $E2E_USER to dormant (removing standing access roles)"
    for r in search-access agents-access jobs-access mail-access admin; do
      kc "remove-roles -r $REALM --uusername $E2E_USER --rolename $r" >/dev/null 2>&1 && echo "  - $r" || true
    done
    echo "init done."
    ;;

  elevate)
    [ -n "$GROUP" ] || { echo "usage: elevate <standard|power|admins>"; exit 1; }
    GID=$(group_id "$GROUP"); EID=$(user_id "$E2E_USER"); DID=$(client_id "$DRIVER")
    [ -n "$GID" ] && [ -n "$EID" ] && [ -n "$DID" ] || { echo "group/user/client not found (run init?)"; exit 1; }
    kc "update users/$EID/groups/$GID -r $REALM -n" >/dev/null && echo "elevated: $E2E_USER + group '$GROUP'"
    kc "update clients/$DID -r $REALM -s directAccessGrantsEnabled=true" >/dev/null && echo "mint window OPEN (e2e-driver direct-grants on)"
    ;;

  mint)
    DID=$(client_id "$DRIVER")
    CS=$(secret_get client_secret); PW=$(secret_get e2e_password)
    [ -n "$CS" ] && [ -n "$PW" ] || { echo "secret $SECRET missing client_secret/e2e_password (run init)"; exit 1; }
    RESP=$(curl -sS -m 20 "$TOKEN_URL" -d grant_type=password -d client_id="$DRIVER" -d client_secret="$CS" -d username="$E2E_USER" -d password="$PW" -d scope=openid)
    TOKEN=$(echo "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("access_token","") or "")')
    [ -n "$TOKEN" ] || { echo "MINT FAILED: $(echo "$RESP" | head -c 400)"; exit 1; }
    echo "$TOKEN" | sudo -n tee /tmp/e2e_token >/dev/null; sudo -n chmod 600 /tmp/e2e_token
    echo "$TOKEN" | cut -d. -f2 | python3 -c 'import sys,base64,json;b=sys.stdin.read().strip();b+="="*(-len(b)%4);d=json.loads(base64.urlsafe_b64decode(b));print("minted /tmp/e2e_token  aud=%s azp=%s user=%s roles=%s"%(d.get("aud"),d.get("azp"),d.get("preferred_username"),[r for r in d.get("realm_access",{}).get("roles",[]) if "access" in r or r=="admin"]))'
    ;;

  revoke)
    EID=$(user_id "$E2E_USER"); DID=$(client_id "$DRIVER")
    for g in standard power admins; do
      GID=$(group_id "$g"); [ -n "$GID" ] && kc "delete users/$EID/groups/$GID -r $REALM" >/dev/null 2>&1 && echo "removed from group '$g'" || true
    done
    [ -n "$DID" ] && kc "update clients/$DID -r $REALM -s directAccessGrantsEnabled=false" >/dev/null && echo "mint window CLOSED (e2e-driver direct-grants off)"
    sudo -n rm -f /tmp/e2e_token 2>/dev/null || true
    echo "revoked: $E2E_USER back to dormant."
    ;;

  status)
    EID=$(user_id "$E2E_USER"); DID=$(client_id "$DRIVER" || true)
    echo -n "$E2E_USER roles: "; kc "get users/$EID/role-mappings/realm -r $REALM --fields name" | grep name | tr -d ' \n'; echo
    echo -n "$E2E_USER groups: "; kc "get users/$EID/groups -r $REALM --fields name" | grep name | tr -d ' \n'; echo
    if [ -n "${DID:-}" ]; then
      echo -n "e2e-driver direct-grants: "; kc "get clients/$DID -r $REALM --fields directAccessGrantsEnabled" | grep -oE 'true|false'
    else echo "e2e-driver: NOT created (run init)"; fi
    ;;
  *) echo "unknown verb: $VERB (init|elevate <group>|mint|revoke|status)"; exit 1;;
esac
REMOTE
