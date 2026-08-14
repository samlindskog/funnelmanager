"""Negative-resolve cache: remember domains Apollo genuinely has no org for.

A workflow re-upload otherwise re-burns ~1 Apollo org-search export credit per
unknown/"not found" domain on every pass. A miss marker (Mongo ``resolve_misses``,
unique on ``value`` + TTL on ``created_at`` — see ``database.init_db``) lets the
resolve/org-search path short-circuit to the same empty result with ZERO Apollo
calls until the marker expires.

CRITICAL non-poisoning rule (enforced at the CALL SITE, not here): a marker is
minted ONLY for a genuine Apollo *empty/not-found* result — never on a credit
refusal (422), rate limit (429), timeout, or 5xx. Those raise, so "mint only after
a successful Apollo call that returned zero orgs" is the safe discipline.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.database import RESOLVE_MISSES_COLLECTION

logger = logging.getLogger(__name__)


def normalize_resolve_key(value: str) -> str:
    """Normalize a domain into the marker key (lowercase bare domain).

    Strips surrounding whitespace, a leading scheme, a leading ``www.``, and any
    path/query so ``https://WWW.Acme.com/careers`` and ``acme.com`` share one key.
    Returns "" for a blank/unusable value (the caller then skips the cache).
    """
    cleaned = (value or "").strip().lower()
    if not cleaned:
        return ""
    if "://" in cleaned:
        cleaned = cleaned.split("://", 1)[1]
    if cleaned.startswith("www."):
        cleaned = cleaned[4:]
    # Drop path / query / fragment — keep only the host.
    for sep in ("/", "?", "#"):
        if sep in cleaned:
            cleaned = cleaned.split(sep, 1)[0]
    return cleaned.strip()


async def is_resolve_miss(db: AsyncIOMotorDatabase, value: str) -> bool:
    """True if a (non-expired) miss marker exists for ``value``.

    Best-effort: a query failure returns False (treat as "unknown", let the caller
    fall through to Apollo) rather than raising — the cache must never break resolve.
    """
    key = normalize_resolve_key(value)
    if not key:
        return False
    try:
        doc = await db[RESOLVE_MISSES_COLLECTION].find_one({"value": key}, {"_id": 1})
    except Exception:
        logger.exception("resolve-miss lookup failed for %s; treating as unknown", key)
        return False
    return doc is not None


async def mark_resolve_miss(db: AsyncIOMotorDatabase, value: str) -> None:
    """Atomically upsert a miss marker (P9: single upsert, never find-then-insert).

    ``$setOnInsert`` keeps ``created_at`` from the FIRST miss, so the marker (and its
    TTL clock) is not refreshed by repeated misses — Apollo is re-checked once per
    TTL regardless of upload frequency. Call ONLY after a genuine empty Apollo result.
    """
    key = normalize_resolve_key(value)
    if not key:
        return
    now = datetime.now(timezone.utc)
    try:
        await db[RESOLVE_MISSES_COLLECTION].update_one(
            {"value": key},
            {"$setOnInsert": {"value": key, "created_at": now}},
            upsert=True,
        )
    except Exception:
        # Never let a cache write break the resolve response.
        logger.exception("resolve-miss mint failed for %s", key)


async def clear_resolve_miss(db: AsyncIOMotorDatabase, value: str) -> None:
    """Delete any miss marker for ``value`` (call on a live Apollo HIT)."""
    key = normalize_resolve_key(value)
    if not key:
        return
    try:
        # delete_many, not delete_one: if the unique index ever degraded and left a
        # duplicate marker, a single delete could leave one surviving a clear.
        await db[RESOLVE_MISSES_COLLECTION].delete_many({"value": key})
    except Exception:
        logger.exception("resolve-miss clear failed for %s", key)


async def learn_domain_alias(
    db: AsyncIOMotorDatabase, org_mongo_id: str, domain: str
) -> None:
    """Record that ``domain`` resolves to an org whose canonical ``domain`` differs.

    An org can exist in Mongo without carrying the domain a workflow queried (an
    ALIAS domain), so the free resolve 404s and the billed org-search re-bills every
    pass. On an org-search HIT we ``$addToSet`` the queried domain onto the matched
    org's ``alias_domains`` (positive knowledge — no TTL), so the FREE resolve path
    matches it next time. Atomic + best-effort (a failure never breaks the search).
    """
    key = normalize_resolve_key(domain)
    if not key or not org_mongo_id:
        return
    try:
        oid = ObjectId(str(org_mongo_id))
    except Exception:
        return
    try:
        await db.leads.update_one(
            {"_id": oid, "entity_type": "organization"},
            {"$addToSet": {"alias_domains": key}},
        )
    except Exception:
        logger.exception("resolve-alias learn failed (%s -> %s)", key, org_mongo_id)
