from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class LoginRequest(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    username: str
    role: str = ""


class SearchRequest(BaseModel):
    query: str = Field(default="", max_length=512)
    entity_type: Literal["people", "companies"] = "people"
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=100, ge=1, le=100)
    # Optional people-search company filter (unset = no company constraint)
    organization_id: str | None = Field(default=None, max_length=128)
    organization_name: str | None = Field(default=None, max_length=255)
    # Display-only company name for history labels when searching by organization_id
    organization_display_name: str | None = Field(default=None, max_length=255)
    # Optional domain used with people company filters / fallback
    organization_domain: str | None = Field(default=None, max_length=255)
    # Company search fields (inclusive-or: at least one required)
    company_name: str | None = Field(default=None, max_length=255)
    company_domain: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def require_query_or_company_for_people(self) -> "SearchRequest":
        query = self.query.strip()
        org_id = (self.organization_id or "").strip()
        org_name = (self.organization_name or "").strip()
        company_name = (self.company_name or "").strip()
        company_domain = (self.company_domain or "").strip()
        if self.entity_type == "companies":
            if not query and not company_name and not company_domain:
                raise ValueError(
                    "company search requires keywords, company name, and/or domain"
                )
            return self
        if not query and not org_id and not org_name:
            raise ValueError(
                "people search requires search text and/or a company (organization_id or organization_name)"
            )
        return self


class CompanyPeopleSearchRequest(BaseModel):
    organization_id: str | None = Field(default=None, max_length=128)
    domain: str | None = Field(default=None, max_length=255)
    company_name: str = Field(min_length=1, max_length=255)
    keywords: str | None = Field(default=None, max_length=512)
    page: int = Field(default=1, ge=1)
    per_page: int = Field(default=100, ge=1, le=100)


class SearchHistorySummary(BaseModel):
    id: int
    query: str
    entity_type: str
    page: int
    per_page: int
    total_results: int
    created_at: datetime
    # Attribution (principle 1 cross-user history): the initiating human and how
    # the request was made. `username` was previously implicit (owner-only reads);
    # now that any user can browse any user's history it is surfaced so the UI can
    # render whose search it is + "alice (via agent)".
    username: str = ""
    origin: str = "user"
    actor: str = ""

    model_config = {"from_attributes": True}


class SearchHistoryDetail(SearchHistorySummary):
    results: list[dict[str, Any]]


class SearchResponse(BaseModel):
    history: SearchHistoryDetail
    pagination: dict[str, Any]


class HistoryOwnerOut(BaseModel):
    """One distinct history owner + how many searches they own (browse index)."""

    username: str
    search_count: int


class McpApolloSearchRequest(BaseModel):
    """Start an Apollo people/company search (MCP surface).

    Mirrors the UI ``SearchRequest`` fields but returns synchronously with the
    created ``search_id`` + leads stream handles instead of an NDJSON stream —
    the caller watches progress via the jobs service.
    """

    entity_type: Literal["people", "companies"] = "people"
    query: str = Field(default="", max_length=512)
    per_page: int = Field(default=100, ge=1, le=100)
    organization_id: str | None = Field(default=None, max_length=128)
    organization_name: str | None = Field(default=None, max_length=255)
    organization_display_name: str | None = Field(default=None, max_length=255)
    organization_domain: str | None = Field(default=None, max_length=255)
    company_name: str | None = Field(default=None, max_length=255)
    company_domain: str | None = Field(default=None, max_length=255)

    @model_validator(mode="after")
    def require_query_or_company(self) -> "McpApolloSearchRequest":
        query = self.query.strip()
        org_id = (self.organization_id or "").strip()
        org_name = (self.organization_name or "").strip()
        company_name = (self.company_name or "").strip()
        company_domain = (self.company_domain or "").strip()
        if self.entity_type == "companies":
            if not query and not company_name and not company_domain:
                raise ValueError(
                    "company search requires keywords, company name, and/or domain"
                )
            return self
        if not query and not org_id and not org_name:
            raise ValueError(
                "people search requires search text and/or a company "
                "(organization_id or organization_name)"
            )
        return self


class McpSearchStartResponse(BaseModel):
    """Handles for a search started via the MCP surface.

    The search runs detached (persisting ids + publishing job events even after
    this call returns); ``ingest_stream_id`` doubles as the job id in the jobs
    service, so the caller can watch/pause/cancel it there.
    """

    search_id: int
    entity_type: str
    query: str
    ingest_stream_id: str | None = None
    embedding_stream_id: str | None = None


class McpSemanticSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    limit: int = Field(default=25, ge=1, le=10000)


class McpEnrichRequest(BaseModel):
    """Enrich one or more leads by Apollo id (MCP surface).

    ``kind`` selects the leads operation (complete-person enrich, people/match
    waterfall, or complete-org enrich). Returns the hydrated records; Apollo is
    reached only through leads.
    """

    ids: list[str] = Field(min_length=1, max_length=100)
    kind: Literal["person", "match", "organization"] = "person"
    run_waterfall_email: bool = False
    run_waterfall_phone: bool = False
    reveal_phone_number: bool = False


class McpExportRequest(BaseModel):
    """Export a search's stored results as a record list (MCP surface).

    ``exclude_contacted`` drops leads already messaged by a campaign, read from
    mail's contacted set over the search->mail hop. ``campaign_id`` scopes the
    exclusion to one campaign; omit it to exclude anyone contacted by any campaign.
    If mail can't be consulted the filter is a graceful no-op (nothing dropped) and
    the response reports ``exclude_contacted_applied=false``; the authoritative
    dedupe still happens at send time in mail.
    """

    exclude_contacted: bool = False
    campaign_id: str | None = Field(default=None, max_length=128)


class McpExportRow(BaseModel):
    mongo_id: str | None = None
    name: str = ""
    email: str | None = None
    entity_type: str = "person"


class McpExportResponse(BaseModel):
    search_id: int
    total: int
    returned: int
    excluded_count: int = 0
    exclude_contacted_requested: bool = False
    exclude_contacted_applied: bool = False
    results: list[McpExportRow] = Field(default_factory=list)


class SimilaritySearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=8000)
    limit: int = Field(default=25, ge=1, le=10000)


class SimilarityHitOut(BaseModel):
    score: float
    record: dict[str, Any]


class SimilaritySearchResponse(BaseModel):
    query: str
    results: list[SimilarityHitOut] = Field(default_factory=list)
    history: SearchHistoryDetail
