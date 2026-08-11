from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    # Derived top-level index fields (semantic-search v2). People carry all six;
    # organizations only phone/linkedin. Absent => null.
    name: str | None = None
    title: str | None = None
    # company_id = the ORGANIZATION DOCUMENT's Mongo _id (the id space the
    # similarity company filter takes natively); company_apollo_id = the raw
    # Apollo org id it was resolved from (the resolution key).
    company_id: str | None = None
    company_apollo_id: str | None = None
    email: str | None = None
    phone: str | None = None
    linkedin: str | None = None
    # v2 marker: when the derived top-level fields above were last (re)computed.
    # Present => those fields are authoritative (a name-less doc's has_email/
    # has_phone can be trusted rather than inferred from name-presence). Null on
    # legacy docs not yet touched by a v2 write or the migration backfill.
    derived_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class BatchMongoIdsRequest(BaseModel):
    """Batch hydrate by MongoDB `_id` strings (order preserved; missing omitted).

    ``fields`` selects the response shape (additive; default ``"full"`` so
    MCP/agent callers see zero change):
    - ``"full"`` — every stored ``apollo_responses`` entry (the source of truth).
    - ``"display"`` — a slimmed ``apollo_responses`` carrying only what the search
      UI renders: for people the display payload (highest-precedence present
      endpoint) plus the search and match entries when present; for organizations
      the display payload only. All other ``LeadOut`` fields are unchanged.
    """

    ids: list[str] = Field(default_factory=list, max_length=500)
    fields: Literal["full", "display"] = "full"


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
    """Similarity search request (v2, additive over v1).

    ``embeds`` selects which per-kind embeddings to rank by (the mean similarity
    across the selected kinds a doc actually has):
    - omitted / ``None`` => ``["apollo"]`` — exact legacy behavior.
    - ``[]``             => pure filter search (no vector ranking; requires a filter).

    Filters: ``company_id`` (a company record's Mongo ``_id`` or its Apollo
    organization id), the ``entity_type`` restriction (``person`` /
    ``organization``), plus the tri-state ``email_exists`` / ``phone_exists`` /
    ``linkedin_exists`` (True=has, False=missing, None=no filter).
    """

    query: str | None = Field(default=None, max_length=8000)
    limit: int = Field(default=25, ge=1, le=10000)
    embeds: list[Literal["apollo", "name", "title"]] | None = None
    company_id: str | None = None
    entity_type: Literal["person", "organization"] | None = None
    email_exists: bool | None = None
    phone_exists: bool | None = None
    linkedin_exists: bool | None = None
    # Hydration shape of the returned leads (additive; default "full" — see
    # BatchMongoIdsRequest.fields). "display" slims each hit's apollo_responses.
    fields: Literal["full", "display"] = "full"

    @model_validator(mode="after")
    def _validate(self) -> "SimilaritySearchRequest":
        if self.embeds is not None and len(set(self.embeds)) != len(self.embeds):
            raise ValueError("embeds must not contain duplicate kinds")
        # Whitespace-only company_id is not a filter — strip it and coerce blank to
        # None BEFORE the at-least-one-filter count so `company_id: "  "` cannot
        # satisfy the requirement.
        if self.company_id is not None:
            cleaned = self.company_id.strip()
            self.company_id = cleaned or None
        # Omitted embeds default to ["apollo"] (legacy); [] is an explicit pure-filter.
        effective = self.embeds if self.embeds is not None else ["apollo"]
        if effective:
            if not (self.query or "").strip():
                raise ValueError("query is required when embeds is non-empty")
        else:
            # A pure-filter run never uses the query text; coerce it to None so a
            # stray query does not mislabel the downstream history row.
            self.query = None
            has_filter = any(
                value is not None
                for value in (
                    self.company_id,
                    self.entity_type,
                    self.email_exists,
                    self.phone_exists,
                    self.linkedin_exists,
                )
            )
            if not has_filter:
                raise ValueError(
                    "a pure filter search (embeds == []) requires at least one filter"
                )
        return self


class SimilarityHitOut(BaseModel):
    score: float | None = None
    lead: LeadOut


class SimilaritySearchResponse(BaseModel):
    results: list[SimilarityHitOut] = Field(default_factory=list)
