# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Funnel Manager is a login-gated tool to search and inspect Apollo person/company records. It is three services plus their data stores, wired together by Docker Compose. The README has the full API reference and env-var table; this file covers the cross-cutting architecture that isn't obvious from any single file.

## Services and the boundary that matters

| Service | Dir | Stack | Data | Faces |
|---|---|---|---|---|
| Auth backend | `auth-backend/` | FastAPI, redis-py (async) | Redis | The browser (via nginx, `/api/auth/*`) + the search backend |
| Search backend | `backend/` | FastAPI, SQLAlchemy (async, asyncpg) | Postgres | The browser (via nginx) |
| Leads backend | `leads-backend/` | FastAPI, Motor, OpenAI, pymilvus | MongoDB + Milvus | The search backend only |
| MCP server | `mcp-server/` | Python MCP SDK (FastMCP, streamable HTTP) | — | Internal MCP clients (OpenClaw) — never via nginx |
| OpenClaw | `openclaw/` (state) | `ghcr.io/openclaw/openclaw` image | bind-mounted `openclaw/` | Telegram + web Control UI (`:18789`) |
| Frontend | `frontend/` | React 19, MUI 9, Vite 8, TS | — | The browser |

The MCP server (`:8003/mcp`) authenticates to the search backend like the UI does (logs into the auth backend with `AUTH_USERNAME`/`AUTH_PASSWORD`, caches the session token, re-logs-in on 401) and calls the leads backend directly with no auth. Its tools split into two tiers that must stay clearly separated: read-only inspection (annotated `readOnlyHint`, never call Apollo — the leads backend's `GET /api/leads/stats` and `GET /api/leads/recent` exist for these) and explicit Apollo actions (`run_*_search`, `enrich_*`, `match_person`) that spend credits and must say so in their descriptions. Search tools return at `first_page` and rely on the search backend's detached-job design to finish ingest. The transport has a Host-header allowlist (`MCP_ALLOWED_HOSTS`) — internal clients dial `mcp-server:8003`. Prod publishes it on loopback only (`127.0.0.1:8003`).

OpenClaw is configured by `openclaw/openclaw.json` (gateway token auth, Telegram channel with pairing, `mcp.servers.funnelmanager` → `http://mcp-server:8003/mcp`). `openclaw/` is bind-mounted as the container's `~/.openclaw`; only `openclaw.json` and `skills/` are versioned — everything else there is runtime state and gitignored. The funnel-* skills teach the agent the MCP tools and credit etiquette; keep them in sync when MCP tools change.

**The core architectural rule: only the leads backend ever talks to Apollo, and only the leads backend holds `APOLLO_API_KEY`.** The search backend reaches Apollo functionality exclusively through `backend/app/leads_client.py` (`LeadsClient`) calling `LEADS_BACKEND_URL`. The browser never calls the leads backend directly — nginx does not expose it (except Apollo webhooks). When adding an Apollo-touching feature, the path is always: frontend → search backend router → `LeadsClient` → leads backend → Apollo. Do not shortcut this.

## Data ownership / hydration model

Apollo payloads live in **MongoDB only**. Postgres stores search *history* and an *ordered index of Mongo `_id`s*, never the payloads:

- `SearchHistory` — one row per search (query label, entity_type, page, per_page, total_results). `results_json` is a **cache of the current page only**, not the source of truth.
- `SearchResult` — one row per hit: `external_id` is the Mongo lead `_id`, `position` preserves order. Unique on `(search_id, external_id)`.

So rendering any page means: read the `_id`s for that page from Postgres → batch-hydrate the full docs from Mongo via `LeadsClient.get_by_mongo_ids` (`POST /api/leads`, chunked at 500) → normalize with `lead_to_record`. See `_hydrate_results` / `_set_current_page` in `backend/app/routers/search.py`.

Mongo lead docs are deduped by `apollo_id` (unique index). Enrichment upserts the existing doc — never a duplicate. `embedding: false` is set on insert and flipped to `true` after Milvus indexing succeeds.

## The streaming system (the hard part)

Searches and enrichment are **NDJSON streams**, not request/response. This is the most intricate code in the repo and touches all three services.

- **Leads side** (`leads-backend/app/stream_jobs.py`): `StreamJobManager` holds in-memory jobs keyed by a `stream_id`. Each search spawns two peer jobs — an **ingest** stream (walks Apollo pages server-side at `per_page=100`, up to 100k entries, publishing `ids` events) and an **embedding** stream (OpenAI embed → Milvus index, publishing `progress`). They run as sibling coroutines linked by a queue (`run_paged_search_with_embedding`) so embedding overlaps the next page fetch. Events are buffered in per-job history so a subscriber that attaches late still gets every event. Finished jobs stay resolvable for ~90s (`_POST_JOB_TTL_SECONDS`) then buffers are dropped. `iter_stream_events` multiplexes many jobs onto one NDJSON response, round-robin so embedding progress isn't stuck behind ingest backlog.
- **Search side** (`backend/app/routers/search.py`): subscribes to the leads streams and re-emits progress to the browser. `POST /api/search` emits `first_page` as soon as page 1 can be filled, then `complete` after ingest; embedding progress may keep flowing after `complete`. Pagination beyond page 1 is a **separate synchronous** call, `POST /api/searches/{id}/page`.
- **Critical invariant:** once an NDJSON response has started, routers must **never raise** — Starlette would reset the connection and the browser reports a generic network error that kills sibling streams sharing the origin. Instead, catch `HTTPException` and emit `{"type": "error", "detail": ...}` as a stream line. Every streaming endpoint in `search.py` follows this pattern; preserve it.
- Event `type`s the frontend consumes: `progress`, `first_page`, `complete`, `embedding_progress`, `ingest_complete`, `error`. Enrichment batches (`_enrich_ndjson_events`) additionally carry `active_ingest_stream_ids` / `active_embedding_stream_ids` so the UI can cancel an in-flight batch.

Frontend stream handling and the floating progress rings live in `frontend/src/api.ts` and `frontend/src/progress.tsx`.

## Auth

Authentication is **centralized in the auth backend** (`auth-backend/`) and uses **server-side sessions**, not self-validating JWTs:

- Single shared login (`AUTH_USERNAME` / `AUTH_PASSWORD`). `POST /api/auth/login` (OAuth2 password form) mints an **opaque session token** (`secrets.token_urlsafe`) stored in **Redis** under `session:<token>` with a native TTL (`SESSION_TTL_SECONDS`, default 1 day). See `auth-backend/app/sessions.py`.
- nginx routes `/api/auth/*` directly to the auth backend (longest-prefix match beats the catch-all `/api/`). The frontend keeps the token in `localStorage` (`fm_token`) and gates a single protected route; logout calls `POST /api/auth/logout` to revoke the session.
- **The search backend issues no tokens.** Its `get_current_user` (`backend/app/auth.py`) extracts the bearer token and validates it via `POST {AUTH_BACKEND_URL}/api/auth/validate` — the one place any *public* backend checks a token. Future public backends do the same.
- **Internal service-to-service calls carry no auth.** The leads backend is internal-only (nginx exposes only its Apollo webhooks, which keep their secret-in-path auth), so it no longer validates tokens, and `LeadsClient` sends no `Authorization` header. There is no longer a shared `SECRET_KEY` between services.

## Commands

Everything runs through Docker Compose; there is one public entrypoint (nginx). Note `docker-compose.dev.yml` also starts Milvus and its deps (`etcd`, `minio`), plus `redis` (auth session store) and the `auth-backend` service.

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
cd backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000

# Leads backend   → :8001  (needs MONGODB_URL + APOLLO_API_KEY; Milvus optional, degrades gracefully)
cd leads-backend && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
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
