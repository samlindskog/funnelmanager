# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Funnel Manager is an Apollo person/company search + enrichment platform with a zero-trust identity architecture: Keycloak is the sole OIDC issuer, every request carries the originating principal as a per-hop-audience JWT (RFC 8693 exchange between services via the shared `libs/fm_runtime`), and authorization is platform-enforced (OPA) on **every** API endpoint, internal or external. The README has the full API reference and env-var table; this file covers the cross-cutting architecture that isn't obvious from any single file.

## Services and the boundary that matters

| Service | Dir | Stack | Data | Faces |
|---|---|---|---|---|
| Keycloak | (compose service; realm in `deploy/keycloak/`) | `quay.io/keycloak/keycloak` | own store (H2 dev / Postgres in k3s) | The browser (OIDC flows) + every service (token endpoint/JWKS) |
| fm_runtime | `libs/fm_runtime/` | Python lib (installed into every backend) | — | Not a service: principal middleware, token-exchange broker, logging, probes |
| Search backend | `search/` | FastAPI, SQLAlchemy (async, asyncpg) | Postgres | The browser/API clients (via nginx) |
| Search UI | `searchui/` | React 19, MUI 9, Vite 8, TS | — | The browser — standalone search app at `/search/` (nginx-proxied, own container; extracted from `frontend/`, mirrors `mailui`/`agentsui`); hub tile via `WEB_APPS` |
| Leads backend | `leads/` | FastAPI, Motor, OpenAI, pymilvus | MongoDB + Milvus | The search backend + MCP server (token-authorized) |
| Mail backend | `mail/` | FastAPI, SQLAlchemy (async, asyncpg), httpx→Gmail | Postgres (dedicated `mail-db` container) | The browser (via nginx, `/api/mail/*`) + Google OAuth/Gmail REST |
| Mail UI | `mailui/` | React 19, MUI 9, Vite 8, TS (no router) | — | The browser — standalone app at `/mail/` (nginx-proxied, own container); hub tile via `WEB_APPS` |
| MCP server | `mcp/` | Python MCP SDK (FastMCP, streamable HTTP) | — | Internal MCP clients — never via nginx |
| Jobs backend | `jobs/` (NEW) | FastAPI, SQLAlchemy (async, asyncpg) | Postgres (own db `funnelmanager_jobs`) | Internal only — subscribes to producers' job-event streams; MCP tools; never via nginx (prod loopback like mcp) |
| Agents backend | `agents/` (NEW) | FastAPI, pydantic-ai (MCP client) | Postgres | The browser (via nginx, `/api/agents/*`) — runs runtime AI agents that act **exclusively** through MCP tools |
| Agents UI | `agentsui/` (NEW) | React/MUI (mirrors `mailui`) | — | The browser — standalone app at `/agents/` (own container, nginx-proxied); hub tile via `WEB_APPS` |
| Frontend | `frontend/` | React 19, MUI 9, Vite 8, TS | — | The browser — **hub only** (landing → Keycloak sign-in → apps; admins get a Keycloak-console link). The search app moved out to `searchui/` (served at `/search/`) |

**Naming convention:** one short name per backend is used **everywhere** — source dir, compose service, container/DNS name, GHCR image (`.../{name}`), and API prefix all match: `search` (`/api/search`), `leads` (`/api/leads`), `mail` (`/api/mail`), `mcp` (`/mcp` — the MCP protocol mount, no `/api`, never nginx-routed), `jobs` (`:8005`, internal), `agents` (`:8006`, `/api/agents`), `searchui` (`/search/`), `agentsui` (`/agents/`). Every public API path is `/api/{service}`. Dev containers are `funnelmanager-{service}-1`.

**Agent-team architecture (jobs, agents, campaigns).** Beyond the original stack, the platform runs **runtime AI agents** that complete a user's task by driving the product's own APIs (deep spec: `docs/agent-build-plan.md`). Both new backends are fully wired into `docker-compose.{dev,prod}.yml` (dedicated `jobs-db`/`agents-db` Postgres → dbs `funnelmanager_jobs`/`funnelmanager_agents`, the `mail-db` pattern) and k3s (`deploy/apps/base/{jobs,agents}` + `.../data/{jobs-db,agents-db}` + netpol, both overlays); both producers are configured (`JOBS_PRODUCERS=search=http://search:8000,agents=http://agents:8006`). `agents` (`:8006`, `/api/agents`) is **browser-facing** (nginx-routed like search/mail; the `frontend` client mints an `agents` audience, so `azp_allow.agents` lists `frontend` alongside `jobs`; its standalone SPA `agentsui` is served same-origin at `/agents/`, `mailui` pattern, hub `WEB_APPS` tile); `jobs` (`:8005`) is **internal-only** (prod loopback like mcp). The caller edges (`jobs→search`/`jobs→agents`) and TCP pairings (`jobs→jobs-db`/`agents→agents-db`) live in `deploy/policy/data.json`. The governing rules are the numbered **program principles (P1–P11)** below.

