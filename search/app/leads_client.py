"""HTTP client for the leads backend (the only Apollo-facing service)."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any, Literal
from urllib.parse import quote

import httpx
import jwt as pyjwt
import orjson
from fastapi import HTTPException, status

from fm_runtime import ExchangeError, get_broker
from fm_runtime.context import current_trace_headers

from app.config import Settings

logger = logging.getLogger(__name__)

APOLLO_MAX_PER_PAGE = 100

# Stop exchanging a subject token this close to (or past) its expiry —
# Keycloak would reject the exchange anyway.
_SUBJECT_EXPIRY_SLACK_SECONDS = 10.0

PERSON_DISPLAY_ENDPOINTS = (
    "/api/v1/people/match",
    "/api/v1/people/{id}",
    "/api/v1/mixed_people/api_search",
)
PERSON_SEARCH_ENDPOINT = "/api/v1/mixed_people/api_search"
ORG_DISPLAY_ENDPOINTS = (
    "/api/v1/organizations/{id}",
    "/api/v1/organizations/enrich",
    "/api/v1/mixed_companies/search",
)

_LEGACY_ENDPOINT_KEYS = {
    "mixed_people/api_search": "/api/v1/mixed_people/api_search",
    "people/{id}": "/api/v1/people/{id}",
    "people/match": "/api/v1/people/match",
    "mixed_companies/search": "/api/v1/mixed_companies/search",
    "organizations/{id}": "/api/v1/organizations/{id}",
    "organizations/enrich": "/api/v1/organizations/enrich",
    "api/v1/mixed_people/api_search": "/api/v1/mixed_people/api_search",
    "api/v1/people/{id}": "/api/v1/people/{id}",
    "api/v1/people/match": "/api/v1/people/match",
    "api/v1/mixed_companies/search": "/api/v1/mixed_companies/search",
    "api/v1/organizations/{id}": "/api/v1/organizations/{id}",
    "api/v1/organizations/enrich": "/api/v1/organizations/enrich",
}


def clamp_per_page(per_page: int) -> int:
    return max(1, min(int(per_page or APOLLO_MAX_PER_PAGE), APOLLO_MAX_PER_PAGE))


def keyword_tags_from_query(query: str) -> list[str]:
    cleaned = " ".join(query.split()).strip()
    if not cleaned:
        return []
    if "," in cleaned:
        return [tag for tag in (part.strip() for part in cleaned.split(",")) if tag]
    return [cleaned]


def normalize_domain(value: str | None) -> str | None:
    if not value:
        return None
    domain = value.strip().lower()
    for prefix in ("https://", "http://"):
        if domain.startswith(prefix):
            domain = domain[len(prefix) :]
    domain = domain.removeprefix("www.")
    domain = domain.split("/")[0].split("?")[0].strip()
    return domain or None


def normalize_pagination(
    pagination: dict[str, Any] | None,
    *,
    page: int,
    per_page: int,
    result_count: int,
    apollo_raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    data = dict(pagination or {})
    raw = apollo_raw if isinstance(apollo_raw, dict) else {}
    total = int(
        data.get("total_entries")
        or raw.get("total_entries")
        or (raw.get("pagination") or {}).get("total_entries")
        or result_count
        or 0
    )
    current_page = int(
        data.get("page")
        or raw.get("page")
        or (raw.get("pagination") or {}).get("page")
        or page
        or 1
    )
    current_per_page = int(
        data.get("per_page")
        or raw.get("per_page")
        or (raw.get("pagination") or {}).get("per_page")
        or per_page
        or result_count
        or 1
    )
    current_per_page = max(current_per_page, 1)
    total_pages = int(
        data.get("total_pages")
        or raw.get("total_pages")
        or (raw.get("pagination") or {}).get("total_pages")
        or (max(1, (total + current_per_page - 1) // current_per_page) if total else 1)
    )
    return {
        "page": current_page,
        "per_page": current_per_page,
        "total_entries": total,
        "total_pages": total_pages,
    }


def normalize_person(raw: dict[str, Any]) -> dict[str, Any]:
    org = raw.get("organization") or {}
    if not isinstance(org, dict):
        org = {}
    last = raw.get("last_name") or raw.get("last_name_obfuscated") or ""
    first = raw.get("first_name") or ""
    name = (raw.get("name") or f"{first} {last}".strip()).strip()
    return {
        "id": raw.get("id"),
        "entity_type": "person",
        "name": name or "Unknown",
        "first_name": first,
        "last_name": last,
        "title": raw.get("title"),
        "headline": raw.get("headline"),
        "linkedin_url": raw.get("linkedin_url"),
        "email": raw.get("email"),
        "emails": raw.get("emails"),
        "phone_numbers": raw.get("phone_numbers"),
        "has_email": raw.get("has_email"),
        "has_phone": raw.get("has_direct_phone") or raw.get("has_phone"),
        "city": raw.get("city"),
        "state": raw.get("state"),
        "country": raw.get("country"),
        "organization": {
            "id": org.get("id"),
            "name": org.get("name"),
            "domain": org.get("primary_domain") or org.get("domain") or org.get("website_url"),
            "linkedin_url": org.get("linkedin_url"),
        },
        "raw": raw,
    }


def normalize_company(raw: dict[str, Any]) -> dict[str, Any]:
    organization_id = raw.get("organization_id") or raw.get("id")
    return {
        "id": organization_id,
        "account_id": raw.get("id"),
        "organization_id": organization_id,
        "entity_type": "company",
        "name": raw.get("name") or "Unknown",
        "domain": raw.get("primary_domain") or raw.get("domain") or raw.get("website_url"),
        "website_url": raw.get("website_url"),
        "linkedin_url": raw.get("linkedin_url"),
        "industry": raw.get("industry"),
        "keywords": raw.get("keywords") or [],
        "employee_count": raw.get("estimated_num_employees"),
        "city": raw.get("city"),
        "state": raw.get("state"),
        "country": raw.get("country"),
        "short_description": raw.get("short_description"),
        "phone": raw.get("phone"),
        "raw": raw,
    }


def _canonical_responses_map(lead: dict[str, Any]) -> dict[str, Any]:
    responses = lead.get("apollo_responses")
    if not isinstance(responses, dict) or not responses:
        return {}
    out: dict[str, Any] = {}
    for key, entry in responses.items():
        canonical = _LEGACY_ENDPOINT_KEYS.get(str(key), str(key))
        if not canonical.startswith("/"):
            canonical = f"/{canonical.lstrip('/')}"
        if canonical in out and str(key) != canonical:
            continue
        out[canonical] = entry
    return out


def _endpoint_data(lead: dict[str, Any], endpoint: str) -> dict[str, Any] | None:
    responses = _canonical_responses_map(lead)
    entry = responses.get(endpoint)
    if not isinstance(entry, dict):
        return None
    data = entry.get("data")
    return data if isinstance(data, dict) else None


def _legacy_apollo_response(lead: dict[str, Any]) -> dict[str, Any] | None:
    raw = lead.get("apollo_response")
    return raw if isinstance(raw, dict) else None


def display_payload_from_lead(lead: dict[str, Any]) -> dict[str, Any] | None:
    """Prefer enriched endpoint groups for UI fields; fall back to search / legacy."""
    entity_type = lead.get("entity_type")
    priority = ORG_DISPLAY_ENDPOINTS if entity_type == "organization" else PERSON_DISPLAY_ENDPOINTS
    for endpoint in priority:
        data = _endpoint_data(lead, endpoint)
        if data:
            if entity_type == "person":
                person = data.get("person")
                return person if isinstance(person, dict) else data
            for key in ("organization", "account"):
                nested = data.get(key)
                if isinstance(nested, dict):
                    return nested
            return data
    responses = _canonical_responses_map(lead)
    for entry in responses.values():
        if not isinstance(entry, dict):
            continue
        data = entry.get("data")
        if isinstance(data, dict) and data:
            if entity_type == "person":
                person = data.get("person")
                return person if isinstance(person, dict) else data
            for key in ("organization", "account"):
                nested = data.get(key)
                if isinstance(nested, dict):
                    return nested
            return data
    return _legacy_apollo_response(lead)


def normalize_apollo_enriched_flags(value: object) -> dict[str, bool]:
    if isinstance(value, dict):
        return {
            "linkedin": bool(value.get("linkedin")),
            "email": bool(value.get("email")),
            "phone": bool(value.get("phone")),
        }
    if value is True:
        # Legacy bool without endpoint context — treat as profile enrich only.
        return {"linkedin": True, "email": False, "phone": False}
    return {"linkedin": False, "email": False, "phone": False}


def any_apollo_enriched(flags: dict[str, bool] | None) -> bool:
    if not flags:
        return False
    return bool(flags.get("linkedin") or flags.get("email") or flags.get("phone"))


def serialize_apollo_responses(lead: dict[str, Any]) -> dict[str, Any]:
    responses = _canonical_responses_map(lead)
    if responses:
        out: dict[str, Any] = {}
        for key, entry in responses.items():
            if not isinstance(entry, dict):
                continue
            data = entry.get("data")
            if not isinstance(data, dict):
                continue
            out[key] = {
                "received_at": entry.get("received_at"),
                "data": data,
            }
        if out:
            return out
    legacy = _legacy_apollo_response(lead)
    if not legacy:
        return {}
    entity_type = lead.get("entity_type")
    enriched = any_apollo_enriched(normalize_apollo_enriched_flags(lead.get("apollo_enriched")))
    if entity_type == "organization":
        key = "/api/v1/organizations/{id}" if enriched else "/api/v1/mixed_companies/search"
    else:
        key = "/api/v1/people/{id}" if enriched else "/api/v1/mixed_people/api_search"
    return {
        key: {
            "received_at": lead.get("updated_at") or lead.get("created_at"),
            "data": legacy,
        }
    }


def _as_contact_signal(value: object) -> bool | None:
    """Normalize Apollo search contact flags (bool or Yes/No strings)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "y", "on"}:
            return True
        if lowered in {"0", "false", "no", "n", "off"}:
            return False
    return bool(value)


