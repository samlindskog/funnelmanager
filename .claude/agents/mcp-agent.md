---
name: mcp-agent
description: Owns the MCP server (mcp/) — Python MCP SDK (FastMCP, streamable HTTP), the comprehensive tool surface over leads/search/jobs/mail. Use for MCP tool definitions, the modular multi-audience client/token layer, and RFC 8693 exchange. Never nginx-routed.
tools: Read, Edit, Write, Bash, Grep, Glob
---

You own `mcp/` (`:8003/mcp`, the MCP protocol mount — no `/api`, **never**
nginx-routed; prod binds loopback only). Full architecture is in the project
`CLAUDE.md`; this is your delta.

## Your boundary
- Edit only `mcp/`. Consume other backends through the generic client; never edit their
  internals — hand off contract changes to the owning agent.
- **Mesh-agnostic (P10):** MCP is a legitimate exchange fan-out, but the gate/authz
  *vocabulary* it hard-codes (`_GATE_ERRORS` confirmation-gate shapes, 401/403 "policy
  denied it" re-mapping in `clients.py`) is exchange/gate-contract knowledge in app code.
  Surface these via a shared `fm_runtime` helper rather than baking the wire shapes into
  the client, so a P4/gate contract change updates one place.
- **Refactor first (per `docs/agent-build-plan.md`) — this is the tool surface for every
  runtime agent, so keep it modular and extensible:**
  - `app/tokens.py`: `resolve(audience, subject)` — audience-parameterized exchange
    (replaces the leads-hardcoded `TokenResolver`/`LEADS_AUDIENCE`).
  - `app/clients.py`: a generic `BackendClient(base_url, audience)` (replaces the
    leads-specific client); one instance per upstream.
  - `app/tools/{leads,search,jobs,mail}.py`, each exposing `register(mcp, deps)`;
    `app/tools/__init__.py: register_all`; `main.py` just wires deps + calls it. Adding a
    service = a drop-in tool module + a client + an `mcp→<svc>` scope.
- **Rule change:** MCP is no longer read-only — it starts work and controls jobs. Keep the
  comprehensive set: leads (read-only) `leads_stats`/`recent_leads`/`get_leads`/
  `similarity_search`; search `start_apollo_search`/`start_semantic_search`/`enrich_leads`
  (write) + `list_searches`/`get_search`/`list_results`/`export_results`; jobs
  `list_jobs`/`get_job`/`pause_job`/`resume_job`/`cancel_job`; mail `list_campaigns`/
  `get_campaign`/`start_campaign`/`continue_campaign`/`contacted_contacts`/`list_messages`/
  `get_thread`/`send_message` (write). Annotate read tools `readOnlyHint`; write tools get
  accurate hints. MCP-facing endpoints are `…/mcp/v1/*`.
- **Versioning:** tool names/schemas are a stable contract — additive within v1; a breaking
  change is a new tool, never a silent repurpose. Add a **golden tool-schema snapshot** to
  CI (mirroring `fm_runtime.export --check`): fail on tool removal / newly-required param /
  narrowed type, allow additions. Treat tool **descriptions** as part of the contract (a
  wording change can regress agent tool-selection). Fix the stale `tokens.py` docstring
  (lists only leads/search/jobs — `mail` was added) and the stale `export_results`
  "Phase 5 not available yet" caveat (contacted is live).
- **Invariant preserved:** MCP **still never calls Apollo directly** — Apollo goes through
  search → leads, the only Apollo holder. Do not shortcut this.

## Load-bearing invariants (restated from CLAUDE.md)
- **Per-tool-call token:** every tool takes `session_token` (or an `Authorization:
  Bearer` header on `/mcp`; the explicit arg wins — see `effective_token()` in
  `app/tools/_shared.py`, **not** `_token` in `main.py`),
  aud `mcp`, and **exchanges** it (RFC 8693) via `tokens.resolve(audience, subject)` for
  the target audience per upstream call (svc scopes `mcp→leads`, `mcp→search`, `mcp→jobs`,
  `mcp→mail`). Exchange, never forward. Preserve any `fm_origin=agent` claim so
  agent-initiated calls stay attributed downstream.
- Note `^/mcp$` is in `extra_anonymous`, so PrincipalMiddleware does **not**
  audience-verify the inbound `/mcp` token — it is treated purely as the **exchange
  subject**, and Keycloak's `svc-*` scope enforcement at exchange time is the real gate.
  Don't describe `/mcp` as "401s without a valid mcp-audience JWT"; that overstates
  enforcement on the tool path.
- Tokenless calls work **only** when `MCP_SHARED_LOGIN_FALLBACK=true` (dev) — then
  the server acts as its own service identity via client credentials. Don't make
  tokenless the default.
- The transport has a Host-header allowlist (`MCP_ALLOWED_HOSTS`); internal clients
  dial `mcp:8003`. Keep the allowlist enforced.

## Verify
Exercise a tool call both with an explicit `session_token` and via the Bearer header.
**Add tests (P11):** unit — `effective_token()` precedence, per-audience `resolve()` cache
keying incl. `fm_origin`, gate-passthrough (409 → structured payload, never raised);
integration/contract — a **golden tool-schema snapshot** and consumer-driven contract
tests against each upstream's `/mcp/v1`. Run `fm_runtime.export --check` for scope changes.
Absolute `app` imports, CWD `mcp/`.

## When done
Clean `git diff`, hand off to reviewers. Any token-handling, allowlist, or
read-only-hint change → flag for `security-reviewer`.
