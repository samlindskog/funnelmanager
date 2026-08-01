# PLACEHOLDERS — values you must supply

Domain, Keycloak host, and Grafana host are now **filled in** for `x9bc433.win`
(the ✅ items below). A few *decided* values are intentionally **redacted** to the
gitignored ops config rather than printed here (the repo is public). The LE contact
email and the object-storage host/bucket are referenced in tracked manifests as
`${cluster_*}` and **Flux substitutes them at reconcile** from the `fm-cluster-vars`
ConfigMap (flux-system, bootstrap-created — see `deploy/bootstrap/bootstrap.sh`
`secrets`); node public IPs live in the gitignored ops config and appear as `<…>`
here. What remains genuinely unset are the credential values that cannot live in
git (bootstrap prompts create their Secrets).

## Domains & endpoints  ✅ (filled for x9bc433.win)

- [x] **App host** → `x9bc433.win` (prod). Consumed by: Gateway
      listeners/Certificates, HTTPRoutes, OPA `config.hosts`, overlay `fm-web`
      (CORS/`PUBLIC_BASE_URL`), Keycloak `frontend` client redirect URIs.
- [x] **Keycloak host** → `kc.x9bc433.win` → issuer
      `https://kc.x9bc433.win/realms/funnelmanager` (prod). Consumed by: OPA
      `issuers`/`keycloak_host`, RequestAuthentication, overlay `fm-oidc`,
      Grafana OIDC.
- [x] **Grafana host** → `grafana.x9bc433.win` (own gateway listener/cert/
      route; OPA `grafana_host` bypass; Grafana enforces Keycloak OIDC).
- [x] **Let's Encrypt email** → `${cluster_le_email}` in the cert-manager
      ClusterIssuers; Flux substitutes it from the `fm-cluster-vars` ConfigMap
      (bootstrap-created), so the real address stays out of this public repo.
- [ ] **DNS records to create** — `A` for `x9bc433.win`, `kc.x9bc433.win`,
      `grafana.x9bc433.win` → the **edge** node's public IP
      (`<edge-public-ip>` — see the gitignored ops config / your infra records).
      Cloudflare proxy may stay **ON** (DNS-01 validates via the API). Today apex
      resolves to Cloudflare — repoint origin to edge. (`dev.x9bc433.win` is the
      compose/usfr3 dev box, not the cluster.)
- [ ] **Cloudflare API token** (DNS-01) — Zone:DNS Edit + Zone:Zone Read on
      `x9bc433.win` → `cloudflare-api-token` Secret in `cert-manager`
      (the `secrets` bootstrap stage prompts for it).
- [ ] Re-register the **Google OAuth redirect URI**
      `https://x9bc433.win/api/mail/oauth/callback` on the OAuth client.

## Identity

- [ ] **Hardened prod realm export** for the cluster, kept OUT of git,
      derived from `deploy/keycloak/realm-funnelmanager-prod.example.json`
      (realm `funnelmanager`; no human users — create them in the console).
      Rotate every `REPLACE-*` client secret; mirror each into the matching
      `fm-oidc-<svc>` Secret. The `keycloak-realm` import Secret points at this
      **prod** export. (The `funnelmanager-dev` realm is compose/local-dev
      only and is never imported to the cluster Keycloak.)
- [ ] **Grafana OIDC client secret** — the `grafana` confidential client's
      secret from the realm export → `grafana-oidc` Secret in `monitoring`
      (key `GF_OAUTH_CLIENT_SECRET`; the `secrets` stage prompts).
- [ ] Production hardening: `directAccessGrantsEnabled` is already `false` on
      the `frontend` client in the prod template; confirm redirect URIs.

## Cluster (bootstrap — `deploy/bootstrap/README.md`)

- [x] **Node roles/IPs** — cp = `usfr4` (`<cp-public-ip>`), edge = `usfr3`
      (`<edge-public-ip>`), worker1 = `usfr2` (`<worker1-public-ip>`). The real
      public IPs live in the gitignored ops config, not this public repo — the ssh
      aliases here are what the tooling uses. Private-network IPs + `PRIVATE_IFACE`
      are still needed for the k3s `--node-ip`/`--flannel-iface` flags.
