#!/usr/bin/env bash
# funnelmanager canary driver — operate the full-stack header-routed canary.
#
# A canary is a SEPARATE `<svc>-canary` Deployment beside stable `<svc>`, built
# from an arbitrary feature ref as `<svc>:canary-<sha>`, reachable ONLY by a
# host-only `fm_debug=<secret>` session cookie PLUS the `fm_route=canary` selector
# on x9bc433.win (debug-session-gate EnvoyFilter -> x-fm-canary header -> app-prod
# HTTPRoute). Activation = the
# build-canary.yml workflow pins the tag + flips replicas 0->1 on main; Flux
# reconciles. Retire = replicas back to 0 (ephemeral-canary pattern).
#
# This script only ACTS on canary lifecycle. Everything else is DELEGATED:
#   * flux reconcile + rollout-wait + cluster reads  -> deploy-funnelmanager (deploy.sh)
#   * post-activation health/drift/smoke verdict      -> prod-health (check.sh)
#   * traffic / idle queries (Grafana MCP)            -> observe-grafana (agent-side)
#   * exercising a live canary                        -> drive-canary (agent-side)
# It never re-implements flux/kubectl/curl/PromQL that a sibling owns.
#
# Usage: .claude/skills/canary/canary.sh <cmd> [args]
#   deploy <svc> <ref> [--confirm-backend]   build+activate a canary of <svc> from <ref>
#   retire <svc> [--force]                    scale an idle canary to 0 (GitOps commit)
#   list | status                             show every <svc>-canary: replicas + pinned tag
#
# Prereqs: gh (authed), ssh alias usfr4, sibling skills deploy-funnelmanager +
# prod-health present. A canary must already be ARMED (its base manifests +
# overlay images entry on main) — this script never scaffolds one.

set -uo pipefail   # NOT -e: a failed remote read must not abort list/status

# Shared ops config + invocation prefixes (FM_CP_HOST from the gitignored ops
# env; SSH/K/FLUX). See .claude/skills/_lib/common.sh.
. "$(cd "$(dirname "$0")/../_lib" && pwd)/common.sh"

BASE=deploy/apps/base
OVERLAY=deploy/apps/overlays/prod/kustomization.yaml
GW_KUSTOMIZATION=deploy/infrastructure/gateway/kustomization.yaml
GW_CANARY_DIR=deploy/infrastructure/gateway/canary
# EAST-WEST (internal) canaries are steered in-mesh by an Istio VirtualService,
# not a gateway HTTPRoute — the in-mesh analogue of the gateway canary routes.
EW_KUSTOMIZATION=deploy/infrastructure/mesh-policies/kustomization.yaml
EW_CANARY_DIR=deploy/infrastructure/mesh-policies/canary
# The debug-session (fm_debug) secret is NO LONGER in git: it lives only in the
# `fm-canary-token` Secret (istio-ingress) and is injected at the gateway by the
# debug-session-gate EnvoyFilter. ALL canary routes/VS now PRESENCE-match
# `x-fm-canary` (regex .+) and must carry NO secret-looking literal — so there is
# no secret to derive here.
DEPLOY=.claude/skills/deploy-funnelmanager/deploy.sh
HEALTH=.claude/skills/prod-health/check.sh

cd "$(git rev-parse --show-toplevel)" || exit 1
OK="✅"; WARN="⚠️"; BAD="❌"
die() { echo "error: $*" >&2; exit 1; }

# --- service taxonomy -------------------------------------------------------
# SPA canaries are static ingress targets (no egress, no exchange identity);
# BACKEND canaries run under the stable service's prod SA against real prod
# datastores — hence the extra --confirm-backend gate in `deploy`.
svc_class() {
  case "$1" in
    frontend|searchui|mailui|agentsui) echo spa ;;
    search|leads|mail|mcp|jobs|agents) echo backend ;;
    *) echo unknown ;;
  esac
}

