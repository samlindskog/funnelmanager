"""Records store: per-user interaction records synced from the source services.

Every table carries provenance back to its source-of-truth row (P9: this store
is a queryable INDEX with denormalized copies of a few columns, never a second
source of truth), an ``owner`` attribution column (P1: attribution, not a read
filter), an ``occurred_at`` event timestamp, and pgvector embedding columns for
the fuzzy-matchable text columns (NULL until embedded; embedded lazily when
OPENAI_API_KEY is configured).
"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import get_settings

_DIM = get_settings().openai_embedding_dimensions


class Base(DeclarativeBase):
    pass


class _RecordMixin:
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    owner: Mapped[str] = mapped_column(String(64), default="", index=True)
    # Source event time (search ran / message sent / session created).
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    # Stamped when this record has been distilled into a Graphiti episode.
    graph_ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SearchRecord(_RecordMixin, Base):
    """A search someone ran (source of truth: search service, search_history)."""

    __tablename__ = "records_searches"

    source_id: Mapped[int] = mapped_column(Integer, unique=True)  # search_history.id
    query: Mapped[str] = mapped_column(Text, default="")
    entity_type: Mapped[str] = mapped_column(String(32), default="")
    total_results: Mapped[int] = mapped_column(Integer, default=0)
    query_emb: Mapped[list[float] | None] = mapped_column(Vector(_DIM))


class MailCampaignRecord(_RecordMixin, Base):
    """An email campaign (source of truth: mail service, mail_campaigns)."""

    __tablename__ = "records_mail_campaigns"

    source_id: Mapped[int] = mapped_column(Integer, unique=True)  # mail_campaigns.id
    name: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="")
    send_strategy: Mapped[str] = mapped_column(String(16), default="")
    name_emb: Mapped[list[float] | None] = mapped_column(Vector(_DIM))
    subject_emb: Mapped[list[float] | None] = mapped_column(Vector(_DIM))


class MailMessageRecord(_RecordMixin, Base):
    """An archived mail message (source of truth: mail service, mail_messages).

    ``owner`` is the user who connected the mailbox; ``direction`` is inferred
    (SENT label / from == account email => outbound).
    """

    __tablename__ = "records_mail_messages"
    __table_args__ = (
        UniqueConstraint("account_id", "gmail_id", name="uq_records_mail_account_gmail"),
        Index("ix_records_mail_owner_dir_date", "owner", "direction", "occurred_at"),
    )

    account_id: Mapped[int] = mapped_column(Integer)
    gmail_id: Mapped[str] = mapped_column(String(32))
    thread_id: Mapped[str] = mapped_column(String(32), default="")
    account_email: Mapped[str] = mapped_column(String(320), default="", index=True)
    direction: Mapped[str] = mapped_column(String(8), default="inbound")  # inbound|outbound
    counterparty_email: Mapped[str] = mapped_column(String(320), default="", index=True)
    counterparty_name: Mapped[str] = mapped_column(Text, default="")
    subject: Mapped[str] = mapped_column(Text, default="")
    snippet: Mapped[str] = mapped_column(Text, default="")
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    subject_emb: Mapped[list[float] | None] = mapped_column(Vector(_DIM))
    snippet_emb: Mapped[list[float] | None] = mapped_column(Vector(_DIM))


class AgentSessionRecord(_RecordMixin, Base):
    """An agent session (source of truth: agents service, agent_session)."""

    __tablename__ = "records_agent_sessions"

    source_id: Mapped[str] = mapped_column(String(64), unique=True)  # agent_session.id
    title: Mapped[str] = mapped_column(Text, default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    last_activity: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Highest agent_message.seq already distilled into the owner's session graph.
    graph_seq: Mapped[int] = mapped_column(Integer, default=0)
    title_emb: Mapped[list[float] | None] = mapped_column(Vector(_DIM))


class SyncState(Base):
    """Per-source watermark + status (drives incremental polling)."""

    __tablename__ = "sync_state"

    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    watermark: Mapped[str] = mapped_column(Text, default="{}")  # JSON blob
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str] = mapped_column(Text, default="")
    last_new: Mapped[int] = mapped_column(Integer, default=0)
    duration_ms: Mapped[float] = mapped_column(Float, default=0.0)
