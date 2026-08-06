"""OpenAI embedding client for the records store's fuzzy columns.

Degrades cleanly: with no OPENAI_API_KEY, sync stores rows with NULL embedding
columns (they simply never match fuzzy filters) and queries using fuzzy filters
fail with a clear 503; once a key appears, the sync loop back-fills NULLs.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException

from app.config import get_settings

logger = logging.getLogger("knowledge.embeddings")

_BATCH = 64
_MAX_CHARS = 6000  # keep any single input well under the model's token cap

_client = None


def _get_client():
    global _client
    if _client is None:
        from openai import AsyncOpenAI

        cfg = get_settings()
        _client = AsyncOpenAI(api_key=cfg.openai_api_key, max_retries=cfg.openai_max_retries)
    return _client


def embeddings_available() -> bool:
    return get_settings().openai_configured


async def embed_texts(texts: list[str]) -> list[list[float] | None]:
    """Embed a batch; empty/blank inputs come back as None (stored as NULL)."""
    cfg = get_settings()
    if not cfg.openai_configured:
        raise HTTPException(status_code=503, detail="fuzzy matching unavailable: OPENAI_API_KEY is not configured")
    out: list[list[float] | None] = [None] * len(texts)
    todo = [(i, t.strip()[:_MAX_CHARS]) for i, t in enumerate(texts) if t and t.strip()]
    client = _get_client()
    for start in range(0, len(todo), _BATCH):
        chunk = todo[start : start + _BATCH]
        resp = await client.embeddings.create(
            model=cfg.openai_embedding_model,
            input=[t for _, t in chunk],
            dimensions=cfg.openai_embedding_dimensions,
        )
        for (i, _), item in zip(chunk, resp.data):
            out[i] = item.embedding
    return out


async def embed_query(text: str) -> list[float]:
    vecs = await embed_texts([text])
    if vecs[0] is None:
        raise HTTPException(status_code=422, detail="fuzzy filter passage is empty")
    return vecs[0]
