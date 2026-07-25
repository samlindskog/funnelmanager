# jobs service

One cross-app view of async work. `jobs` is an **observer + controller**, never a
doer: it does not run searches, embeddings, campaigns, or agent tasks — it tracks
and steers the jobs the owning apps run.

- Port `8005`, database `funnelmanager_jobs` (own logical DB, created on first boot).
- Audience `jobs`. Prod binds **loopback only** like `mcp` — never nginx-routed.
- FastAPI + async SQLAlchemy (asyncpg), structurally modeled on `mail/`.

## What it does

1. **Job store** (`app/models.py`, `app/store.py`) — one `jobs` row per tracked unit
   of work, keyed by `(app, external_job_id)`. Fields: `id, app, external_job_id,
   type, user (human owner), origin, actor, status, progress, exit_status,
   started_at, ended_at, last_event_at, meta`. **Cross-user visible by design**
   (guiding principle 1): `user`/`origin`/`actor` *attribute* the work; nothing here
   is ever used to *filter a read by owner*. Keycloak's audience + role is the only
   access gate.

2. **Subscriber framework** (`app/subscriber.py`) — one background task per
   configured producer that tails the producer's `GET /internal/jobs/v1/stream`
   (NDJSON of `fm_runtime.job_events.JobEvent`) and upserts job state. It
   **exchanges, never forwards**: it calls with `audience = <producer>` as this
   service's own client-credentials identity (`azp = jobs`). Producers are
   **config-driven** (`JOBS_PRODUCERS`); v1 producers are `search` + `agents` only,
   and mail/others can join later with no code change. A producer that is
   absent/unreachable is tolerated — reconnect with capped backoff, never crash.
   Upserts are **replay-safe**: events strictly older than the newest already applied
   are ignored, so a redelivered `running` can never regress a `completed`.

3. **MCP-facing API** (`app/routers/mcp.py`, `/api/jobs/mcp/v1/*`) — `list` jobs
   (filter by user/app/status), `get` a job + progress, and `pause|resume|cancel`
   that **proxy** to the owning app's `POST /internal/jobs/v1/{id}/{action}`
   (`app/control.py`). Idempotent. Distinct from any future UI routes (principle 2).
   Cross-user visible.

## Auth / exchange — the internal-jobs trust boundary (flag for security review)

The two-tier trust for a control action:

- **Tier 1 — the human is authorized at the jobs MCP API.** Inbound audience
  `jobs` (`fm_runtime` PrincipalMiddleware) + the `jobs-access` grant. The `mcp`
  server reaches these routes (svc scope `mcp→jobs`, `azp_allow[jobs] = [mcp]`).
  This is the ONLY place the human is authorized; it happens *before* the proxy.
- **Tier 2 — the producer trusts the jobs SERVICE ACCOUNT, never the human.** A
  producer's `/internal/jobs/v1/*` endpoints (BOTH stream read and control write)
  are callable ONLY by `jobs`, authorized by a dedicated **`jobs-internal`** realm
  role held by the `jobs` client's service account (defined by the runtime
  workstream). So **both** the subscriber (read) **and** the control proxy (write)
  authenticate as `jobs` via **client-credentials** (`azp = jobs`), exchanging
  `jobs→search` / `jobs→agents` — the control proxy uses a non-context-following
  `InternalClient` with no subject token, so it mints the same service-account
  token as the subscriber rather than the acting human's.
- **The human rides as AUDIT METADATA, not a token.** The control call carries
  `X-FM-Acting-User` (+ `X-FM-Acting-Origin`, `X-FM-Acting-Actor`) derived from
  the request principal, so the producer can attribute the action ("alice",
  "alice (via agent)") without the human ever crossing the internal-jobs boundary
  as a credential.
- These svc scopes + `azp_allow` + the `jobs-internal` role/grant are provisioned
  in the platform/runtime workstream (`fm_runtime/grants.py` `SVC_EXCHANGE_SCOPES`,
  `deploy/policy/data.json`, the realm). Adding a producer means adding its
  `jobs→<app>` edge (and granting the producer's `jobs-internal` role) there too.
- Only anonymous route: `GET /api/jobs/health` (legacy probe; k8s uses
  `/healthz` + `/readyz`).

## Not wired here (deferred to platform-agent, Phase 2)

Per the build plan, docker-compose / k3s manifests / the dedicated `jobs-db` are
**intentionally not added in this phase** — that wiring lands with `search` (the
first real producer) so the whole path can be integration-tested end-to-end. This
service is self-contained and boots against any Postgres; the platform wiring
(compose service, `jobs-db`, prod loopback bind, manifests) is a separate step.

## Local verification (no cluster, no real producers)

A throwaway stub producer (`scripts/stub_producer.py`, not shipped in the image)
implements the P0 producer contract so the full path can be exercised now:

```bash
# 1) Postgres (any instance). Then, from repo root, install deps into a venv:
python3 -m venv jobs/.venv && source jobs/.venv/bin/activate
pip install -e libs/fm_runtime -r jobs/requirements.txt uvicorn

# 2) Start the stub producer (a fake "search") on :8000
JOBS_STUB_STEP_SECONDS=2 python jobs/scripts/stub_producer.py

# 3) Start jobs (bare-dev passthrough exchange) pointed at the stub, in jobs/:
cd jobs
DATABASE_URL=postgresql+asyncpg://funnel:funnel@localhost:5432/funnelmanager_jobs \
JOBS_PRODUCERS=search=http://localhost:8000 \
FM_SERVICE_NAME=jobs \
uvicorn app.main:app --port 8005

# 4) The MCP routes ALWAYS require a principal (Depends(require_principal)), so
#    mint a dev token (bare dev = FM_JWT_VERIFY off, so an unsigned/HS256 JWT
#    with aud=jobs parses). Then watch the job appear + progress, and steer it:
TOKEN=$(python -c "import jwt,time; print(jwt.encode({'preferred_username':'tester','aud':['jobs'],'exp':int(time.time())+3600,'realm_access':{'roles':['admin']}}, 'x'*32))")
H="Authorization: Bearer $TOKEN"
curl -H "$H" localhost:8005/api/jobs/mcp/v1/jobs
curl -X POST -H "$H" localhost:8005/api/jobs/mcp/v1/jobs/1/pause
curl -X POST -H "$H" localhost:8005/api/jobs/mcp/v1/jobs/1/resume
curl -X POST -H "$H" localhost:8005/api/jobs/mcp/v1/jobs/1/cancel
```

The stub logs and echoes the `X-FM-Acting-User` audit header (plus origin/actor),
so the control responses look like `{"status": "paused", "acting_user": "tester"}`
— confirming the acting human is carried as metadata even though, in the mesh, the
CALL itself is the jobs service account's (client-credentials, `azp=jobs`).

(Bare-dev passthrough is a local shortcut — no token endpoint, so the outbound
control call carries no exchanged token, which the auth-less stub ignores; the
audit headers are still sent. In compose/mesh the client-credentials exchange
runs as the jobs service account (`jobs-internal` role) and OPA/audience enforce
access. Setting the producer's audience via `JOBS_PRODUCERS` name must match a
real `jobs→<name>` svc scope there.)

There is **no test suite** in this repo — verify by running the service.
