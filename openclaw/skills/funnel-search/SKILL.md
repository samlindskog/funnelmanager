---
name: funnel-search
description: Search Apollo for people or companies through Funnel Manager. Use when asked to find leads, people at a company, or companies by keyword/domain. Spends Apollo credits.
user-invocable: true
---

# Funnel Manager — search

Use the `funnelmanager` MCP tools. Searches call the Apollo API and **spend
credits** — before running a new search, call `search_history` to check whether
an equivalent search already exists; if it does, read it with `search_results`
instead of re-running it.

## Auth (required)

Every funnelmanager tool needs a `session_token` for the person this
conversation belongs to. The harness usually injects it automatically; if a
tool fails with a missing/expired token, call `funnelmanager_session_token`
and pass its `session_token` value explicitly on each call. Never invent a
token. If the tool reports this chat is not linked to a profile, tell the user
an admin must approve the pending channel request in the Funnel Manager hub.

## People search — `run_people_search`

- `query`: free-text keywords (title, name, skills…)
- Company filter (optional): `organization_name` **or** `organization_id`
  (never both), plus optional `organization_domain`
- Needs query text and/or a company filter

## Company search — `run_company_search`

- `keywords`: comma-separated keyword tags
- `company_name` and/or `company_domain` also work alone

## How results behave

Both tools persist a search-history entry, return page 1 as
`results_preview`, and **keep ingesting all matching pages server-side**
(up to 100k entries) after returning. To read more:

- `search_results(search_id, page)` — any stored page, 100 per page
- `search_history` — watch `total_results` grow while ingest runs

Each result carries `mongo_id` (for `get_lead` / `get_leads`) and `id`
(the Apollo id, needed for enrichment — see the funnel-enrich skill).

If the user asks about credit balance or you are about to run several
searches, check `apollo_credits` first.
