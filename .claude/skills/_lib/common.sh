#!/usr/bin/env bash
# Shared library for the funnelmanager operator skills (deploy-funnelmanager,
# prod-health, canary). It is `source`d, NOT executed. It does two things:
#
#   (a) Loads the GITIGNORED ops config (${FM_OPS_ENV:-$HOME/.config/fm-ops/env})
#       that supplies the node host/alias, kubeconfig path, and Cloudflare zone id
#       — values that must NOT live in a public repo. The tracked TEMPLATE showing
#       the shape is _lib/ops-env.example; copy it to ~/.config/fm-ops/env (outside
#       the repo, so it is never committed).
#
#   (b) Exports the shared SSH / K / FLUX remote-invocation prefixes, the single
#       FM_SERVICES list (the ten deployed services), and the canary-secret helper,
#       so the three drivers stop re-declaring the same consts/lists five times.
#
# Only FM_CF_ZONE_ID is genuinely required (no default; the Cloudflare purge path
# errors without it). FM_CP_HOST / FM_KUBECONFIG carry working defaults for the
# current cluster so the read paths keep working out of the box; override them in
# the ops env if the topology changes.

# Idempotent: a driver may source this once; guard against a double-source.
if [ -n "${FM_COMMON_SH:-}" ]; then return 0; fi
FM_COMMON_SH=1

# --- gitignored ops config --------------------------------------------------
FM_OPS_ENV="${FM_OPS_ENV:-$HOME/.config/fm-ops/env}"
if [ -f "$FM_OPS_ENV" ]; then
  # shellcheck disable=SC1090
  . "$FM_OPS_ENV"
fi

# k3s control-plane ssh alias (resolved via ~/.ssh/config); flux + kubectl live
# there. Kubeconfig is root's on that box.
FM_CP_HOST="${FM_CP_HOST:-usfr4}"
FM_KUBECONFIG="${FM_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
# Cloudflare zone id for the public app host — used ONLY by `deploy.sh purge`.
# No default: the purge verb must fail loudly (pointing at the ops env) rather
# than baking the id into this tracked file.
FM_CF_ZONE_ID="${FM_CF_ZONE_ID:-}"

# The TEN deployed services (backends + frontends), all 1-replica Deployments in
# prod. This is the authoritative rollout-wait list and the release/health
# workload set. `backup` is a batch CronJob image, NOT a Deployment, so it is not
# here — it IS the eleventh BUILT image, but nothing waits on its rollout.
FM_SERVICES="frontend searchui mailui agentsui search leads mail mcp jobs agents"

# Shared remote-invocation prefixes. Plain flux/kubectl on the control plane fail
# with "dial tcp [::1]:8080: connection refused" — root's kubeconfig must be
# passed explicitly; hence the `sudo -n env KUBECONFIG=…` prefix on flux and the
# `sudo -n` on kubectl. BatchMode + a short ConnectTimeout keep ssh non-interactive.
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10 $FM_CP_HOST"
K="sudo -n kubectl"
FLUX="sudo -n env KUBECONFIG=$FM_KUBECONFIG flux"

# The canary-cookie secret, read from its canonical source (the EnvoyFilter Lua)
# so it is never re-hardcoded in a driver. Takes the cookie-gate file path as $1;
# echoes the hex token, or nothing if the filter/token can't be found.
fm_canary_secret() {
  grep -oE 'secret = "[0-9a-fA-F]+"' "${1:?fm_canary_secret: cookie-gate file path required}" 2>/dev/null \
    | grep -oE '[0-9a-fA-F]{8,}' | head -1
}