The MCP server (`:8003/mcp`) takes a **per-tool-call token** (the acting principal's, aud `mcp`): every tool accepts `session_token` (an `Authorization: Bearer` header on `/mcp` also works; the explicit argument wins — see `effective_token()` in `mcp/app/tools/_shared.py`) and exchanges it (RFC 8693) for the **target upstream's** audience per call — one `BackendClient` per backend it fans out to (`leads`/`search`/`jobs`/`mail`), each reached at its own internal URL (`LEADS_BACKEND_URL`, `SEARCH_BACKEND_URL=http://search:8000`, `JOBS_BACKEND_URL=http://jobs:8005`, mail). The one-hop exchange edges `mcp→search` / `mcp→jobs` / `mcp→mail` (svc-scopes in the realm, `azp_allow` in `data.json`, `SVC_EXCHANGE_SCOPES` in `grants.py`) authorize those hops. Tokenless calls only work when `MCP_SHARED_LOGIN_FALLBACK=true` (dev compose) — they act as the MCP server's own service identity via client credentials. Tools span read-only inspection over leads (`leads_stats`, `recent_leads`, `get_leads`, `similarity_search`, all `readOnlyHint`) and the write/awareness surface described above (search-started work funnels through search → leads, so only leads ever talks to Apollo; `jobs` tools observe running work). The transport has a Host-header allowlist (`MCP_ALLOWED_HOSTS`) — internal clients dial `mcp:8003`. Prod publishes it on loopback only (`127.0.0.1:8003`).

**The mail service** (`mail/`, `:8004`) archives every message of OAuth-connected Gmail/Workspace mailboxes into a **dedicated Postgres container** (`mail-db`, database `funnelmanager_mail` — the service can also create the database itself when pointed at any Postgres) and sends mail via the Gmail API (scopes `gmail.readonly` + `gmail.send`; only the mail service talks to Google). Sync is a background loop (`app/sync.py`): per-mailbox newest-first backfill with a persisted page token, plus `history.list` increments anchored at connect time (deletions flag `is_deleted`, never delete rows — the archive outlives the mailbox). Auth follows the standard principal flow (mail-audience JWT) with two exemptions annotated `@anonymous`: the probes and `GET /api/mail/oauth/callback`, which is instead validated by a single-use state row bound to the initiating user (minted by `/api/mail/oauth/url`). Mailbox refresh tokens live in that database in plaintext. The mail UI is a **standalone app** (`mailui/` — React + MUI, deliberately shares no code with `frontend/`) built with Vite `base: '/mail/'` and served by its own container behind nginx's `/mail/` location; same-origin serving is what lets it share the hub's Keycloak session from localStorage (`fm_oidc_*` keys; unauthenticated → redirect to Keycloak). It appears on the hub only as a `WEB_APPS` tile (`/mail/`). Planned-but-not-built: semantic inbox querying and MCP tools over this store.

**The core architectural rule: only the leads backend ever talks to Apollo, and only the leads backend holds `APOLLO_API_KEY`.** The search backend reaches Apollo functionality exclusively through `search/app/leads_client.py` (`LeadsClient`) calling `LEADS_BACKEND_URL`. The browser never calls the leads backend directly — nginx does not expose it (except Apollo webhooks). When adding an Apollo-touching feature, the path is always: frontend → search backend router → `LeadsClient` → leads backend → Apollo. Do not shortcut this.

## Program principles (P1–P11)

The platform runs runtime AI agents that complete a user's task by driving the product's own APIs. Eleven numbered principles govern the whole program (deep spec: `docs/agent-build-plan.md`; auth detail: `docs/authentication.md`; architecture: `docs/architecture.md`). Each is tagged with how strongly it is enforced today. **Before touching roles, auth, streams, the Apollo path, or the mesh, re-read P1, P5, P6, P7, P8, and P10 — they are the non-negotiables.**

**Legend.** **enforced** — a machine (OPA, `fm_runtime` middleware, a unique index, CI, the type checker) actually holds this today; a violation fails closed. **partial** — the mechanism exists but coverage is incomplete, or it is enforced in one place and honored by convention elsewhere. **aspirational** — intended direction; not yet enforced anywhere; do not rely on it holding until it is promoted.

### Non-negotiable invariants (read first)

*(Surface the load-bearing rules before the long sections. Each links to its full principle below.)*

1. **Only `leads` holds `APOLLO_API_KEY` and only `leads` talks to Apollo.** Every Apollo/Mongo/Milvus path funnels through `leads`. (P5)
2. **Once an NDJSON response has started, never raise** — emit `{"type":"error","detail":…}` as a stream line instead. A raise resets the connection and kills sibling streams sharing the origin. (P8)
3. **Internal calls exchange, never forward** the caller's token; every service accepts only JWTs whose `aud` names it. (P6)
4. **`@anonymous` is the only anonymous allowlist** — a route tolerating no principal must be annotated `fm_runtime.anonymous(reason)`. (P6)
5. **A principal needs the per-service `-access` role or OPA / `fm_runtime` grants return 403.** Authorization is platform-enforced, never re-implemented in app code. (P1, P10)
6. **Run `python -m fm_runtime.export --check deploy/policy/data.json --realm …` before and after any role/scope/anonymous change** — it proves the grants ↔ policy ↔ realm legs are in lockstep. **Caveat:** it does **not yet** cover the `@anonymous` allowlist (that export is dump-only, never wired into `--check`), so the anonymous leg can still drift until it is — see P6/P7 drift #13. (P7)

### P1 — Per-service access; uniform functionality; cross-user visibility · access gate **enforced** / uniform visibility **partial (convention)**

**Split enforcement (per the legend).** This principle has two legs of different strength: **(a) the per-service `-access` gate is `enforced`** — a machine (OPA / `fm_runtime` grants) fails closed if a principal lacks the role. **(b) the uniform-visibility / no-per-user-hiding rule is `partial` — held by convention:** nothing fails closed if a dev adds a `WHERE username = current_principal` reads filter to a router (today reads are cross-user only because the code chooses to be, e.g. `_get_search_any`). Leg (b) is enforced socially by P10 review, not by a mechanism — treat re-introducing a reads filter as a defect a reviewer must catch.

**Enforced by (leg a):** `libs/fm_runtime/fm_runtime/grants.py` (`_DEFAULT_ROLE_GRANTS`), `deploy/policy/data.json` (`funnelmanager.roles`), the Keycloak realms, OPA in the mesh / `FM_ENFORCE_GRANTS` in compose. **Leg b** has no enforcing machine.

Access is gated **per service** by a distinct realm role — `search-access`, `mail-access`, `jobs-access`, `agents-access` (plus `admin` = composite of the four, `internal-service` = leads-only, machine-only `jobs-internal`). A principal may call a service's API **only if it holds that service's access role**; otherwise the platform returns 403.

**The rule for future code:** the realm role gates *whether you may call the API*, **not which rows you see**. Within a service, every principal that has access sees the **same features and the same data**. There is **no per-user data hiding in app code** — no `WHERE username = current_principal` filter in a router. Writes are *attributed* (`owner`/`origin`/`actor`) but never *hidden*. If a future feature genuinely needs narrower access, express it as a **new realm role + grant** (mesh-enforceable), never an in-router conditional.

**One sanctioned exception:** *destructive ownership* — delete/mutate-my-own-row gates (search delete, campaign ownership) may 404 another user's row. This is a deliberate carve-out from "same features," not a reads filter. Keep such gates **minimal and self-documented**; a reads filter smuggled in this way is a P1 + P10 violation.

The roles are encoded in three places that must stay in sync: `fm_runtime/grants.py` `_DEFAULT_ROLE_GRANTS`, `deploy/policy/data.json` `funnelmanager.roles`, and the Keycloak realm roles — proven equal by `python -m fm_runtime.export --check … --realm` (see P7).

▶ **Reconciliation of drift:**
- search `_get_owned_search` / `delete_search`, mail account delete, and the frontend/mailui "cannot delete — owned by X" affordances are the **sanctioned destructive-ownership exception** → *prose corrected here to name the exception explicitly* (previously "same features" read as absolute).
- The **dead `user.role` field** copied into `mailui`/`agentsui` (`App.tsx`) that is never read → **fix code** (remove the vestigial field) — flagged, low priority.

### P2 — Versioned, distinct MCP & internal surfaces · **partial**

**Enforced by:** route prefixes in `search/app/routers/mcp.py`, `jobs/app/routers/mcp.py`, `mail/app/routers/mcp.py`, `*/internal_jobs.py`; tool registration in `mcp/app/tools/*`.

*(This is the previously-undefined "principle 2.")* MCP-facing and internal routes are **versioned and kept distinct from UI routes**: `/api/{service}/mcp/v1/*` for the MCP tool surface, `/internal/{domain}/v1/*` for the job-event stream + control. Within a version, changes are **additive only** (new endpoints, new tools, new **optional** params with defaults); a breaking change ships as a **new version** (`v2`) **alongside** the old, with a published deprecation window (research: ≥ 12 months is the MCP-ecosystem norm). **MCP tool names, schemas, *and descriptions* are a stable contract to agents** — a description/param-semantics edit that changes tool selection is itself breaking. Evolve additively; never silently repurpose a tool. Keep `readOnlyHint`/`destructiveHint`/`idempotentHint` truthful — they are advisory hints, **not** an access-control mechanism (that is P1/P6).

**Future-facing enhancements (research):**
- Add a **golden tool-schema snapshot** check to CI (mirroring `fm_runtime.export --check`): fail on removal of a tool, a newly-required param, or a narrowed type; allow additions. Put the diff logic in a shared `fm_runtime`/export helper, not per-service.
- Publish a per-version **changelog/migration doc** for the MCP + `/internal/jobs` surfaces and **pin** the agents service to a tested tool set, not "latest."

▶ **Reconciliation of drift:**
- **Leads MCP-consumed routes are unversioned** (`/api/leads/stats`, `/recent`, `/similarity-search`, `POST /api/leads`) while search/jobs/mail use `/mcp/v1/*` → **fix code** (either version the leads read surface, or *document in prose* that leads is an internal data source reached natively, not a versioned MCP surface). Recommendation: **document** — leads is deliberately unopinionated and reached only via `mcp→leads`; the stability contract lives at the MCP tool layer, not the leads route layer. Prose here adopts that.
- MCP surface mounted under `/api/search/mcp/v1` sits **inside nginx's public `/api/search/*`** location → *prose corrected*: distinctness is by **path + audience/exchange**, not network isolation. The gateway is the outer wall, not the only wall (OPA re-checks `azp`); still, note the exposure explicitly.
- **Leads' live stream-control surface is undocumented and off-contract** → **fix prose** (decide + document): `POST /api/leads/stream/{id}/control/{pause|resume|cancel}` is a real cost-control lever (it pauses/cancels live Apollo ingest + embedding and backs search's pause/resume story) yet it is neither an `/internal/jobs/v1/*` route nor documented — its rationale lives only in an inline comment. Either document it as leads' **native internal control lever** (reached by `search`, audience `leads`) or fold it under the jobs-control contract; name it in P2's leads item either way.

