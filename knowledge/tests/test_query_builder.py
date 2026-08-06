"""Query-engine validation + exact-clause compilation (no DB, no OpenAI)."""

import asyncio
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.query import _exact_clause, _parse_dt, run_query
from app.schema_registry import RECORD_TYPES


class _EmptyResult:
    def all(self):
        return []


class _FakeSession:
    """Captures the statement and returns no rows."""

    def __init__(self):
        self.stmt = None

    async def execute(self, stmt):
        self.stmt = stmt
        return _EmptyResult()


def _run(coro):
    return asyncio.run(coro)


def test_unknown_record_type_rejected():
    with pytest.raises(HTTPException) as exc:
        _run(run_query(_FakeSession(), "nope", {}, None))
    assert exc.value.status_code == 422
    assert "knowledge_schema" in str(exc.value.detail)


def test_unknown_column_rejected():
    with pytest.raises(HTTPException) as exc:
        _run(run_query(_FakeSession(), "searches", {"bogus": {"eq": 1}}, None))
    assert exc.value.status_code == 422


def test_wrong_op_for_type_rejected():
    with pytest.raises(HTTPException) as exc:
        _run(run_query(_FakeSession(), "searches", {"owner": {"gte": 3}}, None))
    assert exc.value.status_code == 422


def test_fuzzy_without_passage_rejected():
    with pytest.raises(HTTPException) as exc:
        _run(run_query(_FakeSession(), "searches", {"query": {"threshold": 0.5}}, None))
    assert exc.value.status_code == 422


def test_fuzzy_threshold_out_of_range_rejected():
    with pytest.raises(HTTPException) as exc:
        _run(run_query(_FakeSession(), "searches", {"query": {"passage": "x", "threshold": 3}}, None))
    assert exc.value.status_code == 422


def test_exact_only_query_compiles_and_runs():
    session = _FakeSession()
    out = _run(
        run_query(
            session,
            "mail_messages",
            {
                "direction": {"eq": "outbound"},
                "occurred_at": {"after": "2026-07-01T00:00:00Z", "before": "2026-08-01T00:00:00Z"},
                "counterparty_email": {"contains": "@acme.com"},
            },
            25,
        )
    )
    assert out["returned"] == 0 and out["rows"] == [] and out["limit"] == 25
    compiled = str(session.stmt)
    assert "direction" in compiled and "occurred_at" in compiled and "counterparty_email" in compiled


def test_limit_capped_to_max():
    session = _FakeSession()
    out = _run(run_query(session, "searches", {}, 10_000))
    from app.config import get_settings

    assert out["limit"] == get_settings().query_limit_max


def test_parse_dt_accepts_z_suffix_and_rejects_garbage():
    dt = _parse_dt("2026-07-01T00:00:00Z", "occurred_at")
    assert dt == datetime(2026, 7, 1, tzinfo=timezone.utc)
    with pytest.raises(HTTPException):
        _parse_dt("yesterday-ish", "occurred_at")


def test_exact_clause_datetime_ops():
    rt = RECORD_TYPES["mail_messages"]
    clauses = _exact_clause(rt.model, "occurred_at", rt.exact["occurred_at"], {"before": "2026-01-01"})
    assert len(clauses) == 1
