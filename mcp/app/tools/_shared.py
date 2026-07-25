"""Shared helpers for tool modules — per-call token extraction.

Every tool takes a ``session_token`` argument, but the token may also ride on
the ``Authorization: Bearer`` header of the ``/mcp`` HTTP request. The explicit
argument wins (see ``effective_token``). The resolved token is the acting
principal's; the ``BackendClient`` exchanges it per upstream.
"""

from __future__ import annotations

from mcp.server.fastmcp import Context

# Read-only tool annotation flag name kept alongside tool modules for reuse.


def _request_token(ctx: Context | None) -> str | None:
    """Bearer token from the MCP HTTP request's Authorization header, if any."""
    try:
        request = ctx.request_context.request if ctx is not None else None
    except (AttributeError, ValueError):
        return None
    if request is None:
        return None
    try:
        header = request.headers.get("authorization") or ""
    except AttributeError:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return value.strip() or None


def effective_token(session_token: str | None, ctx: Context | None) -> str | None:
    """Effective token for one tool call (explicit arg wins over HTTP header)."""
    return (session_token or "").strip() or _request_token(ctx)
