# Semantic Search v2 — top-level lead fields, multi-embed similarity, filterable search

Status: PLANNED (not started). Owner: leads (primary), search, mcp, searchui.
Intended consumer beyond the UI: mail campaigns (audience building — later program).

## Goal

1. Promote six derived fields to the **top level of Mongo lead documents**, populated on every
   Apollo search/enrichment write: `name` (people only), `title` (job title, people only),
   `company_id` (people only — the **Apollo** organization id of their company), `email`,
   `phone`, `linkedin`.
2. Embed `name` and `title` (people) into Milvus **alongside** the existing Apollo-passage
   embedding.
3. Upgrade `POST /api/leads/similarity-search` to rank by the **average similarity** between the
   query passage and a **caller-selected subset** of the embeds (`name`, `title`, `apollo`) —
   including the empty subset (= pure filter search) — with optional filters: **company**
   (by the Mongo `_id` of a stored organization doc), **email exists**, **phone exists**,
   **linkedin exists** (all three contact fields get identical exists-filter treatment).
4. Thread the new parameters through search backend → MCP surfaces → search UI's
   "Saved leads (similarity)" mode.
5. In the results UI, show **linkedin / mail / phone icons** next to a record when it has an
   existing top-level `linkedin` / `email` / `phone` value.

Everything stays inside the P5 boundary (all Apollo/Mongo/Milvus work in `leads/`), and every
API change is **additive** on the existing v1 surfaces (P2).

---

## 1. Mongo: derived top-level fields

### 1.1 Semantics

`apollo_responses` remains the **source of truth** (P9). The new top-level fields are a
**derived index** — recomputed from the merged `apollo_responses` on every write, raw values
verbatim from the Apollo payload (no formatting/UI shaping; leads stays unopinionated —
normalization for display remains in `search/`).

| Field | Entity | Source (person payload) | Notes |
|---|---|---|---|
| `name` | person only | `name` else `first_name + last_name` | |
| `title` | person only | `title` | |
| `company_id` | person only | resolved from `company_apollo_id` | **The organization DOCUMENT's Mongo `_id`** (corrected 2026-08-11 — this is what the spec meant; the raw Apollo org id is kept as `company_apollo_id`, the resolution key, so pending links resolve once the org doc exists) |
| `email` | person (orgs have none) | `email` | **Placeholder-aware**: Apollo's locked `email_not_unlocked@…` must be treated as absent (mirror the semantics of `contact_signals_from_person_search` in `search/app/leads_client.py`; re-implement minimally in leads — no cross-service import) |
| `phone` | person + org | person: first of `phone_numbers[].sanitized_number` (match/webhook payloads); org: `phone`/`sanitized_phone` | |
| `linkedin` | person + org | `linkedin_url` | |

Missing values are simply **absent/null** — never empty-string sentinels.

### 1.2 Extraction helper

New module `leads/app/derived.py` (or a section in `app/apollo_endpoints.py`):

```python
def derive_top_fields(entity_type: str, responses: dict) -> dict:
    """Best-payload extraction of the six top-level fields.

    Uses the SAME payload precedence as embeddings (person: MATCH > BY_ID > SEARCH,
    org: BY_ID > ENRICH > SEARCH via person_payload_from_doc / organization_payload_from_doc
    in app/embeddings.py) so a match/enrich payload always wins over a search hit.
    Returns only non-null keys.
    """
```

Fallback rule: for a field the best payload lacks (e.g. phone only exists in the MATCH webhook
payload while name is best from BY_ID), fall through the precedence chain **per field**, not per
payload — take the first non-empty value walking payloads in precedence order.

### 1.3 Write-path wiring (all in `leads/app/routers/leads.py`)

Apply `derive_top_fields` in all three upsert paths, on **both** update and insert branches:

- `_upsert_search_records` (~line 302) — search ingest (PERSON_SEARCH / ORG_SEARCH).
- `_upsert_enriched_record` (~line 363) — people/{id}, organizations/{id}, people/match.
- `apollo_people_match_webhook` (~line 1790) — async phone-reveal / waterfall.

