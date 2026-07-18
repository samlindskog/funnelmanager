# Funnel Manager — Apollo Inspector

Apollo person/company search + enrichment platform, driven primarily through an AI agent (OpenClaw over Telegram / web UI) and gated by a central auth service with per-user profiles, roles, and OPA-backed authorization on every API endpoint. The search backend stores history and Mongo lead `_id`s in Postgres; the leads backend is the only service that talks to Apollo and stores full records in MongoDB.

## Stack

- **Auth backend:** FastAPI + Redis — user profiles (bcrypt), roles, channel links, pending account/channel requests, and opaque session tokens (1-day TTL); owns the OPA policy + role data and answers `POST /api/auth/authorize` for every service
- **OPA:** Open Policy Agent — the authorization decision point; the auth backend pushes the Rego policy and role grants into it (fail closed when unreachable)
- **Search backend:** FastAPI, SQLAlchemy (Postgres) — search history + Mongo `_id` index; authorizes every request via the auth backend; relays searches to leads with the caller's token
- **Leads backend:** FastAPI, MongoDB (Motor) — Apollo People/Organization Search + Complete Info enrichment; internal-only but still authorizes every request (webhooks keep secret-in-path auth)
- **MCP server:** Python MCP SDK (streamable HTTP) — internal-only read-only tools over the leads backend for agents; every tool call carries a per-profile session token that is forwarded upstream
- **OpenClaw agent:** personal AI agent (Telegram + web Control UI) wired to the MCP server, with a funnel-activity skill and a `funnelmanager-auth` plugin that maps channel senders to profiles
- **Frontend:** React + TypeScript + Vite + Material UI — nondescript landing (sign in / request access) + post-login hub (profile, apps, admin panels) + the search app at `/search` (searches, streamed ingest/embedding progress, enrichment)
- **Deploy:** Docker Compose, nginx, Postgres, MongoDB, Redis, OPA

## Features

- Minimal landing page (no product info) with sign-in and "request access" (username only)
- Post-login hub: current profile (username + role), configured web apps (e.g. OpenClaw Control UI)
- Admin panels (admin role only): pending channel requests (assign a Telegram sender to a new/existing user), pending account requests, user management, role management with per-service grants
- Every API endpoint — public or internal — validates the session token and checks the caller's role against the OPA policy
- OpenClaw channel identities (e.g. Telegram sender ids) are linked to profiles; the agent acts with the linked user's authority
- Leads service: store Apollo people/org search hits in MongoDB (deduped by `apollo_id`), embed in Milvus in the background, and enrich via Complete Person/Organization Info

## Authorization model

- **Profiles** live in the auth backend (Redis): username, bcrypt password hash, one **role**, linked channels.
- **Roles** carry **grants**: `{service, methods, path_prefix}` rows (service is `auth` / `search` / `leads` / `*`). The built-in `admin` role grants everything and cannot be deleted; more roles can be created in the hub UI.
- The auth backend pushes the Rego policy + role grants to **OPA** at startup, on every role change, and periodically (self-heals OPA restarts).
- Every service resolves each request with one call — `POST /api/auth/authorize` `{token, service, method, path}` → `401` (bad token), `{allowed: false}` (deny → 403), or `{allowed: true, username, role}`. OPA unreachable ⇒ deny (fail closed).
- Auth's own surface: `login` / `request-account` are anonymous; `me`, `logout`, `apps`, `validate`, `authorize` are open to any valid profile; everything else under `/api/auth/admin/*` requires a role whose grants cover the `auth` service.
- Sessions store only the username; role and existence are re-resolved from the profile store on every check, so role changes and deletions apply to live sessions immediately.

## Quick start (Docker development)

```bash
cp .env.example .env
# Edit .env and set APOLLO_API_KEY (and optionally AUTH_USERNAME / AUTH_PASSWORD)

docker compose -f docker-compose.dev.yml up --build
```

- App (nginx → Vite): http://localhost:5173
- APIs (nginx → backends): http://localhost:8000/api
  - `/api/auth/…` → auth backend (login, profiles, authorization; `/internal/*` is **not** routed)
  - `/api/search/…` → search backend (search/history/enrich)
  - Leads is internal-only (`LEADS_BACKEND_URL`); the browser does not call it
- Postgres: localhost:5432
- MongoDB: localhost:27017
- Redis: localhost:6379

nginx is the public reverse proxy in front of Vite, the auth backend (`/api/auth/…`), and the search backend (`/api/search/…`). The leads backend, OPA, and Redis are reached only by other services on the Docker network; the auth backend's `/internal/*` routes (OpenClaw session minting) are likewise unreachable from outside.

