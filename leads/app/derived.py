"""Derive the six top-level index fields from merged Apollo payloads.

``apollo_responses`` remains the source of truth (P9). These fields are a derived
index recomputed from the merged responses on every write, with raw values taken
verbatim from the Apollo payload (no UI shaping — leads stays unopinionated;
normalization for display lives in ``search/``).

Per-FIELD payload precedence (not per-payload): for each field we walk the stored
payloads in the same precedence order the embeddings pipeline uses
(person: MATCH > BY_ID > SEARCH; org: BY_ID > ENRICH > SEARCH) and take the first
non-empty value. So a phone that only appears in the async MATCH webhook payload
still lands even when the best payload for ``name`` is the BY_ID enrich.

Fields (per the plan table):
- ``name``              — person only: ``name`` else ``first_name + last_name``
- ``title``             — person only: ``title``
- ``company_apollo_id`` — person only: ``organization_id`` else ``organization.id`` (the raw
  Apollo org id — the RESOLUTION KEY, not the canonical link). The canonical
  ``company_id`` (the org document's Mongo ``_id``) is resolved by the write
  paths via ``resolve_company_ids`` — derivation stays pure/sync.
- ``email``             — person only, placeholder-aware (``email_not_unlocked@…`` => absent)
- ``phone``             — person: first ``phone_numbers[].sanitized_number``; org: ``phone``/``sanitized_phone``
- ``linkedin``          — person + org: ``linkedin_url``

``derive_top_fields`` returns ONLY the non-null keys, so a caller can merge it into
a Mongo ``$set`` without ever regressing an existing value to null.
"""

from __future__ import annotations

from typing import Any

from app.apollo_endpoints import (
    ORG_BY_ID,
    ORG_ENRICH,
    ORG_SEARCH,
    ORG_SEARCH_LEGACY,
    PERSON_BY_ID,
    PERSON_MATCH,
    PERSON_SEARCH,
    unwrap_organization_payload,
    unwrap_person_payload,
)

# Same precedence chains as app/embeddings.py (highest first).
_PERSON_PRIORITY = (PERSON_MATCH, PERSON_BY_ID, PERSON_SEARCH)
_ORG_PRIORITY = (ORG_BY_ID, ORG_ENRICH, ORG_SEARCH, ORG_SEARCH_LEGACY)

# Apollo returns this local-part when a contact's email is locked / not revealed.
# Treat it as absent (mirrors search's contact-signal handling, re-implemented here
# so leads never imports from search).
_EMAIL_PLACEHOLDER_PREFIX = "email_not_unlocked@"


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _ordered_payloads(
    responses: dict[str, Any],
    priority: tuple[str, ...],
    unwrap,
) -> list[dict[str, Any]]:
    """Unwrapped payloads in precedence order, then any remaining ones."""
    ordered: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in priority:
        entry = responses.get(key)
        if isinstance(entry, dict):
            data = entry.get("data")
            if isinstance(data, dict) and data:
                ordered.append(unwrap(data))
        seen.add(key)
    for key, entry in responses.items():
        if key in seen:
            continue
        if isinstance(entry, dict):
            data = entry.get("data")
            if isinstance(data, dict) and data:
                ordered.append(unwrap(data))
    return ordered


def _first(payloads: list[dict[str, Any]], extractor) -> str | None:
    for payload in payloads:
        value = extractor(payload)
        if value is not None:
            return value
    return None


def _person_name(person: dict[str, Any]) -> str | None:
    name = _clean_str(person.get("name"))
    if name:
        return name
    parts = [_clean_str(person.get("first_name")), _clean_str(person.get("last_name"))]
    joined = " ".join(part for part in parts if part)
    return joined or None


def _person_title(person: dict[str, Any]) -> str | None:
    return _clean_str(person.get("title"))


def _person_company_id(person: dict[str, Any]) -> str | None:
    company_id = _clean_str(person.get("organization_id"))
    if company_id:
        return company_id
    org = person.get("organization")
    if isinstance(org, dict):
        return _clean_str(org.get("id"))
    return None


def _person_email(person: dict[str, Any]) -> str | None:
    email = _clean_str(person.get("email"))
    if not email:
        return None
    if email.lower().startswith(_EMAIL_PLACEHOLDER_PREFIX):
        return None
    return email


def _person_phone(person: dict[str, Any]) -> str | None:
    numbers = person.get("phone_numbers")
    if isinstance(numbers, list):
        for number in numbers:
            if isinstance(number, dict):
                value = (
                    _clean_str(number.get("sanitized_number"))
                    or _clean_str(number.get("raw_number"))
                    or _clean_str(number.get("number"))
                )
                if value:
                    return value
            else:
                value = _clean_str(number)
                if value:
                    return value
    return None


def _linkedin(payload: dict[str, Any]) -> str | None:
    return _clean_str(payload.get("linkedin_url"))


def _org_phone(org: dict[str, Any]) -> str | None:
    return _clean_str(org.get("phone")) or _clean_str(org.get("sanitized_phone"))


def derive_top_fields(entity_type: str, responses: dict[str, Any] | None) -> dict[str, str]:
    """Best-payload extraction of the top-level fields; returns only non-null keys.

    ``responses`` is the merged ``apollo_responses`` map (endpoint-keyed, already
    normalized via ``responses_from_doc``). Uses per-field precedence so a
    higher-quality payload always wins over a search hit, but a field present only
    in a lower-precedence payload is still picked up.
    """
    responses = responses or {}
    out: dict[str, str] = {}

    if (entity_type or "person") == "organization":
        payloads = _ordered_payloads(responses, _ORG_PRIORITY, unwrap_organization_payload)
        phone = _first(payloads, _org_phone)
        if phone is not None:
            out["phone"] = phone
        linkedin = _first(payloads, _linkedin)
        if linkedin is not None:
            out["linkedin"] = linkedin
        return out

    payloads = _ordered_payloads(responses, _PERSON_PRIORITY, unwrap_person_payload)
    name = _first(payloads, _person_name)
    if name is not None:
        out["name"] = name
    title = _first(payloads, _person_title)
    if title is not None:
        out["title"] = title
    company_apollo_id = _first(payloads, _person_company_id)
    if company_apollo_id is not None:
        out["company_apollo_id"] = company_apollo_id
    email = _first(payloads, _person_email)
    if email is not None:
        out["email"] = email
    phone = _first(payloads, _person_phone)
    if phone is not None:
        out["phone"] = phone
    linkedin = _first(payloads, _linkedin)
    if linkedin is not None:
        out["linkedin"] = linkedin
    return out
