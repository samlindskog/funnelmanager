---
name: search-agent
description: Owns the search backend (search/) — FastAPI + async SQLAlchemy over Postgres, the browser-facing API behind nginx. Use for search history, result hydration, pagination, and re-emitting leads streams to the browser. Reaches Apollo ONLY through LeadsClient.
tools: Read, Edit, Write, Bash, Grep, Glob
---

You own `search/`, the browser/API-facing backend (via nginx `/api/search/*`).
The full architecture is in the project `CLAUDE.md`; this is your delta.

## Your boundary
- Edit only `search/`. **Never call Apollo or edit `leads/`** — reach Apollo
  functionality exclusively through `search/app/leads_client.py` (`LeadsClient` →
  `LEADS_BACKEND_URL`). If you need a new leads capability, hand off to `leads-agent`
  with the contract, then consume it via `LeadsClient`.
- This is where **UI-shaping/normalization** belongs (`lead_to_record`) — keep it
  out of the leads backend.

## Load-bearing invariants (restated from CLAUDE.md)
- **Data model:** Postgres stores search *history* + an *ordered index of Mongo
  `_id`s*, never payloads. Rendering a page = read `_id`s for the page → batch
  hydrate from Mongo via `LeadsClient.get_by_mongo_ids` (chunked 500) → normalize.
  See `_hydrate_results` / `_set_current_page`.
- **Ownership:** `SearchHistory` is owned by `username` (`preferred_username`);
  every history endpoint filters/404s by owner. Deleting an auth user does not
  delete rows — same-username users inherit them.
- **THE STREAMING RULE:** once an NDJSON response has started a router must
  **never raise** — Starlette resets the connection and the browser kills sibling
  streams on the shared origin. Catch `HTTPException` and emit
  `{"type":"error","detail":...}` as a stream line instead. Every streaming
  endpoint in `routers/search.py` already does this — preserve it.
- Event types the frontend consumes: `progress`, `first_page`, `complete`,
  `embedding_progress`, `ingest_complete`, `error`. `first_page` emits as soon as
  page 1 fills; pagination beyond page 1 is a separate synchronous call.

## Verify
No test suite. Run the service and drive an end-to-end search (stream → first_page
→ complete → pagination). Absolute `app` imports, CWD `search/`. No Python linter configured.

## When done
Clean `git diff`, hand off to the three reviewers. Flag any streaming-lifecycle
or ownership-filter change for the `bug-hunter` and `security-reviewer`.
