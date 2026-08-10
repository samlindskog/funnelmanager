"""Search tools — over the search backend's MCP surface (``/api/search/mcp/v1/*``).

Exchange edge: ``mcp->search``. These back the runtime agent's ability to *start*
and inspect work. **MCP never calls Apollo directly** — every Apollo-touching
tool funnels through the search backend, which reaches Apollo only via leads.

Read tools (``list_searches``/``get_search``/``list_results``/``export_results``)
are annotated ``readOnlyHint``. Write tools carry accurate hints:
``start_apollo_search``/``start_semantic_search``/``enrich_leads`` are not
read-only, not destructive (they create/upsert, never delete) and not idempotent
(each call starts new work / spends credits).
"""

from __future__ import annotations

from typing import Any, Literal

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from app.deps import Deps
from app.tools._shared import effective_token

_READ_ONLY = ToolAnnotations(readOnlyHint=True)
# Creates new work / mutates the store, but never deletes and is not safe to
# blindly repeat (each call starts a fresh search or spends Apollo credits).
_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False
)


def register(mcp: FastMCP, deps: Deps) -> None:
    search = deps.search

    @mcp.tool(annotations=_WRITE)
    async def start_apollo_search(
        query: str = "",
        entity_type: Literal["people", "companies"] = "people",
        per_page: int = 100,
        organization_id: str | None = None,
        organization_name: str | None = None,
        organization_display_name: str | None = None,
        organization_domain: str | None = None,
        company_name: str | None = None,
        company_domain: str | None = None,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Start an Apollo people/company search (via the search backend, which
        funnels through leads -> Apollo — this tool never touches Apollo directly).
        Returns the created search_id + leads stream handles; the search runs
        detached and is watchable/controllable via the jobs tools (ingest_stream_id
        doubles as the job id). People search needs query and/or a company
        (organization_id or organization_name); company search needs keywords
        (query), company_name and/or company_domain."""
        body: dict[str, Any] = {
            "entity_type": entity_type,
            "query": query,
            "per_page": max(1, min(int(per_page), 100)),
        }
        for key, value in (
            ("organization_id", organization_id),
            ("organization_name", organization_name),
            ("organization_display_name", organization_display_name),
            ("organization_domain", organization_domain),
            ("company_name", company_name),
            ("company_domain", company_domain),
        ):
            if value is not None:
                body[key] = value
        return await search.request(
            "POST",
            "/api/search/mcp/v1/searches/apollo",
            json_body=body,
            token=effective_token(session_token, ctx),
        )

    @mcp.tool(annotations=_WRITE)
    async def start_semantic_search(
        query: str | None = None,
        limit: int = 25,
        embeds: list[Literal["apollo", "name", "title"]] | None = None,
        company_id: str | None = None,
        entity_type: Literal["person", "organization"] | None = None,
        email_exists: bool | None = None,
        phone_exists: bool | None = None,
        linkedin_exists: bool | None = None,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Semantic (Milvus) search via the search backend; persists a
        cross-user-visible history row (hence a write). Synchronous — returns the
        scored results directly. To search leads WITHOUT recording history, use the
        leads similarity_search tool instead.

        Additive v2 params (omit any to keep exact legacy behavior):
        - embeds: which per-kind embeddings to rank by — any of "apollo" (the
          Apollo profile passage), "name", "title". The score is the mean
          similarity across the selected kinds a doc actually has. Omitted =>
          ["apollo"] (legacy). [] => pure-filter mode: no vector ranking (ranked
          by recency), query is ignored, and at least one filter is required.
        - company_id: accepts EITHER a company record's Mongo _id (the same value
          returned as `mongo_id` on that company's summary) OR the Apollo
          organization id (the `company_id` field on a person summary) — the two
          live in different id spaces and leads resolves whichever you pass. Keeps
          only people whose company is that org.
        - entity_type: restrict to "person" or "organization".
        - email_exists / phone_exists / linkedin_exists: tri-state contact-field
          filters (True=must have, False=must be missing, None=no filter).

        query is required whenever embeds is non-empty (the default); it may be
        omitted only in pure-filter mode (embeds=[]), where company_id,
        entity_type, or any contact filter each counts as the required filter. A
        history row is written in every mode — that is the contrast with the leads
        similarity_search tool, which records none."""
        body: dict[str, Any] = {"limit": max(1, min(int(limit), 10000))}
        if query is not None:
            body["query"] = query
        if embeds is not None:
            body["embeds"] = embeds
        if company_id is not None:
            body["company_id"] = company_id
        if entity_type is not None:
            body["entity_type"] = entity_type
        if email_exists is not None:
            body["email_exists"] = email_exists
        if phone_exists is not None:
            body["phone_exists"] = phone_exists
        if linkedin_exists is not None:
            body["linkedin_exists"] = linkedin_exists
        return await search.request(
            "POST",
            "/api/search/mcp/v1/searches/semantic",
            json_body=body,
            token=effective_token(session_token, ctx),
        )

    @mcp.tool(annotations=_WRITE)
    async def enrich_leads(
        ids: list[str],
        kind: Literal["person", "match", "organization"] = "person",
        run_waterfall_email: bool = False,
        run_waterfall_phone: bool = False,
        reveal_phone_number: bool = False,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Enrich one or more leads by Apollo id (via search -> leads -> Apollo).
        `kind` selects the operation: person (complete-person enrich), match
        (people/match waterfall for email/phone), or organization. Mutates the
        leads store and may spend Apollo credits — not idempotent. Returns the
        hydrated records + per-id errors. Up to 100 ids per call."""
        clean = [str(i).strip() for i in ids if str(i or "").strip()]
        body = {
            "ids": clean[:100],
            "kind": kind,
            "run_waterfall_email": run_waterfall_email,
            "run_waterfall_phone": run_waterfall_phone,
            "reveal_phone_number": reveal_phone_number,
        }
        return await search.request(
            "POST",
            "/api/search/mcp/v1/leads/enrich",
            json_body=body,
            token=effective_token(session_token, ctx),
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def list_searches(
        username: str | None = None,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> list[dict[str, Any]]:
        """List saved searches (cross-user by design; pass username to narrow to
        one owner). Newest first."""
        params = {"username": username} if username is not None else None
        return await search.request(
            "GET",
            "/api/search/mcp/v1/searches",
            params=params,
            token=effective_token(session_token, ctx),
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def get_search(
        search_id: int,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Get one search plus its current page of hydrated results (cross-user)."""
        return await search.request(
            "GET",
            f"/api/search/mcp/v1/searches/{int(search_id)}",
            token=effective_token(session_token, ctx),
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def list_results(
        search_id: int,
        limit: int = 200,
        offset: int = 0,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> list[dict[str, Any]]:
        """List a page of a search's stored results as compact records
        (mongo_id/name/email/entity_type), order preserved. Cross-user."""
        params = {"limit": max(1, int(limit)), "offset": max(0, int(offset))}
        return await search.request(
            "GET",
            f"/api/search/mcp/v1/searches/{int(search_id)}/results",
            params=params,
            token=effective_token(session_token, ctx),
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def export_results(
        search_id: int,
        exclude_contacted: bool = False,
        campaign_id: str | None = None,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Export a search's stored results as a record list (read-only — nothing
        is mutated). Set exclude_contacted=True to drop leads already messaged by a
        campaign (optionally scoped to campaign_id). NOTE: exclude_contacted is a
        graceful no-op until mail's Phase 5 contacted endpoint is available — the
        response's exclude_contacted_applied reports whether it actually ran."""
        body: dict[str, Any] = {"exclude_contacted": exclude_contacted}
        if campaign_id is not None:
            body["campaign_id"] = campaign_id
        return await search.request(
            "POST",
            f"/api/search/mcp/v1/searches/{int(search_id)}/export",
            json_body=body,
            token=effective_token(session_token, ctx),
        )
