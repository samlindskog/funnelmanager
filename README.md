# Funnel Manager — Apollo Inspector

Apollo person/company search + enrichment platform with a zero-trust identity architecture: Keycloak is the sole OIDC issuer, every request carries the originating principal as a JWT (exchanged per hop via RFC 8693), and authorization belongs to the platform (OPA). The search backend stores history and Mongo lead `_id`s in Postgres; the leads backend is the only service that talks to Apollo and stores full records in MongoDB.

## Stack

- **Keycloak:** the sole identity provider (realm `funnelmanager`) — human login via auth-code + PKCE, service/agent clients via client credentials, per-hop delegation via RFC 8693 token exchange (see `deploy/keycloak/`)
- **fm_runtime** (`libs/fm_runtime`): shared runtime installed in every backend — principal extraction (sub + act/azp), audience checks, cached token-exchange broker, structured JSON logs, `/healthz` `/readyz` `/metrics`, `@anonymous` route annotations (the source of truth for the public-anonymous allowlist)
- **Search backend:** FastAPI, SQLAlchemy (Postgres) — search history + Mongo `_id` index; exchanges the caller's token for a leads-audience token on every relay
- **Leads backend:** FastAPI, MongoDB (Motor) — Apollo People/Organization Search + Complete Info enrichment; internal-only, accepts only leads-audience JWTs (webhooks keep secret-in-path auth)
- **Mail backend:** FastAPI, SQLAlchemy (dedicated `mail-db` Postgres container) — archives every message from connected Gmail/Workspace mailboxes (any number of domains, OAuth per mailbox), keeps them in sync in the background, and sends mail via the Gmail API
- **Mail UI:** its own React + TypeScript + Material UI app (`mailui/`, separate container) served same-origin at `/mail/` — appears on the hub as an app tile and shares the hub's session token; no code shared with the hub frontend (`frontend/`) or the search UI (`searchui/`)
- **Search UI:** its own React + TypeScript + Vite + Material UI app (`searchui/`, separate container) served same-origin at `/search/` — the search app (searches, streamed ingest/embedding progress, enrichment); extracted from `frontend/`, mirrors `mailui`/`agentsui`; appears on the hub as an app tile and shares the hub's session token
- **MCP server:** Python MCP SDK (streamable HTTP) — internal-only read-only tools over the leads backend for agents; every tool call carries a per-profile session token that is forwarded upstream
- **Frontend:** React + TypeScript + Vite + Material UI — the **hub only**: nondescript landing (sign in / request access) + post-login hub (profile, apps, admin panels). The search app moved out to the standalone `searchui/` (served at `/search/`)
- **Deploy:** Docker Compose (interim), nginx, Postgres, MongoDB, Keycloak — migrating to k3s + Istio + OPA (see `deploy/`)

## Features

- Minimal landing page (no product info) with sign-in and "request access" (username only)
- Post-login hub: current profile (username + role), configured web apps (Search, Mail)
- Admin panels (admin role only): pending account requests, user management, role management with per-service grants
- Every API endpoint — public or internal — requires a JWT whose audience names the service, except the explicitly annotated anonymous allowlist (webhooks, OAuth callback, probes)
- Leads service: store Apollo people/org search hits in MongoDB (deduped by `apollo_id`), embed in Milvus in the background, and enrich via Complete Person/Organization Info
- Mail service: connect Gmail/Workspace mailboxes across any domains via OAuth, archive all their mail in Postgres (kept in sync incrementally), browse inbox/sent per mailbox in the hub Mail app, and send email as any connected mailbox

## Identity model

The full reference (two identities per request, per-hop audiences, token exchange, the anonymous allowlist, dev vs prod) lives in [`docs/authentication.md`](docs/authentication.md). In short:

- **Keycloak** issues all identity: humans (auth-code + PKCE), services and AI agents (confidential clients). Users/roles are managed in the Keycloak console (linked from the hub for admins).
- Every service accepts only JWTs whose `aud` names it; internal hops exchange (never forward) tokens via RFC 8693 — the realm's `svc-<target>` client scopes are the one-hop pairing allowlist (`search→leads`, `mcp→leads`, `agent→mcp`).
- Requests in the mesh also carry the calling workload's mTLS identity; OPA (ext_authz) decides on both. Applications receive the principal through `fm_runtime` and key user-owned data (search history) on `preferred_username`.

### Provisioning users

Human access is granted via **Keycloak groups** (role bundles), not by picking per-service roles one at a time. The realm ships three defaults (`deploy/keycloak/realm-funnelmanager-*.json` `groups` block) — **adjust these to taste**:

| Group | Grants | For |
|---|---|---|
| `/standard` | `search-access`, `mail-access` | day-to-day users |
| `/power` | `+ jobs-access`, `agents-access` | full product surface |
| `/admins` | `admin` (all services) | administrators |

