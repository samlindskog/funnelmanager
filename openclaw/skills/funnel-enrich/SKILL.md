---
name: funnel-enrich
description: Enrich Apollo people or companies via Funnel Manager — full profiles, verified email, phone reveal. Use when asked to enrich a lead or get contact info. Spends Apollo credits.
user-invocable: true
---

# Funnel Manager — enrich

Use the `funnelmanager` MCP tools. Enrichment calls Apollo and **spends
credits per person/company** — get explicit confirmation before enriching
more than ~10 records in one go, and check `apollo_credits` first for bulk
work.

Auth: every tool needs a `session_token` (usually injected by the harness).
If a call fails with a missing/expired token, fetch one with
`funnelmanager_session_token` and pass it explicitly; if the chat is not
linked to a profile, an admin must approve the pending channel request in the
Funnel Manager hub.

All enrichment needs the **Apollo id** (`id` on search results / `apollo_id`
on lead summaries), not the `mongo_id`. Enrichment upserts the stored lead —
it never creates duplicates.

## Tools

- `enrich_person(apollo_id)` — Complete Person Info (profile, org, usually email)
- `enrich_organization(apollo_id)` — Complete Organization Info
- `match_person(apollo_id, run_waterfall_email, run_waterfall_phone, reveal_phone_number)`
  — contact reveal. Email usually lands immediately in the returned record.
  **Phone numbers arrive asynchronously** via webhook: if
  `phone_reveal_pending` is true, wait ~1 minute and re-fetch with
  `get_lead(mongo_id)`. Waterfall/phone reveal costs extra credits — confirm
  with the user before using those flags.

## Checking enrichment state

A lead's `apollo_enriched` flags (`linkedin`, `email`, `phone`) show which
enrichment already ran — don't re-enrich a lead whose flag is already set
unless the user explicitly wants a refresh. `apollo_endpoints` shows when
each Apollo payload was received.
