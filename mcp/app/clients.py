"""HTTP client for the leads backend.

Every tool call carries the acting principal's token (the `session_token`
argument or the Authorization header on the /mcp request). It is the
*subject* token: fm_runtime's TokenBroker exchanges it (RFC 8693, cached) for
a leads-audience token on every upstream call, so leads sees the original
principal with this server in the `act` chain.

With MCP_SHARED_LOGIN_FALLBACK=true (dev only), tokenless calls act as the
MCP server's own service identity via a client-credentials token instead.
"""

from __future__ import annotations

from typing import Any

import httpx

from fm_runtime import ExchangeError, get_broker
from fm_runtime.context import current_trace_headers

from app.config import Settings

LEADS_AUDIENCE = "leads"


class UpstreamError(RuntimeError):
    """Raised when a backend call fails; message is surfaced to the MCP client."""


MISSING_TOKEN_MESSAGE = (
    "No token was provided for this tool call. Pass session_token explicitly "
    "or send an Authorization: Bearer header on the MCP request. Tokens "
    "expire — fetch a fresh one if this keeps failing."
)

INVALID_TOKEN_MESSAGE = (
    "The token was rejected (expired or revoked). Fetch a fresh one and retry."
)

FORBIDDEN_MESSAGE = (
    "The principal behind this token is not authorized for this action (the "
    "policy denied it). An admin can adjust the principal's permissions."
)


def _error_detail(response: httpx.Response) -> Any:
    try:
        payload = response.json()
        return payload.get("detail", payload)
    except Exception:
        return response.text


def _raise_for_auth(response: httpx.Response, context: str) -> None:
    if response.status_code == 401:
        raise UpstreamError(f"{context}: {INVALID_TOKEN_MESSAGE}")
    if response.status_code == 403:
        raise UpstreamError(f"{context}: {FORBIDDEN_MESSAGE}")


class TokenResolver:
    """Explicit per-call subject token, else (dev fallback only) the server's
    own service identity via client credentials."""

    def __init__(self, settings: Settings):
        self._settings = settings

    async def resolve(self, explicit: str | None) -> str:
        """Returns the *outbound* leads-audience bearer."""
        subject = (explicit or "").strip() or None
        if subject is None and not self._settings.mcp_shared_login_fallback:
            raise UpstreamError(MISSING_TOKEN_MESSAGE)
        try:
            # subject=None -> client-credentials (service identity, dev only).
            return await get_broker().token_for(LEADS_AUDIENCE, subject)
        except ExchangeError as exc:
            raise UpstreamError(f"Token exchange failed: {exc}") from exc


class LeadsBackendClient:
    """Calls the internal leads backend with an exchanged bearer token."""

    def __init__(self, settings: Settings, tokens: TokenResolver):
        self._settings = settings
        self._tokens = tokens

    def _url(self, path: str) -> str:
        base = self._settings.leads_backend_url.rstrip("/")
        return f"{base}/{path.lstrip('/')}"

    async def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | list[Any] | None = None,
    ) -> Any:
        bearer = await self._tokens.resolve(token)
        headers = current_trace_headers()
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.request(
                method,
                self._url(path),
                params=params,
                json=json_body,
                headers=headers,
            )
        _raise_for_auth(response, f"Leads backend {method} {path}")
        if response.status_code >= 400:
            raise UpstreamError(
                f"Leads backend {method} {path} failed "
                f"({response.status_code}): {_error_detail(response)}"
            )
        if response.status_code == 204:
            return None
        return response.json()
