"""Internal MCP server for Funnel Manager.

The tool surface for every runtime AI agent, exposed over MCP streamable HTTP
(endpoint ``/mcp``). Runs only on the compose network — nginx never routes to
it (prod publishes it on loopback only).

Modular, multi-audience architecture (one tool module + one client + one
``mcp->{svc}`` svc scope per upstream):
  - ``app/tokens.py``    — ``resolve(audience, subject)`` RFC 8693 exchange.
  - ``app/clients.py``   — generic ``BackendClient(base_url, audience)``.
  - ``app/tools/*``      — ``register(mcp, deps)`` per service; ``register_all``.
  - ``app/main.py``      — wires deps (clients + resolver) + calls register_all.

Tools:
  - leads (read-only): leads_stats, recent_leads, get_leads, similarity_search.
  - search (via search's mcp/v1): start_apollo_search, start_semantic_search,
    enrich_leads (write) + list_searches, get_search, list_results,
    export_results.
  - jobs (via jobs' mcp/v1): list_jobs, get_job, pause_job, resume_job, cancel_job.
  - mail (via mail's mcp/v1): list_campaigns, get_campaign, contacted_contacts,
    list_messages, get_thread (read) + start_campaign, continue_campaign,
    send_message (write). An agent-initiated over-threshold write can return a
    needs_human_approval gate response — the tool surfaces it faithfully (never
    swallows or auto-confirms) so the agents service can pause the run.

Invariant: **MCP never calls Apollo directly** — Apollo goes through
search -> leads (the only Apollo holder).

Every tool call carries the acting principal's token and EXCHANGES it (never
forwards) per upstream audience. Priority order:
  1. the ``session_token`` tool argument (explicit arg wins),
  2. an ``Authorization: Bearer`` header on the MCP HTTP request,
  3. the optional dev fallback (MCP_SHARED_LOGIN_FALLBACK): act as the MCP
     server's own service identity via client credentials.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from fm_runtime import configure_logging
from fm_runtime.middleware import PrincipalMiddleware
from fm_runtime.settings import get_runtime_settings

from app.clients import BackendClient
from app.config import get_settings
from app.deps import Deps
from app.tokens import TokenResolver
from app.tools import register_all

settings = get_settings()
tokens = TokenResolver(settings)

# One client per upstream — each exchanges the per-call subject token for its own
# per-hop audience (svc scopes mcp->leads / mcp->search / mcp->jobs / mcp->mail).
deps = Deps(
    leads=BackendClient("leads", settings.leads_backend_url, "leads", tokens),
    search=BackendClient("search", settings.search_backend_url, "search", tokens),
    jobs=BackendClient("jobs", settings.jobs_backend_url, "jobs", tokens),
    mail=BackendClient("mail", settings.mail_backend_url, "mail", tokens),
)

mcp = FastMCP(
    "funnelmanager",
    instructions=(
        "Funnel Manager agent tool surface. LEADS tools (leads_stats, "
        "recent_leads, get_leads, similarity_search) are read-only and free — "
        "they inspect stored leads and never call Apollo. SEARCH tools start and "
        "inspect Apollo/semantic searches and enrichment (start_apollo_search, "
        "start_semantic_search, enrich_leads are writes; list_searches, "
        "get_search, list_results, export_results are reads) — Apollo is reached "
        "only through the search backend, never directly. JOBS tools (list_jobs, "
        "get_job, pause_job, resume_job, cancel_job) watch and control running "
        "work. MAIL tools run campaigns and operate the mailbox: list_campaigns, "
        "get_campaign, contacted_contacts, list_messages, get_thread are reads; "
        "start_campaign, continue_campaign, send_message are writes. If a mail "
        "write returns needs_human_approval (an agent-initiated action over the "
        "cost threshold), STOP: do not retry or auto-confirm — surface it so a "
        "human can approve out of band. AUTH: every tool call must pass "
        "session_token explicitly (or send "
        "it as an Authorization: Bearer header). Tokens expire: on an auth or "
        "policy error, fetch a fresh one and retry. Never invent a token."
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

register_all(mcp, deps)


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "mcp"})


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/readyz", methods=["GET"])
async def readyz(_: Request) -> JSONResponse:
    # Stateless server: ready as soon as it serves HTTP (upstream reachability
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
