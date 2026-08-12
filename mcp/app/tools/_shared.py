"""Shared helpers for tool modules — per-call token extraction.

Every tool takes a ``session_token`` argument, but the token may also ride on
the ``Authorization: Bearer`` header of the ``/mcp`` HTTP request. The explicit
argument wins (see ``effective_token``). The resolved token is the acting
principal's; the ``BackendClient`` exchanges it per upstream.
"""

from __future__ import annotations

from typing import Any

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


def build_similarity_body(
    query: str | None,
    limit: int,
    embeds: list[str] | None,
    company_id: str | None,
    company_ids: list[str] | None,
    email_exists: bool | None,
    phone_exists: bool | None,
    linkedin_exists: bool | None,
    entity_type: str | None,
) -> dict[str, Any]:
    """Build the similarity-search request body shared by the leads
    ``similarity_search`` and search ``start_semantic_search`` tools.

    Identical wire contract for both callers: ``limit`` is coerced to int and
    clamped to [1, 10000]; every other field is omitted when None. Note
    ``embeds=[]`` is *not* None, so it is forwarded — that is how pure-filter
    mode is selected.
    """
    body: dict[str, Any] = {"limit": max(1, min(int(limit), 10000))}
    for key, value in (
        ("query", query),
        ("embeds", embeds),
        ("company_id", company_id),
        ("company_ids", company_ids),
        ("entity_type", entity_type),
        ("email_exists", email_exists),
        ("phone_exists", phone_exists),
        ("linkedin_exists", linkedin_exists),
    ):
        if value is not None:
            body[key] = value
    return body
