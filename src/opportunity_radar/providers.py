from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from opportunity_radar.models import MatchMode, Opportunity, OpportunityProfile
from opportunity_radar.fetching import FetchedPage
from opportunity_radar.scoring import (
    FeasibilityLevel,
    FrictionLevel,
    InformationConfidenceLevel,
    RelevanceLevel,
    TimingLevel,
    ValueLevel,
)


@dataclass(frozen=True)
class SemanticJudgment:
    relevance_level: RelevanceLevel
    relevance_reason: str
    value_level: ValueLevel
    value_reason: str
    feasibility_level: FeasibilityLevel
    feasibility_reason: str
    timing_level: TimingLevel
    timing_reason: str
    friction_level: FrictionLevel
    friction_reason: str
    confidence_level: InformationConfidenceLevel
    confidence_reason: str
    match_mode: MatchMode
    why_you: str
    why_it_matters: str
    discovery_reason: str | None = None
    top_positive_signals: tuple[str, ...] = ()
    concerns: tuple[str, ...] = ()


class PageFetcher(Protocol):
    def fetch(self, url: str) -> FetchedPage: ...


class OpportunityExtractor(Protocol):
    def extract(self, page: FetchedPage) -> Opportunity: ...


class SemanticAssessor(Protocol):
    def assess(
        self,
        opportunity: Opportunity,
        profile: OpportunityProfile,
    ) -> SemanticJudgment: ...


class UnavailablePageFetcher:
    """CLI placeholder until live fetching is explicitly approved."""

    def fetch(self, url: str) -> FetchedPage:
        raise RuntimeError("live page fetching is not enabled for evaluate_urls")


class FakePageFetcher:
    def __init__(self, pages: dict[str, FetchedPage | Exception]) -> None:
        self.pages = pages

    def fetch(self, url: str) -> FetchedPage:
        result = self.pages[url]
        if isinstance(result, Exception):
            raise result
        return result


class FakeOpportunityExtractor:
    def __init__(self, opportunities: dict[str, Opportunity | Exception]) -> None:
        self.opportunities = opportunities

    def extract(self, page: FetchedPage) -> Opportunity:
        result = self.opportunities[page.final_url]
        if isinstance(result, Exception):
            raise result
        return result.model_copy(deep=True)


class FakeSemanticAssessor:
    def __init__(self, judgments: dict[str, SemanticJudgment | Exception]) -> None:
        self.judgments = judgments

    def assess(
        self,
        opportunity: Opportunity,
        profile: OpportunityProfile,
    ) -> SemanticJudgment:
        del profile
        result = self.judgments[str(opportunity.source_url)]
        if isinstance(result, Exception):
            raise result
        return result
