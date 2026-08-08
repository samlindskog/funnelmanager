# Agent-team build plan — jobs, AI agents, campaigns

This is the shared coordination spec for a multi-service program executed by the
agent fleet (`.claude/agents/`). Each owning agent reads this plus the project
`CLAUDE.md`. It is the source of truth for the target architecture **until
`CLAUDE.md` is updated in Phase 0** to describe the same thing.

> **STATUS (2026-08-06) — the jobs/agents program is DELIVERED.** `jobs`, `agents`,
> and `agentsui` are built and running. Beyond this original plan, the `agents`
> backend was rebuilt from one-shot **runs → interactive multi-turn SESSIONS** (chat):
> NDJSON-streamed turns, per-session model choice, verbatim history + summarization,
> per-turn token usage, in-chat HITL approvals, and an internal `schedule_agent_job`
> tool (persisted schedules fired by an in-process poller; a non-terminal `SCHEDULED`
> job status). Telemetry is **Pydantic Logfire** (dual-export to Tempo, gated to
> dev/canary). The producer plumbing is the shared `fm_runtime.JobProducer` helper
> (`docs/jobs-producer-api.md`). Full program plan + phase history:
> `~/.claude/plans/immutable-swimming-frog.md`. The section below is the original
> jobs/agents/campaigns spec; the sessions/scheduling/telemetry delta lives in
> `agents-agent.md` / `agentsui-agent.md` and that plan.

## Glossary (three different "agents" — do not conflate)
- **Claude agent** — a subagent in `.claude/agents/*.md` that *builds* a service
  (e.g. `search-agent`). This doc's topology.
- **Runtime AI agent** — a pydantic-ai agent the new `agents` service *runs* to
  complete a user's task. A product feature.
- **Adversarial reviewer** — global `bug-hunter` / `security-reviewer` /
  `quality-reviewer` that verify a Claude agent's diff.
- **Job** — a tracked unit of async work (a search ingest, an embedding pass, a
  runtime-agent run, a campaign send) surfaced by the new `jobs` service.

## Guiding principles (binding)
1. **Uniform functionality per user.** Every authenticated user of a service sees
   the *same* features and the *same* data. There is **no per-user data hiding in
   app code** — Keycloak (per-service audience + role) is the only access gate.
   Concretely this reverses today's owner-only filtering: search history, mail
   inboxes, and campaigns become visible to every user of that service. Writes are
   still *attributed* to the acting principal; they are just not *hidden* from others.
2. **MCP uses APIs separate from the UI's.** When a service exposes MCP
   functionality, it does so through **dedicated endpoints**, never the ones the UI
   calls. Convention: MCP-facing routes live under `/api/{service}/mcp/…`. (Leads is
   already MCP-suitable; search gets new `/api/search/mcp/v1/*` endpoints.)
3. **New service ⇒ new local Claude agent**, created in `.claude/agents/` and thereby
   auto-wired into the project self-improvement hooks (the capture hook keys off the
   existence of `.claude/agents/<name>.md`). Done here for `jobs`, `agents`, `agentsui`.
4. **Guard expensive actions.** Any operation that may consume an extreme amount of
   resources (a full mailbox backup, a large embeddings backfill, a huge search or
   campaign) must **estimate first and require confirmation before proceeding** — never
   silently run something costly. Pattern: the endpoint computes an estimate; if it
   exceeds a **configurable threshold**, it returns `409 confirmation_required` with the
   estimate + a `confirm` token instead of running; the caller re-invokes with
   `confirm=true`. A **UI renders a dialog**; a **runtime AI agent must escalate the
   confirmation to its human** (never auto-confirm). Concrete default: **mailbox backup
   > 2 GB** requires confirmation. Provide the estimate→confirm helper in `fm_runtime`
   so every service does it identically. (Folded into `CLAUDE.md` in Phase 0.)
   **Agent hard-enforcement (decided Phase 4, built Phase 5):** the gate **rejects
   `confirm=true` when `origin=agent`** on an over-threshold action — a runtime agent
   can never self-confirm. Instead the run **pauses** and surfaces a *pending approval*
   to the initiating human (in `agentsui`); the human approves out-of-band and the run
   resumes. The confirmation helper becomes origin-aware; `agents` gains a pending-
   approval store + resume path; `agentsui` renders the approval. This is prompt-only
   today — Phase 5 makes it real (agent-started campaigns are the key case).

