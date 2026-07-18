---
name: funnel-activity
description: View Funnel Manager lead activity — recent ingest/enrichment, lead stats, semantic search over stored leads. Read-only and free; use for any "what happened / what do we have" question.
user-invocable: true
---

# Funnel Manager — activity & inspection

Use the `funnelmanager` MCP tools. Everything here is **read-only and
free** (no Apollo calls) — these tools inspect leads already stored by the
platform. New Apollo searches and enrichment are run by humans in the
Funnel Manager search app, not by the agent; if the user asks to run a new
search or enrich a lead, point them at the search app in the hub.

Auth: every tool needs a `session_token` (usually injected by the harness).
If a call fails with a missing/expired token, fetch one with
`funnelmanager_session_token` and pass it explicitly; if the chat is not
linked to a profile, an admin must approve the pending channel request in the
Funnel Manager hub.

## Lead & enrichment activity (leads backend)

- `leads_stats` — totals: people vs companies, embedded vs pending, enrichment counts
- `recent_leads(entity_type?, enriched?, embedded?, limit, skip)` — most recently
  updated leads with per-lead Apollo endpoint timelines; `enriched=true` is the
  enrichment activity feed
- `get_leads(mongo_ids)` — inspect specific leads by Mongo `_id` (batch, up to
  500; pass `include_raw=true` only when full Apollo payloads are needed)
- `similarity_search(query)` — semantic search over already-stored leads (free,
  never calls Apollo) — good for "do we already have people like X?"
