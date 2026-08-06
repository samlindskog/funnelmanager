#!/usr/bin/env bash
# funnelmanager prod-health driver — READ-ONLY status/health of the k3s prod
# deployment and its CI/CD. Complements deploy-funnelmanager (which releases);
# this one never writes, patches, deploys, or mutates the realm.
#
# Prod is k3s: control plane usfr4 (ssh alias), namespaces prod (apps) +
# identity (Keycloak), Istio mesh + OPA ext_authz, Flux GitOps on main, public
# https://x9bc433.win behind Cloudflare. Images:
# ghcr.io/samlindskog/funnelmanager/<svc>:sha-<sha>, pin lives in
# deploy/apps/overlays/prod/kustomization.yaml (committed by CI).
#
# Every remote command is a read (get/logs/rollout status/flux get). No apply,
# patch, reconcile, scale, delete, or kcadm write is ever issued.
#
# Usage: .claude/skills/prod-health/check.sh [cmd]
#   all      (default) run every signal, print a one-screen verdict
#   pods     pod + deployment/statefulset readiness (prod + identity)
#   drift    running image sha vs pinned sha vs origin/main (deploy lag)
#   flux     Flux kustomization status (apps-prod revision / ready / suspended)
#   logs     bounded error tail of backends + Keycloak token-exchange errors
#   ci       latest release-prod run + newest v* tag / pin-landed check
#   smoke    public-URL checks (no ssh) — 200 hub, 403 whoami (OPA deny = healthy)

set -uo pipefail   # NOT -e: a failed signal must not abort the whole report

# Shared ops config + read-only invocation prefixes (FM_CP_HOST / FM_KUBECONFIG
# from the gitignored ops env; SSH/K/FLUX). See .claude/skills/_lib/common.sh.
. "$(cd "$(dirname "$0")/../_lib" && pwd)/common.sh"

URL=https://x9bc433.win
OVERLAY=deploy/apps/overlays/prod/kustomization.yaml
# Backends that carry an app container named after the service (for log scans) —
# the subset of FM_SERVICES with a scannable app container (frontends have none).
BACKENDS="search leads mail mcp jobs agents knowledge"

cd "$(git rev-parse --show-toplevel)" || exit 1
OK="✅"; WARN="⚠️"; BAD="❌"

pods() {
  echo "== pods / rollouts (prod + identity) =="
  $SSH "
    echo '--- prod deployments (READY/WANT) ---'
    $K -n prod get deploy -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,WANT:.spec.replicas' 2>&1
    echo '--- prod statefulsets ---'
    $K -n prod get statefulset -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,WANT:.spec.replicas' 2>&1
    echo '--- CNPG database pods ---'
    $K -n prod get pods -l cnpg.io/podRole=instance -o custom-columns='NAME:.metadata.name,READY:.status.containerStatuses[0].ready,STATUS:.status.phase' 2>&1
    echo '--- identity (Keycloak) ---'
    $K -n identity get pods 2>&1
    echo '--- any prod pod NOT Running/Completed (excl. mesh-init) ---'
    $K -n prod get pods --no-headers 2>&1 | awk '\$3!=\"Running\" && \$3!=\"Completed\"{print}' || true
  " || echo "$BAD cluster unreachable — try 'check.sh smoke' for public-only signal"
}

