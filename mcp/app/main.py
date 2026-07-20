"""Internal MCP server for Funnel Manager.

Exposes read-only inspection tools over MCP streamable HTTP (endpoint ``/mcp``)
for internal AI-agent clients. Runs only on the compose network — nginx never
routes to it. All tools read stored Apollo leads + enrichment state via
the leads backend; nothing here calls Apollo, spends credits, or touches the
search backend (searches are run by humans in the search app).

Every tool call must carry the acting principal's token, which this server
exchanges (RFC 8693) for a leads-audience token per upstream call. Priority
order:

1. the ``session_token`` tool argument (the agent's harness supplies the
   acting principal's token),
2. an ``Authorization: Bearer`` header on the MCP HTTP request,
3. the optional dev fallback (MCP_SHARED_LOGIN_FALLBACK): act as the MCP
   server's own service identity via client credentials.
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from fm_runtime import configure_logging
from fm_runtime.middleware import PrincipalMiddleware
from fm_runtime.settings import get_runtime_settings

from app.clients import LeadsBackendClient, TokenResolver
from app.config import get_settings
from app.summarize import summarize_lead

settings = get_settings()
tokens = TokenResolver(settings)
leads_backend = LeadsBackendClient(settings, tokens)

mcp = FastMCP(
    "funnelmanager",
    instructions=(
        "Funnel Manager (Apollo person/company inspector). All tools "
        "(leads_stats, recent_leads, get_leads, similarity_search) are "
        "read-only and free — they inspect leads already stored by the "
        "platform and never call Apollo or spend credits. New Apollo "
        "searches/enrichment happen in the Funnel Manager search app, not "
        "through this server. AUTH: every tool call must pass session_token "
        "explicitly (or send it as an Authorization: Bearer header). Tokens "
        "expire: on an auth or policy error, fetch a fresh one and retry. "
        "Never invent a token."
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


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "mcp"})


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/readyz", methods=["GET"])
async def readyz(_: Request) -> JSONResponse:
    # Stateless server: ready as soon as it serves HTTP (leads reachability
    # is surfaced per tool call, not here).
    return JSONResponse({"status": "ready"})


@mcp.custom_route("/metrics", methods=["GET"])
async def metrics(_: Request) -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


# Served by uvicorn (see Dockerfile); streamable HTTP endpoint mounts at /mcp.
app = mcp.streamable_http_app()

configure_logging("mcp", get_runtime_settings().log_level)
# The /mcp transport is principal-optional at the HTTP layer: tools receive
# the acting principal's token as a call argument (or the Authorization
# header, which this middleware parses into context when present). Probe and
# metrics endpoints are cluster-internal. Everything else 401s without a
# valid mcp-audience JWT.
app.add_middleware(
    PrincipalMiddleware,
    service="mcp",
    extra_anonymous=(
        r"^/mcp(/.*)?$",
        r"^/health$",
        r"^/healthz$",
        r"^/readyz$",
        r"^/metrics$",
    ),
)
