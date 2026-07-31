# PLACEHOLDERS — values you must supply

Domain, Keycloak host, Grafana host, and the LE email are now **filled in**
for `x9bc433.win` (the ✅ items below). What remains are cluster/credential
values that cannot live in git — the only literal `REPLACE_` tokens left are
the object-storage bucket names + region (`grep -rn REPLACE_ deploy`).

## Domains & endpoints  ✅ (filled for x9bc433.win)

- [x] **App host** → `x9bc433.win` (apex = prod), dev → `dev.x9bc433.win`.
      Consumed by: Gateway listeners/Certificates, HTTPRoutes, OPA
      `config.hosts`, overlay `fm-web` (CORS/`PUBLIC_BASE_URL`), Keycloak
      `frontend` client redirect URIs.
- [x] **Keycloak host** → `kc.x9bc433.win` → issuer
      `https://kc.x9bc433.win/realms/funnelmanager` (prod) /
      `…/funnelmanager-dev` (dev). Consumed by: OPA `issuers`/`keycloak_host`,
      RequestAuthentication, overlay `fm-oidc`, Grafana OIDC.
- [x] **Grafana host** → `grafana.x9bc433.win` (own gateway listener/cert/
      route; OPA `grafana_host` bypass; Grafana enforces Keycloak OIDC).
- [x] **Let's Encrypt email** → `sam@slindskog.net` (cert-manager issuers).
- [ ] **DNS records to create** — `A` for `x9bc433.win`, `dev.x9bc433.win`,
      `kc.x9bc433.win`, `grafana.x9bc433.win` → the **edge** node's public IP
      (`192.81.135.223`). Cloudflare proxy may stay **ON** (DNS-01 validates
      via the API). Today apex/dev resolve to Cloudflare — repoint origin to
      edge.
- [ ] **Cloudflare API token** (DNS-01) — Zone:DNS Edit + Zone:Zone Read on
      `x9bc433.win` → `cloudflare-api-token` Secret in `cert-manager`
      (the `secrets` bootstrap stage prompts for it).
- [ ] Re-register the **Google OAuth redirect URI**
      `https://x9bc433.win/api/mail/oauth/callback` on the OAuth client.

## Identity

- [ ] **Two hardened realm exports**, kept OUT of git, derived from the
      templates in `deploy/keycloak/`:
      - prod realm `funnelmanager` (from
        `realm-funnelmanager-prod.example.json`) — no human users; create
        them in the console.
      - dev realm `funnelmanager-dev` (from `realm-funnelmanager-dev.json`,
        renaming `realm` to `funnelmanager-dev` and swapping localhost
        redirect URIs for `https://dev.x9bc433.win/*`).
      Rotate every `REPLACE-*`/`dev-*` client secret; mirror each into the
      matching `fm-oidc-<svc>` Secret. The `keycloak-realm` import Secret
      points at the **prod** export; import the dev realm via the console or a
      second import.
- [ ] **Grafana OIDC client secret** — the `grafana` confidential client's
      secret from the realm export → `grafana-oidc` Secret in `monitoring`
      (key `GF_OAUTH_CLIENT_SECRET`; the `secrets` stage prompts).
- [ ] Production hardening: `directAccessGrantsEnabled` is already `false` on
      the `frontend` client in the prod template; confirm redirect URIs.

## Cluster (bootstrap — `deploy/bootstrap/README.md`)

- [x] **Node roles/IPs** — cp = `usfr4` (`45.33.110.78`), edge = `usfr3`
      (`192.81.135.223`), worker1 = `usfr2` (`192.155.85.254`). Private-network
      IPs + `PRIVATE_IFACE` still needed for the k3s `--node-ip`/`--flannel-iface`
      flags.
- [ ] **Linode Object Storage** — create two buckets and one access-key pair;
      fill `REPLACE_BUCKET_CNPG`, `REPLACE_BUCKET_LOKI`, `REPLACE_REGION`
      (endpoint `https://<REPLACE_REGION>.linodeobjects.com`) in
      `deploy/apps/base/data/*/cluster.yaml`, `deploy/infrastructure/identity/cluster.yaml`,
      `deploy/infrastructure/observability/loki.yaml`. Keys go into the
      `objectstore-backups` / `objectstore-loki` Secrets (bootstrap prompts).
- [ ] **GHCR read-only token** → `ghcr-pull` (prod + dev ns; bootstrap prompts).
- [ ] **App/identity secrets** (bootstrap prompts, both prod + dev ns):
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