- [ ] **Linode Object Storage** — create two buckets (observability + backups)
      and one access-key pair. The host + bucket names are referenced as
      `${cluster_obj_host}` (endpoint `https://${cluster_obj_host}`),
      `${cluster_obj_bucket}` (Loki/Tempo) and `${cluster_obj_bucket_backups}`
      (CNPG WAL + mongo dumps) in `deploy/apps/base/data/*/`,
      `deploy/infrastructure/identity/cluster.yaml`, and
      `deploy/infrastructure/observability/{loki,tempo}.yaml`; Flux substitutes
      them from the `fm-cluster-vars` ConfigMap (bootstrap prompts), so the names
      stay out of this public repo. The access KEYS go into the
      `objectstore-backups` / `objectstore-loki` Secrets (bootstrap prompts).
- [ ] **GHCR read-only token** → `ghcr-pull` (prod ns; bootstrap prompts).
- [ ] **App/identity secrets** (bootstrap prompts, prod ns):
      `keycloak-admin`, per-service `fm-oidc-*`, `apollo`, `openai`,
      `google-oauth`, `milvus-minio`, `grafana-admin`.

## Data migration (pre-cutover backup → cluster)

- [x] **Leads Mongo backed up** — `~/funnelmanager-backups/<ts>/leads-usfr2.archive.gz`
      (63,545 docs). After the cluster's mongo is up:
      `deploy/bootstrap/bootstrap.sh restore-leads <archive> prod`, then
      re-index embeddings (`leads/scripts/reembed.py` or the `~/ops` reconcile)
      since cluster Milvus starts empty.
- [ ] **Search history (Postgres)** — decide whether to carry it forward; if
      so, dump `funnelmanager` from the old prod `db` container and restore
      into the CNPG `app-db` (schema is unchanged in this repo).

## Debug session & canary access

