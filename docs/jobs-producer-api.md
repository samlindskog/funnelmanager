# Jobs Producer API (v1)

The **standardized contract every service implements so the `jobs` service can view and control that service's in-flight work.** `jobs` is a pure aggregator/controller — it runs no work of its own; each producing service exposes a uniform surface and `jobs` subscribes to it.

This is the authoritative spec. The shared implementation is `fm_runtime.JobProducer` (`libs/fm_runtime/fm_runtime/jobs_producer.py`) over the wire schema in `fm_runtime.job_events` (`libs/fm_runtime/fm_runtime/job_events.py`). `search/app/jobs_registry.py` + `search/app/routers/internal_jobs.py` are the **reference wiring**.

## Who is a producer

Any service with **running / long-running / scheduled** in-service work worth surfacing for introspection and control: `search` (`apollo_search`, `embedding`), `agents` (`agent_turn`, `agent_schedule`), and future services. A service becomes a producer by (a) registering with `jobs` and (b) exposing the two routes below, wired to `fm_runtime.JobProducer`.

## The two routes (versioned `/internal/jobs/v1`, `jobs`-only)

Both are callable **only by the `jobs` service account** (realm role `jobs-internal`, scoped to `/internal/jobs` on the producer — enforced by OPA in the mesh / `fm_runtime` grants in compose). Never nginx-exposed; prod-internal only (loopback like `mcp`).

### `GET /internal/jobs/v1/stream`
An NDJSON stream of `JobEvent`s. On connect: replay a **snapshot of every non-terminal job** (running / paused / queued / **scheduled**), then live events. **Never raise once the response has started** (P8) — a stray exception resets the connection and `jobs` sees a generic network error, then reconnects. `JobProducer.stream_ndjson()` is the never-raise generator; wire it as `StreamingResponse(producer.stream_ndjson(), media_type="application/x-ndjson")`.

### `POST /internal/jobs/v1/{job_id}/{action}`
`action ∈ {pause, resume, cancel}` (`JobControlAction`). The producer applies it to its own engine **under its own service identity** (the acting human never crosses this boundary as a token — they were authorized earlier at the jobs API and ride only as the `X-FM-Acting-*` audit header), then **re-publishes a `JobEvent`** so the stream stays the single source of truth for job state. `JobProducer.dispatch_control(job_id, action)` is the apply-then-republish scaffold. A control on a terminal job is a no-op.

## The wire schema (`fm_runtime.job_events`)

Defined once and imported by every side, so producers and the consumer can never drift.

