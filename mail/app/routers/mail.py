"""Mail API: connected Google mailboxes, their synced messages, and sending.

Every route is authorized through the central auth service (service="mail")
except the OAuth callback, which authenticates with the single-use state row
minted by ``GET /oauth/url`` (Google cannot send a bearer token), and the
health probe in ``main.py``.
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
from sqlalchemy.ext.asyncio import AsyncSession

from app import gmail
from app.auth import get_current_user
from app.config import Settings, get_settings
from app.database import get_db
from app.gmail import GmailAuthError, GmailClient, GmailError
from app.models import MailAccount, MailMessage, MailOauthState
from app.schemas import (
    AccountOut,
    AttachmentOut,
    MessageDetail,
    MessagePage,
    MessageSummary,
    OauthUrlOut,
    SendRequest,
    SyncTriggerOut,
    UserOut,
)
from app.sync import apply_parsed, sync_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/mail", tags=["mail"])

_STATE_TTL = timedelta(minutes=10)
# Gmail label ids we accept as list filters (system labels + user label ids).
_LABEL_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")


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
    asyncio.create_task(sync_manager.sync_account(account.id))
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
    label = label.strip().upper()
    conditions = [MailMessage.account_id == account_id]
    if label != "ALL":
        if not _LABEL_RE.match(label):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid label"
            )
        conditions.append(MailMessage.label_ids.like(f'%"{label}"%'))
    if not include_deleted:
        conditions.append(MailMessage.is_deleted.is_(False))
    needle = q.strip()
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


@router.post("/accounts/{account_id}/send", response_model=MessageDetail)
async def send_message(
    account_id: int,
    body: SendRequest,
    _: UserOut = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> MessageDetail:
    account = await _get_account(db, account_id)
    to = [addr.strip() for addr in body.to if addr.strip()]
    cc = [addr.strip() for addr in body.cc if addr.strip()]
    bcc = [addr.strip() for addr in body.bcc if addr.strip()]
    if not to:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="At least one recipient"
        )
    for addr in to + cc + bcc:
        if "@" not in addr or " " in addr:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid recipient: {addr}",
            )
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
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.detail) from exc
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
    await db.commit()
    await db.refresh(message)
    return _detail(message)
