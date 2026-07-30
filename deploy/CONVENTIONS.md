# deploy/ conventions (binding for every manifest)

One source of truth for names, labels, scheduling, resources, and secret
refs. Manifests that deviate from this file are bugs.

## Cluster & nodes

k3s, `--disable traefik`, flannel CNI, k3s NetworkPolicy controller.

| Node | Role | Labels | Taints |
|---|---|---|---|
| `cp` | control plane (SQLite datastore) | `node-role.kubernetes.io/control-plane=true` | `node-role.kubernetes.io/control-plane=:NoSchedule` |
| `edge` | DMZ: ingress gateway + Keycloak only | `role=edge` | `role=edge:NoSchedule` |
| `worker1` | apps, CNPG Postgres, observability | `role=worker` | none |
| `worker2` | apps headroom (canary/second-pod capacity) | `role=worker` | none |

Never assume single-worker scheduling: schedule to `role=worker` via
nodeSelector/affinity on the *label*, not the node name. Both workers share
`role=worker`; when a workload wants a *specific* worker (e.g. the frontend
canary prefers the `worker2` headroom) single it out with a **soft**
`kubernetes.io/hostname` nodeAffinity preference, never a hard requirement.
istiod runs on `cp` (toleration + nodeSelector); OPA is a DaemonSet on every
node (tolerates both taints). Only `edge` gets public 80/443.

## Namespaces

| Namespace | Contents | Istio injection |
|---|---|---|
| `istio-system` | istiod | n/a |
| `istio-ingress` | ingress gateway (edge) | gateway-only |
| `identity` | Keycloak (edge) + `kc-db` CNPG cluster (worker) | disabled (trust root; CNPG jobs fight sidecars — NetworkPolicy-guarded instead) |
| `opa-system` | OPA DaemonSet + Service | disabled (OPA is the decider, not a mesh client) |
| `cnpg-system` | CloudNativePG operator | disabled |
| `monitoring` | Prometheus, Loki, Grafana, Fluent Bit | disabled (scrapes/ships across the mesh boundary) |
| `prod` | full app + data stack | enabled (`istio-injection: enabled`) |
| `dev` | full app + data stack (small) | enabled |
| `flux-system` | Flux controllers | disabled |

`prod` PriorityClass `fm-prod` (value 100000) > `dev` `fm-dev` (10000);
every pod in prod/dev sets `priorityClassName` accordingly (patched by the
overlay, not in base).

## Names, labels, service accounts

- App/workload names are the short service names: `search`, `leads`,
  `mail`, `mcp`, `jobs`, `agents`, `frontend`, `mailui`, `agentsui`,
  `keycloak`, `opa`.
