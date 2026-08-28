from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from opportunity_radar.deduplication import canonical_url
from opportunity_radar.discovery_models import ChangeClassification, SourceType, TrustedSource
from opportunity_radar.milestone2 import _digest_card
from opportunity_radar.models import Opportunity, OpportunityAssessment, OpportunityProfile, OpportunityStatus, PriorityBand, ProcessingStage
from opportunity_radar.persistence import PersistenceOutcome, PersistenceStore
from opportunity_radar.page_shape import PageShape, classify_page_shape
from opportunity_radar.pipeline import run_pipeline
from opportunity_radar.providers import OpportunityExtractor, PageFetcher, SemanticAssessor
from opportunity_radar.search import SearchProvider, collect_search_candidates, generate_search_queries
from opportunity_radar.search_models import SearchCandidate, SearchConfiguration, SearchFailure, SearchFailureStage, SearchQuery


_NON_EXTRACTABLE_SHAPES = {
    PageShape.MULTI_OPPORTUNITY_LISTING,
    PageShape.GENERIC_ADVICE_OR_ARTICLE,
    PageShape.RECURRING_PROGRAMME_LANDING,
    PageShape.JOB_BOARD_OR_CAREERS_LANDING,
    PageShape.ORGANIZATION_PAGE,
}


class PageShapeObservingExtractor:
    """Record page-shape outcomes without changing the wrapped extractor's decisions."""

    def __init__(self, wrapped: OpportunityExtractor, candidates: list[SearchCandidate]) -> None:
        self.wrapped = wrapped
        self.candidates = candidates

    def extract(self, page):
        keys = {canonical_url(page.requested_url), canonical_url(page.final_url)}
        candidate = next((item for item in self.candidates if canonical_url(item.url) in keys), None)
        classification = classify_page_shape(page)
        if candidate is not None:
            candidate.page_shape = classification.shape
            candidate.page_shape_signals = list(classification.signals)
            candidate.proceeded_to_extraction = classification.shape not in _NON_EXTRACTABLE_SHAPES
            candidate.rejection_reason = None
        try:
            return self.wrapped.extract(page)
        except Exception as exc:
            if candidate is not None:
                candidate.rejection_reason = str(exc)
            raise


def _source_id(provider: str) -> str:
    clean = re.sub(r"[^a-z0-9_-]+", "-", provider.casefold()).strip("-") or "provider"
    return f"search-{clean}"[:100]


def _search_source(provider: str) -> TrustedSource:
    source_id = _source_id(provider)
    return TrustedSource(
        id=source_id,
        name=f"Open-web search: {provider}",
        url=f"https://search.opportunity-radar.invalid/{source_id}",
        source_type=SourceType.OPPORTUNITY_HUB,
        enabled=True,
        category_focus=[],
        check_frequency="on_demand",
        max_links_per_run=100,
        notes="Virtual source representing open-web search provenance.",
    )


@dataclass(frozen=True)
class Milestone3Result:
    queries: list[SearchQuery]
    candidates: list[SearchCandidate]
    opportunities: list[Opportunity]
    assessments: list[OpportunityAssessment]
    persistence: list[PersistenceOutcome]
    failures: list[SearchFailure]
    digest: str

    @property
    def counts(self) -> dict[str, int]:
        changes = Counter(item.classification.value for item in self.persistence)
        modes = Counter(item.query_mode.value for item in self.candidates)
        return {
            "queries": len(self.queries),
            "candidates": len(self.candidates),
            "specific_opportunities_evaluated": len(self.assessments),
            "new_opportunities": changes[ChangeClassification.NEW.value],
            "unchanged_opportunities": changes[ChangeClassification.KNOWN_UNCHANGED.value],
            "changed_opportunities": changes[ChangeClassification.CHANGED.value],
            "failures": len(self.failures),
            **{f"candidate_mode_{key}": value for key, value in modes.items()},
        }


def _candidate_for(opportunity: Opportunity, candidates: list[SearchCandidate]) -> SearchCandidate | None:
    keys = {canonical_url(opportunity.source_url), canonical_url(opportunity.official_url or opportunity.source_url)}
    return next((item for item in candidates if canonical_url(item.url) in keys), None)


