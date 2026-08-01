# PLACEHOLDERS — values you must supply

Domain, Keycloak host, and Grafana host are now **filled in** for `x9bc433.win`
(the ✅ items below). A few *decided* values are intentionally **redacted** to the
gitignored ops config rather than printed here (the repo is public): the LE contact
email and the node public IPs appear as `<…>` placeholders. What remains genuinely
unset are cluster/credential values that cannot live in git — the only literal
`REPLACE_` tokens left are the object-storage bucket names + region
(`grep -rn REPLACE_ deploy`).

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
- [x] **Let's Encrypt email** → `<le-contact-email>` (cert-manager issuers; the
      real address lives in the gitignored ops config, not this public repo).
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
- [ ] **Linode Object Storage** — create two buckets and one access-key pair;
      fill `REPLACE_BUCKET_CNPG`, `REPLACE_BUCKET_LOKI`, `REPLACE_REGION`
      (endpoint `https://<REPLACE_REGION>.linodeobjects.com`) in
      `deploy/apps/base/data/*/cluster.yaml`, `deploy/infrastructure/identity/cluster.yaml`,
      `deploy/infrastructure/observability/loki.yaml`. Keys go into the
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

## Canary access

- [x] **Canary cookie secret** — lives in **exactly one place**: the
      `fm-canary-token` Secret in the **`istio-ingress`** namespace (key `token`).
      It is **not** in git. The value is delivered to the gateway's Envoy container
      as the env var **`FM_CANARY_SECRET`** (`secretKeyRef` in
      `deploy/infrastructure/istio/gateway-deployment.yaml`), and the
      `canary-cookie-gate` EnvoyFilter
      (`deploy/infrastructure/mesh-policies/canary-cookie-gate.yaml`) reads it via
      `os.getenv("FM_CANARY_SECRET")`.

      The canary is gated by a **host-only cookie** `fm_canary=<secret>` on
      `x9bc433.win`, NOT a client-sent header. The EnvoyFilter validates the cookie
      at the gateway, **always strips** any client-supplied `x-fm-canary` header,
      and re-injects `x-fm-canary: <secret>` only on a valid cookie. Every canary
      route/VS then **presence-matches** `x-fm-canary` (regex `.+`) — the secret is
      NOT in any route — and routes the request to `<svc>-canary`. Set the cookie
      with no `Domain` attribute, so it is host-only and is NOT sent to
      `kc.`/`grafana.` subdomains.

      **Fail-safe.** If `FM_CANARY_SECRET` is unset/empty the filter degrades
      safely: it never injects `x-fm-canary`, `/canary/on` sets no cookie, and it
      never errors — normal traffic flows to stable. The client-header strip stays
      unconditional. The filter is purely additive: if it detaches, no route
      injects the marker and callers fall through to stable (never fail-open).

      **Easy toggle (preferred).** The same EnvoyFilter answers two server-side
      cookie-setter endpoints entirely at the gateway (INSERT_BEFORE jwt_authn, so
      they never hit OPA or the app):
      - `https://x9bc433.win/canary/on?t=<secret>` sets the cookie and
        302-redirects to `/` (a wrong/absent/missing-secret `t` fails closed:
        redirects with NO cookie set).
      - `https://x9bc433.win/canary/off` clears the cookie and redirects to `/`.
      The cookie is set SERVER-SIDE, so it is **HttpOnly** (JS cannot read it); it
      stays `Secure` + `SameSite=Lax`. The `enter-canary` launcher automates this,
      reading the secret from `~/.config/fm-e2e/creds.env` (`FM_CANARY_TOKEN`).

      **Getting the secret value** (to set the cookie manually or drive E2E):
      read it from the `fm-canary-token` Secret
      (`kubectl -n istio-ingress get secret fm-canary-token -o
      jsonpath='{.data.token}' | base64 -d`) or from `~/.config/fm-e2e/creds.env`.

      **Rotation** — the secret is in ONE place, not five:
      1. `kubectl -n istio-ingress create secret generic fm-canary-token
         --from-literal=token=<new> --dry-run=client -o yaml | kubectl apply -f -`
      2. `kubectl -n istio-ingress rollout restart deploy/istio-ingress`
         (so Envoy re-reads the env var), and update `~/.config/fm-e2e/creds.env`.
      No manifest edit — the routes presence-match and carry no secret.

      Each `canary/<svc>-canary.yaml` is a separate HTTPRoute present in the
      gateway kustomization **only while that canary is active**
      (`canary-if-exists-else-stable`), so an idle canary never 503s the cookie.
      Only ever build the canary from a TRUSTED branch — the canary serves
      feature-branch JS same-origin and can read the prod Keycloak session of
      anyone who reaches it (see the frontend-canary deployment trust-boundary
      note).

- [ ] **`fm-canary-token` Secret** (`istio-ingress` ns, key `token`) — the canary
      cookie secret; provisioned by the `secrets` bootstrap stage (prompts, or
      auto-generates a random 32-hex value). Rotate as above.

## Deferred hardening

- [ ] **Base-image digest pinning** — resolve version tags to `@sha256:`.
- [ ] **CI push-to-main** — `build-images.yml` commits image pins to `main`
      with `GITHUB_TOKEN`; ensure branch protection allows it (or supply a
      dedicated bot PAT) and that a required reviewer gates the `production`
      environment on `release-prod`.
- [ ] **Mail mailbox ownership** — any user with the `mail` grant sees all
      mailboxes; revisit in OPA data if isolation is needed.