Update branch: merge the recomputed fields into the existing `$set` (only non-null keys — never
`$unset`/overwrite an existing value with null; since extraction recomputes from the *merged*
`apollo_responses` with per-field precedence, a lower-precedence write cannot regress a field).
Insert branch: include them in the initial doc.

**Upsert guarantee (explicit requirement):** the contact fields (`email`, `phone`, `linkedin`)
must be **upserted onto already-stored docs** whenever a later write reveals them — a doc first
seen via search ingest (locked email, no phone) that is subsequently enriched (`people/{id}`,
`people/match`) or hit by the async phone-reveal webhook gets its top-level `email`/`phone`/
`linkedin` populated on that existing doc, not only on fresh inserts. This falls out of applying
`derive_top_fields` on the *update* branch of all three write paths, and is a named verification
case in §8 (it is the property mail campaigns depend on).

### 1.4 Indexes (`leads/app/database.py` `init_db`)

Additive, following the existing pattern:

```python
await db.leads.create_index("company_id")
await db.leads.create_index("email")     # supports {"email": {"$exists": true}} / {"$ne": None}
await db.leads.create_index("phone")
await db.leads.create_index("linkedin")  # linkedin gets the same exists filtering as email/phone
```

### 1.5 Backfill for existing docs

Extend `leads/scripts/reembed.py` (which already walks every embeddable doc and must run anyway
for the Milvus migration, §3) to also write the derived top-level fields on each doc it visits,
plus a pass over non-embedded docs so *every* doc gets fields. Alternatively a standalone
`scripts/backfill_top_fields.py` — recommendation: **one combined migration script**, since the
operator runs exactly one thing after deploy (§7).

---

## 2. Milvus: multi-embed schema (v2 collection)

### 2.1 Design decision — one vector field + `embed_kind` rows (chosen)

Two options considered on Milvus v2.5.4:

- **A. Multiple vector fields per row** (`apollo_vec`, `name_vec`, `title_vec`) + native hybrid
  search (`AnnSearchRequest` + `WeightedRanker`). Rejected: vector fields cannot be null, so docs
  missing a title would need zero-vector placeholders (undefined cosine); and `WeightedRanker`
  averages *normalized* scores with 0-contribution for entities absent from a field's top-k —
  not the exact "average similarity" semantics requested, and harder to reason about.
- **B. One vector field, one row per (doc, kind)** — **chosen**. Exact control of the averaging
  semantics, no placeholder vectors, no schema tricks; costs 3 rows per person and up to 3 ANN
  searches per query (bounded, and the `MilvusGate` already serializes SDK calls).

### 2.2 Schema (`leads/app/milvus_client.py` `ensure_collection`)

New collection (the old one cannot be migrated in place — `ensure_collection` returns an
existing collection untouched):

```python
FieldSchema("pk",          VARCHAR, is_primary=True, max_length=96)   # f"{mongo_id}:{kind}"
FieldSchema("mongo_id",    VARCHAR, max_length=64)
FieldSchema("apollo_id",   VARCHAR, max_length=128)
FieldSchema("entity_type", VARCHAR, max_length=16)
FieldSchema("embed_kind",  VARCHAR, max_length=16)                    # "apollo" | "name" | "title"
FieldSchema("company_id",  VARCHAR, max_length=128)                   # "" when absent
FieldSchema("has_email",   BOOL)
FieldSchema("has_phone",   BOOL)
FieldSchema("has_linkedin", BOOL)
FieldSchema("embedding",   FLOAT_VECTOR, dim=settings.openai_embedding_dimensions)
```

Same IVF_FLAT/COSINE index (`nlist: 128`, `nprobe: 16`). Default collection name bumps to
**`leads_embeds_v2`** in `app/config.py` — and `MILVUS_COLLECTION` is explicitly pinned in
`docker-compose.{dev,prod}.yml`, `.env{,.prod}.example`, `leads/.env.example`, and
`deploy/apps/base/{leads,leads-canary}/deployment.yaml`, so **all seven pins must be updated
together** (platform-agent touchpoint).

### 2.3 Indexing pipeline (`index_lead_docs`, `app/embeddings.py`)

