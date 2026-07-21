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

## Deferred hardening

- [ ] **Base-image digest pinning** — resolve version tags to `@sha256:`.
- [ ] **CI push-to-main** — `build-images.yml` commits image pins to `main`
      with `GITHUB_TOKEN`; ensure branch protection allows it (or supply a
      dedicated bot PAT) and that a required reviewer gates the `production`
      environment on `release-prod`.
- [ ] **Mail mailbox ownership** — any user with the `mail` grant sees all
      mailboxes; revisit in OPA data if isolation is needed.
