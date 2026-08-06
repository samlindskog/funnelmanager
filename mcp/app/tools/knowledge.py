"""Knowledge tools — over the knowledge backend (``/api/knowledge/mcp/v1/*``).

Exchange edge: ``mcp->knowledge``. Two deliberately separate answer modes
(the knowledge-engine plan's core principle):

- ``knowledge_query_records`` — DETERMINISTIC queries over the records store
  (per-user interaction data: searches, campaigns, mail messages, agent
  sessions). Exact columns filter with SQL predicates; fuzzy columns match by
  embedding cosine similarity above a threshold. Every row carries provenance.
  Definite questions ("who did I message since X") belong HERE.
- ``knowledge_search`` — semantic recall over the Graphiti temporal knowledge
  graphs (shared application graph + the calling user's session graph).

``knowledge_remember`` stores cross-session context in the calling user's own
graph. ``knowledge_sync`` triggers immediate ingestion (its expensive graph
pass is P4-gated: over-threshold backlogs return confirmation_required, and an
agent-origin caller needs a human_approval token — never auto-confirm).
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from app.deps import Deps
from app.tools._shared import effective_token

_READ_ONLY = ToolAnnotations(readOnlyHint=True)
_WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False)
_SYNC = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)

_MCP = "/api/knowledge/mcp/v1"


def register(mcp: FastMCP, deps: Deps) -> None:
    knowledge = deps.knowledge
    if knowledge is None:  # pragma: no cover — registration is gated in tools/__init__
        return

    @mcp.tool(annotations=_READ_ONLY)
    async def knowledge_schema(
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Describe the queryable records schema: every record type, its exact
        columns (with supported operators) and fuzzy columns (embedding
        cosine-similarity matched), row counts, and the default similarity
        threshold. Call this BEFORE composing a knowledge_query_records call."""
        return await knowledge.request(
            "GET", f"{_MCP}/schema", token=effective_token(session_token, ctx)
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def knowledge_query_records(
        record_type: str,
        filters: dict[str, Any] | None = None,
        limit: int | None = None,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Deterministically query user-interaction records (cross-user; filter
        by owner for one person's activity). Use for DEFINITE questions — the
        answer comes from the source-of-truth data with provenance on each row.

        filters maps column -> filter-spec. Exact columns take
        {"eq"|"in"|"contains"|"gte"|"lte"|"before"|"after": value} per the
        schema; fuzzy columns take {"passage": text, "threshold": 0..1?} and
        match rows whose column embedding is cosine-similar to your passage
        above the threshold (scores are returned). Example — outbound mail to
        anyone since July 1st: record_type="mail_messages", filters=
        {"direction": {"eq": "outbound"}, "occurred_at": {"after":
        "2026-07-01T00:00:00Z"}}. Call knowledge_schema first for the schema."""
        body: dict[str, Any] = {"record_type": record_type, "filters": filters or {}}
        if limit is not None:
            body["limit"] = int(limit)
        return await knowledge.request(
            "POST", f"{_MCP}/query/records", json_body=body,
            token=effective_token(session_token, ctx),
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def knowledge_search(
        query: str = "",
        scope: Literal["app", "user", "all"] = "app",
        mode: Literal["search", "recent"] = "search",
        limit: int = 20,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Semantic search over the knowledge graphs — returns temporally
        stamped facts (valid_at/invalid_at) with episode provenance. scope:
        'app' = the shared application graph, 'user' = YOUR cross-session
        context graph, 'all' = both. mode='recent' lists recent episodes
        instead of searching (query may be empty). Use for fuzzy/relationship
        recall — NOT for definite counts/lists (use knowledge_query_records)."""
        return await knowledge.request(
            "POST", f"{_MCP}/search",
            json_body={"query": query, "scope": scope, "mode": mode, "limit": int(limit)},
            token=effective_token(session_token, ctx),
        )

    @mcp.tool(annotations=_WRITE)
    async def knowledge_remember(
        content: str,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Store session context for the CURRENT user, durable across sessions
        (their private context graph). Write a self-contained fact/preference/
        state note; retrieve later via knowledge_search(scope='user')."""
        return await knowledge.request(
            "POST", f"{_MCP}/remember", json_body={"content": content},
            token=effective_token(session_token, ctx),
        )

    @mcp.tool(annotations=_SYNC)
    async def knowledge_sync(
        sources: list[str] | None = None,
        confirm: bool = False,
        confirm_token: str | None = None,
        human_approval: str | None = None,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Immediately sync source data into the knowledge stores (normally
        automatic every few minutes). sources subset of: searches,
        mail_campaigns, mail_messages, agent_sessions. Records/embedding passes
        always run; the LLM graph-ingestion pass is cost-gated — an
        over-threshold backlog returns confirmation_required with an estimate
        instead of running (re-invoke with confirm=true; agent-origin callers
        must escalate to their human for approval, never self-confirm)."""
        body: dict[str, Any] = {"confirm": bool(confirm)}
        if sources is not None:
            body["sources"] = sources
        if confirm_token:
            body["confirm_token"] = confirm_token
        if human_approval:
            body["human_approval"] = human_approval
        return await knowledge.request(
            "POST", f"{_MCP}/sync", json_body=body,
            token=effective_token(session_token, ctx),
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def knowledge_stats(
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Knowledge-store status: record counts, per-source sync watermarks/
        errors, graph availability + pending (un-ingested) episode backlog,
        fuzzy-matching availability, and configured models."""
        return await knowledge.request(
            "GET", f"{_MCP}/stats", token=effective_token(session_token, ctx)
        )