Groups only ever confer the human-facing `-access` roles or `admin` — never the machine roles (`internal-service`, `jobs-internal`); `python -m fm_runtime.export --check … --realm` fails closed otherwise.

Provision against the live prod realm with the `provision-user` skill (kcadm inside the Keycloak pod):

```bash
.claude/skills/provision-user/provision.sh create alice alice@acme.com standard   # + temp password, forced reset
.claude/skills/provision-user/provision.sh add    alice power                      # change tier
.claude/skills/provision-user/provision.sh remove alice standard
.claude/skills/provision-user/provision.sh list-users
.claude/skills/provision-user/provision.sh list-groups
```

Changing *who* is in a group is a pure Keycloak op; changing the *bundles* is a realm edit (mirror both realm files + the skill's role map, then re-run the `--check`). Full detail: [`docs/authentication.md`](docs/authentication.md#what-each-client-type-interfaces).

## Quick start (Docker development)

```bash
cp .env.example .env
# Edit .env and set APOLLO_API_KEY

docker compose -f docker-compose.dev.yml up --build
```

- App (nginx → Vite): http://localhost:5173
- APIs (nginx → backends): http://localhost:8000/api
  - `/api/search/…` → search backend (search/history/enrich)
  - `/api/mail/…` → mail backend
  - Leads is internal-only (`LEADS_BACKEND_URL`); the browser does not call it
- Keycloak: http://localhost:8080 (realm `funnelmanager`; console login `admin`/`admin`)
- Postgres: localhost:5432; MongoDB: localhost:27017

nginx is the public reverse proxy in front of Vite and the search/mail backends. The leads backend is reached only by other services on the Docker network; Keycloak is published so the browser can run the OIDC flow.

The MCP server listens at `http://localhost:8003/mcp` in dev (streamable HTTP). nginx never routes to it; in prod it is published on loopback only (`127.0.0.1:8003`).

One short name per backend is used everywhere — source directory, compose
service, container / DNS name, GHCR image, and API prefix all match:
`search` (`/api/search`), `leads` (`/api/leads`), `mail` (`/api/mail`), and
`mcp` (internal `/mcp`).

Source is bind-mounted:

- `./search`, `./leads`, `./mail`, `./mcp` → their containers
- `./libs/fm_runtime` → every backend (editable install; live lib edits)
- `./frontend`, `./searchui`, `./mailui` → their Vite dev containers
- `./frontend/nginx.dev.conf` → `nginx` container

Default login: `admin` / `admin` (seeded by the dev Keycloak realm import;
manage users in the Keycloak console afterwards).

## Production (Docker + nginx)

```bash
cp .env.prod.example .env.prod
# Set DOMAIN, KC_* / FM_OIDC_*, APOLLO_API_KEY, POSTGRES_PASSWORD, CORS_ORIGINS, DATABASE_URL

# Prod uses prebuilt images from GHCR (no local builds) — built and pushed by
# .github/workflows/deploy-prod.yml; see deploy/README.md for the full flow.
docker compose -f docker-compose.prod.yml --env-file .env.prod pull
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
```

- nginx serves the built SPA on port 80 for `${DOMAIN}`
- `/api/search/*` → search FastAPI (Postgres), which relays to leads internally; `/api/mail/*` → mail
- Keycloak must be reachable at `KC_HOSTNAME` (browser-facing HTTPS; TLS in front of its published port)
- Postgres and MongoDB data are stored in Docker volumes

Point your DNS A/AAAA record for `${DOMAIN}` at the host. Put TLS in front (Cloudflare, Caddy, Traefik, or certbot) as needed.

## Local non-Docker (optional)

### Search backend

```bash
cd search
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
cd leads
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Set MONGODB_URL (e.g. mongodb://127.0.0.1:27017) and APOLLO_API_KEY
uvicorn app.main:app --reload --port 8001
```

### Mail backend

```bash
cd mail
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Set DATABASE_URL, GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET, and
# OAUTH_REDIRECT_URL (must be registered on the Google OAuth client).
uvicorn app.main:app --reload --port 8004
```

### MCP server

```bash
cd mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
# Set LEADS_BACKEND_URL to a reachable leads service (e.g. http://127.0.0.1:8001).
# Non-Docker runs also need: pip install -e ../libs/fm_runtime
uvicorn app.main:app --reload --port 8003
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

Vite proxies `/api` for non-Docker local runs (`VITE_API_PROXY_TARGET`, default `http://127.0.0.1:8000`). The hub frontend no longer hosts the search app — it moved to the standalone `searchui/` (below). In Docker Compose, nginx is the public entry and routes `/api/search` traffic to the search backend.

### Search UI

```bash
cd searchui
npm install
npm run dev   # serves at http://localhost:5173/search/ (base /search/)
```

Standalone app — same MUI theme, no shared code with `frontend/` (extracted from it; mirrors `mailui`/`agentsui`). It shares the hub's Keycloak OIDC session via localStorage (`fm_oidc_*` keys; it can also sign in on its own via `/search/callback`) and proxies `/api` to `VITE_API_PROXY_TARGET` (default `http://127.0.0.1:8000`).

### Mail UI

```bash
cd mailui
npm install
npm run dev   # serves at http://localhost:5173/mail/ (base /mail/)
```

Standalone app — same MUI theme, no shared code with `frontend/`. It shares the hub's Keycloak OIDC session via localStorage (`fm_oidc_*` keys; it can also sign in on its own via `/mail/callback`) and proxies `/api` to `VITE_API_PROXY_TARGET` (default `http://127.0.0.1:8004`).

## Environment

| Variable | Description |
|---|---|
| `APOLLO_API_KEY` | Apollo master API key (**leads backend only**) |
| `KC_ADMIN_USERNAME` / `KC_ADMIN_PASSWORD` | Keycloak bootstrap admin (console account, not a realm user) |
| `KC_HOSTNAME` | Browser-facing Keycloak URL (prod; TLS-terminated in front) |
| `FM_OIDC_ISSUER` | OIDC issuer as seen by browsers and asserted in tokens, e.g. `https://kc.<domain>/realms/funnelmanager` |
| `FM_OIDC_<SVC>_SECRET` | Confidential client secret per service (`SEARCH`/`LEADS`/`MAIL`/`MCP`) — must match the imported realm |
| `KEYCLOAK_REALM_FILE` | Realm import file. **Required in prod** (no default — start from `deploy/keycloak/realm-funnelmanager-prod.example.json`); dev compose mounts the tracked dev realm |
| `FM_SERVICE_NAME`, `FM_JWT_VERIFY`, `FM_OIDC_TOKEN_URL`, `FM_OIDC_JWKS_URL`, `FM_OIDC_CLIENT_ID`, `FM_OIDC_CLIENT_SECRET`, `FM_SERVICE_AUDIENCE`, `FM_ENFORCE_AUDIENCE`, `FM_REQUIRE_PRINCIPAL`, `FM_ENFORCE_GRANTS`, `FM_ROLE_GRANTS`, `FM_ROLE_GRANTS_FILE`, `FM_EXCHANGE_SCOPE_TEMPLATE`, `FM_LOG_LEVEL` | fm_runtime per-service identity config (set by compose/k8s manifests; see `libs/fm_runtime/fm_runtime/settings.py`). `FM_ENFORCE_GRANTS` applies OPA's role-grant rule in-process where no mesh runs (both compose files set it) |
| `FRONTEND_OIDC_CLIENT_ID` | Public browser client id (default `frontend`); baked with the issuer into `/config.js` at container start |
| `WEB_APPS` | JSON list of hub apps `[{"name","description","url"}]` baked into `/config.js`; blank = default Search (`/search/`) + Mail (`/mail/`) entries |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Google OAuth client (Web application type, Gmail API enabled) used by the mail backend to connect mailboxes — scopes `gmail.readonly` + `gmail.send` |
| `MAIL_OAUTH_REDIRECT_URL` | Authorized redirect URI registered on the OAuth client; blank = derived from `PUBLIC_BASE_URL`, dev default `http://localhost:8000/api/mail/oauth/callback` |
| `MAIL_DATABASE_URL` | Mail backend Postgres URL (prod compose; the dedicated `mail-db` container) |
| `MCP_SHARED_LOGIN_FALLBACK` | MCP server: tokenless tool calls act as the MCP service identity via client credentials (dev compose: `true`; prod default: `false`) |
| `CORS_ORIGINS` | Allowed frontend origins (comma-separated) |
| `DATABASE_URL` | Async SQLAlchemy URL (`postgresql+asyncpg://...`) |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Postgres bootstrap (compose) |
| `MONGODB_URL` | MongoDB connection URL |
| `MONGODB_DB` | MongoDB database name for leads |
| `LEADS_BACKEND_URL` | Internal URL used by the search backend to call leads (e.g. `http://leads:8001`) |
| `MCP_ALLOWED_HOSTS` | Host headers the MCP transport accepts (default `mcp:8003,localhost:8003,127.0.0.1:8003`) |
| `DOMAIN` | Production hostname for nginx `server_name` |

## API

### Identity (Keycloak)

There is no in-repo auth API anymore. Sign-in, tokens, users, and roles live in Keycloak (realm `funnelmanager`): browsers run auth-code + PKCE against `FM_OIDC_ISSUER`, services exchange tokens per hop (RFC 8693), and admins manage principals in the Keycloak console (linked from the hub). See [`docs/authentication.md`](docs/authentication.md). Every backend also serves `/healthz`, `/readyz`, and Prometheus `/metrics`, plus an authenticated `GET /api/{service}/whoami` (principal echo: sub, username, roles). Unlike the probes it is **not** `@anonymous` — reaching it requires a valid JWT and a covering role grant, which is what the hub uses for app discovery: each tile's `probe` URL is fetched with the user's token and the tile is hidden on 401/403, so the tile list always mirrors what OPA/grants actually allow.

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

Search history is per-user: every `/api/search/searches*` endpoint is scoped to the authenticated profile (other users' searches 404).

### Mail backend

nginx routes `/api/mail/*` here and `/mail/` to the standalone mail UI (`mailui`, its own React app + container; same origin as the hub, so it shares the hub's Keycloak session — unauthenticated visits redirect to Keycloak). Every API route requires a mail-audience JWT, except the probes and the OAuth callback (validated by a single-use `state` bound to the user who started the flow). Data lives in the dedicated `mail-db` Postgres container; mailbox OAuth refresh tokens are stored there too — treat it as secret material.

Connecting a mailbox: hit **Connect** in the Mail app → Google consent (offline access) → callback stores tokens and starts syncing. The background loop (every `SYNC_INTERVAL_SECONDS`, default 180) backfills the whole mailbox newest-first (`BACKFILL_PAGES_PER_CYCLE` pages of 100 per cycle) and applies Gmail history increments (new mail, deletions → `is_deleted` flag, label changes). Messages deleted on Google's side stay archived here.

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/mail/accounts` | Connected mailboxes with sync status + inbox/sent/total counts |
| `DELETE` | `/api/mail/accounts/{id}` | Remove a mailbox and its stored messages |
| `POST` | `/api/mail/accounts/{id}/sync` | Trigger a sync cycle now (202) |
| `GET` | `/api/mail/oauth/url` | Mint the Google consent URL (single-use state) |
| `GET` | `/api/mail/oauth/callback` | OAuth redirect target — bounces back to `/mail?connected=…` or `?error=…` |
| `GET` | `/api/mail/accounts/{id}/messages` | Paged list; `label=INBOX\|SENT\|ALL` (any Gmail label id), `q=` substring filter, `page`/`per_page` |
| `GET` | `/api/mail/messages/{id}` | Full message (text + HTML bodies, attachment metadata) |
| `GET` | `/api/mail/messages/{id}/attachments/{attachment_id}` | Attachment bytes (proxied live from Gmail) |
| `POST` | `/api/mail/accounts/{id}/send` | `{to, cc, bcc, subject, body_text, body_html?, reply_to_message_id?}` — sends via Gmail and stores the sent message immediately |

### Leads backend (internal)

Reached by the search backend and MCP server at `LEADS_BACKEND_URL` (not exposed to the browser via nginx, except Apollo webhooks). Every route requires a leads-audience JWT (obtained by callers via RFC 8693 exchange), except the probes and the secret-in-path Apollo webhooks.

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

[MCP](https://modelcontextprotocol.io) server for internal AI-agent clients — streamable HTTP at `/mcp`, plus `GET /health`. It is never routed through nginx: dev publishes `8003` on the host, prod publishes `127.0.0.1:8003` (loopback only).

**Auth:** every tool accepts an optional `session_token` argument; the MCP server also honors an `Authorization: Bearer` header on the `/mcp` request (the explicit argument wins). The token is the acting principal's (aud `mcp`); the server exchanges it (RFC 8693) for a leads-audience token per upstream call, so leads sees the same principal with `azp: mcp`. With `MCP_SHARED_LOGIN_FALLBACK=true` (dev), tokenless calls act as the MCP server's own service identity via client credentials.

All tools are read-only inspection over the leads backend (free, no Apollo calls — new searches/enrichment happen in the search app):

| Tool | Backing call | Description |
|---|---|---|
| `leads_stats` | leads `GET /api/leads/stats` | Collection counts: entity types, embedding, enrichment |
| `recent_leads` | leads `GET /api/leads/recent` | Enrichment/ingest activity feed with per-lead Apollo endpoint timeline |
| `get_leads` | leads `POST /api/leads` | Batch hydrate by Mongo `_id` (compact summaries; `include_raw` for full docs) |
| `similarity_search` | leads `POST /api/leads/similarity-search` | Semantic search over stored leads (no Apollo call, no history row) |

The transport's DNS-rebinding protection allows `mcp:8003`, `localhost:8003`, and `127.0.0.1:8003` by default — extend via `MCP_ALLOWED_HOSTS` if clients dial another hostname.

Point an MCP client at `http://127.0.0.1:8003/mcp` (transport: streamable HTTP). Example config:

```json
{
  "mcpServers": {
    "funnelmanager": { "type": "http", "url": "http://127.0.0.1:8003/mcp" }
  }
}
```
