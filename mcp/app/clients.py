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

# HTTP 409 detail.error values the Principle-4 expensive-action gate raises
# (fm_runtime.confirmation): the human "re-invoke with confirm=true" gate and
# the agent "a human must approve out of band" gate. See _gate_payload.
_GATE_ERRORS = {"confirmation_required", "human_approval_required"}


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


def _gate_payload(response: httpx.Response) -> dict[str, Any] | None:
    """The structured Principle-4 gate detail if this is a gate 409, else None.

    An expensive action a service guards (a large campaign, a >2 GB mailbox
    backup, a huge search) returns HTTP 409 with a structured body — either
    ``confirmation_required`` (a human re-invokes with ``confirm=true``) or, for
    an agent-initiated over-threshold action, ``human_approval_required`` /
    ``needs_human_approval`` (a human must approve out of band; an agent must
    NOT self-confirm). This is a control-flow signal, **not** a backend error:
    the tool must surface it faithfully so the caller (the runtime agent, via
    the agents service) pauses instead of retrying or swallowing it. Only
    gate-shaped 409s pass through; any other 409 stays a fail-loud error.
    """
    if response.status_code != 409:
        return None
    try:
        payload = response.json()
    except Exception:
        return None
    detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
    if not isinstance(detail, dict):
        return None
    if detail.get("needs_human_approval") or detail.get("error") in _GATE_ERRORS:
        return detail
    return None


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
        # Principle-4 gate (confirmation_required / needs_human_approval): a
        # control-flow signal, not an error — surface it faithfully instead of
        # raising, so the caller pauses rather than retrying/auto-confirming.
        gate = _gate_payload(response)
        if gate is not None:
            return gate
        if response.status_code >= 400:
            raise UpstreamError(
                f"{context} failed ({response.status_code}): {_error_detail(response)}"
            )
        if response.status_code == 204:
            return None
        return response.json()