- New `lead_embedding_texts(doc) -> dict[kind, text]`: `apollo` = existing
  `lead_embedding_text` passage (unchanged builder); for people additionally `name` = the raw
  name string and `title` = the raw title string (skip kind when the field is absent). Orgs get
  `apollo` only.
- `index_lead_docs` embeds all texts for the batch in one `embed_texts` call (name/title strings
  are tiny — negligible OpenAI cost) and upserts one row per (doc, kind) with the scalar fields
  from the doc's derived top-level fields (`entity_type`, `company_id`, `has_email` =
  email-present, `has_phone`, `has_linkedin`).
- **Precedence/flag semantics unchanged**: `embedding_source` never-downgrade skip, and the
  Mongo `embedding: true` flip, key off the doc as today (the three kinds ride along in the same
  upsert). **Scalar-drift re-index** (review-driven): for docs the precedence guard would skip
  (e.g. MATCH-embedded, later BY_ID-enriched), `index_lead_docs` batch-queries the existing
  apollo-kind rows' scalars and re-indexes any doc whose derived `has_*`/`company_id` drifted —
  otherwise a stale `has_email=false` in Milvus would silently exclude true matches from
  `email_exists:true` recall (the Mongo re-check in §4.4 can only drop false positives, never
  recover false negatives). The drift query failing falls back to the plain skip (never breaks
  ingest).
