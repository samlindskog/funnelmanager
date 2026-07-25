"""Mail tools — over the mail backend's MCP surface (``/api/mail/mcp/v1/*``).

Exchange edge: ``mcp->mail``. These let a runtime agent run campaigns and operate
the mailbox as a normal inbox. Cross-user by design (principle 1): campaigns and
inboxes are attribute-filtered, never access-gated, in app code — Keycloak
(audience + role) is the only gate. Paths/shapes mirror mail's dedicated MCP
router exactly (distinct from the UI routes, Principle 2).

Read tools (``list_campaigns``/``get_campaign``/``contacted_contacts``/
``list_messages``/``get_thread``) are annotated ``readOnlyHint``. Write tools
(``start_campaign``/``continue_campaign``/``send_message``) carry accurate hints:
they launch/append/send, never delete (not destructive) and are not idempotent.

Principle-4 gate (critical): launching a large campaign is an expensive action.
When the initiating principal is an **agent** and the action is over the
recipient threshold, mail returns a ``needs_human_approval`` gate response (HTTP
409, surfaced as a structured payload by ``BackendClient``). ``start_campaign``
returns that payload **verbatim** — it never retries, never auto-sets
``confirm=true`` (an agent cannot self-confirm) — so the agents service can pause
the run and escalate to the initiating human. A human/UI drives the ordinary
path with ``confirm=true`` (+ ``confirm_token``); an approved agent action
resumes by passing the minted ``human_approval`` token. All three are opaque
pass-throughs here; MCP does not mint or self-confirm.

Invariant: MCP never calls Apollo — mail does not touch Apollo at all.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from app.deps import Deps
from app.tools._shared import effective_token

_MCP = "/api/mail/mcp/v1"

_READ_ONLY = ToolAnnotations(readOnlyHint=True)
# Launches/appends/sends but never deletes, and is not safe to blindly repeat
# (each call starts a campaign, adds recipients, or sends a fresh message).
_WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False
)


def register(mcp: FastMCP, deps: Deps) -> None:
    mail = deps.mail

    # ---- campaigns (read) ------------------------------------------------

    @mcp.tool(annotations=_READ_ONLY)
    async def list_campaigns(
        username: str | None = None,
        status: str | None = None,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> list[dict[str, Any]]:
        """List campaigns, newest first (cross-user by design). Optional filters:
        username (initiating owner) and status. Each row records owner/origin/actor
        so a UI can render "alice" or "alice (via agent)". Returns up to 500."""
        params: dict[str, Any] = {}
        if username is not None:
            params["username"] = username
        if status is not None:
            params["status"] = status
        return await mail.request(
            "GET",
            f"{_MCP}/campaigns",
            params=params or None,
            token=effective_token(session_token, ctx),
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def get_campaign(
        campaign_id: int,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Get one campaign with its status, send strategy/throttle, source searches
        and send stats (recipients/sent/suppressed/failed). Cross-user visible."""
        return await mail.request(
            "GET",
            f"{_MCP}/campaigns/{int(campaign_id)}",
            token=effective_token(session_token, ctx),
        )

    # ---- campaigns (write) -----------------------------------------------

    @mcp.tool(annotations=_WRITE)
    async def start_campaign(
        campaign_id: int,
        confirm: bool = False,
        confirm_token: str | None = None,
        human_approval: str | None = None,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Launch an existing campaign (identified by campaign_id) — it begins
        sending from the initiating user's own connected mailboxes per its send
        strategy, with within-campaign dedupe, cross-campaign suppression and the
        per-domain daily cap enforced server-side at send time. WRITE, not
        idempotent.

        Principle-4 gate: over the recipient threshold this returns a structured
        gate response instead of starting. If YOU are a runtime agent and the
        response has needs_human_approval=true, STOP — do NOT retry and do NOT set
        confirm=true (an agent cannot self-confirm); surface it so a human approves
        out of band, then resume by passing the human_approval token the agents
        service mints. confirm=true (+ confirm_token) is only for a human/UI
        re-invoking after a confirmation_required response."""
        params: dict[str, Any] = {"confirm": confirm}
        if confirm_token is not None:
            params["confirm_token"] = confirm_token
        if human_approval is not None:
            params["human_approval"] = human_approval
        return await mail.request(
            "POST",
            f"{_MCP}/campaigns/{int(campaign_id)}/start",
            params=params,
            token=effective_token(session_token, ctx),
        )

    @mcp.tool(annotations=_WRITE)
    async def continue_campaign(
        campaign_id: int,
        search_id: str = "",
        label: str = "",
        recipients: list[dict[str, Any]] | None = None,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Continue a campaign by adding another search's recipients. Provide the
        source's search_id and/or an explicit recipients list (each: email, plus
        optional apollo_id and name). Dedupe and cross-campaign suppression are
        re-applied, so no one already messaged is re-added. Returns how the batch
        landed (submitted/added/duplicate_in_campaign/suppressed). WRITE, not
        idempotent."""
        clean: list[dict[str, Any]] = []
        for r in recipients or []:
            email = str(r.get("email", "")).strip()
            if not email:
                continue
            clean.append(
                {
                    "email": email,
                    "apollo_id": str(r.get("apollo_id", "")),
                    "name": str(r.get("name", "")),
                }
            )
        body = {"search_id": search_id, "label": label, "recipients": clean}
        return await mail.request(
            "POST",
            f"{_MCP}/campaigns/{int(campaign_id)}/continue",
            json_body=body,
            token=effective_token(session_token, ctx),
        )

    # ---- contacted set (read) --------------------------------------------

    @mcp.tool(annotations=_READ_ONLY)
    async def contacted_contacts(
        campaign_id: int | None = None,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """List the contacted set — people already messaged (emails, with
        last_contacted + the campaign_ids that reached them), the basis for
        cross-campaign suppression and search's exclude_contacted filter. Optionally
        scope to a single campaign_id; omitted = across all campaigns. Read-only."""
        params = {"campaign_id": int(campaign_id)} if campaign_id is not None else None
        return await mail.request(
            "GET",
            f"{_MCP}/contacts/contacted",
            params=params,
            token=effective_token(session_token, ctx),
        )

    # ---- mailbox (read) --------------------------------------------------

    @mcp.tool(annotations=_READ_ONLY)
    async def list_messages(
        account_id: int | None = None,
        label: str = "INBOX",
        q: str = "",
        page: int = 1,
        per_page: int = 50,
        include_deleted: bool = False,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """List archived messages across the connected mailboxes (cross-user; all
        inboxes to all users). account_id narrows to one mailbox (omit for all);
        label selects the folder (e.g. INBOX, SENT); q is a full-text query over the
        archive. Paginated, newest first. Read-only — served from the Postgres
        archive."""
        params: dict[str, Any] = {
            "label": label,
            "q": q,
            "page": max(1, int(page)),
            "per_page": max(1, min(int(per_page), 200)),
            "include_deleted": include_deleted,
        }
        if account_id is not None:
            params["account_id"] = int(account_id)
        return await mail.request(
            "GET",
            f"{_MCP}/messages",
            params=params,
            token=effective_token(session_token, ctx),
        )

    @mcp.tool(annotations=_READ_ONLY)
    async def get_thread(
        account_id: int,
        thread_id: str,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Get a full conversation thread on one mailbox (account_id) — every
        archived message in it, ordered, with bodies. Cross-user visible.
        Read-only."""
        return await mail.request(
            "GET",
            f"{_MCP}/accounts/{int(account_id)}/threads/{thread_id}",
            token=effective_token(session_token, ctx),
        )

    # ---- mailbox (write) -------------------------------------------------

    @mcp.tool(annotations=_WRITE)
    async def send_message(
        account_id: int,
        to: list[str],
        subject: str = "",
        body_text: str = "",
        body_html: str = "",
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to_message_id: int | None = None,
        session_token: str | None = None,
        ctx: Context = None,
    ) -> dict[str, Any]:
        """Send an email from one of the initiating user's connected mailboxes
        (account_id) via the Gmail API. Provide body_text and/or body_html. Set
        reply_to_message_id to the id of a stored message on the SAME account to
        reply within its thread. WRITE and NOT idempotent — every call sends a new
        message. This is a normal one-off send (not a campaign); it does not apply
        campaign suppression."""
        payload: dict[str, Any] = {
            "to": [str(r).strip() for r in to if str(r or "").strip()],
            "cc": [str(r).strip() for r in (cc or []) if str(r or "").strip()],
            "bcc": [str(r).strip() for r in (bcc or []) if str(r or "").strip()],
            "subject": subject,
            "body_text": body_text,
            "body_html": body_html,
        }
        if reply_to_message_id is not None:
            payload["reply_to_message_id"] = int(reply_to_message_id)
        return await mail.request(
            "POST",
            f"{_MCP}/accounts/{int(account_id)}/send",
            json_body=payload,
            token=effective_token(session_token, ctx),
        )
