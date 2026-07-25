"""MCP-facing mail API — ``/api/mail/mcp/v1/*``.

Deliberately **distinct handlers from the UI routes** (Principle 2: MCP uses
dedicated endpoints, never the ones the browser calls) and **versioned**
(``/v1``, additive-only within the version). Callable by the ``mcp`` service over
the ``mcp->mail`` exchange edge; the acting human stays the token subject, so
records persist with the correct owner/origin/actor and stay cross-user visible.

Auth is the standard principal flow (audience ``mail``) via ``get_current_user`` —
no new ``@anonymous`` routes. An agent-initiated campaign start still hits the
same Principle-4 gate as the UI: because the principal carries ``fm_origin=agent``,
``confirm=true`` cannot self-approve — a human ``human_approval`` token is required.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import campaigns
from app.auth import get_current_user
from app.config import Settings, get_settings
from app.database import get_db
from app.models import Campaign
from app.routers.mail import (
    _get_account,
    _get_campaign,
    _message_conditions,
    _message_page,
    _thread,
    do_send,
    gate_and_add_source,
    gate_and_start_campaign,
)
from app.schemas import (
    CampaignOut,
    CampaignSourceIn,
    ContactedOut,
    ContactOut,
    MessageDetail,
    MessagePage,
    SendRequest,
    SourceMergeOut,
    ThreadOut,
    UserOut,
)

router = APIRouter(prefix="/api/mail/mcp/v1", tags=["mail-mcp"])


# --- campaigns -------------------------------------------------------------


@router.get("/campaigns", response_model=list[CampaignOut])
async def mcp_list_campaigns(
    username: str | None = Query(None),
    status: str | None = Query(None),
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CampaignOut]:
    stmt = select(Campaign).order_by(Campaign.created_at.desc()).limit(500)
    if username is not None:
        stmt = stmt.where(Campaign.owner == username)
    if status is not None:
        stmt = stmt.where(Campaign.status == status)
    rows = (await db.execute(stmt)).scalars().all()
    return [await campaigns.serialize_campaign(db, campaign) for campaign in rows]


@router.get("/campaigns/{campaign_id}", response_model=CampaignOut)
async def mcp_get_campaign(
    campaign_id: int,
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CampaignOut:
    campaign = await _get_campaign(db, campaign_id)
    return await campaigns.serialize_campaign(db, campaign)


@router.post("/campaigns/{campaign_id}/start", response_model=CampaignOut)
async def mcp_start_campaign(
    campaign_id: int,
    confirm: bool = Query(False),
    confirm_token: str | None = Query(None),
    human_approval: str | None = Query(None),
    user: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CampaignOut:
    """Start (launch) a campaign. Over the recipient threshold this raises the
    Principle-4 gate; for an agent caller ``confirm=true`` is ignored and a
    human_approval token is the only lever that lets it run."""
    campaign = await _get_campaign(db, campaign_id)
    campaign = await gate_and_start_campaign(
        db,
        user,
        campaign,
        confirm=confirm,
        confirm_token=confirm_token,
        human_approval=human_approval,
    )
    return await campaigns.serialize_campaign(db, campaign)


@router.post("/campaigns/{campaign_id}/continue", response_model=SourceMergeOut)
async def mcp_continue_campaign(
    campaign_id: int,
    body: CampaignSourceIn,
    confirm: bool = Query(False),
    confirm_token: str | None = Query(None),
    human_approval: str | None = Query(None),
    user: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SourceMergeOut:
    """Continue a campaign by adding another search's results (dedupe +
    suppression re-applied). Gated on the campaign's cumulative send size
    (Principle 4): an agent caller that would grow the campaign past the
    threshold cannot self-confirm — ``confirm=true`` is ignored and only a
    human_approval token lets it proceed."""
    campaign = await _get_campaign(db, campaign_id)
    source, counts = await gate_and_add_source(
        db,
        user,
        campaign,
        search_id=body.search_id,
        label=body.label,
        recipients=[r.model_dump() for r in body.recipients],
        confirm=confirm,
        confirm_token=confirm_token,
        human_approval=human_approval,
    )
    return SourceMergeOut(source_id=source.id, **counts)


@router.get("/contacts/contacted", response_model=ContactedOut)
async def mcp_contacted(
    campaign_id: int | None = Query(None),
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ContactedOut:
    contacts = await campaigns.contacted_contacts(db, campaign_id)
    return ContactedOut(
        emails=[c["email"] for c in contacts],
        contacts=[
            ContactOut(
                email=c["email"],
                last_contacted=c["last_contacted"],
                campaign_ids=c["campaign_ids"],
            )
            for c in contacts
        ],
    )


# --- inbox -----------------------------------------------------------------


@router.get("/messages", response_model=MessagePage)
async def mcp_list_messages(
    account_id: int | None = Query(None, description="One mailbox, or omit for all"),
    label: str = Query("INBOX"),
    q: str = Query("", max_length=256),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    include_deleted: bool = Query(False),
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MessagePage:
    if account_id is not None:
        await _get_account(db, account_id)
    conditions = _message_conditions(account_id, label, q, include_deleted)
    return await _message_page(db, conditions, page, per_page)


@router.get("/accounts/{account_id}/threads/{thread_id}", response_model=ThreadOut)
async def mcp_get_thread(
    account_id: int,
    thread_id: str,
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ThreadOut:
    return await _thread(db, account_id, thread_id)


@router.post("/accounts/{account_id}/send", response_model=MessageDetail)
async def mcp_send_message(
    account_id: int,
    body: SendRequest,
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MessageDetail:
    account = await _get_account(db, account_id)
    return await do_send(db, settings, account, body)
