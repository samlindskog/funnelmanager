"""Leads tools — read-only inspection over the leads backend.

All four tools are ``readOnlyHint`` and free: they inspect leads already stored
by the platform and never call Apollo or spend credits. Exchange edge:
``mcp->leads``.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from app.deps import Deps
from app.summarize import summarize_lead
from app.tools._shared import effective_token

_READ_ONLY = ToolAnnotations(readOnlyHint=True)


def register(mcp: FastMCP, deps: Deps) -> None:
    leads = deps.leads

    @mcp.tool(annotations=_READ_ONLY)
    async def leads_stats(
        session_token: str | None = None, ctx: Context = None
    ) -> dict[str, Any]:
        """MongoDB lead-collection overview: totals by entity type, how many are
        embedded in Milvus vs pending, and enrichment counts (linkedin/email/phone)."""
        return await leads.request(
            "GET", "/api/leads/stats", token=effective_token(session_token, ctx)
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def recent_leads(
        entity_type: Literal["person", "organization"] | None = None,
        enriched: bool | None = None,
        embedded: bool | None = None,
        limit: int = 50,
        skip: int = 0,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> list[dict[str, Any]]:
        """Most recently updated leads — the ingest/enrichment activity feed of the
        leads backend. Filter by entity_type, enriched (True: any enrichment ran;
        False: none), or embedded (Milvus index state). Returns compact summaries
        including the per-lead Apollo endpoint timeline; use get_leads(include_raw=True)
        for full payloads."""
        params: dict[str, Any] = {
            "limit": max(1, min(limit, 500)),
            "skip": max(skip, 0),
        }
        if entity_type is not None:
            params["entity_type"] = entity_type
        if enriched is not None:
            params["enriched"] = enriched
        if embedded is not None:
            params["embedded"] = embedded
        rows = await leads.request(
            "GET",
            "/api/leads/recent",
            params=params,
            token=effective_token(session_token, ctx),
        )
        return [summarize_lead(lead) for lead in rows if isinstance(lead, dict)]

    @mcp.tool(annotations=_READ_ONLY)
    async def get_leads(
        mongo_ids: list[str],
        include_raw: bool = False,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> list[dict[str, Any]]:
        """Batch-fetch leads by Mongo `_id` (up to 500, order preserved, missing
        omitted). Compact summaries by default; include_raw=True returns the full
        stored documents with every endpoint-keyed Apollo payload (large!)."""
        ids = [str(item).strip() for item in mongo_ids if str(item or "").strip()]
        if not ids:
            return []
        rows = await leads.request(
            "POST",
            "/api/leads",
            json_body={"ids": ids[:500]},
            token=effective_token(session_token, ctx),
        )
        if include_raw:
            return [lead for lead in rows if isinstance(lead, dict)]
        return [summarize_lead(lead) for lead in rows if isinstance(lead, dict)]

    @mcp.tool(annotations=_READ_ONLY)
    async def similarity_search(
        query: str,
        limit: int = 25,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> list[dict[str, Any]]:
        """Semantic search over already-stored leads (OpenAI embedding + Milvus).
        Searches only what's in MongoDB — never calls Apollo and writes no search
        history. Returns scored compact summaries."""
        data = await leads.request(
            "POST",
            "/api/leads/similarity-search",
            json_body={"query": query, "limit": max(1, min(limit, 10000))},
            token=effective_token(session_token, ctx),
        )
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            return []
        out: list[dict[str, Any]] = []
        for hit in results:
            if not isinstance(hit, dict) or not isinstance(hit.get("lead"), dict):
                continue
            out.append({"score": hit.get("score"), **summarize_lead(hit["lead"])})
        return out
