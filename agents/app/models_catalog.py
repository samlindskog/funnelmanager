"""Model list + context-window map + cost, for GET /models and GET /stats.

``client.models.list()`` returns only ``id``/``created``/``owned_by`` — **no
context window and no price**. So we:

- filter the OpenAI model list to **chat-capable** ids (excluding embeddings,
  audio, image, moderation, realtime, …);
- **join** each with a maintained, hardcoded context-window map (prefix-matched)
  — the honest source for "max context tokens", since the API does not expose it;
- compute cost from stored usage via ``genai-prices`` (which pydantic-ai already
  depends on).

The listing is cached in-process (short TTL) so the OpenAI round-trip does not run
per request. If ``OPENAI_API_KEY`` is unset or the call fails, we fall back to the
hardcoded map's keys so the UI still renders a usable picker.
"""

from __future__ import annotations

import logging
import time

from app.config import Settings

logger = logging.getLogger(__name__)

# --- maintained context-window map (max total tokens) -----------------------
# Prefix-matched (longest prefix wins). Values are the model's max *context*
# window (input+output budget), sourced from the OpenAI model pages — trust those
# over third-party aggregators. Update this map when adding a model; it is the
# only place "how big is this model's context" is asserted. Kept intentionally
# small and explicit rather than guessed.
_CONTEXT_WINDOWS: dict[str, int] = {
    "gpt-5": 400_000,
    "gpt-4.1": 1_047_576,
    "gpt-4o-mini": 128_000,
    "gpt-4o": 128_000,
    "gpt-4-turbo": 128_000,
    "gpt-4": 8_192,
    "gpt-3.5-turbo": 16_385,
    "o4-mini": 200_000,
    "o3-mini": 200_000,
    "o3": 200_000,
    "o1-mini": 128_000,
    "o1": 200_000,
}

# Default context window when a chat model isn't in the map — conservative so the
# summarizing history processor errs toward summarizing sooner, never overrunning.
_DEFAULT_CONTEXT_WINDOW = 128_000

# Substrings that mark a NON-chat model id (filtered out of the picker).
_NON_CHAT_MARKERS = (
    "embedding",
    "whisper",
    "tts",
    "audio",
    "realtime",
    "dall-e",
    "image",
    "moderation",
    "transcribe",
    "search",  # e.g. *-search-preview retrieval helpers, not general chat
    "codex",
)

# Only ids beginning with one of these are considered chat-capable.
_CHAT_PREFIXES = ("gpt-", "o1", "o3", "o4", "chatgpt-")

_CACHE_TTL_SECONDS = 300.0
_cache: dict[str, object] = {"at": 0.0, "models": None}


def context_window_for(model_id: str) -> int:
    """Max context tokens for ``model_id`` (longest-prefix match; a safe default
    for an unmapped chat model)."""
    bare = (model_id or "").split(":", 1)[-1].strip()
    best: tuple[int, int] | None = None  # (prefix_len, window)
    for prefix, window in _CONTEXT_WINDOWS.items():
        if bare.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), window)
    return best[1] if best is not None else _DEFAULT_CONTEXT_WINDOW


def _is_chat_model(model_id: str) -> bool:
    lowered = model_id.lower()
    if any(marker in lowered for marker in _NON_CHAT_MARKERS):
        return False
    return lowered.startswith(_CHAT_PREFIXES)


def _fallback_models(default_model: str) -> list[dict]:
    ids = list(_CONTEXT_WINDOWS.keys())
    if default_model and default_model not in ids:
        ids.insert(0, default_model)
    return [
        {"id": mid, "context_window": context_window_for(mid), "owned_by": "openai"}
        for mid in ids
    ]


async def list_models(settings: Settings) -> list[dict]:
    """Chat-capable OpenAI models joined with the context-window map. Cached."""
    now = time.monotonic()
    cached = _cache.get("models")
    if cached is not None and (now - float(_cache["at"])) < _CACHE_TTL_SECONDS:
        return cached  # type: ignore[return-value]

    default_model = settings.model_name
    if not settings.openai_api_key:
        models = _fallback_models(default_model)
        _cache["models"], _cache["at"] = models, now
        return models

    try:
        from openai import AsyncOpenAI  # noqa: PLC0415 — lazy: only when serving /models

        client = AsyncOpenAI(api_key=settings.openai_api_key)
        listing = await client.models.list()
        seen: set[str] = set()
        models: list[dict] = []
        for m in listing.data:
            mid = getattr(m, "id", "")
            if not mid or mid in seen or not _is_chat_model(mid):
                continue
            seen.add(mid)
            models.append(
                {
                    "id": mid,
                    "context_window": context_window_for(mid),
                    "owned_by": getattr(m, "owned_by", "openai"),
                }
            )
        if default_model and default_model not in seen:
            models.insert(
                0,
                {
                    "id": default_model,
                    "context_window": context_window_for(default_model),
                    "owned_by": "openai",
                },
            )
        models.sort(key=lambda x: x["id"])
        _cache["models"], _cache["at"] = models, now
        return models
    except Exception:  # noqa: BLE001 — network/auth failure -> usable fallback
        logger.warning("agents: OpenAI models.list() failed; using fallback map", exc_info=True)
        models = _fallback_models(default_model)
        _cache["models"], _cache["at"] = models, now
        return models


def estimate_cost(model_id: str, input_tokens: int, output_tokens: int) -> float | None:
    """USD cost for a token spend via genai-prices, or None if the model is
    unknown to the price data (never raises)."""
    if not model_id:
        return None
    try:
        from genai_prices import Usage, calc_price  # noqa: PLC0415

        bare = model_id.split(":", 1)[-1]
        price = calc_price(
            Usage(input_tokens=int(input_tokens or 0), output_tokens=int(output_tokens or 0)),
            model_ref=bare,
        )
        return float(price.total_price)
    except Exception:  # noqa: BLE001 — unknown model / offline price data
        return None


__all__ = ["context_window_for", "estimate_cost", "list_models"]