# ROUTING taxonomy — ORTHOGONAL to svc_class (spa/backend). It decides HOW the
# canary marker is steered, and therefore which toggled "route" artifact + which
# kustomization the tooling operates on:
#   * gateway  — edge-reachable services: a cookie-gated gateway HTTPRoute under
#                deploy/infrastructure/gateway/canary/<svc>-canary.yaml that
#                Exact-matches the secret x-fm-canary header.
#   * eastwest — INTERNAL-only services (never through the ingress gateway):
#                an in-mesh Istio VirtualService under
#                deploy/infrastructure/mesh-policies/canary/<svc>-canary-eastwest.yaml
#                that PRESENCE-matches x-fm-canary (regex .+, NEVER the secret).
routing_class() {
  case "$1" in
    frontend|searchui|mailui|agentsui|search|mail|agents) echo gateway ;;
    leads|mcp|jobs) echo eastwest ;;
    *) echo unknown ;;
  esac
}

# The toggled canary route/VS file for a service (single source of the path,
# used by armed() and retire()). Class-aware: gateway HTTPRoute vs east-west VS.
canary_route_file() {
  case "$(routing_class "$1")" in
    gateway)  echo "$GW_CANARY_DIR/${1}-canary.yaml" ;;
    eastwest) echo "$EW_CANARY_DIR/${1}-canary-eastwest.yaml" ;;
    *) return 1 ;;
  esac
}

# The kustomization whose resources: list is the ON/OFF toggle for this service's
# canary route (presence == active).
canary_kustomization() {
  case "$(routing_class "$1")" in
    gateway)  echo "$GW_KUSTOMIZATION" ;;
    eastwest) echo "$EW_KUSTOMIZATION" ;;
    *) return 1 ;;
  esac
}

# The resource entry (path relative to the kustomization dir) that is added on
# activate / removed on retire.
canary_route_entry() {
  case "$(routing_class "$1")" in
    gateway)  echo "canary/${1}-canary.yaml" ;;
    eastwest) echo "canary/${1}-canary-eastwest.yaml" ;;
    *) return 1 ;;
  esac
}

# True iff the (UNCOMMENTED) resource line for this service's canary route is
# listed in its kustomization. Selects only real list items (`^  - `) FIRST, so a
# commented example/note line (e.g. the east-west kustomization's documentation)
# is never mistaken for an active toggle — presence here == canary active.
route_listed() {
  local svc=$1 kust entry
  kust=$(canary_kustomization "$svc") || return 2
  entry=$(canary_route_entry "$svc")  || return 2
  grep -E '^[[:space:]]*- ' "$kust" | grep -qF -- "- ${entry}"
}

