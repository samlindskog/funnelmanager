# Cluster bootstrap runbook

Executed in a **separate operational session** (this repo's authoring session
has no cluster access and applies nothing). Companion script:
`bootstrap.sh` — idempotent, run section by section; after every stage,
verify its health gate before continuing. Placeholders: `PLACEHOLDERS.md`
at the repo root.

## 0. Prerequisites

- Three Linodes on one private network (VLAN/private IPs):
  `cp` (4GB), `edge` (4GB), `worker1` (8GB) — addresses in PLACEHOLDERS.
- DNS: `A` records for `<DOMAIN>`, `dev.<DOMAIN>`, and `kc.<DOMAIN>` → the
  **edge** node's public IP.
- Linode Object Storage buckets created (CNPG + Loki; names in
  PLACEHOLDERS) with an access key pair.
- Hardened Keycloak realm exports (prod realm `funnelmanager`, dev realm
  `funnelmanager-dev`) with rotated client secrets — derived from
  `deploy/keycloak/realm-funnelmanager-dev.json`, kept out of git.
- `flux` CLI ≥ 2.3 and `kubectl` on the operator workstation.

## 1. Firewall (per node, before install)

| Node | Public exposure | Private network (nodes only) |
|---|---|---|
| `cp` | **nothing** (SSH only, keyed) | 6443/tcp (apiserver), 8472/udp (flannel VXLAN), 10250/tcp (kubelet) |
| `edge` | **80/tcp + 443/tcp** (+SSH) | 8472/udp, 10250/tcp |
| `worker1` | **nothing** (SSH only) | 8472/udp, 10250/tcp |

k8s API (6443) and inter-node traffic stay on the private interface
(`--node-ip`/`--flannel-iface` pin this). Verify: from the internet, only
`edge:80/443` answers.

## 2. k3s install (stage gate after each node)

```bash
# on cp
export KC_ISSUER=https://kc.<DOMAIN>/realms/funnelmanager CP_PRIVATE_IP=<ip> GITHUB_REPO=<owner>/funnelmanager
./bootstrap.sh server           # prints the join token
# on edge
export K3S_URL=https://<cp-private-ip>:6443 K3S_TOKEN=<token> NODE_PRIVATE_IP=<ip>
./bootstrap.sh agent-edge
# on worker1
export K3S_URL=... K3S_TOKEN=... NODE_PRIVATE_IP=<ip>
./bootstrap.sh agent-worker
# operator
./bootstrap.sh label-taint
```

Notes baked into the flags: `--disable traefik` **and** `--disable
servicelb` (the Istio gateway owns 80/443 via hostPort on edge; nothing else
may bind public ports), SQLite datastore on `cp`, kube/system reservations
on every node, OIDC apiserver args pointing at Keycloak for `kubectl` auth
(requires a public `kubectl` client + a `groups` mapper in the realm — see
PLACEHOLDERS; token auth activates once Keycloak is up, certificate admin
access keeps working regardless).

**Gate:** `kubectl get nodes` shows 3 Ready nodes with the expected
labels/taints; `kubectl get pods -A` has no traefik/svclb pods.

## 3. Pre-Flux secrets

```bash
./bootstrap.sh secrets
```

Creates (names fixed, values prompted): `identity/keycloak-admin`,
`identity/keycloak-realm` (hardened export), `objectstore-backups`
(prod/dev/identity), `monitoring/objectstore-loki`,
`monitoring/grafana-admin`, `fm-oidc-<svc>` + `apollo` + `openai` +
`google-oauth` + `milvus-minio` (prod/dev), `ghcr-pull` (prod/dev).
If adopting SOPS later, these become encrypted manifests; the bootstrap
key would be created here.

**Gate:** `kubectl get secrets -n identity,prod,monitoring` lists them all.

## 4. Flux install + repo bind

```bash
./bootstrap.sh flux   # flux bootstrap github --path deploy/clusters/prod
```

Flux commits its own `deploy/clusters/prod/flux-system/` and starts
reconciling. Expected order (enforced by `dependsOn` + `wait`):

1. `infra-namespaces` → namespaces, quotas, LimitRanges, PriorityClasses
2. `infra-gateway-api-crds`, `infra-cert-manager` (**gate:** `kubectl get
   crd gateways.gateway.networking.k8s.io`; cert-manager pods Ready)
3. `infra-cnpg-operator` (**gate:** cnpg-controller-manager Ready)
4. `infra-istio` (**gate:** istiod Ready on `cp`; `istio-ingress` pod
   Running on `edge` with hostPorts 80/443)
5. `infra-opa` (**gate:** DaemonSet 3/3 Ready — `/health?bundles` means the
   bundle loaded on every node)
