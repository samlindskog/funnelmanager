# Authentication & Authorization

**Keycloak is the sole identity provider** (one realm, `funnelmanager`): humans
authenticate with the OIDC auth-code + PKCE flow, machine/AI agents are
confidential clients using client credentials, and delegation between services
uses **RFC 8693 token exchange**. Authorization is enforced by the platform —
in the k3s deployment, Istio validates JWTs and mTLS workload identities and
routes every request through **OPA (ext_authz)**; the applications themselves
only *consume* identity through the shared `fm_runtime` middleware.

Every request carries **two identities**:

1. **The originating principal** — a JWT (`sub`, `preferred_username`,
   `realm_access.roles`) issued by Keycloak. On internal hops the token is
   *exchanged*, never forwarded: the target-service audience replaces the
   caller's, and `azp` records the client that performed the exchange.
2. **The calling workload** — the mTLS SPIFFE identity of the pod making the
   call (Envoy forwards it in `x-forwarded-client-cert`; `fm_runtime` parses
   it for logging, OPA enforces on it).

## Per-hop audiences and token exchange

Each service only accepts JWTs whose `aud` names it (`search`, `leads`,
`mail`, `mcp` — checked by Istio/OPA and again by `fm_runtime`). To call
another service, a client exchanges its inbound token at Keycloak for a
token with the target's audience:

- The realm defines client scopes `svc-<service>` (an audience mapper per
  service). A client may exchange toward a target **only if it holds that
  optional scope** — the one-hop pairing allowlist lives in the realm:
  `search → leads`, `mcp → leads`, `agent-example → mcp`. Anything else
  (e.g. `mail → leads`) is refused by Keycloak itself.
- `fm_runtime`'s `TokenBroker` performs the exchange
  (`grant_type=token-exchange`, `audience=<target>`, `scope=svc-<target>`)
  and caches results per (subject, audience) — never once per request.
  Calls made without an inbound principal (background jobs) use a
  client-credentials token: the service acts as itself. Detached jobs
  (streaming search/enrich in `LeadsClient`) capture the principal's subject
  token but downgrade to the service's own client-credentials identity once
  it expires mid-job — the `search`/`mcp` service accounts hold the
  `internal-service` realm role, whose grants cover only the leads API.
  A configured token endpoint with a blank `FM_OIDC_CLIENT_SECRET` refuses
  to start (`RuntimeSettings.validate()`) — passthrough exists only for
  bare dev with no IdP at all.
- Keycloak 26.2's standard token exchange keeps the subject (`sub`,
  `preferred_username`, roles) and stamps the exchanging client into `azp`.
  It does **not** emit nested RFC 8693 `act` chains; `fm_runtime` parses
  `act` when present (future-proof), and OPA combines `azp` with the mTLS
  workload identity for delegation constraints.

## What each application does (fm_runtime)

- `PrincipalMiddleware` parses the forwarded JWT into a `Principal`
  available to every handler (contextvar + `request.state`). In-mesh it
  parses without verifying (Istio `RequestAuthentication` already did);
  outside the mesh (docker-compose dev) `FM_JWT_VERIFY=true` turns on full
  JWKS verification. Wrong/missing audience ⇒ 401. A JWKS outage is **not**
  a 401: the cache serves stale keys through it, rate-limits refetches
  (unknown `kid`s cannot trigger a fetch per request), and when no cached
  key matches the middleware answers a retryable 503 — clients keep their
  sessions instead of being force-logged-out by a Keycloak restart.
- With `FM_ENFORCE_GRANTS=true` (set in both compose files) the middleware
  also applies the **role grants** — the same
  `{service, methods, path_prefix}` rule OPA's `grant_ok_for` enforces in
  the mesh, keyed by the JWT's realm roles. Grant data comes from
  `FM_ROLE_GRANTS` (inline JSON) or `FM_ROLE_GRANTS_FILE` (a full OPA
  `data.json` works); unset, the built-in default mirrors
  `deploy/policy/data.json` (`admin` = everything, `internal-service` =
  leads only). No covering grant ⇒ 403. This keeps compose deployments
  fail-closed per request even though they run no OPA.