### P3 — `fm_origin` agent-identity & multi-hop attribution · **partial**

**Enforced by:** the Keycloak `fm-origin-passthrough` script mapper (`deploy/keycloak/providers/`) carried by the `fm-origin` client scope + the `agents` client's hardcoded mint; `libs/fm_runtime/fm_runtime/principal.py` / `tokens.py` (`resolve_origin`); persisted `owner`/`origin`/`actor` columns in `search`/`mail`/`agents`/`jobs`.

A runtime AI agent (the `agents` service, a confidential client) acts via **RFC 8693 token exchange that keeps the human as the subject** (`preferred_username` unchanged = record owner). A propagated **`fm_origin` claim** distinguishes agent-initiated calls (`fm_origin=agent`; **default `user`**); the acting client rides in `azp`. Persist `owner=preferred_username`, `origin=fm_origin`, `actor=azp` so UIs render "alice" or "alice (via agent)". **No synthetic per-user users.** **How origin survives every hop:** the `agents` client **mints** `fm_origin=agent` on the first hop (its own hardcoded-claim mapper, and the *only* client without the passthrough scope); every *subsequent* exchange (`mcp→search`, `search→leads`, …) is done by a different client that carries the **`fm-origin-passthrough` script mapper** — it reads the inbound `subject_token`'s `fm_origin` and carries it onto the newly issued token (default `user`), so agent origin reaches the final audience. The mapper reads only the validated `subject_token`, never a caller-supplied param, so origin **cannot be forged**. This requires `KC_FEATURES=scripts` + the pinned provider JAR (dev/prod compose + k3s ConfigMap; CI `keycloak-provider` job guards it against drift). `fm_runtime` does **not** send `fm_origin` to the token endpoint — it only folds origin into the broker cache key. **Every new exchanging client must get the `fm-origin` scope, or origin silently resets to `user` downstream.**

**The rule for future code:** never mint a per-user service account to "be" a user; never let a service act as itself for user work when a human subject is available. `fm_origin` is an **attribution** claim today; future work (research) should make it an **enforceable policy input** — high-risk/destructive endpoints and MCP write tools may require the P4 human-approval path *because* `fm_origin=agent`.