## Locked cross-cutting decisions
- **Naming (total convention):** new services `jobs`, `agents` (pydantic-ai backend),
  `agentsui` (its frontend). dir = compose service = container/DNS = GHCR image =
  `/api/{name}` all match. Ports: next free after mail:8004 → `jobs:8005`,
  `agents:8006` (confirm at wiring time).
- **Agent identity (Keycloak):** the `agents` service is a confidential client.
  A runtime AI agent acts via **RFC 8693 token exchange that keeps the human as the
  subject** (`preferred_username` unchanged = record owner). A propagated
  **`fm_origin` claim** distinguishes agent-initiated calls (`fm_origin=agent`, default
  `user`); the acting client rides in `azp`. Persisted records store
  `owner=preferred_username`, `origin=fm_origin`, `actor=azp` → UIs render "alice (via
  agent)". No synthetic per-user users.
- **Job control:** progress is **read** from a per-app job-event stream the `jobs`
  service subscribes to. Control (pause/resume/cancel) is a **write** to a per-app
  internal API `POST /internal/jobs/v1/{id}/{pause|resume|cancel}` (audience-scoped) that
  the `jobs` service calls. Streams stay read-only.

## Contracts every app implements (define in Phase 0)
In v1 the **only job producers are `search` and `agents`** — `leads` is the internal
engine behind search's jobs, and mail campaigns are tracked inside `mail` (see those
sections). The producer list is **config-driven**, so mail/others can join later without
code changes. The point of `jobs` is to be the one place that knows every running job so
they can be managed — and so the `agents` service can query it to act with awareness.

**Versioning (stability contract).** MCP-facing and `/internal/*` routes are
**versioned** — `/api/{service}/mcp/v1/*`, `/internal/{domain}/v1/*`. Within a version,
changes are **additive only**; a breaking change ships as a new version (e.g. a `v2`
route/tool) alongside the old. MCP **tool names + schemas are a stable contract** to
agents — evolve additively, never silently repurpose a tool.