- Stale-kind rows (a doc that once had a title and later doesn't) are left in place — the
  Mongo re-check also covers this edge; note it in the module docstring rather than adding a
  delete-by-expr round-trip.

---

## 3. Reembed / migration script

Extend `leads/scripts/reembed.py` into the single post-deploy migration:

1. Walk **all** lead docs (not just `embedding: true`): backfill derived top-level fields (§1.5).
2. Drop/recreate the **new** collection name; re-embed every previously-embedded doc into all
   applicable kinds (existing chunked flow; `SOURCE_PRECEDENCE` behavior unchanged).

Cost note: a full apollo-passage re-embed at `text-embedding-3-small` prices is negligible
(~$0.02/1M tokens); copying existing apollo vectors out of the old collection was considered and
rejected as complexity that saves cents. The script stays operator-run (P4: the interactive
similarity endpoint itself needs no confirm gate — one query-embed call per request; the
expensive path remains the already-gated backfill/reembed).

---

## 4. Leads: `POST /api/leads/similarity-search` v2 (additive)

### 4.1 Request (`leads/app/schemas.py` `SimilaritySearchRequest`)

```python
query: str | None = None            # 1..8000 when provided; REQUIRED iff embeds is non-empty
limit: int = 25                     # 1..10000 (unchanged)
embeds: list[Literal["apollo","name","title"]] | None = None
                                    # None (omitted) => ["apollo"]  — exact legacy behavior
                                    # []             => pure filter search, no vector ranking
company_id: str | None = None       # Mongo _id (hex) of a stored ORGANIZATION lead doc
email_exists: bool | None = None    # True => must have email; False => must NOT; None => no filter
phone_exists: bool | None = None    # same tri-state
linkedin_exists: bool | None = None # same tri-state
entity_type: Literal["person","organization"] | None = None
                                    # review-driven: orgs carry phone/linkedin, so exists-filters
                                    # would otherwise mix orgs into people-targeted audiences
```

Validation (422): `embeds` non-empty ⇒ `query` required; `embeds == []` ⇒ at least one filter
required (otherwise it's an unbounded "list leads"; `entity_type` counts as a filter); duplicate
kinds rejected. Normalization in the validator: `embeds == []` coerces a supplied `query` to
None (pure-filter runs ignore it — keeps history labels honest); `company_id` is stripped and
blank coerces to None *before* the filter count.

`company_id` resolution (review-driven dual resolution — kills the id-space round-trip trap):
try the value as the Mongo `_id` of an organization doc first; on miss, fall back to
`find_one({apollo_id: value, entity_type: "organization"})` — so both the record id a UI shows
and the Apollo org id on a person summary are valid inputs. Use the resolved org's `apollo_id`
as the filter value against people's top-level `company_id`. 404 only when both miss, with a
detail naming both accepted id forms.

### 4.2 Ranking semantics (the core change)

- Embed the query **once**; run one ANN search per selected kind with
  `expr = 'embed_kind == "<kind>"' + scalar filters`, each fetching an oversampled
  `min(limit * 4, 16384)` candidates.
- Merge in app code by `mongo_id`: **score = mean of cosine similarities over the selected
  kinds the doc actually has** (a doc missing a title embed is averaged over its present kinds,
  not penalized to 0; a doc present in a kind's index but outside that kind's top-k contributes
  nothing for that kind — the oversample keeps this rare). Docs with none of the selected kinds
  simply don't appear.
- Sort by mean score desc, cut to `limit`, hydrate from Mongo as today (order-preserving).

### 4.3 Pure filter search (`embeds == []`)

Straight Mongo query — no Milvus, no OpenAI (works even when either is down/unconfigured):
`{entity_type/company_id/email/phone conditions}`, sort `updated_at` desc, `limit`. Hits carry
`score: null` → `SimilarityHitOut.score` becomes `float | None` (additive).

### 4.4 Filter application (both paths)

Milvus `expr` scalar filters give **recall** (a filtered top-k, not top-k-then-filter); the
hydrated Mongo docs are then **re-checked authoritatively** against the same filters before
returning (defense against the §2.3 scalar staleness). Filter mapping: `company_id` →
`company_id == "<apollo org id>"`; `email_exists` → `has_email == True/False`; likewise
`phone_exists` → `has_phone` and `linkedin_exists` → `has_linkedin`.

### 4.5 Response

Shape unchanged (`{results: [{score, lead}]}`); `LeadOut` gains the six new top-level fields
(additive). MCP `summarize_lead` output gets them for free or via a one-line addition.

---

## 5. Search backend + MCP pass-through (all additive on v1 — P2)

- `search/app/schemas.py`: `SimilaritySearchRequest` and `McpSemanticSearchRequest` gain the
  same optional fields (`embeds`, `company_id`, `email_exists`, `phone_exists`,
  `linkedin_exists`; `query` becomes optional with the same conditional-requirement validation).
- `search/app/leads_client.py` `similarity_search(...)` (~line 842): forward the new params
  verbatim (omit `None`s).
- `search/app/routers/search.py` `_run_similarity_search` (~line 1365): accept + forward the new
  params; record them in `search_params_json` (so a history row shows *how* it was produced);
  history `query` label falls back to a filter description when `query` is empty (pure filter).
  Both consumers — the UI route `POST /api/search/similarity-search` (~line 1468) and the MCP
  route `POST /api/search/mcp/v1/searches/semantic` (`routers/mcp.py` ~line 146) — get the new
  behavior through this one shared helper.
- MCP tools (optional params with defaults = additive within v1; extend descriptions, don't
  repurpose):
  - `mcp/app/tools/leads.py` `similarity_search` — history-less variant, direct to leads.
  - `mcp/app/tools/search.py` `start_semantic_search` — history-writing variant.
- `lead_to_record` (`search/app/leads_client.py` ~line 334): prefer the new top-level
  `email`/`phone`/`linkedin`/`title`/`name` when present (cheaper + more accurate than re-deriving
  from raw payloads); output record shape is unchanged, so `searchui/src/types.ts` needs nothing.

**Mail-campaign fit (later program, design constraint now):** campaigns consume a
`search_id` + resolved recipients (`mail/app/schemas.py` `CampaignSourceIn`). The
history-writing semantic path already yields that `search_id`; `email_exists: true` +
`company_id` filtering is exactly the audience-building primitive. No mail changes in this
program — the endpoint shape is simply designed so the search-side surface is sufficient.

---

## 6. Search UI (`searchui/`)

All inside the existing similarity branch of `src/pages/SearchPage.tsx` (~lines 885–912) plus
`src/api.ts` (~659–680):

- **Embed-set selector**: three checkboxes (or a compact `ToggleButtonGroup`) — Apollo profile /
  Name / Title — **all three checked by default** (the new headline behavior; the UI always
  sends `embeds` explicitly, so the server's omitted-param legacy default never applies to the
  UI). Unchecking all three = pure filter search.
- **Filters**: "Company (record id)" text input (helper text: the Mongo id shown on a company
  record's detail pane), and three tri-state selects Any / Has / Missing for **Email**,
  **Phone**, and **LinkedIn** (maps to the `email_exists`/`phone_exists`/`linkedin_exists`
  tri-state).
- **Contact-presence icons on result rows** (`src/components/SearchResultsView.tsx`): show the
  linkedin / mail / phone icons next to a record when it has an existing top-level `linkedin` /
  `email` / `phone` value. The row icons currently key off `apollo_enriched` flags
  (`enrichedFlags`, ~lines 78–88 and 299–319 — "enrichment revealed this"), which is a different
  and narrower signal; **rekey them to value presence** on the normalized record
  (`linkedin_url` / resolved email / resolved phone, which `lead_to_record` now fills from the
  top-level Mongo fields). This applies to *all* results views (Apollo and similarity — same
  component), and the placeholder-aware extraction in §1.1 guarantees a mail icon means a real,
  unlocked email.
- **Submit gate** (`canSubmit`, ~line 211): embeds selected ⇒ non-empty query required (as
  today); embeds empty ⇒ at least one filter set; query field disabled/marked optional when no
  embeds are selected.
- `executeSimilaritySearch` (~line 231) and `SimilaritySearchParams`/`runSimilaritySearch`
  (`api.ts`) pass the new fields through. Results render through the existing
  `SearchResultsView`/history flow unchanged (the response `history` row is what the UI consumes).
- Verify with `npm run build` + `npm run lint`.

---

## 7. Rollout order

1. **leads — Mongo leg** (§1): derived fields + write paths + indexes. Independently shippable;
   new docs start carrying fields immediately.
2. **leads — Milvus leg** (§2–4): schema v2, indexing, endpoint v2, migration script.
3. **search + mcp pass-through** (§5). Backward compatible at every intermediate state (old
   search ↔ new leads works: omitted params = legacy behavior).
4. **searchui** (§6).
5. **Config pins** (platform-agent): `MILVUS_COLLECTION` in compose/env-examples/k3s manifests.
6. **Migrate**: dev — run the combined script in the leads container, drive every flow (§8);
   then prod deploy + run the script (`kubectl exec` into the leads pod). Between prod rollout
   and script completion the new collection is empty ⇒ similarity returns few/no hits — a known,
   bounded degradation window (pure-filter and Apollo search are unaffected); schedule the script
   immediately after rollout.

Implementation can run as a `/service-workstream` (leads-agent → search-agent + mcp-agent →
searchui-agent, adversarially reviewed per diff), with `/adversarial-review` + `/ship-branch`
to land it.

## 8. Verification (P11 interim contract — no test suite exists)

Drive dev compose end-to-end:

- Apollo people search → Mongo doc has `name/title/company_id` (+ `email/phone/linkedin` when
  unlocked); org search → org doc fields; enrich/match/webhook → fields upgrade, never regress;
  placeholder `email_not_unlocked@…` never lands in `email`.
- **Contact-field upsert (§1.3 guarantee)**: ingest a person via search (no email/phone), then
  enrich and fire the phone webhook — the *existing* doc's top-level `email`/`phone`/`linkedin`
  populate in place; re-running a plain search on the same person afterwards does not regress
  them.
- Run the migration script → old docs gain fields; new collection populated with per-kind rows.
- Similarity endpoint matrix: each embeds subset incl. default-omitted (legacy parity) and `[]`;
  each filter alone + combined; company_id of a real org doc / a person doc (404) / garbage
  (422); `embeds` set with empty query (422); `embeds: []` with no filters (422); Milvus down ⇒
  pure-filter still works, vector path 503s as today.
- UI: all-three default, subset selection, pure filter, all three tri-state filters (email /
  phone / linkedin), history row reopenable; row icons appear exactly for records with top-level
  `linkedin`/`email`/`phone` (and not for locked-placeholder emails); MCP: both tools with new
  params via a token-authorized call.
- Add unit tests where pure logic makes it cheap (`derive_top_fields` precedence/placeholder
  handling, score-merge averaging) as the start of the P11 pyramid.

**Verified live 2026-08-10 (dev compose, 95,729-doc legacy corpus):** all seven request-validation
cases; company + people Apollo ingest (fresh docs get name/title + `derived_at`); enrichment
upsert-in-place (name refined, `company_id`/`linkedin` populated, null email stays absent — zero
placeholder leaks corpus-wide); the combined migration (pass 1 backfilled all 95,729 docs, pass 2
rebuilt `leads_embeds_v2` with 7,045 rows ≈ 3 kinds/person); the similarity matrix (embed subsets
rank from distinct spaces, filters compose with vectors, pure-filter returns null scores,
company dual-resolution works from both id spaces with the descriptive 404, `entity_type`/
`linkedin_exists` filters); searchui serves via nginx. Operator invocation: `docker exec
funnelmanager-leads-1 python scripts/reembed.py` (k3s: `kubectl exec` into the leads pod).
**Not** exercised live: the Apollo phone-reveal webhook (needs a public callback), MCP tools at
protocol level (schema-verified only), browser-level UI interaction, and the write-race under a
real concurrent load (mock-verified).

**Payload-shape finding (expectation-setting):** Apollo `mixed_people/api_search` hits are
teaser-shaped — `first_name` + obfuscated last name, `title`, `has_*` flags, and an
organization object with **no id**. So search-ingested people get top-level `name`/`title` only;
`company_id`/`email`/`phone`/`linkedin` populate **via enrichment/match/webhook** (the §1.3
upsert guarantee is the load-bearing path). Mail-campaign audience filters like
`email_exists:true` therefore select from the enriched subset of the corpus.

## 9. Judgment calls baked into this plan (flag on review if wrong)

1. **Server default for omitted `embeds` is `["apollo"]`** (exact legacy behavior for existing
   MCP/agent callers, P2-additive); the *UI* defaults to all three. Alternative — make the
   3-way average the server default — rejected as a silent behavior change to a stable tool.
2. **Missing-kind averaging**: mean over the kinds a doc has, not penalize-to-zero.
3. **Org docs**: get top-level `phone`/`linkedin` only; `name`/`title`/`company_id`/`email`
   stay people-only per the spec ("name for people only").
4. **Milvus row-per-kind (Design B)** over native multi-vector hybrid search (§2.1).
5. **`company_id` filter 404s** on an unknown/non-org Mongo id rather than returning empty.
6. **Row icons are rekeyed, not duplicated**: the existing `apollo_enriched`-driven icons in
   `SearchResultsView` change meaning from "enrichment revealed this" to "record has this
   contact value" (a strictly more useful superset for campaign targeting) — rather than adding
   a second parallel icon set. `apollo_enriched` remains stored/visible in the record detail
   pane.

---

## 10. Review-driven amendments (implemented on the branch)

The adversarial review pass (bug-hunter + security-reviewer + quality-reviewer per workstream)
produced fixes that amend the spec above; all are implemented:

1. **Placeholder emails are stripped at every layer**, not just at extraction: `lead_to_record`
   blanks a placeholder `record.email`/`emails` for ALL docs (legacy included) before applying
   the top-level override; the MCP `summarize_lead` raw-email fallback is placeholder-aware;
   the UI keys person icons off the authoritative `has_email`/`has_phone` booleans and its CSV
   email resolver skips placeholders. (Previously the placeholder survived whenever the
   top-level field was absent — the exact case the suppression exists for.)
2. **`company_id` dual resolution** (§4.1) and the UI now shows the Mongo Record ID on the
   company detail pane; MCP docstrings name both id spaces.
3. **`entity_type` filter param** (§4.1) added through leads → search → MCP (no UI control yet).
4. **Fill-to-limit after the authoritative re-check**: candidates are hydrated in 500-doc chunks
   and filtered until `limit` passing docs accumulate — no more silently-short result sets.
5. **Scalar-drift re-index** (§2.3) closes the `email_exists:true` false-negative recall gap.
6. **Person top-level phone is surfaced** into `phone_numbers` (not just `has_phone`) so the UI
   can render/export it; org phone fallback includes `sanitized_phone` in MCP summaries.
7. **`_run_similarity_search` is the authoritative gate** for both validation rules (schema
   validators remain the user-facing 422 layer); embed name/title texts read the top-level
   derived fields so embeds agree with what the API reports; dead `lead_embedding_text` removed.
8. **Rollout note (§7)**: a live `.env` copied before this change may still pin
   `MILVUS_COLLECTION=leads_people`, which silently overrides the new default — verifying no
   stale override is an explicit migration step (prod k3s pins are authoritative and updated).
   *(Post-review: `docker-compose.prod.yml` + `.env.prod.example` were deleted outright —
   prod is k3s-only — so the seven pins referenced throughout this doc are now five.)*

Second round (full-branch re-review):

9. **Migration fail-fast**: `reembed.py` hard-fails (non-zero SystemExit) when `OPENAI_API_KEY`
   is unset, before pass 1 — previously it exited 0 after backfilling fields while silently
   skipping the entire Milvus rebuild.
10. **Lost-update race closed — two layers**: (a) the three write paths' update branches `$set`
    the single `apollo_responses.<endpoint>` entry via a dotted path instead of rewriting the
    whole map (verified no endpoint key contains a `.`); (b) because derived scalars are still
    computed from each writer's pre-write snapshot, the update is additionally **optimistically
    guarded** on the `(updated_at, derived_at)` values it read — a guard miss re-reads (seeing
    the racer's entry) and re-derives from the fuller map, converging on the correct
    per-field-precedence values; after 4 contended attempts a fallback writes only the safe
    additive parts (endpoint entry, no derived fields) and the next write self-heals. Known
    residual: a same-millisecond write pair can slip the ms-precision guard (vanishingly
    narrow, self-healing).
11. **`derived_at` authority marker**: every application of derived fields stamps `derived_at`
    (all write paths + backfill; exposed on LeadOut). `lead_to_record` gates the authoritative
    `has_email`/`has_phone` on this marker — not the name-presence proxy — so a name-less
    webhook-created person gets correct contact flags; legacy docs without the marker keep the
    contact-signals fallback.
12. **Similarity work caps** (agent-reachable amplification): the per-kind ANN budget is split
    across selected kinds (`min(limit*4, max(limit, 16384 // len(embeds)))`) — total candidate
    work is bounded by `max(16384, limit × len(embeds))` (the per-kind floor of `limit` is
    deliberate so each kind can contribute a full result set; at the 10000-limit ceiling with
    3 kinds that is 30000, vs 49152 uncapped) — and the fill-to-limit hydration scan examines
    at most `max(limit*10, 2000)` candidates, logging when truncated (no silent caps).
13. **Quality**: single `_milvus_str_literal` (router imports from `milvus_client`);
    `_Prepared`/`_prepare_lead_row` and the scalar-drift re-check extracted to module level so
    `index_lead_docs` reads as orchestration; shared `build_similarity_body` in
    `mcp/app/tools/_shared.py` (tool schemas verified byte-identical before/after).

Third round (prod field report, 2026-08-11):

14a. **`company_id` id-space correction (v1.15.3)**: the stored `company_id` on person docs is
    the **organization document's Mongo `_id`** (the original spec intent), not the Apollo org
    id — the raw Apollo id moved to `company_apollo_id` (resolution key; indexed). Write paths
    resolve key→link at write time (per-batch cached; re-resolves when the key changes, i.e. a
    new employer; pending links resolve on a later write once the org doc exists). The
    similarity filter still accepts both id spaces but normalizes to the Mongo `_id`; person
    summaries' `company_id` now round-trips directly into the filter. `reembed.py` pass 1
    migrates legacy Apollo-id values (dev: 185 resolved, 2 pending-unset).

14. **Context-fallback `company_id`** (fixes "company-filtered similarity returns nothing"):
    because `mixed_people` search hits are teaser-shaped, search-ingested people carried no
    `company_id` — in prod only ~0.4% of docs had one, so company-scoped similarity was
    near-guaranteed empty. When a people search is scoped to exactly one `organization_ids`
    filter, the request context now supplies the fallback `company_id` for ingested people
    (payload/enrichment-derived values always win; re-running an org-scoped search back-fills
    that company's existing people). The UI filter row gained a helper line explaining that
    exists-filters select from the enriched subset.
