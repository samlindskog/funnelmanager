"""Compact summaries of leads-service documents for MCP tool output.

Raw ``LeadOut`` docs carry every endpoint-keyed Apollo payload and can run to
hundreds of KB per lead. Tools return these summaries by default and only
include full payloads when explicitly asked (``include_raw=True``).

Display priority mirrors the search backend's ``display_payload_from_lead``:
prefer enriched endpoint payloads, fall back to search payloads.
"""

from __future__ import annotations

from typing import Any

# Apollo emits this local-part when a person's email is locked / not revealed. It
# is a placeholder, not a real address, and must be treated as absent — same
# constant/semantics leads applies in ``leads/app/derived.py`` (re-declared here,
# not imported, since MCP never imports from another service's app package).
_EMAIL_PLACEHOLDER_PREFIX = "email_not_unlocked@"


def _is_placeholder_email(value: str) -> bool:
    """True if ``value`` is Apollo's locked-email placeholder (case-insensitive)."""
    return value.strip().lower().startswith(_EMAIL_PLACEHOLDER_PREFIX)


PERSON_DISPLAY_ENDPOINTS = (
    "/api/v1/people/match",
    "/api/v1/people/{id}",
    "/api/v1/mixed_people/api_search",
)
ORG_DISPLAY_ENDPOINTS = (
    "/api/v1/organizations/{id}",
    "/api/v1/organizations/enrich",
    "/api/v1/mixed_companies/search",
)


def _endpoint_data(lead: dict[str, Any], endpoint: str) -> dict[str, Any] | None:
    responses = lead.get("apollo_responses")
    if not isinstance(responses, dict):
        return None
    entry = responses.get(endpoint)
    if not isinstance(entry, dict):
        return None
    data = entry.get("data")
    return data if isinstance(data, dict) else None


def _unwrap(data: dict[str, Any], entity_type: str) -> dict[str, Any]:
    if entity_type == "person":
        person = data.get("person")
        return person if isinstance(person, dict) else data
    for key in ("organization", "account"):
        nested = data.get(key)
        if isinstance(nested, dict):
            return nested
    return data


def _display_payload(lead: dict[str, Any]) -> dict[str, Any]:
    entity_type = str(lead.get("entity_type") or "person")
    priority = ORG_DISPLAY_ENDPOINTS if entity_type == "organization" else PERSON_DISPLAY_ENDPOINTS
    for endpoint in priority:
        data = _endpoint_data(lead, endpoint)
        if data:
            return _unwrap(data, entity_type)
    responses = lead.get("apollo_responses")
    if isinstance(responses, dict):
        for entry in responses.values():
            if not isinstance(entry, dict):
                continue
            data = entry.get("data")
            if isinstance(data, dict) and data:
                return _unwrap(data, entity_type)
    return {}


def _first_email(raw: dict[str, Any]) -> str | None:
    """First real email off a raw payload, placeholder-aware.

    A locked Apollo ``email_not_unlocked@…`` value is a placeholder, not an
    address — it is skipped here so it can never land in a summary (mirrors the
    placeholder-aware top-level derived ``email`` field this falls back from).
    """
    email = raw.get("email")
    if isinstance(email, str) and email.strip() and not _is_placeholder_email(email):
        return email.strip()
    emails = raw.get("emails")
    if isinstance(emails, list):
        for item in emails:
            if isinstance(item, str) and item.strip() and not _is_placeholder_email(item):
                return item.strip()
            if isinstance(item, dict):
                nested = item.get("email")
                if (
                    isinstance(nested, str)
                    and nested.strip()
                    and not _is_placeholder_email(nested)
                ):
                    return nested.strip()
    return None


def _first_phone(raw: dict[str, Any]) -> str | None:
    phones = raw.get("phone_numbers")
    if isinstance(phones, list):
        for item in phones:
            if isinstance(item, dict):
                for key in ("sanitized_number", "raw_number"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            elif isinstance(item, str) and item.strip():
                return item.strip()
    # Org phone source is `phone`/`sanitized_phone` (people carry theirs on
    # `phone_numbers[]` above); include both scalars in the fallback chain.
    for key in ("phone", "sanitized_phone"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _location(raw: dict[str, Any]) -> str | None:
    parts = [str(raw.get(key) or "").strip() for key in ("city", "state", "country")]
    joined = ", ".join(part for part in parts if part)
    return joined or None


def _top_field(lead: dict[str, Any], key: str) -> str | None:
    """Read a derived top-level index field (semantic-search v2) off the lead doc.

    These are recomputed from the merged Apollo payloads on every write and are
    the authoritative, placeholder-aware values (a locked ``email_not_unlocked@…``
    never lands here). Prefer them over re-deriving from the raw payload.
    """
    value = lead.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def summarize_lead(lead: dict[str, Any]) -> dict[str, Any]:
    """One compact dict per lead: identity, contact, enrichment state, timeline."""
    entity_type = str(lead.get("entity_type") or "person")
    raw = _display_payload(lead)

    responses = lead.get("apollo_responses")
    endpoints: dict[str, Any] = {}
    if isinstance(responses, dict):
        for key, entry in responses.items():
            endpoints[str(key)] = entry.get("received_at") if isinstance(entry, dict) else None

    summary: dict[str, Any] = {
        "mongo_id": lead.get("id"),
        "apollo_id": lead.get("apollo_id"),
        "entity_type": entity_type,
        "embedding": bool(lead.get("embedding")),
        "apollo_enriched": lead.get("apollo_enriched"),
        # Which Apollo endpoints have stored payloads, and when they landed —
        # this is the per-lead enrichment timeline.
        "apollo_endpoints": endpoints,
        "created_at": lead.get("created_at"),
        "updated_at": lead.get("updated_at"),
    }

    if entity_type == "person":
        first = str(raw.get("first_name") or "").strip()
        last = str(raw.get("last_name") or raw.get("last_name_obfuscated") or "").strip()
        org = raw.get("organization") if isinstance(raw.get("organization"), dict) else {}
        # Prefer the derived top-level index fields (semantic-search v2) when
        # present; fall back to re-deriving from the raw payload for older docs.
        summary.update(
            {
                "name": _top_field(lead, "name")
                or (raw.get("name") or f"{first} {last}".strip() or None),
                "title": _top_field(lead, "title") or raw.get("title"),
                # company_id = the org DOCUMENT's Mongo _id (round-trips directly
                # into the similarity company filter); company_apollo_id = the raw
                # Apollo org id it was resolved from.
                "company_id": _top_field(lead, "company_id"),
                "company_apollo_id": _top_field(lead, "company_apollo_id"),
                "email": _top_field(lead, "email") or _first_email(raw),
                "phone": _top_field(lead, "phone") or _first_phone(raw),
                "linkedin_url": _top_field(lead, "linkedin") or raw.get("linkedin_url"),
                "organization": org.get("name"),
                "location": _location(raw),
            }
        )
    else:
        # Orgs carry only phone/linkedin at the top level (v2); name/etc. still
        # come from the payload.
        summary.update(
            {
                "name": raw.get("name"),
                "domain": raw.get("primary_domain") or raw.get("domain") or raw.get("website_url"),
                "industry": raw.get("industry"),
                "employee_count": raw.get("estimated_num_employees"),
                "phone": _top_field(lead, "phone") or _first_phone(raw),
                "linkedin_url": _top_field(lead, "linkedin") or raw.get("linkedin_url"),
                "location": _location(raw),
            }
        )
    return summary
