from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, HttpUrl, model_validator


class SourceType(StrEnum):
    ORGANIZATION_HOMEPAGE = "organization_homepage"
    OPPORTUNITY_HUB = "opportunity_hub"
    JOB_BOARD = "job_board"
    EVENTS_PAGE = "events_page"
    PROGRAMME_DIRECTORY = "programme_directory"
    RECURRING_PROGRAMME = "recurring_programme"


class TrustedSource(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str
    url: HttpUrl
    source_type: SourceType
    enabled: bool = True
    category_focus: list[str] = Field(default_factory=list)
    check_frequency: str = "daily"
    max_links_per_run: int = Field(default=20, ge=1, le=100)
    notes: str | None = None


class SourceConfiguration(BaseModel):
    sources: list[TrustedSource]
    global_max_candidate_pages: int = Field(default=100, ge=1, le=100)

    @model_validator(mode="after")
    def unique_ids(self) -> SourceConfiguration:
        ids = [source.id for source in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("source ids must be unique")
        return self


class CandidateClassification(StrEnum):
    SPECIFIC_OPPORTUNITY = "specific_opportunity"
    OPPORTUNITY_HUB = "opportunity_hub"
    ORGANIZATION_PAGE = "organization_page"
    NAVIGATION = "navigation"
    NON_OPPORTUNITY = "non_opportunity"
    UNCERTAIN = "uncertain"


class DiscoveryCandidate(BaseModel):
    url: HttpUrl
    source_id: str
    discovered_from_url: HttpUrl
    depth: int = Field(ge=1, le=2)
    anchor_text: str
    nearby_context: str
    discovery_score: int
    discovery_signals: list[str] = Field(default_factory=list)
    classification: CandidateClassification
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DiscoveryFailureStage(StrEnum):
    SOURCE_FETCH = "source_fetch"
    SOURCE_PARSE = "source_parse"
    NO_CANDIDATES = "no_candidates"
    CANDIDATE_FETCH = "candidate_fetch"
    CANDIDATE_CLASSIFICATION = "candidate_classification"
    EXTRACTION = "extraction"
    PERSISTENCE = "persistence"


class DiscoveryFailure(BaseModel):
    stage: DiscoveryFailureStage
    reason: str
    source_id: str | None = None
    url: str | None = None


class ChangeClassification(StrEnum):
    NEW = "new"
    KNOWN_UNCHANGED = "known_unchanged"
    CHANGED = "changed"


def load_source_configuration(path: str | Path) -> SourceConfiguration:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("source configuration must contain a mapping")
    return SourceConfiguration.model_validate(data)
