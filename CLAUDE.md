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
| Leads backend | `leads/` | FastAPI, Motor, OpenAI, pymilvus | MongoDB + Milvus | The search backend + MCP server (token-authorized) |
| Mail backend | `mail/` | FastAPI, SQLAlchemy (async, asyncpg), httpx→Gmail | Postgres (dedicated `mail-db` container) | The browser (via nginx, `/api/mail/*`) + Google OAuth/Gmail REST |
| Mail UI | `mailui/` | React 19, MUI 9, Vite 8, TS (no router) | — | The browser — standalone app at `/mail/` (nginx-proxied, own container); hub tile via `WEB_APPS` |
| MCP server | `mcp/` | Python MCP SDK (FastMCP, streamable HTTP) | — | Internal MCP clients — never via nginx |
| Frontend | `frontend/` | React 19, MUI 9, Vite 8, TS | — | The browser — hub (landing → Keycloak sign-in → apps; admins get a Keycloak-console link) + the search app at `/search` |

**Naming convention:** one short name per backend is used **everywhere** — source dir, compose service, container/DNS name, GHCR image (`.../{name}`), and API prefix all match: `search` (`/api/search`), `leads` (`/api/leads`), `mail` (`/api/mail`), `mcp` (`/mcp` — the MCP protocol mount, no `/api`, never nginx-routed). Every public API path is `/api/{service}`. Dev containers are `funnelmanager-{service}-1`.