**Job-event stream** — each app that has jobs exposes an internal NDJSON stream
(`GET /internal/jobs/v1/stream`, audience `= app`, `jobs`-role grant) emitting lifecycle
events: `{job_id, type, user, origin, actor, status, progress, ts, exit_status?, meta}`.
`jobs` subscribes (exchanging for the app's audience) and persists.

**Job-control API** — each app exposes `POST /internal/jobs/v1/{id}/{pause|resume|cancel}`;
only the `jobs` service audience+role may call it; the app maps it onto its own
job manager. Idempotent; returns the new status.

**MCP-facing endpoints** — `/api/{service}/mcp/v1/*`, distinct handlers from UI routes,
audience `= service`, callable by the `mcp` service (svc scope `mcp→{service}`).

## Component work

### search backend — `search-agent`
- **History → cross-user (principle 1).** Keep writing `username` owner; **remove
  owner-only filtering on read** endpoints. Add columns `origin` (`user|agent`) and
  `actor` (azp). New read API to browse history by any user.
- **New MCP-facing endpoints** (`/api/search/mcp/v1/*`, separate from UI routes) — the
  comprehensive set backing the search MCP tools: `POST .../searches/apollo`,
  `POST .../searches/semantic`, `POST .../leads/enrich`, `GET .../searches` (list),
  `GET .../searches/{id}` (get), `GET .../searches/{id}/results` (list results),
  `POST .../searches/{id}/export` (result list honoring `exclude_contacted`). These
  **still funnel Apollo through leads** — the "only leads holds APOLLO_API_KEY / talks to
  Apollo" invariant is preserved.
- **Jobs integration:** publish search ingest/embedding jobs on `/internal/jobs/v1/stream`;
  implement `/internal/jobs/v1/*` control mapping onto the existing stream handling.
- **Exclude already-contacted (opt-in):** search results (and result-list export)
  support an `exclude_contacted` filter that drops leads already messaged by any
  campaign. The contacted set is owned by `mail`; search reads it via a new documented
  hop **search→mail** (`GET /api/mail/contacts/contacted`, returns emails/person ids).
  This is a convenience filter — the *authoritative* dedupe still happens at send time in
  `mail` (below). Optionally scope the exclusion to a specific campaign or all campaigns.
- Preserve the **streaming never-raise** invariant on all new streaming routes.

### leads backend — `leads-agent`
- **Embedding backfill endpoint:** `POST /api/leads/embeddings/backfill` — embed docs
  with `embedding:false` (or `?force=true` to re-embed), OpenAI → Milvus, flip flag
  only after indexing succeeds (existing invariant). The *job identity* is owned by
  **search** (the single front door for search + embedding jobs); leads just runs it.
- **Leads is the engine, not a `jobs` producer (v1).** Its `StreamJobManager` runs the
  actual ingest/embedding streams. Expose an **internal cancel/pause hook** that
  *search* calls to stop those streams; leads does **not** publish to `jobs` directly.

### jobs service — `jobs-agent` (NEW)
- FastAPI + Postgres (own db `funnelmanager_jobs`). `Job` row: `id, app,
  external_job_id, type, user (human owner), origin, actor, status
  (queued|running|paused|completed|failed|canceled), started_at, ended_at,
  exit_status, progress, last_event_at, meta jsonb`.
- **Stream subscriber framework:** background tasks subscribe to each registered app's
  `/internal/jobs/v1/stream` (exchange → app audience) and upsert job state. v1 producers
  are **search** and **agents** only; keep the producer list **config-driven** so mail
  and others join later without code changes.
- **MCP-facing API** (`/api/jobs/mcp/v1/*`): list jobs (filter by user/app/status), get
  job + progress, and control `pause|resume|cancel` (proxied to the owning app's
  `/internal/jobs/v1/*`). Cross-user visible (principle 1). These tools are what let the
  `agents` service see what is running and act with awareness.
- Auth: audience `jobs`; svc scopes `jobs→search`, `jobs→agents` (add others when they
  become producers); `mcp→jobs`. Prod loopback-bind like mcp.

### mcp server — `mcp-agent` (rule change + refactor — see updated agent file)
- **Refactor to a modular, multi-audience architecture first** (this is the MCP tool
  surface for every runtime agent, so it must scale):
  - `app/tokens.py`: `resolve(audience, subject)` — audience-parameterized exchange
    (replaces the leads-hardcoded `TokenResolver`).
  - `app/clients.py`: a generic `BackendClient(base_url, audience)` (replaces the
    leads-specific client); one instance per upstream (leads/search/jobs/mail).
  - `app/tools/{leads,search,jobs,mail}.py`: each exposes `register(mcp, deps)`;
    `app/tools/__init__.py` has `register_all`; `main.py` wires deps + calls it. Adding a
    service's tools becomes a drop-in module + a client + a scope.
- **Comprehensive tool set** (agents act **exclusively** through MCP → this is their whole
  capability + situational-awareness surface):
  - leads (existing, read-only): `leads_stats`, `recent_leads`, `get_leads`, `similarity_search`.
  - search: `start_apollo_search`, `start_semantic_search`, `enrich_leads` (write),
    `list_searches`, `get_search`, `list_results`, `export_results` (honors `exclude_contacted`).
  - jobs: `list_jobs`, `get_job`, `pause_job`, `resume_job`, `cancel_job`.
  - mail (`mcp→mail`, `/api/mail/mcp/v1/*`): `list_campaigns`, `get_campaign`,
    `start_campaign`, `continue_campaign`, `contacted_contacts`, `list_messages`,
    `get_thread`, `send_message` (write).
- New clients + svc scopes `mcp→search`, `mcp→jobs`, `mcp→mail`. Annotate read tools
  `readOnlyHint`; write tools carry accurate destructive/idempotent hints.
- **Versioning:** tool names/schemas are a stable contract — additive within v1; a breaking
  change ships as a new tool, never a silent repurpose. Endpoints are `…/mcp/v1/*`.
- Invariant still holds: **MCP never calls Apollo directly** — Apollo goes through
  search → leads.

### agents service — `agents-agent` (NEW, pydantic-ai)
- FastAPI backend that runs **runtime AI agents** (pydantic-ai) to complete tasks
  from `POST /api/agents/tasks` (a goal + params). The runtime agent is an **MCP
  client** — it calls MCP tools (search/leads/mail/jobs) under the human's identity
  via token exchange with `fm_origin=agent`, so anything it persists shows as
  "alice (via agent)".
- It **acts exclusively through MCP tools** (one audited capability surface) — no direct
  backend calls. It reads user activity (prior searches, running campaigns, jobs) through
  MCP read tools to plan its actions.
- Each run is a **job**: expose `/internal/jobs/v1/stream` + `/internal/jobs/v1/*` so runs
  surface in `jobs` and are pausable/cancelable.
- Keycloak client `agents`; svc scope `agents→mcp`; `fm_origin=agent` mapper.
- Model note: the *runtime* agents' LLM is app config (default to a Claude model);
  independent of the Claude agent that builds this service.

### agents frontend — `agentsui-agent` (NEW)
- Standalone React/MUI app (mirror `mailui` structure), Vite `base:'/agents/'`, own
  container behind nginx `/agents/`, shares the hub Keycloak session (localStorage
  `fm_oidc_*`). Start tasks, watch progress (from `jobs`), view results/history.
- Appears on the hub as a `WEB_APPS` tile (principle 1: same tile for everyone).

### mail backend — `mail-agent` (campaigns)
- **Mailbox client (confirmed scope).** `mail` is a **full multi-domain mailbox**, not
  just archive + campaigns. Beyond the archive, expose the normal operations across all of
  a user's connected domains: **compose & send**, **view inbox/sent/threads**, **search**,
  read bodies/attachments. Reads serve from the Postgres archive (fast, offline) reconciled
  with Gmail; sends use `gmail.send`. Current scopes (`gmail.readonly` + `gmail.send`) cover
  read + send; **mutating Gmail state** (delete/label/mark-read) needs `gmail.modify` —
  a guarded scope-broadening decision (Principle 4 + security review), deferred unless approved.
- **All inboxes to all users** (principle 1): drop per-user mailbox read scoping.
- **Full backup on init (Principle 4 gate).** On mailbox connect, back up **all** messages
  from the Google server into the never-delete archive. **Before** starting, estimate size;
  if it exceeds the configurable threshold (**default 2 GB**) return `confirmation_required`
  with the estimate and wait for explicit user confirmation. Estimate via `messagesTotal` ×
  sampled avg message size (or account storage signals).

**Campaigns** (separate feature/page — see UI):
- **Campaign model:** `id, owner (initiating user), origin (user|agent), actor, name,
  status, send_strategy (balanced|sequential), throttle jsonb (per_domain_daily default
  20), created_at`. Records the initiating user/agent → reads "alice" or "alice (via agent)".
- **Source searches (many, appendable):** a campaign draws recipients from **one or more**
  search result lists via a `campaign_sources` join (not a single `source_search_id`). A
  campaign can be **continued** — add another search's results later; new recipients are
  merged with dedupe/suppression re-applied so no one already messaged is re-added.
- **Sent-message store:** `campaign_messages` row per send — `campaign_id, person
  (email/apollo_id), mailbox/domain used, gmail_message_id, sent_at, status`. The campaign's
  record of everything it sent, and the basis for dedupe/suppression + the contacted set.
- **Sender = the initiating user's own connected mailboxes**, possibly **multiple domains**.
  `send_strategy`: `balanced` (spread across the user's domains in parallel) or `sequential`
  (fill one domain to its daily cap, then the next). Per-domain daily cap enforced either
  way (default 20/domain/day, configurable).
- **Suppression / throttle (authoritative, server-side at send time):** (1) within-campaign
  dedupe, (2) cross-campaign per-person suppression via a global contacted/last-contacted
  table (built from `campaign_messages`), (3) per-domain daily cap. Expose the contacted set
  at `GET /api/mail/contacts/contacted` (optionally per campaign) for search's
  `exclude_contacted` filter.
- **Cross-user visibility (principle 1):** campaigns are per-user-owned but everyone can
  **view campaigns by user, or all together**.
- **MCP-facing surface** (`/api/mail/mcp/v1/*`, `mcp→mail`, distinct from UI routes):
  campaign list/get/start/continue, contacted-set read, and inbox list_messages/get_thread/
  send_message — so runtime agents can run the full search→campaign flow and operate the
  mailbox. Built in P5; the `mcp` mail tool module lands with it.
- **Lifecycle in `mail`** — own status/progress + pause/resume/cancel, paced by throttles.
  Not a `jobs` producer in v1; an agent-launched campaign is visible via that agent's job.
  A very large campaign is itself an expensive action → Principle 4 confirmation over threshold.

### mail UI — `mailui-agent`
- **Inbox pages (primary):** use it like Gmail across all domains — message list
  (inbox/sent/threads), read view, **compose & send**, search. This is the default
  experience; keep it clean and separate from campaigns.
- **Campaigns page (separate):** create a campaign from one or more saved search lists,
  choose **send strategy** (`balanced`/`sequential`) across the user's mailboxes/domains and
  the per-domain cap, launch, watch progress; **continue** a campaign by adding another
  search's results; **browse campaigns by user or all together**.
- **Confirmation dialogs (Principle 4):** render the >2 GB backup and large-campaign
  confirmations when the backend returns `confirmation_required`.

### platform + runtime — `platform-agent`, `runtime-agent`
- **Keycloak:** clients `jobs`, `agents`; audiences; svc scopes `mcp→search`, `mcp→jobs`,
  **`mcp→mail`**, `agents→mcp`, `jobs→search`, `jobs→agents`, **`search→mail`** (for the
  `exclude_contacted` read); `fm_origin` claim mapper + propagation across exchanges;
  roles/grants for the new services in `deploy/policy/data.json` **and**
  `fm_runtime/grants.py` (keep in sync, re-run `python -m fm_runtime.export`).
- **Shared confirmation-gate helper (Principle 4):** add an `fm_runtime` utility for the
  estimate→`409 confirmation_required`→`confirm=true` pattern so every service guards
  expensive actions identically. Its default thresholds are configurable.
- **Manifests/compose/nginx:** add `jobs`, `agents`, `agentsui` (+ their DBs); nginx
  `/agents/` and `/api/agents/*`, `/api/mail/*` already covers campaigns; `jobs` and
  `mcp` stay internal/loopback.
- **Docs:** update `CLAUDE.md` + `docs/architecture.md` + `docs/authentication.md` to
  describe the new services, the `fm_origin` model, principle-1 visibility, the
  MCP-can-start-searches path, and **Principle 4 (guard expensive actions)**. **Do this in
  Phase 0** so later agents don't fight stale rules.

## Agent-team topology & how a workstream runs
Every workstream is owned by exactly one Claude agent and gated by adversarial
review. The orchestration workflow `.claude/workflows/service-workstream.js` runs, per
workstream: **implement (owning agent, Opus / high effort) → parallel
bug-hunter + security-reviewer + quality-reviewer on the diff (also Opus / high) →
triage → owning agent fixes → re-review the fix diff.** Opus/high is enforced by the
workflow (`model:'opus', effort:'high'` on every `agent()` call), so it applies even
though the agent files don't pin a model. The capture/self-improvement hooks fire
automatically for both the domain agent and the reviewers.

## Phased sequencing (dependencies)
- **Phase 0 — Foundations** (`platform-agent`, `runtime-agent`): Keycloak
  clients/scopes/`fm_origin` (incl. `search→mail`); grants in data.json + grants.py; the
  job-stream + `/internal/jobs` **contract**; the **`fm_runtime` confirmation-gate helper**
  (Principle 4); update `CLAUDE.md`/docs. *Blocks new-hop auth.*
- **Phase 1 — jobs service** (`jobs-agent`): backend, store, subscriber framework,
  MCP-facing API, control proxy. *Depends on P0 contract.*
- **Phase 2 — search + leads** (`search-agent`, `leads-agent`, parallel): search
  history cross-user + MCP endpoints; **search becomes the job producer** (publishes its
  searches + embedding progress to `jobs`, controls the underlying leads streams). leads:
  embedding-backfill endpoint + an internal cancel/pause hook for search (leads is the
  engine, not a direct `jobs` producer). *Depends on P0.*
- **Phase 3 — MCP refactor + tools** (`mcp-agent`): **first** refactor to the modular
  multi-audience architecture (`tokens.resolve(audience,…)`, generic `BackendClient`,
  `tools/` modules), **then** add the comprehensive tool set for leads/search/jobs. The
  **mail tool module depends on P5** — land it with/after mail's MCP endpoints (or stub).
  *Depends on P1 + P2.*
- **Phase 4 — agents service** (`agents-agent`, then `agentsui-agent`): pydantic-ai
  MCP-client backend (origin=agent), task API, job registration; then UI + hub tile.
  *Depends on P3 + P1.*
- **Phase 5 — mail: full client + campaigns** (`mail-agent`, `mailui-agent`): the
  **full multi-domain mailbox client** (compose/send, inbox/sent/threads, search) +
  **full-backup-on-init with the >2 GB confirmation gate**; then campaigns — model +
  `campaign_messages` sent-message store + multi-search `campaign_sources` (continuable) +
  **multi-domain send (balanced|sequential)** + suppression + per-domain throttle +
  contacted-set endpoint; all-inbox + cross-user campaign visibility; **separate inbox vs
  campaigns pages**. *Depends on search lists (P2); the `search` `exclude_contacted` filter
  depends on this phase's contacted endpoint (wire that side of P2 after P5, or stub it).*
- **Phase 6 — frontend/hub** (`frontend-agent`): search-history browser UI; hub tiles;
  ensure principle-1 visibility everywhere.
- **Phase 7 — integration & E2E adversarial pass:** whole-diff `/adversarial-review`
  + the E2E scenario below.

## Definition of done (E2E scenario to verify)
A runtime AI agent, started by **alice** from `agentsui`, runs an Apollo search via
MCP → the search appears in search history as **"alice (via agent)"** and the agent run
shows in `jobs` (pausable/cancelable) → alice builds a search list with
**`exclude_contacted`** so already-messaged people are dropped → she turns it into a
**campaign** sent from her own mailboxes across her domains (`balanced` or `sequential`),
respecting within-campaign dedupe, cross-campaign suppression, and the per-domain daily cap;
every send is recorded in `campaign_messages` → she later **continues** the campaign by
adding another search's results (already-messaged people are not re-added) → connecting a
new large mailbox triggers the **>2 GB backup confirmation** before it archives → alice
also uses the mail app as a **normal inbox** (compose/send/view sent across her domains) →
**bob** (a different user) can view alice's history, her job, and her campaigns (by user or
all), because Keycloak let him into each service (principle 1).

## Assumptions / open items (flag if wrong)
- Ports `jobs:8005`, `agents:8006` are provisional.
- **`jobs` producers in v1 = `search` + `agents` only** (leads is search's engine; mail
  campaigns are mail-tracked). The producer registry is config-driven, so mail can be
  promoted to a producer later.
- Campaign sender = the initiating user's **own** connected mailboxes, possibly across
  **multiple domains**; `send_strategy` = `balanced` | `sequential` (user-configurable);
  the per-domain daily cap is always enforced.
- Runtime-agent LLM: **OpenAI** (user decision, Phase 4) via pydantic-ai; model configurable
  via `AGENTS_LLM_MODEL`, `OPENAI_API_KEY` from the shared openai secret (as leads uses).
- `jobs` has no UI in v1 (surfaced inside `agentsui`/mail/search progress + MCP). Add a
  jobs dashboard later if wanted.
- `mail` is a **full mailbox client** (compose/send, inbox/sent/threads, search) across all
  domains, with campaigns on a separate page. Read + send use current scopes; **mutating**
  Gmail (delete/label/mark-read) via `gmail.modify` is deferred pending your approval.
- **Expensive-action gate (Principle 4)** applies everywhere; concrete default =
  **mailbox backup > 2 GB** needs confirmation. Threshold(s) configurable.
- `search`'s `exclude_contacted` filter needs `mail`'s contacted endpoint (P5), so it lands
  or is stubbed accordingly if search (P2) ships first.
- MCP is **modular + multi-audience**, tools are **comprehensive per service**, and agents
  act **only** through MCP (one audited surface). Contracts are **v1-versioned**, evolved
  additively — a breaking change is a new version/tool, never a silent repurpose.
- Mail is exposed via MCP (`/api/mail/mcp/v1/*`, `mcp→mail`): campaigns + contacted + inbox
  read/send; the `mcp` mail tool module lands with/after P5.
