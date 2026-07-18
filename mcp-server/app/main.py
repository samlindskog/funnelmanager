"""Internal MCP server for Funnel Manager.

Exposes read-only inspection tools over MCP streamable HTTP (endpoint ``/mcp``)
for internal agents (e.g. OpenClaw). Runs only on the compose network — nginx
never routes to it. All data flows through the existing service APIs: search
history / user activity via the search backend, stored Apollo leads +
enrichment state via the leads backend. No tool calls Apollo directly.

Every backend now enforces per-profile authorization (auth service + OPA), so
each tool call must carry a session token. Priority order:

1. the ``session_token`` tool argument (the OpenClaw harness plugin injects it
   or the agent passes the value from ``funnelmanager_session_token``),
2. an ``Authorization: Bearer`` header on the MCP HTTP request,
3. the optional shared-login dev fallback (MCP_SHARED_LOGIN_FALLBACK).
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.clients import (
    AuthSession,
    LeadsBackendClient,
    SearchBackendClient,
    TokenResolver,
)
from app.config import get_settings
from app.summarize import slim_record, summarize_lead

settings = get_settings()
tokens = TokenResolver(AuthSession(settings))
search_backend = SearchBackendClient(settings, tokens)
leads_backend = LeadsBackendClient(settings, tokens)

mcp = FastMCP(
    "funnelmanager",
    instructions=(
        "Funnel Manager (Apollo person/company inspector). Inspection tools "
        "(search_history, search_results, get_lead*, recent_leads, leads_stats, "
        "similarity_search, apollo_credits) are read-only and free. Action tools "
        "(run_people_search, run_company_search, enrich_person, enrich_organization, "
        "match_person) call Apollo and SPEND CREDITS — check apollo_credits when in "
        "doubt and never loop them blindly. Searches persist to history and keep "
        "ingesting server-side after the tool returns page 1; poll search_results "
        "for later pages. AUTH: every tool accepts session_token — the OpenClaw "
        "harness injects it automatically; if a call fails with a missing/expired "
        "token, pass the value from the funnelmanager_session_token tool. Never "
        "invent a token."
    ),
    stateless_http=True,
    json_response=True,
    # Default rebinding protection only allows localhost hosts; internal
    # clients dial the compose service name, so allow it explicitly
    # (override with MCP_ALLOWED_HOSTS).
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=settings.mcp_allowed_host_list,
    ),
)

_READ_ONLY = ToolAnnotations(readOnlyHint=True)
# Apollo-calling tools: not read-only (they upsert leads + history and spend
# credits), but not destructive either — they only add/refresh data.
_APOLLO_ACTION = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)


def _request_token(ctx: Context | None) -> str | None:
    """Bearer token from the MCP HTTP request's Authorization header, if any."""
    try:
        request = ctx.request_context.request if ctx is not None else None
    except (AttributeError, ValueError):
        return None
    if request is None:
        return None
    header = ""
    try:
        header = request.headers.get("authorization") or ""
    except AttributeError:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def _token(session_token: str | None, ctx: Context | None) -> str | None:
    """Effective token for one tool call (explicit arg wins over HTTP header)."""
    return (session_token or "").strip() or _request_token(ctx)


# ---------------------------------------------------------------------------
# Search backend — user activity
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY)
async def search_history(
    session_token: str | None = None, ctx: Context = None
) -> list[dict[str, Any]]:
    """List recent searches (most recent first, up to 100): query label,
    entity type, result count, and when each search ran. This is the user
    activity log of the search backend."""
    return await search_backend.request(
        "GET", "/api/search/searches", token=_token(session_token, ctx)
    )


