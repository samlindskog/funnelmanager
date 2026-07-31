#!/usr/bin/env bash
# funnelmanager deploy driver — GitOps release to the k3s cluster.
#
# A prod deploy is: tag v* on main -> release-prod workflow builds the images to
# GHCR and commits a sha-pin to main -> Flux (on usfr4) reconciles the pin ->
# prod namespace rolls out. This script wraps that release loop. All health /
# verification (smoke, drift, flux, ci) lives in the prod-health skill; release
# verification and `status` delegate to it rather than re-implementing curl/flux
# checks — prod-health is the single source of truth for "is prod healthy".
#
# Usage: .claude/skills/deploy-funnelmanager/deploy.sh <cmd> [args]
#   status              local git state + prod-health drift/flux/ci (read-only)
#   release vX.Y.Z      full prod release: preflight, tag, watch CI, reconcile, verify
#   rollback <ref>      re-release an older tag/sha via workflow_dispatch
#   watch               watch the latest release-prod run, then reconcile + verify
#   reconcile           force Flux to pick up the latest pin (skips the ~1m poll)
#   rollout             wait for prod deployments to finish rolling
#   smoke               public-URL checks (delegates to prod-health smoke)
#   purge <url...>      purge Cloudflare edge cache for specific asset URLs

set -euo pipefail

# Shared ops config + invocation prefixes (FM_CP_HOST / FM_KUBECONFIG /
# FM_CF_ZONE_ID from the gitignored ops env, plus SSH/K/FLUX and the FM_SERVICES
# rollout-wait list — the ten deployed 1-replica Deployments). See
# .claude/skills/_lib/common.sh + ops-env.example.
. "$(cd "$(dirname "$0")/../_lib" && pwd)/common.sh"

cd "$(git rev-parse --show-toplevel)"
die() { echo "error: $*" >&2; exit 1; }

# prod-health is the verifier. Release verification and `status` shell out to it
# instead of duplicating smoke/drift/flux/ci logic. `|| true` so a ⚠️/❌ health
# line never aborts an in-progress release (the human reads the printed verdict).
HEALTH=.claude/skills/prod-health/check.sh
health() { [ -f "$HEALTH" ] || die "prod-health skill missing at $HEALTH"; bash "$HEALTH" "$@" || true; }

preflight() {
  [ "$(git rev-parse --abbrev-ref HEAD)" = main ] || die "not on main"
  [ -z "$(git status --porcelain)" ] || die "working tree not clean — commit (or stash) first"
  git fetch -q origin main
  [ "$(git rev-parse HEAD)" = "$(git rev-parse origin/main)" ] \
    || die "local main != origin/main. If behind: probably the CI pin commit — 'git pull'. If ahead: 'git push' first."
}

reconcile() {
  echo "--- forcing Flux reconcile on $FM_CP_HOST (otherwise it polls within ~1m) ---"
  $SSH "$FLUX reconcile source git flux-system -n flux-system 2>&1 | tail -2
        $FLUX reconcile kustomization flux-system -n flux-system 2>&1 | tail -2
        $FLUX reconcile kustomization apps-prod 2>&1 | tail -2"
  # A release/reconcile forces ONLY apps-prod. The infra Kustomizations
  # (infra-istio, infra-mesh-policies, infra-gateway, infra-identity,
  # infra-observability, infra-opa) are SEPARATE and land on Flux's own ~1m poll —
  # they are NOT forced here. When an infra change must apply NOW, name it/them:
  #   deploy.sh reconcile infra-gateway infra-mesh-policies
  if [ "$#" -gt 0 ]; then
    echo "--- also forcing infra kustomization(s): $* ---"
    for kust in "$@"; do
      $SSH "$FLUX reconcile kustomization $kust 2>&1 | tail -2"
    done
  fi
}

rollout() {
  echo "--- waiting for prod rollout ---"
  $SSH "for d in $FM_SERVICES; do $K -n prod rollout status deploy/\$d --timeout=300s 2>&1 | tail -1; done"
}

# Post-rollout verification — delegated to prod-health (running-sha-vs-pin drift
# + public smoke). This is the same verifier ship-branch runs in its step 5, so
# a release and a manual health check give the same verdict.
verify() {
  echo "--- verifying release (via prod-health) ---"
  health drift
  health smoke
}

watch_run() {
  sleep 5   # give GH a beat to register the run
  local run_id
  run_id=$(gh run list --workflow=release-prod --limit 1 --json databaseId --jq '.[0].databaseId')
  echo "--- watching release-prod run $run_id ---"
  gh run watch "$run_id" --exit-status --interval 15 >/dev/null \
    || die "run $run_id failed — inspect with: gh run view $run_id --log-failed"
  echo "--- CI done; pulling the pin commit it pushed to main ---"
  git pull -q origin main
  git log -1 --oneline
  reconcile
  rollout
  verify
}

cmd=${1:-status}
case "$cmd" in
  status)
    echo "--- local git ---"
    git fetch -q origin main
    git status -sb | head -2
    git log origin/main --oneline -3
    echo "--- prod cluster + CI (via prod-health) ---"
    health drift
    health flux
    health ci
    ;;
  release)
    tag=${2:-}; [[ "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "usage: deploy.sh release vX.Y.Z"
    git rev-parse -q --verify "refs/tags/$tag" >/dev/null && die "tag $tag already exists"
    preflight
    echo "--- releasing $(git rev-parse --short HEAD) as $tag ---"
    git tag "$tag" && git push origin "$tag"
    watch_run
    ;;
  rollback)
    ref=${2:-}; [ -n "$ref" ] || die "usage: deploy.sh rollback <tag|sha|branch>"
    echo "--- dispatching release-prod for $ref ---"
    gh workflow run release-prod -f ref="$ref"
    watch_run
    ;;
  watch)     watch_run ;;
  reconcile) shift; reconcile "$@"; rollout ;;   # extra args = infra kustomizations to also force
  rollout)   rollout ;;
  smoke)     health smoke ;;
  purge)
    shift; [ $# -gt 0 ] || die "usage: deploy.sh purge <full-url> [...]"
    [ -n "$FM_CF_ZONE_ID" ] || die "FM_CF_ZONE_ID is unset — set it in the ops env (\$FM_OPS_ENV, default ~/.config/fm-ops/env; see .claude/skills/_lib/ops-env.example)"
    files=$(printf '"%s",' "$@"); files="[${files%,}]"
    # Token lives in the cluster; it is purge/DNS-scoped and rejects requests
    # arriving over IPv6, hence curl -4. Zone lookup may also fail auth — the
    # zone id is supplied via the gitignored ops env (FM_CF_ZONE_ID) and used
    # directly rather than looked up.
    $SSH "TOKEN=\$($K -n cert-manager get secret cloudflare-api-token -o jsonpath='{.data.api-token}' | base64 -d)
          curl -4 -sS -m 10 -X POST -H \"Authorization: Bearer \$TOKEN\" -H 'Content-Type: application/json' \
            'https://api.cloudflare.com/client/v4/zones/$FM_CF_ZONE_ID/purge_cache' \
            --data '{\"files\":$files}' | python3 -m json.tool"
    ;;
  *) sed -n '11,19p' "$0"; exit 1 ;;
esac
