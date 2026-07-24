#!/usr/bin/env bash
# funnelmanager deploy driver — GitOps release to the k3s cluster.
#
# A prod deploy is: tag v* on main -> release-prod workflow builds the six
# images to GHCR and commits a sha-pin to main -> Flux (on usfr4) reconciles
# the pin -> prod namespace rolls out. This script wraps that loop plus the
# verification that the 2026-07-23 v1.2.0 release actually used.
#
# Usage: .claude/skills/deploy-funnelmanager/deploy.sh <cmd> [args]
#   status              local git state, recent CI runs, cluster state (read-only)
#   release vX.Y.Z      full prod release: preflight, tag, watch CI, reconcile, verify
#   rollback <ref>      re-release an older tag/sha via workflow_dispatch
#   dev [ref]           deploy a ref to the dev overlay (default: main)
#   watch               watch the latest release-prod run, then reconcile + verify
#   reconcile           force Flux to pick up the latest pin (skips the ~1m poll)
#   rollout             wait for prod deployments to finish rolling; print pinned image
#   smoke               public-URL checks (no ssh needed)
#   purge <url...>      purge Cloudflare edge cache for specific asset URLs

set -euo pipefail

CP=usfr4                              # k3s control plane; flux/kubectl live here
URL=https://x9bc433.win
SERVICES="frontend mailui search mail leads"
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10 $CP"
# Plain `flux`/`kubectl` on the box fail with "dial tcp [::1]:8080: connection
# refused" — root's kubeconfig must be passed explicitly.
FLUX="sudo -n env KUBECONFIG=/etc/rancher/k3s/k3s.yaml flux"
K="sudo -n kubectl"

cd "$(git rev-parse --show-toplevel)"
die() { echo "error: $*" >&2; exit 1; }

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
  $SSH "for d in $SERVICES; do $K -n prod rollout status deploy/\$d --timeout=300s 2>&1 | tail -1; done
        echo -n 'frontend image: '; $K -n prod get deploy frontend -o jsonpath='{.spec.template.spec.containers[0].image}'; echo"
}

smoke() {
  echo "--- public smoke ($URL) ---"
  curl -sS -m 10 -o /dev/null -w "GET /            -> %{http_code} (want 200)\n" "$URL/"
  curl -sS -m 10 "$URL/" | grep -o "<title>[^<]*</title>" || echo "(no <title>)"
  # Unauthenticated whoami is denied by OPA with 403 (not 401) — that's healthy.
  curl -sS -m 10 -o /dev/null -w "whoami unauth   -> %{http_code} (want 403)\n" "$URL/api/search/whoami"
  curl -sS -m 10 -o /dev/null -w "mailui whoami   -> %{http_code} (want 403)\n" "$URL/api/mail/whoami"
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
  smoke
}

cmd=${1:-status}
case "$cmd" in
  status)
    echo "--- local git ---"
    git fetch -q origin main
    git status -sb | head -2
    git log origin/main --oneline -3
    echo "--- recent release-prod runs ---"
    gh run list --workflow=release-prod --limit 3
    echo "--- flux ($CP) ---"
    $SSH "$FLUX get kustomizations 2>&1 | head -15; $K -n prod get deploy" \
      || echo "(cluster unreachable — public smoke still available: deploy.sh smoke)"
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
  dev)
    ref=${2:-main}
    gh workflow run deploy-dev -f ref="$ref"
    echo "dispatched deploy-dev for $ref. Note: apps-dev ships suspended —"
    echo "  $SSH \"$FLUX resume kustomization apps-dev\"   # to actually serve it"
    ;;
  watch)     watch_run ;;
  reconcile) reconcile; rollout ;;
  rollout)   rollout ;;
  smoke)     smoke ;;
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
  *) sed -n '2,19p' "$0"; exit 1 ;;
esac
