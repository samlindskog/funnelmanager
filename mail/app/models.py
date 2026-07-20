from datetime import datetime

from sqlalchemy import (
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
