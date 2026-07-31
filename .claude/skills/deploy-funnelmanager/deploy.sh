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

CP=usfr4                              # k3s control plane; flux/kubectl live here
# Every prod Deployment we wait on during a release (matches prod-health's
# authoritative workload list: backends + frontends, all 1-replica Deployments).
SERVICES="frontend searchui mailui agentsui search leads mail mcp jobs agents"
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10 $CP"
# Plain `flux`/`kubectl` on the box fail with "dial tcp [::1]:8080: connection
# refused" — root's kubeconfig must be passed explicitly.
FLUX="sudo -n env KUBECONFIG=/etc/rancher/k3s/k3s.yaml flux"
K="sudo -n kubectl"

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
  echo "--- forcing Flux reconcile on $CP (otherwise it polls within ~1m) ---"
  $SSH "$FLUX reconcile source git flux-system -n flux-system 2>&1 | tail -2
        $FLUX reconcile kustomization flux-system -n flux-system 2>&1 | tail -2
        $FLUX reconcile kustomization apps-prod 2>&1 | tail -2"
}

rollout() {
  echo "--- waiting for prod rollout ---"
  $SSH "for d in $SERVICES; do $K -n prod rollout status deploy/\$d --timeout=300s 2>&1 | tail -1; done"
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
  reconcile) reconcile; rollout ;;
  rollout)   rollout ;;
  smoke)     health smoke ;;
  purge)
    shift; [ $# -gt 0 ] || die "usage: deploy.sh purge <full-url> [...]"
    files=$(printf '"%s",' "$@"); files="[${files%,}]"
    # Token lives in the cluster; it is purge/DNS-scoped and rejects requests
    # arriving over IPv6, hence curl -4. Zone lookup may also fail auth — the
    # known zone id for x9bc433.win is used directly.
    $SSH "TOKEN=\$($K -n cert-manager get secret cloudflare-api-token -o jsonpath='{.data.api-token}' | base64 -d)
          curl -4 -sS -m 10 -X POST -H \"Authorization: Bearer \$TOKEN\" -H 'Content-Type: application/json' \
            'https://api.cloudflare.com/client/v4/zones/8303098fbf6f966ac44b6f2945e9d732/purge_cache' \
            --data '{\"files\":$files}' | python3 -m json.tool"
    ;;
  *) sed -n '11,19p' "$0"; exit 1 ;;
esac
