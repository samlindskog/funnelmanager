"""Mail API: connected Google mailboxes, their synced messages, and sending.

Authorization is enforced by the mesh (Istio + OPA) plus fm_runtime's
principal middleware; the only anonymous routes are the OAuth callback
(annotated below — it authenticates with the single-use state row minted by
``GET /oauth/url``, since Google cannot send a bearer token) and the health
probes in ``main.py``.
"""

import asyncio
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, func, or_, select

from fm_runtime import anonymous, confirmation_threshold, require_confirmation
from sqlalchemy.ext.asyncio import AsyncSession

from app import campaigns, gmail
from app.auth import get_current_user
from app.config import Settings, get_settings
from app.database import SessionLocal, get_db
from app.gmail import GmailAuthError, GmailClient, GmailError
from app.models import (
    CAMPAIGN_CANCELLED,
    CAMPAIGN_COMPLETED,
    CAMPAIGN_PAUSED,
    CAMPAIGN_RUNNING,
    Campaign,
    MailAccount,
    MailMessage,
    MailOauthState,
)
from app.schemas import (
    AccountOut,
    AttachmentOut,
    BackupEstimateOut,
    BackupStartOut,
    CampaignCreate,
    CampaignOut,
    CampaignSettingsOut,
    CampaignSettingsUpdate,
    CampaignSourceIn,
    ContactedOut,
    ContactOut,
    MessageDetail,
    MessagePage,
    MessageSummary,
    OauthUrlOut,
    SendRequest,
    SourceMergeOut,
    SyncTriggerOut,
    ThreadOut,
    UserOut,
)
from app.campaigns import campaign_manager
from app.sync import apply_parsed, estimate_backup_bytes, sync_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mail", tags=["mail"])

_STATE_TTL = timedelta(minutes=10)
# Gmail label ids we accept as list filters (system labels + user label ids).
_LABEL_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
# Minimal well-formedness gate for a send recipient: local@domain.tld, no
# whitespace. Deliberately conservative — it only rejects addresses Gmail is
# certain to 400 (empty local/domain, no dot in domain); real delivery
# validation stays Gmail's job. Bare addresses only; the UI strips display names
# (extractEmail) before send.
_RECIPIENT_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _gmail_http_error(exc: "GmailError") -> HTTPException:
    """Map a GmailError to the right client-facing HTTPException.

    Gmail's own status class decides ours: a 4xx (bad recipient/MIME, quota,
    rate limit) is a caller/input condition — pass it through with Gmail's
    reason so the user learns WHY; only a 5xx (or a non-HTTP failure) is a true
    upstream 502. Never collapse a Gmail 4xx to 502 — that both mislabels an
    unfixable-by-retry input error as transient AND, being a 5xx, gets replaced
    by the edge's generic error page, hiding the reason.
    """
    code = exc.status_code
    # 401 and 403 are RESERVED by this platform's own layers and must never carry
    # an UPSTREAM (Gmail) meaning to a caller:
    #   * 401 — mailui's request() force-clears tokens + redirects to Keycloak on
    #     ANY 401 from /api/mail/* (a Gmail-side 401 would spuriously log the user
    #     out), and the MCP client treats 401 as "token expired, refetch + retry"
    #     — dangerous on the non-idempotent send path (a post-send get_message 401
    #     would retry and duplicate the email).
    #   * 403 — the MCP client maps ANY 403 to "FM policy denied; an admin can
    #     adjust permissions", which misdiagnoses a Gmail scope/quota 403.
    # So map both to 502 (matching the GmailAuthError branch). The other Gmail 4xx
    # DO carry a user-actionable, non-colliding reason and pass through: bad
    # recipient 400, not-found 404, conflict 409, unprocessable 422, rate-limit
    # 429. Only a genuine Gmail 5xx (or a non-HTTP GmailError) is a plain 502.
    if 400 <= code < 500 and code not in (401, 403):
        return HTTPException(status_code=code, detail=exc.detail)
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.detail)


def _backup_threshold() -> float:
    """Bytes above which a full mailbox backup needs confirmation (Principle 4)."""
    return confirmation_threshold(
        "MAIL_BACKUP_BYTES", get_settings().backup_confirm_bytes_default
    )


