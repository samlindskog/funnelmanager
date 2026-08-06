# Knowledge & Semantic Understanding Engine — Program Plan

Status: **APPROVED 2026-08-06** (design decisions confirmed by Sam; research-grounded).
**Implementation status (2026-08-06):** the `knowledge` service is BUILT (see §14 —
Implementation notes) with the full MCP tool surface, per-user session graphs, the
schema-exposed records store, and watermark auto-ingestion + immediate sync. Agent
integration is opt-in via `KNOWLEDGE_BACKEND_URL` on the `mcp` container (set in dev
compose, empty in prod). Not yet done: k3s Neo4j StatefulSet + capacity work (§9.4),
jobs-producer wiring, the golden eval set.
Companion docs: `docs/agent-build-plan.md` (agents program), `docs/jobs-producer-api.md`
(producer contract), `docs/architecture.md`, `docs/authentication.md`.

## 1. Goal

Give the platform's AI (the `agents` runtime + this operator session) a knowledge/semantic
understanding layer that does two different jobs well:

1. **Deep context-awareness** — fuzzy, associative, relationship- and history-aware recall
   ("what do we know about this person/company, how did the relationship evolve, what was
   discussed") that makes agent responses feel informed rather than amnesiac.
2. **Correct definite answers** — questions with a canonical, checkable answer
   ("all senior devs in finance I messaged in the past month") answered from the
   **source-of-truth databases** with provenance, never from LLM recall.

The flagship example (not to be built verbatim — it shapes the design): *"find all the
cracked devs working in finance that I have messaged over the past month"* =
(mail DB: distinct counterparties of my sent mail since T) ∩ (leads DB: persons matching
structured predicates + embedding similarity above a threshold), joined on email, with
every returned row traceable to source records.

## 2. The load-bearing design principle: two answer modes, two systems

The research (see §13) is unambiguous and this plan is built on it:

- **Definite questions are a query-routing + deterministic-query + provenance problem,
  not a memory-recall problem.** Structured predicates (industry, seniority,
  messaged-since) must be scalar filters over the operational stores; semantic phrasing
  ("cracked devs") is an embedding-similarity component **with a score threshold**;
  results carry source-row provenance. An LLM-extracted graph re-answering these
  hallucinates and costs 20–100× more (GraphRAG cost literature; "Fidelity Before
  Structure", arXiv 2601.00821).
- **A temporal knowledge graph earns its cost as a supplementary semantic/relationship
  index** — evolving facts (title/company changes), cross-source relationship context,
  conversation-derived knowledge — with **provenance pointers back to source records**,
  never as the authority. Graphiti's bi-temporal model (valid_at/invalid_at +
  created_at/expired_at, contradiction ⇒ invalidate-never-delete) is exactly right for
  this role (arXiv 2501.13956).
- **The two modes are separated at the MCP tool boundary** (AWS MCP tool-design guidance):
  `knowledge_query_records` (deterministic, composable filters, provenance-bearing) vs
  `knowledge_search` (graph/semantic recall). One fuzzy god-tool answering definite
  questions is the failure mode this plan exists to prevent.

## 3. Decisions record (2026-08-06)

| # | Decision | Choice | Notes |
|---|---|---|---|
| D1 | Architecture strategy | **Dual-path, canonical first** | One new `knowledge` service owns both; canonical query path ships first, Graphiti KG layers on top |
| D2 | Graph store | **Neo4j 5.26 (community)** | Most mature Graphiti driver; requires capacity work (§9.4): quota bump + a third/bigger worker. FalkorDB rejected for its open concurrency bug (graphiti#1331) + 1000ms timeout footgun; Kuzu is deprecated/dead |
| D3 | V1 ingestion sources | **All four**: mail, leads, search history, agent sessions | Agent sessions land last (Phase 5), after the `feat/agents-sessions-logfire` rebuild merges |
| D4 | Extraction LLM | **OpenAI mid-tier extractor** (`gpt-4.1` main + `gpt-4.1-mini`/nano small_model), `text-embedding-3-small` (1536) | Quality-first extraction on dense email text; embeddings identical to leads' existing model. Env-tunable to mini-class if cost demands |

## 4. Architecture overview

New backend service **`knowledge`** (`:8007`, internal-only — never nginx-routed, prod
loopback like `mcp`/`jobs`), scaffolded by the `new-service` skill, with a dedicated
**Neo4j** StatefulSet (`knowledge-graph`) as its private store. It embeds
**`graphiti-core` as a Python library** (pinned `0.29.x`) — not Zep's REST/MCP servers —
so auth, tenancy, cost gating, and observability ride the platform's own rails
(fm_runtime `install()`, per-hop audiences, P4 gates).

```
                    ┌────────────────────────── browser (none in v1 — no UI)
agents ──MCP──► mcp ──exchange──► knowledge (:8007)
                                     │  ├── canonical query engine (composes source queries)
                                     │  ├── graphiti-core → Neo4j (knowledge-graph:7687)
                                     │  └── ingestion workers (poll/sync + P4-gated backfills)
                                     │        │
                          InternalClient (exchange, never forward)
                                     ▼
                     mail (:8004)   leads (:8001, via search where needed)   search (:8000)
```

Identity edges (all RFC 8693, lockstep-verified per P7):
- `mcp→knowledge` — tool calls carry the acting principal (human subject, `fm_origin`
  propagated).
- `knowledge→mail`, `knowledge→search`, `knowledge→leads` — the canonical query engine
  and ingestion workers. Background sync loops run as the service's client-credentials
  identity; user-triggered backfills run detached with the initiating principal
  (`InternalClient.detached`).
- New realm role **`knowledge-access`** gating `/api/knowledge` ONLY (P1: per-service
  access role; added to the `admin` composite → five `-access` roles). Grant it to the
  same groups that hold `agents-access` (the consumers are agents acting for humans).
  The service's own read fan-out to mail/search/agents lives on a separate
  **machine-only `knowledge-internal` role** (GET-only, held by the knowledge service
  account — the `jobs-internal` pattern); humans never receive it, so holding
  `knowledge-access` exposes no mail bodies/exports/transcripts directly.
- `jobs→knowledge` — knowledge is a **jobs producer** (`JOBS_PRODUCERS += knowledge=`),
  so backfills/sync work is visible and controllable (pause/resume/cancel) platform-wide.

Data-ownership boundaries preserved (P5/P9): knowledge holds **no** Apollo/Mongo/Milvus
client and never talks to Apollo/Google; it reads leads data through the sanctioned
APIs and stores only **derived** knowledge (graph) + **references** (provenance keys),
never copies of Apollo payloads or mail bodies as a second source of truth.

## 5. The canonical query path (ships first)

### 5.1 `query_records` — the composed deterministic query

A structured (JSON, not free-text) query contract executed by the knowledge service by
composing per-source deterministic sub-queries over `InternalClient` and joining on
deterministic keys. v1 grammar (additive per P2):

```jsonc
{
  "leads": {                       // optional
    "entity_type": "person",
    "filters": { "title_contains": ["engineer","developer"], "industry": ["finance"] },
    "semantic": { "passage": "exceptional software engineer", "threshold": 0.42, "limit": 500 }
  },
  "mail": {                        // optional
    "messaged_since": "2026-07-06T00:00:00Z",
    "direction": "outbound"        // sent by a connected mailbox
  },
  "combine": "intersect_on_email"  // or a single-source query with no combine
}
```

Response rows: normalized person/company records, each carrying **provenance**
(`lead:{mongo_id}`, `mail:{account_id}:{gmail_id}` / campaign message ids) and the
similarity score when a semantic component ran. The LLM **plans** the query (fills the
JSON via the MCP tool schema); the **system executes** it deterministically — the
TGMS pattern (arXiv 2607.10265). No text-to-SQL in v1; the tool schema is the semantic
layer.

### 5.2 Source-service enhancements (hand-offs)

- **leads** (leads-agent): extend similarity search with optional structured filters +
  score threshold. v1 implementation: vector search with an enlarged candidate limit →
  hydrate from Mongo → apply predicates (entity_type, title/industry/etc. from the raw
  Apollo payload) → threshold cut. This is post-filtering — acceptable at current corpus
  size; the documented upgrade path is a **v2 Milvus collection with scalar fields**
  (pre-filtering preserves recall; Milvus filtered-search docs) behind the same API.
  Note: today `similarity_search` can't even filter person vs organization — this fixes
  that too.
- **mail** (mail-agent): a deterministic **correspondents** endpoint, e.g.
  `GET /api/mail/mcp/v1/contacts/messaged?since=&until=&direction=` → distinct
  counterparty emails + first/last dates + counts + message-id provenance, computed in
  SQL over `mail_messages` (direction inferred from `SENT` label / `from_addr` =
  account email) and `mail_campaign_messages`. (The existing `/contacts/contacted` is
  campaign-scoped; this covers the whole archive with time bounds.)

### 5.3 Provenance & abstention contract

Every definite answer carries evidence IDs as first-class output. The MCP tool
descriptions + the agents system prompt instruct: **definite questions go to
`knowledge_query_records`; if it can't express the question, say so — never answer a
definite question from `knowledge_search` recall.** Empty/thin results are returned as
such (abstention over confident guessing; arXiv 2603.14170).

## 6. The knowledge graph (Graphiti on Neo4j)

### 6.1 Tenancy & attribution

- **One shared graph** — single `group_id` (`main`). P1: every principal with access
  sees the same knowledge; no per-user hiding. Writes are *attributed* via episode
  metadata (`owner`/`origin`/`actor` from the ingesting principal), never used to filter
  reads.
- Bi-temporal semantics as designed by Graphiti: `reference_time` = source event time
  (message `internal_date`, lead `updated_at`), so `valid_at` reflects the world, and
  contradictions invalidate rather than delete.

### 6.2 Ontology v1 (custom Pydantic entity/edge types)

Tight on purpose — additive evolution later (P2):

- Entities: `Person` (email, apollo_id, title, seniority, location),
  `Company` (primary_domain, apollo_id, industry, headcount_band).
  (Reserved attribute names — `uuid`,`name`,`group_id`,`labels`,`created_at`,`summary`,
  `attributes`,`name_embedding` — must not be reused.)
- Edges: `Employment` (Person→Company: title, seniority, valid-time-heavy),
  `Communication` (Person↔Person: channel, topics — relationship context, **not** the
  canonical send log, which stays in mail), plus the built-in `RELATES_TO` fallback.
- `edge_type_map`: `("Person","Company"): ["Employment"]`,
  `("Person","Person"): ["Communication"]`, wildcard fallback for the rest.

### 6.3 Entity resolution (the graph↔source join)

Deterministic keys first and authoritative: **email** for persons, **primary_domain**
for companies, `apollo_id`/`mongo_id` where present. Ingestion pre-resolves: episodes
embed the canonical key ("Jane Roe <jane@acme.com>") so extraction anchors on it, and a
post-ingest reconciliation pass links graph nodes to source records by key, storing the
key as a node attribute. LLM dedup handles only the residue and **never overrides a
deterministic key match** (LLM-dedup non-determinism is a documented failure mode).
Reconciliation drift (node without a source key where one should exist) warns + counts
(P9 observability discipline).

### 6.4 Retrieval surface

`graphiti.search()` hybrid (cosine + BM25 + BFS) with RRF default; `center_node_uuid` +
node-distance reranking for entity-scoped questions ("what do we know about ACME").
Facts return with `valid_at`/`invalid_at` so agents can reason about currency, and each
fact's episode provenance rides along. **Reranker: RRF (free) in v1** — the default
OpenAI reranker costs one LLM call per candidate; revisit only if eval demands.

## 7. Ingestion pipelines & cost control

Ingestion is the dominant cost (multiple LLM + embedding calls per episode; real-world
runaway reports: 1000+ requests for ~10k chars, graphiti#290). Controls are structural:

- **Tiered, selective ingestion.** Mail: skip promotional/no-reply/notification traffic
  (label heuristics: `CATEGORY_PROMOTIONS`/`CATEGORY_UPDATES`, no-reply senders);
  **thread-level episodes** (message-format, one episode per thread window) with long
  bodies summarized by the small_model first (summarize-then-ingest). Campaign sends
  ingest as cheap structured **json episodes** from `mail_campaign_messages` (no LLM
  narrative needed). Leads: one json episode per meaningfully-enriched record (dedup on
  `apollo_id`, re-ingest only on material change). Search history: tiny text episodes
  ("Sam searched X, N results") — near-free. Agent sessions (Phase 5): one summary
  episode per completed session, produced from the sessions rebuild's own summarizer.
- **Watermark polling** (no source events exist): mail — page `internal_date`/`updated_at`
  watermark + `gmail_id` dedup (mail emits no events; verified); leads — `updated_at`
  (indexed); search — poll `/mcp/v1/searches` or subscribe later. Sync loop cadence ~5m,
  mail-sync-style per-source locks.
- **Sequential `add_episode`, chronological per source** (oldest-first, correct
  `reference_time`) — preserves temporal invalidation; `add_episode_bulk` explicitly
  skips contradiction handling and is used **only** if a cold backfill proves too slow,
  documented as such. `build_communities()` deferred to end-of-backfill, then
  incremental. Backfill temporal gaps with out-of-order data are a known issue
  (graphiti#1489) — chronological ordering is the mitigation.
- **P4 gates on every backfill**: the backfill endpoint estimates episodes × avg tokens ×
  model price → over `FM_CONFIRM_KNOWLEDGE_BACKFILL_USD` (default $25) returns
  `409 confirmation_required` via `fm_runtime.require_confirmation`; agent origin
  requires `human_approval` (HMAC) as everywhere else. Plus a **daily ingest budget**
  (`KNOWLEDGE_DAILY_INGEST_USD`, circuit-breaker pauses the sync loop and emits a
  `paused` job event) — the OWASP LLM10 denial-of-wallet discipline.
- **Throughput/ratelimits**: `SEMAPHORE_LIMIT` env (start 5); ingestion runs as jobs
  (visible, pausable, cancelable via the jobs system).
- **Ballpark** (validate in Phase 3 with a 100-message sample before any real backfill):
  mid-tier extraction ≈ $0.02–0.05 per thread-episode → a 10k-thread archive ≈ $200–500
  one-time; steady-state incremental flow is small. The P4 estimate endpoint makes the
  real number visible before anything runs.

## 8. MCP tool surface (mcp service, new `knowledge` module)

`/api/knowledge/mcp/v1/*` on the service; tools registered in
`mcp/app/tools/knowledge.py` (audience `knowledge`, `mcp→knowledge` edge). Small,
intent-shaped set (P2: names/schemas/descriptions are a stable contract):

| Tool | Hint | Purpose |
|---|---|---|
| `knowledge_query_records` | read-only | The deterministic composed query (§5.1) — definite questions |
| `knowledge_search` | read-only | Graphiti hybrid search — facts/entities/communities with temporal metadata + provenance |
| `knowledge_entity` | read-only | Entity card: resolved node + current facts + timeline (center-node search) |
| `knowledge_stats` | read-only | Graph size, ingestion watermarks, last-sync, budget state |
| `knowledge_backfill` | write, P4-gated | Estimate-then-run a source backfill (never auto-runs over threshold) |

Agents discover tools automatically at connect (no agents-side allowlist today);
`agents/app/runner.py`'s SYSTEM_PROMPT gets one added paragraph naming the two answer
modes and the routing rule (§5.3).

## 9. Platform integration (P1–P11 mapping)

1. **Scaffold** via `.claude/skills/new-service` (spec: `name=knowledge`, `port=8007`,
   internal-only — `browser:false`, `callers=[mcp, jobs]`, `deps=[mail, search, leads]`,
   stateful=false for CNPG (the graph store is bespoke, §9.2)) — emitting all lockstep
   touchpoints: compose (both files), k3s base+overlay, netpol, realm clients/scopes
   (`svc-knowledge` on `mcp`+`jobs`; `svc-mail`/`svc-search`/`svc-leads` on `knowledge`;
   `fm-origin` default scope — **mandatory or origin resets to `user`**), `grants.py` +
   `data.json` + both realms (`knowledge-access`, admin composite → 5 roles,
   `jobs-internal` prefix on knowledge), CI (build matrix + import-test — jobs/agents
   were missed before, drift #23; knowledge must not repeat that), Flux healthChecks
   (drift #30 likewise), `FM_SERVICES` in the skill drivers, `knowledge-agent.md`,
   east-west canary manifest. Verify: `python -m fm_runtime.export --check … --realm`
   both realms.
2. **Neo4j StatefulSet** (`deploy/apps/base/data/knowledge-graph/`, mongo/milvus
   pattern): image `neo4j:5.26-community`, single replica, `role=worker` nodeSelector,
   PVC 10Gi local-path, auth from a `knowledge-graph-auth` Secret, heap 1g + pagecache
   512m via env, resources `requests 200m/1Gi, limits 1000m/2560Mi` (milvus-class),
   netpol: ingress only from `knowledge` on 7687 (no 7474 exposure), sidecar injection
   off (DB pattern). Nightly `neo4j-admin database dump` CronJob to the backups bucket
   (`knowledge-graph/` prefix), 14d retention — mirroring the CNPG backup posture.
3. **Canary**: `knowledge-canary` east-west manifest from the scaffold + **add its label
   to the keycloak-ingress allowlist** (the agents-canary lesson — token exchange is
   REJECTed at Keycloak otherwise) + Logfire/tracing via the standard `FM_LOGFIRE` gate.
4. **Capacity (pre-req, Phase 0)**: prod ns `limits.memory` quota 18Gi has ~2.5–3.5Gi
   free; Neo4j (2560Mi) + knowledge backend (768Mi) exceed it. (a) bump `fm-quota`
   `limits.memory` 18→22Gi and `requests.memory` 6→7Gi (`quotas.yaml`, precedent: the
   14→18 agents-wave bump); (b) **add worker3 (8GB Linode, `role=worker`)** per the
   ops-runbook node-join path — the two 8GB workers are already tight and `maxSurge: 0`
   exists because there's no surge headroom. Dev compose gets a `knowledge-graph`
   container (`neo4j:5.26-community`, mem_limit ~1g) alongside the service.
5. **P8/P10**: any streaming (backfill progress) uses the never-raise NDJSON pattern via
   `fm_runtime.JobProducer`; no mesh/exchange/authz vocabulary in app code — the P10
   sanctioned edges only (`InternalClient`, `require_confirmation`, JobProducer).
6. **P11 tests born with the service** (not retrofitted): unit — query-grammar
   compilation, ER key precedence, tier/skip heuristics, budget breaker; integration —
   Testcontainers Neo4j + mocked source edges: ingest→search round-trip, provenance
   integrity (every fact → episode → source key), P4 two-branch backfill, watermark
   resume; the golden eval set (§10). `pip install graphiti-core[neo4j]` pinned;
   `GRAPHITI_TELEMETRY_ENABLED=false`.

## 10. Evaluation & promotion gates

Vendor benchmarks (DMR/LoCoMo/LongMemEval) are contested and gameable — **build our own
golden set** from real product questions (the audited-LoCoMo lesson):

- ~50 questions across the modes: definite (known-answer from source DBs), entity-card,
  relationship/multi-hop, temporal ("as of June"), abstention-required (answer doesn't
  exist). Grade retrieval separately from generation: Recall@5/@10 per question type;
  definite questions graded exact-set vs source-DB truth; abstention graded as refusal.
- Gates: Phase 1 ships when definite-set accuracy = 100% on expressible queries (it's
  deterministic — anything less is a bug). The graph earns Phase-4 promotion only if it
  lifts the fuzzy/multi-hop classes over the no-graph baseline (the "verbatim chunks may
  beat extraction" contrarian check, run honestly). Eval harness lives in
  `knowledge/tests/eval/` and runs against the canary (drive-canary + observe-grafana
  loop; P4-budgeted read-mostly).

## 11. Phases

- **Phase 0 — Capacity + scaffold.** Worker3 + quota bump; `new-service` scaffold;
  Neo4j StatefulSet + dev-compose container; empty service green in prod
  (healthz/readyz, whoami 403, export --check clean). *Owners: platform-agent (+
  provisioning runbook), knowledge-agent (scaffold output).*
- **Phase 1 — Canonical query path.** leads filtered-similarity + mail correspondents
  endpoints; query engine + `knowledge_query_records` + `knowledge_stats` MCP tools;
  provenance contract; golden definite-set green. The flagship example query works
  end-to-end here — before any graph exists. *Owners: leads-agent, mail-agent,
  knowledge-agent, mcp-agent.*
- **Phase 2 — Graphiti core.** graphiti-core wired to Neo4j; ontology v1; internal
  episode API + `knowledge_search`/`knowledge_entity` tools; manual episodes only;
  ingest→search integration tests. *Owner: knowledge-agent (+ mcp-agent).*
- **Phase 3 — Ingestion.** Watermark sync loops + P4-gated backfills for mail (tiered,
  thread-level), leads, search history; jobs-producer wiring; budget breaker; 100-sample
  cost validation, then operator-approved backfills. *Owners: knowledge-agent;
  jobs-agent (producer registration).*
- **Phase 4 — Agent integration + eval gate.** System-prompt routing paragraph; full
  golden-set run vs the no-graph baseline; canary-driven E2E (drive-canary); promotion
  decision on the eval evidence. *Owners: agents-agent, knowledge-agent.*
- **Phase 5 — Agent-session ingestion + tuning.** After `feat/agents-sessions-logfire`
  merges: session-summary episodes; communities; reranker/recipe tuning; revisit Milvus
  v2 scalar-field collection if post-filter recall measurably suffers. *Owners:
  knowledge-agent, agents-agent, leads-agent.*

Each phase ends with the standard gate: owning agents implement → adversarial review
(bug-hunter / security-reviewer / quality-reviewer) → fix → verify (P11 ladder) → ship
via ship-branch/deploy-funnelmanager. Phases 1 and 2 can overlap after Phase 0; nothing
in 2–5 blocks the Phase-1 value.

## 12. Risks & mitigations

| Risk | Mitigation |
|---|---|
| Ingestion cost blowup (the #1 reported Graphiti failure) | Tiering + summarize-then-ingest + P4 estimate/confirm + daily budget breaker + SEMAPHORE_LIMIT; costs visible via `knowledge_stats` |
| LLM dedup merges two real people | Deterministic keys authoritative (§6.3); reconciliation warns on drift; graph is advisory — canonical answers never depend on it |
| Neo4j RAM pressure on the cluster | Phase-0 capacity work is a hard pre-req; milvus-class envelope; `maxSurge:0` respected; FalkorDB remains a documented fallback behind Graphiti's driver abstraction |
| graphiti-core version churn (0.29 was a breaking redesign) | Pin exact version; upgrade deliberately with the eval set as the regression gate |
| Graph answers leaking into definite questions | Tool-boundary separation + system-prompt routing + eval abstention class; provenance mandatory in `query_records` output |
| Stale graph vs fresh source (post-ingest lag) | Facts carry `valid_at` + provenance; agents re-verify definite claims via `query_records`; watermarks exposed in `knowledge_stats` |
| Mail poll misses edits/deletions | Watermark on `updated_at` (label flips / `is_deleted` bump it); `is_deleted` rows trigger episode removal + orphan sweep (graphiti#1083 pattern) |

## 13. Research references (load-bearing)

- Graphiti: repo/docs (github.com/getzep/graphiti, help.getzep.com), paper arXiv
  2501.13956; issues #290/#1193 (cost), #1331 (FalkorDB group_id contamination), #1826
  (FalkorDB timeout), #1489 (backfill temporal gaps), #1083 (orphan nodes); Zep scaling
  post (blog.getzep.com/scaling-agent-memory-zep-30x) — their biggest cost fixes live in
  the hosted layer, not OSS.
- Benchmark skepticism: github.com/getzep/zep-papers/issues/5 (LoCoMo 84→58.44 dispute);
  audited LoCoMo flaws (wrong keys, judge gameability) — hence §10's own golden set.
- Canonical-answer patterns: TGMS deterministic operators (arXiv 2607.10265);
  semantic-layer text-to-SQL reliability (WrenAI/MDL); Milvus pre- vs post-filtering
  (milvus.io filtered-search); evidence-ID citation enforcement + abstention (arXiv
  2603.14170).
- Cost/skeptic literature: GraphRAG index economics + LazyGraphRAG (microsoft.com/research);
  HippoRAG 2 (github.com/osu-nlp-group/hipporag); "Fidelity Before Structure" verbatim-
  chunks result (arXiv 2601.00821) — the honest-baseline check in §10.

## 14. Implementation notes (as built, 2026-08-06)

Deltas and refinements vs the sections above, decided during the build:

- **Records store = `knowledge-db` Postgres + pgvector** (not Milvus): the
  deterministic `query_records` engine runs exact SQL predicates and fuzzy
  cosine-similarity (`1 - cosine_distance >= threshold`) in ONE statement over
  `knowledge`'s own database. §5.2's source-service enhancements (leads filtered
  similarity, mail correspondents endpoint) became unnecessary for v1 — the
  records store denormalizes the needed columns with provenance instead, and the
  sources are read via GET-only `knowledge→{mail,search,agents}` edges. Record
  types v1: `searches`, `mail_campaigns`, `mail_messages` (direction inferred:
  SENT label / from==account), `agent_sessions` — the per-user interaction data.
  The schema is agent-visible via `knowledge_schema` (registry-driven; the same
  registry validates queries and compiles SQL).
- **Per-user session graphs** (user request, supersedes §6.1's single-group
  design): Graphiti group_ids — `main` for application knowledge, `user-<name>`
  per user for cross-session context (`knowledge_remember`, `knowledge_search
  scope=user`, auto-ingestion of agent-session transcripts). Neo4j community is
  single-database; group_id is Graphiti's designed tenancy primitive. The
  per-user scope is an approved P1 carve-out (own-memory, principal-bound);
  the records store stays cross-user with `owner` as an ordinary column.
- **MCP tools shipped**: `knowledge_schema`, `knowledge_query_records`,
  `knowledge_search` (scope app|user|all, mode search|recent),
  `knowledge_remember`, `knowledge_sync` (P4-gated graph pass),
  `knowledge_stats`. Registered only when `KNOWLEDGE_BACKEND_URL` is set on the
  mcp container (dev: on; prod: empty until the operator opts in).
- **Ingestion**: watermark polling every `SYNC_INTERVAL_SECONDS` (mail has no
  event stream — verified), single-flight; cheap passes (records + embeddings)
  always run; the LLM graph pass auto-ingests ≤ `GRAPH_AUTO_BATCH`/cycle from
  `GRAPH_INGEST_SOURCES` (default searches,mail_campaigns; mail_messages
  opt-in) and REFUSES backlogs over `KNOWLEDGE_SYNC_CONFIRM_EPISODES` — those
  run only through the confirmed `knowledge_sync` (409 estimate → confirm;
  agent origin needs human approval). Chronological (oldest-first) sequential
  add_episode per §7.
- **Auth deltas** (post security-review): the roles are SPLIT — `knowledge-access`
  (humans) grants `/api/knowledge` only; **`knowledge-internal`** (machine-only,
  held by the `knowledge` service account in both realms, `jobs-internal`
  pattern) grants GET-only on mail/search/agents for the sync connectors, which
  always run under the service identity (`follow_context=False`) — never the
  calling human's. Verified by `fm_runtime.export --check` on both realms.
- **Degradation contract**: missing Neo4j/OPENAI_API_KEY keeps the service
  healthy (records queries work; graph/fuzzy 503 with a reason, surfaced in
  `knowledge_stats`) — this is what makes shipping the service ahead of the
  §9.4 capacity work safe. Prod compose + k3s deploy of Neo4j itself remains
  platform work (dev compose has `knowledge-graph` neo4j:5.26-community).
- **Pins**: graphiti-core==0.29.3 (0.29.0 was a breaking redesign — upgrade only
  against the eval set), openai==1.109.1 (graphiti floors >=1.91), pgvector
  image for knowledge-db (`pgvector/pgvector:pg16` in compose; CNPG image must
  bundle pgvector — see the cluster.yaml note).
