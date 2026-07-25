"""Campaign engine — recipient sourcing, suppression, and paced multi-domain send.

A campaign draws recipients from one or more search result lists (``campaign_sources``;
the caller supplies the resolved recipients — ``mail`` never calls search). Recipients
are merged into a deduped pool (``campaign_recipients``, unique per ``(campaign, email)``)
and sent from the **initiating user's own** connected mailboxes across possibly multiple
domains, paced by the campaign throttle.

Suppression is **authoritative and server-side at send time** (never trust a pre-filter):

1. within-campaign dedupe — never message the same person twice in one campaign;
2. cross-campaign per-person suppression — a global contacted set built from
   ``campaign_messages`` (optionally within a cooldown window);
3. per-domain daily cap — the throttle's ``per_domain_daily`` (default 20).

``send_strategy``:

- ``balanced`` — spread sends across the user's domains in parallel (each domain up to
  its daily cap);
- ``sequential`` — fill one domain to its daily cap, then move to the next.

Lifecycle (draft/running/paused/completed/cancelled) lives here and is paced by a
background loop, mirroring ``sync.py``. A per-campaign asyncio lock keeps the loop and
manual triggers off the same campaign concurrently.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, status
from sqlalchemy import func, select

from app.config import get_settings
from app.database import SessionLocal
from app.gmail import GmailAuthError, GmailClient, GmailError, build_mime, parse_message
from app.models import (
    CAMPAIGN_COMPLETED,
    CAMPAIGN_DRAFT,
    CAMPAIGN_RUNNING,
    RECIPIENT_FAILED,
    RECIPIENT_PENDING,
    RECIPIENT_SENT,
    RECIPIENT_SUPPRESSED,
    SEND_STRATEGIES,
    Campaign,
    CampaignMessage,
    CampaignRecipient,
    CampaignSource,
    MailAccount,
    MailMessage,
)
from app.schemas import (
    CampaignOut,
    CampaignSourceOut,
    CampaignStats,
)
from app.sync import apply_parsed

logger = logging.getLogger(__name__)


def _norm_email(value: str) -> str:
    return (value or "").strip().lower()


def _domain_of(email: str) -> str:
    return email.rsplit("@", 1)[-1] if "@" in email else ""


def _today_start() -> datetime:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _per_domain_daily(campaign: Campaign) -> int:
    throttle = campaign.throttle if isinstance(campaign.throttle, dict) else {}
    try:
        value = int(throttle.get("per_domain_daily"))
    except (TypeError, ValueError):
        value = 0
    if value <= 0:
        value = get_settings().campaign_per_domain_daily_default
    return value


def _render(template: str, recipient: CampaignRecipient) -> str:
    return (
        (template or "")
        .replace("{{name}}", recipient.name or "")
        .replace("{{email}}", recipient.email or "")
    )


# --- suppression -----------------------------------------------------------


async def _contacted_globally(session, email: str) -> datetime | None:
    """Most-recent send time to ``email`` across ALL campaigns, honoring the
    configured cooldown window. ``None`` = not suppressed by cross-campaign
    contact."""
    settings = get_settings()
    conditions = [
        CampaignMessage.person_email == email,
        CampaignMessage.status == "sent",
    ]
    cooldown = settings.campaign_suppression_cooldown_days
    if cooldown > 0:
        conditions.append(
            CampaignMessage.sent_at >= datetime.now(timezone.utc) - timedelta(days=cooldown)
        )
    row = await session.execute(
        select(func.max(CampaignMessage.sent_at)).where(*conditions)
    )
    return row.scalar()


async def _already_sent_in_campaign(session, campaign_id: int, email: str) -> bool:
    row = await session.execute(
        select(func.count())
        .select_from(CampaignMessage)
        .where(
            CampaignMessage.campaign_id == campaign_id,
            CampaignMessage.person_email == email,
            CampaignMessage.status == "sent",
        )
    )
    return (row.scalar() or 0) > 0


async def suppression_reason(session, campaign: Campaign, email: str) -> str:
    """Authoritative send-time check. Returns a suppression reason, or ``""``
    when the recipient may be messaged."""
    if await _already_sent_in_campaign(session, campaign.id, email):
        return "already_sent_in_campaign"
    if await _contacted_globally(session, email) is not None:
        return "globally_contacted"
    return ""


async def _sent_in_campaign_set(session, campaign_id: int, emails: list[str]) -> set[str]:
    """Batch of the ``emails`` already sent in this campaign (within-campaign
    dedupe). Replaces the per-recipient N+1 query in merge."""
    if not emails:
        return set()
    rows = await session.execute(
        select(func.distinct(CampaignMessage.person_email)).where(
            CampaignMessage.campaign_id == campaign_id,
            CampaignMessage.status == "sent",
            CampaignMessage.person_email.in_(emails),
        )
    )
    return {e for e in rows.scalars() if e}


async def _contacted_set(session, emails: list[str]) -> set[str]:
    """Batch of the ``emails`` globally contacted across ALL campaigns AND
    one-off sends (cross-campaign suppression), honoring the cooldown window."""
    if not emails:
        return set()
    conditions = [
        CampaignMessage.status == "sent",
        CampaignMessage.person_email.in_(emails),
    ]
    cooldown = get_settings().campaign_suppression_cooldown_days
    if cooldown > 0:
        conditions.append(
            CampaignMessage.sent_at >= datetime.now(timezone.utc) - timedelta(days=cooldown)
        )
    rows = await session.execute(
        select(func.distinct(CampaignMessage.person_email)).where(*conditions)
    )
    return {e for e in rows.scalars() if e}


# --- recipient merge (source add / continue) -------------------------------


async def merge_recipients(
    session,
    campaign: Campaign,
    source: CampaignSource,
    recipients: list[dict],
) -> dict[str, int]:
    """Merge a source's recipients into the campaign pool, applying
    within-campaign dedupe and a suppression pre-mark. Returns counts."""
    existing = set(
        (
            await session.execute(
                select(CampaignRecipient.email).where(
                    CampaignRecipient.campaign_id == campaign.id
                )
            )
        ).scalars()
    )
    # Normalize + de-dupe the incoming batch up front so suppression can be
    # resolved in TWO batched queries instead of two-per-recipient (N+1).
    normalized: list[dict] = []
    batch_emails: list[str] = []
    for item in recipients:
        email = _norm_email(str(item.get("email") or ""))
        if not email or "@" not in email:
            continue
        normalized.append({"email": email, "item": item})
        batch_emails.append(email)
    sent_in_campaign = await _sent_in_campaign_set(session, campaign.id, batch_emails)
    globally_contacted = await _contacted_set(session, batch_emails)

    submitted = added = duplicate = suppressed = 0
    seen_this_batch: set[str] = set()
    for entry in normalized:
        email = entry["email"]
        item = entry["item"]
        submitted += 1
        if email in existing or email in seen_this_batch:
            duplicate += 1
            continue
        seen_this_batch.add(email)
        if email in sent_in_campaign:
            reason = "already_sent_in_campaign"
        elif email in globally_contacted:
            reason = "globally_contacted"
        else:
            reason = ""
        recipient_status = RECIPIENT_SUPPRESSED if reason else RECIPIENT_PENDING
        if reason:
            suppressed += 1
        else:
            added += 1
        session.add(
            CampaignRecipient(
                campaign_id=campaign.id,
                source_id=source.id,
                email=email,
                apollo_id=str(item.get("apollo_id") or ""),
                name=str(item.get("name") or ""),
                status=recipient_status,
                suppressed_reason=reason,
            )
        )
    source.recipient_count = submitted
    source.added_count = added
    await session.flush()
    return {
        "submitted": submitted,
        "added": added,
        "duplicate_in_campaign": duplicate,
        "suppressed": suppressed,
    }


# --- shared campaign operations (reused by UI + MCP routers) ---------------


async def create_campaign(
    session,
    *,
    owner: str,
    origin: str,
    actor: str,
    name: str,
    subject: str,
    body_text: str,
    body_html: str,
    send_strategy: str,
    per_domain_daily: int,
    sources: list,
) -> Campaign:
    """Create a draft campaign and merge its initial source lists (dedupe +
    suppression applied). ``sources`` items expose ``search_id``, ``label`` and
    ``recipients`` (each ``{email, apollo_id, name}``)."""
    if send_strategy not in SEND_STRATEGIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"send_strategy must be one of {sorted(SEND_STRATEGIES)}",
        )
    campaign = Campaign(
        owner=owner,
        origin=origin,
        actor=actor,
        name=name,
        status=CAMPAIGN_DRAFT,
        send_strategy=send_strategy,
        throttle={"per_domain_daily": int(per_domain_daily)},
        subject=subject,
        body_text=body_text,
        body_html=body_html,
    )
    session.add(campaign)
    await session.flush()
    for source_in in sources:
        await add_source(
            session,
            campaign,
            search_id=getattr(source_in, "search_id", "") or "",
            label=getattr(source_in, "label", "") or "",
            added_by=owner,
            recipients=[
                r.model_dump() if hasattr(r, "model_dump") else dict(r)
                for r in (getattr(source_in, "recipients", None) or [])
            ],
        )
    await session.commit()
    await session.refresh(campaign)
    return campaign


async def add_source(
    session,
    campaign: Campaign,
    *,
    search_id: str,
    label: str,
    added_by: str,
    recipients: list,
) -> tuple[CampaignSource, dict[str, int]]:
    """Append a source list to a campaign (continue), merging its recipients
    with dedupe + suppression re-applied so nobody already messaged is re-added."""
    source = CampaignSource(
        campaign_id=campaign.id,
        search_id=search_id,
        label=label,
        added_by=added_by,
    )
    session.add(source)
    await session.flush()
    counts = await merge_recipients(session, campaign, source, list(recipients or []))
    return source, counts


async def serialize_campaign(session, campaign: Campaign) -> CampaignOut:
    sources = list(
        (
            await session.execute(
                select(CampaignSource)
                .where(CampaignSource.campaign_id == campaign.id)
                .order_by(CampaignSource.id)
            )
        )
        .scalars()
        .all()
    )
    stats = await campaign_stats(session, campaign.id)
    return CampaignOut(
        id=campaign.id,
        owner=campaign.owner,
        origin=campaign.origin,
        actor=campaign.actor,
        name=campaign.name,
        status=campaign.status,
        send_strategy=campaign.send_strategy,
        throttle=campaign.throttle if isinstance(campaign.throttle, dict) else {},
        subject=campaign.subject,
        body_text=campaign.body_text,
        body_html=campaign.body_html,
        last_error=campaign.last_error,
        created_at=campaign.created_at,
        updated_at=campaign.updated_at,
        sources=[
            CampaignSourceOut(
                id=s.id,
                search_id=s.search_id,
                label=s.label,
                added_by=s.added_by,
                recipient_count=s.recipient_count,
                added_count=s.added_count,
                created_at=s.created_at,
            )
            for s in sources
        ],
        stats=CampaignStats(**stats),
    )


# --- stats -----------------------------------------------------------------


async def campaign_stats(session, campaign_id: int) -> dict:
    rows = await session.execute(
        select(CampaignRecipient.status, func.count())
        .where(CampaignRecipient.campaign_id == campaign_id)
        .group_by(CampaignRecipient.status)
    )
    by_status = {recipient_status: count for recipient_status, count in rows.all()}
    messages_sent = (
        await session.execute(
            select(func.count())
            .select_from(CampaignMessage)
            .where(
                CampaignMessage.campaign_id == campaign_id,
                CampaignMessage.status == "sent",
            )
        )
    ).scalar() or 0
    today_rows = await session.execute(
        select(CampaignMessage.domain, func.count())
        .where(
            CampaignMessage.campaign_id == campaign_id,
            CampaignMessage.status == "sent",
            CampaignMessage.sent_at >= _today_start(),
        )
        .group_by(CampaignMessage.domain)
    )
    return {
        "recipients_total": sum(by_status.values()),
        "pending": by_status.get(RECIPIENT_PENDING, 0),
        "sent": by_status.get(RECIPIENT_SENT, 0),
        "suppressed": by_status.get(RECIPIENT_SUPPRESSED, 0),
        "failed": by_status.get(RECIPIENT_FAILED, 0),
        "messages_sent": messages_sent,
        "sent_today_by_domain": {domain: count for domain, count in today_rows.all()},
    }


# --- contacted set (for search's exclude_contacted) ------------------------


async def contacted_contacts(session, campaign_id: int | None = None) -> list[dict]:
    conditions = [CampaignMessage.status == "sent"]
    if campaign_id is not None:
        conditions.append(CampaignMessage.campaign_id == campaign_id)
    rows = await session.execute(
        select(
            CampaignMessage.person_email,
            func.max(CampaignMessage.sent_at),
            func.array_agg(func.distinct(CampaignMessage.campaign_id)),
        )
        .where(*conditions)
        .group_by(CampaignMessage.person_email)
    )
    out: list[dict] = []
    for email, last_contacted, campaign_ids in rows.all():
        if not email:
            continue
        out.append(
            {
                "email": email,
                "last_contacted": last_contacted,
                "campaign_ids": sorted(cid for cid in (campaign_ids or []) if cid is not None),
            }
        )
    return out


# --- global per-domain cap + cumulative volume (Principle-4 growth gate) ----


def global_per_domain_cap() -> int:
    """The anti-spam per-domain daily send cap, counted GLOBALLY across every
    campaign and every one-off send from a given sending domain."""
    return get_settings().campaign_per_domain_daily_default


async def global_domain_sent_today(session, domain: str) -> int:
    """Total messages sent today from ``domain`` across ALL campaigns AND
    one-off sends. This is the authoritative per-domain daily budget consumer —
    the cap holds globally, so N campaigns can no longer each send a full cap."""
    row = await session.execute(
        select(func.count())
        .select_from(CampaignMessage)
        .where(
            CampaignMessage.status == "sent",
            CampaignMessage.domain == domain,
            CampaignMessage.sent_at >= _today_start(),
        )
    )
    return row.scalar() or 0


async def domains_sent_today(session, domains: list[str]) -> dict[str, int]:
    """Batched ``{domain: sent_today}`` across ALL campaigns + one-off sends."""
    if not domains:
        return {}
    rows = await session.execute(
        select(CampaignMessage.domain, func.count())
        .where(
            CampaignMessage.status == "sent",
            CampaignMessage.sent_at >= _today_start(),
            CampaignMessage.domain.in_(domains),
        )
        .group_by(CampaignMessage.domain)
    )
    return {domain: count for domain, count in rows.all()}


async def campaign_send_volume(session, campaign_id: int) -> int:
    """The campaign's CUMULATIVE send size = recipients that will be (pending)
    or already have been (sent) messaged. This is what the Principle-4 growth
    gate estimates on, so an agent cannot grow a campaign past the threshold in
    small increments without a human approval."""
    row = await session.execute(
        select(func.count())
        .select_from(CampaignRecipient)
        .where(
            CampaignRecipient.campaign_id == campaign_id,
            CampaignRecipient.status.in_([RECIPIENT_PENDING, RECIPIENT_SENT]),
        )
    )
    return row.scalar() or 0


# --- one-off send controls (send_message parity with campaigns) -------------


async def enforce_direct_send(session, account: MailAccount, recipients: list[str]) -> None:
    """Apply the SAME anti-spam controls to a one-off ``send_message`` as to a
    campaign send: cross-campaign suppression + the GLOBAL per-domain daily cap.
    Raises ``HTTPException`` (409/429) when the send is not allowed. A single
    send under the cap passes; looping to bulk-send trips the cap or suppression,
    so ``send_message`` is not an escape hatch around gated campaigns."""
    normalized = sorted({_norm_email(e) for e in recipients if _norm_email(e)})
    if not normalized:
        return
    contacted = await _contacted_set(session, normalized)
    if contacted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Recipient(s) already contacted (cross-campaign suppression): "
                + ", ".join(sorted(contacted))
            ),
        )
    cap = global_per_domain_cap()
    sent_today = await global_domain_sent_today(session, account.domain)
    if sent_today + len(normalized) > cap:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Per-domain daily send cap reached for {account.domain} "
                f"({sent_today}/{cap}); use a gated campaign for bulk sending."
            ),
        )


def record_direct_send(
    session,
    account: MailAccount,
    recipients: list[str],
    *,
    gmail_message_id: str,
    thread_id: str,
) -> None:
    """Record a one-off send into the shared per-send log (``campaign_id=NULL``)
    so it counts toward the global per-domain budget and the cross-campaign
    contacted/suppression set, exactly like a campaign send."""
    now = datetime.now(timezone.utc)
    for raw in sorted({_norm_email(e) for e in recipients if _norm_email(e)}):
        session.add(
            CampaignMessage(
                campaign_id=None,
                person_email=raw,
                account_id=account.id,
                domain=account.domain,
                gmail_message_id=gmail_message_id,
                thread_id=thread_id,
                status="sent",
                sent_at=now,
            )
        )


# --- send engine -----------------------------------------------------------


class CampaignManager:
    def __init__(self) -> None:
        self._locks: dict[int, asyncio.Lock] = {}
        self._task: asyncio.Task | None = None

    def _lock(self, campaign_id: int) -> asyncio.Lock:
        return self._locks.setdefault(campaign_id, asyncio.Lock())

    def start(self) -> None:
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self) -> None:
        settings = get_settings()
        while True:
            try:
                await self.tick_all()
            except Exception:
                logger.exception("Campaign pacer cycle failed")
            await asyncio.sleep(settings.campaign_interval_seconds)

    async def tick_all(self) -> None:
        async with SessionLocal() as session:
            ids = list(
                (
                    await session.execute(
                        select(Campaign.id).where(Campaign.status == CAMPAIGN_RUNNING)
                    )
                ).scalars()
            )
        for campaign_id in ids:
            try:
                await self.tick(campaign_id)
            except Exception:
                logger.exception("Campaign %s pacing failed", campaign_id)

    async def tick(self, campaign_id: int) -> None:
        async with self._lock(campaign_id):
            await self._tick_locked(campaign_id)

    async def _budget(self, session, campaign: Campaign, domains: list[str]) -> dict[str, int]:
        """Remaining sends allowed this cycle per domain, honoring the daily cap
        and the strategy (sequential fills one domain before the next)."""
        settings = get_settings()
        cap = _per_domain_daily(campaign)
        per_cycle = settings.campaign_sends_per_domain_per_cycle
        # GLOBAL per-domain budget: count today's sends from each domain across
        # ALL campaigns and one-off sends, not just this campaign. Otherwise N
        # campaigns on the same domain would each get a full cap (anti-spam
        # bypass).
        sent_today = await domains_sent_today(session, domains)
        budget: dict[str, int] = {}
        if campaign.send_strategy == "sequential":
            for domain in domains:
                remaining_today = cap - sent_today.get(domain, 0)
                if remaining_today > 0:
                    budget[domain] = min(remaining_today, per_cycle)
                    break  # fill this domain first; others wait
        else:  # balanced
            for domain in domains:
                remaining_today = cap - sent_today.get(domain, 0)
                if remaining_today > 0:
                    budget[domain] = min(remaining_today, per_cycle)
        return budget

    def _pick_domain(self, strategy: str, domains: list[str], budget: dict[str, int]) -> str:
        avail = [d for d in domains if budget.get(d, 0) > 0]
        if not avail:
            return ""
        if strategy == "sequential":
            return avail[0]
        # balanced: even spread → most remaining budget first.
        return max(avail, key=lambda d: budget[d])

    async def _tick_locked(self, campaign_id: int) -> None:
        settings = get_settings()
        async with SessionLocal() as session:
            campaign = await session.get(Campaign, campaign_id)
            if campaign is None or campaign.status != CAMPAIGN_RUNNING:
                return

            # Sender mailboxes = the initiating user's OWN active mailboxes,
            # grouped by domain (possibly multiple domains).
            accounts = list(
                (
                    await session.execute(
                        select(MailAccount).where(
                            MailAccount.connected_by == campaign.owner,
                            MailAccount.status == "active",
                        )
                    )
                )
                .scalars()
                .all()
            )
            by_domain: dict[str, list[MailAccount]] = {}
            for account in accounts:
                by_domain.setdefault(account.domain, []).append(account)
            if not by_domain:
                campaign.last_error = (
                    "No active connected mailboxes for this owner to send from"
                )
                await session.commit()
                return

            domains = sorted(by_domain)
            budget = await self._budget(session, campaign, domains)

            pending_total = (
                await session.execute(
                    select(func.count())
                    .select_from(CampaignRecipient)
                    .where(
                        CampaignRecipient.campaign_id == campaign.id,
                        CampaignRecipient.status == RECIPIENT_PENDING,
                    )
                )
            ).scalar() or 0
            if pending_total == 0:
                campaign.status = CAMPAIGN_COMPLETED
                campaign.last_error = ""
                await session.commit()
                return
            if not budget:
                return  # daily caps hit; try again next cycle / tomorrow

            # Pull enough pending recipients to fill the cycle budget (plus a
            # margin for ones we may suppress at send time).
            want = sum(budget.values())
            pending = list(
                (
                    await session.execute(
                        select(CampaignRecipient)
                        .where(
                            CampaignRecipient.campaign_id == campaign.id,
                            CampaignRecipient.status == RECIPIENT_PENDING,
                        )
                        .order_by(CampaignRecipient.id)
                        .limit(want * 4 + 20)
                    )
                )
                .scalars()
                .all()
            )

            clients: dict[int, GmailClient] = {}
            rr: dict[str, int] = {}
            try:
                for recipient in pending:
                    if sum(budget.values()) <= 0:
                        break
                    email = _norm_email(recipient.email)
                    reason = await suppression_reason(session, campaign, email)
                    if reason:
                        recipient.status = RECIPIENT_SUPPRESSED
                        recipient.suppressed_reason = reason
                        await session.commit()  # persist the suppression decision
                        continue
                    domain = self._pick_domain(campaign.send_strategy, domains, budget)
                    if not domain:
                        break
                    pool = by_domain[domain]
                    account = pool[rr.get(domain, 0) % len(pool)]
                    rr[domain] = rr.get(domain, 0) + 1
                    client = clients.get(account.id)
                    if client is None:
                        client = GmailClient(settings, account)
                        await client.__aenter__()
                        clients[account.id] = client
                    ok = await self._send_one(session, campaign, recipient, account, client)
                    # Persist THIS send's CampaignMessage + recipient status
                    # immediately: a Gmail send is irreversible, so a mid-cycle
                    # crash must never re-send already-delivered mail on retry.
                    await session.commit()
                    if ok:
                        budget[domain] -= 1
            finally:
                for client in clients.values():
                    await client.__aexit__(None, None, None)

            # Recompute completion after this batch.
            remaining = (
                await session.execute(
                    select(func.count())
                    .select_from(CampaignRecipient)
                    .where(
                        CampaignRecipient.campaign_id == campaign.id,
                        CampaignRecipient.status == RECIPIENT_PENDING,
                    )
                )
            ).scalar() or 0
            if remaining == 0:
                fresh = await session.get(Campaign, campaign_id)
                if fresh is not None and fresh.status == CAMPAIGN_RUNNING:
                    fresh.status = CAMPAIGN_COMPLETED
                    await session.commit()

    async def _send_one(
        self,
        session,
        campaign: Campaign,
        recipient: CampaignRecipient,
        account: MailAccount,
        client: GmailClient,
    ) -> bool:
        sender = account.email
        if account.display_name:
            sender = f"{account.display_name} <{account.email}>"
        raw = build_mime(
            sender=sender,
            to=[recipient.email],
            cc=[],
            bcc=[],
            subject=_render(campaign.subject, recipient),
            body_text=_render(campaign.body_text, recipient),
            body_html=_render(campaign.body_html, recipient),
        )
        try:
            sent = await client.send(raw)
            gmail_id = str(sent.get("id") or "")
            thread_id = str(sent.get("threadId") or "")
        except GmailAuthError as exc:
            account.status = "error"
            account.last_error = str(exc)
            recipient.status = RECIPIENT_FAILED
            session.add(
                CampaignMessage(
                    campaign_id=campaign.id,
                    recipient_id=recipient.id,
                    person_email=recipient.email,
                    person_apollo_id=recipient.apollo_id,
                    account_id=account.id,
                    domain=account.domain,
                    status="failed",
                    error=str(exc),
                )
            )
            return False
        except GmailError as exc:
            recipient.status = RECIPIENT_FAILED
            session.add(
                CampaignMessage(
                    campaign_id=campaign.id,
                    recipient_id=recipient.id,
                    person_email=recipient.email,
                    person_apollo_id=recipient.apollo_id,
                    account_id=account.id,
                    domain=account.domain,
                    status="failed",
                    error=exc.detail,
                )
            )
            return False

        recipient.status = RECIPIENT_SENT
        session.add(
            CampaignMessage(
                campaign_id=campaign.id,
                recipient_id=recipient.id,
                person_email=recipient.email,
                person_apollo_id=recipient.apollo_id,
                account_id=account.id,
                domain=account.domain,
                gmail_message_id=gmail_id,
                thread_id=thread_id,
                status="sent",
                sent_at=datetime.now(timezone.utc),
            )
        )
        # Best-effort mirror into the archive so it shows in SENT immediately.
        # This must NEVER prevent the caller's per-send commit: the Gmail send
        # already succeeded and its CampaignMessage/RECIPIENT_SENT row is staged,
        # so any failure here (not just GmailError — a parse bug, a mirror insert
        # conflict, anything) must be swallowed. Otherwise the row would roll back
        # and a mid-cycle retry would re-send already-delivered mail (Principle-4
        # irreversibility). The next sync reconciles the archive regardless.
        try:
            full = await client.get_message(gmail_id)
            existing = (
                await session.execute(
                    select(MailMessage).where(
                        MailMessage.account_id == account.id,
                        MailMessage.gmail_id == gmail_id,
                    )
                )
            ).scalar_one_or_none()
            message = existing or MailMessage(account_id=account.id, gmail_id=gmail_id)
            if existing is None:
                session.add(message)
            apply_parsed(message, parse_message(full))
        except Exception:
            logger.warning("SENT-mirror failed for %s (send already recorded)", gmail_id)
        return True


campaign_manager = CampaignManager()
