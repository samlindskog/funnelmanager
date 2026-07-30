# Funnel Manager — target architecture (k3s + Istio + OPA + Keycloak)

What the restructure built, how a request flows, and the day-2 runbooks.
Companions: `docs/authentication.md` (identity model in depth),
`deploy/CONVENTIONS.md` (names/resources/scheduling),
`deploy/bootstrap/README.md` (bring-up + budget), `PLACEHOLDERS.md`
(values you must supply).

## Shape

Three k3s nodes (`--disable traefik --disable servicelb`, flannel + the k3s
NetworkPolicy controller):

- **cp** (4GB) — k3s server (SQLite), tainted; runs istiod (toleration) and
  an OPA DaemonSet member.
- **edge** (4GB) — the DMZ: the Istio ingress gateway (hostPorts 80/443 —
  the only public ports anywhere) and Keycloak. Tainted `role=edge`.
- **worker1** (8GB) — every app pod, CNPG Postgres clusters, Mongo, Milvus
  (+etcd/minio), and the observability stack. More workers can join —
  nothing assumes single-worker scheduling (anti-affinity is pre-wired;
  CNPG clusters flip to `instances: 2` when a second worker exists).

Everything is declarative under `deploy/`, reconciled by **Flux**
(chosen over ArgoCD: native Kustomize + Helm controllers, no UI server to
host on a 4GB node, `flux bootstrap` binds this same repo) in a dependency
graph with health gates: namespaces → cert-manager/Gateway-API
CRDs/CNPG → Istio → OPA → mesh policies → identity → gateway →
observability → apps.

Identity: **Keycloak** (realms `funnelmanager`, `funnelmanager-dev`),
humans via auth-code+PKCE, services/agents as confidential clients,
**RFC 8693 token exchange per internal hop** with the realm's `svc-<target>`
scopes as the exchange allowlist. Apps consume identity through
`libs/fm_runtime` (principal middleware, cached exchange broker, JSON logs,
probes, `@anonymous` annotations).

Authorization: **OPA** DaemonSet on all three nodes, called by every Envoy
through ext_authz (`internalTrafficPolicy: Local` → node-local decisions),
loading one git-generated bundle (`deploy/policy`). Default deny. Beneath
it: Istio STRICT mTLS + a DENY-without-principal policy (anonymous
allowlist excepted), and default-deny NetworkPolicies with one policy per
dedicated dependency `(app → dependency)` admitting only its owner.

## Request flows

**(a) Public user request** — browser → `https://<domain>/api/search/...`:
edge gateway terminates TLS (cert-manager/Let's Encrypt), Istio
`RequestAuthentication` validates the Keycloak JWT (aud `search`,
forwarded onward), the gateway's OPA check runs (host → env, route →
service, anonymous or token+grant). The HTTPRoute forwards to
`search.prod` over mesh mTLS; search's sidecar re-runs OPA with full
workload context (gateway → search allowed); `fm_runtime` hands the
principal to the handler, which keys history rows on
`preferred_username`.

**(b) Internal hop with token exchange** — search must call leads: the
`LeadsClient` asks the broker for a leads-audience token; the broker
exchanges the *subject* token at Keycloak (`grant_type=token-exchange`,
`audience=leads`, `scope=svc-leads`; cached per subject+audience). The new
token keeps the principal (`sub`, roles) and stamps `azp: search`. At
leads' sidecar, OPA checks: caller workload ∈ {search, mcp}, issuer/aud
correct, `azp` ∈ {search, mcp}, role grant covers the path. The original
browser token would fail two of those — hops exchange, never forward.

**(c) Anonymous webhook** — Apollo POSTs
`/api/leads/webhooks/apollo/<secret>`: gateway OPA allows it via the
anonymous allowlist (no JWT exists), routes to leads; leads' sidecar OPA
allows the gateway workload **only** on the webhook prefix; the app then
does its constant-time secret compare (503 if unconfigured). The same
Apollo delivery cannot reach any other leads path.

**Agents** — the `agents` confidential client exchanges the human's token to
`aud: mcp` (minting `fm_origin=agent`) and calls `/mcp` (workload-gated by OPA
`callers.mcp`). The MCP server exchanges onward toward leads/search/… (`azp:
mcp`); the acting principal's *service account* / user must hold a granted
realm role — same rules as humans, by decision.

## Agent-team services (jobs, agents, agentsui)

The platform runs **runtime AI agents** that complete a user's task by
driving the product's own APIs through MCP. Three new workloads, same
conventions (dir = compose service = container/DNS = image = `/api/{name}`):