**Known standards gap (track, don't rebuild):** Keycloak 26.2 standard token exchange records only the *immediate* exchanging client in `azp` and emits **no RFC 8693 `act` chain**, so on `agents→mcp→search→leads` the two-hop-back agent origin survives only in `fm_origin`, and `actor=azp` under-attributes the chain. Mitigation: thread a **stable agent-run / job correlation id** through every hop (fm_runtime already propagates `traceparent`) and persist it, so ancestry is reconstructable from records. Adopt real `act` chains if Keycloak ships them (issue #38279); the code already parses `act` when present.

▶ **Reconciliation of drift:**
- CLAUDE.md said "**fm_runtime propagates the fm_origin claim across exchange hops**"; in fact fm_runtime does **not** send it to the token endpoint — propagation is **Keycloak-mapper-native**; fm_runtime only folds origin into the broker cache key → **fix prose** (mechanism mis-attributed; corrected above).
- agents default LLM is **OpenAI `gpt-4o-mini`**, not "a Claude model"; CLAUDE.md / agents-agent said Claude → **fix prose + agents-agent.md** (OpenAI was resolved in Phase 4).
- agents' downgrade to service identity fires on **any `ExchangeError`, permanently**, not only on human-token expiry (`mcp_client.py`) → **fix code** (transient blip should not irreversibly drop the human subject; only genuine expiry should downgrade). See also P6 drift on the documented-vs-real downgrade mechanism.

### P4 — Guard expensive actions (estimate → confirm; agents escalate to a human) · **partial**

**Enforced by:** `libs/fm_runtime/fm_runtime/confirmation.py` (`require_confirmation`, `make_/verify_human_approval`); today wired into `leads` embeddings backfill and `mail` backup + campaign start/grow only.

Any operation that may consume extreme resources (a large search, a big embeddings backfill, a full mailbox backup, a large campaign) must **estimate first and require confirmation before proceeding** — never silently run something costly. Pattern (a shared `fm_runtime` helper so every service does it identically): the endpoint computes an estimate; if it exceeds a **configurable threshold** it returns **`409 confirmation_required`** with the estimate + a `confirm` token instead of running; the caller re-invokes with `confirm=true`. A UI renders a dialog; **a runtime AI agent must escalate the confirmation to its human — never auto-confirm.** The agent bypass is blocked cryptographically: an over-threshold agent action can proceed only with a single-use, HMAC-signed `human_approval` token the LLM never holds (`AgentApprovalRequired`, `make_human_approval`, `FM_CONFIRM_APPROVAL_SECRET`, fail-closed if unset). Concrete default: **mailbox backup > 2 GB** requires confirmation.

**The rule for future code / research enhancements:**
- **Keep the estimator service-local, the gate mechanism in `fm_runtime`.** Each producer estimates in its own units (leads: doc count/credits; mail: bytes/recipients; search: expected results); `fm_runtime` stays unopinionated about cost. (This is the P10 boundary applied to P4.)
- **Risk tiers, not a flat threshold:** auto-proceed < threshold; single confirm for sensitive; strongest gate (mandatory human approval even for `origin=user`, short TTL) for irreversible/high-spend. Thread the tier in the `require_confirmation` `meta` (additive per P2).
- **Per-agent-run cost budget + circuit breaker** (OWASP LLM10 Denial-of-Wallet): cap cumulative Apollo credits + OpenAI $ across a run, not just call count/wall clock; trip after N consecutive gated/expensive calls.
- Approvals are **single-use, short-lived, replay-protected**; back the consumed ledger with a **shared/persistent** store, not an in-process set (breaks under replicas).

▶ **Reconciliation of drift (this is the single biggest coverage gap):**
- `POST /api/search/search` (Apollo ingest walking up to 100k entries), `POST /api/search/mcp/v1/searches/apollo` (agent-callable), and `enrich_leads` have **no estimate/confirm gate** → **fix code** (wire `require_confirmation`; named explicitly in P4 as "a huge search"). The MCP write path makes this a prompt-injection Denial-of-Wallet vector.
- **CONFIRMED — the agent gate can be silently bypassed** → **fix code** (highest-severity P4 item): the human-approval gate is honored only if MCP surfaces it as a normal tool-result dict. `pydantic-ai`'s `MCPToolset` defaults `tool_error_behavior='retry'`, so a gate that surfaces as an **HTTP/tool error** is fed back to the LLM and retried — **past the human approval**. The gate must be structurally incapable of appearing as a retryable tool error: MCP returns the `409 confirmation_required` / `AgentApprovalRequired` as a structured *result* (never a raised error), and `agents` sets `tool_error_behavior` so gate payloads are never retried or hidden from the pause path. Flag for `security-reviewer`.
- `export.csv` / MCP `export_results` are unbounded / silently truncated at 5000 → **fix code** (estimate + confirm, or paginate).
- leads streamed search/enrich/match walk 100k entries ungated (only backfill is gated) → **fix code**.
- The frontend/mailui compute a **client-side** estimate but neither handles a **`409 confirmation_required`** from the server nor re-invokes with `confirm=true` → **fix code** (a non-browser caller bypasses the client heuristic entirely; the server handshake must be the gate).
- The **full HMAC/`human_approval` machinery** is far larger than CLAUDE.md documented → **fix prose** (documented above as a security boundary, not a nicety).

### P5 — The Apollo boundary · **enforced**

**Enforced by:** absence of `APOLLO_API_KEY`/Mongo/Milvus clients outside `leads/`; `search/app/leads_client.py`; nginx exposing only Apollo webhooks; `leads/app/apollo.py` (`X-Api-Key` server-side, client keys stripped).

**Only the leads backend ever talks to Apollo, and only the leads backend holds `APOLLO_API_KEY`.** Every Apollo-touching feature takes the path frontend → search router → `LeadsClient` → leads → Apollo (MCP writes funnel through search → leads too). The browser never calls leads directly (nginx exposes only `/api/leads/webhooks/*`). Mongo holds Apollo payloads; Milvus holds derived vectors; **search holds neither driver.** Do not shortcut this. Leads stays **unopinionated** — raw Apollo payloads, no UI shaping (a future MCP consumer depends on raw data; normalization lives in `search/`).

▶ **Reconciliation of drift:**
- leads ships a **CORS middleware with browser origins + credentials** though no browser ever reaches it → **fix code** (vestigial, misleading about the trust boundary; remove).
- `GET /api/leads/health` is `@anonymous` and leaks Milvus URI / config → **fix code** (drop config disclosure from the anonymous probe). See P6.

### P6 — Zero-trust auth: per-hop audiences, exchange-never-forward, `@anonymous` · **enforced**

**Enforced by:** `libs/fm_runtime/fm_runtime/principal.py` (`parse_token`, audience enforcement independent of signature verification), `middleware.py` (401/403/503), `tokens.py` (`TokenBroker`, RFC 8693), `annotations.py` (`@anonymous`), OPA `deploy/policy/funnelmanager/authz.rego`, Istio `RequestAuthentication`.

- **Per-hop audiences:** a service accepts only JWTs whose `aud` names it — enforced even when signature verification is off. Verify `aud` on **every** request (RFC 9700 replay defense).
- **Exchange, never forward:** `TokenBroker` mints a fresh downscoped token per target audience (RFC 8693), gated by the realm's `svc-<target>` optional client scopes as the one-hop allowlist. Never re-forward the inbound `Authorization` header from a router.
- **`@anonymous` is the allowlist:** routes tolerating no principal are annotated `fm_runtime.anonymous(reason)` — Apollo webhooks (secret-in-path), mail OAuth callback (single-use state row), probes/metrics. Every new exemption goes through the annotation + `fm_runtime.export`; **never hand-edit the mesh anonymous set.**
- **IdP outage = retryable 503, never 401.** Short-lived access tokens are the primary de-authorization lever (bearer tokens can't be revoked mid-life); keep mesh/exchange token lifetimes short and treat the token lifespan as the real revocation latency.
- **Dev compose has no mesh:** backends verify JWTs locally (`FM_JWT_VERIFY=true` + JWKS); issuer pinned to the browser URL while token/JWKS dial the `keycloak` container; keep `KC_HOSTNAME_BACKCHANNEL_DYNAMIC=false`.

**Future-facing (research):** evaluate **sender-constrained** (mTLS-bound, RFC 8705) mesh tokens leveraging the SPIFFE workload certs Istio already issues, so a leaked/cached exchange token can't be replayed by a different workload; enable **OPA decision logging** to a SIEM (allow/deny + `deny_reasons` + owner/origin/actor) as the durable authorization audit trail; in `authz.rego`, either switch the JWT branch from `io.jwt.decode` (decode-only) to `io.jwt.decode_verify`, or assert Istio actually verified the token before trusting `aud`/`azp`/roles.

▶ **Reconciliation of drift:**
- CLAUDE.md says detached jobs "**downgrade to client-credentials once the subject token expires mid-job**"; in fact `InternalClient.detached` freezes the subject token and, on expiry, the exchange simply **fails** (no client-credentials fallback) — client-credentials is reached only when there is **no** principal at all → **fix prose** (or **fix code** to implement the documented fallback). This is the intended behavior worth deciding: recommend **fix code** so long detached jobs survive expiry as designed.
- CLAUDE.md/authentication.md still describe the Principal as "sub + RFC 8693 `act` chain"; under KC 26.2 the `act` path never populates → **fix prose** (keep the `act` parser as future-proofing; describe the real model as `azp` + `fm_origin`).
- Several `@anonymous` health probes (`leads`, `jobs`, `agents`) leak config / producer inventory / internal hostnames → **fix code** (return liveness only; the fm_runtime `/healthz` `/readyz` already exist).
- `fm_runtime.export --check` covers grants + exchange + realm but **not** the `@anonymous` list → **fix code/CI** (wire the anonymous export into `--check` so it can't drift). See P7.

### P7 — Provable code ↔ policy ↔ realm lockstep · **partial**

**Enforced by:** `python -m fm_runtime.export --check deploy/policy/data.json --realm <realm.json>` (`verify_policy` / `_verify_realm_roles`). **Not yet a CI gate.**

The three encodings of authorization — `fm_runtime/grants.py` (`_DEFAULT_ROLE_GRANTS`, `SVC_EXCHANGE_SCOPES`), `deploy/policy/data.json` (`roles`, `azp_allow`), and the Keycloak realm roles/scopes — **must stay in lockstep**, machine-proven: `--check` asserts grants ↔ data.json and exchange scopes ↔ `azp_allow`; `--realm` additionally asserts the realm defines every role and that `admin` is a composite of exactly the four `-access` roles.

**The rule for future code:** any role/scope/anonymous change is a **coordinated edit across all three legs**, verified by the tool. Longer term, prefer a **single authored source** that *generates* `grants.py` and `data.json`, rather than two hand-mirrored copies kept equal by a check.

▶ **Reconciliation of drift:**
- **No CI job runs `--check`** — the strongest leg (the realm) is only runnable by hand; a hand-edited realm or a leftover `svc-*` over-grant would ship undetected → **fix CI** (add `--check … --realm` for both dev and prod realm files to the `policy` job). Highest-leverage, lowest-cost fix in the audit.
- The `@anonymous` list is **not** in `--check` (see P6) → **fix code/CI**.

### P8 — Never raise once a stream has started; correct streaming lifecycle · **partial (convention)**

**Honored by (convention, not machine-enforced):** the never-raise pattern is **hand-repeated** in `search/app/routers/search.py` (`_run_search_job`, enrich/match/org generators, CSV sentinel), `search/app/routers/internal_jobs.py`, `leads/app/stream_jobs.py` (`run_paged_search_stream`), the `agents` job stream; searchui `src/api.ts` / `src/progress.tsx`. **Nothing catches a stray `raise` in a streaming router** — the connection simply resets. This is `partial`, not `enforced`: promoting it to `enforced` means lifting the pattern into the shared `fm_runtime` streaming helper below (then a violation becomes structurally impossible, and the new `agents` service can't regress it).

Searches and enrichment are **NDJSON streams**. **Once an NDJSON response has started, a router must never raise** — Starlette resets the connection and the browser reports a generic network error that kills sibling streams sharing the origin. Instead, catch `HTTPException`/`Exception` and emit `{"type":"error","detail":…}` as a stream line, and always emit a terminal sentinel. Preserve the sibling-isolation, per-job history buffering (late subscribers replay), and round-robin multiplexing (embedding progress isn't stuck behind ingest). RUNNING jobs never TTL-expire; finished jobs drop after ~90s.

**Event vocabulary the frontend consumes:** `progress`, `first_page`, `complete`, `embedding_progress`, `ingest_complete`, `error` — **plus `item_error`** (per-row enrich failure) and `active_ingest_stream_ids` / `active_embedding_stream_ids` on enrich batches. `complete` may precede further embedding progress.

**The rule for future code / research enhancements:**
- **Lift the never-raise + heartbeat pattern into a shared `fm_runtime` streaming helper** so every current *and future* producer gets it for free (it is currently re-implemented in ≥ 3 places; the new `agents` service is a fresh chance to regress it). This is a P10-consistent home for a cross-cutting concern, and is the path to promoting P8 from **partial** to **enforced**.
- **Heartbeats + timeout hygiene:** add a periodic heartbeat line (below the smallest intermediary idle timeout) to every long-lived stream; set the gateway **absolute** request timeout to `0s` (disabled) for streaming routes and rely on idle timeout + heartbeats (an absolute 300s ceiling cuts a legitimate large search mid-stream); give the internal `/internal/jobs/v1/stream` an explicit no-timeout mesh route.
- **Backpressure:** bound the fan-out queues; make progress/heartbeat events lossy but lifecycle/terminal events lossless — on lifecycle overflow disconnect the one wedged subscriber rather than OOM the producer.
- **Cancellation as a P4 concern:** an abandoned UI-origin search still burning Apollo credits should be cancellable on disconnect (beacon on unload, or a server grace timer), while detached/MCP starts intentionally continue.

▶ **Reconciliation of drift:**
- `item_error` emitted to the browser but absent from the documented event set → **fix prose** (added above; also update `frontend-agent.md` / `search-agent.md`).
- leads embedding stream can publish `complete` at 100% while `index_lead_docs` soft-failed to `[]` (zero docs actually indexed, flags stay false) → **fix code** (progress must reflect real indexing, not chunk advance).

### P9 — Index/payload split, idempotent upsert & cache discipline · **partial**

**Enforced by:** `apollo_id` unique index (`leads/app/database.py`); `SearchResult` unique `(search_id, external_id)`; the hydration path `_hydrate_results`/`_set_current_page` (`search/app/routers/search.py`); embedding never-downgrade precedence (`leads/app/milvus_client.py`).

Apollo payloads live in **MongoDB only** (the write model / source of truth). Postgres stores search *history* + an *ordered index of Mongo `_id`s*, **never the payloads** — except an explicit **current-page cache** (`results_json`). Milvus holds the derived embedding read model. Render a page = read `_id`s for the page from Postgres → batch-hydrate from Mongo (`get_by_mongo_ids`, chunked at 500) → normalize. Mongo docs are deduped by `apollo_id`; enrichment **upserts**, never duplicates; `embedding:false` on insert flips `true` only after Milvus indexing succeeds (never downgraded: search < complete-info < match).

**The rule for future code / research enhancements:**
- **A cache is never a source of truth:** `results_json` must be treated as an ephemeral, best-effort page cache — re-hydrate from Mongo, or stamp it with a `hydrated_at`/version and ignore it past a short TTL. Under P1 (cross-user visibility) a stale shared cache is wrong for *every* reader.
- **Idempotent dedup must be atomic:** prefer a single atomic upsert (`update_one(..., upsert=True)` with an aggregation pipeline + `$setOnInsert`) guarded by the unique index over find-then-insert-with-retry (a TOCTOU pattern).
- **Reconciliation observability:** index → payload drift (a `SearchResult` pointing at an absent Mongo doc) must **warn + count**, not degrade silently.
- Keep this **data-layer logic in `search`/`leads`, never in `fm_runtime`** (P10).

▶ **Reconciliation of drift:**
- `results_json` is written full hydrated dicts and `_to_detail` reads it back **without re-hydration or invalidation** → **fix code** (stale after re-enrichment; served wrong to all P1 viewers).
- leads upsert is find-then-insert-with-retry (TOCTOU) → **fix code** (atomic upsert) — correctness holds today only via the unique-index + DuplicateKey retry.
- "never payloads" top-line → **fix prose** (qualified: index + a documented current-page cache).

### P10 — Separation of concerns: each service owns its task and is mesh-agnostic · **partial** *(NEW — first-class)*

**Enforced by:** `fm_runtime` being the single home for principal/authz/exchange plumbing (`install()` wires it in one call); OPA in the mesh as the PEP/PDP. **Not enforced against violations** — held by convention today, and the audit found leaks.

**The directive.** Each service owns **exactly its own task** and is otherwise **mesh-agnostic**. Application/business code must contain **no** service-mesh / Istio / OPA / cross-hop-authz / token-exchange-*policy* plumbing. The **only** cross-cutting concern permitted inline in a service is the **`fm_runtime` middleware that injects the principal / user metadata onto the request** (plus its sanctioned helpers called at the edge — see below). **Authorization is platform-enforced** (OPA in the mesh, `fm_runtime` grants in compose) — **never re-implemented in app code.**

**Sanctioned inline edges** (via `fm_runtime` primitives, *not* re-implemented):
- `InternalClient(base_url, audience=…)` — a service names its *target exchange audience* at the call site. That is exchange **use**, not exchange **policy**; acceptable because the broker owns the mechanism.
- `require_confirmation(...)` (P4) — an authorization-bearing gate invoked at expensive endpoints. Acceptable as a **shared helper**; the *estimate* is service-local, the *mechanism* is `fm_runtime`.
- The never-raise streaming wrapper (P8) once lifted into `fm_runtime`.

**Everything past that edge is a violation.** When building or reviewing a service, **actively hunt for**: subject-token-expiry / downgrade *decisions* in app code; hand-selection of which identity to use for a hop; re-mapping upstream 401/403/409 into bespoke authz semantics; hard-coded exchange-edge topology ("jobs→leads is not allowed") in a handler; `fm_runtime` gate/error *vocabulary* baked into app code; any `if username == …` authorization in a router (that is P1).

**Chassis caveat.** `fm_runtime` is a shared "chassis": a change forces **every** backend to redeploy. Version it and ship bumps as **platform releases** through the sha-pinned pipeline, with `--check` run before rollout. The per-language-duplication cost of a chassis doesn't apply (uniform Python) — which is *why* the shared-library choice, not a policy sidecar, is defensible here. The SPAs' integration seam (same-origin + shared `fm_oidc_*` session, **no shared code**) is the correct counter-example: integrate at the platform/identity boundary, not via a shared lib.

▶ **Reconciliation of drift — known violations to fix in code (not prose):**
- `search/app/leads_client.py` — subject-expiry parsing + client-credentials **downgrade policy** in app code → **fix code** (move the lifecycle decision behind `fm_runtime`; keep only `InternalClient` use at the call site).
- `search/app/routers/internal_jobs.py` `_leads_control` — hard-codes "jobs→leads is not an allowed exchange edge" and hand-picks search's own identity → **fix code** (topology belongs in the mesh/broker).
- `search/app/mail_client.py`, `mcp/app/clients.py`, `agents/app/mcp_client.py` — `ExchangeError`/gate-shape/authz-status *vocabulary* interpreted inline → **fix code** (surface via a `fm_runtime` helper).
- `jobs/app/control.py` — two-tier service-account-vs-human identity selection + hand-built `X-FM-Acting-User` audit headers → **partially sanctioned** (built on `InternalClient`); keep the header thin, and hoist the identity-selection decision into a `fm_runtime` helper (`InternalClient.detached`-style) rather than re-deciding it per handler.
- All **compliant** services (`jobs` MCP routes, `mail`, `frontend`, `mailui`, `agentsui`) — **keep as-is**; note them as the reference pattern.

### P11 — Testing strategy: the pyramid · **aspirational** *(NEW — first-class)*

**Enforced by:** nothing today. The repo has **no test suite** (no pytest/vitest); verification is "run the service and drive the flow." Frontends have `npm run build` (tsc, the typecheck) + `npm run lint` (oxlint). This principle defines the **target** pyramid and the **interim** verify contract.

The current "just run it" is the *floor*, not the ceiling. Build the pyramid broad-base-first; keep E2E a thin, deliberate cap.

**Unit (pure logic, per service, no I/O)**
- **Concentrate authz coverage in `fm_runtime`, not per service** (P10): the grants matrix (each `-access` → only its prefix; `admin`=service:\*; `internal-service`=leads-only; `jobs-internal` scoped to `/internal/jobs` on search+agents), the `@anonymous` allowlist, the `TokenBroker` cache keying, and the P4 confirmation/HMAC helper. **Service tests must not re-test authz** (duplicating it violates P10).
- Per service: pure transforms — `lead_to_record` normalization, CSV cell neutralization, the jobs `store.apply_event` ordering/terminal guards, the leads embedding precedence, streaming event framing.

**Integration (a service against its real data store + mocked upstream edges)**
- Spin the service against a **real** Postgres/Mongo/Milvus (Testcontainers); **mock the outbound service edges** (Apollo, Google, sibling FM services).
- **Stream tests are mandatory here** and must use a **chunk-consuming** async client (not a buffered read; beware the `httpx.AsyncClient.stream()`-over-ASGI hang — bound streams + timeouts). Assert the **P8 never-raise invariant directly**: force a downstream `HTTPException` mid-stream and verify an `{"type":"error"}` line is emitted and the connection is **not** reset; cover the full event vocabulary, late-subscriber replay, and round-robin multiplexing.
- **P4 two-branch test:** under threshold runs; over threshold returns `409 confirmation_required` + estimate + token; re-invoke with `confirm=true` proceeds; **agent origin can never auto-confirm.**
- Idempotent-upsert dedup race; hydration chunking at 500; index/payload drift warning.

**E2E (a flow across services through the real auth/exchange path)**
- Thin cap of critical journeys through nginx against a **real Keycloak** (Testcontainers, realm imported) — **never mocked JWTs**. Assert what mocks hide: a service rejects a token whose `aud` names another service; exchange succeeds **only** along allowlisted `svc-<target>` scopes and Keycloak refuses others; `fm_origin` defaults to `user` and the `agents` client overrides to `agent` and it survives hops; `azp` records the exchanging client.
- **Contract tests** on every cross-service hop (Pact-style consumer-driven): primary target `search→leads` (`get_by_mongo_ids`, chunk 500, apollo routes), plus message-style contract tests on the `/internal/jobs/v1/stream` NDJSON between each producer and the jobs subscriber, and a **golden MCP tool-schema snapshot** (P2).
- **Wire `fm_runtime.export --check … --realm` into CI as a contract gate** (P7).
- Assert **P1 by role**: a user *with* `search-access` sees cross-user history; a user *without* it gets **403 at the API** (not per-row hiding).

**How each owning agent VERIFIES (replaces "just run it")**
Until the suites exist, each agent's `## Verify` step is: **(1)** run the flow end-to-end as today, **(2)** additionally add/extend the **unit + integration** tests at the level its change touches, and **(3)** for any auth/role/anonymous/scope change, run `python -m fm_runtime.export --check … --realm`. A change to product source always has a runtime surface to drive — driving it (not just typecheck) remains required.

**The future agent-driven E2E loop (ties into the Roadmap)**
The thin E2E cap becomes **agent-drivable**: an `agents`-service verification run drives the product's own APIs through **MCP tools** against a canary and queries telemetry/logs to gate promotion. Snapshot-mode (accessibility-tree) Playwright MCP authors/self-heals browser journeys with **deterministic** end-of-flow assertions. Because `agents` already acts exclusively through MCP tools, the same audited surface exercises both the product agent and the test agent. Such verification runs **obey P4** (estimate-first; escalate over-threshold to a human, even inside CD) and default to a read-mostly tool profile.

▶ **Reconciliation of drift:** every service summary reports `test_coverage: none`; CI import-tests only search/leads/mail/mcp (jobs + agents **not** import-tested despite shipping) → **fix CI** (add jobs/agents to the `backends` import job) and **fix code** (begin the pyramid). P11 is the standing plan.

### Canary & observability program — DELIVERED *(was the Roadmap appendix; now built)*

The program the principles were written to accommodate — telemetry-enabled SPAs, a header/cookie-routed canary, and a Claude-driven E2E debugging loop — **is now built and running in prod** (shipped incrementally through tags up to v1.10.0). It is consistent with P1–P11 by construction; each piece below names the principle it satisfies, and the genuinely-**future** parts are called out at the end so this stays honest.

**Observability stack (built).** **Tempo** (traces) + **Grafana Faro** browser RUM + **Alloy** (collector, same-origin `/telemetry/collect`) now sit alongside the existing **Prometheus + Loki**. Mesh Istio tracing runs at a **5% forensic baseline** in ambient prod (`telemetry.yaml` `randomSamplingPercentage: 5`) — enough for incident forensics, cheap enough to always ship. **Canary flows are fully traced end-to-end** because the canary SPA's Faro originates a `sampled=1` `traceparent` that every sidecar honors (honor-incoming-sampled — verified live: browser → istio-ingress → `search-canary` under one trace id at the thin baseline). Grafana dashboards are **provisioned as code** (`deploy/infrastructure/observability/dashboards/` — generate.py → JSON → ConfigMaps via the substitution-free `infra-dashboards` Flux Kustomization; Grafana is stateless, UI edits don't survive). Log→trace→metric pivots are wired in Grafana. *(Cross-cutting, owned by `fm_runtime` + the mesh, out of business code — P6/P10.)*

**Telemetry is DEV/CANARY-ONLY (built).** `initTelemetry()` is gated behind a `VITE_TELEMETRY` flag + a **dynamic import**, so **prod SPA bundles tree-shake Faro out entirely** (live-verified: all three prod bundles grep zero Faro/Grafana) — zero prod overhead. Local dev (`import.meta.env.DEV`) and the canary build (`VITE_TELEMETRY=1` build-arg) turn it on. `data-testid`s stay in source always but are **inert in prod** (tool-agnostic, negligible cost). A dev/canary build also draws a thin **red "CANARY · telemetry on" viewport outline** so a canary tab is unmistakable. (Faro here is backend-trace-correlated **debugging**, not product analytics.)

**The header/cookie-routed canary + debug session (built).** Per-service **`<svc>-canary` workloads** run beside stable (`frontend-canary`, `search-canary` today), each a faithful copy of its stable peer that **reuses the stable service's ServiceAccount / audience / OPA grants** — same prod identity, real datastores — so a canary request rides the same auth/token-exchange path as prod. Access is a **single value-encoded host-only cookie** validated at the gateway by the **`debug-session-gate` EnvoyFilter** (istio-ingress, formerly `canary-cookie-gate`): **`fm_debug=<secret>`** is the un-forgeable, HttpOnly **debug-session grant** — by itself it routes to **stable/prod** but it **permits** canary routing and **gates the prod-tracing capability** — and **`fm_debug=<secret>|canary`** is the SAME debug session routed to the canary. Route selection is thus **itself secret-gated** (the `|canary` suffix is only honored as part of the exact secret value — there is **no** non-secret selector cookie, so a canary route can't be planted on a victim by a cross-site `Set-Cookie`). The filter validates the cookie by **exact equality**, **always strips any client-supplied `x-fm-canary`** header, and **re-injects the secret only when `fm_debug=<secret>|canary` is present** — so the marker is **un-forgeable** and the filter is purely additive / fail-safe (if it ever detaches, callers fall back to stable, never fail-open). **Prod-tracing gate:** a sampled incoming `traceparent` (`…-01`) with no valid `fm_debug` is reset to `-00` (Envoy's random baseline decides), and a non-canonical (non-55-length) sampled `traceparent` is stripped entirely, so only a debug session can force-sample prod; with `fm_debug` the `traceparent` is honored untouched. Gateway `/debug/on` (session→stable), `/debug/canary/on` (session→canary), and `/debug/off` (clear) endpoints toggle the cookie server-side (each emits one Set-Cookie, 302 to `/`). Routing is **per-service HTTPRoutes whose presence tracks activation**, yielding **"canary-if-exists-else-stable"**: a route exists only while its canary is active, so an idle/retired canary simply has no route and degrades to stable (**never 503**). `fm_runtime` **propagates the marker across every internal hop** (added to `TRACE_HEADERS`) and stamps `variant`/`canary` log labels + a per-pod `FM_DEPLOYMENT_VARIANT`. **`is_canary` is telemetry-only and is NEVER an authz input** — the gate re-injects a secret precisely because a client-set marker cannot be trusted (P6/P10).

**The `e2e-canary` identity (built).** A dedicated Keycloak test user `e2e-canary` (normal `search-access` role, never admin) drives or hand-navigates the canary, self-capped to cheap read-mostly flows under a per-run cost budget (the P4 Denial-of-Wallet discipline, applied to verification).

**Operator skills (built).** Five `.claude/skills/` own the lifecycle and the loop: **`canary`** (build/activate/retire/list — cookie-gated, route-tied; delegates rollout to `deploy-funnelmanager` and health to `prod-health`), **`observe-grafana`** (the Grafana-MCP LogQL/TraceQL/PromQL query cookbook over the `variant`/`canary` labels + Faro events), **`drive-canary`** (the headless Playwright-MCP loop as `e2e-canary`), **`enter-canary`** (a human-browser cookie launcher), and **`watch-canary`** (Claude observes a human's live canary session and root-causes what they hit). The original "give Claude the buttons and correlate a click to pod telemetry" ask is **delivered — authenticated and full-stack** (browser click → named canary pod → trace + logs read back).

**Still future (NOT built).** The **automated CD promotion gate** remains aspirational: **Flagger** `Canary` CRDs auto-gating on the `fm_http_*` success-rate / P99 series with automatic rollback, and its confirm-promotion **webhook triggering an `agents`-service verification run** that drives the canary exclusively through MCP tools (each run a P2/`jobs` job, P3 attribution) and returns pass/fail. Today activation/retirement is **operator-driven** through the `canary` skill, not an automated Flagger rollout, and the driving agent is this Claude session, not the `agents` service. When that gate lands it obeys **P4** (estimate-first; escalate over-threshold to a human even under CD) and carries a bounded, read-mostly tool profile + per-run budget. So **P3, P4, P6/P10, P8 (streaming under canary), and P11 (agent-drivable E2E)** already point at the finished target — the remaining work needs no principle it doesn't have.

### Consolidated drift reconciliation

| # | Drift (from service summaries) | Principle | Verdict |
|---|---|---|---|
| 1 | Search-start / enrich / export ungated by P4 | P4 | **fix code** |
| 2 | Client computes estimate but ignores server 409 handshake | P4 | **fix code** |
| 3 | leads streamed search/enrich/match ungated (only backfill gated) | P4 | **fix code** |
| 4 | HMAC `human_approval` machinery undocumented | P4 | **fix prose** (done) |
| 5 | leads MCP-consumed routes unversioned | P2 | **fix prose** (leads = native internal source) |
| 6 | MCP surface sits inside public `/api/search/*` | P2 | **fix prose** (distinct by path+aud, not network) |
| 7 | `fm_origin` "propagated by fm_runtime" — actually KC-native | P3 | **fix prose** (done) |
| 8 | agents default LLM OpenAI, not Claude | P3 | **fix prose + agents-agent.md** |
| 9 | agents downgrade fires on any ExchangeError, permanent | P3/P6 | **fix code** |
| 10 | Detached-job "expiry → client-credentials" not implemented | P6 | **fix code** (implement documented fallback) |
| 11 | Principal still described as `act` chain; KC 26.2 emits none | P6 | **fix prose** (keep parser) |
| 12 | `@anonymous` health probes leak config/topology | P5/P6 | **fix code** |
| 13 | `@anonymous` list not in `--check` | P6/P7 | **fix code/CI** |
| 14 | No CI runs `fm_runtime.export --check` | P7 | **fix CI** (highest leverage) |
| 15 | `item_error` event undocumented | P8 | **fix prose** (done) |
| 16 | leads embedding `complete` at 100% though 0 indexed | P8 | **fix code** |
| 17 | `results_json` read back without re-hydration/invalidation | P9 | **fix code** |
| 18 | leads upsert find-then-insert TOCTOU | P9 | **fix code** (atomic upsert) |
| 19 | "never payloads" absolute | P9 | **fix prose** (page-cache exception) |
| 20 | Mesh/exchange/authz vocabulary inline in app code (leads_client, internal_jobs, mail_client, mcp clients, agents mcp_client, jobs control) | P10 | **fix code** |
| 21 | Search/mail delete + UI affordances read as P1 violation | P1 | **fix prose** (destructive-ownership exception, done) |
| 22 | Dead `user.role` in mailui/agentsui | P1 | **fix code** (remove) |
| 23 | No test suite; jobs/agents not even import-tested in CI | P11 | **fix CI + fix code** (build pyramid) |
| 24 | mail campaign subsystem + mcp surface undocumented | P2/P4 | **fix prose** (document in service tables) |
| 25 | mail account delete hard-purges archived rows (contradicts "never delete") | P9-adjacent | **fix prose** (Google-side deletions only) or **fix code** — flag for review |
| 26 | search→mail edge undocumented | P5/P6 | **fix prose** |
| 27 | agents OpenAI + Keycloak egress omitted from "only egress edges" | P3 | **fix prose** |
| 28 | CI/compose reference nonexistent `deploy-prod.yml`; "six images" is ten | infra | **fix prose** |
| 29 | jobs README claims not-wired though compose/k3s exist | infra | **fix prose** (stale README) |
| 30 | Flux prod healthChecks omit agents/agentsui | infra | **fix code** (manifest) |
| 31 | **agents P4 approval gate silently bypassable** (MCPToolset `tool_error_behavior='retry'` feeds a gate-as-error back to the LLM) | P4/P10 | **fix code** — CONFIRMED, security-reviewer |
| 32 | leads live stream-control surface (`/stream/{id}/control/*`) undocumented & off the jobs contract | P2 | **fix prose** (document as native control lever, or fold under jobs-control) |
| 33 | prod compose runs Keycloak as `start-dev --import-realm` on **H2** (dev storage engine as sole prod OIDC issuer) | infra/P6 | **fix code** — durability/security, flag for review |
| 34 | frontend `DEFAULT_APPS` omits the agents tile | infra/P1 | **fix code** (low) |
| 35 | mail boot-time additive `ALTER` migrations are an undocumented mechanism | P9-adjacent | **fix prose** (low) |
| 36 | stale `AUTH_BACKEND_URL` in `mail/.env.example` | infra | **fix prose** (low) |
| 37 | `leads.py` duplicate import | quality | **fix code** (low) |
| 38 | `backup` image absent from CONVENTIONS Names/Images | infra | **fix prose** (low) |
| 39 | agentsui history hard-capped at 100 | quality | **fix code** (low) |

*(Items 24–30 are `CLAUDE.md`/README/manifest prose or infra fixes outside the principle block; 31–33 are the items the verify pass flagged as dropped/unreconciled (31 is a CONFIRMED security fix); 34–39 are the remaining minor summary drifts, folded in so this table is the **complete** set rather than an abridged one. The service tables and the platform-agent pick up the prose/infra rows.)*

## Data ownership / hydration model

Apollo payloads live in **MongoDB only**. Postgres stores search *history* and an *ordered index of Mongo `_id`s*, never the payloads:

- `SearchHistory` — one row per search (query label, entity_type, page, per_page, total_results), owned by `username` (the principal's `preferred_username`). Historically every history endpoint filtered/404'd by owner; **principle 1 (above) reverses the read side** — rows stay *attributed* to `username` (plus new `origin`/`actor` columns) but become visible to every user of the search service. Deleting an auth user does **not** delete their rows — a later user created with the same username inherits them (usernames are admin-controlled; rename is impossible). `results_json` is a **cache of the current page only**, not the source of truth.
- `SearchResult` — one row per hit: `external_id` is the Mongo lead `_id`, `position` preserves order. Unique on `(search_id, external_id)`.

So rendering any page means: read the `_id`s for that page from Postgres → batch-hydrate the full docs from Mongo via `LeadsClient.get_by_mongo_ids` (`POST /api/leads`, chunked at 500) → normalize with `lead_to_record`. See `_hydrate_results` / `_set_current_page` in `search/app/routers/search.py`.

Mongo lead docs are deduped by `apollo_id` (unique index). Enrichment upserts the existing doc — never a duplicate. `embedding: false` is set on insert and flipped to `true` after Milvus indexing succeeds.

## The streaming system (the hard part)

Searches and enrichment are **NDJSON streams**, not request/response. This is the most intricate code in the repo and touches all three services.

- **Leads side** (`leads/app/stream_jobs.py`): `StreamJobManager` holds in-memory jobs keyed by a `stream_id`. Each search spawns two peer jobs — an **ingest** stream (walks Apollo pages server-side at `per_page=100`, up to 100k entries, publishing `ids` events) and an **embedding** stream (OpenAI embed → Milvus index, publishing `progress`). They run as sibling coroutines linked by a queue (`run_paged_search_with_embedding`) so embedding overlaps the next page fetch. Events are buffered in per-job history so a subscriber that attaches late still gets every event. Finished jobs stay resolvable for ~90s (`_POST_JOB_TTL_SECONDS`) then buffers are dropped. `iter_stream_events` multiplexes many jobs onto one NDJSON response, round-robin so embedding progress isn't stuck behind ingest backlog.
- **Search side** (`search/app/routers/search.py`): subscribes to the leads streams and re-emits progress to the browser. `POST /api/search/search` emits `first_page` as soon as page 1 can be filled, then `complete` after ingest; embedding progress may keep flowing after `complete`. Pagination beyond page 1 is a **separate synchronous** call, `POST /api/search/searches/{id}/page`.
- **Critical invariant:** once an NDJSON response has started, routers must **never raise** — Starlette would reset the connection and the browser reports a generic network error that kills sibling streams sharing the origin. Instead, catch `HTTPException` and emit `{"type": "error", "detail": ...}` as a stream line. Every streaming endpoint in `search.py` follows this pattern; preserve it.
- Event `type`s the frontend consumes: `progress`, `first_page`, `complete`, `embedding_progress`, `ingest_complete`, `error`. Enrichment batches (`_enrich_ndjson_events`) additionally carry `active_ingest_stream_ids` / `active_embedding_stream_ids` so the UI can cancel an in-flight batch.

Search-app stream handling and the floating progress rings live in `searchui/src/api.ts` and `searchui/src/progress.tsx` (extracted from `frontend/` with the search app).

## Auth + authorization

Full reference: `docs/authentication.md`. The load-bearing rules:

- **Keycloak** (realm `funnelmanager`, source `deploy/keycloak/`) issues everything: browsers use auth-code + PKCE (`frontend/src/oidc.ts`, mirrored in `mailui/src/oidc.ts`, shared localStorage session), services and agents are confidential clients. Users/roles are Keycloak realm data — there is no in-repo auth API or user store.
- **Per-hop audiences:** a service accepts only JWTs whose `aud` names it (enforced by `fm_runtime`'s PrincipalMiddleware — plus Istio/OPA in the mesh). Internal calls **exchange, never forward**: `TokenBroker` (RFC 8693, cached per subject+audience) with the realm's `svc-<target>` optional client scopes as the one-hop allowlist (`search→leads`, `mcp→leads`, `agent→mcp`; Keycloak refuses anything else). KC 26.2 records the exchanging client in `azp` — it does not emit nested `act` chains. Detached jobs downgrade to the service's client-credentials identity (realm role `internal-service`, leads-only grants) once the captured subject token expires mid-job.
- **Role grants:** authorization beyond audience is the `{service, methods, path_prefix}` grants keyed by realm role (`deploy/policy/data.json`). The human-facing model is **one access role per service** — `search-access`→`/api/search`, `mail-access`→`/api/mail`, `jobs-access`→`/api/jobs`, `agents-access`→`/api/agents` (full methods within that prefix), plus `admin` = `service:*` and `internal-service` = leads-only. Humans get **no direct `leads` grant** (leads is internal, reached via search/mcp). In the mesh OPA enforces them; in compose `FM_ENFORCE_GRANTS=true` makes PrincipalMiddleware apply the identical rule in-process (`fm_runtime/grants.py` — its built-in default mirrors data.json; change them together, verify with `python -m fm_runtime.export --check`). No covering grant ⇒ 403, so compose stays fail-closed without OPA.
- **@anonymous is the allowlist:** routes tolerating no principal are annotated in code (`fm_runtime.anonymous(reason)`) — Apollo webhooks (secret-in-path), the mail OAuth callback (single-use state row), probes/metrics/legacy health. Export with `python -m fm_runtime.export`; the OPA policy data is generated from it, so code and policy cannot drift.
- **Dev compose has no mesh**, so backends verify JWTs locally (`FM_JWT_VERIFY=true` + JWKS). The issuer is pinned to the browser-facing URL (`http://localhost:8080/...`) while token/JWKS URLs dial the `keycloak` container — keep that split, and keep `KC_HOSTNAME_BACKCHANNEL_DYNAMIC=false`, or `iss` validation breaks. Prod compose **requires** `KEYCLOAK_REALM_FILE` (the tracked realm is dev-only: admin/admin + published secrets; template in `deploy/keycloak/realm-funnelmanager-prod.example.json`).
- nginx routes `/api/search/*`, `/api/mail/*`, `/api/leads/webhooks/*`, `/mail/`, and `/` only (no generic `/api/` catch-all, no auth routes). The hub shows the Keycloak-console card only for the `admin` realm role, but the server side (OPA + audience checks), not the UI, is the enforcement point.

## Commands

Everything runs through Docker Compose; there is one public entrypoint (nginx). Note `docker-compose.dev.yml` also starts Milvus and its deps (`etcd`, `minio`) plus `keycloak` (identity; imports the dev realm on first start).

```bash
# Dev (bind-mounted source, hot reload). App at http://localhost:5173, API at http://localhost:8000/api
cp .env.example .env      # set APOLLO_API_KEY
docker compose -f docker-compose.dev.yml up --build

# Prod — pulls prebuilt images from GHCR; never builds locally (see deploy/README.md).
# Images are built/pushed by .github/workflows/release-prod.yml (tag v* or manual dispatch).
cp .env.prod.example .env.prod
docker compose -f docker-compose.prod.yml --env-file .env.prod pull
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
```

Default dev login: `admin` / `admin`.

Non-Docker (each service independently — see README for full env setup):

```bash
# Search backend  → :8000
cd search && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Leads backend   → :8001  (needs MONGODB_URL + APOLLO_API_KEY; Milvus optional, degrades gracefully)
cd leads && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
uvicorn app.main:app --reload --port 8001

# Frontend        → :5173  (Vite proxies /api to VITE_API_PROXY_TARGET, default http://127.0.0.1:8000)
cd frontend && npm install && npm run dev
```

Frontend checks:

```bash
cd frontend
npm run build   # tsc -b && vite build  (this is the typecheck — run it after TS changes)
npm run lint    # oxlint
```

There is **no test suite** in this repo (no pytest/vitest) and no Python linter is configured. Verify backend changes by running the service and exercising the flow.

## Conventions worth knowing

- Backend imports are absolute from the `app` package (`from app.routers import ...`), run with the service dir as CWD.
- Both backends init their DB in a FastAPI `lifespan`; the leads backend also ensures the Milvus collection at startup and logs-but-tolerates failure (similarity search just becomes unavailable).
- Leads Apollo-facing routes mirror the native Apollo API path/params under `/api/leads/apollo/…` (see README table). Clients must not supply an Apollo key — it's server-side only.
- `PUBLIC_BASE_URL` + `APOLLO_WEBHOOK_SECRET` on the leads backend build the async phone/waterfall webhook URL; any client-supplied `webhook_url` is ignored.
