from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ApolloParamsBody(BaseModel):
    """Passthrough body for Apollo query parameters.

    Any documented Apollo search/enrich query param may be supplied as an
    optional field. Unknown keys are also accepted and forwarded upstream.

    Do not send Apollo API keys here — the leads service authenticates to
    Apollo with APOLLO_API_KEY from the server environment only.
    """

    model_config = ConfigDict(extra="allow")


class ApolloEndpointResponseOut(BaseModel):
    received_at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class ApolloEnrichedFlags(BaseModel):
    """Which enrichment jobs have been requested/run for this lead."""

    linkedin: bool = False
    email: bool = False
    phone: bool = False


class LeadOut(BaseModel):
    id: str
    apollo_id: str
    entity_type: Literal["person", "organization"]
    embedding: bool = False
    apollo_enriched: ApolloEnrichedFlags = Field(default_factory=ApolloEnrichedFlags)
    """Endpoint-keyed Apollo payloads; keys appear only after that endpoint is used."""
    apollo_responses: dict[str, ApolloEndpointResponseOut] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class BatchMongoIdsRequest(BaseModel):
    """Batch hydrate by MongoDB `_id` strings (order preserved; missing omitted)."""

    ids: list[str] = Field(default_factory=list, max_length=500)


class SearchIdsOut(BaseModel):
    """People/org search result: page ids and/or stream job handles.

    - ``stream=false``: ``ids`` populated; ``ingest_stream_id`` and ``embedding_stream_id`` are null.
    - ``stream=true``: ``ids`` is null; both stream ids are set for progress subscription.

    Consume either stream via ``GET /api/leads/stream/{id}`` or multiplex with
    ``POST /api/leads/stream``.
    """

    ingest_stream_id: str | None = None
    embedding_stream_id: str | None = None
    ids: list[str] | None = None


class StreamSubscribeRequest(BaseModel):
    """Subscribe to one or more stream jobs on a single NDJSON connection."""

    stream_ids: list[str] = Field(default_factory=list, min_length=1, max_length=50)


class EmbeddingBackfillResponse(BaseModel):
    """Result of starting an embedding backfill.

    ``matched`` is the estimated doc count the run will (re-)embed. When it is 0
    there is nothing to do and ``embedding_stream_id`` is null; otherwise the
    caller subscribes to ``embedding_stream_id`` for progress (``GET
    /api/leads/stream/{id}``), and the ``embedding`` flag flips to True per doc
    only after Milvus indexing succeeds.
    """

    embedding_stream_id: str | None = None
    matched: int = 0
    force: bool = False


class StreamControlResponse(BaseModel):
    """Result of a pause/resume/cancel control action on a stream job.

    ``status`` is one of ``running`` / ``paused`` / ``canceled`` / ``complete`` /
    ``error``. ``applied`` is False when the action was a no-op (idempotent
    re-invocation), but the request still succeeded and reports the status.
    """

    stream_id: str
    action: str
    status: str
    applied: bool


class SimilaritySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    limit: int = Field(default=25, ge=1, le=10000)


class SimilarityHitOut(BaseModel):
    score: float
    lead: LeadOut


class SimilaritySearchResponse(BaseModel):
    results: list[SimilarityHitOut] = Field(default_factory=list)
