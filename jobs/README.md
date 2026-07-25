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
   (`app/control.py`, exchange → the app's audience). Idempotent. Distinct from any
   future UI routes (principle 2). Cross-user visible.

## Auth / exchange (flag for security review)

- Inbound audience `jobs` (`fm_runtime` PrincipalMiddleware). The `mcp` server calls
  these routes (svc scope `mcp→jobs`, `azp_allow[jobs] = [mcp]`).
- Outbound: the subscriber uses `jobs→search` / `jobs→agents` as the jobs service's
  own identity (client credentials) for the **read-only** stream. The control proxy
  uses a context-following client, so the **acting human's** token is exchanged
  `jobs→{app}` (human stays the subject, `azp` becomes `jobs`) — the producer
  authorizes the `jobs` caller on its `/internal/jobs/v1/*` control API.
- These svc scopes + `azp_allow` + grants were provisioned in **Phase 0**
  (`fm_runtime/grants.py` `SVC_EXCHANGE_SCOPES`, `deploy/policy/data.json`,
  the realm). Adding a producer means adding its `jobs→<app>` edge there too.
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

(Bare-dev passthrough is a local shortcut — no token endpoint, so the outbound
control call carries no exchanged token, which the auth-less stub ignores. In
compose/mesh the exchange runs and OPA/audience enforce access. Setting the
producer's audience via `JOBS_PRODUCERS` name must match a real `jobs→<name>`
svc scope there.)

There is **no test suite** in this repo — verify by running the service.