- Routes annotated `@anonymous("reason")` tolerate an absent principal.
  That annotation is the **single source of truth** for the
  public-anonymous allowlist (exported with `python -m fm_runtime.export`,
  consumed by the OPA policy data):
  - leads: `POST /api/leads/webhooks/apollo[/{secret}]` — Apollo cannot send
    bearer tokens; constant-time secret compare, 503 when unconfigured.
  - mail: `GET /api/mail/oauth/callback` — Google redirect; single-use
    state row (10-min TTL) bound to the user who minted it.
  - every service: `/healthz`, `/readyz`, `/metrics`, legacy `/health` —
    kubelet probes and Prometheus scrapes are cluster-internal.
- `GET /api/{service}/whoami` (installed by `fm_runtime.install` on every
  backend) echoes the acting principal (sub, username, roles). It is
  deliberately **not** `@anonymous`: answering requires a valid JWT plus a
  covering role grant, so the hub uses it as its app-discovery probe — a
  tile is shown only when the user's token gets a 2xx from the app's
  `probe` URL, keeping the hub's tile list derived from real enforcement
  instead of duplicated config.
- All outbound internal calls go through the broker-backed clients
  (`LeadsClient` in search, `LeadsBackendClient` in mcp) — no service makes
  an internal call outside this middleware. Trace headers
  (`traceparent`/`b3`/`x-request-id`) propagate on every hop.

## How each client type interfaces

**Browser (hub + search + mail apps):** the `frontend` public client,
auth-code + PKCE (`frontend/src/oidc.ts`, mirrored in `mailui/src/oidc.ts`).
Tokens live in localStorage (`fm_oidc_*`), shared same-origin by both apps;
whichever app is open refreshes the session. Access tokens carry
`aud: [search, mail]`; the gateway (Istio) validates them, OPA authorizes
per route. Sign-out is RP-initiated logout at Keycloak.

**Internal services:** exchange-then-call (above). A browser action that
reaches leads is authorized at the gateway (frontend token, aud search),
then again at leads (exchanged token, aud leads) — defense in depth;
internal ≠ trusted.

**Machine/AI agents:** confidential clients (see `agent-example`) obtain
client-credentials tokens (`aud: mcp`) and call the MCP server; each tool
call carries the token (`session_token` argument or Authorization header).
The MCP server exchanges it toward leads, so the agent acts as its own
principal with `azp: mcp`, and OPA can constrain the delegation.

**Admin/user management:** the Keycloak console (linked from the hub for
admin-role users). There is exactly one human principal today (`admin`,
realm role `admin`); adding users/roles is realm + OPA-data change, not a
code change.

## Dev vs prod

docker-compose dev runs Keycloak in dev mode importing
`deploy/keycloak/realm-funnelmanager-dev.json` (dev-only secrets,
`admin`/`admin`). Backends verify JWTs locally (`FM_JWT_VERIFY=true`)
because there is no mesh in compose. The issuer is pinned to the
browser-facing URL (`http://localhost:8080/realms/funnelmanager`) while
backends dial the `keycloak` container for token/JWKS endpoints, so `iss`
claims stay consistent — which also requires
`KC_HOSTNAME_BACKCHANNEL_DYNAMIC=false`: a dynamic backchannel hostname
would stamp `iss=http://keycloak:8080` on exchanged tokens and break the
pinned-issuer check. (`--import-realm` only imports a realm that does not
exist yet — after editing the realm file, drop the Keycloak volume or apply
the change in the console.)

The prod compose deployment **requires** `KEYCLOAK_REALM_FILE` (no
default): the tracked realm file is the dev realm — an `admin`/`admin`
application user and published client secrets — and must never be imported
into prod. Start from
`deploy/keycloak/realm-funnelmanager-prod.example.json`, which imports no
human users. In the k3s deployment Istio + OPA enforce everything and the
apps run with verification off.