drift() {
  echo "== image / version drift =="
  git fetch -q origin main 2>/dev/null
  local pinned head
  pinned=$(grep -m1 'newTag:' "$OVERLAY" | sed 's/.*newTag: *//')
  head=$(git rev-parse --short origin/main)
  echo "pinned in $OVERLAY : $pinned"
  echo "origin/main HEAD          : $head  $(git log -1 --format='%s' origin/main)"

  # 1. Running images == pinned? (mid-roll or manual edit otherwise.)
  local running
  running=$($SSH "$K -n prod get deploy -o jsonpath='{range .items[*]}{.metadata.name}={.spec.template.spec.containers[0].image}{\"\n\"}{end}'" 2>/dev/null)
  local mismatch=0
  while IFS= read -r line; do
    [ -z "$line" ] && continue
    local name=${line%%=*}
    local img=${line#*=}
    # Canary workloads run their OWN `canary-*` tag (pinned separately by
    # build-canary.yml), never the stable `sha-*` pin — so don't compare them to
    # $pinned. But DO still check them: extract this workload's canary pin from
    # the overlay (the `newTag:` of the images entry whose `name:` ends in the
    # workload name) and compare the RUNNING tag to it. Match = OK; mismatch =
    # WARN (Flux stuck / stale tag / out-of-band swap).
    case "$name" in
      *-canary)
        local cpin ctag
        cpin=$(awk -v w="$name" '
          /^[[:space:]]*- name:/ { inblock = ($0 ~ (w "$")) ? 1 : 0 }
          inblock && /^[[:space:]]*newTag:/ { print $2; exit }
        ' "$OVERLAY")
        ctag=${img##*:}
        if [ -z "$cpin" ]; then
          echo "$WARN $name: no canary pin found in $OVERLAY (running $ctag)"
        elif [ "$ctag" = "$cpin" ]; then
          echo "$OK $name (canary): running == pin ($cpin)"
        else
          echo "$WARN $name canary running != pin: running $ctag, pinned $cpin"
        fi
        continue ;;
    esac
    case "$img" in
      *":$pinned") ;;                       # matches the overlay pin
      *) echo "$WARN running != pin: $line"; mismatch=1 ;;
    esac
  done <<< "$running"
  [ "$mismatch" -eq 0 ] && echo "$OK all prod deploys run the pinned sha ($pinned)"

  # 2. Deploy lag: code commits on origin/main past the pinned build (the pin
  #    commit itself is a [skip ci] no-op and doesn't count).
  local shortpin=${pinned#sha-}
  if git cat-file -e "$shortpin" 2>/dev/null; then
    local lag
    lag=$(git log --oneline "$shortpin"..origin/main 2>/dev/null | grep -vc 'ci(prod): pin')
    if [ "$lag" -eq 0 ]; then
      echo "$OK no deploy lag — pinned build is origin/main tip (only the pin commit sits above it)"
    else
      echo "$WARN deploy lag: $lag code commit(s) merged to main but not released:"
      git log --oneline "$shortpin"..origin/main 2>/dev/null | grep -v 'ci(prod): pin' | sed 's/^/    /'
    fi
  else
    echo "$WARN pinned sha $shortpin not in local history (fetch may be shallow) — skipping lag check"
  fi
}

flux_status() {
  echo "== Flux (GitOps reconcile) =="
  $SSH "$FLUX get kustomization apps-prod 2>&1" \
    || echo "$BAD flux unreachable"
}

logs_scan() {
  echo "== recent errors (bounded tails) =="
  # Match on structured level (error/critical) or a raw traceback — NOT the bare
  # substring "error", which hits the info-level "uvicorn.error" logger name on
  # every startup line and cries wolf.
  for svc in $BACKENDS; do
    local hits
    hits=$($SSH "$K -n prod logs deploy/$svc --tail=300 -c $svc 2>/dev/null | grep -iE '\"level\": \"(error|critical)\"|Traceback \(most recent' | tail -3" 2>/dev/null)
    if [ -n "$hits" ]; then
      echo "$WARN $svc:"; echo "$hits" | sed 's/^/    /'
    else
      echo "$OK $svc: clean (last 300 lines, error/critical level)"
    fi
  done
  echo "--- Keycloak TOKEN_EXCHANGE_ERROR (last 500 lines) ---"
  # A few are normal: an expired subject token mid-detached-job triggers a
  # downgrade and logs one. A sustained surge from one client is the real signal.
  local kc
  kc=$($SSH "$K -n identity logs deploy/keycloak --tail=500 2>/dev/null | grep -c TOKEN_EXCHANGE_ERROR" 2>/dev/null)
  echo "TOKEN_EXCHANGE_ERROR count: ${kc:-?} (a handful is normal; a surge from one client is not)"
}

ci() {
  echo "== CI/CD (release-prod) =="
  gh run list --workflow=release-prod --limit 3 2>&1 | sed 's/^/  /'
  local tag
  tag=$(git tag -l 'v*' --sort=-v:refname | head -1)
  echo "newest v* tag: $tag"
  git fetch -q origin main 2>/dev/null
  # Did that tag's release land a pin on main? Look for a pin commit at/after it.
  if git merge-base --is-ancestor "$tag" origin/main 2>/dev/null; then
    if git log --oneline "$tag"..origin/main 2>/dev/null | grep -q 'ci(prod): pin'; then
      echo "$OK pin commit for $tag (or later) is on origin/main"
    elif [ "$(git rev-parse "$tag^{commit}" 2>/dev/null)" = "$(git rev-parse origin/main 2>/dev/null)" ]; then
      echo "$WARN $tag == origin/main tip with no pin above it — CI pin may still be in flight"
    else
      echo "$OK $tag is an ancestor of origin/main"
    fi
  else
    echo "$WARN $tag not yet on origin/main — release may be building"
  fi
}

smoke() {
  echo "== public smoke ($URL) — no ssh =="
  local c
  c=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' "$URL/" 2>/dev/null)
  [ "$c" = 200 ] && echo "$OK GET /            -> 200" || echo "$BAD GET /            -> $c (want 200)"
  curl -sS -m 10 "$URL/" 2>/dev/null | grep -o '<title>[^<]*</title>' | sed 's/^/    /' || echo "    (no <title>)"
  # OPA default-deny answers before auth: unauthenticated whoami is 403, NOT 401.
  # 403 is the HEALTHY signal — a 200 here would mean auth is broken/open.
  for p in search mail agents; do
    c=$(curl -sS -m 10 -o /dev/null -w '%{http_code}' "$URL/api/$p/whoami" 2>/dev/null)
    [ "$c" = 403 ] && echo "$OK $p whoami unauth -> 403 (OPA deny = healthy)" \
                   || echo "$BAD $p whoami unauth -> $c (want 403)"
  done
}

case "${1:-all}" in
  pods)  pods ;;
  drift) drift ;;
  flux)  flux_status ;;
  logs)  logs_scan ;;
  ci)    ci ;;
  smoke) smoke ;;
  all)
    echo "############ funnelmanager PROD health — $(date -u '+%Y-%m-%dT%H:%M:%SZ') ############"
    echo; smoke
    echo; pods
    echo; drift
    echo; flux_status
    echo; ci
    echo; logs_scan
    echo; echo "############ end — read-only; nothing was changed ############"
    ;;
  *) sed -n '15,22p' "$0"; exit 1 ;;   # print the Usage block (lines 15–22)
esac