def _campaign_threshold() -> float:
    """Recipient count above which starting a campaign needs confirmation."""
    return confirmation_threshold(
        "MAIL_CAMPAIGN_RECIPIENTS", get_settings().campaign_confirm_recipients_default
    )


async def _auto_backup_decision(account_id: int) -> None:
    """Background: estimate the mailbox size at connect time and, when it is
    under the confirmation threshold, authorize the full backup automatically
    (a human just completed the OAuth flow, so origin is always ``user``). A
    mailbox over the threshold stays unauthorized until an explicit confirmed
    ``POST /accounts/{id}/backup`` — the Principle-4 gate."""
    settings = get_settings()
    async with SessionLocal() as session:
        account = await session.get(MailAccount, account_id)
        if account is None or account.backfill_authorized or account.backfill_done:
            if account is not None:
                await session.commit()
        else:
            try:
                async with GmailClient(settings, account) as client:
                    estimate, total = await estimate_backup_bytes(
                        client, settings.backup_estimate_sample
                    )
            except GmailError as exc:
                account.last_error = exc.detail
                await session.commit()
            else:
                account.backup_estimate_bytes = estimate
                account.messages_total = total
                if estimate <= _backup_threshold():
                    account.backfill_authorized = True
                await session.commit()
    # Kick a sync either way (incremental always; backfill only if authorized).
    await sync_manager.sync_account(account_id)


def _labels(message: MailMessage) -> list[str]:
    try:
        parsed = json.loads(message.label_ids)
    except ValueError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _str_list(raw: str) -> list[str]:
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _summary(message: MailMessage) -> MessageSummary:
    labels = _labels(message)
    return MessageSummary(
        id=message.id,
        account_id=message.account_id,
        gmail_id=message.gmail_id,
        thread_id=message.thread_id,
        subject=message.subject,
        snippet=message.snippet,
        from_addr=message.from_addr,
        to_addrs=_str_list(message.to_addrs),
        date=message.internal_date,
        label_ids=labels,
        has_attachments=message.has_attachments,
        unread="UNREAD" in labels,
        is_deleted=message.is_deleted,
    )


def _detail(message: MailMessage) -> MessageDetail:
    attachments = []
    try:
        raw = json.loads(message.attachments_json)
    except ValueError:
        raw = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and item.get("attachment_id"):
                attachments.append(
                    AttachmentOut(
                        attachment_id=str(item["attachment_id"]),
                        filename=str(item.get("filename") or "attachment"),
                        mime_type=str(item.get("mime_type") or ""),
                        size=int(item.get("size") or 0),
                    )
                )
    return MessageDetail(
        **_summary(message).model_dump(),
        cc_addrs=_str_list(message.cc_addrs),
        bcc_addrs=_str_list(message.bcc_addrs),
        rfc822_message_id=message.rfc822_message_id,
        body_text=message.body_text,
        body_html=message.body_html,
        size_estimate=message.size_estimate,
        attachments=attachments,
    )


async def _get_account(db: AsyncSession, account_id: int) -> MailAccount:
    account = await db.get(MailAccount, account_id)
    if account is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Account not found")
    return account


def _message_conditions(
    account_id: int | None, label: str, q: str, include_deleted: bool
) -> list:
    """Shared WHERE clause for message listings (per-account and aggregated).
    No per-user scoping — every user sees every mailbox (Principle 1)."""
    conditions: list = []
    if account_id is not None:
        conditions.append(MailMessage.account_id == account_id)
    label = (label or "").strip().upper()
    if label and label != "ALL":
        if not _LABEL_RE.match(label):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid label"
            )
        conditions.append(MailMessage.label_ids.like(f'%"{label}"%'))
    if not include_deleted:
        conditions.append(MailMessage.is_deleted.is_(False))
    needle = (q or "").strip()
    if needle:
        pattern = f"%{needle}%"
        conditions.append(
            or_(
                MailMessage.subject.ilike(pattern),
                MailMessage.from_addr.ilike(pattern),
                MailMessage.to_addrs.ilike(pattern),
                MailMessage.snippet.ilike(pattern),
                MailMessage.body_text.ilike(pattern),
            )
        )
    return conditions