The MCP server listens at `http://localhost:8003/mcp` in dev (streamable HTTP). nginx never routes to it; in prod it is published on loopback only (`127.0.0.1:8003`).

Source is bind-mounted:

- `./backend` → search backend container
- `./leads-backend` → leads backend container
- `./frontend` → frontend container (with a named volume for `node_modules`)
- `./frontend/nginx.dev.conf` → nginx container

Default login: `admin` / `admin` (the bootstrap admin user — created on first
startup from `AUTH_USERNAME` / `AUTH_PASSWORD`, then managed like any other
user in the hub UI; later changes to those env vars do not update it).

## Production (Docker + nginx)

```bash
cp .env.prod.example .env.prod
# Set DOMAIN, AUTH_PASSWORD, APOLLO_API_KEY, POSTGRES_PASSWORD, CORS_ORIGINS, DATABASE_URL

# Prod uses prebuilt images from GHCR (no local builds) — built and pushed by
# .github/workflows/deploy-prod.yml; see deploy/README.md for the full flow.
docker compose -f docker-compose.prod.yml --env-file .env.prod pull
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
```

- nginx serves the built SPA on port 80 for `${DOMAIN}`
- `/api/auth/*` → auth FastAPI (Redis sessions); `/api/search/*` → search FastAPI (Postgres), which relays to leads internally
- Postgres, MongoDB, and Redis data are stored in Docker volumes

Point your DNS A/AAAA record for `${DOMAIN}` at the host. Put TLS in front (Cloudflare, Caddy, Traefik, or certbot) as needed.

## Local non-Docker (optional)