**`JobEvent`** — `job_id` (the producer's own id, unique within its app), `type` (free-form kind; the app owns its vocabulary), `user` / `origin` / `actor` (attribution — *never* a read filter), `status` (`JobStatus`), `progress` (`0.0`–`1.0` or `None`), `ts` (ISO-8601 UTC), `exit_status` (terminal only), `meta` (JSON catch-all for counts / stream-ids / future keys), `raw_status` (set only when a wire status was unrecognized). First-party producers construct **strictly** (real enums, so a status typo fails loud in-process); the consumer's wire-decode is **lenient** (an unknown future status falls back to a non-terminal value with `raw_status` preserved, never dropped) — this is what makes `v1` additively evolvable.

**`JobStatus`** — `queued, running, paused, scheduled, completed, failed, canceled`. **Terminal** = `completed / failed / canceled`. `scheduled` = persisted future work that has not yet begun; it is **non-terminal** and therefore *is* in the active snapshot, and it must **not** stamp `started_at` on the consumer (real execution has not started).

**`JobControlAction`** — `pause, resume, cancel`.

## Surfacing philosophy (normative): *idle emits no job*

`jobs` exists to surface **running / long-running / scheduled** work for deterministic CPU/work introspection — **never idle state**. Every producer obeys this:

- **Lazy creation.** A job exists only once the producer publishes its first event — when real work actually starts, or when a schedule is registered. There is no "entity exists ⇒ job" mapping. An idle agent session, a finished search, an un-triggered feature: **none are jobs.**
- **Active-only snapshot.** The subscribe snapshot replays only **non-terminal** jobs. `scheduled` is included (pending work); terminal jobs are not (their terminal event already reached every connected subscriber).
- **Terminal ages out** of producer memory after a short TTL; the durable history lives in the `jobs` store, not the producer. This bounds producer memory to *active* work.

## The shared helper: `fm_runtime.JobProducer`

One instance per producing service. It provides the broadcast hub (latest-event-per-job + N-subscriber fan-out), the active-only snapshot, the terminal TTL prune, the never-raise NDJSON generator, and the control apply-then-republish. A producing service supplies **only two engine callbacks**:

- `enumerate_active_jobs() -> list[JobEvent]` *(optional; sync or async)* — the engine's currently non-terminal jobs, unioned into each subscribe snapshot. This is what lets **persisted** state (e.g. schedules reloaded after a pod restart) be surfaced before any in-memory event exists for it. **Omit** it if the producer keeps no durable out-of-band job store — the hub's own in-memory latest-event cache is then the authoritative snapshot (this is what `search` does).
- `apply_control(job_id, action) -> JobEvent | None` *(sync or async)* — apply a control action to the engine and return the resulting `JobEvent` (which `dispatch_control` re-publishes), or `None` for an unknown job / no-op.

Publish lifecycle transitions with `producer.publish(JobEvent(...))`; the two routes call `producer.stream_ndjson()` and `producer.dispatch_control(...)`. Routing/auth of the routes is service-local (a service names its own job types, ids, and attribution).

> **Evolving (tracked).** `apply_control` currently returns only the published `JobEvent`, so a producer that must thread caller context *in* (e.g. the acting user) or a richer outcome *out* (e.g. an "applied vs. already-in-state" flag) adapts around it. This is being refined **before the second consumer (`agents`) adopts the helper**, with both consumers in view; the change is additive (e.g. an optional `context` argument + reading the outcome off the returned event) and will **not** change the two routes or the wire schema.

## Registration checklist (make a new service a producer)

1. **Add the service to `JOBS_PRODUCERS`** (jobs config, `name=base_url`). The name is simultaneously the `jobs` row's `app`, the token-exchange audience, and the control target.
2. **Auth legs, in lockstep** — verify with `python -m fm_runtime.export --check deploy/policy/data.json --realm <realm>` (green is required):
   - a `jobs→<svc>` svc-exchange edge: the `svc-<svc>` optional client scope on the `jobs` client (both realms), `azp_allow.<svc>` includes `jobs` (`deploy/policy/data.json`), and the entry in `SVC_EXCHANGE_SCOPES` (`fm_runtime/grants.py`).
   - the `jobs-internal` role granting `{methods: *, path_prefix: /internal/jobs}` on `<svc>` (`grants.py` `_DEFAULT_ROLE_GRANTS` + `data.json` `roles` + both realm files).
3. **Construct one `fm_runtime.JobProducer`** and wire the two `/internal/jobs/v1` routes to `stream_ndjson()` / `dispatch_control()` (auth: `jobs-internal` via the standard principal dependency; read `X-FM-Acting-User` for audit only, never as an authz input).
4. **Publish lifecycle events** at the real transitions (work starts → `RUNNING`; a schedule is set → `SCHEDULED`; pause → `PAUSED`; done → a terminal status). Obey *idle emits no job*.

## Consumer side (the `jobs` service)

`jobs` subscribes to each producer's `/stream` as the `jobs` service account and upserts one row per `(app, job_id)` via `store.apply_event`, guarded by **terminal-absorbing** (a non-terminal event on a terminal row is dropped) + **ts-ordering** (older-than-newest-applied dropped; equal-ts idempotent newest-wins). `started_at` is stamped on the first status past `queued`/`scheduled`; `ended_at` on the first terminal. The MCP surface (`/api/jobs/mcp/v1`) lists / gets / controls; `status=scheduled` filters pending work. `jobs` never deletes — the active view sorts most-recent-active first and callers filter by `status`.

## Reference implementation

- `search/app/jobs_registry.py` — constructs the `JobProducer`, keeps `publish_job(...)`, supplies the `apply_control` engine callback (maps a control action onto the leads stream engine under search's own `search→leads` identity).
- `search/app/routers/internal_jobs.py` — the two thin route adapters.

`search` supplies **no** `enumerate_active_jobs` (its jobs are purely in-memory), which is the minimal producer shape.