- **`jobs`** (`:8005`, internal/loopback like `mcp`, own dedicated Postgres
  `jobs-db` / db `funnelmanager_jobs`) — the one place that knows every running
  job. Subscribes to each producer's internal NDJSON stream
  (`GET /internal/jobs/v1/stream`, exchanging → the producer's audience) and
  persists job state; proxies `pause|resume|cancel` to the owning app's
  `/internal/jobs/v1/*`. Reaching those `/internal/jobs/*` endpoints is the
  machine-only **`jobs-internal`** realm role, held by the `jobs` client's
  service account (`azp=jobs`, client-credentials) — never a human; the acting
  human rides as audit metadata. The producer list is **config-driven**
  (`JOBS_PRODUCERS`, `name=base_url` pairs): v1 producers are **`search` +
  `agents`**, both now wired
  (`search=http://search:8000,agents=http://agents:8006`) — add `mail`/others
  later with no code change. Exposes MCP tools (`/api/jobs/mcp/v1/*`). Wired
  into `docker-compose.{dev,prod}.yml` (with a dedicated `jobs-db` container,
  the `mail-db` pattern) and k3s (`deploy/apps/base/jobs`,
  `deploy/apps/base/data/jobs-db`, netpol `jobs`/`jobs-db`, both overlays); the
  `jobs→search`/`jobs→agents` caller edges + `jobs→jobs-db` TCP pairing live in
  `deploy/policy/data.json`.
- **`agents`** (`:8006`, `/api/agents/*`) — a pydantic-ai backend that runs
  runtime AI agents. **Browser-facing** (gateway/nginx-routed like search/mail;
  gated by the `agents-access` realm role) and a **v1 job producer** (each run
  is a job on `/internal/jobs/v1/*`, so `jobs` subscribes to it). Each agent is
  an **MCP client** acting under the human's identity via exchange with
  `fm_origin=agent`; it acts **exclusively** through MCP tools (no direct
  backend calls). Backed by its own dedicated Postgres `agents-db`
  (db `funnelmanager_agents`). **Now wired** into
  `docker-compose.{dev,prod}.yml` (dedicated `agents-db` container) and k3s
  (`deploy/apps/base/agents`, `deploy/apps/base/data/agents-db`, netpol
  `agents`/`agents-db`, both overlays); its egress edges are `agents→mcp`
  (exchange) and the `agents→agents-db` TCP pairing; the browser reaches it via
  the gateway `/api/agents/` route + `azp_allow.agents` listing `frontend`.
- **`agentsui`** (`/agents/`) — a standalone React/MUI app (mirrors `mailui`):
  own container behind nginx `/agents/`, shares the hub Keycloak session
  (localStorage `fm_oidc_*`); a `WEB_APPS` hub tile (same tile for everyone —
  principle 1). Wired in `docker-compose.{dev,prod}.yml` and k3s
  (`deploy/apps/base/agentsui`, netpol `agentsui`, both overlays); the gateway
  routes `/agents/` to it.
- **`mail` (Phase 5 expansion)** — beyond the archive, `mail` becomes a **full
  multi-domain mailbox client** (compose/send, inbox/sent/threads, search;
  full-backup-on-connect behind the >2 GB Principle-4 gate) plus a **campaigns**
  feature: campaigns draw recipients from one or more saved search result lists
  (`campaign_sources`, continuable), send from the initiating user's own
  connected mailboxes across domains (`balanced|sequential`, per-domain daily
  cap), and record every send in `campaign_messages` — the basis for
  within-campaign dedupe, cross-campaign suppression, and the contacted set at
  `GET /api/mail/contacts/contacted` (read by `search`'s `exclude_contacted`
  filter over the `search → mail` edge). Campaigns are cross-user visible
  (principle 1). A **mail MCP surface** (`/api/mail/mcp/v1/*`, distinct from UI
  routes, reached over the `mcp → mail` edge via `MAIL_BACKEND_URL`) exposes
  campaign list/get/start/continue, the contacted-set read, and inbox
  list/get-thread/send so runtime agents can run the full search→campaign flow.
  A large campaign is itself a Principle-4 expensive action; an agent-launched
  one hits the mint/verify approval gate above.

**Identity edges these add** (realm `svc-*` scopes + OPA `azp_allow` +
`grants.py` `SVC_EXCHANGE_SCOPES`, kept in lockstep, provable with
`python -m fm_runtime.export`): `agents → mcp`; `mcp → {search, jobs, mail}`
(MCP's tool fan-out — MCP **can start searches**, funneled search → leads, so
"only leads talks to Apollo" still holds; each upstream is a `BackendClient`
reached via its env URL, e.g. `MAIL_BACKEND_URL=http://mail:8004` for the mail
tool module's `/api/mail/mcp/v1/*` calls); `jobs → {search, agents}`;
`search → mail` (the `exclude_contacted` contacted-set read). Each new edge is
paired at every layer — realm `svc-*` scope, `azp_allow` + `callers` in
`data.json`, `SVC_EXCHANGE_SCOPES` in `grants.py`, and a NetworkPolicy egress↔ingress
pair (`mcp`/`search` → `mail:8004`). The `fm_origin`
claim (default `user`, minted `agent` by the `agents` client, then carried
across every exchange by a Keycloak script mapper that copies the subject
token's `fm_origin` forward — feature `scripts`, `deploy/keycloak/providers/`)
rides every hop so records read "alice (via agent)".

**Cross-cutting rules baked in at Phase 0** — *Principle 1*: no per-user data
hiding in app code; Keycloak (audience + role) is the only gate, so history,
inboxes, and campaigns are cross-user visible (writes stay attributed).
*Principle 4*: expensive actions (mailbox backup > 2 GB default, large
backfills/searches/campaigns) **estimate first and return
`409 confirmation_required`** with a `confirm` token before running — a shared
`fm_runtime` helper (`libs/fm_runtime/confirmation.py`). A human re-invokes with
`confirm=true`; a runtime **agent can never self-confirm** — the origin-aware
gate rejects `confirm=true` when `fm_origin=agent` on an over-threshold action,
pausing the run and surfacing a *pending approval* to the initiating human (in
`agentsui`). The human approves out-of-band, the `agents` service **mints** a
signed **human-approval token** (HMAC over the shared
**`FM_CONFIRM_APPROVAL_SECRET`**), and the guarding service (**`mail`**, **`leads`**)
**verifies** that token before proceeding. The secret is provisioned identically
on `agents` (mint) + `mail` + `leads` (verify) — compose defaults in dev, the
`fm-approval` Secret (`secret` key) via `secretKeyRef` in k3s. **Fail-closed:** if
it is unset or mismatched, agent-initiated over-threshold actions are permanently
blocked. *Versioning*: `/api/{service}/mcp/v1/*` and `/internal/{domain}/v1/*` are
additive-within-version; a breaking change is a new version, never a silent
repurpose.

## Runbooks

**Rotate Keycloak signing keys:** Realm settings → Keys: add a new
RSA key provider with a higher priority; keep the old key *enabled but
passive* so existing tokens verify until expiry (access tokens live 5 min);
after the longest session TTL, delete the old provider. Consumers (Istio
JWKS, fm_runtime in dev) refetch JWKS automatically (Istio ~20 min cache;
OPA reads claims only). No app restarts. For an emergency revocation,
delete the old key immediately and accept a wave of 401s → re-logins.

**Add a service `foo`:** (1) realm: confidential client `foo` (+
`svc-foo` client scope; grant it as optional scope to each caller allowed
to exchange toward it). (2) app: install `fm_runtime`
(`install(app, service="foo")`), annotate any anonymous routes. (3)
manifests: copy an app base dir (SA `foo`, Deployment, Service, netpol),
add to both overlays + a secret `fm-oidc-foo`. (4) policy `data.json`:
`callers.foo`, an `azp_allow.foo` if constrained, grants if a new role is
involved; add an HTTPRoute only if it is public. (5) CI builds it via the
deploy-workflow matrix. Nothing else changes.

**Add an anonymous endpoint:** (1) annotate the route in code —
`@anonymous("reason")` (this is the source of truth; the OpenAPI gains
`x-public-anonymous`). (2) mirror it in `deploy/policy/data.json`
`config.anonymous` (diff against `python -m fm_runtime.export`). (3) if it
must be reachable from the internet, confirm the gateway route covers the
path and add it to the `require-principal` DENY `notPaths` in
`deploy/infrastructure/mesh-policies/`. Ship both in one commit so code
and policy cannot drift.

## Deviations & known compromises (deliberate)

- KC 26.2 standard token exchange emits `azp` (single-hop actor), not
  nested `act` chains — OPA combines `azp` with the mTLS workload identity.
- `identity` namespace is unmeshed (Keycloak is the trust root; CNPG jobs
  fight sidecars) — NetworkPolicy-fenced instead.
- The two Postgres clusters share port 5432; OPA's TCP branch matches on
  workload *identities* (SA names), so isolation still holds; L4
  AuthorizationPolicies + NetworkPolicies back it up.
- Static SPA images still run stock root nginx (follow-up:
  `nginx-unprivileged`); base images pin versions, not digests
  (PLACEHOLDERS).
- No service hides data per-user in app code (**principle 1**): any principal
  whose role covers a service sees all of its data — mail sees every mailbox,
  search every history, mail every campaign. Access is gated only by the
  Keycloak audience + role, never by owner-filtering in the app.
