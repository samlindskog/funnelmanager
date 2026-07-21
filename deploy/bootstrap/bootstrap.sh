#!/usr/bin/env bash
# Funnel Manager k3s cluster bootstrap.
#
# IDEMPOTENT: every step checks before it changes. Run section by section
# alongside deploy/bootstrap/README.md (the runbook explains each stage,
# the firewall expectations, and the health gate to verify BEFORE moving
# on). This script is executed on the nodes / an operator workstation in a
# LATER session — nothing in the authoring session applies it.
#
# Usage:
#   ./bootstrap.sh server     # on cp
#   ./bootstrap.sh agent-edge # on edge
#   ./bootstrap.sh agent-worker <n>  # on workerN
#   ./bootstrap.sh label-taint       # from an operator shell with kubectl
#   ./bootstrap.sh secrets           # pre-Flux secrets (prompts; no values in git)
#   ./bootstrap.sh flux              # install Flux + bind the repo
set -euo pipefail

K3S_VERSION="${K3S_VERSION:-v1.31.5+k3s1}"
# --- Fill from PLACEHOLDERS.md (exported in the shell, never committed) ---
: "${KC_ISSUER:?export KC_ISSUER=https://<kc-host>/realms/funnelmanager}"
: "${CP_PRIVATE_IP:?export CP_PRIVATE_IP=<cp private ip>}"
: "${GITHUB_REPO:?export GITHUB_REPO=<owner>/funnelmanager}"

