from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl

from opportunity_radar.page_shape import PageShape


class SearchQueryMode(StrEnum):
    MATCH = "match"
    DISCOVERY = "discovery"


class SearchQuery(BaseModel):
    text: str = Field(min_length=3)
    mode: SearchQueryMode
    family: str


class SearchResult(BaseModel):
    title: str
    url: HttpUrl
    snippet: str = ""
    rank: int = Field(ge=1)
    query: str
    provider: str


class SearchProvenance(BaseModel):
    query: str
    query_mode: SearchQueryMode
    provider: str
    search_rank: int = Field(ge=1)


class SearchCandidate(BaseModel):
    url: HttpUrl
    title: str
    snippet: str = ""
    query: str
    query_mode: SearchQueryMode
    search_rank: int = Field(ge=1)
    provider: str
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    search_score: int = 0
    search_signals: list[str] = Field(default_factory=list)
    provenance: list[SearchProvenance] = Field(default_factory=list)
    page_shape: PageShape | None = None
    page_shape_signals: list[str] = Field(default_factory=list)
    proceeded_to_extraction: bool | None = None
    rejection_reason: str | None = None


class SearchFailureStage(StrEnum):
    QUERY = "query"
    SEARCH = "search"
    CANDIDATE_FETCH = "candidate_fetch"
    EXTRACTION = "extraction"
    PERSISTENCE = "persistence"


class SearchFailure(BaseModel):
    stage: SearchFailureStage
    reason: str
    query: str | None = None
    url: str | None = None
    provider: str | None = None


class SearchConfiguration(BaseModel):
    max_queries_per_run: int = Field(default=12, ge=1, le=100)
    max_results_per_query: int = Field(default=10, ge=1, le=50)
    global_candidate_cap: int = Field(default=80, ge=1, le=500)
    fetch_cap: int = Field(default=30, ge=1, le=200)
    match_share: float = Field(default=0.65, ge=0.25, le=0.9)