# True iff an east-west VS's `gateways:` list is EXACTLY [mesh] — no ingress /
# `fm-gateway` entry. A stray edge gateway in that list would attach this
# INTERNAL-only route to the ingress, exposing the internal service at the edge,
# so this is a fail-closed guard (block- and flow-style [mesh] both handled; any
# count other than a single `mesh` entry fails).
ew_gateways_only_mesh() {
  awk '
    /^[[:space:]]*gateways:[[:space:]]*\[/ {
      l=$0; sub(/.*\[/,"",l); sub(/\].*/,"",l); gsub(/[[:space:]"]/,"",l);
      n=split(l,a,","); for(i=1;i<=n;i++) if(a[i]!=""){t++; if(a[i]=="mesh")m++}
      next
    }
    /^[[:space:]]*gateways:[[:space:]]*$/ {g=1; next}
    g && /^[[:space:]]*#/ {next}
    g && /^[[:space:]]*-/ {
      v=$0; sub(/^[[:space:]]*-[[:space:]]*/,"",v); sub(/[[:space:]]*#.*/,"",v); gsub(/[[:space:]"]/,"",v);
      t++; if(v=="mesh")m++; next
    }
    g && /^[[:space:]]*$/ {next}
    g {g=0}
    END{ exit (t==1 && m==1)?0:1 }
  ' "$1"
}

# Armed = ALL legs of the canary contract present on main (mirrors the
# build-canary.yml preflight guard): the base Deployment + the overlay images
# entry + the toggled route/VS, and that artifact must actually GATE correctly on
# the `x-fm-canary` marker. Fail closed if any leg is missing or the gate is
# wrong. The route/VS leg is what makes "canary-if-exists-else-stable" work.
#
# Correctness (now UNIFORM across classes): the route/VS must PRESENCE-match
# x-fm-canary (regex .+) and must carry NO secret-looking literal — the secret
# lives only in the fm-canary-token Secret and is injected at the gateway, so
# neither a gateway HTTPRoute nor a mesh VS may contain it. The marker is
# non-forgeable at the edge (the gateway strips any client-supplied header and
# re-injects the secret only for a valid cookie), so presence is sufficient.
#   * gateway  — Gateway API HTTPRoute: type RegularExpression, value ".+".
#   * eastwest — Istio VS: header regex ".+" on gateways EXACTLY [mesh] (no
#     ingress/fm-gateway leak). Both fail closed if a 32-hex secret literal shows.
armed() {
  local svc=$1 rf rc; rc=$(routing_class "$svc")
  [ "$rc" = unknown ] && return 1
  rf=$(canary_route_file "$svc") || return 1
  [ -f "$BASE/${svc}-canary/deployment.yaml" ] || return 1
  grep -q "funnelmanager/${svc}-canary" "$OVERLAY" || return 1
  [ -f "$rf" ] || return 1
  grep -q 'x-fm-canary' "$rf" || return 1
  # A secret-looking 32-hex literal must NEVER appear in a route/VS anymore.
  grep -qE '[0-9a-fA-F]{32}' "$rf" && return 1
  if [ "$rc" = gateway ]; then
    grep -Eq 'type:[[:space:]]*RegularExpression' "$rf" || return 1
    grep -Eq 'value:[[:space:]]*"?\.\+' "$rf" || return 1
  else
    # east-west VS: presence regex, gateways EXACTLY [mesh].
    grep -Eq 'regex:[[:space:]]*"?\.\+' "$rf" || return 1
    ew_gateways_only_mesh "$rf" || return 1   # no ingress/fm-gateway leak
  fi
  return 0
}

# The pinned canary-<sha> tag for a workload, read from the overlay images
# block whose name ends in `<svc>-canary` (awk, not yq — the overlay's comments
# must survive; same block-scoped parse build-canary.yml uses).
canary_tag() {
  awk -v w="$1-canary" '
    /^[[:space:]]*- name:/ { inblock = ($0 ~ ("funnelmanager/" w "$")) ? 1 : 0 }
    inblock && /^[[:space:]]*newTag:/ { print $2; exit }
  ' "$OVERLAY"
}

# Drift-check: diff <svc>-canary against stable <svc> and flag any changed line
# NOT explained by a known, intentional canary delta. We deliberately keep the
# canary a FULL COPY of stable (not a patch-over-base), so this catches the
# env-copy drift trap — a stale env var / probe / resource that diverged from
# stable. Heuristic (documented as such): a +/- line whose trimmed content
# matches none of the known-delta keywords is surfaced for human review.
drift_check() {
  local svc=$1
  local a="$BASE/${svc}/deployment.yaml" b="$BASE/${svc}-canary/deployment.yaml"
  [ -f "$a" ] || { echo "$WARN drift-check skipped: no stable manifest $a"; return; }
  echo "--- drift-check: ${svc}-canary vs stable ${svc} (known deltas ignored) ---"
  # Compare EFFECTIVE config only: strip comments + blank lines from both (the
  # canary manifest carries large explanatory headers that are prose, not drift).
  local strip='grep -vE "^[[:space:]]*(#|$)"'
  local diff_out unexplained
  diff_out=$(diff -u <(eval "$strip" "$a") <(eval "$strip" "$b") 2>/dev/null)
  # Known-benign canary deltas (case-insensitive substring on the changed line).
  # The nodeAffinity block (operator/values/list items) rides the affinity delta.
  local keep='name|app.kubernetes.io|version:|replicas|priorityclassname|affinity|nodeaffinity|preferred|weight|preference|matchexpressions|operator:|values:|- worker|hostname|worker|image:|fm_deployment_variant|canary|selector|matchlabels'
  unexplained=$(printf '%s\n' "$diff_out" \
    | grep -E '^[+-]' | grep -Ev '^(\+\+\+|---)' \
    | sed -E 's/^[+-][[:space:]]*//' \
    | grep -viE "$keep" \
    | grep -vE '^[[:space:]]*$')
  if [ -n "$unexplained" ]; then
    echo "$WARN ${svc}-canary diverges from stable OUTSIDE the known canary deltas — REVIEW:"
    printf '%s\n' "$unexplained" | sed 's/^/    /'
  else
    echo "$OK ${svc}-canary differs from stable only in the expected canary deltas"
  fi
}

# Reconcile + verify are DELEGATED to the sibling skills (single source of truth
# for flux/rollout and for the health verdict — no duplicate logic here).
reconcile() {
  [ -f "$DEPLOY" ] || die "deploy-funnelmanager skill missing at $DEPLOY"
  echo "--- flux reconcile (delegated to deploy-funnelmanager) ---"
  bash "$DEPLOY" reconcile || true
}
verify() {
  [ -f "$HEALTH" ] || die "prod-health skill missing at $HEALTH"
  echo "--- verifying (delegated to prod-health: canary drift + smoke) ---"
  bash "$HEALTH" drift || true   # prod-health drift checks *-canary running==pin
  bash "$HEALTH" smoke || true
}

watch_build() {
  sleep 5
  local id
  id=$(gh run list --workflow=build-canary.yml --limit 1 --json databaseId --jq '.[0].databaseId' 2>/dev/null)
  [ -n "$id" ] || die "could not find the build-canary run — check 'gh run list --workflow=build-canary.yml'"
  echo "--- watching build-canary run $id (builds, pins tag, flips replicas 0->1, commits to main) ---"
  gh run watch "$id" --exit-status --interval 15 >/dev/null \
    || die "build-canary run $id failed — inspect: gh run view $id --log-failed"
  echo "--- CI done; pulling the pin+activate commit it pushed to main ---"
  git pull -q origin main
  git log -1 --oneline
}

# The idle/traffic query is OWNED by observe-grafana (Grafana MCP). A pure shell
# driver can't call an MCP tool, so it cannot itself PROVE a canary is idle —
# tearing down a workload we can't prove idle is unsafe. Therefore `retire`
# fails safe: it prints the exact query for the agent to run via observe-grafana
# and REFUSES unless --force (which asserts "idle confirmed, or overriding").
idle_gate() {
  local svc=$1 force=$2
  echo "--- idle-check for ${svc}-canary ---"
  echo "Run this via observe-grafana (Grafana MCP) over the last ~30m — expect NO rows/samples:"
  # The LOKI query is the authoritative idle-proof: the per-pod FM_DEPLOYMENT_VARIANT
  # already stamps `variant` on every canary log line today, so an empty result here
  # genuinely means no canary traffic.
  echo "    LogQL : {service_name=\"$svc\"} | json | variant=\"canary\"   (authoritative idle-proof — works today)"
  # The PromQL `variant` label on fm_http_* is being ADDED in a parallel workstream;
  # until those images redeploy the selector may match NOTHING (an empty result is
  # then NOT proof of idle). Keep it — it starts working post-release — but rely on
  # the Loki query above for the actual idle decision.
  echo "    PromQL: sum(rate(fm_http_requests_total{service=\"$svc\",variant=\"canary\"}[30m]))   (may be EMPTY until the fm_http_* variant label ships — not idle-proof yet)"
  if [ "$force" = 1 ]; then
    echo "$WARN --force set: skipping idle proof, scaling ${svc}-canary to 0 anyway."
    return 0
  fi
  die "cannot prove ${svc}-canary is idle from the shell. Confirm idle via observe-grafana, then re-run: canary.sh retire $svc --force"
}

# --- verbs ------------------------------------------------------------------
cmd=${1:-status}; shift || true

# Collect positionals (svc, ref) and flags.
POS=(); CONFIRM_BACKEND=0; FORCE=0
for a in "$@"; do
  case "$a" in
    --confirm-backend) CONFIRM_BACKEND=1 ;;
    --force) FORCE=1 ;;
    -*) die "unknown flag: $a" ;;
    *) POS+=("$a") ;;
  esac