- Data workloads: `app-db` (search's Postgres, CNPG), `mail-db` (CNPG),
  `jobs-db` (CNPG), `agents-db` (CNPG), `kc-db` (CNPG, in `identity`),
  `mongo`, `milvus`, `etcd`, `minio`.
- Every Deployment/StatefulSet has its own ServiceAccount named exactly
  like the workload → SPIFFE `spiffe://cluster.local/ns/<ns>/sa/<name>`.
  No workload uses `default`, none is cluster-admin.
- Common labels on everything: `app.kubernetes.io/name: <name>`,
  `app.kubernetes.io/part-of: funnelmanager`. Selectors use
  `app.kubernetes.io/name` only.
- `version` is an OPTIONAL label distinguishing a canary variant from stable
  (e.g. `version: canary` on `frontend-canary`), useful for Tempo/Grafana
  split-by-version. It goes on the Deployment/pod template only — never in
  Service or NetworkPolicy selectors (they key on `app.kubernetes.io/name`).
- Container ports keep the compose numbers: search 8000, leads 8001,
  mcp 8003, mail 8004, jobs 8005, agents 8006, static nginx 8080 (non-root
  nginx), Keycloak 8080.
  Service port == container port; Service names == workload names.

## Images

- Apps: `ghcr.io/samlindskog/funnelmanager/<name>` — tag is set ONLY in
  overlays via the kustomize `images:` transformer (`sha-<gitsha>`; the
  prod overlay carries the currently-deployed pin). Never `:latest`.
- Third-party images pinned to exact versions:
  `quay.io/keycloak/keycloak:26.2.5@sha256:4883630ef9db14031cde3e60700c9a9a8eaf1b5c24db1589d6a2d43de38ba2a9`, `mongo:7.0.21`,
  `milvusdb/milvus:v2.5.4`, `quay.io/coreos/etcd:v3.5.18`,
  `minio/minio:RELEASE.2024-12-18T13-15-44Z`,
  `openpolicyagent/opa:1.6.0-envoy` (envoy ext_authz build),
  `nginxinc/nginx-unprivileged:1.27-alpine` (static apps).

## Resources (prod requests/limits — the worker1 budget)

Every container sets requests AND limits; sidecars are sized globally in
istiod values (64Mi/256Mi). The summed budget lives in
`deploy/bootstrap/README.md` and must stay ≤ ~6GB on worker1.

| Workload | requests cpu/mem | limits cpu/mem |
|---|---|---|
| search | 50m / 128Mi | 500m / 512Mi |
| leads | 100m / 256Mi | 1000m / 1536Mi |
| mail | 50m / 128Mi | 500m / 512Mi |
| mcp | 50m / 128Mi | 500m / 768Mi |
| jobs | 50m / 128Mi | 500m / 512Mi |
| agents | 50m / 128Mi | 500m / 768Mi |
| frontend (nginx) | 10m / 16Mi | 100m / 64Mi |
| mailui (nginx) | 10m / 16Mi | 100m / 64Mi |
| agentsui (nginx) | 10m / 16Mi | 100m / 64Mi |
| app-db / mail-db / jobs-db / agents-db / kc-db (CNPG, each) | 100m / 192Mi | 500m / 512Mi |
| mongo | 100m / 384Mi | 1000m / 1536Mi |
| milvus | 200m / 512Mi | 1500m / 2560Mi |
| etcd | 50m / 128Mi | 300m / 512Mi |
| minio | 50m / 128Mi | 300m / 512Mi |
| keycloak (edge) | 200m / 640Mi | 1000m / 1Gi |
| opa (per node) | 25m / 64Mi | 200m / 256Mi |
| prometheus | 100m / 512Mi | 500m / 1Gi |
| loki | 100m / 256Mi | 500m / 512Mi |
| grafana | 50m / 128Mi | 300m / 256Mi |
| fluent-bit (per node) | 25m / 64Mi | 100m / 128Mi |
| kube-state-metrics | 25m / 64Mi | 100m / 128Mi |

Dev workloads halve app requests, keep limits, replicas 1 everywhere. (The
GitOps dev-preview overlay that applied this was removed pending an Istio
canary for dev pods; the `dev` namespace, quotas, `fm-dev` PriorityClass, and
mesh/gateway policies remain for that future work.)

## Identity & app env

- Keycloak realm per env: `funnelmanager` (prod), `funnelmanager-dev`
  (dev). Issuer `https://<KC_HOST>/realms/<realm>`.
- Backend env (from `fm-oidc` ConfigMap + `fm-oidc-<svc>` Secret per ns):
  `FM_SERVICE_NAME`, `FM_OIDC_ISSUER`, `FM_OIDC_CLIENT_ID`,
  `FM_OIDC_CLIENT_SECRET` (secretKeyRef), `FM_JWT_VERIFY: "false"`
  (mesh validates; RequestAuthentication), plus service-specific vars
  mirroring compose. Split horizon is required: the app namespaces'
  default-deny egress cannot reach the public issuer host, so every service
  that performs an RFC 8693 exchange (`search`, `mcp`, `jobs`, `agents`)
  sets `FM_OIDC_TOKEN_URL` (fm-oidc `token-url`) to
  `keycloak.identity.svc:8080` — issued tokens still carry the public
  issuer because Keycloak's backchannel hostname is not dynamic.
- Probes: HTTP GET `/healthz` (liveness) and `/readyz` (readiness) on the
  app port for the backends (`search`, `leads`, `mail`, `mcp`, `jobs`,
  `agents`); `/` for static nginx (`agentsui` probes `/agents/`, its base
  path); Keycloak uses `/health/ready` on port 9000 (management).

## Secrets (refs only — values supplied at bootstrap / via SOPS)

| Secret (ns) | Keys | Used by |
|---|---|---|
| `fm-oidc-<svc>` (prod/dev) | `client-secret` | each backend |
| `apollo` (prod/dev) | `api-key`, `webhook-secret` | leads |
| `openai` (prod/dev) | `api-key` | leads, agents |
| `google-oauth` (prod/dev) | `client-id`, `client-secret` | mail |
| `fm-approval` (prod/dev) | `secret` | agents (mints), leads + mail (verify) — Principle-4 human-approval HMAC; one shared value per env |
| `objectstore-backups` (prod/dev/identity) | `ACCESS_KEY_ID`, `ACCESS_SECRET_KEY` | CNPG barmanObjectStore |
| `objectstore-loki` (monitoring) | `ACCESS_KEY_ID`, `ACCESS_SECRET_KEY` | Loki |
| `keycloak-admin` (identity) | `username`, `password` | Keycloak bootstrap |
| `keycloak-realm` (identity) | `realm.json` | realm import (prod-hardened export) |
| `kc-db-app` etc. | generated by CNPG | Keycloak DB creds |

## Storage

k3s default StorageClass `local-path`. PVCs: app-db 5Gi, mail-db 10Gi,
jobs-db 5Gi, agents-db 5Gi, kc-db 2Gi, mongo 20Gi, milvus 10Gi, etcd 2Gi,
minio 10Gi, prometheus 10Gi,
loki 5Gi (cache; chunks go to object storage).

## Mesh & dedicated dependencies

- STRICT mTLS mesh-wide. Data stores join the mesh (sidecar) and are
  guarded three ways: (1) OPA data-document pairing rules, (2) an Istio
  L4 `AuthorizationPolicy` allowing only the owner's SA principal, (3) a
  NetworkPolicy admitting only the owner. Pairings:
  `search→app-db`, `mail→mail-db`, `jobs→jobs-db`, `agents→agents-db`,
  `keycloak→kc-db`, `leads→mongo`, `leads→milvus`, `milvus→etcd`,
  `milvus→minio` (nested).
- Never routed from the gateway: leads (except `/api/leads/webhooks/`),
  mcp, jobs, all data stores, OPA. Gateway-routed: `search` (`/api/search`),
  `mail` (`/api/mail`), `agents` (`/api/agents`), the SPAs `frontend` (`/`),
  `mailui` (`/mail/`), `agentsui` (`/agents/`), and `leads`'s webhook prefix.
