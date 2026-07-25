"""MCP client wiring — the runtime agent's ONLY capability surface.

A runtime AI agent acts **exclusively** through the internal MCP server
(``MCP_URL``): every search/leads/jobs/mail action is an MCP tool call. This
module builds the pydantic-ai toolset that exposes that MCP tool surface to the
LLM, and — critically — authenticates every MCP HTTP request as **the human,
via the agents service, with ``fm_origin=agent``**.

IDENTITY / EXCHANGE (security-review surface):
- The MCP server takes a per-call token whose ``aud`` is ``mcp`` and exchanges it
  onward (``mcp->leads`` / ``mcp->search`` / …) per upstream call. So the token we
  attach must be an **mcp-audience** token minted by RFC 8693 exchange of the
  **initiating human's** subject token (``agents->mcp`` svc scope). Keycloak keeps
  the human as subject (``preferred_username`` unchanged) and — because the agents
  client mints it — stamps ``fm_origin=agent``; the exchanging client rides in
  ``azp``. Anything MCP then persists downstream (a search) reads "alice (via
  agent)". We never invent a synthetic user.
- Tokens are short-lived and a run may outlive one, so we exchange **per HTTP
  request** through the shared ``TokenBroker`` (cached per subject+audience+origin;
  it re-exchanges when the cached token nears expiry) rather than pinning one
  static header. This is the detached-job pattern: while the captured human
  subject token is still valid we exchange it (subject = human); once it expires
  mid-run we **downgrade to the agents service's own client-credentials identity**
  (leads-only-equivalent, service-scoped) — never a different human.
"""

from __future__ import annotations

import logging

import httpx
from fm_runtime import ExchangeError, TokenBroker, get_broker
from pydantic_ai.mcp import MCPToolset, StreamableHttpTransport

from app.config import Settings

logger = logging.getLogger(__name__)

# The MCP server's audience — the token we present to it must name this.
MCP_AUDIENCE = "mcp"


class _AgentExchangeAuth(httpx.Auth):
    """httpx auth that injects a freshly-exchanged mcp-audience token on every
    request, keeping the human as subject and ``fm_origin=agent``.

    Downgrade posture (detached run): while the captured human subject token is
    accepted by the exchange we act as the human; the first time Keycloak refuses
    it (expired mid-run) we flip ``_downgraded`` and thereafter mint the agents
    service's own client-credentials token — the same detached fallback every
    long-running job uses. The origin stays ``agent`` throughout.
    """

    def __init__(self, broker: TokenBroker, subject_token: str | None, origin: str) -> None:
        self._broker = broker
        self._subject_token = subject_token
        self._origin = origin
        self._downgraded = False

    async def _token(self) -> str:
        if self._subject_token and not self._downgraded:
            try:
                return await self._broker.token_for(
                    MCP_AUDIENCE, self._subject_token, origin=self._origin
                )
            except ExchangeError as exc:
                # Human subject token no longer exchangeable (expired mid-run):
                # downgrade ONCE to the service identity and stay there.
                logger.warning(
                    "agents: human subject token exchange for mcp failed (%s); "
                    "downgrading this run to the agents service identity",
                    exc,
                )
                self._downgraded = True
        # Service identity (client-credentials); still fm_origin=agent because the
        # agents Keycloak client mints it.
        return await self._broker.token_for(MCP_AUDIENCE, None, origin=self._origin)

    async def async_auth_flow(self, request: httpx.Request):  # type: ignore[override]
        token = await self._token()
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request


def build_mcp_toolset(
    settings: Settings, *, subject_token: str | None, origin: str
) -> MCPToolset:
    """Build the pydantic-ai toolset backed by the internal MCP server.

    The whole tool surface (search/leads/jobs/mail) is discovered from the MCP
    server at connect time — the agent's capability + situational-awareness
    surface — and every call authenticates via ``_AgentExchangeAuth``.
    """
    auth = _AgentExchangeAuth(get_broker(), subject_token, origin)
    transport = StreamableHttpTransport(url=settings.mcp_url, auth=auth)
    # tool_error_behavior='retry' (default): a failing MCP tool call is surfaced
    # to the model as a retry prompt so the agent can adapt (e.g. re-plan after a
    # 409 confirmation_required) rather than crashing the whole run.
    return MCPToolset(transport, id="mcp")


__all__ = ["MCP_AUDIENCE", "build_mcp_toolset"]