The MCP server (`:8003/mcp`) takes a **per-tool-call token** (the acting principal's, aud `mcp`): every tool accepts `session_token` (an `Authorization: Bearer` header on `/mcp` also works; the explicit argument wins — see `_token` in `mcp/app/main.py`) and exchanges it (RFC 8693) for a leads-audience token per upstream call. Tokenless calls only work when `MCP_SHARED_LOGIN_FALLBACK=true` (dev compose) — they act as the MCP server's own service identity via client credentials. All tools (`leads_stats`, `recent_leads`, `get_leads`, `similarity_search`) are read-only inspection (annotated `readOnlyHint`) over the leads backend and never call Apollo or the search backend — Apollo searches/enrichment are human-driven through the search app. The transport has a Host-header allowlist (`MCP_ALLOWED_HOSTS`) — internal clients dial `mcp:8003`. Prod publishes it on loopback only (`127.0.0.1:8003`).

**The mail service** (`mail/`, `:8004`) archives every message of OAuth-connected Gmail/Workspace mailboxes into a **dedicated Postgres container** (`mail-db`, database `funnelmanager_mail` — the service can also create the database itself when pointed at any Postgres) and sends mail via the Gmail API (scopes `gmail.readonly` + `gmail.send`; only the mail service talks to Google). Sync is a background loop (`app/sync.py`): per-mailbox newest-first backfill with a persisted page token, plus `history.list` increments anchored at connect time (deletions flag `is_deleted`, never delete rows — the archive outlives the mailbox). Auth follows the standard principal flow (mail-audience JWT) with two exemptions annotated `@anonymous`: the probes and `GET /api/mail/oauth/callback`, which is instead validated by a single-use state row bound to the initiating user (minted by `/api/mail/oauth/url`). Mailbox refresh tokens live in that database in plaintext. The mail UI is a **standalone app** (`mailui/` — React + MUI, deliberately shares no code with `frontend/`) built with Vite `base: '/mail/'` and served by its own container behind nginx's `/mail/` location; same-origin serving is what lets it share the hub's Keycloak session from localStorage (`fm_oidc_*` keys; unauthenticated → redirect to Keycloak). It appears on the hub only as a `WEB_APPS` tile (`/mail/`). Planned-but-not-built: semantic inbox querying and MCP tools over this store.

**The core architectural rule: only the leads backend ever talks to Apollo, and only the leads backend holds `APOLLO_API_KEY`.** The search backend reaches Apollo functionality exclusively through `search/app/leads_client.py` (`LeadsClient`) calling `LEADS_BACKEND_URL`. The browser never calls the leads backend directly — nginx does not expose it (except Apollo webhooks). When adding an Apollo-touching feature, the path is always: frontend → search backend router → `LeadsClient` → leads backend → Apollo. Do not shortcut this.

## Data ownership / hydration model

Apollo payloads live in **MongoDB only**. Postgres stores search *history* and an *ordered index of Mongo `_id`s*, never the payloads:

- `SearchHistory` — one row per search (query label, entity_type, page, per_page, total_results), owned by `username` (the principal's `preferred_username`): every history endpoint filters/404s by owner. Deleting an auth user does **not** delete their rows — a later user created with the same username inherits them (usernames are admin-controlled; rename is impossible). `results_json` is a **cache of the current page only**, not the source of truth.
- `SearchResult` — one row per hit: `external_id` is the Mongo lead `_id`, `position` preserves order. Unique on `(search_id, external_id)`.

So rendering any page means: read the `_id`s for that page from Postgres → batch-hydrate the full docs from Mongo via `LeadsClient.get_by_mongo_ids` (`POST /api/leads`, chunked at 500) → normalize with `lead_to_record`. See `_hydrate_results` / `_set_current_page` in `search/app/routers/search.py`.

Mongo lead docs are deduped by `apollo_id` (unique index). Enrichment upserts the existing doc — never a duplicate. `embedding: false` is set on insert and flipped to `true` after Milvus indexing succeeds.

## The streaming system (the hard part)

Searches and enrichment are **NDJSON streams**, not request/response. This is the most intricate code in the repo and touches all three services.

- **Leads side** (`leads/app/stream_jobs.py`): `StreamJobManager` holds in-memory jobs keyed by a `stream_id`. Each search spawns two peer jobs — an **ingest** stream (walks Apollo pages server-side at `per_page=100`, up to 100k entries, publishing `ids` events) and an **embedding** stream (OpenAI embed → Milvus index, publishing `progress`). They run as sibling coroutines linked by a queue (`run_paged_search_with_embedding`) so embedding overlaps the next page fetch. Events are buffered in per-job history so a subscriber that attaches late still gets every event. Finished jobs stay resolvable for ~90s (`_POST_JOB_TTL_SECONDS`) then buffers are dropped. `iter_stream_events` multiplexes many jobs onto one NDJSON response, round-robin so embedding progress isn't stuck behind ingest backlog.
- **Search side** (`search/app/routers/search.py`): subscribes to the leads streams and re-emits progress to the browser. `POST /api/search/search` emits `first_page` as soon as page 1 can be filled, then `complete` after ingest; embedding progress may keep flowing after `complete`. Pagination beyond page 1 is a **separate synchronous** call, `POST /api/search/searches/{id}/page`.
- **Critical invariant:** once an NDJSON response has started, routers must **never raise** — Starlette would reset the connection and the browser reports a generic network error that kills sibling streams sharing the origin. Instead, catch `HTTPException` and emit `{"type": "error", "detail": ...}` as a stream line. Every streaming endpoint in `search.py` follows this pattern; preserve it.
- Event `type`s the frontend consumes: `progress`, `first_page`, `complete`, `embedding_progress`, `ingest_complete`, `error`. Enrichment batches (`_enrich_ndjson_events`) additionally carry `active_ingest_stream_ids` / `active_embedding_stream_ids` so the UI can cancel an in-flight batch.

Frontend stream handling and the floating progress rings live in `frontend/src/api.ts` and `frontend/src/progress.tsx`.

## Auth + authorization

Full reference: `docs/authentication.md`. The load-bearing rules:

- **Keycloak** (realm `funnelmanager`, source `deploy/keycloak/`) issues everything: browsers use auth-code + PKCE (`frontend/src/oidc.ts`, mirrored in `mailui/src/oidc.ts`, shared localStorage session), services and agents are confidential clients. Users/roles are Keycloak realm data — there is no in-repo auth API or user store.
- **Per-hop audiences:** a service accepts only JWTs whose `aud` names it (enforced by `fm_runtime`'s PrincipalMiddleware — plus Istio/OPA in the mesh). Internal calls **exchange, never forward**: `TokenBroker` (RFC 8693, cached per subject+audience) with the realm's `svc-<target>` optional client scopes as the one-hop allowlist (`search→leads`, `mcp→leads`, `agent→mcp`; Keycloak refuses anything else). KC 26.2 records the exchanging client in `azp` — it does not emit nested `act` chains. Detached jobs downgrade to the service's client-credentials identity (realm role `internal-service`, leads-only grants) once the captured subject token expires mid-job.
- **Role grants:** authorization beyond audience is the `{service, methods, path_prefix}` grants keyed by realm role (`deploy/policy/data.json`). In the mesh OPA enforces them; in compose `FM_ENFORCE_GRANTS=true` makes PrincipalMiddleware apply the identical rule in-process (`fm_runtime/grants.py` — its built-in default mirrors data.json; change them together). No covering grant ⇒ 403, so compose stays fail-closed without OPA.
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
# Images are built/pushed by .github/workflows/deploy-prod.yml (tag v* or manual dispatch).
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
