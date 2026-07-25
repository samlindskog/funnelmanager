from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class SearchHistory(Base):
    __tablename__ = "search_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Owning profile (auth username); '' on legacy rows, which match nobody.
    # NB (principle 1 / cross-user history): `username` still *attributes* a row
    # to its initiating human — writes record the owner — but READ endpoints no
    # longer filter by it. Any principal holding the search-access role may
    # browse every user's history; row ownership is not an access gate.
    username: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default="", index=True
    )
    # Attribution of the initiating request (fm_origin): "user" (a human) or
    # "agent" (a runtime AI agent acting on the human's behalf). Rendered as
    # "alice (via agent)" when origin == "agent".
    origin: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user", server_default="user"
    )
    # The machine client that performed the acting exchange (azp) — pairs with
    # `origin` for attribution. Empty for a direct human/browser call.
    actor: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", server_default=""
    )
    query: Mapped[str] = mapped_column(String(512), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)  # people | companies
    page: Mapped[int] = mapped_column(Integer, default=1)
    per_page: Mapped[int] = mapped_column(Integer, default=100)
    total_results: Mapped[int] = mapped_column(Integer, default=0)
    # Kept as a hydrated page cache only; Apollo payloads live in Mongo via leads.
    results_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    # Original search request params (audit / reopen)
    search_params_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    results: Mapped[list["SearchResult"]] = relationship(
        back_populates="search",
        cascade="all, delete-orphan",
        order_by="SearchResult.position",
    )


class SearchResult(Base):
    __tablename__ = "search_results"
    __table_args__ = (
        UniqueConstraint("search_id", "external_id", name="uq_search_results_search_external"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    search_id: Mapped[int] = mapped_column(
        ForeignKey("search_history.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)  # Mongo lead `_id`
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    search: Mapped[SearchHistory] = relationship(back_populates="results")