def contact_signals_from_person_search(lead: dict[str, Any]) -> dict[str, bool | None]:
    """Always read has_email / has_phone from mixed_people/api_search when present."""
    data = _endpoint_data(lead, PERSON_SEARCH_ENDPOINT)
    if not isinstance(data, dict):
        return {"has_email": None, "has_phone": None}
    person = data.get("person") if isinstance(data.get("person"), dict) else data
    if not isinstance(person, dict):
        return {"has_email": None, "has_phone": None}
    return {
        "has_email": _as_contact_signal(person.get("has_email")),
        "has_phone": _as_contact_signal(
            person.get("has_direct_phone")
            if person.get("has_direct_phone") is not None
            else person.get("has_phone")
        ),
    }


def _top_field(lead: dict[str, Any], key: str) -> str | None:
    """Read a derived top-level index field (semantic-search v2), None if absent/blank."""
    value = lead.get(key)
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def _has_derived_marker(lead: dict[str, Any]) -> bool:
    """True if leads stamped a ``derived_at`` marker on this doc — the authoritative
    signal that the derived top-level fields (name/title/email/phone/linkedin) were
    (re)computed and can be trusted verbatim. Absent/null on legacy docs not yet
    touched by a v2 write or the migration backfill. Do NOT use name-presence as a
    v2 proxy: a phone-reveal-webhook person has authoritative email/phone but no
    name, so it would be mis-classified as legacy and read stale contact signals."""
    return bool(lead.get("derived_at"))


