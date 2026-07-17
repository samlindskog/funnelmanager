---
name: funnel-activity
description: View Funnel Manager activity — search history, stored results, recent ingest/enrichment, lead stats, Apollo credit balance. Read-only and free; use for any "what happened / what do we have" question.
user-invocable: true
---

# Funnel Manager — activity & inspection

Use the `funnelmanager` MCP tools. Everything here is **read-only and
free** (no Apollo calls) — prefer these tools over re-searching or
re-enriching.

## User activity (search backend)

- `search_history` — recent searches: label, entity type, result counts, timestamps
- `search_results(search_id, page)` — stored results for any search, hydrated
  like the UI (pass `include_raw=true` only when full Apollo payloads are needed)

## Lead & enrichment activity (leads backend)

- `leads_stats` — totals: people vs companies, embedded vs pending, enrichment counts
- `recent_leads(entity_type?, enriched?, embedded?, limit, skip)` — most recently
  updated leads with per-lead Apollo endpoint timelines; `enriched=true` is the
  enrichment activity feed
- `get_lead(mongo_id)` / `get_leads(mongo_ids)` — inspect specific leads
- `similarity_search(query)` — semantic search over already-stored leads (free,
  never calls Apollo) — good for "do we already have people like X?"

## Credits

- `apollo_credits` — current Apollo balance; report this when asked about usage
  or before recommending credit-spending work.