- [x] **Debug-session secret** — lives in **exactly one place**: the
      `fm-canary-token` Secret in the **`istio-ingress`** namespace (key `token`).
      It is **not** in git. The value is delivered to the gateway's Envoy container
      as the env var **`FM_CANARY_SECRET`** (`secretKeyRef` in
      `deploy/infrastructure/istio/gateway-deployment.yaml`), and the
      `debug-session-gate` EnvoyFilter
      (`deploy/infrastructure/mesh-policies/debug-session-gate.yaml`) reads it via
      `os.getenv("FM_CANARY_SECRET")`. *(The k8s Secret name `fm-canary-token` and
      the env var `FM_CANARY_SECRET` DELIBERATELY keep their bootstrap names — a
      rename would leave the gateway referencing a nonexistent Secret until
      bootstrap recreates it. The value is the `fm_debug` session secret.)*

      **ONE value-encoded cookie, two forms** (on `x9bc433.win`, host-only — no
      `Domain` attribute, so NOT sent to `kc.`/`grafana.` subdomains). It is HttpOnly,
      server-set, same secret mechanism as the old `fm_canary`:
      - **`fm_debug=<secret>`** — the un-forgeable **debug-session** grant. It
        **permits** canary routing, but by itself routes to **STABLE/prod** pods.
        (It does NOT gate prod tracing — see the note below.)
      - **`fm_debug=<secret>|canary`** — the SAME debug session, routed to the
        **canary**. Route selection is itself secret-gated: the `|canary` suffix is
        only ever honored as part of the exact secret value (`|` is a valid RFC6265
        cookie-octet). There is **no** non-secret selector cookie, so a canary route
        cannot be planted on a victim by a cross-site `Set-Cookie`.

      The EnvoyFilter validates the cookie at the gateway with **exact equality**
      (`<secret>` or `<secret>|canary`, never a prefix), **always strips** any
      client-supplied `x-fm-canary` header, and re-injects `x-fm-canary: <secret>`
      **only when `fm_debug=<secret>|canary` is present**. Every canary route/VS then
      **presence-matches** `x-fm-canary` (regex `.+`) — the secret is NOT in any
      route — and routes the request to `<svc>-canary`.

      **No prod-tracing gate** (attempted, removed). Istio ingress honors an incoming
      sampled `traceparent` from **any** client, and that can't be made
      cookie-conditional in-band — Envoy fixes the sampling decision before the HTTP
      filter chain (confirmed live). So "any client can force-sample prod" remains
      open: a **telemetry-cost-only** residual (tracing is never authz), bounded by
      Cloudflare + Tempo ingestion caps. The `--target prod` shim still traces prod
      via honor-incoming-sampled. True prevention would need a Cloudflare edge
      `traceparent` rewrite + origin-locked-to-CF (tracked follow-up).

      **Fail-safe.** If `FM_CANARY_SECRET` is unset/empty the filter degrades
      safely: it never injects `x-fm-canary`, the `/debug/*` endpoints set no
      `fm_debug` cookie, and it never errors — normal traffic flows to stable. The
      client-header strip stays unconditional. If the filter detaches, no route
      injects the marker and callers fall through to stable (never fail-open).

      **Easy toggle (preferred).** The same EnvoyFilter answers server-side
      cookie-setter endpoints entirely at the gateway (INSERT_BEFORE jwt_authn, so
      they never hit OPA or the app):
      - `https://x9bc433.win/debug/on?t=<secret>` sets **`fm_debug=<secret>`** (debug
        session on stable) and 302-redirects home (a wrong/absent/missing-secret `t`
        or a non-navigation fails closed: redirects with NO cookie set).
      - `https://x9bc433.win/debug/canary/on?t=<secret>` sets
        **`fm_debug=<secret>|canary`** (debug session on the canary), same
        fail-closed guard.
      - `https://x9bc433.win/debug/off` clears the `fm_debug` cookie and redirects
        home.
      Each endpoint emits exactly **one** Set-Cookie and 302s straight to `/` — the
      single value-encoded cookie means no internal redirect relay is needed.
      `fm_debug` is set SERVER-SIDE, so it is **HttpOnly** (JS cannot read it) and
      stays `Secure` + `SameSite=Lax`. The `enter-canary` launcher automates this,
      reading the secret from `~/.config/fm-e2e/creds.env` (`FM_DEBUG_TOKEN`).

      **Getting the secret value** (to set the cookies manually or drive E2E):
      read it from the `fm-canary-token` Secret
      (`kubectl -n istio-ingress get secret fm-canary-token -o
      jsonpath='{.data.token}' | base64 -d`) or from `~/.config/fm-e2e/creds.env`.

      **Rotation** — the secret is in ONE place, not five:
      1. `kubectl -n istio-ingress create secret generic fm-canary-token
         --from-literal=token=<new> --dry-run=client -o yaml | kubectl apply -f -`
      2. `kubectl -n istio-ingress rollout restart deploy/istio-ingress`
         (so Envoy re-reads the env var), and update `~/.config/fm-e2e/creds.env`
         (`FM_DEBUG_TOKEN`).
      No manifest edit — the routes presence-match and carry no secret.

      Each `canary/<svc>-canary.yaml` is a separate HTTPRoute present in the
      gateway kustomization **only while that canary is active**
      (`canary-if-exists-else-stable`), so an idle canary never 503s the cookies.
      Only ever build the canary from a TRUSTED branch — the canary serves
      feature-branch JS same-origin and can read the prod Keycloak session of
      anyone who reaches it (see the frontend-canary deployment trust-boundary
      note).

- [ ] **`fm-canary-token` Secret** (`istio-ingress` ns, key `token`) — the
      debug-session `fm_debug` secret; provisioned by the `secrets` bootstrap stage
      (prompts, or auto-generates a random 32-hex value). Rotate as above.

## Deferred hardening

- [ ] **Base-image digest pinning** — resolve version tags to `@sha256:`.
- [ ] **CI push-to-main** — `build-images.yml` commits image pins to `main`
      with `GITHUB_TOKEN`; ensure branch protection allows it (or supply a
      dedicated bot PAT) and that a required reviewer gates the `production`
      environment on `release-prod`.
- [ ] **Mail mailbox ownership** — any user with the `mail` grant sees all
      mailboxes; revisit in OPA data if isolation is needed.
