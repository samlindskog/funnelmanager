"""Internal MCP server for Funnel Manager.

Exposes read-only inspection tools over MCP streamable HTTP (endpoint ``/mcp``)
for internal agents (e.g. OpenClaw). Runs only on the compose network — nginx
never routes to it. All data flows through the existing service APIs: search
history / user activity via the search backend (with a real session token),
stored Apollo leads + enrichment state via the internal leads backend. No tool
calls Apollo, spends credits, or mutates data.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.clients import AuthSession, LeadsBackendClient, SearchBackendClient
from app.config import get_settings
from app.summarize import slim_record, summarize_lead

settings = get_settings()
auth = AuthSession(settings)
search_backend = SearchBackendClient(settings, auth)
leads_backend = LeadsBackendClient(settings)

mcp = FastMCP(
    "funnelmanager",
    instructions=(
        "Read-only inspection of the Funnel Manager stack. Use the search_* tools "
        "to see user activity (search history and stored results, hydrated the same "
        "way the UI renders them) and the leads_* tools to inspect stored Apollo "
        "records and their enrichment state. Nothing here calls Apollo or mutates data."
    ),
    stateless_http=True,
    json_response=True,
)

_READ_ONLY = ToolAnnotations(readOnlyHint=True)


# ---------------------------------------------------------------------------
# Search backend — user activity
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY)
async def search_history() -> list[dict[str, Any]]:
    """List recent searches (most recent first, up to 100): query label,
    entity type, result count, and when each search ran. This is the user
    activity log of the search backend."""
    return await search_backend.request("GET", "/api/searches")


@mcp.tool(annotations=_READ_ONLY)
async def search_results(
    search_id: int,
    page: int = 1,
    include_raw: bool = False,
) -> dict[str, Any]:
    """Load one page of stored results for a search, hydrated from MongoDB
    exactly as the UI renders them (100 results per page). Set include_raw=True
    to include full Apollo payloads per record (large!)."""
    data = await search_backend.request(
        "POST", f"/api/searches/{search_id}/page", json_body={"page": page}
    )
    history = data.get("history") if isinstance(data, dict) else None
    if include_raw or not isinstance(history, dict):
        return data
    results = history.get("results")
    if isinstance(results, list):
        history["results"] = [
            slim_record(record) if isinstance(record, dict) else record for record in results
        ]
    return data


@mcp.tool(annotations=_READ_ONLY)
async def get_lead(mongo_id: str, include_raw: bool = False) -> dict[str, Any]:
    """Fetch one lead by Mongo `_id`, normalized like the UI detail pane
    (name, contact info, enrichment flags). Set include_raw=True for the full
    endpoint-keyed Apollo payloads."""
    record = await search_backend.request("GET", f"/api/leads/{mongo_id}")
    if include_raw or not isinstance(record, dict):
        return record
    return slim_record(record)


@mcp.tool(annotations=_READ_ONLY)
async def apollo_credits() -> dict[str, Any]:
    """Current Apollo credit balance (credits_remaining, lead_credits_used,
    effective_lead_credits) — the same numbers shown in the UI header."""
    return await search_backend.request("GET", "/api/apollo/credits")


# ---------------------------------------------------------------------------
# Leads backend — stored Apollo records and enrichment state
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY)
async def leads_stats() -> dict[str, Any]:
    """MongoDB lead-collection overview: totals by entity type, how many are
    embedded in Milvus vs pending, and enrichment counts (linkedin/email/phone)."""
    return await leads_backend.request("GET", "/api/leads/stats")


@mcp.tool(annotations=_READ_ONLY)
async def recent_leads(
    entity_type: Literal["person", "organization"] | None = None,
    enriched: bool | None = None,
    embedded: bool | None = None,
    limit: int = 50,
    skip: int = 0,
) -> list[dict[str, Any]]:
    """Most recently updated leads — the ingest/enrichment activity feed of the
    leads backend. Filter by entity_type, enriched (True: any enrichment ran;
    False: none), or embedded (Milvus index state). Returns compact summaries
    including the per-lead Apollo endpoint timeline; use get_leads(include_raw=True)
    for full payloads."""
    params: dict[str, Any] = {"limit": max(1, min(limit, 500)), "skip": max(skip, 0)}
    if entity_type is not None:
        params["entity_type"] = entity_type
    if enriched is not None:
        params["enriched"] = enriched
    if embedded is not None:
        params["embedded"] = embedded
    leads = await leads_backend.request("GET", "/api/leads/recent", params=params)
    return [summarize_lead(lead) for lead in leads if isinstance(lead, dict)]


@mcp.tool(annotations=_READ_ONLY)
async def get_leads(mongo_ids: list[str], include_raw: bool = False) -> list[dict[str, Any]]:
    """Batch-fetch leads by Mongo `_id` (up to 500, order preserved, missing
    omitted). Compact summaries by default; include_raw=True returns the full
    stored documents with every endpoint-keyed Apollo payload (large!)."""
    ids = [str(item).strip() for item in mongo_ids if str(item or "").strip()]
    if not ids:
        return []
    leads = await leads_backend.request("POST", "/api/leads", json_body={"ids": ids[:500]})
    if include_raw:
        return [lead for lead in leads if isinstance(lead, dict)]
    return [summarize_lead(lead) for lead in leads if isinstance(lead, dict)]


@mcp.tool(annotations=_READ_ONLY)
async def similarity_search(query: str, limit: int = 25) -> list[dict[str, Any]]:
    """Semantic search over already-stored leads (OpenAI embedding + Milvus).
    Searches only what's in MongoDB — never calls Apollo and writes no search
    history. Returns scored compact summaries."""
    data = await leads_backend.request(
        "POST",
        "/api/leads/similarity-search",
        json_body={"query": query, "limit": max(1, min(limit, 10000))},
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


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "mcp"})


# Served by uvicorn (see Dockerfile); streamable HTTP endpoint mounts at /mcp.
app = mcp.streamable_http_app()
