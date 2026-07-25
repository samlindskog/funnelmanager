"""Audience-parameterized RFC 8693 token exchange for the MCP server.

Every tool call carries the acting principal's token (the ``session_token``
argument or the ``Authorization: Bearer`` header on the ``/mcp`` request). It is
the *subject* token: for each upstream hop the resolver exchanges it (RFC 8693,
cached by fm_runtime's ``TokenBroker``) for a token whose ``aud`` names that
specific upstream — ``leads``, ``search`` or ``jobs`` — via the corresponding
``mcp->{audience}`` svc scope. **Exchange, never forward.**

Replaces the leads-hardcoded resolver: ``resolve(audience, subject)`` is the one
entry point, so adding an upstream is just a new ``BackendClient`` with its
audience — no new resolver.

``fm_origin`` propagation is Keycloak-native: the exchange mapper preserves the
inbound subject token's ``fm_origin`` claim across the hop (so an
agent-initiated call stays ``fm_origin=agent`` downstream). We additionally fold
the resolved origin into the broker's cache key (``resolve_origin``) so an
agent-context and a user-context token for the same subject+audience never
collide in cache.

With ``MCP_SHARED_LOGIN_FALLBACK=true`` (dev only), tokenless calls act as the
MCP server's own service identity via a client-credentials token instead.
"""

from __future__ import annotations

from fm_runtime import ExchangeError, current_principal, get_broker, resolve_origin
from fm_runtime.settings import get_runtime_settings

from app.config import Settings

# Surfaced to MCP clients on a missing/rejected/forbidden token.
MISSING_TOKEN_MESSAGE = (
    "No token was provided for this tool call. Pass session_token explicitly "
    "or send an Authorization: Bearer header on the MCP request. Tokens "
    "expire — fetch a fresh one if this keeps failing."
)


class UpstreamError(RuntimeError):
    """Raised when a token exchange or backend call fails; the message is
    surfaced to the MCP client."""


class TokenResolver:
    """Audience-parameterized subject-token exchange.

    ``resolve(audience, explicit)`` returns the *outbound* bearer for that
    audience: the exchanged per-call subject token, or (dev fallback only) the
    server's own service identity via client credentials.
    """

    def __init__(self, settings: Settings):
        self._settings = settings

    async def resolve(self, audience: str, explicit: str | None) -> str:
        subject = (explicit or "").strip() or None
        if subject is None and not self._settings.mcp_shared_login_fallback:
            raise UpstreamError(MISSING_TOKEN_MESSAGE)
        # Cache-key dimension only; the wire fm_origin claim is set by
        # Keycloak's exchange mapper, not by us.
        origin = resolve_origin(current_principal(), get_runtime_settings())
        try:
            # subject=None -> client-credentials (service identity, dev only).
            return await get_broker().token_for(audience, subject, origin=origin)
        except ExchangeError as exc:
            raise UpstreamError(f"Token exchange failed: {exc}") from exc
