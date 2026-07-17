# Funnel Manager — Apollo Inspector

Login-gated tool to search and inspect Apollo person and company records. The search backend stores history and Mongo lead `_id`s in Postgres; the leads backend is the only service that talks to Apollo and stores full records in MongoDB.

## Stack

- **Auth backend:** FastAPI + Redis — issues and stores opaque session tokens (1-day TTL); the single place login and token validation live
- **Search backend:** FastAPI, SQLAlchemy (Postgres) — search history + Mongo `_id` index; validates request tokens against the auth backend; relays searches to leads
- **Leads backend:** FastAPI, MongoDB (Motor) — Apollo People/Organization Search + Complete Info enrichment; internal-only, no request auth
- **MCP server:** Python MCP SDK (streamable HTTP) — internal-only, read-only inspection tools over the search + leads backends for agents (e.g. OpenClaw)
- **Frontend:** React + TypeScript + Vite + Material UI
- **Deploy:** Docker Compose, nginx, Postgres, MongoDB, Redis

## Features

- Login page (server-side session tokens issued by the auth backend)
- Search area for people or companies (search backend relays to `/api/leads`, which calls Apollo)
- Sidebar of previous searches (Postgres history + Mongo `_id`s; result payloads hydrated from Mongo)
- Results view with record list + detail pane
- Leads service: store Apollo people/org search hits in MongoDB (deduped by `apollo_id`), embed in Milvus in the background, and enrich via Complete Person/Organization Info

## Quick start (Docker development)

```bash
cp .env.example .env
# Edit .env and set APOLLO_API_KEY (and optionally AUTH_USERNAME / AUTH_PASSWORD)

docker compose -f docker-compose.dev.yml up --build
```

- App (nginx → Vite): http://localhost:5173
- APIs (nginx → backends): http://localhost:8000/api
  - `/api/auth/…` → auth backend (login, session validation)
  - other `/api/…` → search backend (search/history/enrich)
  - Leads is internal-only (`LEADS_BACKEND_URL`); the browser does not call it
- Postgres: localhost:5432
- MongoDB: localhost:27017
- Redis: localhost:6379

nginx is the public reverse proxy in front of Vite, the auth backend (`/api/auth/…`), and the search backend (everything else under `/api/…`). The leads backend and Redis are reached only by other services on the Docker network.

The MCP server listens at `http://localhost:8003/mcp` in dev (streamable HTTP). nginx never routes to it; in prod it is published on loopback only (`127.0.0.1:8003`).

Source is bind-mounted:

- `./backend` → search backend container
- `./leads-backend` → leads backend container
- `./frontend` → frontend container (with a named volume for `node_modules`)
- `./frontend/nginx.dev.conf` → nginx container

Default login: `admin` / `admin`

## Production (Docker + nginx)

```bash
cp .env.prod.example .env.prod
# Set DOMAIN, AUTH_PASSWORD, APOLLO_API_KEY, POSTGRES_PASSWORD, CORS_ORIGINS, DATABASE_URL

docker compose -f docker-compose.prod.yml --env-file .env.prod up --build -d
```

- nginx serves the built SPA on port 80 for `${DOMAIN}`
- `/api/auth/*` → auth FastAPI (Redis sessions); other `/api/*` → search FastAPI (Postgres), which relays to leads internally
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
# Set SEARCH_BACKEND_URL / LEADS_BACKEND_URL / AUTH_BACKEND_URL to reachable
# services (e.g. http://127.0.0.1:8000 / :8001 / :8002) plus AUTH_USERNAME / AUTH_PASSWORD.
uvicorn app.main:app --reload --port 8003
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Vite proxies `/api` for non-Docker local runs (`VITE_API_PROXY_TARGET`, default `http://127.0.0.1:8000`). In Docker Compose, nginx is the public entry and routes all `/api` traffic to the search backend.

## Environment