@mcp.tool(annotations=_READ_ONLY)
async def search_results(
    search_id: int,
    page: int = 1,
    include_raw: bool = False,
    session_token: str | None = None,
    ctx: Context = None,
) -> dict[str, Any]:
    """Load one page of stored results for a search, hydrated from MongoDB
    (100 results per page). Set include_raw=True to include full Apollo
    payloads per record (large!)."""
    data = await search_backend.request(
        "POST",
        f"/api/search/searches/{search_id}/page",
        json_body={"page": page},
        token=_token(session_token, ctx),
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
async def get_lead(
    mongo_id: str,
    include_raw: bool = False,
    session_token: str | None = None,
    ctx: Context = None,
) -> dict[str, Any]:
    """Fetch one lead by Mongo `_id`, normalized like the UI detail pane
    (name, contact info, enrichment flags). Set include_raw=True for the full
    endpoint-keyed Apollo payloads."""
    record = await search_backend.request(
        "GET", f"/api/search/leads/{mongo_id}", token=_token(session_token, ctx)
    )
    if include_raw or not isinstance(record, dict):
        return record
    return slim_record(record)


@mcp.tool(annotations=_READ_ONLY)
async def apollo_credits(
    session_token: str | None = None, ctx: Context = None
) -> dict[str, Any]:
    """Current Apollo credit balance (credits_remaining, lead_credits_used,
    effective_lead_credits)."""
    return await search_backend.request(
        "GET", "/api/search/apollo/credits", token=_token(session_token, ctx)
    )


# ---------------------------------------------------------------------------
# Leads backend — stored Apollo records and enrichment state
# ---------------------------------------------------------------------------


@mcp.tool(annotations=_READ_ONLY)
async def leads_stats(
    session_token: str | None = None, ctx: Context = None
) -> dict[str, Any]:
    """MongoDB lead-collection overview: totals by entity type, how many are
    embedded in Milvus vs pending, and enrichment counts (linkedin/email/phone)."""
    return await leads_backend.request(
        "GET", "/api/leads/stats", token=_token(session_token, ctx)
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
    params: dict[str, Any] = {"limit": max(1, min(limit, 500)), "skip": max(skip, 0)}
    if entity_type is not None:
        params["entity_type"] = entity_type
    if enriched is not None:
        params["enriched"] = enriched
    if embedded is not None:
        params["embedded"] = embedded
    leads = await leads_backend.request(
        "GET", "/api/leads/recent", params=params, token=_token(session_token, ctx)
    )
    return [summarize_lead(lead) for lead in leads if isinstance(lead, dict)]


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
    leads = await leads_backend.request(
        "POST", "/api/leads", json_body={"ids": ids[:500]}, token=_token(session_token, ctx)
    )
    if include_raw:
        return [lead for lead in leads if isinstance(lead, dict)]
    return [summarize_lead(lead) for lead in leads if isinstance(lead, dict)]


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
    data = await leads_backend.request(
        "POST",
        "/api/leads/similarity-search",
        json_body={"query": query, "limit": max(1, min(limit, 10000))},
        token=_token(session_token, ctx),
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


# ---------------------------------------------------------------------------
# Apollo actions — spend credits, mutate stored data (searches + enrichment)
# ---------------------------------------------------------------------------


def _search_outcome(outcome: dict[str, Any], preview_limit: int) -> dict[str, Any]:
    data = outcome.get("data") or {}
    history = data.get("history") if isinstance(data.get("history"), dict) else {}
    results = history.get("results") if isinstance(history.get("results"), list) else []
    preview = [
        slim_record(record)
        for record in results[: max(0, preview_limit)]
        if isinstance(record, dict)
    ]
    complete = outcome.get("event") == "complete"
    return {
        "search_id": history.get("id"),
        "query": history.get("query"),
        "status": "complete" if complete else "ingest_running",
        "note": (
            "Ingest finished; all results are stored."
            if complete
            else "Page 1 returned; ingest keeps running server-side. Use search_results "
            "(same search_id) to read later pages, and search_history to watch "
            "total_results grow."
        ),
        "pagination": data.get("pagination"),
        "results_preview": preview,
    }


@mcp.tool(annotations=_APOLLO_ACTION)
async def run_people_search(
    query: str = "",
    organization_name: str | None = None,
    organization_id: str | None = None,
    organization_domain: str | None = None,
    preview_limit: int = 25,
    session_token: str | None = None,
    ctx: Context = None,
) -> dict[str, Any]:
    """Run an Apollo people search (SPENDS APOLLO CREDITS). Needs query text
    and/or a company filter (organization_name OR organization_id, optionally
    organization_domain). Persists a search-history entry, returns page 1 as a
    preview, and keeps ingesting all matching pages (up to 100k) server-side.
    Do not repeat an identical search; reuse the stored one via search_results
    instead."""
    body: dict[str, Any] = {"entity_type": "people", "query": query or ""}
    if organization_name:
        body["organization_name"] = organization_name
    if organization_id:
        body["organization_id"] = organization_id
    if organization_domain:
        body["organization_domain"] = organization_domain
    outcome = await search_backend.stream_search(
        "/api/search/search", body, token=_token(session_token, ctx)
    )
    return _search_outcome(outcome, preview_limit)


@mcp.tool(annotations=_APOLLO_ACTION)
async def run_company_search(
    keywords: str = "",
    company_name: str | None = None,
    company_domain: str | None = None,
    preview_limit: int = 25,
    session_token: str | None = None,
    ctx: Context = None,
) -> dict[str, Any]:
    """Run an Apollo company/organization search (SPENDS APOLLO CREDITS). Needs
    keywords and/or company_name and/or company_domain. Same behavior as
    run_people_search: persists history, returns page 1, ingest continues
    server-side."""
    body: dict[str, Any] = {"entity_type": "companies", "query": keywords or ""}
    if company_name:
        body["company_name"] = company_name
    if company_domain:
        body["company_domain"] = company_domain
    outcome = await search_backend.stream_search(
        "/api/search/search", body, token=_token(session_token, ctx)
    )
    return _search_outcome(outcome, preview_limit)


@mcp.tool(annotations=_APOLLO_ACTION)
async def enrich_person(
    apollo_id: str,
    include_raw: bool = False,
    session_token: str | None = None,
    ctx: Context = None,
) -> dict[str, Any]:
    """Apollo Complete Person Info enrichment for one person (SPENDS APOLLO
    CREDITS). Upserts the stored lead (no duplicates) and returns the refreshed
    record. Get apollo_id from search results / lead records (the `id` field)."""
    record = await search_backend.request(
        "POST",
        f"/api/search/people/enrich/{apollo_id}",
        json_body={},
        token=_token(session_token, ctx),
    )
    if include_raw or not isinstance(record, dict):
        return record
    return slim_record(record)


@mcp.tool(annotations=_APOLLO_ACTION)
async def enrich_organization(
    apollo_id: str,
    include_raw: bool = False,
    session_token: str | None = None,
    ctx: Context = None,
) -> dict[str, Any]:
    """Apollo Complete Organization Info enrichment for one company (SPENDS
    APOLLO CREDITS). Upserts the stored lead and returns the refreshed record."""
    record = await search_backend.request(
        "POST",
        f"/api/search/organizations/enrich/{apollo_id}",
        json_body={},
        token=_token(session_token, ctx),
    )
    if include_raw or not isinstance(record, dict):
        return record
    return slim_record(record)


@mcp.tool(annotations=_APOLLO_ACTION)
async def match_person(
    apollo_id: str,
    run_waterfall_email: bool = False,
    run_waterfall_phone: bool = False,
    reveal_phone_number: bool = False,
    session_token: str | None = None,
    ctx: Context = None,
) -> dict[str, Any]:
    """Apollo people/match — reveal contact info for one person (SPENDS APOLLO
    CREDITS; waterfall/phone reveal costs extra). Email usually lands in the
    returned record immediately; phone numbers arrive asynchronously via the
    Apollo webhook — re-fetch the lead (get_lead) after a minute if
    phone_reveal_pending is true."""
    body: dict[str, Any] = {}
    if run_waterfall_email:
        body["run_waterfall_email"] = True
    if run_waterfall_phone:
        body["run_waterfall_phone"] = True
    if reveal_phone_number:
        body["reveal_phone_number"] = True
    data = await search_backend.request(
        "POST",
        f"/api/search/people/match/{apollo_id}",
        json_body=body,
        token=_token(session_token, ctx),
    )
    if not isinstance(data, dict):
        return data
    lead = data.get("lead")
    return {
        "lead": slim_record(lead) if isinstance(lead, dict) else lead,
        "phone_reveal_pending": data.get("phone_reveal_pending"),
        "waterfall_pending": data.get("waterfall_pending"),
    }


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "mcp"})


# Served by uvicorn (see Dockerfile); streamable HTTP endpoint mounts at /mcp.
app = mcp.streamable_http_app()
