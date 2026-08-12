# Bulk Prospector workflow — plan

Status: PLANNED. Goal: paste N company domains → automated people ingest over all of
them → one semantic search filtered to those companies. Zero repeated manual entry.
UI-only orchestration (no new backend surface); every stage reuses an existing
endpoint and existing searchui components.

## Why UI-only works

The three stages are exactly the calls the UI already makes one-at-a-time:
1. domain → company: `runSearch({entity_type:"companies", company_domain})` (1 hit, upserts org doc, yields org id + name)
2. org → people: `runSearch({entity_type:"people", organization_id, organization_display_name, organization_domain})` — NDJSON stream, context-fallback stamps `company_id` on every ingested person (v1.15.2+)
3. companies → people: `runSimilaritySearch({query?, embeds, companyIds:[...] , ...})` (v1.15.7 multi-company OR)

The only missing piece is an orchestrator that sequences them with progress — a
client-side state machine. Server-side orchestration (jobs/agents) is deliberately
NOT needed for v1; noted as the upgrade path.

## The page: "Prospect" view in searchui

searchui is routerless (view-state based). Add a third top-level view alongside
search/results — `mainView: 'prospect'` — reachable from a nav button. Layout is a
3-stage vertical stepper, each stage reusing existing pieces:

### Stage 1 — Domains in
- Multiline paste box (one domain per line, commas tolerated) + optional CSV file
  (reuse `parseCsv`, accept a `domain` column). Dedupe + normalize (strip scheme/www).
- Shows a count chip. "Resolve companies" button starts stage 1.

### Stage 2 — Resolve + ingest (the automation core)
A per-company status table (domain | company | people | status) driven by a small
orchestrator hook (`useProspectRun`):
- **Resolve**: company search per domain (cheap, idempotent). Failures marked
  `not found` and skipped, never abort the run.
- **Skip-if-ingested probe** (cost saver, ON by default): before people-ingest,
  `runSimilaritySearch({embeds:[], companyIds:[orgRecordId], limit:1})` — a
  Mongo-only probe; >0 results ⇒ people already linked ⇒ status `already ingested`,
  skip the Apollo spend. Toggle "re-ingest anyway" for refresh runs.
- **People ingest**: existing streaming `runSearch` per org, progress per row from
  the same NDJSON handling `useProgress` already implements (ring + counts).
- **Confirmation gate before ingest** (P4 discipline, client-side v1): show
  "N companies to ingest, ~M people estimated (Σ employee_count from stage 1,
  capped per-company at the 100k walk limit)" and require an explicit Start click.
- Stage-2 results feed a summary line: "X companies resolved, Y ingested, Z skipped,
  ~P people linked".

### Stage 3 — Semantic search over the set
- Render the EXISTING similarity form block (embeds toggles, passage, tri-state
  contact filters, limit) — extracted into a shared component
  `SimilarityForm` used by both SearchPage and the Prospect view (move, don't fork).
- Company chips pre-filled from stage 2 (named chips — machinery exists), editable.
- Submit → `runSimilaritySearch` → the normal `SearchResultsView` (history row,
  export CSV, import, enrich — the whole downstream toolchain applies unchanged).

## Performance mechanisms (lessons already paid for)

1. **Bounded concurrency, default 2** (hard cap 3) for stage-2 ingests. The
   2026-08-11 incident proved ~30 concurrent streams starve the single search
   replica even at 1.5 cores; sequential-ish is barely slower end-to-end (Apollo
   page-walking dominates) and never trips probes. Resolve step may run at 3
   (tiny requests).
2. **Skip-if-ingested probe** (above) — the biggest real-world saver: repeat runs
   cost ~1 Mongo query per already-done company instead of a full re-ingest.
3. **localStorage checkpoint** of the run state (domains, per-domain status, org
   ids) — refresh/crash resumes where it left off instead of re-entering
   everything (the exact pain this feature kills). Server-side ingest of the
   in-flight company continues on disconnect (existing behavior); the queue
   resumes from the checkpoint on return. Clear on completion.
4. **Stagger starts** ~2s apart so first_page bursts don't align; embedding backlog
   is already fair-scheduled server-side (MilvusGate nice tiers).
5. Corpus growth from bulk ingest is absorbed by Milvus mmap (v1.15.4) — no
   memory-ceiling chase; minio/CPU headroom already raised.

## Deliberate non-goals (v1)

- **No new backend surface.** If runs outgrow the browser (close-the-laptop
  durability, scheduled refreshes), the upgrade path is the `agents`/`jobs`
  services driving the SAME endpoints via MCP — the P2/P11 architecture — not a
  bespoke bulk endpoint.
- **No server-side P4 estimate gate on search-start yet** (drift item #1 stands);
  the stage-2 confirmation is the interim client-side gate, consistent with how
  the rest of the UI handles it today.
- Domain→org disambiguation beyond Apollo's first hit (rare; the row shows the
  resolved name so a wrong match is visible and deletable before ingest).

## Build checklist

1. Extract `SimilarityForm` from SearchPage (shared, props-driven). ~1 move.
2. `useProspectRun` hook: state machine (idle→resolving→confirm→ingesting→done),
   bounded-concurrency queue, localStorage checkpoint, per-row status.
3. `ProspectPage` view: paste box + CSV input, status table, confirm panel,
   `SimilarityForm` + chips, wired to `showResults`/history refresh.
4. Nav affordance in the app bar (Search | Prospect) + testids for drive-canary.
5. Verify (dev): 5-domain run incl. one bogus domain, one already-ingested
   company (skip path), tab-refresh mid-run (resume), then stage-3 search +
   enrich round-trip. `npm run build`/`lint`.