def render_milestone3_digest(
    opportunities: list[Opportunity], assessments: list[OpportunityAssessment],
    outcomes: list[PersistenceOutcome], candidates: list[SearchCandidate],
) -> str:
    assessment_by_id = {item.opportunity_id: item for item in assessments}
    changed = {item.opportunity.id: item for item in outcomes if item.classification in {ChangeClassification.NEW, ChangeClassification.CHANGED}}
    actionable = [item.opportunity for item in outcomes if item.opportunity.id in changed]
    sections: list[tuple[str, list[Opportunity]]] = [
        ("New Opportunities", [item.opportunity for item in outcomes if item.classification is ChangeClassification.NEW]),
        ("Changed Opportunities", [item.opportunity for item in outcomes if item.classification is ChangeClassification.CHANGED]),
        ("Closing Soon", [item for item in actionable if item.status is OpportunityStatus.CLOSING_SOON]),
        ("Strong Matches", [item for item in actionable if assessment_by_id.get(item.id) and assessment_by_id[item.id].priority_band in {PriorityBand.EXCEPTIONAL, PriorityBand.STRONG_MATCH}]),
    ]
    lines = ["# Opportunity Radar Open-Web Search Digest", ""]
    for heading, items in sections:
        lines.extend([f"## {heading}", ""])
        if not items:
            lines.extend(["None.", ""])
            continue
        for opportunity in items:
            assessment = assessment_by_id.get(opportunity.id)
            if assessment is None:
                continue
            candidate = _candidate_for(opportunity, candidates)
            label = "Match Search" if candidate and candidate.query_mode.value == "match" else "Discovery Search"
            suffix = ""
            outcome = changed.get(opportunity.id)
            if heading == "Changed Opportunities" and outcome:
                suffix = f" — changed: {', '.join(outcome.changed_fields)}"
            card = _digest_card(opportunity, assessment, suffix)
            card.insert(-1, f"- Discovered via: {label}")
            lines.extend(card)
    return "\n".join(lines)


def run_search_pipeline(
    profile: OpportunityProfile, provider: SearchProvider, fetcher: PageFetcher,
    extractor: OpportunityExtractor, assessor: SemanticAssessor, store: PersistenceStore,
    *, configuration: SearchConfiguration | None = None, as_of: date,
    opportunity_transform=None, now: datetime | None = None,
) -> Milestone3Result:
    config = configuration or SearchConfiguration()
    timestamp = now or datetime.now(timezone.utc)
    failures: list[SearchFailure] = []
    queries = generate_search_queries(profile, config, rotation_date=timestamp.date())
    candidates, search_errors = collect_search_candidates(queries, provider, config, now=timestamp)
    for query, exc in search_errors:
        failures.append(SearchFailure(stage=SearchFailureStage.SEARCH, reason=str(exc), query=query.text, provider=provider.name))

    sources = [_search_source(name) for name in sorted({item.provider for item in candidates} or {provider.name})]
    try:
        store.persist_sources(sources, checked_at=timestamp)
    except Exception as exc:
        failures.append(SearchFailure(stage=SearchFailureStage.PERSISTENCE, reason=str(exc), provider=provider.name))

    selected = sorted(candidates, key=lambda item: (-item.search_score, item.search_rank, canonical_url(item.url)))[: config.fetch_cap]
    observing_extractor = PageShapeObservingExtractor(extractor, selected)
    pipeline = run_pipeline(
        [str(item.url) for item in selected], profile, fetcher, observing_extractor, assessor,
        as_of=as_of, opportunity_transform=opportunity_transform,
    )
    for failure in pipeline.failures:
        stage = SearchFailureStage.CANDIDATE_FETCH if failure.stage is ProcessingStage.FETCH else SearchFailureStage.EXTRACTION
        failures.append(SearchFailure(stage=stage, url=failure.url, reason=failure.reason, provider=provider.name))

    try:
        store.persist_search_candidates(candidates)
    except Exception as exc:
        failures.append(SearchFailure(stage=SearchFailureStage.PERSISTENCE, reason=str(exc), provider=provider.name))

    outcomes: list[PersistenceOutcome] = []
    for opportunity in pipeline.opportunities:
        candidate = _candidate_for(opportunity, selected)
        if candidate is None:
            failures.append(SearchFailure(stage=SearchFailureStage.PERSISTENCE, url=str(opportunity.source_url), reason="could not map extracted opportunity to search candidate"))
            continue
        try:
            outcome = store.persist_opportunity(opportunity, source_id=_source_id(candidate.provider), seen_at=timestamp)
            store.persist_search_provenance(outcome.database_id, candidate)
            outcomes.append(outcome)
        except Exception as exc:
            failures.append(SearchFailure(stage=SearchFailureStage.PERSISTENCE, url=str(candidate.url), reason=str(exc), provider=candidate.provider))

    digest = render_milestone3_digest(pipeline.opportunities, pipeline.assessments, outcomes, selected)
    try:
        store.mark_digested([item.database_id for item in outcomes if item.classification in {ChangeClassification.NEW, ChangeClassification.CHANGED}], at=timestamp)
    except Exception as exc:
        failures.append(SearchFailure(stage=SearchFailureStage.PERSISTENCE, reason=str(exc), provider=provider.name))
    return Milestone3Result(queries, candidates, pipeline.opportunities, pipeline.assessments, outcomes, failures, digest)


def write_milestone3_outputs(result: Milestone3Result, output_dir: str | Path) -> None:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    payloads = {
        "search_queries.json": json.dumps([item.model_dump(mode="json") for item in result.queries], indent=2),
        "search_candidates.json": json.dumps([item.model_dump(mode="json") for item in result.candidates], indent=2),
        "opportunities.json": json.dumps([item.model_dump(mode="json") for item in result.opportunities], indent=2),
        "assessments.json": json.dumps([item.model_dump(mode="json") for item in result.assessments], indent=2),
        "failures.json": json.dumps([item.model_dump(mode="json") for item in result.failures], indent=2),
        "digest.md": result.digest,
    }
    for name, content in payloads.items():
        (destination / name).write_text(content + "\n", encoding="utf-8")
