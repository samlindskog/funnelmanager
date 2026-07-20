# PLACEHOLDERS — values you must supply

Everything the restructure could not infer from the repo. In manifests,
placeholder hostnames are schema-valid sentinels ending in `.example.com`
(`replace-app`, `replace-dev`, `replace-kc`) — grep for `example.com` and
`REPLACE_` to find every substitution point. Each item lists
where the value is consumed. Nothing here is invented elsewhere — manifests
and configs reference these names symbolically (env vars / secret refs).

## Domains & endpoints

- [ ] **App domain** (`DOMAIN`) — e.g. `funnel.example.com`. Consumed by:
      nginx `server_name`, Gateway API hostnames (Phase 2), CORS origins,
      Google OAuth redirect URI, `PUBLIC_BASE_URL` for Apollo webhooks.
- [ ] **Keycloak hostname** (`KC_HOSTNAME` / `FM_OIDC_ISSUER`) — e.g.
      `https://kc.example.com` → issuer
      `https://kc.example.com/realms/funnelmanager`. Consumed by: Keycloak,
      every service's FM_OIDC_ISSUER, frontend/mailui `/config.js`,
      Istio RequestAuthentication (Phase 2).
- [ ] **Let's Encrypt registration email** — cert-manager ClusterIssuer
      (Phase 2).
- [ ] Re-register the **Google OAuth redirect URI**
      (`https://<DOMAIN>/api/mail/oauth/callback`) on the OAuth client when
      the domain changes.

## Identity

- [ ] **Realm name** — defaulted to `funnelmanager`
      (`deploy/keycloak/realm-funnelmanager-dev.json`). Confirm or rename
      (also update `FM_OIDC_ISSUER` paths and compose/k8s env).
- [ ] **Production realm secrets** — the tracked realm file carries
      **dev-only** client secrets and the `admin`/`admin` user. For prod:
      produce a hardened realm export (strong secrets, real admin password)
      kept OUT of git; point `KEYCLOAK_REALM_FILE` (compose) or the realm
      import Secret (k3s) at it, and mirror the client secrets into
      `FM_OIDC_<SVC>_SECRET`.
- [ ] **Permission model for OPA data** (Phase 2 stop-gate): today exactly
      one human principal (`admin`, realm role `admin`, full access). Define
      any additional roles → service/method/path permissions before the Rego
      data documents are finalized.
- [ ] Production hardening: disable `directAccessGrantsEnabled` on the
      `frontend` client (enabled for dev/test convenience only) and restrict
      its `redirectUris`/`post.logout.redirect.uris` to the real origin.

## Cluster (Phase 2 bootstrap — separate session)

- [ ] **Node addresses** for `cp` (4GB), `edge` (4GB), `worker1` (8GB):
      public IPs, private-network IPs, SSH users.
- [ ] **Linode Object Storage**: bucket names + region + access/secret keys
      for (a) CNPG WAL archive + base backups, (b) Loki chunks. Secret refs
      only; never committed.
- [ ] **GHCR image-pull credential** for the cluster (read-only PAT or
      GitHub App token) → `imagePullSecrets`.
- [ ] **SOPS age key** (or Sealed Secrets key) — generated at bootstrap,
      stored outside git.

## Deferred hardening

- [ ] **Base-image digest pinning** — images pin version tags
      (`python:3.13-slim`, `node:22-alpine`, `nginx:1.27-alpine`,
      `keycloak:26.2`); resolve to `@sha256:` digests (e.g. via Renovate) in
      a follow-up.
- [ ] **Mail mailbox ownership** — decided (Phase 0): any user with the
      `mail` grant sees all mailboxes. Revisit in OPA data if multi-user
      isolation is ever needed.