- [x] **Canary cookie token** — `8640c2f1285bf39d0323bbe540e51694`.
      The canary is gated by a **host-only cookie** `fm_canary=<token>` on
      `x9bc433.win`, NOT a client-sent header. The `canary-cookie-gate`
      EnvoyFilter (`deploy/infrastructure/mesh-policies/canary-cookie-gate.yaml`)
      validates the cookie at the gateway, strips any client-supplied
      `x-fm-canary` header, and re-injects `x-fm-canary: <token>` (the SAME
      secret) on a match; each active canary's dedicated route (see below) still
      matches that secret header value and routes it to `<svc>-canary`. To reach
      the canary, set the cookie (no `Domain` attribute, so it is host-only and is
      NOT sent to `kc.`/`grafana.` subdomains).

      **Easy toggle (preferred).** The same EnvoyFilter now answers two
      server-side cookie-setter endpoints entirely at the gateway (INSERT_BEFORE
      jwt_authn, so they never hit OPA or the app):
      - `https://x9bc433.win/canary/on?t=8640c2f1285bf39d0323bbe540e51694`
        sets the cookie and 302-redirects to `/` (a wrong/absent `t` fails closed:
        redirects with NO cookie set).
      - `https://x9bc433.win/canary/off` clears the cookie and redirects to `/`.
      The cookie is set SERVER-SIDE, so it is **HttpOnly** (JS cannot read it,
      closing the XSS-harvest exposure of the `document.cookie` approach); it stays
      `Secure` + `SameSite=Lax`.

      The manual way still works but the endpoint is preferred — e.g. in the
      browser console on `https://x9bc433.win/`:
      `document.cookie = 'fm_canary=8640c2f1285bf39d0323bbe540e51694; path=/; secure; samesite=lax'`
      then reload; anyone without the cookie stays on stable.

      **The secret value appears in these files that must stay byte-identical:**
      1. `deploy/infrastructure/mesh-policies/canary-cookie-gate.yaml` (the EnvoyFilter Lua),
      2. `deploy/infrastructure/gateway/canary/search-canary.yaml` (the search-canary route match),
      3. `deploy/infrastructure/gateway/canary/frontend-canary.yaml` (the frontend-canary route match),
      4. `deploy/infrastructure/gateway/canary/searchui-canary.yaml` (the searchui-canary route match),
      5. this doc.

      Note: `leads-canary` is INTERNAL (east-west) and its route
      (`deploy/infrastructure/mesh-policies/canary/leads-canary-eastwest.yaml`)
      deliberately does **not** carry the secret — it presence-matches
      `x-fm-canary` (regex `.+`), because the marker is already non-forgeable in
      the mesh (the gateway injects it only for a valid cookie). So east-west VS
      route files are NOT part of this byte-identical set.

      There is **one route file per canaried service** (`canary/<svc>-canary.yaml`),
      so the count grows by one for every service armed as a canary — that
      duplication is the deliberate cost of the fail-safe **secret-in-route**
      design: keeping the secret in each route is the floor, so if the EnvoyFilter
      detaches, the route still requires the secret header and external callers
      fail safe to stable (never fail-open). Each `canary/<svc>-canary.yaml` is a
      separate HTTPRoute present in the gateway kustomization **only while that
      canary is active** (`canary-if-exists-else-stable`), so an idle canary never
      503s the cookie. The token is a capability/obscurity value (same class as the
      Apollo webhook secret-in-path), committed in-manifest and **rotatable** by
      editing all the files listed above (and any future gateway
      `canary/<svc>-canary.yaml`; east-west VS files stay secret-free and are NOT
      in this set), then re-deploying. Only ever build the canary from a TRUSTED branch — the
      canary serves feature-branch JS same-origin and can read the prod Keycloak
      session of anyone who reaches it (see the frontend-canary deployment
      trust-boundary note).

## Deferred hardening

- [ ] **Base-image digest pinning** — resolve version tags to `@sha256:`.
- [ ] **CI push-to-main** — `build-images.yml` commits image pins to `main`
      with `GITHUB_TOKEN`; ensure branch protection allows it (or supply a
      dedicated bot PAT) and that a required reviewer gates the `production`
      environment on `release-prod`.
- [ ] **Mail mailbox ownership** — any user with the `mail` grant sees all
      mailboxes; revisit in OPA data if isolation is needed.
