from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from conftest import make_opportunity
from opportunity_radar.deterministic_assessment import DeterministicSemanticAssessor
from opportunity_radar.deterministic_extraction import DeterministicOpportunityExtractor
from opportunity_radar.discovery import discover, score_link
from opportunity_radar.discovery_models import (
    CandidateClassification, ChangeClassification, SourceConfiguration, SourceType, TrustedSource,
    load_source_configuration,
)
from opportunity_radar.fetching import FetchedPage
from opportunity_radar.html_preparation import prepare_html
from opportunity_radar.milestone2 import run_discovery_pipeline
from opportunity_radar.models import OpportunityStatus
from opportunity_radar.persistence import InMemoryOpportunityStore, POSTGRES_SCHEMA, PostgresOpportunityStore
from opportunity_radar.providers import FakePageFetcher


FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)


def fixture_page(name: str, url: str) -> FetchedPage:
    html = (FIXTURES / name).read_text(encoding="utf-8")
    return FetchedPage(requested_url=url, final_url=url, raw_html=html, cleaned_text=prepare_html(html, base_url=url), fetched_at=NOW)


def source_configuration(maximum: int = 20) -> SourceConfiguration:
    return SourceConfiguration(global_max_candidate_pages=maximum, sources=[TrustedSource(id="trusted", name="Trusted Foundation", url="https://trusted.example/", source_type=SourceType.ORGANIZATION_HOMEPAGE, category_focus=["Fellowship", "Scholarship"], max_links_per_run=20)])


def page_map() -> dict[str, FetchedPage]:
    return {
        "https://trusted.example/": fixture_page("discovery_organization.html", "https://trusted.example/"),
        "https://trusted.example/opportunities": fixture_page("discovery_hub.html", "https://trusted.example/opportunities"),
        "https://trusted.example/security-fellowship-2027": fixture_page("discovery_fellowship.html", "https://trusted.example/security-fellowship-2027"),
        "https://trusted.example/scholarship-2027": fixture_page("discovery_scholarship.html", "https://trusted.example/scholarship-2027"),
        "https://trusted.example/subhub": fixture_page("discovery_subhub.html", "https://trusted.example/subhub"),
        "https://applications.example.test/global-fellowship-2027": fixture_page("discovery_external_application.html", "https://applications.example.test/global-fellowship-2027"),
    }


class RecordingFetcher(FakePageFetcher):
    def __init__(self, pages):
        super().__init__(pages); self.requested: list[str] = []

    def fetch(self, url: str) -> FetchedPage:
        self.requested.append(url)
        return super().fetch(url)


def test_source_example_configuration_loads() -> None:
    configuration = load_source_configuration(Path(__file__).parents[1] / "config" / "sources.example.yaml")
    assert configuration.sources and all(source.max_links_per_run > 0 for source in configuration.sources)


def test_candidate_scoring_uses_positive_context_and_negative_navigation() -> None:
    source = source_configuration().sources[0]
    high, signals, forced = score_link("https://trusted.example/fellowships/security-2027", "Security Fellowship 2027 — Apply", "Current cybersecurity opportunities", source)
    low, _, negative = score_link("https://trusted.example/privacy", "Privacy", "Footer", source)
    assert high >= 6 and "same-domain signal" in signals and forced is None
    assert low < 3 and negative is CandidateClassification.NON_OPPORTUNITY


def test_bounded_discovery_deduplicates_tracking_and_never_traverses_depth_three() -> None:
    fetcher = RecordingFetcher(page_map())
    result = discover(source_configuration(), fetcher, now=NOW)
    urls = [str(item.url) for item in result.candidates]
    assert urls.count("https://trusted.example/security-fellowship-2027") == 1
    assert any(item.classification is CandidateClassification.OPPORTUNITY_HUB and item.depth == 1 for item in result.candidates)
    assert any(item.classification is CandidateClassification.OPPORTUNITY_HUB and item.depth == 2 for item in result.candidates)
    assert any(item.classification is CandidateClassification.SPECIFIC_OPPORTUNITY and item.depth == 2 for item in result.candidates)
    assert "https://applications.example.test/global-fellowship-2027" in urls
    assert not any("never-depth-3" in url for url in fetcher.requested)
    assert all(item.depth <= 2 for item in result.candidates)


def test_global_and_per_source_page_limits_are_enforced() -> None:
    fetcher = RecordingFetcher(page_map())
    configuration = source_configuration(maximum=1)
    result = discover(configuration, fetcher, now=NOW)
    assert result.sources_fetched == 1
    assert len(fetcher.requested) == 2  # source plus only one candidate page


def test_recurring_fields_require_explicit_labels() -> None:
    page = fixture_page("discovery_recurring.html", "https://trusted.example/research-fellowship")
    opportunity = DeterministicOpportunityExtractor().extract(page)
    assert opportunity.program_family == "Research Fellowship"
    assert opportunity.cycle_label == "2027 Cohort"
    assert opportunity.cycle_year == 2027


def test_persistence_detects_new_changed_and_unchanged() -> None:
    source = source_configuration().sources[0]
    original = make_opportunity(url="https://trusted.example/fellowship")
    changed = original.model_copy(update={"status": OpportunityStatus.CLOSING_SOON, "deadline": datetime(2026, 9, 1, 23, 59, tzinfo=timezone.utc)})
    store = InMemoryOpportunityStore(); store.persist_sources([source], checked_at=NOW)
    first = store.persist_opportunity(original, source_id=source.id, seen_at=NOW)
    second = store.persist_opportunity(original, source_id=source.id, seen_at=NOW)
    third = store.persist_opportunity(changed, source_id=source.id, seen_at=NOW)
    assert first.classification is ChangeClassification.NEW
    assert second.classification is ChangeClassification.KNOWN_UNCHANGED
    assert third.classification is ChangeClassification.CHANGED and {"status", "deadline"} <= set(third.changed_fields)
    assert store.version_count == 2


def test_full_offline_discovery_pipeline_and_unchanged_not_new(profile) -> None:
    configuration = source_configuration(); fetcher = RecordingFetcher(page_map())
    store = InMemoryOpportunityStore()
    first = run_discovery_pipeline(configuration, profile, fetcher, DeterministicOpportunityExtractor(), DeterministicSemanticAssessor(), store, as_of=date(2026, 8, 28), now=NOW)
    second = run_discovery_pipeline(configuration, profile, RecordingFetcher(page_map()), DeterministicOpportunityExtractor(), DeterministicSemanticAssessor(), store, as_of=date(2026, 8, 28), now=NOW)
    assert first.counts["specific_opportunities_evaluated"] >= 3
    assert first.counts["new_opportunities"] == len(first.persistence)
    assert second.counts["new_opportunities"] == 0
    assert all(item.classification is ChangeClassification.KNOWN_UNCHANGED for item in second.persistence)
    assert "## New Opportunities\n\nNone." in second.digest
    assert "## Changed Opportunities" in second.digest


def test_postgres_schema_and_environment_boundary(monkeypatch) -> None:
    assert "JSONB" in POSTGRES_SCHEMA and "TIMESTAMPTZ" in POSTGRES_SCHEMA and "BIGSERIAL" in POSTGRES_SCHEMA
    assert "AUTOINCREMENT" not in POSTGRES_SCHEMA and "PRAGMA" not in POSTGRES_SCHEMA
    monkeypatch.delenv("OPPORTUNITY_RADAR_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="OPPORTUNITY_RADAR_DATABASE_URL"):
        PostgresOpportunityStore.from_environment()
