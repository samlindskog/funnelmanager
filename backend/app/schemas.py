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

    model_config = {"from_attributes": True}


class SearchHistoryDetail(SearchHistorySummary):
    results: list[dict[str, Any]]


class SearchResponse(BaseModel):
    history: SearchHistoryDetail
    pagination: dict[str, Any]


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
