"""The deterministic records-query engine behind knowledge_query_records.

Compiles a schema-validated filter spec into one SQL statement over the records
store: exact predicates become WHERE clauses, fuzzy predicates become pgvector
cosine-similarity terms with a threshold floor. Column names are resolved
through the schema registry only — nothing caller-supplied ever reaches SQL as
an identifier. Results carry provenance refs and per-fuzzy-column scores.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.embeddings import embed_query
from app.schema_registry import RECORD_TYPES, ExactCol, RecordType


def _bad(msg: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"{msg}. Call knowledge_schema for the queryable schema.")


def _parse_dt(value: Any, col: str) -> datetime:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise _bad(f"column '{col}': {value!r} is not ISO 8601") from exc


def _exact_clause(model: type, col_name: str, col: ExactCol, spec: dict[str, Any]) -> list[Any]:
    attr = getattr(model, col.attr)
    clauses: list[Any] = []
    unknown = set(spec) - set(col.ops)
    if unknown:
        raise _bad(f"column '{col_name}' ({col.type}) does not support ops {sorted(unknown)} (supported: {col.ops})")
    if not spec:
        raise _bad(f"column '{col_name}': empty filter spec")
    if "eq" in spec:
        v: Any = spec["eq"]
        if col.type == "datetime":
            v = _parse_dt(v, col_name)
        clauses.append(attr == v)
    if "in" in spec:
        if not isinstance(spec["in"], list) or not spec["in"]:
            raise _bad(f"column '{col_name}': 'in' takes a non-empty list")
        clauses.append(attr.in_(spec["in"]))
    if "contains" in spec:
        clauses.append(attr.ilike(f"%{spec['contains']}%"))
    if "gte" in spec:
        clauses.append(attr >= spec["gte"])
    if "lte" in spec:
        clauses.append(attr <= spec["lte"])
    if "before" in spec:
        clauses.append(attr < _parse_dt(spec["before"], col_name))
    if "after" in spec:
        clauses.append(attr >= _parse_dt(spec["after"], col_name))
    return clauses


async def run_query(
    session: AsyncSession,
    record_type: str,
    filters: dict[str, Any] | None,
    limit: int | None,
) -> dict[str, Any]:
    cfg = get_settings()
    rt: RecordType | None = RECORD_TYPES.get(record_type)
    if rt is None:
        raise _bad(f"unknown record_type {record_type!r} (known: {sorted(RECORD_TYPES)})")
    filters = filters or {}
    if not isinstance(filters, dict):
        raise _bad("'filters' must be an object of column -> filter-spec")
    limit = min(int(limit or cfg.query_limit_default), cfg.query_limit_max)

    model = rt.model
    stmt: Select = select(model)
    fuzzy_score_cols: list[tuple[str, Any]] = []

    for col_name, spec in filters.items():
        if not isinstance(spec, dict):
            raise _bad(f"column '{col_name}': filter spec must be an object")
        if col_name in rt.exact:
            for clause in _exact_clause(model, col_name, rt.exact[col_name], spec):
                stmt = stmt.where(clause)
        elif col_name in rt.fuzzy:
            fz = rt.fuzzy[col_name]
            passage = spec.get("passage")
            if not passage or not str(passage).strip():
                raise _bad(f"fuzzy column '{col_name}' needs {{'passage': <text>}}")
            unknown = set(spec) - {"passage", "threshold"}
            if unknown:
                raise _bad(f"fuzzy column '{col_name}' supports only 'passage' and 'threshold' (got {sorted(unknown)})")
            threshold = float(spec.get("threshold", cfg.fuzzy_threshold_default))
            if not 0.0 <= threshold <= 1.0:
                raise _bad(f"fuzzy column '{col_name}': threshold must be within [0, 1]")
            vec = await embed_query(str(passage))
            emb_attr = getattr(model, fz.emb_attr)
            score = (1 - emb_attr.cosine_distance(vec)).label(f"score_{col_name}")
            stmt = stmt.where(emb_attr.is_not(None)).where(score >= threshold)
            stmt = stmt.add_columns(score)
            fuzzy_score_cols.append((col_name, score))
        else:
            raise _bad(f"record_type '{record_type}' has no column '{col_name}' (columns: {rt.output_columns()})")

    if fuzzy_score_cols:
        # Rank by combined similarity, best first.
        order = fuzzy_score_cols[0][1]
        for _, sc in fuzzy_score_cols[1:]:
            order = order + sc
        stmt = stmt.order_by(order.desc())
    else:
        stmt = stmt.order_by(getattr(model, "occurred_at").desc().nullslast())
    stmt = stmt.limit(limit + 1)

    result = await session.execute(stmt)
    raw = result.all()
    has_more = len(raw) > limit
    raw = raw[:limit]

    rows: list[dict[str, Any]] = []
    for row in raw:
        rec = row[0]
        out: dict[str, Any] = {"record_type": record_type}
        for name, col in rt.exact.items():
            v = getattr(rec, col.attr)
            out[name] = v.isoformat() if isinstance(v, datetime) else v
        for name, fz in rt.fuzzy.items():
            out[name] = getattr(rec, fz.attr)
        out["provenance"] = rt.provenance.format(**{
            k: getattr(rec, k) for k in ("source_id", "account_id", "gmail_id") if hasattr(rec, k)
        })
        if fuzzy_score_cols:
            out["scores"] = {
                name: round(float(row[i + 1]), 4) for i, (name, _) in enumerate(fuzzy_score_cols)
            }
        rows.append(out)

    return {
        "record_type": record_type,
        "returned": len(rows),
        "has_more": has_more,
        "limit": limit,
        "rows": rows,
    }