| Variable | Description |
|---|---|
| `APOLLO_API_KEY` | Apollo master API key (**leads backend only**) |
| `AUTH_USERNAME` / `AUTH_PASSWORD` | App login credentials (consumed by the **auth backend**) |
| `SESSION_TTL_SECONDS` | Session token lifetime (auth backend; default `86400` = 1 day) |
| `REDIS_URL` | Redis URL for the auth backend session store (e.g. `redis://redis:6379/0`) |
| `AUTH_BACKEND_URL` | Internal URL the search backend uses to validate session tokens (e.g. `http://auth-backend:8002`) |
| `CORS_ORIGINS` | Allowed frontend origins (comma-separated) |
| `DATABASE_URL` | Async SQLAlchemy URL (`postgresql+asyncpg://...`) |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Postgres bootstrap (compose) |
| `MONGODB_URL` | MongoDB connection URL |
| `MONGODB_DB` | MongoDB database name for leads |
| `LEADS_BACKEND_URL` | Internal URL used by the search backend to call leads (e.g. `http://leads-backend:8001`) |
| `SEARCH_BACKEND_URL` | Internal URL the MCP server uses to call the search backend (default `http://backend:8000`) |
| `DOMAIN` | Production hostname for nginx `server_name` |

## API

### Auth backend

nginx routes `/api/auth/*` here; it owns login and the Redis-backed session store. The search backend (and any future public backend) validate request tokens via `POST /api/auth/validate` on the Docker network.

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/auth/login` | OAuth2 password form → opaque session token (stored in Redis, 1-day TTL) |
| `GET` | `/api/auth/me` | Current user for a valid bearer token |
| `POST` | `/api/auth/validate` | Internal: `{ "token": … }` → user or 401 (called by public backends) |
| `POST` | `/api/auth/logout` | Revoke the caller's session |

### Search backend

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/search` | NDJSON: progress → `first_page` as soon as page 1 can be filled → `complete`; further pages sync |
| `POST` | `/api/search/company-people` | People-at-company search via leads (same NDJSON ingest) |
| `POST` | `/api/searches/{id}/page` | Synchronous page change: hydrate stored Mongo `_id`s for that page |
| `GET` | `/api/searches` | List previous searches |
| `GET` | `/api/searches/{id}` | Load stored results (hydrated from leads) |
| `DELETE` | `/api/searches/{id}` | Remove a history entry |
| `GET` | `/api/leads/{mongo_id}` | Hydrate one lead by Mongo `_id` |
| `POST` | `/api/people/{apollo_id}/enrich` | Proxy to leads complete-person enrich |
| `POST` | `/api/organizations/{apollo_id}/enrich` | Proxy to leads complete-organization enrich |

### Leads backend (internal)

Reached by the search backend at `LEADS_BACKEND_URL` (not exposed to the browser via nginx, except Apollo webhooks).

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
- The search backend starts leads streams (`stream=true`) and relays progress to the browser as NDJSON on `POST /api/search`.

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

Read-only [MCP](https://modelcontextprotocol.io) server for internal agents (e.g. OpenClaw) — streamable HTTP at `/mcp`, plus `GET /health`. It is never routed through nginx: dev publishes `8003` on the host, prod publishes `127.0.0.1:8003` (loopback only). It logs into the auth backend with `AUTH_USERNAME`/`AUTH_PASSWORD` and calls the search backend with that session token (re-login on 401); the leads backend is called directly with no auth. No tool calls Apollo, spends credits, or mutates data.

| Tool | Backing call | Description |
|---|---|---|
| `search_history` | `GET /api/searches` | User activity: recent searches (label, entity type, counts, timestamps) |
| `search_results` | `POST /api/searches/{id}/page` | One hydrated page of stored results (UI-normalized; `include_raw` for full payloads) |
| `get_lead` | `GET /api/leads/{mongo_id}` | One lead, normalized like the UI detail pane |
| `apollo_credits` | `GET /api/apollo/credits` | Apollo credit balance |
| `leads_stats` | leads `GET /api/leads/stats` | Collection counts: entity types, embedding, enrichment |
| `recent_leads` | leads `GET /api/leads/recent` | Enrichment/ingest activity feed with per-lead Apollo endpoint timeline |
| `get_leads` | leads `POST /api/leads` | Batch hydrate by Mongo `_id` (compact summaries; `include_raw` for full docs) |
| `similarity_search` | leads `POST /api/leads/similarity-search` | Semantic search over stored leads (no Apollo call, no history row) |

Point an MCP client at `http://127.0.0.1:8003/mcp` (transport: streamable HTTP). Example OpenClaw/Claude-style config:

```json
{
  "mcpServers": {
    "funnelmanager": { "type": "http", "url": "http://127.0.0.1:8003/mcp" }
  }
}
```