# Apollo emits this local-part when a person's email is locked / not revealed.
# It is a placeholder, not a real address, and must be treated as absent — same
# constant/semantics leads applies in ``leads/app/derived.py`` (re-declared here,
# not imported, since search never imports from leads).
_EMAIL_PLACEHOLDER_PREFIX = "email_not_unlocked@"


def _is_placeholder_email(value: object) -> bool:
    """True if ``value`` is Apollo's locked-email placeholder (case-insensitive)."""
    return (
        isinstance(value, str)
        and value.strip().lower().startswith(_EMAIL_PLACEHOLDER_PREFIX)
    )


def _strip_placeholder_emails(value: object) -> object:
    """Drop locked-placeholder entries from an Apollo ``emails`` list.

    Preserves the value's shape: a non-list is returned unchanged so the record's
    ``emails`` field keeps whatever type it had.
    """
    if not isinstance(value, list):
        return value
    kept: list[Any] = []
    for item in value:
        candidate = item.get("email") if isinstance(item, dict) else item
        if _is_placeholder_email(candidate):
            continue
        kept.append(item)
    return kept


def _has_resolvable_phone(numbers: object) -> bool:
    """Mirror the UI's ``phoneNumbersFromValue``: does ``numbers`` yield any number?"""
    if not isinstance(numbers, list):
        return False
    for item in numbers:
        if isinstance(item, str) and item.strip():
            return True
        if isinstance(item, dict):
            for key in ("sanitized_number", "raw_number", "number"):
                candidate = item.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return True
    return False


def lead_to_record(lead: dict[str, Any]) -> dict[str, Any] | None:
    entity_type = lead.get("entity_type")
    raw = display_payload_from_lead(lead)
    if not isinstance(raw, dict):
        return None
    # Derived top-level index fields (semantic-search v2). When present they are the
    # authoritative, precedence-resolved values (placeholder-aware email) — prefer
    # them over re-deriving from raw payloads. Absent (old docs) => fall back.
    # A doc is "v2/authoritative" iff leads stamped a ``derived_at`` marker, NOT iff
    # a derived name happens to be present (a name-less reveal-webhook doc is still v2).
    is_derived = _has_derived_marker(lead)
    top_name = _top_field(lead, "name")
    top_title = _top_field(lead, "title")
    top_email = _top_field(lead, "email")
    top_phone = _top_field(lead, "phone")
    top_linkedin = _top_field(lead, "linkedin")
    record: dict[str, Any] | None = None
    if entity_type == "person":
        record = normalize_person(raw)
        # A locked Apollo email (email_not_unlocked@…) is a placeholder set verbatim
        # by normalize_person; never let it survive on the normalized record (it
        # would make has_email=False contradict a non-empty email and leak the
        # placeholder into CSV export + every consumer). Strip it from BOTH the
        # scalar and the emails list for ALL docs (v2 + legacy) BEFORE applying the
        # placeholder-aware top-level override.
        if _is_placeholder_email(record.get("email")):
            record["email"] = None
        record["emails"] = _strip_placeholder_emails(record.get("emails"))
        if top_name:
            record["name"] = top_name
        if top_title is not None:
            record["title"] = top_title
        if top_email is not None:
            record["email"] = top_email
        if top_linkedin is not None:
            record["linkedin_url"] = top_linkedin
        # The person's company link — the ORGANIZATION DOCUMENT's Mongo _id
        # (round-trips directly into the similarity company filter) plus the raw
        # Apollo org id it resolved from.
        record["company_id"] = _top_field(lead, "company_id")
        record["company_apollo_id"] = _top_field(lead, "company_apollo_id")
        # Surface a reveal-webhook phone that only exists at the derived top level:
        # without this a person can show has_phone=True yet resolve to no number
        # (the display payload's phone_numbers is empty). Append it in the shape the
        # UI's resolvedRecordPhone reads (sanitized_number) — shape unchanged.
        if top_phone is not None and not _has_resolvable_phone(record.get("phone_numbers")):
            existing = record.get("phone_numbers")
            numbers = list(existing) if isinstance(existing, list) else []
            numbers.append({"sanitized_number": top_phone})
            record["phone_numbers"] = numbers
        # Contact availability chips: a v2 doc (recognizable by leads' `derived_at`
        # marker) keys them off actual top-level value presence; old docs lacking the
        # marker fall back to People API Search signals. Gating on the marker rather
        # than name-presence is what lets a name-less reveal-webhook person (real,
        # resolvable email/phone but no name) report has_email/has_phone truthfully
        # instead of reading stale signals from a payload it may not even have.
        if is_derived:
            record["has_email"] = top_email is not None
            record["has_phone"] = top_phone is not None
        else:
            signals = contact_signals_from_person_search(lead)
            record["has_email"] = signals["has_email"]
            record["has_phone"] = signals["has_phone"]
    elif entity_type == "organization":
        record = normalize_company(raw)
        if top_linkedin is not None:
            record["linkedin_url"] = top_linkedin
        if top_phone is not None:
            record["phone"] = top_phone
    if record is None:
        return None
    record["mongo_id"] = str(lead.get("id") or "").strip() or None
    record["embedding"] = bool(lead.get("embedding"))
    record["apollo_enriched"] = normalize_apollo_enriched_flags(lead.get("apollo_enriched"))
    record["apollo_responses"] = serialize_apollo_responses(lead)
    return record


