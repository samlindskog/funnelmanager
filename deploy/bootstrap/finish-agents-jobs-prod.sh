#!/usr/bin/env bash
# One-shot: finish the P0–P5 (agents/jobs) rollout on the PROD k3s cluster.
#
# The realm's new clients/roles/scopes/mappers and all scope wiring were already
# applied in-place via the Keycloak Admin API. This script closes the last two
# SENSITIVE gaps that the agent's auto-mode guardrail (correctly) would not do
# unattended — a permission grant and secret writes:
#
#   1. Sets the `agents` + `jobs` confidential-client secrets in Keycloak AND
#      creates the matching prod k8s secrets (fm-oidc-agents / fm-oidc-jobs),
#      plus the shared Principle-4 approval HMAC (fm-approval). Values are
#      generated HERE and written to both places, so they always match and
#      never appear in a chat transcript.
#   2. Grants the realm `admin` role the new *-access composites so admins can
#      reach the new services.
#
# Idempotent: re-running rotates the three secrets consistently (KC + k8s) and
# re-asserts the composites. Run it ON usfr4 (needs sudo kubectl + the keycloak
# pod), e.g. from your laptop:
#
#     ssh usfr4 'bash -s' < deploy/bootstrap/finish-agents-jobs-prod.sh
#
# After it prints PROD-FINISH-DONE, cut the release (builds jobs/agents/agentsui
# images, pins, Flux rolls out; leads/mail leave CreateContainerConfigError once
# fm-approval exists):
#
#     .claude/skills/deploy-funnelmanager/deploy.sh release vX.Y.Z
set -euo pipefail

AGENTS_SECRET="$(openssl rand -hex 32)"
JOBS_SECRET="$(openssl rand -hex 32)"
APPROVAL_SECRET="$(openssl rand -hex 32)"

echo "== 1/2  Keycloak: client secrets + admin composites (in-pod) =="
sudo -n kubectl -n identity exec -i deploy/keycloak -- \
  env AS="$AGENTS_SECRET" JS="$JOBS_SECRET" bash -s <<'POD'
set -u
K=/opt/keycloak/bin/kcadm.sh; R=funnelmanager
$K config credentials --server http://localhost:8080 --realm master \
  --user "$KC_BOOTSTRAP_ADMIN_USERNAME" --password "$KC_BOOTSTRAP_ADMIN_PASSWORD" >/dev/null 2>&1
cid(){ $K get clients -r $R -q clientId=$1 --fields id --format csv 2>/dev/null | tr -d '"' | head -1; }
rid(){ $K get roles/$1 -r $R --fields id --format csv 2>/dev/null | tr -d '"' | head -1; }
$K update clients/"$(cid agents)" -s "secret=$AS" -r $R && echo "  agents client secret set"
$K update clients/"$(cid jobs)"   -s "secret=$JS" -r $R && echo "  jobs client secret set"
{ printf '['; f=1
  for ch in search-access mail-access jobs-access agents-access; do
    i=$(rid "$ch"); [ $f -eq 1 ] || printf ','
    printf '{"id":"%s","name":"%s","composite":false,"clientRole":false,"containerId":"'"$R"'"}' "$i" "$ch"
    f=0
  done; printf ']'; } > /tmp/kids.json
$K create roles/admin/composites -r $R -f /tmp/kids.json && echo "  admin composites set" || echo "  admin composites already present"
rm -f /tmp/kids.json
POD

echo "== 2/2  k3s prod secrets (create-or-update) =="
mk(){ # mk <name> <key> <value>
  sudo -n kubectl -n prod create secret generic "$1" --from-literal="$2=$3" \
    --dry-run=client -o yaml | sudo -n kubectl -n prod apply -f - >/dev/null
  echo "  prod/$1 applied"
}
mk fm-oidc-agents client-secret "$AGENTS_SECRET"
mk fm-oidc-jobs   client-secret "$JOBS_SECRET"
mk fm-approval    secret        "$APPROVAL_SECRET"

echo "PROD-FINISH-DONE"