async def _message_page(
    db: AsyncSession, conditions: list, page: int, per_page: int
) -> MessagePage:
    total = (
        await db.execute(select(func.count()).select_from(MailMessage).where(*conditions))
    ).scalar() or 0
    rows = (
        (
            await db.execute(
                select(MailMessage)
                .where(*conditions)
                .order_by(MailMessage.internal_date.desc().nullslast(), MailMessage.id.desc())
                .offset((page - 1) * per_page)
                .limit(per_page)
            )
        )
        .scalars()
        .all()
    )
    return MessagePage(
        items=[_summary(message) for message in rows],
        total=total,
        page=page,
        per_page=per_page,
    )


async def _thread(db: AsyncSession, account_id: int, thread_id: str) -> ThreadOut:
    await _get_account(db, account_id)
    rows = (
        (
            await db.execute(
                select(MailMessage)
                .where(
                    MailMessage.account_id == account_id,
                    MailMessage.thread_id == thread_id,
                )
                .order_by(MailMessage.internal_date.asc().nullsfirst(), MailMessage.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Thread not found")
    return ThreadOut(
        thread_id=thread_id,
        account_id=account_id,
        messages=[_detail(message) for message in rows],
    )


# --- accounts --------------------------------------------------------------


@router.get("/accounts", response_model=list[AccountOut])
async def list_accounts(
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[AccountOut]:
    accounts = (await db.execute(select(MailAccount).order_by(MailAccount.email))).scalars().all()

    async def counts(*conditions) -> dict[int, int]:
        rows = await db.execute(
            select(MailMessage.account_id, func.count())
            .where(MailMessage.is_deleted.is_(False), *conditions)
            .group_by(MailMessage.account_id)
        )
        return dict(rows.all())

    total = await counts()
    inbox = await counts(MailMessage.label_ids.like('%"INBOX"%'))
    sent = await counts(MailMessage.label_ids.like('%"SENT"%'))
    return [
        AccountOut(
            id=account.id,
            email=account.email,
            domain=account.domain,
            display_name=account.display_name,
            status=account.status,
            last_error=account.last_error,
            backfill_done=account.backfill_done,
            backfill_authorized=account.backfill_authorized,
            backup_estimate_bytes=account.backup_estimate_bytes,
            messages_total=account.messages_total,
            last_sync_at=account.last_sync_at,
            connected_by=account.connected_by,
            created_at=account.created_at,
            message_count=total.get(account.id, 0),
            inbox_count=inbox.get(account.id, 0),
            sent_count=sent.get(account.id, 0),
        )
        for account in accounts
    ]


@router.delete("/accounts/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_account(
    account_id: int,
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    account = await _get_account(db, account_id)
    await db.delete(account)  # messages go with it (FK ON DELETE CASCADE)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/accounts/{account_id}/sync",
    response_model=SyncTriggerOut,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_sync(
    account_id: int,
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SyncTriggerOut:
    await _get_account(db, account_id)
    asyncio.create_task(sync_manager.sync_account(account_id))
    return SyncTriggerOut()


# --- OAuth connect flow ----------------------------------------------------


@router.get("/oauth/url", response_model=OauthUrlOut)
async def oauth_url(
    user: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> OauthUrlOut:
    if not settings.oauth_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET are not configured",
        )
    state = secrets.token_urlsafe(32)
    db.add(MailOauthState(state=state, username=user.username))
    await db.execute(
        delete(MailOauthState).where(
            MailOauthState.created_at < datetime.now(timezone.utc) - _STATE_TTL
        )
    )
    await db.commit()
    return OauthUrlOut(url=gmail.build_auth_url(settings, state))


@router.get("/oauth/callback")
@anonymous(
    "Google OAuth redirect target — Google cannot send our bearer token; "
    "authenticated by a single-use MailOauthState row (10-min TTL) bound to "
    "the user who minted it via GET /api/mail/oauth/url"
)
async def oauth_callback(
    state: str = "",
    code: str = "",
    error: str = "",
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Google redirects here after consent. Unauthenticated by design — the
    single-use state row (minted by /oauth/url, 10-minute TTL) is the
    credential. Always bounces back to the Mail app with ?connected= or
    ?error= so the browser never dead-ends on an API response."""

    def bounce(param: str, value: str) -> RedirectResponse:
        separator = "&" if "?" in settings.mail_app_url else "?"
        return RedirectResponse(
            f"{settings.mail_app_url}{separator}{param}={quote(value)}",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    now = datetime.now(timezone.utc)
    row = await db.get(MailOauthState, state) if state else None
    if row is not None:
        # Single use, valid or not.
        await db.execute(delete(MailOauthState).where(MailOauthState.state == state))
        await db.commit()
    if row is None or (row.created_at or now) < now - _STATE_TTL:
        return bounce("error", "Sign-in link expired or invalid — try connecting again")
    if error:
        return bounce("error", f"Google returned: {error}")
    if not code:
        return bounce("error", "Google returned no authorization code")

    try:
        tokens = await gmail.exchange_code(settings, code)
    except GmailError as exc:
        return bounce("error", exc.detail)
    refresh_token = str(tokens.get("refresh_token") or "")
    probe = MailAccount(
        email="",
        domain="",
        refresh_token=refresh_token,
        access_token=str(tokens.get("access_token") or ""),
        token_expiry=now + timedelta(seconds=int(tokens.get("expires_in", 3600))),
    )
    try:
        async with GmailClient(settings, probe) as client:
            profile = await client.get_profile()
    except GmailError as exc:
        return bounce("error", exc.detail)
    email = str(profile.get("emailAddress") or "").lower()
    if not email:
        return bounce("error", "Could not determine the mailbox address")

    account = (
        await db.execute(select(MailAccount).where(MailAccount.email == email))
    ).scalar_one_or_none()
    if not refresh_token:
        # prompt=consent should always yield one; guard anyway.
        if account is None:
            return bounce(
                "error",
                "Google returned no refresh token — remove this app under "
                "myaccount.google.com/permissions and connect again",
            )
        refresh_token = account.refresh_token
    if account is None:
        account = MailAccount(
            email=email,
            domain=email.rsplit("@", 1)[-1],
            refresh_token=refresh_token,
            connected_by=row.username,
            history_id=str(profile.get("historyId") or ""),
        )
        db.add(account)
    account.refresh_token = refresh_token
    account.access_token = probe.access_token
    account.token_expiry = probe.token_expiry
    account.scopes = str(tokens.get("scope") or "")
    account.status = "active"
    account.last_error = ""
    if not account.history_id:
        account.history_id = str(profile.get("historyId") or "")
    await db.commit()
    await db.refresh(account)
    # Estimate the mailbox size and auto-authorize the full backup only when it
    # is under the Principle-4 threshold; larger mailboxes wait for an explicit
    # confirmed backup. Runs in the background so the redirect stays snappy.
    asyncio.create_task(_auto_backup_decision(account.id))
    return bounce("connected", email)


# --- messages --------------------------------------------------------------


@router.get("/accounts/{account_id}/messages", response_model=MessagePage)
async def list_messages(
    account_id: int,
    label: str = Query("INBOX", description="Gmail label id, or ALL"),
    q: str = Query("", max_length=256),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    include_deleted: bool = Query(False),
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MessagePage:
    await _get_account(db, account_id)
    conditions = _message_conditions(account_id, label, q, include_deleted)
    return await _message_page(db, conditions, page, per_page)


@router.get("/messages", response_model=MessagePage)
async def list_all_messages(
    label: str = Query("INBOX", description="Gmail label id, or ALL"),
    q: str = Query("", max_length=256),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    include_deleted: bool = Query(False),
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MessagePage:
    """Aggregated inbox/sent/search across ALL connected mailboxes and domains
    (the full multi-domain client view). No per-user scoping (Principle 1)."""
    conditions = _message_conditions(None, label, q, include_deleted)
    return await _message_page(db, conditions, page, per_page)


@router.get("/accounts/{account_id}/threads/{thread_id}", response_model=ThreadOut)
async def get_thread(
    account_id: int,
    thread_id: str,
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ThreadOut:
    """All archived messages of one Gmail thread on an account, oldest first."""
    return await _thread(db, account_id, thread_id)


@router.get("/messages/{message_id}", response_model=MessageDetail)
async def get_message(
    message_id: int,
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MessageDetail:
    message = await db.get(MailMessage, message_id)
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    return _detail(message)


@router.get("/messages/{message_id}/attachments/{attachment_id}")
async def download_attachment(
    message_id: int,
    attachment_id: str,
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    message = await db.get(MailMessage, message_id)
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    meta = next(
        (a for a in _detail(message).attachments if a.attachment_id == attachment_id), None
    )
    if meta is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attachment not found")
    account = await _get_account(db, message.account_id)
    try:
        async with GmailClient(settings, account) as client:
            payload = await client.get_attachment(message.gmail_id, attachment_id)
    except GmailError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.detail)
    await db.commit()  # persist a refreshed access token, if any
    data = gmail.decode_base64url(str(payload.get("data") or ""))
    filename = meta.filename.replace('"', "") or "attachment"
    return Response(
        content=data,
        media_type=meta.mime_type or "application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{quote(filename)}"',
        },
    )


# --- send ------------------------------------------------------------------


async def do_send(
    db: AsyncSession, settings: Settings, account: MailAccount, body: SendRequest
) -> MessageDetail:
    """Compose + send via the Gmail API, then mirror the sent message into the
    archive so it appears in SENT immediately. Shared by the UI and MCP routes."""
    to = [addr.strip() for addr in body.to if addr.strip()]
    cc = [addr.strip() for addr in body.cc if addr.strip()]
    bcc = [addr.strip() for addr in body.bcc if addr.strip()]
    if not to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="At least one recipient"
        )
    for addr in to + cc + bcc:
        # Catch obviously-malformed recipients BEFORE the Gmail round-trip so the
        # caller gets a precise 422 naming the bad address, rather than a generic
        # Gmail 400 -> (previously) opaque 502. Requires local@domain.tld shape;
        # the old check (`"@" in addr and " " not in addr`) let `foo@` / `@x.com`
        # through to Gmail, which rejected them 400 (the reported send failure).
        if not _RECIPIENT_RE.match(addr):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid recipient: {addr}",
            )
    # A one-off send is subject to the SAME anti-spam controls as a campaign
    # send (cross-campaign suppression + the GLOBAL per-domain daily cap) so it
    # cannot be looped to bulk-mail around the gated campaign path. Raises
    # 409/429 when the send is not allowed.
    await campaigns.enforce_direct_send(db, account, to + cc + bcc)
    thread_id = ""
    in_reply_to = ""
    if body.reply_to_message_id is not None:
        original = await db.get(MailMessage, body.reply_to_message_id)
        if original is None or original.account_id != account.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Message to reply to not found on this account",
            )
        thread_id = original.thread_id
        in_reply_to = original.rfc822_message_id
    sender = account.email
    if account.display_name:
        sender = f"{account.display_name} <{account.email}>"
    raw = gmail.build_mime(
        sender=sender,
        to=to,
        cc=cc,
        bcc=bcc,
        subject=body.subject,
        body_text=body.body_text,
        body_html=body.body_html,
        in_reply_to=in_reply_to,
    )
    try:
        async with GmailClient(settings, account) as client:
            sent = await client.send(raw, thread_id=thread_id)
            full = await client.get_message(str(sent.get("id") or ""))
    except GmailAuthError as exc:
        account.status = "error"
        account.last_error = str(exc)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Google rejected the mailbox credentials — reconnect the account",
        ) from exc
    except GmailError as exc:
        # Honor Gmail's status class: a 4xx means Gmail rejected THIS message
        # (bad recipient, malformed MIME, oversize) — a caller/input problem, so
        # surface it as that 4xx with Gmail's reason, NOT a 502. Mapping it to
        # 502 (the old behavior) told the user "server error, retry" for an
        # unfixable-by-retry input problem, and a 5xx also gets replaced by
        # Cloudflare's generic error page, hiding the reason entirely. Only a
        # genuine Gmail 5xx is a bad-gateway condition.
        raise _gmail_http_error(exc) from exc
    # Store the sent message immediately so it shows in SENT without waiting
    # for the next sync cycle.
    gmail_id = str(full.get("id") or "")
    existing = (
        await db.execute(
            select(MailMessage).where(
                MailMessage.account_id == account.id,
                MailMessage.gmail_id == gmail_id,
            )
        )
    ).scalar_one_or_none()
    message = existing or MailMessage(account_id=account.id, gmail_id=gmail_id)
    if existing is None:
        db.add(message)
    apply_parsed(message, gmail.parse_message(full))
    # Record the contact(s) into the shared per-send log so this send counts
    # toward the global per-domain budget and the cross-campaign contacted set.
    campaigns.record_direct_send(
        db,
        account,
        to + cc + bcc,
        gmail_message_id=gmail_id,
        thread_id=str(full.get("threadId") or ""),
    )
    await db.commit()
    await db.refresh(message)
    return _detail(message)


@router.post("/accounts/{account_id}/send", response_model=MessageDetail)
async def send_message(
    account_id: int,
    body: SendRequest,
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MessageDetail:
    account = await _get_account(db, account_id)
    return await do_send(db, settings, account, body)


# --- full-backup gate (Principle 4) ----------------------------------------


@router.get("/accounts/{account_id}/backup", response_model=BackupEstimateOut)
async def backup_estimate(
    account_id: int,
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> BackupEstimateOut:
    """Estimate the size of a full mailbox backup (cached, recomputed live when
    unknown) so a UI can decide whether the >threshold confirmation applies."""
    account = await _get_account(db, account_id)
    estimate = account.backup_estimate_bytes
    total = account.messages_total
    if estimate <= 0 and not account.backfill_done:
        try:
            async with GmailClient(settings, account) as client:
                estimate, total = await estimate_backup_bytes(
                    client, settings.backup_estimate_sample
                )
        except GmailError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.detail)
        account.backup_estimate_bytes = estimate
        account.messages_total = total
        await db.commit()
    threshold = _backup_threshold()
    return BackupEstimateOut(
        account_id=account.id,
        messages_total=total,
        estimated_bytes=estimate,
        threshold_bytes=int(threshold),
        over_threshold=estimate > threshold,
        backfill_authorized=account.backfill_authorized,
        backfill_done=account.backfill_done,
    )


@router.post("/accounts/{account_id}/backup", response_model=BackupStartOut)
async def start_backup(
    account_id: int,
    confirm: bool = Query(False),
    confirm_token: str | None = Query(None),
    human_approval: str | None = Query(None),
    user: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> BackupStartOut:
    """Authorize the full-mailbox backup into the never-delete archive.

    Principle-4 gate: estimates size first; if it exceeds the threshold the call
    returns 409 ``confirmation_required`` (a human re-invokes with ``confirm=true``)
    — and for an ``origin=agent`` caller ``confirm=true`` is ignored and the gate
    hard-enforces a human ``human_approval`` token instead."""
    account = await _get_account(db, account_id)
    if account.backfill_authorized or account.backfill_done:
        return BackupStartOut(
            account_id=account.id,
            status="already_authorized",
            estimated_bytes=account.backup_estimate_bytes,
            messages_total=account.messages_total,
        )
    estimate = account.backup_estimate_bytes
    total = account.messages_total
    if estimate <= 0:
        try:
            async with GmailClient(settings, account) as client:
                estimate, total = await estimate_backup_bytes(
                    client, settings.backup_estimate_sample
                )
        except GmailError as exc:
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.detail)

    # Raises ConfirmationRequired (409) / AgentApprovalRequired (409) as needed.
    require_confirmation(
        float(estimate),
        _backup_threshold(),
        confirm=confirm,
        confirm_token=confirm_token,
        unit="bytes",
        action=f"mail_backup:{account.id}",
        origin=user.origin,
        human_approval=human_approval,
        subject=user.username,
        message=(
            f"Full backup of {account.email} is estimated at {estimate} bytes "
            f"({total} messages); confirm to archive all of it."
        ),
    )
    account.backfill_authorized = True
    account.backup_estimate_bytes = estimate
    account.messages_total = total
    await db.commit()
    asyncio.create_task(sync_manager.sync_account(account.id))
    return BackupStartOut(
        account_id=account.id,
        status="authorized",
        estimated_bytes=estimate,
        messages_total=total,
    )


# --- campaigns -------------------------------------------------------------


async def _get_campaign(db: AsyncSession, campaign_id: int) -> Campaign:
    campaign = await db.get(Campaign, campaign_id)
    if campaign is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Campaign not found")
    return campaign


async def _pending_count(db: AsyncSession, campaign_id: int) -> int:
    from app.models import RECIPIENT_PENDING, CampaignRecipient

    return (
        await db.execute(
            select(func.count())
            .select_from(CampaignRecipient)
            .where(
                CampaignRecipient.campaign_id == campaign_id,
                CampaignRecipient.status == RECIPIENT_PENDING,
            )
        )
    ).scalar() or 0


async def gate_and_start_campaign(
    db: AsyncSession,
    user: UserOut,
    campaign: Campaign,
    *,
    confirm: bool,
    confirm_token: str | None,
    human_approval: str | None,
) -> Campaign:
    """Shared start/resume: a large campaign is an expensive action, so the
    number of pending recipients is gated (Principle 4, origin-aware — an agent
    cannot self-confirm; it needs a human_approval token)."""
    if campaign.status in (CAMPAIGN_COMPLETED, CAMPAIGN_CANCELLED):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Campaign is {campaign.status}",
        )
    pending = await _pending_count(db, campaign.id)
    require_confirmation(
        float(pending),
        _campaign_threshold(),
        confirm=confirm,
        confirm_token=confirm_token,
        unit="recipients",
        action=f"mail_campaign_start:{campaign.id}",
        origin=user.origin,
        human_approval=human_approval,
        subject=user.username,
        message=(
            f"Campaign '{campaign.name}' will message {pending} recipients; "
            "confirm to launch."
        ),
    )
    campaign.status = CAMPAIGN_RUNNING
    campaign.last_error = ""
    await db.commit()
    await db.refresh(campaign)  # reload server-side onupdate columns in async ctx
    asyncio.create_task(campaign_manager.tick(campaign.id))
    return campaign


async def gate_and_add_source(
    db: AsyncSession,
    user: UserOut,
    campaign: Campaign,
    *,
    search_id: str,
    label: str,
    recipients: list,
    confirm: bool,
    confirm_token: str | None,
    human_approval: str | None,
):
    """Shared continue/add-source path. Growing a campaign's send volume is
    itself an expensive action, so EVERY growth is gated (Principle 4,
    origin-aware) on the campaign's CUMULATIVE total send size — not just the
    marginal batch. This closes the bypass where an agent starts a campaign
    sub-threshold and then grows it past the threshold with no approval: an
    over-threshold ``origin=agent`` caller needs a human_approval token; a human
    re-invokes with ``confirm=true``.

    The recipients are merged first (so the estimate reflects post-dedupe/
    suppression reality); if the gate raises, the surrounding request session is
    rolled back and nothing is persisted."""
    source, counts = await campaigns.add_source(
        db,
        campaign,
        search_id=search_id,
        label=label,
        added_by=user.username,
        recipients=recipients,
    )
    total = await campaigns.campaign_send_volume(db, campaign.id)
    require_confirmation(
        float(total),
        _campaign_threshold(),
        confirm=confirm,
        confirm_token=confirm_token,
        unit="recipients",
        action=f"mail_campaign_grow:{campaign.id}",
        origin=user.origin,
        human_approval=human_approval,
        subject=user.username,
        message=(
            f"Continuing campaign '{campaign.name}' would grow it to {total} "
            "recipients (cumulative); confirm to proceed."
        ),
    )
    await db.commit()
    return source, counts


@router.get("/campaigns", response_model=list[CampaignOut])
async def list_campaigns(
    username: str | None = Query(None, description="Filter to one owner; omit for all"),
    status_filter: str | None = Query(None, alias="status"),
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CampaignOut]:
    """Cross-user: every campaign, or narrowed by ``?username=`` / ``?status=``."""
    stmt = select(Campaign).order_by(Campaign.created_at.desc()).limit(500)
    if username is not None:
        stmt = stmt.where(Campaign.owner == username)
    if status_filter is not None:
        stmt = stmt.where(Campaign.status == status_filter)
    rows = (await db.execute(stmt)).scalars().all()
    return [await campaigns.serialize_campaign(db, campaign) for campaign in rows]


@router.post("/campaigns", response_model=CampaignOut, status_code=status.HTTP_201_CREATED)
async def create_campaign(
    body: CampaignCreate,
    user: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CampaignOut:
    campaign = await campaigns.create_campaign(
        db,
        owner=user.username,
        origin=user.origin,
        actor=user.actor,
        name=body.name,
        subject=body.subject,
        body_text=body.body_text,
        body_html=body.body_html,
        send_strategy=body.send_strategy,
        sources=body.sources,
    )
    return await campaigns.serialize_campaign(db, campaign)


# NOTE: the /campaigns/settings routes MUST be registered BEFORE the
# /campaigns/{campaign_id} route below — FastAPI matches in registration order,
# so a bare {campaign_id} route defined first would shadow "settings" (and 422
# trying to coerce it to int).
@router.get("/campaigns/settings", response_model=CampaignSettingsOut)
async def get_campaign_settings(
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CampaignSettingsOut:
    """Read the campaign-manager-wide GLOBAL anti-spam per-domain daily send cap.
    mail-access gated (not anonymous)."""
    value = await campaigns.get_global_per_domain_cap(db)
    return CampaignSettingsOut(per_domain_daily=value)


@router.put("/campaigns/settings", response_model=CampaignSettingsOut)
async def update_campaign_settings(
    body: CampaignSettingsUpdate,
    user: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CampaignSettingsOut:
    """Set the campaign-manager-wide GLOBAL per-domain daily send cap (applies to
    the campaign pacer AND the one-off direct-send path). mail-access gated."""
    value = await campaigns.set_global_per_domain_cap(
        db, body.per_domain_daily, updated_by=user.username
    )
    return CampaignSettingsOut(per_domain_daily=value)


@router.get("/campaigns/{campaign_id}", response_model=CampaignOut)
async def get_campaign(
    campaign_id: int,
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CampaignOut:
    campaign = await _get_campaign(db, campaign_id)
    return await campaigns.serialize_campaign(db, campaign)


@router.post("/campaigns/{campaign_id}/sources", response_model=SourceMergeOut)
async def add_campaign_source(
    campaign_id: int,
    body: CampaignSourceIn,
    confirm: bool = Query(False),
    confirm_token: str | None = Query(None),
    human_approval: str | None = Query(None),
    user: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SourceMergeOut:
    """Continue a campaign: append another search's results, re-running dedupe +
    suppression so nobody already messaged is re-added. Gated on the campaign's
    cumulative send size (Principle 4) so growth past the threshold needs
    approval — an agent caller cannot self-confirm."""
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


@router.post("/campaigns/{campaign_id}/start", response_model=CampaignOut)
async def start_campaign(
    campaign_id: int,
    confirm: bool = Query(False),
    confirm_token: str | None = Query(None),
    human_approval: str | None = Query(None),
    user: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CampaignOut:
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


@router.post("/campaigns/{campaign_id}/pause", response_model=CampaignOut)
async def pause_campaign(
    campaign_id: int,
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CampaignOut:
    campaign = await _get_campaign(db, campaign_id)
    if campaign.status == CAMPAIGN_RUNNING:
        campaign.status = CAMPAIGN_PAUSED
        await db.commit()
        await db.refresh(campaign)
    return await campaigns.serialize_campaign(db, campaign)


@router.post("/campaigns/{campaign_id}/resume", response_model=CampaignOut)
async def resume_campaign(
    campaign_id: int,
    confirm: bool = Query(False),
    confirm_token: str | None = Query(None),
    human_approval: str | None = Query(None),
    user: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CampaignOut:
    campaign = await _get_campaign(db, campaign_id)
    if campaign.status != CAMPAIGN_PAUSED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Campaign is {campaign.status}, not paused",
        )
    campaign = await gate_and_start_campaign(
        db,
        user,
        campaign,
        confirm=confirm,
        confirm_token=confirm_token,
        human_approval=human_approval,
    )
    return await campaigns.serialize_campaign(db, campaign)


@router.post("/campaigns/{campaign_id}/cancel", response_model=CampaignOut)
async def cancel_campaign(
    campaign_id: int,
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CampaignOut:
    campaign = await _get_campaign(db, campaign_id)
    if campaign.status not in (CAMPAIGN_COMPLETED, CAMPAIGN_CANCELLED):
        campaign.status = CAMPAIGN_CANCELLED
        await db.commit()
        await db.refresh(campaign)
    return await campaigns.serialize_campaign(db, campaign)


# --- contacts / suppression set --------------------------------------------


@router.get("/contacts/contacted", response_model=ContactedOut)
async def contacted(
    campaign_id: int | None = Query(None, description="Scope to one campaign; omit for all"),
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ContactedOut:
    """The already-contacted set (from ``campaign_messages``) that search's
    ``exclude_contacted`` filter consults over the documented search->mail hop.
    Cross-campaign by default; ``?campaign_id=`` narrows it to one campaign."""
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
