"""The exposed logical schema of the records store — the single source of truth
for what ``knowledge_query_records`` can see and filter on.

Every queryable record type declares its columns as either **exact** (SQL
predicates: eq/in/contains for strings, before/after for datetimes, eq/gte/lte
for numbers) or **fuzzy** (a passage is embedded and matched by cosine
similarity against the column's stored embedding, above a threshold). The
registry drives three things identically: request validation, SQL compilation
(no free-form column names ever reach SQL), and the ``knowledge_schema``
describe output an agent reads before composing a query.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.models import (
    AgentSessionRecord,
    MailCampaignRecord,
    MailMessageRecord,
    SearchRecord,
)

EXACT_OPS: dict[str, list[str]] = {
    "string": ["eq", "in", "contains"],
    "integer": ["eq", "gte", "lte"],
    "datetime": ["before", "after"],
    "boolean": ["eq"],
}


@dataclass(frozen=True)
class ExactCol:
    attr: str
    type: str  # string | integer | datetime | boolean
    description: str

    @property
    def ops(self) -> list[str]:
        return EXACT_OPS[self.type]


@dataclass(frozen=True)
class FuzzyCol:
    attr: str
    emb_attr: str
    description: str


@dataclass(frozen=True)
class RecordType:
    name: str
    model: type
    description: str
    provenance: str  # human-readable format of the provenance ref
    exact: dict[str, ExactCol] = field(default_factory=dict)
    fuzzy: dict[str, FuzzyCol] = field(default_factory=dict)

    def output_columns(self) -> list[str]:
        return [*self.exact.keys(), *self.fuzzy.keys()]


_COMMON_EXACT = {
    "owner": ExactCol("owner", "string", "username the record is attributed to (P1: attribution, not hiding)"),
    "occurred_at": ExactCol("occurred_at", "datetime", "when the underlying event happened (ISO 8601)"),
}

RECORD_TYPES: dict[str, RecordType] = {
    "searches": RecordType(
        name="searches",
        model=SearchRecord,
        description="Apollo/semantic searches users have run (source of truth: search service history).",
        provenance="search:{source_id}",
        exact={
            **_COMMON_EXACT,
            "entity_type": ExactCol("entity_type", "string", "'people' or 'companies'"),
            "total_results": ExactCol("total_results", "integer", "result count reported by the search"),
        },
        fuzzy={
            "query": FuzzyCol("query", "query_emb", "the search query text"),
        },
    ),
    "mail_campaigns": RecordType(
        name="mail_campaigns",
        model=MailCampaignRecord,
        description="Email campaigns (source of truth: mail service).",
        provenance="campaign:{source_id}",
        exact={
            **_COMMON_EXACT,
            "status": ExactCol("status", "string", "draft|running|paused|completed|cancelled"),
            "send_strategy": ExactCol("send_strategy", "string", "balanced|sequential"),
        },
        fuzzy={
            "name": FuzzyCol("name", "name_emb", "campaign name"),
            "subject": FuzzyCol("subject", "subject_emb", "campaign email subject"),
        },
    ),
    "mail_messages": RecordType(
        name="mail_messages",
        model=MailMessageRecord,
        description=(
            "Archived mailbox messages, both directions (source of truth: mail archive). "
            "direction='outbound' means the connected mailbox sent it — use with "
            "occurred_at after/before for 'everyone I messaged since X'."
        ),
        provenance="mail:{account_id}:{gmail_id}",
        exact={
            **_COMMON_EXACT,
            "direction": ExactCol("direction", "string", "'outbound' (sent by the mailbox) or 'inbound'"),
            "account_email": ExactCol("account_email", "string", "the connected mailbox address"),
            "counterparty_email": ExactCol("counterparty_email", "string", "the other side's email address"),
            "counterparty_name": ExactCol(
                "counterparty_name", "string", "display name of the counterparty (substring match via contains)"
            ),
            "is_deleted": ExactCol("is_deleted", "boolean", "deleted on the Google side (archive keeps it)"),
        },
        fuzzy={
            "subject": FuzzyCol("subject", "subject_emb", "message subject"),
            "snippet": FuzzyCol("snippet", "snippet_emb", "message preview snippet"),
        },
    ),
    "agent_sessions": RecordType(
        name="agent_sessions",
        model=AgentSessionRecord,
        description="Runtime AI agent sessions (source of truth: agents service).",
        provenance="agent-session:{source_id}",
        exact={
            **_COMMON_EXACT,
            "model": ExactCol("model", "string", "LLM model the session used"),
            "last_activity": ExactCol("last_activity", "datetime", "last turn/update in the session"),
        },
        fuzzy={
            "title": FuzzyCol("title", "title_emb", "session title"),
        },
    ),
}

def describe(default_threshold: float) -> dict[str, Any]:
    """The machine-readable schema handed to agents via knowledge_schema."""
    types: dict[str, Any] = {}
    for rt in RECORD_TYPES.values():
        types[rt.name] = {
            "description": rt.description,
            "provenance": rt.provenance,
            "columns": {
                **{
                    name: {"kind": "exact", "type": col.type, "ops": col.ops, "description": col.description}
                    for name, col in rt.exact.items()
                },
                **{
                    name: {
                        "kind": "fuzzy",
                        "type": "string",
                        "ops": ["passage (+ optional threshold)"],
                        "description": col.description
                        + " — matched by embedding cosine similarity against your passage",
                    }
                    for name, col in rt.fuzzy.items()
                },
            },
        }
    return {
        "record_types": types,
        "fuzzy": {
            "default_threshold": default_threshold,
            "semantics": (
                "For fuzzy columns pass {'passage': <text to embed>, 'threshold': <optional 0..1>}; "
                "rows match when cosine_similarity(column_embedding, passage_embedding) >= threshold. "
                "Rows not yet embedded never match fuzzy filters."
            ),
        },
        "exact_semantics": {
            "string": "{'eq': v} | {'in': [v,..]} | {'contains': substring (case-insensitive)}",
            "integer": "{'eq': n} | {'gte': n} | {'lte': n}",
            "datetime": "{'before': iso8601} | {'after': iso8601} (either or both)",
            "boolean": "{'eq': true|false}",
        },
    }