case "${1:-}" in
  server)
    # cp: k3s server, SQLite datastore (default), traefik + servicelb OFF
    # (the istio gateway owns 80/443 via hostPort on edge), kube reservations
    # so system daemons survive pressure, OIDC kubectl auth against Keycloak.
    if ! systemctl is-active --quiet k3s 2>/dev/null; then
      curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" sh -s - server \
        --disable traefik \
        --disable servicelb \
        --node-ip "$CP_PRIVATE_IP" \
        --flannel-iface "${PRIVATE_IFACE:-eth1}" \
        --node-taint node-role.kubernetes.io/control-plane=:NoSchedule \
        --kubelet-arg=kube-reserved=cpu=250m,memory=512Mi \
        --kubelet-arg=system-reserved=cpu=250m,memory=512Mi \
        --kube-apiserver-arg=oidc-issuer-url="$KC_ISSUER" \
        --kube-apiserver-arg=oidc-client-id=kubectl \
        --kube-apiserver-arg=oidc-username-claim=preferred_username \
        --kube-apiserver-arg=oidc-username-prefix='kc:' \
        --kube-apiserver-arg=oidc-groups-claim=groups \
        --kube-apiserver-arg=oidc-groups-prefix='kc:'
    fi
    echo "join token: $(sudo cat /var/lib/rancher/k3s/server/node-token)"
    ;;

  agent-edge)
    : "${K3S_URL:?export K3S_URL=https://<cp private ip>:6443}"
    : "${K3S_TOKEN:?export K3S_TOKEN=<node-token from server>}"
    if ! systemctl is-active --quiet k3s-agent 2>/dev/null; then
      curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" \
        K3S_URL="$K3S_URL" K3S_TOKEN="$K3S_TOKEN" sh -s - agent \
        --node-ip "${NODE_PRIVATE_IP:?}" \
        --flannel-iface "${PRIVATE_IFACE:-eth1}" \
        --node-label role=edge \
        --node-taint role=edge:NoSchedule \
        --kubelet-arg=kube-reserved=cpu=250m,memory=384Mi \
        --kubelet-arg=system-reserved=cpu=250m,memory=384Mi
    fi
    ;;

  agent-worker)
    : "${K3S_URL:?}" ; : "${K3S_TOKEN:?}"
    if ! systemctl is-active --quiet k3s-agent 2>/dev/null; then
      curl -sfL https://get.k3s.io | INSTALL_K3S_VERSION="$K3S_VERSION" \
        K3S_URL="$K3S_URL" K3S_TOKEN="$K3S_TOKEN" sh -s - agent \
        --node-ip "${NODE_PRIVATE_IP:?}" \
        --flannel-iface "${PRIVATE_IFACE:-eth1}" \
        --node-label role=worker \
        --kubelet-arg=kube-reserved=cpu=250m,memory=512Mi \
        --kubelet-arg=system-reserved=cpu=250m,memory=512Mi
    fi
    ;;

  label-taint)
    # Safety net if labels/taints were not applied at install time.
    kubectl get nodes -o name
    kubectl label node cp node-role.kubernetes.io/control-plane=true --overwrite
    kubectl taint node cp node-role.kubernetes.io/control-plane=:NoSchedule --overwrite || true
    kubectl label node edge role=edge --overwrite
    kubectl taint node edge role=edge:NoSchedule --overwrite || true
    kubectl label node worker1 role=worker --overwrite
    ;;

  secrets)
    # Pre-Flux secrets (NAMES fixed here; values prompted — nothing lands in
    # git). Run after `label-taint`, before `flux`.
    ns() { kubectl get ns "$1" >/dev/null 2>&1 || kubectl create ns "$1"; }
    ensure() { # ensure <ns> <name> -- kubectl create secret args...
      local n="$1" s="$2"; shift 2
      kubectl -n "$n" get secret "$s" >/dev/null 2>&1 && { echo "have $n/$s"; return; }
      kubectl -n "$n" create secret "$@"
    }
    for n in identity prod dev monitoring flux-system cert-manager; do ns "$n"; done

    # cert-manager DNS-01 solver: Cloudflare API token (Zone:DNS Edit +
    # Zone:Zone Read on the domain zone). Lives in the cert-manager namespace.
    read -rsp "Cloudflare API token (DNS-01): " CFT; echo
    ensure cert-manager cloudflare-api-token generic cloudflare-api-token \
      --from-literal=api-token="$CFT"

    read -rsp "Keycloak console admin password: " KCPW; echo
    ensure identity keycloak-admin generic keycloak-admin \
      --from-literal=username=admin --from-literal=password="$KCPW"
    echo "Provide the HARDENED prod realm export (secrets rotated; see PLACEHOLDERS.md):"
    read -rp "path to realm.json: " REALM
    ensure identity keycloak-realm generic keycloak-realm --from-file=realm.json="$REALM"

    read -rp  "Object storage ACCESS_KEY_ID: " OSK
    read -rsp "Object storage SECRET: " OSS; echo
    for n in prod dev identity; do
      ensure "$n" objectstore-backups generic objectstore-backups \
        --from-literal=ACCESS_KEY_ID="$OSK" --from-literal=ACCESS_SECRET_KEY="$OSS"
    done
    ensure monitoring objectstore-loki generic objectstore-loki \
      --from-literal=ACCESS_KEY_ID="$OSK" --from-literal=ACCESS_SECRET_KEY="$OSS"

    read -rsp "Grafana admin password: " GFPW; echo
    ensure monitoring grafana-admin generic grafana-admin \
      --from-literal=admin-user=admin --from-literal=admin-password="$GFPW"
    # Grafana Keycloak OIDC: the `grafana` confidential-client secret from the
    # realm export. Loaded via envFromSecret; the key name IS Grafana's native
    # env override for [auth.generic_oauth].client_secret.
    read -rsp "Grafana OIDC client secret (realm 'grafana' client): " GFOIDC; echo
    ensure monitoring grafana-oidc generic grafana-oidc \
      --from-literal=GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET="$GFOIDC"

    for n in prod dev; do
      for svc in search leads mail mcp; do
        read -rsp "fm-oidc client-secret for $svc ($n realm): " CS; echo
        ensure "$n" "fm-oidc-$svc" generic "fm-oidc-$svc" --from-literal=client-secret="$CS"
      done
      read -rsp "Apollo API key ($n): " AK; echo
      read -rsp "Apollo webhook secret ($n): " AW; echo
      ensure "$n" apollo generic apollo --from-literal=api-key="$AK" --from-literal=webhook-secret="$AW"
      read -rsp "OpenAI API key ($n): " OK2; echo
      ensure "$n" openai generic openai --from-literal=api-key="$OK2"
      read -rp  "Google OAuth client id ($n): " GID
      read -rsp "Google OAuth client secret ($n): " GSC; echo
      ensure "$n" google-oauth generic google-oauth \
        --from-literal=client-id="$GID" --from-literal=client-secret="$GSC"
      read -rsp "Milvus/minio access key ($n): " MK; echo
      read -rsp "Milvus/minio secret key ($n): " MS; echo
      ensure "$n" milvus-minio generic milvus-minio \
        --from-literal=access-key="$MK" --from-literal=secret-key="$MS"
    done
    # GHCR pull secret (read-only token) for the app namespaces.
    read -rp  "GHCR username: " GU
    read -rsp "GHCR read-only token: " GT; echo
    for n in prod dev; do
      ensure "$n" ghcr-pull docker-registry ghcr-pull \
        --docker-server=ghcr.io --docker-username="$GU" --docker-password="$GT"
      kubectl -n "$n" patch serviceaccount default \
        -p '{"imagePullSecrets":[{"name":"ghcr-pull"}]}' || true
    done
    echo "NOTE: app ServiceAccounts also reference ghcr-pull via the overlays."
    ;;

  flux)
    command -v flux >/dev/null || { echo "install the flux CLI first"; exit 1; }
    flux check --pre
    flux bootstrap github \
      --owner "${GITHUB_REPO%%/*}" \
      --repository "${GITHUB_REPO##*/}" \
      --branch main \
      --path deploy/clusters/prod \
      --personal
    ;;

  restore-leads)
    # Restore the pre-cutover leads Mongo archive into the cluster's mongo.
    # Usage: ./bootstrap.sh restore-leads <path-to-leads.archive.gz> [prod|dev]
    # (archive produced by: docker exec <mongo> mongodump --db=funnelmanager_leads --archive --gzip)
    ARCHIVE="${2:?path to leads-*.archive.gz}"; NS="${3:-prod}"
    POD=$(kubectl -n "$NS" get pod -l app.kubernetes.io/name=mongo -o jsonpath='{.items[0].metadata.name}')
    echo "Restoring $ARCHIVE into $NS/$POD (funnelmanager_leads)…"
    kubectl -n "$NS" exec -i "$POD" -- mongorestore --archive --gzip \
      --nsInclude='funnelmanager_leads.*' < "$ARCHIVE"
    echo "Restored. Re-index embeddings on the cluster afterwards if Milvus is fresh."
    ;;

  *)
    echo "usage: $0 {server|agent-edge|agent-worker|label-taint|secrets|flux|restore-leads}" >&2
    exit 2
    ;;
esac