def _token_expiry(token: str | None) -> float | None:
    """Unix ``exp`` of a JWT, parsed without verification (the middleware
    already verified the inbound token; this is only for expiry scheduling)."""
    if not token:
        return None
    try:
        claims = pyjwt.decode(token, options={"verify_signature": False})
    except pyjwt.InvalidTokenError:
        return None
    exp = claims.get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


class LeadsClient:
    """Relay to /api/leads — never calls Apollo directly.

    ``token`` is the caller's *subject* token: on every outbound call it is
    exchanged (RFC 8693, cached) for a token whose audience is the leads
    service, so leads sees the same originating principal with this service
    in the ``act`` chain. Access tokens are short-lived (minutes) while
    detached ingest/enrich jobs and NDJSON streams can run far longer; once
    the captured subject token expires no exchange can succeed, so the client
    downgrades to this service's own client-credentials identity for the rest
    of the job — the principal authorized the work when it started, and the
    ``internal-service`` role grant scopes what the service identity may call.
    Without a subject token at all, calls use client credentials from the
    start.
    """

    AUDIENCE = "leads"

    def __init__(self, settings: Settings, token: str | None = None):
        self.settings = settings
        self.subject_token = token
        self._subject_expires_at = _token_expiry(token)
        self._downgraded = False
        # Trace context is captured at construction (request scope) so
        # detached jobs keep correlating with the request that started them.
        self._trace_headers = current_trace_headers()

    def _effective_subject(self) -> str | None:
        subject = self.subject_token
        if (
            subject
            and self._subject_expires_at is not None
            and time.time() >= self._subject_expires_at - _SUBJECT_EXPIRY_SLACK_SECONDS
        ):
            if not self._downgraded:
                self._downgraded = True
                logger.warning(
                    "Subject token expired mid-job; continuing under the "
                    "search service identity (client credentials)"
                )
            subject = None
        return subject

    async def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            **self._trace_headers,
        }
        try:
            outbound = await get_broker().token_for(self.AUDIENCE, self._effective_subject())
        except ExchangeError as exc:
            # Keycloak unreachable or refusing: retryable upstream failure.
            # Streaming endpoints catch HTTPException and emit an ``error``
            # event; synchronous ones return this 503 as-is.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Identity provider unavailable: {exc}",
            ) from exc
        if outbound:
            headers["Authorization"] = f"Bearer {outbound}"
        return headers

    def _url(self, path: str) -> str:
        base = self.settings.leads_backend_url.rstrip("/")
        return f"{base}/{path.lstrip('/')}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | list[Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        async with httpx.AsyncClient(timeout=90.0) as client:
            response = await client.request(
                method,
                self._url(path),
                headers=await self._headers(),
                json=json_body,
                params=params,
            )
        if response.status_code >= 400:
            detail: Any
            try:
                payload = response.json()
                detail = payload.get("detail", payload)
            except Exception:
                detail = response.text
            # Preserve leads/Apollo status semantics for the UI when possible.
            raise HTTPException(
                status_code=response.status_code
                if response.status_code in {400, 401, 403, 404, 409, 422}
                else status.HTTP_502_BAD_GATEWAY,
                detail=detail
                if response.status_code in {400, 401, 403, 404, 409, 422}
                else {"leads_status": response.status_code, "leads_error": detail},
            )
        if response.status_code == 204:
            return None
        # Success path only: parse the (often large, nested) leads payload with
        # orjson straight off the raw bytes. Error paths above keep response.json()
        # so their status/detail behavior is byte-for-byte identical.
        return orjson.loads(response.content)

    @staticmethod
    def _apollo_path(relative: str) -> str:
        cleaned = relative.strip().lstrip("/")
        if cleaned.startswith("api/v1/"):
            cleaned = cleaned[len("api/v1/") :]
        return f"/api/leads/apollo/api/v1/{cleaned}"

    @staticmethod
    def _resolve_id(
        apollo_id: str | None,
        params: dict[str, Any],
        *keys: str,
    ) -> str | None:
        if apollo_id is not None and str(apollo_id).strip():
            return str(apollo_id).strip()
        for key in keys:
            value = params.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    @staticmethod
    def _as_search_ids_out(data: Any) -> dict[str, Any]:
        """Normalize leads ``SearchIdsOut`` (or legacy shapes)."""
        if isinstance(data, list):
            ids = [str(item).strip() for item in data if str(item or "").strip()]
            return {
                "ingest_stream_id": None,
                "embedding_stream_id": None,
                "ids": ids,
            }
        if not isinstance(data, dict):
            raise HTTPException(status_code=502, detail="Leads search returned invalid payload")
        # Prefer new field names; accept legacy ``stream_id`` as ingest.
        ingest_raw = data.get("ingest_stream_id")
        if ingest_raw is None:
            ingest_raw = data.get("stream_id")
        ingest = str(ingest_raw).strip() if ingest_raw is not None else ""
        embed_raw = data.get("embedding_stream_id")
        embed = str(embed_raw).strip() if embed_raw is not None else ""
        raw_ids = data.get("ids")
        if raw_ids is None:
            ids: list[str] = []
        elif isinstance(raw_ids, list):
            ids = [str(item).strip() for item in raw_ids if str(item or "").strip()]
        else:
            raise HTTPException(status_code=502, detail="Leads search returned invalid ids")
        return {
            "ingest_stream_id": ingest or None,
            "embedding_stream_id": embed or None,
            # Preserve null when leads returns null (stream=true); else list.
            "ids": None if raw_ids is None else ids,
        }

    @staticmethod
    def _as_mongo_id_list(data: Any) -> list[str]:
        out = LeadsClient._as_search_ids_out(data)
        raw = out.get("ids")
        return list(raw) if isinstance(raw, list) else []

    async def search_people(self, params: dict[str, Any]) -> list[str]:
        """Apollo people search via leads; returns Mongo `_id` strings for that page."""
        body = {**params, "stream": False}
        data = await self._request(
            "POST",
            self._apollo_path("mixed_people/api_search"),
            json_body=body,
        )
        return self._as_mongo_id_list(data)

    async def search_organizations(self, params: dict[str, Any]) -> list[str]:
        """Apollo org search via leads; returns Mongo `_id` strings for that page."""
        body = {**params, "stream": False}
        data = await self._request(
            "POST",
            self._apollo_path("mixed_companies/search"),
            json_body=body,
        )
        return self._as_mongo_id_list(data)

    async def start_people_search_stream(self, params: dict[str, Any]) -> dict[str, str | None]:
        """Start multi-page people search; return ingest + embedding stream ids."""
        body = {**params, "stream": True}
        data = await self._request(
            "POST",
            self._apollo_path("mixed_people/api_search"),
            json_body=body,
        )
        out = self._as_search_ids_out(data)
        if not out.get("ingest_stream_id"):
            raise HTTPException(status_code=502, detail="Leads search stream missing ingest_stream_id")
        return {
            "ingest_stream_id": out.get("ingest_stream_id"),
            "embedding_stream_id": out.get("embedding_stream_id"),
        }

    async def start_organizations_search_stream(
        self, params: dict[str, Any]
    ) -> dict[str, str | None]:
        """Start multi-page org search; return ingest + embedding stream ids."""
        body = {**params, "stream": True}
        data = await self._request(
            "POST",
            self._apollo_path("mixed_companies/search"),
            json_body=body,
        )
        out = self._as_search_ids_out(data)
        if not out.get("ingest_stream_id"):
            raise HTTPException(status_code=502, detail="Leads search stream missing ingest_stream_id")
        return {
            "ingest_stream_id": out.get("ingest_stream_id"),
            "embedding_stream_id": out.get("embedding_stream_id"),
        }

    async def cancel_stream(self, stream_id: str) -> dict[str, Any]:
        """Cancel a running ingest or embedding stream job on leads."""
        cleaned = str(stream_id or "").strip()
        if not cleaned:
            raise HTTPException(status_code=400, detail="stream_id is required")
        data = await self._request(
            "POST",
            f"/api/leads/stream/{quote(cleaned, safe='')}/cancel",
        )
        return data if isinstance(data, dict) else {"stream_id": cleaned, "cancelled": True}

    async def stream_control(self, stream_id: str, action: str) -> dict[str, Any]:
        """Pause/resume/cancel a leads stream via its internal control hook.

        This is the leads-side hook search's ``/internal/jobs/v1/{id}/{action}``
        control proxy maps onto. Idempotent on the leads side; returns
        ``{stream_id, action, status, applied}``.
        """
        cleaned = str(stream_id or "").strip()
        normalized = str(action or "").strip().lower()
        if not cleaned:
            raise HTTPException(status_code=400, detail="stream_id is required")
        if normalized not in {"pause", "resume", "cancel"}:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported control action {action!r}; expected pause, resume, or cancel",
            )
        data = await self._request(
            "POST",
            f"/api/leads/stream/{quote(cleaned, safe='')}/control/{normalized}",
        )
        if isinstance(data, dict):
            return data
        return {"stream_id": cleaned, "action": normalized, "status": "unknown", "applied": False}

    async def subscribe_streams(self, stream_ids: list[str]) -> AsyncIterator[dict[str, Any]]:
        """Yield NDJSON events from one or more leads stream jobs."""
        cleaned = [str(item).strip() for item in stream_ids if str(item or "").strip()]
        if not cleaned:
            return
        headers = {
            **await self._headers(),
            "Accept": "application/x-ndjson",
        }
        timeout = httpx.Timeout(connect=30.0, read=None, write=30.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            if len(cleaned) == 1:
                response = await client.send(
                    client.build_request(
                        "GET",
                        self._url(f"/api/leads/stream/{cleaned[0]}"),
                        headers=headers,
                    ),
                    stream=True,
                )
            else:
                response = await client.send(
                    client.build_request(
                        "POST",
                        self._url("/api/leads/stream"),
                        headers=headers,
                        json={"stream_ids": cleaned},
                    ),
                    stream=True,
                )
            try:
                if response.status_code >= 400:
                    detail: Any
                    try:
                        payload = await response.aread()
                        detail = json.loads(payload.decode("utf-8")).get("detail", payload.decode())
                    except Exception:
                        detail = (await response.aread()).decode("utf-8", errors="replace")
                    raise HTTPException(
                        status_code=response.status_code
                        if response.status_code in {400, 401, 403, 404, 409, 422}
                        else status.HTTP_502_BAD_GATEWAY,
                        detail=detail
                        if response.status_code in {400, 401, 403, 404, 409, 422}
                        else {"leads_status": response.status_code, "leads_error": detail},
                    )
                buffer = ""
                async for chunk in response.aiter_text():
                    buffer += chunk
                    while "\n" in buffer:
                        line, buffer = buffer.split("\n", 1)
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if isinstance(event, dict):
                            yield event
            finally:
                await response.aclose()

    async def exists_by_mongo_ids(self, mongo_ids: list[str]) -> set[str]:
        """Which of these Mongo `_id`s exist in leads (no hydration)."""
        present: set[str] = set()
        for start in range(0, len(mongo_ids), 50000):
            chunk = mongo_ids[start : start + 50000]
            data = await self._request("POST", "/api/leads/exists", json_body={"ids": chunk})
            for value in data.get("present", []) if isinstance(data, dict) else []:
                present.add(str(value))
        return present

    async def recent_leads(
        self,
        *,
        entity_type: str | None = None,
        limit: int = 25,
    ) -> list[dict[str, Any]]:
        """Most recently updated leads (GET /api/leads/recent passthrough)."""
        params: dict[str, Any] = {"limit": limit}
        if entity_type:
            params["entity_type"] = entity_type
        data = await self._request("GET", "/api/leads/recent", params=params)
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    async def resolve_organization_value(self, value: str) -> dict[str, Any]:
        """Resolve a company record Mongo _id OR Apollo org id to the stored org lead."""
        return await self._request(
            "GET", "/api/leads/organizations/resolve", params={"value": value}
        )

    async def get_by_mongo_ids(
        self,
        mongo_ids: list[str],
        *,
        fields: Literal["full", "display"] = "display",
    ) -> list[dict[str, Any]]:
        """Batch-hydrate leads by Mongo ``_id``.

        ``fields`` defaults to ``"display"`` — leads slims each doc's
        ``apollo_responses`` to the display subset ``lead_to_record`` consumes
        (display payload + PERSON_SEARCH + PERSON_MATCH for people; display-only
        for orgs). The single-lead RecordDetail refresh passes ``fields="full"``
        to keep every stored Apollo endpoint for the raw-response viewer.
        """
        if not mongo_ids:
            return []
        data = await self._request(
            "POST",
            "/api/leads",
            json_body={"ids": mongo_ids, "fields": fields},
        )
        if not isinstance(data, list):
            raise HTTPException(status_code=502, detail="Leads batch lookup returned invalid payload")
        return [item for item in data if isinstance(item, dict)]

    async def enrich_person(
        self,
        apollo_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Complete-person enrich; returns leads ``SearchIdsOut`` (ids and/or stream handles)."""
        body = dict(params or {})
        resolved = self._resolve_id(apollo_id, body, "id", "person_id")
        if not resolved:
            raise HTTPException(status_code=400, detail="apollo_id is required")
        # Do not forward stream via this helper; use start_person_enrich_stream.
        body.pop("stream", None)
        data = await self._request(
            "GET",
            self._apollo_path(f"people/{quote(resolved, safe='')}"),
            params={**body, "stream": False},
        )
        return self._as_search_ids_out(data)

    async def start_person_enrich_stream(self, apollo_id: str) -> dict[str, str | None]:
        """Start complete-person enrich stream via GET people/{id}?stream=true."""
        resolved = str(apollo_id or "").strip()
        if not resolved:
            raise HTTPException(status_code=400, detail="apollo_id is required")
        data = await self._request(
            "GET",
            self._apollo_path(f"people/{quote(resolved, safe='')}"),
            params={"stream": True},
        )
        out = self._as_search_ids_out(data)
        if not out.get("ingest_stream_id"):
            raise HTTPException(status_code=502, detail="Leads enrich stream missing ingest_stream_id")
        return {
            "ingest_stream_id": out.get("ingest_stream_id"),
            "embedding_stream_id": out.get("embedding_stream_id"),
        }

    async def start_organization_enrich_stream(self, apollo_id: str) -> dict[str, str | None]:
        """Start complete-org enrich stream via GET organizations/{id}?stream=true."""
        resolved = str(apollo_id or "").strip()
        if not resolved:
            raise HTTPException(status_code=400, detail="apollo_id is required")
        data = await self._request(
            "GET",
            self._apollo_path(f"organizations/{quote(resolved, safe='')}"),
            params={"stream": True},
        )
        out = self._as_search_ids_out(data)
        if not out.get("ingest_stream_id"):
            raise HTTPException(status_code=502, detail="Leads enrich stream missing ingest_stream_id")
        return {
            "ingest_stream_id": out.get("ingest_stream_id"),
            "embedding_stream_id": out.get("embedding_stream_id"),
        }

    async def match_person(
        self,
        apollo_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """People/match; returns leads ``SearchIdsOut`` (ids and/or stream handles)."""
        body = dict(params or {})
        resolved = self._resolve_id(apollo_id, body, "id", "person_id")
        if resolved:
            body["id"] = resolved
        # Do not forward stream via this helper; use start_person_match_stream.
        body.pop("stream", None)
        data = await self._request(
            "POST",
            self._apollo_path("people/match"),
            json_body={**body, "stream": False},
        )
        return self._as_search_ids_out(data)

    async def start_person_match_stream(
        self,
        apollo_id: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, str | None]:
        """Start people/match stream via POST people/match with ``stream=true``."""
        resolved = str(apollo_id or "").strip()
        if not resolved:
            raise HTTPException(status_code=400, detail="apollo_id is required")
        body = dict(params or {})
        body.pop("stream", None)
        body["id"] = resolved
        data = await self._request(
            "POST",
            self._apollo_path("people/match"),
            json_body={**body, "stream": True},
        )
        out = self._as_search_ids_out(data)
        if not out.get("ingest_stream_id"):
            raise HTTPException(status_code=502, detail="Leads match stream missing ingest_stream_id")
        return {
            "ingest_stream_id": out.get("ingest_stream_id"),
            "embedding_stream_id": out.get("embedding_stream_id"),
        }

    async def enrich_organization(
        self,
        apollo_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Complete-org enrich; returns leads ``SearchIdsOut`` (ids and/or stream handles)."""
        body = dict(params or {})
        resolved = self._resolve_id(apollo_id, body, "organization_id", "id")
        if not resolved:
            raise HTTPException(status_code=400, detail="apollo_id is required")
        body.pop("stream", None)
        data = await self._request(
            "GET",
            self._apollo_path(f"organizations/{quote(resolved, safe='')}"),
            params={**body, "stream": False},
        )
        return self._as_search_ids_out(data)

    async def get_apollo_credits(self) -> dict[str, Any]:
        """Fetch Apollo api_profile (with credits) via leads Apollo GET proxy."""
        data = await self._request(
            "GET",
            self._apollo_path("users/api_profile"),
            params={"include_credit_usage": "true"},
        )
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=502,
                detail="Leads Apollo users/api_profile returned invalid payload",
            )

        def _int_or_none(value: object) -> int | None:
            if value is None or value == "":
                return None
            try:
                return int(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return None

        # Map native Apollo fields to the UI credits shape.
        return {
            "credits_remaining": _int_or_none(data.get("num_credits_remaining")),
            "lead_credits_used": _int_or_none(data.get("num_lead_credits_used")),
            "effective_lead_credits": _int_or_none(data.get("effective_num_lead_credits")),
        }

    async def similarity_search(
        self,
        query: str | None = None,
        *,
        limit: int = 25,
        embeds: list[str] | None = None,
        company_id: str | None = None,
        company_ids: list[str] | None = None,
        entity_type: str | None = None,
        email_exists: bool | None = None,
        phone_exists: bool | None = None,
        linkedin_exists: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Search Milvus via leads; return scored hits with raw LeadOut payloads.

        v2 (additive): forward the optional embed-subset + filter params, omitting
        any that are ``None`` so leads applies its own defaults (omitted ``embeds``
        => ``["apollo"]``, exact legacy behavior).

        Each hit's ``lead`` feeds ``lead_to_record``, so request the slim
        ``fields="display"`` shape (leads' default is ``"full"``)."""
        json_body: dict[str, Any] = {"limit": limit, "fields": "display"}
        if query is not None:
            json_body["query"] = query
        if embeds is not None:
            json_body["embeds"] = embeds
        if company_id is not None:
            json_body["company_id"] = company_id
        if company_ids is not None:
            json_body["company_ids"] = company_ids
        if entity_type is not None:
            json_body["entity_type"] = entity_type
        if email_exists is not None:
            json_body["email_exists"] = email_exists
        if phone_exists is not None:
            json_body["phone_exists"] = phone_exists
        if linkedin_exists is not None:
            json_body["linkedin_exists"] = linkedin_exists
        data = await self._request(
            "POST",
            "/api/leads/similarity-search",
            json_body=json_body,
        )
        if not isinstance(data, dict):
            raise HTTPException(
                status_code=502,
                detail="Leads similarity search returned invalid payload",
            )
        results = data.get("results")
        if not isinstance(results, list):
            raise HTTPException(
                status_code=502,
                detail="Leads similarity search returned invalid results",
            )
        return [item for item in results if isinstance(item, dict)]

    async def resolve_organization_by_name(self, name: str) -> dict[str, Any]:
        mongo_ids = await self.search_organizations(
            {
                "q_organization_name": name,
                "page": 1,
                "per_page": 5,
            }
        )
        if not mongo_ids:
            raise HTTPException(
                status_code=404,
                detail=f'No Apollo company found matching name "{name}"',
            )
        leads = await self.get_by_mongo_ids(mongo_ids[:1])
        if not leads:
            raise HTTPException(
                status_code=404,
                detail=f'No Apollo company found matching name "{name}"',
            )
        lead = leads[0]
        raw = display_payload_from_lead(lead) if isinstance(lead, dict) else None
        if not isinstance(raw, dict):
            raw = {}
        org_id = lead.get("apollo_id") if isinstance(lead, dict) else None
        if not org_id:
            raise HTTPException(status_code=502, detail="Apollo company match was missing an id")
        return {
            "id": str(org_id),
            "name": raw.get("name") or name,
            "domain": normalize_domain(
                raw.get("primary_domain") or raw.get("website_url") or raw.get("domain")
            ),
        }

    async def resolve_organization_by_id(self, organization_id: str) -> dict[str, Any] | None:
        mongo_ids = await self.search_organizations(
            {
                "organization_ids": [organization_id],
                "page": 1,
                "per_page": 1,
            }
        )
        if not mongo_ids:
            return None
        leads = await self.get_by_mongo_ids(mongo_ids[:1])
        if not leads:
            return None
        lead = leads[0]
        raw = display_payload_from_lead(lead) if isinstance(lead, dict) else None
        if not isinstance(raw, dict):
            raw = {}
        org_id = lead.get("apollo_id") if isinstance(lead, dict) else None
        if not org_id:
            return None
        return {
            "id": str(org_id),
            "name": raw.get("name") or organization_id,
            "domain": normalize_domain(
                raw.get("primary_domain") or raw.get("website_url") or raw.get("domain")
            ),
        }

    def build_people_params(
        self,
        *,
        query: str | None,
        page: int,
        per_page: int,
        organization_ids: list[str] | None = None,
        organization_domains: list[str] | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": clamp_per_page(per_page),
        }
        cleaned = (query or "").strip()
        if cleaned:
            params["q_keywords"] = cleaned
        if organization_ids:
            params["organization_ids"] = organization_ids
        if organization_domains:
            params["q_organization_domains_list"] = organization_domains
        return params

    def build_company_params(
        self,
        *,
        keywords: str | None,
        page: int,
        per_page: int,
        company_name: str | None = None,
        company_domain: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": page,
            "per_page": clamp_per_page(per_page),
        }
        tags = keyword_tags_from_query(keywords or "")
        if tags:
            params["q_organization_keyword_tags"] = tags
        name = " ".join((company_name or "").split()).strip()
        if name:
            params["q_organization_name"] = name
        domain = normalize_domain(company_domain)
        if domain:
            params["q_organization_domains_list"] = [domain]
        return params

    async def search_people_page(
        self,
        *,
        query: str | None,
        page: int,
        per_page: int,
        organization_id: str | None = None,
        organization_name: str | None = None,
        organization_display_name: str | None = None,
        organization_domain: str | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        """Returns (mongo_ids, meta) for one Apollo people-search page."""
        org_id = (organization_id or "").strip() or None
        org_name = " ".join((organization_name or "").split()).strip() or None
        display_name = " ".join((organization_display_name or "").split()).strip() or None
        domain = normalize_domain(organization_domain)
        meta: dict[str, Any] = {}

        if org_id and org_name:
            raise HTTPException(
                status_code=400,
                detail="Provide either organization_id or organization_name, not both",
            )

        if org_name:
            resolved = await self.resolve_organization_by_name(org_name)
            org_id = resolved["id"]
            domain = domain or normalize_domain(resolved.get("domain"))
            meta["resolved_organization"] = resolved

        cleaned_query = (query or "").strip() or None
        if not cleaned_query and not org_id and not domain:
            raise HTTPException(
                status_code=400,
                detail="people search requires search text and/or a company filter",
            )

        params = self.build_people_params(
            query=cleaned_query,
            page=page,
            per_page=per_page,
            organization_ids=[org_id] if org_id else None,
            organization_domains=[domain] if domain and not org_id else None,
        )
        mongo_ids = await self.search_people(params)

        if org_id and not mongo_ids and domain:
            fallback = self.build_people_params(
                query=cleaned_query,
                page=page,
                per_page=per_page,
                organization_domains=[domain],
            )
            mongo_ids = await self.search_people(fallback)
            meta["organization_filter"] = {"type": "domain", "domain": domain}

        if org_id and "resolved_organization" not in meta:
            company_name = display_name
            if not company_name:
                looked_up = await self.resolve_organization_by_id(org_id)
                if looked_up:
                    company_name = looked_up.get("name")
                    domain = domain or normalize_domain(looked_up.get("domain"))
                    meta["resolved_organization"] = looked_up
            if company_name and "resolved_organization" not in meta:
                meta["resolved_organization"] = {
                    "id": org_id,
                    "name": company_name,
                    "domain": domain,
                }
        elif display_name and "resolved_organization" not in meta:
            meta["resolved_organization"] = {
                "id": org_id,
                "name": display_name,
                "domain": domain,
            }

        return mongo_ids, meta

    async def search_companies_page(
        self,
        *,
        keywords: str | None,
        page: int,
        per_page: int,
        company_name: str | None = None,
        company_domain: str | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        """Returns (mongo_ids, meta) for one Apollo organization-search page."""
        params = self.build_company_params(
            keywords=keywords,
            page=page,
            per_page=per_page,
            company_name=company_name,
            company_domain=company_domain,
        )
        mongo_ids = await self.search_organizations(params)
        return mongo_ids, {}
