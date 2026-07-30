"""Build lead embedding text and call OpenAI embeddings (async)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from openai import AsyncOpenAI

from app.apollo_endpoints import (
    ORG_BY_ID,
    ORG_ENRICH,
    ORG_SEARCH,
    PERSON_BY_ID,
    PERSON_MATCH,
    PERSON_SEARCH,
    responses_from_doc,
    unwrap_organization_payload,
    unwrap_person_payload,
)
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

# Embedding source precedence (higher wins). A search must never overwrite an
# embedding produced from richer data, and match outranks complete-info (people/{id}).
EMBED_SOURCE_SEARCH = 1
EMBED_SOURCE_COMPLETE_INFO = 2
EMBED_SOURCE_MATCH = 3

# Precedence of the data each stored Apollo response represents, per endpoint.
_SOURCE_PRECEDENCE: dict[str, int] = {
    PERSON_MATCH: EMBED_SOURCE_MATCH,
    PERSON_BY_ID: EMBED_SOURCE_COMPLETE_INFO,
    PERSON_SEARCH: EMBED_SOURCE_SEARCH,
    ORG_BY_ID: EMBED_SOURCE_COMPLETE_INFO,
    ORG_ENRICH: EMBED_SOURCE_COMPLETE_INFO,
    ORG_SEARCH: EMBED_SOURCE_SEARCH,
}

# Which stored response supplies the embedding text: highest precedence first, so
# match beats complete-info beats search.
_PERSON_EMBED_PRIORITY = (PERSON_MATCH, PERSON_BY_ID, PERSON_SEARCH)
_ORG_EMBED_PRIORITY = (ORG_BY_ID, ORG_ENRICH, ORG_SEARCH)


def endpoint_source_precedence(endpoint: str) -> int:
    """Embedding precedence for the operation that wrote ``endpoint`` (0 if unknown)."""
    return _SOURCE_PRECEDENCE.get(endpoint, 0)


def lead_embedding_precedence(doc: dict[str, Any]) -> int:
    """Highest precedence among the lead's stored responses that carry real data.

    This is the quality tier of the text the lead would embed to, and thus the
    precedence recorded for its Milvus vector.
    """
    responses = responses_from_doc(doc)
    best = 0
    for endpoint, precedence in _SOURCE_PRECEDENCE.items():
        entry = responses.get(endpoint)
        if not isinstance(entry, dict):
            continue
        data = entry.get("data")
        if isinstance(data, dict) and data:
            best = max(best, precedence)
    return best

# Cap concurrent embedding HTTP calls across background page tasks. One semaphore
# per event loop (lazy-init); its size is taken from ``Settings.embed_concurrency``
# the first time it is created on a loop.
_embed_semaphore: asyncio.Semaphore | None = None
_embed_semaphore_loop: asyncio.AbstractEventLoop | None = None


def _embed_sem(settings: Settings | None = None) -> asyncio.Semaphore:
    global _embed_semaphore, _embed_semaphore_loop
    loop = asyncio.get_running_loop()
    if _embed_semaphore is None or _embed_semaphore_loop is not loop:
        cfg = settings or get_settings()
        _embed_semaphore = asyncio.Semaphore(max(1, int(cfg.embed_concurrency)))
        _embed_semaphore_loop = loop
    return _embed_semaphore


def _location_line(person: dict[str, Any]) -> str:
    parts = [
        person.get("city"),
        person.get("state"),
        person.get("country"),
        person.get("present_raw_address"),
    ]
    return ", ".join(str(p).strip() for p in parts if p and str(p).strip())


def _org_line(person: dict[str, Any]) -> str:
    org = person.get("organization")
    if not isinstance(org, dict):
        org = {}
    name = org.get("name") or person.get("organization_name") or person.get("account", {})
    if isinstance(name, dict):
        name = name.get("name")
    domain = org.get("primary_domain") or org.get("domain") or person.get("organization_domain")
    bits = [str(name).strip()] if name else []
    if domain:
        bits.append(str(domain).strip())
    return " · ".join(bits)


def _headline(person: dict[str, Any]) -> str:
    for key in ("headline", "seo_description", "functions", "departments"):
        value = person.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, list):
            joined = ", ".join(str(item).strip() for item in value if item)
            if joined:
                return joined
    return ""


def person_payload_from_doc(doc: dict[str, Any]) -> dict[str, Any] | None:
    responses = responses_from_doc(doc)
    for key in _PERSON_EMBED_PRIORITY:
        entry = responses.get(key)
        if not isinstance(entry, dict):
            continue
        data = entry.get("data")
        if not isinstance(data, dict) or not data:
            continue
        return unwrap_person_payload(data)
    for entry in responses.values():
        if not isinstance(entry, dict):
            continue
        data = entry.get("data")
        if isinstance(data, dict) and data:
            return unwrap_person_payload(data)
    return None


def person_embedding_text(doc: dict[str, Any]) -> str:
    """Compact passage describing a person lead for embedding."""
    person = person_payload_from_doc(doc) or {}
    lines: list[str] = []

    name = person.get("name") or " ".join(
        str(part).strip()
        for part in (person.get("first_name"), person.get("last_name"))
        if part
    )
    if name:
        lines.append(f"Name: {name}")

    title = person.get("title")
    if title:
        lines.append(f"Title: {title}")

    org = _org_line(person)
    if org:
        lines.append(f"Company: {org}")

    location = _location_line(person)
    if location:
        lines.append(f"Location: {location}")

    headline = _headline(person)
    if headline:
        lines.append(f"Profile: {headline}")

    keywords = person.get("keywords")
    if isinstance(keywords, list) and keywords:
        lines.append("Keywords: " + ", ".join(str(k) for k in keywords[:20] if k))
    elif isinstance(keywords, str) and keywords.strip():
        lines.append(f"Keywords: {keywords.strip()}")

    if not lines:
        apollo_id = doc.get("apollo_id")
        return f"Apollo person {apollo_id}" if apollo_id else "Unknown person"
    return "\n".join(lines)


def organization_payload_from_doc(doc: dict[str, Any]) -> dict[str, Any] | None:
    responses = responses_from_doc(doc)
    for key in _ORG_EMBED_PRIORITY:
        entry = responses.get(key)
        if not isinstance(entry, dict):
            continue
        data = entry.get("data")
        if not isinstance(data, dict) or not data:
            continue
        return unwrap_organization_payload(data)
    for entry in responses.values():
        if not isinstance(entry, dict):
            continue
        data = entry.get("data")
        if isinstance(data, dict) and data:
            return unwrap_organization_payload(data)
    return None


def organization_embedding_text(doc: dict[str, Any]) -> str:
    """Compact passage describing an organization lead for embedding."""
    org = organization_payload_from_doc(doc) or {}
    lines: list[str] = []

    name = org.get("name")
    if name:
        lines.append(f"Name: {name}")

    domain = org.get("primary_domain") or org.get("domain") or org.get("website_url")
    if domain:
        lines.append(f"Domain: {domain}")

    industry = org.get("industry")
    if industry:
        lines.append(f"Industry: {industry}")

    location = _location_line(org)
    if location:
        lines.append(f"Location: {location}")

    employees = org.get("estimated_num_employees")
    if employees is not None and str(employees).strip():
        lines.append(f"Employees: {employees}")

    description = org.get("short_description") or org.get("seo_description")
    if isinstance(description, str) and description.strip():
        lines.append(f"Profile: {description.strip()}")

    keywords = org.get("keywords")
    if isinstance(keywords, list) and keywords:
        lines.append("Keywords: " + ", ".join(str(k) for k in keywords[:20] if k))
    elif isinstance(keywords, str) and keywords.strip():
        lines.append(f"Keywords: {keywords.strip()}")

    if not lines:
        apollo_id = doc.get("apollo_id")
        return f"Apollo organization {apollo_id}" if apollo_id else "Unknown organization"
    return "\n".join(lines)


def lead_embedding_text(doc: dict[str, Any]) -> str:
    """Embedding passage for a person or organization lead document."""
    if (doc.get("entity_type") or "person") == "organization":
        return organization_embedding_text(doc)
    return person_embedding_text(doc)


async def embed_texts(
    texts: list[str],
    *,
    settings: Settings | None = None,
) -> list[list[float]]:
    """Embed texts with AsyncOpenAI (non-blocking). Returns one vector per input.

    OpenAI is the slow, lock-free stage: the API-max chunks are embedded
    concurrently (bounded by ``Settings.embed_concurrency``) so calls overlap
    instead of running strictly serially. Output order matches ``texts``.
    """
    if not texts:
        return []
    cfg = settings or get_settings()
    if not cfg.openai_configured:
        raise RuntimeError("OPENAI_API_KEY is not configured")

    chunk_size = 64
    chunks = [texts[start : start + chunk_size] for start in range(0, len(texts), chunk_size)]
    sem = _embed_sem(cfg)

    # A higher retry budget lets sustained 429s (likely once embed_concurrency is
    # raised) back off (the SDK does exponential backoff + honors Retry-After) and
    # recover instead of exhausting → RateLimitError → docs silently left unembedded.
    async with AsyncOpenAI(
        api_key=cfg.openai_api_key,
        max_retries=max(0, int(cfg.openai_max_retries)),
    ) as client:

        async def _embed_chunk(chunk: list[str]) -> list[list[float]]:
            async with sem:
                response = await client.embeddings.create(
                    model=cfg.openai_embedding_model,
                    input=chunk,
                    dimensions=cfg.openai_embedding_dimensions,
                )
            by_index = {item.index: item.embedding for item in response.data}
            return [list(by_index[offset]) for offset in range(len(chunk))]

        # return_exceptions=True so a failing chunk never tears the client down
        # while sibling chunks are still in flight — that would run them against a
        # closed client and orphan tasks ("Task exception never retrieved"). Every
        # chunk settles inside the client context; the first error surfaces after.
        results = await asyncio.gather(
            *(_embed_chunk(chunk) for chunk in chunks),
            return_exceptions=True,
        )

    for result in results:
        if isinstance(result, BaseException):
            raise result

    # gather preserves input order, so vectors line up with ``texts``.
    vectors: list[list[float]] = []
    for chunk_result in results:
        vectors.extend(chunk_result)
    return vectors
