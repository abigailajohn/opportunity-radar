from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from opportunity_radar.deduplication import canonical_url
from opportunity_radar.discovery import DiscoveryResult, discover
from opportunity_radar.discovery_models import (
    ChangeClassification, DiscoveryFailure, DiscoveryFailureStage, SourceConfiguration,
)
from opportunity_radar.models import Opportunity, OpportunityAssessment, OpportunityProfile, OpportunityStatus, PriorityBand
from opportunity_radar.persistence import PersistenceOutcome, PersistenceStore
from opportunity_radar.pipeline import run_pipeline
from opportunity_radar.providers import OpportunityExtractor, PageFetcher, SemanticAssessor


class CachedPageFetcher:
    def __init__(self, cache: dict[str, object], fallback: PageFetcher) -> None:
        self.cache = cache; self.fallback = fallback

    def fetch(self, url: str):
        return self.cache.get(canonical_url(url)) or self.fallback.fetch(url)


@dataclass(frozen=True)
class Milestone2Result:
    discovery: DiscoveryResult
    opportunities: list[Opportunity]
    assessments: list[OpportunityAssessment]
    persistence: list[PersistenceOutcome]
    failures: list[DiscoveryFailure]
    digest: str

    @property
    def counts(self) -> dict[str, int]:
        changes = Counter(item.classification.value for item in self.persistence)
        classifications = Counter(item.classification.value for item in self.discovery.candidates)
        return {
            "sources_fetched": self.discovery.sources_fetched,
            "candidate_links_found": len(self.discovery.candidates),
            "specific_opportunities_evaluated": len(self.assessments),
            "new_opportunities": changes[ChangeClassification.NEW.value],
            "changed_opportunities": changes[ChangeClassification.CHANGED.value],
            "failures": len(self.failures),
            **{f"classification_{key}": value for key, value in classifications.items()},
        }


def _digest_card(opportunity: Opportunity, assessment: OpportunityAssessment, suffix: str = "") -> list[str]:
    deadline = opportunity.deadline.date().isoformat() if opportunity.deadline else "Rolling" if opportunity.rolling_application else "Unknown"
    return [
        f"### {opportunity.title}{suffix}",
        f"- Organization: {opportunity.organization or 'Unknown'}",
        f"- Priority: {assessment.priority_band.value.replace('_', ' ').title()} ({assessment.total_score:g}/100)",
        f"- Status: {opportunity.status.value.replace('_', ' ').title()}",
        f"- Deadline: {deadline}",
        f"- Recommended action: {assessment.recommended_action.value.replace('_', ' ').title()}",
        f"- Link: {opportunity.official_url or opportunity.source_url}", "",
    ]


def render_milestone2_digest(opportunities: list[Opportunity], assessments: list[OpportunityAssessment], outcomes: list[PersistenceOutcome]) -> str:
    by_id = {item.id: item for item in opportunities}; assessment_by_id = {item.opportunity_id: item for item in assessments}; outcome_by_id = {item.opportunity.id: item for item in outcomes}
    sections: list[tuple[str, list[Opportunity], bool]] = [
        ("New Opportunities", [item.opportunity for item in outcomes if item.classification is ChangeClassification.NEW], False),
        ("Changed Opportunities", [item.opportunity for item in outcomes if item.classification is ChangeClassification.CHANGED], True),
        ("Closing Soon", [item for item in opportunities if item.status is OpportunityStatus.CLOSING_SOON], False),
        ("Strong Matches", [by_id[item.opportunity_id] for item in assessments if item.priority_band in {PriorityBand.EXCEPTIONAL, PriorityBand.STRONG_MATCH}], False),
    ]
    lines = ["# Opportunity Radar Discovery Digest", ""]
    for heading, items, show_changes in sections:
        lines.extend([f"## {heading}", ""])
        if not items:
            lines.extend(["None.", ""]); continue
        seen: set[object] = set()
        for opportunity in items:
            if opportunity.id in seen or opportunity.id not in assessment_by_id: continue
            seen.add(opportunity.id); outcome = outcome_by_id.get(opportunity.id)
            suffix = f" — changed: {', '.join(outcome.changed_fields)}" if show_changes and outcome else ""
            lines.extend(_digest_card(opportunity, assessment_by_id[opportunity.id], suffix))
    return "\n".join(lines)


def run_discovery_pipeline(
    configuration: SourceConfiguration, profile: OpportunityProfile, fetcher: PageFetcher,
    extractor: OpportunityExtractor, assessor: SemanticAssessor, store: PersistenceStore,
    *, as_of: date, opportunity_transform=None, now: datetime | None = None,
) -> Milestone2Result:
    timestamp = now or datetime.now(timezone.utc); failures: list[DiscoveryFailure] = []
    try: store.persist_sources(configuration.sources, checked_at=timestamp)
    except Exception as exc: failures.append(DiscoveryFailure(stage=DiscoveryFailureStage.PERSISTENCE, reason=str(exc)))
    discovery = discover(configuration, fetcher, now=timestamp); failures.extend(discovery.failures)
    try: store.persist_candidates(discovery.candidates)
    except Exception as exc: failures.append(DiscoveryFailure(stage=DiscoveryFailureStage.PERSISTENCE, reason=str(exc)))
    urls = discovery.specific_urls
    cached = CachedPageFetcher(discovery.pages, fetcher)
    pipeline = run_pipeline(urls, profile, cached, extractor, assessor, as_of=as_of, opportunity_transform=opportunity_transform)
    for failure in pipeline.failures:
        failures.append(DiscoveryFailure(stage=DiscoveryFailureStage.EXTRACTION, url=failure.url, reason=f"{failure.stage.value}: {failure.reason}"))
    source_by_url = {canonical_url(item.url): item.source_id for item in discovery.candidates}
    outcomes: list[PersistenceOutcome] = []
    for opportunity in pipeline.opportunities:
        source_id = source_by_url.get(canonical_url(opportunity.source_url)) or source_by_url.get(canonical_url(opportunity.official_url or opportunity.source_url))
        if source_id is None:
            failures.append(DiscoveryFailure(stage=DiscoveryFailureStage.PERSISTENCE, url=str(opportunity.source_url), reason="could not map opportunity to discovery source")); continue
        try: outcomes.append(store.persist_opportunity(opportunity, source_id=source_id, seen_at=timestamp))
        except Exception as exc: failures.append(DiscoveryFailure(stage=DiscoveryFailureStage.PERSISTENCE, source_id=source_id, url=str(opportunity.source_url), reason=str(exc)))
    digest = render_milestone2_digest(pipeline.opportunities, pipeline.assessments, outcomes)
    store.mark_digested([item.database_id for item in outcomes if item.classification in {ChangeClassification.NEW, ChangeClassification.CHANGED}], at=timestamp)
    return Milestone2Result(discovery, pipeline.opportunities, pipeline.assessments, outcomes, failures, digest)


def write_milestone2_outputs(result: Milestone2Result, output_dir: str | Path) -> None:
    destination = Path(output_dir); destination.mkdir(parents=True, exist_ok=True)
    payloads = {
        "discovery_candidates.json": json.dumps([item.model_dump(mode="json") for item in result.discovery.candidates], indent=2),
        "opportunities.json": json.dumps([item.model_dump(mode="json") for item in result.opportunities], indent=2),
        "assessments.json": json.dumps([item.model_dump(mode="json") for item in result.assessments], indent=2),
        "failures.json": json.dumps([item.model_dump(mode="json") for item in result.failures], indent=2),
        "digest.md": result.digest,
    }
    for name, content in payloads.items(): (destination / name).write_text(content + "\n", encoding="utf-8")
