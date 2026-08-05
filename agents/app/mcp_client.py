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
- Tokens are short-lived and a turn may outlive one, so we exchange **per HTTP
  request** through the shared ``TokenBroker`` (cached per subject+audience+origin)
  rather than pinning one static header.

DOWNGRADE POSTURE — fix #9 (drift): a detached turn may fall back to the agents
service's own identity **only after the human subject token GENUINELY EXPIRES
mid-turn** — never on a *transient* ``ExchangeError`` (a Keycloak/network blip).
The old code flipped a permanent downgrade on ANY ``ExchangeError``, so one blip
irreversibly dropped the human subject for the rest of the turn. Now we downgrade
only when the subject token's own ``exp`` has actually passed; a transient failure
re-raises (the still-valid human token is retried on the next call). The fallback
is leads-only-equivalent (service-scoped), like every other detached job, and
stays ``fm_origin=agent``.

P10 NOTE (flag for security-reviewer): distinguishing "genuine expiry" from a
"transient blip" is done here with a local ``exp`` check because ``fm_runtime``'s
``ExchangeError`` does not carry that distinction. The lifecycle *decision* ideally
moves behind an ``fm_runtime`` helper (drift #20) — out of scope for this phase;
the ``exp`` read below is a time check on the captured subject token, not a
re-implementation of authorization (which stays platform-enforced).
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from typing import TYPE_CHECKING

import httpx
from fm_runtime import ExchangeError, TokenBroker, get_broker
from pydantic_ai.mcp import MCPToolset, StreamableHttpTransport

from app.config import Settings

if TYPE_CHECKING:
    from pydantic_ai.mcp import ProcessToolCallback

logger = logging.getLogger(__name__)

# The MCP server's audience — the token we present to it must name this.
MCP_AUDIENCE = "mcp"

# Treat the subject token as expired this many seconds BEFORE its real ``exp`` so
# we never race the deadline mid-exchange.
_EXP_SKEW_SECONDS = 5.0


def _jwt_exp(token: str) -> float | None:
    """Best-effort read of a JWT's ``exp`` (unix seconds), or None if unreadable.

    A plain time check on the captured subject token — NOT signature verification
    (Keycloak already validated it when it was minted, and would refuse to
    exchange a forged one). Returns None on any parse failure so the caller treats
    an unreadable token as *not provably expired* (transient), never silently
    downgrading the human subject on a token it cannot reason about.
    """
    try:
        payload_b64 = token.split(".")[1]
        padding = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))
        exp = payload.get("exp")
        return float(exp) if exp is not None else None
    except (IndexError, ValueError, binascii.Error, json.JSONDecodeError, TypeError):
        return None


class _AgentExchangeAuth(httpx.Auth):
    """httpx auth injecting a freshly-exchanged mcp-audience token on every
    request, keeping the human as subject and ``fm_origin=agent``.

    Downgrades to the agents service identity ONLY after the captured human
    subject token's ``exp`` has genuinely passed (see module docstring, fix #9)."""

    def __init__(self, broker: TokenBroker, subject_token: str | None, origin: str) -> None:
        self._broker = broker
        self._subject_token = subject_token
        self._origin = origin
        self._downgraded = False

    def _subject_expired(self) -> bool:
        if not self._subject_token:
            return True
        exp = _jwt_exp(self._subject_token)
        if exp is None:
            return False  # unreadable -> not provably expired -> transient
        return time.time() >= (exp - _EXP_SKEW_SECONDS)

    async def _token(self) -> str:
        if self._subject_token and not self._downgraded:
            try:
                return await self._broker.token_for(
                    MCP_AUDIENCE, self._subject_token, origin=self._origin
                )
            except ExchangeError as exc:
                if self._subject_expired():
                    # GENUINE expiry: permanently downgrade to the service identity
                    # (leads-only-equivalent) for the rest of the detached turn.
                    logger.warning(
                        "agents: human subject token expired mid-turn (%s); "
                        "downgrading this turn to the agents service identity",
                        exc,
                    )
                    self._downgraded = True
                else:
                    # TRANSIENT blip while a still-valid human token exists: do NOT
                    # downgrade. Re-raise so this MCP call fails transiently and the
                    # human subject is retried next call — never silently act as the
                    # service while the human token is still good.
                    logger.warning(
                        "agents: transient token-exchange failure for mcp (%s); "
                        "keeping the human subject (no downgrade)",
                        exc,
                    )
                    raise
        # Service identity (client-credentials); still fm_origin=agent because the
        # agents Keycloak client mints it.
        return await self._broker.token_for(MCP_AUDIENCE, None, origin=self._origin)

    async def async_auth_flow(self, request: httpx.Request):  # type: ignore[override]
        token = await self._token()
        if token:
            request.headers["Authorization"] = f"Bearer {token}"
        yield request


def build_mcp_toolset(
    settings: Settings,
    *,
    subject_token: str | None,
    origin: str,
    process_tool_call: "ProcessToolCallback | None" = None,
) -> MCPToolset:
    """Build the pydantic-ai toolset backed by the internal MCP server.

    The whole tool surface (search/leads/jobs/mail) is discovered from the MCP
    server at connect time — the agent's capability + situational-awareness
    surface — and every call authenticates via ``_AgentExchangeAuth``.

    ``process_tool_call`` (the runner's) wraps every tool call to enforce the
    Principle-4 human-approval gate.

    FIX #31 (CONFIRMED security — structurally un-bypassable gate):
    ``tool_error_behavior`` is set **explicitly** rather than left at the SDK
    default ``'retry'``. The default turns a failing tool call into a
    ``ModelRetry`` the LLM can loop on — so if the P4 approval gate ever surfaced
    as a *tool error* (not a structured result), the model would be fed it and
    could retry **past** the human approval (a silent bypass). Two guarantees make
    that impossible: (1) the MCP server returns the gate as a **structured tool
    *result*** (``needs_human_approval``) which ``process_tool_call`` intercepts
    and routes to the pause path *before* any error branch — see
    ``runner._extract_approval_gate``; (2) here we set
    ``tool_error_behavior='failed'`` so even a genuine tool *error* becomes a
    single, non-retried ``ToolFailed`` result surfaced to the model (visible, not
    hidden, and NOT an automatic retry loop) — the gate can never appear as a
    retryable ``ModelRetry``. Flag for security-reviewer on any change here.
    """
    auth = _AgentExchangeAuth(get_broker(), subject_token, origin)
    transport = StreamableHttpTransport(url=settings.mcp_url, auth=auth)
    return MCPToolset(
        transport,
        id="mcp",
        process_tool_call=process_tool_call,
        tool_error_behavior="failed",
    )


__all__ = ["MCP_AUDIENCE", "build_mcp_toolset"]
