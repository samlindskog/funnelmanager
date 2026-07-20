# Authentication & Authorization

**Authentication** is centralized in the auth service using **server-side opaque sessions** stored in Redis — no JWTs. **Authorization** is **role grants evaluated by OPA**; the auth service is the only thing that talks to OPA (it owns and pushes the policy). Every other service holds zero identity state and is a pure enforcement point: for each incoming request it asks the auth service one question — `POST /api/auth/authorize` — and obeys the answer. Decisions **fail closed**. The same bearer token rides every hop (browser → search, search → leads, MCP → leads), so each hop independently re-authorizes the same end user.

## Trust boundaries (what nginx exposes)

nginx is the only public entry:

| Location | Upstream |
|---|---|
| `/api/auth/` | `auth:8002` |
| `/api/search/` | `search:8000` |
| `/api/leads/webhooks/` | `leads:8001` (the *only* leads path exposed) |
| `/` | frontend (Vite in dev, built SPA in prod) |

Deliberately **never routed**: the rest of `/api/leads/*`, OPA, Redis, and `/mcp` (published on host `:8003` in dev, loopback-only in prod).

## Endpoint inventory

### Auth service — public surface (`/api/auth/*`), three access tiers

**Tier 1 — anonymous** (`auth/app/routers/auth.py`):

| Endpoint | What it does |
|---|---|
| `POST /api/auth/login` | OAuth2 form → bcrypt check (timing-equalized dummy verify on unknown usernames) → mints a random token (`secrets.token_urlsafe(32)`), stored at `session:<token>` in Redis with native TTL (`SESSION_TTL_SECONDS`, default 1 day) |
| `POST /api/auth/request-account` | Always answers 202 (no username enumeration); pending set capped at 500 |
| `GET /api/auth/health` | Liveness |

**Tier 2 — any valid session** (the token in the request *is* the credential):

| Endpoint | What it does |
|---|---|
| `GET /api/auth/me` | Token → `{username, role}` |
| `GET /api/auth/apps` | Hub app list from `WEB_APPS` (default: Search at `/search` + Mail at `/mail/`) |
| `POST /api/auth/logout` | Deletes the Redis session key — instant revocation |
| `POST /api/auth/validate` | Body `{token}` → introspection for other backends |
| **`POST /api/auth/authorize`** | **The** endpoint: `{token, service, method, path}` → resolve session (401 if dead/expired) → OPA decision → `{allowed, username, role}`; **503 when OPA is unreachable (fail closed)** |

**Tier 3 — OPA-gated admin** (`require_authorized` in `auth/app/security.py` — auth gates *itself* through the same authorize mechanism, service `"auth"`):

- Users: `GET|POST /api/auth/admin/users`, `PATCH|DELETE .../users/{username}` (self-delete + last-admin guards serialized under a write lock)
- Roles: `GET|POST /api/auth/admin/roles`, `DELETE .../roles/{name}` (admin role undeletable; in-use undeletable; every change re-pushed to OPA)
- Requests: `GET .../account-requests` + `approve`/`deny`

### OPA (`:8181`, auth-only client — `auth/app/opa.py`)

`PUT /v1/policies/funnelmanager`, `PUT /v1/data/funnelmanager/roles`, `POST /v1/data/funnelmanager/authz/allow`. Auth pushes at startup, on role changes, every 60 s, and re-pushes when a decision comes back undefined (OPA restarted → self-heals). Grants are `{service, methods, path_prefix}` with `"*"` wildcards; path matching is exact or **segment-boundary** prefix (`/api/search/search` does *not* authorize `/api/search/searches`).

### Search service (`/api/search/*`, service name `search`)

All routes carry `Depends(get_current_user)` (`search/app/auth.py`): extract bearer → `authorize(service="search")` → 401 / 403 (`allowed: false`) / 503 (auth down). Only `GET /api/search/health` is anonymous. Every route also constructs `LeadsClient(settings, token)` with the caller's raw bearer, so **leads re-authorizes the same user** — including NDJSON streams and detached ingest jobs, which capture the token for the job's lifetime.

### Leads service (`/api/leads/*`, service name `leads`, internal except webhooks)

One **router-level** dependency (`enforce_authorization`, `leads/app/auth.py`) covers every route: missing bearer → 401, else `authorize(service="leads")`. The dependency itself exempts exactly `GET /api/leads/health` and `/api/leads/webhooks/*`. The webhook (`POST /api/leads/webhooks/apollo[/{secret}]`) refuses to serve (503) unless `APOLLO_WEBHOOK_SECRET` is configured, and does a constant-time compare of the path/query secret — Apollo cannot send bearer tokens, so this is the one secret-in-path exception.

### MCP server (`:8003`)

`/mcp` is the MCP protocol mount (not REST; Host-header allowlist `mcp:8003,localhost:8003,127.0.0.1:8003`); `GET /health` anonymous. Per-tool-call token priority: ① `session_token` tool argument → ② `Authorization: Bearer` on the `/mcp` request → ③ shared-login fallback **only** when `MCP_SHARED_LOGIN_FALLBACK=true` (dev). The fallback token is transparently re-minted on 401; explicit tokens never are — those errors surface to the agent with instructions to fetch a fresh one.

## How each client type interfaces

**Browser:** login → `fm_token` in `localStorage` → `frontend/src/api.ts` adds `Authorization: Bearer` to every request (streams included). A mid-session 401 clears the token and fires `onUnauthorized` → `AuthProvider` drops the user → redirect to `/login`; transient auth-service outages do *not* drop the token. The hub reads `me`/`apps`; admin panels call `/api/auth/admin/*` (UI hides them for non-admins, but the server is the enforcement point); the search app calls `/api/search/*`.

**Internal services:** one uniform pattern everywhere — forward the caller's bearer, ask `authorize` with your own service name + method + path. A single browser action that reaches leads is authorized **twice** (search's check, then leads' check) — deliberate defense in depth; internal ≠ trusted.

**MCP clients:** pass the acting profile's session token on every tool call (`session_token` argument or `Authorization: Bearer` header); the MCP server forwards it to the leads backend, which re-authorizes it, so the client acts with **exactly that profile's role**.

## Session semantics

Sessions store **only the username**; role and existence are re-resolved on every check — so role changes and user deletions bite live sessions instantly, and logout is an instant revocation. No refresh tokens: expiry means the browser re-logs-in. `AUTH_USERNAME`/`AUTH_PASSWORD` only bootstrap the first admin user (created if missing, never updated).