done

case "$cmd" in
  deploy)
    svc=${POS[0]:-}; ref=${POS[1]:-}
    [ -n "$svc" ] && [ -n "$ref" ] || die "usage: canary.sh deploy <service> <ref> [--confirm-backend]"
    class=$(svc_class "$svc")
    [ "$class" = unknown ] && die "unknown service '$svc' (deployable services only; not 'backup')"
    rc=$(routing_class "$svc")

    # 1. REQUIRE armed — never auto-scaffold (arming a canary is a reviewed
    #    trust decision: feature-branch code under stable's prod identity).
    if ! armed "$svc"; then
      rf=$(canary_route_file "$svc")
      if [ "$rc" = eastwest ]; then
        tmpl="leads-canary (INTERNAL, east-west VirtualService)"
        gate_note="the east-west VirtualService must PRESENCE-match x-fm-canary (regex .+) on gateways:[mesh] and MUST NOT contain the secret token"
      else
        tmpl=$([ "$class" = backend ] && echo "search-canary (backend, gateway route)" || echo "frontend-canary (SPA, gateway route)")
        gate_note="the gateway HTTPRoute must Exact-match the x-fm-canary secret header"
      fi
      die "'$svc' is NOT armed (routing_class=$rc). Missing one of: $BASE/${svc}-canary/ manifests, the '${svc}-canary' overlay images entry, or the toggled route/VS $rf — and $gate_note.
       Arming a canary is a deliberate, reviewed trust decision — copy the $tmpl template
       ($BASE/${svc}-canary + $BASE/netpol + the overlay images entry + the ${rf#deploy/} route/VS), get it reviewed, land it on main, THEN retry.
       This driver will not scaffold one."
    fi

    # 2. DRIFT-CHECK (full-copy env drift trap).
    drift_check "$svc"

    # 3. Backend hard prerequisites (SPAs skip — static, egress-less).
    if [ "$class" = backend ]; then
      echo "--- BACKEND canary prerequisites (read before --confirm-backend) ---"
      echo "$WARN (a) SCHEMA COMPATIBILITY: ${svc}-canary shares the REAL prod datastore and runs stable"
      echo "       ${svc}'s startup DDL against it. A destructive/renaming migration on '$ref' will break PROD $svc"
      echo "       for all users. Only activate schema-COMPATIBLE branches (or point the branch at a scratch DB)."
      echo "$WARN (b) ACTIVATION APPROVAL GATE (now EXISTS): build-canary's pin/activate job (replicas 0->1) runs"
      echo "       under the GitHub 'canary-activation' environment, so activation PAUSES for a manual reviewer"
      echo "       approval in the Actions UI before feature-branch backend code goes live under stable $svc's prod"
      echo "       SA/identity. Build+push run unguarded; only ACTIVATION waits. Still only deploy a TRUSTED, reviewed ref."
      [ "$CONFIRM_BACKEND" = 1 ] || die "backend canary requires explicit --confirm-backend after reading the above."
    fi

    # 4. Build + activate via the workflow (it commits the pin+activate to main).
    echo "--- dispatching build-canary: service=$svc ref=$ref ---"
    gh workflow run build-canary.yml -f service="$svc" -f ref="$ref" || die "gh workflow run failed"
    watch_build

    # 5. Reconcile (delegated) + wait the CANARY rollout (deploy.sh's fixed
    #    stable-service list omits <svc>-canary, so wait it here) + verify.
    reconcile
    echo "--- waiting for ${svc}-canary rollout ---"
    $SSH "$K -n prod rollout status deploy/${svc}-canary --timeout=300s 2>&1 | tail -1" \
      || echo "$WARN could not confirm ${svc}-canary rollout via ssh — verify with prod-health drift"
    verify

    echo "--- reach the canary ---"
    if [ "$rc" = eastwest ]; then
      echo "'$svc' is INTERNAL — it has NO gateway route. Reach ${svc}-canary via a marked request that"
      echo "propagates the x-fm-canary marker IN-MESH (fm_runtime carries it across hops). Concretely, for leads:"
      echo "run a cookie-gated canary SEARCH (search-canary must be active) whose search->${svc} hop carries the"
      echo "marker onto ${svc}-canary. Preferred: the enter-canary launcher (reads the token from ~/.config/fm-e2e/creds.env)."
      echo "Manual: server-set both cookies via the gateway (token = the fm-canary-token Secret / creds.env FM_DEBUG_TOKEN):"
      echo "    open https://x9bc433.win/debug/canary/on?t=<token>   # sets fm_debug (HttpOnly) + fm_route=canary"
      echo "Then exercise a search via drive-canary; its internal ${svc} call lands on ${svc}-canary."
    else
      echo "Preferred: the enter-canary launcher (reads the token from ~/.config/fm-e2e/creds.env and hits /debug/canary/on)."
      echo "Manual: server-set both cookies via the gateway (token = the fm-canary-token Secret / creds.env FM_DEBUG_TOKEN):"
      echo "    open https://x9bc433.win/debug/canary/on?t=<token>   # sets fm_debug (HttpOnly) + fm_route=canary"
      echo "Then load the app; requests carrying both cookies route to ${svc}-canary. Exercise it via drive-canary."
    fi
    ;;

  retire)
    svc=${POS[0]:-}
    [ -n "$svc" ] || die "usage: canary.sh retire <service> [--force]"
    [ "$(svc_class "$svc")" = unknown ] && die "unknown service '$svc'"
    armed "$svc" || die "'$svc' is not armed — nothing to retire ($BASE/${svc}-canary missing)"
    dep="$BASE/${svc}-canary/deployment.yaml"
    # Class-aware toggle target: gateway HTTPRoute in the gateway kustomization, or
    # east-west VS in the mesh-policies kustomization.
    kust=$(canary_kustomization "$svc") || die "cannot resolve kustomization for '$svc'"
    route_entry=$(canary_route_entry "$svc")
    # State of BOTH legs. Retire is fully done only when replicas==0 AND the route
    # line is gone; short-circuit only then. If replicas==0 but a stale route line
    # remains (drift / bad merge), we must NOT report "already idle" — that stale
    # zero-endpoint route is exactly the 503 bug — so fall through and self-heal it.
    replicas_active=0; grep -qE '^[[:space:]]*replicas:[[:space:]]*[1-9]' "$dep" && replicas_active=1
    route_present=0;   route_listed "$svc" && route_present=1
    if [ "$replicas_active" = 0 ] && [ "$route_present" = 0 ]; then
      echo "$OK ${svc}-canary is already idle (replicas: 0, no route) in git — nothing to retire."
      exit 0
    fi
    if [ "$replicas_active" = 0 ] && [ "$route_present" = 1 ]; then
      echo "$WARN DRIFT: ${svc}-canary replicas already 0 but its route line is still present — self-healing (removing the stale route)."
    fi

    # 1. IDLE-CHECK (fail-safe; delegates the actual query to observe-grafana).
    #    Only meaningful while a workload is up; a pure stale-route cleanup
    #    (replicas already 0) has no live canary to protect, so skip the gate.
    [ "$replicas_active" = 1 ] && idle_gate "$svc" "$FORCE"

    # 2. Scale down (replicas 1->0) AND remove the canary route in the SAME commit
    #    (canary-if-exists-else-stable): once the canary is idle its route must be
    #    gone so the x-fm-canary cookie falls through to stable instead of 503'ing
    #    on a zero-endpoint Service. build-canary re-adds the route line on the
    #    next activation. Stage only what actually changes; commit only if staged.
    echo "--- retiring ${svc}-canary (replicas -> 0 + route removed, via GitOps) ---"
    [ "$(git rev-parse --abbrev-ref HEAD)" = main ] || die "not on main — retire commits to main"
    git fetch -q origin main
    if [ "$replicas_active" = 1 ]; then
      sed -i.bak -E 's/(^[[:space:]]*replicas:[[:space:]]*)[1-9][0-9]*/\10/' "$dep" && rm -f "$dep.bak"
      git add "$dep"
      echo "$OK ${svc}-canary replicas -> 0"
    fi
    if [ "$route_present" = 1 ]; then
      # Delete the exact UNCOMMENTED resource line (# is the sed address delimiter;
      # ^ anchors line-start so a COMMENTED note line — e.g. in the east-west
      # kustomization — can never be matched/removed).
      sed -i.bak -E "\\#^[[:space:]]*- ${route_entry}[[:space:]]*\$#d" "$kust" && rm -f "$kust.bak"
      route_listed "$svc" && die "failed to remove ${route_entry} from $kust"
      git add "$kust"
      echo "$OK removed canary route ${route_entry} from $kust"
    fi
    if git diff --cached --quiet; then
      echo "$OK nothing to commit (state already consistent)."; exit 0
    fi
    git commit -q -m "ci(canary): retire ${svc}-canary (replicas -> 0, route removed) [skip ci]"
    git push origin main || die "push failed — resolve and re-run (or 'git pull' if the CI pin commit landed)"
    reconcile
    echo "$OK ${svc}-canary retired (replicas: 0, route removed). Its canary-<sha> pin stays in the overlay but is never pulled while idle."
    ;;

  list|status)
    echo "== canary workloads =="
    # Live replicas for every *-canary Deployment (one ssh round-trip).
    live=$($SSH "$K -n prod get deploy -o custom-columns='NAME:.metadata.name,READY:.status.readyReplicas,WANT:.spec.replicas' --no-headers 2>/dev/null | grep -- '-canary' || true" 2>/dev/null)
    for d in "$BASE"/*-canary; do
      [ -d "$d" ] || continue
      name=$(basename "$d"); svc=${name%-canary}
      tag=$(canary_tag "$svc"); [ -n "$tag" ] || tag="(no overlay entry)"
      row=$(printf '%s\n' "$live" | awk -v n="$name" '$1==n{print}')
      if [ -n "$row" ]; then
        want=$(printf '%s' "$row" | awk '{print $3}')
        ready=$(printf '%s' "$row" | awk '{print $2}')
        state=$([ "${want:-0}" -gt 0 ] 2>/dev/null && echo "ACTIVE (ready ${ready:-0}/${want})" || echo "idle (replicas 0)")
      else
        # No live Deployment yet (armed but never reconciled) — fall back to git.
        gitrep=$(grep -m1 -E '^[[:space:]]*replicas:' "$d/deployment.yaml" | grep -oE '[0-9]+')
        state=$([ "${gitrep:-0}" -gt 0 ] 2>/dev/null && echo "ACTIVE per git (not seen in cluster)" || echo "idle per git (not in cluster)")
      fi
      printf '  %-18s %-32s pinned=%s\n' "$name" "$state" "$tag"
    done
    echo
    echo "For last-seen canary traffic, run observe-grafana (Grafana MCP) per workload:"
    echo "    LogQL : {service_name=\"<svc>\"} | json | variant=\"canary\"   (last ~30m; works today)"
    # NOTE: the fm_http_* `variant` label is being added in a parallel workstream;
    # until those images redeploy this PromQL may return nothing. Prefer the Loki
    # `variant` field (above) for a definitive idle/traffic read today.
    echo "    PromQL: sum(rate(fm_http_requests_total{variant=\"canary\"}[30m])) by (service)   (may be EMPTY until the variant label ships)"
    ;;

  *) sed -n '18,21p' "$0"; exit 1 ;;
esac