### Search backend

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Set DATABASE_URL to a reachable Postgres instance, e.g.
# postgresql+asyncpg://funnel:funnel@localhost:5432/funnelmanager
uvicorn app.main:app --reload --port 8000
```

### Leads backend

```bash
cd leads-backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Set MONGODB_URL (e.g. mongodb://127.0.0.1:27017) and APOLLO_API_KEY
uvicorn app.main:app --reload --port 8001
```

### Auth backend

```bash
cd auth-backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Set REDIS_URL to a reachable Redis (e.g. redis://127.0.0.1:6379/0) and AUTH_USERNAME / AUTH_PASSWORD
uvicorn app.main:app --reload --port 8002
```

### MCP server

```bash
cd mcp-server
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Set LEADS_BACKEND_URL / AUTH_BACKEND_URL to reachable services
# (e.g. http://127.0.0.1:8001 / :8002) plus AUTH_USERNAME / AUTH_PASSWORD.
uvicorn app.main:app --reload --port 8003
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Vite proxies `/api` for non-Docker local runs (`VITE_API_PROXY_TARGET`, default `http://127.0.0.1:8000`). In Docker Compose, nginx is the public entry and routes `/api/search` traffic to the search backend.

## Environment

| Variable | Description |
|---|---|
| `APOLLO_API_KEY` | Apollo master API key (**leads backend only**) |
| `AUTH_USERNAME` / `AUTH_PASSWORD` | Bootstrap admin credentials — the auth backend creates this admin-role user if missing (never updates an existing one) |
| `SESSION_TTL_SECONDS` | Session token lifetime (auth backend; default `86400` = 1 day) |
| `REDIS_URL` | Redis URL for the auth backend profile + session store (e.g. `redis://redis:6379/0`) |
| `OPA_URL` | OPA decision service URL (auth backend; default `http://opa:8181`) |
| `WEB_APPS` | JSON list of hub apps `[{"name","description","url"}]`; blank = default Search (`/search`) + OpenClaw entries |
| `AUTH_BACKEND_URL` | Internal URL services use to authorize requests (e.g. `http://auth-backend:8002`) |
| `MCP_SHARED_LOGIN_FALLBACK` | MCP server: allow tokenless tool calls to fall back to the shared login (dev compose: `true`; prod default: `false`) |
| `CORS_ORIGINS` | Allowed frontend origins (comma-separated) |
| `DATABASE_URL` | Async SQLAlchemy URL (`postgresql+asyncpg://...`) |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Postgres bootstrap (compose) |
| `MONGODB_URL` | MongoDB connection URL |
| `MONGODB_DB` | MongoDB database name for leads |
| `LEADS_BACKEND_URL` | Internal URL used by the search backend to call leads (e.g. `http://leads-backend:8001`) |
| `MCP_ALLOWED_HOSTS` | Host headers the MCP transport accepts (default `mcp-server:8003,localhost:8003,127.0.0.1:8003`) |
| `OPENCLAW_GATEWAY_TOKEN` | Shared-secret auth for the OpenClaw Control UI / gateway API |
| `ANTHROPIC_API_KEY` | Optional: preferred OpenClaw agent model once set (see `openclaw/openclaw.json`); OpenAI is used until then |
| `TELEGRAM_BOT_TOKEN` | Telegram bot token for the OpenClaw Telegram channel |
| `DOMAIN` | Production hostname for nginx `server_name` |

## API

### Auth backend

nginx routes `/api/auth/*` here; it owns profiles, roles, sessions, and the OPA policy. Services authorize every request via `POST /api/auth/authorize` on the Docker network.

Public / any-profile:

| Method | Path | Access | Description |
|---|---|---|---|
| `POST` | `/api/auth/login` | anonymous | OAuth2 password form → opaque session token (Redis, 1-day TTL) |
| `POST` | `/api/auth/request-account` | anonymous | `{ "username": … }` → pending account request (always answers 202) |
| `GET` | `/api/auth/me` | any profile | Current username + role |
| `GET` | `/api/auth/apps` | any profile | Hub app list (from `WEB_APPS`) |
| `POST` | `/api/auth/validate` | token in body | `{ "token": … }` → user or 401 |
| `POST` | `/api/auth/authorize` | token in body | `{ token, service, method, path }` → `{ allowed, username, role }` (the OPA decision endpoint) |
| `POST` | `/api/auth/logout` | any profile | Revoke the caller's session |

Admin (role grants must cover the `auth` service):

| Method | Path | Description |
|---|---|---|
| `GET/POST` | `/api/auth/admin/users` | List / create users (`{username, password, role}`) |
| `PATCH/DELETE` | `/api/auth/admin/users/{username}` | Change role/password; delete (guards the last admin) |
| `DELETE` | `/api/auth/admin/users/{u}/channels/{channel}/{device_id}` | Unlink a channel identity |
| `GET/POST` | `/api/auth/admin/roles`, `DELETE /roles/{name}` | Roles with grants; changes are pushed to OPA |
| `GET` | `/api/auth/admin/account-requests` (+ `/approve`, `/deny`) | Pending account requests |
| `GET` | `/api/auth/admin/channel-requests` (+ `/assign`, `/deny`) | Pending channel requests; assign to an existing or new user |

Internal (Docker network only — nginx never routes `/internal/*`):

| Method | Path | Description |
|---|---|---|
| `POST` | `/internal/openclaw/session` | `{ channel, device_id, display_name? }` → session token for the linked profile, or 403 `{pending: true}` after recording a pending channel request |

### Search backend

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/search/search` | NDJSON: progress → `first_page` as soon as page 1 can be filled → `complete`; further pages sync |
| `POST` | `/api/search/search/company-people` | People-at-company search via leads (same NDJSON ingest) |
| `POST` | `/api/search/searches/{id}/page` | Synchronous page change: hydrate stored Mongo `_id`s for that page |
| `GET` | `/api/search/searches` | List previous searches |
| `GET` | `/api/search/searches/{id}` | Load stored results (hydrated from leads) |
| `DELETE` | `/api/search/searches/{id}` | Remove a history entry |
| `GET` | `/api/search/leads/{mongo_id}` | Hydrate one lead by Mongo `_id` |
| `POST` | `/api/search/people/{apollo_id}/enrich` | Proxy to leads complete-person enrich |
| `POST` | `/api/search/organizations/{apollo_id}/enrich` | Proxy to leads complete-organization enrich |

### Leads backend (internal)

Reached by the search backend and MCP server at `LEADS_BACKEND_URL` (not exposed to the browser via nginx, except Apollo webhooks). Every route requires a bearer session token and is authorized against the auth service + OPA (service `leads`), except `GET /api/leads/health` and the secret-in-path Apollo webhooks.

Apollo-facing routes use the native Apollo API relative path under `/api/leads/apollo/…`. Method, path, and parameters match [Apollo’s API](https://docs.apollo.io/reference) (path may include or omit the `api/v1/` prefix). Apollo auth uses `APOLLO_API_KEY` from the server `.env` only — clients must not supply an Apollo API key.

| Method | Path | Native Apollo endpoint | Response / side effects |
|---|---|---|---|
| `GET` | `/api/leads/health` | — | Liveness / config probe |
| `GET` | `/api/leads/stats` | — | Lead-collection counts: entity types, embedded vs pending, enrichment flags |
| `GET` | `/api/leads/recent` | — | Recently updated leads (`entity_type`, `enriched`, `embedded`, `limit`, `skip`) |
| `GET` | `/api/leads/apollo/api/v1/users/api_profile` | [Get current user profile](https://docs.apollo.io/reference/get-current-user-profile) | Raw Apollo JSON |
| `POST` | `/api/leads/apollo/api/v1/mixed_people/api_search` | [People API Search](https://docs.apollo.io/reference/people-api-search) | `{ ingest_stream_id, embedding_stream_id, ids }` — optional `stream`; see notes |
| `POST` | `/api/leads/apollo/api/v1/mixed_companies/search` | [Organization Search](https://docs.apollo.io/reference/organization-search) | `{ ingest_stream_id, embedding_stream_id, ids }` — optional `stream`; see notes |
| `GET` | `/api/leads/stream/{stream_id}` | — | NDJSON for one job (ingest or embedding; events tagged with `kind`) |
| `POST` | `/api/leads/stream` | — | Multiplex many jobs: body `{ "stream_ids": [...] }` → tagged NDJSON |
| `GET` | `/api/leads/apollo/api/v1/people/{id}` | [Get complete person info](https://docs.apollo.io/reference/get-complete-person-info) | `SearchIdsOut` (`stream` like search); upsert + async embed |
| `GET` | `/api/leads/apollo/api/v1/organizations/{id}` | [Get complete organization info](https://docs.apollo.io/reference/get-complete-organization-info) | `SearchIdsOut` (`stream` like search); upsert + async embed |
| `POST` | `/api/leads/apollo/api/v1/people/match` | [People enrichment](https://docs.apollo.io/reference/people-enrichment) | `PersonMatchOut`; upsert + embed; may inject webhook URL |
| `POST` | `/api/leads` | — | Batch hydrate by Mongo `_id`s (`{ "ids": [...] }`, order preserved; missing omitted) |
| `POST` | `/api/leads/similarity-search` | — | Embed query → Milvus → hydrate Mongo leads |

**Interface notes**

- People/org search accept Apollo params plus optional `stream` (not forwarded to Apollo). Response shape:
  - `ingest_stream_id` — Apollo multi-page walk when `stream=true` (else null)
  - `embedding_stream_id` — embedding progress for this search (null if nothing to embed)
  - `ids` — Mongo `_id`s for the page when `stream=false` (empty when streaming)
  - `stream=false` (default): upsert one Apollo page + start embedding stream for those ids.
  - `stream=true`: background job walks Apollo pages at `per_page=100` (max) up to 100,000 entries; consume `ingest_stream_id` / `embedding_stream_id` via `/api/leads/stream/…`. Ingest events: `{ kind: "ingest", type: "ids"|"complete"|"error", … }`. Embedding events: `{ kind: "embedding", type: "progress"|"complete"|"error", done, total, … }`. Stream ids stay usable for ~90s after the job ends (or ~90s from create if abandoned/stuck), then buffers are dropped.
- Search sets `embedding: false` on upserted docs, then embeds in the background (async OpenAI + Milvus work off the event loop) and sets `embedding: true` when indexing succeeds. Progress is exposed on `embedding_stream_id`.
- People match takes Apollo identity params (`id`, email, linkedin_url, …) in the body/query.
- Client-supplied `webhook_url` on people/match is ignored; when async phone/waterfall flags are set, the leads service injects `{PUBLIC_BASE_URL}/api/leads/webhooks/apollo/{APOLLO_WEBHOOK_SECRET}`.
- Postgres `search_results.external_id` stores Mongo `_id` strings for later hydrate via `POST /api/leads`.
- The search backend starts leads streams (`stream=true`) and relays progress to the browser as NDJSON on `POST /api/search/search`.

MongoDB lead documents:

| Field | Description |
|---|---|
| `_id` | Generated MongoDB ObjectId (not Apollo’s id) |
| `apollo_id` | Apollo person or organization id (unique index) |
| `entity_type` | `person` or `organization` |
| `embedding` | `true` after a successful Milvus index; `false` while pending/failed |
| `apollo_responses` | Endpoint-keyed Apollo payloads (`/api/v1/…` → `{ received_at, data }`) |
| `apollo_enriched` | `{ linkedin, email, phone }` flags for enrichment jobs |

Enrichment updates an existing document when `apollo_id` already exists (no duplicates).

### MCP server (internal)

[MCP](https://modelcontextprotocol.io) server for internal agents (e.g. OpenClaw) — streamable HTTP at `/mcp`, plus `GET /health`. It is never routed through nginx: dev publishes `8003` on the host, prod publishes `127.0.0.1:8003` (loopback only).

**Auth:** every tool accepts an optional `session_token` argument; the MCP server also honors an `Authorization: Bearer` header on the `/mcp` request (the explicit argument wins). The token is forwarded unchanged to the leads backend, which authorizes it per request against the auth service + OPA — so the agent acts with the authority of the profile behind the token. The OpenClaw `funnelmanager-auth` plugin fetches these tokens per channel sender. With `MCP_SHARED_LOGIN_FALLBACK=true` (dev), tokenless calls fall back to a shared `AUTH_USERNAME`/`AUTH_PASSWORD` login.

All tools are read-only inspection over the leads backend (free, no Apollo calls — new searches/enrichment happen in the search app):

| Tool | Backing call | Description |
|---|---|---|
| `leads_stats` | leads `GET /api/leads/stats` | Collection counts: entity types, embedding, enrichment |
| `recent_leads` | leads `GET /api/leads/recent` | Enrichment/ingest activity feed with per-lead Apollo endpoint timeline |
| `get_leads` | leads `POST /api/leads` | Batch hydrate by Mongo `_id` (compact summaries; `include_raw` for full docs) |
| `similarity_search` | leads `POST /api/leads/similarity-search` | Semantic search over stored leads (no Apollo call, no history row) |

The transport's DNS-rebinding protection allows `mcp-server:8003`, `localhost:8003`, and `127.0.0.1:8003` by default — extend via `MCP_ALLOWED_HOSTS` if clients dial another hostname.

Point an MCP client at `http://127.0.0.1:8003/mcp` (transport: streamable HTTP). Example OpenClaw/Claude-style config:

```json
{
  "mcpServers": {
    "funnelmanager": { "type": "http", "url": "http://127.0.0.1:8003/mcp" }
  }
}
```

## OpenClaw agent

The `openclaw` compose service runs an [OpenClaw](https://docs.openclaw.ai) gateway wired to the funnelmanager MCP server (`mcp.servers` in `openclaw/openclaw.json`), reachable over **Telegram** and the **web Control UI** (`http://localhost:18789`, prod: loopback only). State lives in the bind-mounted `openclaw/` dir (only `openclaw.json`, `skills/`, and `extensions/` are versioned; runtime state is gitignored) plus a named volume for auth-profile encryption keys.

**Channel identity → profile:** the versioned `funnelmanager-auth` plugin (`openclaw/extensions/funnelmanager-auth/`) resolves each conversation's channel + sender id, fetches a session token for the linked profile from the auth service's internal endpoint (`POST /internal/openclaw/session`), and injects it as `session_token` into funnelmanager MCP tool calls (`before_tool_call`). For harnesses whose native MCP path cannot rewrite tool arguments, it also registers a `funnelmanager_session_token` agent tool the model can call and pass along explicitly. Unlinked senders are blocked and recorded as **pending channel requests**, which admins assign to a new or existing user from the hub's admin panel. Inbound messages also report the sender's identity (throttled per identity), so a chat that was paired with OpenClaw before the plugin existed still surfaces as a pending request without waiting for a tool call.

Skills (in `openclaw/skills/`):

- `funnel-activity` — read-only activity/inspection over stored leads: recent enrichment, stats, similarity search
- `csv`, `web-research` — ClawHub community skills (verified with `openclaw skills verify` before install) for lead-list CSV handling and cited prospect research

First-run setup:

1. Set in `.env` (see `.env.example`): `OPENCLAW_GATEWAY_TOKEN` (generate: `openssl rand -hex 32`) and `TELEGRAM_BOT_TOKEN` (from `@BotFather` → `/newbot`). The agent model runs on `OPENAI_API_KEY` (`openai/gpt-5.6`); Anthropic stays wired in `openclaw/openclaw.json` — set `ANTHROPIC_API_KEY` and flip `model.primary` to `anthropic/claude-opus-4-8` when you have a key.
2. `docker compose -f docker-compose.dev.yml up -d openclaw`
3. Web UI: open `http://localhost:18789` and paste the gateway token.
4. Telegram: DM your bot, then approve the pairing code:
   `docker exec funnelmanager-openclaw-1 openclaw pairing approve telegram <CODE>`

Manage skills inside the container: `docker exec funnelmanager-openclaw-1 openclaw skills list|search|verify|install …` (installs land in `openclaw/skills/` and persist).
