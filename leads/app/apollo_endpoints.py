"""Apollo endpoint group keys for stored lead responses."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

# Full relative paths under https://api.apollo.io
PERSON_SEARCH = "/api/v1/mixed_people/api_search"
PERSON_BY_ID = "/api/v1/people/{id}"
PERSON_MATCH = "/api/v1/people/match"

# api_search = Apollo's free teaser variant (same switch people search already
# uses); the plain /search variant consumes credits per returned record — a bulk
# Prospect run burned ~100k company-records' credits on it (2026-08-12).
ORG_SEARCH = "/api/v1/mixed_companies/api_search"
# Legacy key: docs written before the api_search switch store this endpoint key.
ORG_SEARCH_LEGACY = "/api/v1/mixed_companies/search"
ORG_BY_ID = "/api/v1/organizations/{id}"
ORG_ENRICH = "/api/v1/organizations/enrich"

PERSON_DISPLAY_PRIORITY = (PERSON_MATCH, PERSON_BY_ID, PERSON_SEARCH)
ORG_DISPLAY_PRIORITY = (ORG_BY_ID, ORG_ENRICH, ORG_SEARCH, ORG_SEARCH_LEGACY)

EMPTY_ENRICHED_FLAGS: dict[str, bool] = {
    "linkedin": False,
    "email": False,
    "phone": False,
}

# Older short keys → current full relative paths
_LEGACY_ENDPOINT_KEYS: dict[str, str] = {
    "mixed_people/api_search": PERSON_SEARCH,
    "people/{id}": PERSON_BY_ID,
    "people/match": PERSON_MATCH,
    "mixed_companies/search": ORG_SEARCH_LEGACY,
    "mixed_companies/api_search": ORG_SEARCH,
    "organizations/{id}": ORG_BY_ID,
    "organizations/enrich": ORG_ENRICH,
    "api/v1/mixed_people/api_search": PERSON_SEARCH,
    "api/v1/people/{id}": PERSON_BY_ID,
    "api/v1/people/match": PERSON_MATCH,
    "api/v1/mixed_companies/search": ORG_SEARCH_LEGACY,
    "api/v1/mixed_companies/api_search": ORG_SEARCH,
    "api/v1/organizations/{id}": ORG_BY_ID,
    "api/v1/organizations/enrich": ORG_ENRICH,
}


def endpoint_entry(data: dict[str, Any], received_at: datetime) -> dict[str, Any]:
    return {"received_at": received_at, "data": data}


def normalize_response_keys(responses: dict[str, Any]) -> dict[str, Any]:
    """Rewrite legacy endpoint keys to full /api/v1/... paths."""
    if not responses:
        return {}
    out: dict[str, Any] = {}
    for key, entry in responses.items():
        canonical = _LEGACY_ENDPOINT_KEYS.get(str(key), str(key))
        if not canonical.startswith("/"):
            canonical = f"/{canonical.lstrip('/')}"
        # Prefer keeping an existing canonical entry if both legacy + new exist
        if canonical in out and str(key) != canonical:
            continue
        out[canonical] = entry
    return out


def empty_enriched_flags() -> dict[str, bool]:
    return dict(EMPTY_ENRICHED_FLAGS)


def merge_enriched_flags(
    existing: dict[str, bool] | None,
    *,
    linkedin: bool = False,
    email: bool = False,
    phone: bool = False,
) -> dict[str, bool]:
    """OR-merge enrichment job flags (once true, stays true)."""
    base = empty_enriched_flags()
    if existing:
        base["linkedin"] = bool(existing.get("linkedin"))
        base["email"] = bool(existing.get("email"))
        base["phone"] = bool(existing.get("phone"))
    if linkedin:
        base["linkedin"] = True
    if email:
        base["email"] = True
    if phone:
        base["phone"] = True
    return base


def any_enriched(flags: dict[str, bool] | None) -> bool:
    if not flags:
        return False
    return bool(flags.get("linkedin") or flags.get("email") or flags.get("phone"))


def normalize_apollo_enriched(
    doc: dict[str, Any],
    *,
    responses: dict[str, Any] | None = None,
) -> dict[str, bool]:
    """Normalize legacy bool / partial maps into {linkedin, email, phone}."""
    raw = doc.get("apollo_enriched")
    if isinstance(raw, dict):
        return merge_enriched_flags(
            None,
            linkedin=bool(raw.get("linkedin")),
            email=bool(raw.get("email")),
            phone=bool(raw.get("phone")),
        )

    response_map = responses
    if response_map is None:
        raw_map = doc.get("apollo_responses")
        response_map = (
            normalize_response_keys(raw_map) if isinstance(raw_map, dict) else {}
        )

    if raw is True:
        # Legacy single bool: linkedin only when complete-person response exists.
        return merge_enriched_flags(
            None,
            linkedin=PERSON_BY_ID in response_map
            or "https://api.apollo.io/api/v1/people/{id}" in response_map
            or ORG_BY_ID in response_map
            or ORG_ENRICH in response_map,
            email=False,
            phone=False,
        )

    return empty_enriched_flags()


def responses_from_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Return apollo_responses map, migrating legacy apollo_response / keys if needed."""
    raw_map = doc.get("apollo_responses")
    if isinstance(raw_map, dict) and raw_map:
        return normalize_response_keys(raw_map)

    legacy = doc.get("apollo_response")
    if not isinstance(legacy, dict) or not legacy:
        return {}

    entity = doc.get("entity_type") or "person"
    flags = normalize_apollo_enriched(doc, responses={})
    enriched = any_enriched(flags)
    if entity == "organization":
        key = ORG_BY_ID if enriched else ORG_SEARCH
    else:
        key = PERSON_BY_ID if enriched else PERSON_SEARCH

    received = doc.get("updated_at") or doc.get("created_at") or datetime.now(timezone.utc)
    return {key: endpoint_entry(legacy, received)}


def entity_payload_from_responses(
    responses: dict[str, Any],
    entity_type: Literal["person", "organization"],
) -> dict[str, Any] | None:
    """Pick the best stored payload for UI normalization (prefer enriched)."""
    priority = ORG_DISPLAY_PRIORITY if entity_type == "organization" else PERSON_DISPLAY_PRIORITY
    for key in priority:
        entry = responses.get(key)
        if not isinstance(entry, dict):
            continue
        data = entry.get("data")
        if isinstance(data, dict) and data:
            return data
    for entry in responses.values():
        if not isinstance(entry, dict):
            continue
        data = entry.get("data")
        if isinstance(data, dict) and data:
            return data
    return None


def unwrap_person_payload(data: dict[str, Any]) -> dict[str, Any]:
    person = data.get("person")
    if isinstance(person, dict):
        return person
    return data


def unwrap_organization_payload(data: dict[str, Any]) -> dict[str, Any]:
    for key in ("organization", "account"):
        value = data.get(key)
        if isinstance(value, dict):
            return value
    return data
