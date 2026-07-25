"""Generic HTTP client for an internal backend.

One ``BackendClient(base_url, audience)`` is created per upstream (leads, search,
jobs). It resolves the per-call token to the client's audience via
``TokenResolver.resolve(audience, subject)`` and issues the request with the
exchanged bearer. Adding an upstream is a new instance + a ``mcp->{audience}``
svc scope — no client subclass.
"""

from __future__ import annotations

from typing import Any

import httpx

from fm_runtime.context import current_trace_headers

from app.tokens import TokenResolver, UpstreamError

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


class BackendClient:
    """Calls one internal backend with an exchanged (per-hop-audience) bearer."""

    def __init__(self, name: str, base_url: str, audience: str, tokens: TokenResolver):
        self._name = name
        self._base_url = base_url
        self._audience = audience
        self._tokens = tokens

    def _url(self, path: str) -> str:
        base = self._base_url.rstrip("/")
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
        bearer = await self._tokens.resolve(self._audience, token)
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
        context = f"{self._name} backend {method} {path}"
        _raise_for_auth(response, context)
        if response.status_code >= 400:
            raise UpstreamError(
                f"{context} failed ({response.status_code}): {_error_detail(response)}"
            )
        if response.status_code == 204:
            return None
        return response.json()