6. `infra-mesh-policies` (**gate:** `istioctl analyze -A` clean; STRICT
   PeerAuthentication present)
7. `infra-identity` (**gate:** kc-db Cluster healthy, Keycloak Ready,
   realm imported — `curl https://kc.<DOMAIN>/realms/funnelmanager/.well-known/openid-configuration`)
8. `infra-gateway` (**gate:** Certificates Ready — Let's Encrypt solved via
   **DNS-01 over Cloudflare** (`cloudflare-api-token` secret; proxy may stay
   ON); Gateway Programmed. The A records for `x9bc433.win`, `dev.`, `kc.`,
   `grafana.` must point at the edge node.)
9. `infra-observability` (**gate:** prometheus/loki/grafana Ready; Grafana
   login via Keycloak at `https://grafana.x9bc433.win`)
10. `apps-prod` (**gate:** all six Deployments Ready). `apps-dev` is
    **suspended** by default (single-worker budget) — `flux resume
    kustomization apps-dev` + the `deploy-dev` workflow to run it.

**First image pin:** the overlays ship `sha-PINME`, which no registry serves,
so `apps-prod` cannot go Ready until real images are pinned. Run the
`release-prod` workflow once (Actions → Run workflow, ref `main`) — it builds
the six images and commits a `sha-<sha>` pin to `main` that Flux rolls out.
Do this right after `flux bootstrap`; the gate simply stays pending until the
pin lands.

**Data restore:** once `prod` mongo is Ready, load the pre-cutover leads
archive — `./bootstrap.sh restore-leads ~/funnelmanager-backups/<ts>/leads-usfr2.archive.gz prod`
— then re-index embeddings (cluster Milvus starts empty).

Watch with `flux get kustomizations --watch`.

## 5. Smoke-test checklist (all must pass before calling it done)

```bash
KC=https://kc.<DOMAIN>/realms/funnelmanager/protocol/openid-connect/token
# 1. Authenticated end-to-end: login (or PKCE via the app), call the API.
TOK=$(curl -s $KC -d grant_type=password -d client_id=frontend \
      -d username=admin -d password=<pw> | jq -r .access_token)
curl -sf https://<DOMAIN>/api/search/searches -H "Authorization: Bearer $TOK"   # 200 []
# 2. Anonymous webhook allowed (503 = reached leads, secret unconfigured
#    counts as reached; with the secret set expect 401-on-bad/200-on-good):
curl -s -o /dev/null -w '%{http_code}\n' -X POST \
  https://<DOMAIN>/api/leads/webhooks/apollo/wrong-secret                        # 401 (not 403-by-mesh)
# 3. Wrong-audience token rejected (mcp-audience token against search):
AT=$(curl -s $KC -d grant_type=client_credentials -d client_id=agent-example \
     -d client_secret=<secret> | jq -r .access_token)
curl -s -o /dev/null -w '%{http_code}\n' \
  https://<DOMAIN>/api/search/searches -H "Authorization: Bearer $AT"            # 401/403
# 4. Dedicated dependency rejects a foreign caller (workload isolation):
kubectl -n prod exec deploy/mail -- python -c \
  "import socket; socket.create_connection(('mongo',27017),3)" && echo LEAK || echo BLOCKED   # BLOCKED
# 5. Tokenless internal call denied by the mesh:
kubectl -n prod exec deploy/frontend -- wget -qO- --timeout=3 http://leads:8001/api/leads/stats || echo DENIED
# 6. Logs and metrics flowing: Grafana → Loki (any app log line with `sub`),
#    Prometheus targets green; `kubectl top nodes` works (k3s metrics-server).
```

## 6. Memory budget (prod requests, steady state)

| worker1 (8GB) | requests |
|---|---|
| apps (search/leads/mail/mcp/frontend/mailui) | 672Mi |
| app sidecars (6 × 64Mi) | 384Mi |
| data (app-db, mail-db, mongo, milvus, etcd, minio) | 1536Mi |
| data sidecars (6 × 64Mi) | 384Mi |
| kc-db (identity) | 192Mi |
| observability (prom/loki/grafana/ksm/fluent-bit/node-exp) | ~1056Mi |
| platform (cnpg op, cert-manager, flux, opa, coredns) | ~640Mi |
| kubelet reservations (kube+system) | 1024Mi |
| **total** | **≈ 5.9GB of 8GB** |

`edge` (4GB): gateway 128Mi + keycloak 640Mi + opa 64Mi + agents/reserved
≈ 1.7GB. `cp` (4GB): k3s server ≈ 1–1.5GB + istiod 384Mi + opa 64Mi.
Dev is suspended by default; resuming it fits only if you accept
overcommit or add a worker (its quota caps it at 3Gi requests).
