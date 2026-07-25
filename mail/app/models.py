from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class MailAccount(Base):
    """One connected Google-hosted mailbox (any domain)."""

    __tablename__ = "mail_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    # OAuth material. The refresh token is the long-lived credential; the
    # access token is a short-lived cache refreshed in place by GmailClient.
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False)
    access_token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    token_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scopes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # active | error | disabled
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Incremental-sync anchor (history.list startHistoryId), captured at
    # connect time so mail arriving during backfill is never missed.
    history_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    # Full-mailbox backfill cursor: next messages.list pageToken.
    backfill_page_token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    backfill_done: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Full-backup gate (Principle 4): the whole-mailbox backfill only runs once
    # authorized — either auto (estimate under threshold at connect) or after an
    # explicit confirmed backup request. Until then only incremental sync runs,
    # so a huge mailbox is never silently downloaded.
    backfill_authorized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Cached size estimate (bytes) computed at connect / backup-estimate time.
    backup_estimate_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    messages_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    connected_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    messages: Mapped[list["MailMessage"]] = relationship(
        back_populates="account", cascade="all, delete-orphan", passive_deletes=True
    )


class MailMessage(Base):
    """One Gmail message, bodies included. Attachments stay in Gmail; only
    their metadata is stored (attachments_json) and bytes are proxied on
    demand. Messages deleted on the Google side are flagged, not removed —
    this store is the archive."""

    __tablename__ = "mail_messages"
    __table_args__ = (
        UniqueConstraint("account_id", "gmail_id", name="uq_mail_messages_account_gmail"),
        Index("ix_mail_messages_account_date", "account_id", "internal_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    account_id: Mapped[int] = mapped_column(
        ForeignKey("mail_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    gmail_id: Mapped[str] = mapped_column(String(32), nullable=False)
    thread_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    history_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    rfc822_message_id: Mapped[str] = mapped_column(Text, nullable=False, default="")
    internal_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    snippet: Mapped[str] = mapped_column(Text, nullable=False, default="")
    from_addr: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # JSON lists of formatted addresses ("Name <a@b.c>").
    to_addrs: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    cc_addrs: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    bcc_addrs: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # JSON list of Gmail label ids (INBOX, SENT, UNREAD, ...).
    label_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    body_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_html: Mapped[str] = mapped_column(Text, nullable=False, default="")
    attachments_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    has_attachments: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    size_estimate: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_deleted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    account: Mapped[MailAccount] = relationship(back_populates="messages")


class MailOauthState(Base):
    """Single-use CSRF state for the OAuth consent flow, bound to the hub
    user who initiated it (the callback itself carries no bearer token)."""

    __tablename__ = "mail_oauth_states"

    state: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# --- Campaigns -------------------------------------------------------------
# Campaign lifecycle states.
CAMPAIGN_DRAFT = "draft"
CAMPAIGN_RUNNING = "running"
CAMPAIGN_PAUSED = "paused"
CAMPAIGN_COMPLETED = "completed"
CAMPAIGN_CANCELLED = "cancelled"
CAMPAIGN_STATUSES = frozenset(
    {CAMPAIGN_DRAFT, CAMPAIGN_RUNNING, CAMPAIGN_PAUSED, CAMPAIGN_COMPLETED, CAMPAIGN_CANCELLED}
)

# Per-recipient states in the deduped/suppressed send pool.
RECIPIENT_PENDING = "pending"
RECIPIENT_SENT = "sent"
RECIPIENT_SUPPRESSED = "suppressed"
RECIPIENT_FAILED = "failed"

SEND_STRATEGIES = frozenset({"balanced", "sequential"})


class Campaign(Base):
    """An outreach campaign owned by its initiating user (origin/actor record
    who kicked it off — a human directly or a runtime agent on their behalf).

    Cross-user visible (Principle 1): everyone can view campaigns by user or
    all together; writes stay attributed to ``owner``. Lifecycle (status +
    pause/resume/cancel) is owned here in ``mail`` and paced by ``throttle``.
    """

    __tablename__ = "mail_campaigns"
    __table_args__ = (Index("ix_mail_campaigns_owner", "owner"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner: Mapped[str] = mapped_column(String(64), nullable=False)
    # fm_origin of the initiating principal: "user" or "agent".
    origin: Mapped[str] = mapped_column(String(16), nullable=False, default="user")
    # azp/client that performed the last exchange (machine actor), for
    # "alice (via agent)" attribution.
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=CAMPAIGN_DRAFT)
    send_strategy: Mapped[str] = mapped_column(String(16), nullable=False, default="balanced")
    # {"per_domain_daily": int, ...}
    throttle: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # Message template. Not in the plan's field list but required to actually
    # send; supports {{name}} / {{email}} substitution.
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    body_html: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    sources: Mapped[list["CampaignSource"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", passive_deletes=True
    )
    recipients: Mapped[list["CampaignRecipient"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", passive_deletes=True
    )
    messages: Mapped[list["CampaignMessage"]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan", passive_deletes=True
    )


class CampaignSource(Base):
    """One search result list feeding a campaign. A campaign is *continuable*:
    add another source later and its recipients are merged with dedupe +
    suppression re-applied. ``search_id`` is an opaque external reference into
    the search service (mail never calls search); the caller supplies the
    resolved recipients when adding the source."""

    __tablename__ = "mail_campaign_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("mail_campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    search_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    label: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    added_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    recipient_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    added_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    campaign: Mapped[Campaign] = relationship(back_populates="sources")


class CampaignRecipient(Base):
    """The merged, deduped recipient pool for a campaign. Within-campaign
    dedupe is enforced by the unique ``(campaign_id, email)`` constraint;
    suppression status is (re)evaluated authoritatively at send time."""

    __tablename__ = "mail_campaign_recipients"
    __table_args__ = (
        UniqueConstraint("campaign_id", "email", name="uq_campaign_recipient_email"),
        Index("ix_campaign_recipients_status", "campaign_id", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int] = mapped_column(
        ForeignKey("mail_campaigns.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("mail_campaign_sources.id", ondelete="SET NULL"), nullable=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    apollo_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=RECIPIENT_PENDING)
    suppressed_reason: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    campaign: Mapped[Campaign] = relationship(back_populates="recipients")


class CampaignMessage(Base):
    """Per-send log — the record of everything sent and the basis for
    dedupe/suppression + the global contacted set. Never deleted.

    ``campaign_id`` is nullable: a one-off ``send_message`` (outside any
    campaign) records a row with ``campaign_id=NULL`` so it counts toward the
    GLOBAL per-domain daily cap and the cross-campaign contacted/suppression set
    exactly like a campaign send (an agent cannot loop ``send_message`` to
    bulk-mail around the campaign gates)."""

    __tablename__ = "mail_campaign_messages"
    __table_args__ = (
        Index("ix_campaign_messages_person", "person_email"),
        Index("ix_campaign_messages_domain_sent", "domain", "sent_at"),
        Index("ix_campaign_messages_campaign", "campaign_id", "sent_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    campaign_id: Mapped[int | None] = mapped_column(
        ForeignKey("mail_campaigns.id", ondelete="CASCADE"), nullable=True, index=True
    )
    recipient_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    person_email: Mapped[str] = mapped_column(String(320), nullable=False)
    person_apollo_id: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    # Sending mailbox + its domain.
    account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    gmail_message_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    thread_id: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    # sent | failed
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="sent")
    error: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    campaign: Mapped[Campaign] = relationship(back_populates="messages")
