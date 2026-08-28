from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from opportunity_radar.deterministic_assessment import DeterministicSemanticAssessor
from opportunity_radar.deterministic_extraction import DeterministicOpportunityExtractor
from opportunity_radar.discovery_models import ChangeClassification, SourceType, TrustedSource
from opportunity_radar.extraction import NotOpportunityPageError
from opportunity_radar.fetching import FetchedPage
from opportunity_radar.html_preparation import prepare_html
from opportunity_radar.milestone3 import run_search_pipeline
from opportunity_radar.page_shape import PageShape, classify_page_shape
from opportunity_radar.persistence import InMemoryOpportunityStore
from opportunity_radar.providers import FakePageFetcher
from opportunity_radar.search import FakeSearchProvider, collect_search_candidates, generate_search_queries, score_search_result
from opportunity_radar.search_models import SearchConfiguration, SearchResult


FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)


def fixture_page(name: str, url: str) -> FetchedPage:
    html = (FIXTURES / name).read_text(encoding="utf-8")
    return FetchedPage(
        requested_url=url, final_url=url, raw_html=html,
        cleaned_text=prepare_html(html, base_url=url), fetched_at=NOW,
    )


def test_multi_opportunity_aggregator_is_not_merged_into_one_opportunity() -> None:
    page = fixture_page("search_aggregator_multiple.html", "https://listing.example/cloud-devops")
    classification = classify_page_shape(page)
    assert classification.shape is PageShape.MULTI_OPPORTUNITY_LISTING
    assert len(classification.child_opportunity_urls) == 3
    with pytest.raises(NotOpportunityPageError, match="multi_opportunity_listing"):
        DeterministicOpportunityExtractor().extract(page)


def test_multi_opportunity_page_retains_discovery_candidate_value_without_opportunity(profile) -> None:
    url = "https://listing.example/cloud-devops"
    page = fixture_page("search_aggregator_multiple.html", url)
    config = SearchConfiguration(max_queries_per_run=1, max_results_per_query=1, global_candidate_cap=1, fetch_cap=1)
    query = generate_search_queries(profile, config, rotation_date=NOW.date())[0]
    provider = FakeSearchProvider({query.text: [SearchResult(title="Cloud scholarships 2026", url=url, snippet="Scholarship listing with deadlines and application links", rank=1, query=query.text, provider="fake")]})
    candidates, failures = collect_search_candidates([query], provider, config, now=NOW)
    assert not failures and len(candidates) == 1
    assert classify_page_shape(page).child_opportunity_urls
    with pytest.raises(NotOpportunityPageError):
        DeterministicOpportunityExtractor().extract(page)


def test_generic_coursera_style_advice_article_is_rejected_even_with_high_search_score() -> None:
    url = "https://example.test/articles/how-to-get-a-cybersecurity-internship"
    page = fixture_page("search_coursera_advice.html", url)
    search_result = SearchResult(
        title="How to Get a Cybersecurity Internship: Your 2026 Guide", url=url,
        snippet="Apply now: applications and internship deadline 2026 with student requirements", rank=1,
        query="cybersecurity internship", provider="fake",
    )
    score, _ = score_search_result(search_result)
    assert score >= 6
    assert classify_page_shape(page).shape is PageShape.GENERIC_ADVICE_OR_ARTICLE
    with pytest.raises(NotOpportunityPageError, match="generic_advice_or_article"):
        DeterministicOpportunityExtractor().extract(page)


def test_recurring_internship_landing_without_current_cycle_is_not_actionable() -> None:
    page = fixture_page("search_bishop_recurring.html", "https://example.test/company/internships")
    assert classify_page_shape(page).shape is PageShape.RECURRING_PROGRAMME_LANDING
    with pytest.raises(NotOpportunityPageError, match="recurring_programme_landing"):
        DeterministicOpportunityExtractor().extract(page)


def test_explicit_plural_fellowship_title_maps_to_fellowship() -> None:
    page = fixture_page("search_alc_fellowship.html", "https://example.test/call-for-applications")
    opportunity = DeterministicOpportunityExtractor().extract(page)
    assert classify_page_shape(page).shape is PageShape.SPECIFIC_OPPORTUNITY
    assert opportunity.category == "Fellowship"
    assert opportunity.application_url is not None


def test_page_shape_outcomes_and_signals_round_trip_through_persistence(profile) -> None:
    specific_url = "https://example.test/call-for-applications"
    advice_url = "https://example.test/articles/how-to-get-an-internship"
    specific = fixture_page("search_alc_fellowship.html", specific_url)
    advice = fixture_page("search_coursera_advice.html", advice_url)
    config = SearchConfiguration(max_queries_per_run=1, max_results_per_query=2, global_candidate_cap=2, fetch_cap=2)
    query = generate_search_queries(profile, config, rotation_date=NOW.date())[0]
    provider = FakeSearchProvider({query.text: [
        SearchResult(title="Call for Applications: Fellowships", url=specific_url, snippet="Apply for the 2026 fellowship", rank=1, query=query.text, provider="fake"),
        SearchResult(title="How to Get an Internship", url=advice_url, snippet="Internship application guide", rank=2, query=query.text, provider="fake"),
    ]})
    store = InMemoryOpportunityStore()
    run = run_search_pipeline(
        profile, provider, FakePageFetcher({specific_url: specific, advice_url: advice}),
        DeterministicOpportunityExtractor(), DeterministicSemanticAssessor(), store,
        configuration=config, as_of=NOW.date(), now=NOW,
    )
    persisted = {str(item.url): item for item in store.search_candidates}
    accepted = persisted[specific_url]
    rejected = persisted[advice_url]
    assert accepted.page_shape is PageShape.SPECIFIC_OPPORTUNITY
    assert accepted.page_shape_signals and accepted.proceeded_to_extraction is True
    assert accepted.rejection_reason is None
    assert rejected.page_shape is PageShape.GENERIC_ADVICE_OR_ARTICLE
    assert rejected.page_shape_signals and rejected.proceeded_to_extraction is False
    assert "generic_advice_or_article" in (rejected.rejection_reason or "")
    assert len(run.assessments) == 1 and len(run.failures) == 1


def test_jobposting_metadata_supplies_specific_title_organization_deadline_and_location() -> None:
    page = fixture_page("search_experian_jobposting.html", "https://jobs.example.test/cloud-intern")
    opportunity = DeterministicOpportunityExtractor().extract(page)
    assert classify_page_shape(page).shape is PageShape.SPECIFIC_OPPORTUNITY
    assert opportunity.title == "Cloud Engineering Summer Intern"
    assert opportunity.organization == "Example Employer"
    assert opportunity.category == "Internship"
    assert opportunity.deadline and opportunity.deadline.date().isoformat() == "2026-09-30"
    assert opportunity.participation_mode.value == "remote"
    assert opportunity.city == "Remote" and opportunity.country == "US"
    assert opportunity.eligibility.student_required is True


@pytest.mark.parametrize(
    ("fixture", "url", "category"),
    [
        ("search_harvard_bootcamp.html", "https://example.test/deep-tech", "Unknown"),
        ("search_cisa_interns.html", "https://example.test/cyber-interns", "Internship"),
    ],
)
def test_genuine_static_pages_pass_with_explicit_current_evidence(fixture: str, url: str, category: str) -> None:
    page = fixture_page(fixture, url)
    opportunity = DeterministicOpportunityExtractor().extract(page)
    assert classify_page_shape(page).shape is PageShape.SPECIFIC_OPPORTUNITY
    assert opportunity.category == category
    assert opportunity.application_url is not None


def test_existing_cybersafe_canonical_record_remains_unchanged() -> None:
    url = "https://cybersafefoundation.org/our-programs/cybersafe-sans-ai-security-fellowship"
    page = fixture_page("cybersafe_sans.html", url)
    opportunity = DeterministicOpportunityExtractor().extract(page)
    source = TrustedSource(id="cybersafe-programmes", name="CyberSafe", url="https://cybersafefoundation.org/", source_type=SourceType.OPPORTUNITY_HUB)
    store = InMemoryOpportunityStore()
    store.persist_sources([source], checked_at=NOW)
    first = store.persist_opportunity(opportunity, source_id=source.id, seen_at=NOW)
    second = store.persist_opportunity(opportunity, source_id=source.id, seen_at=NOW)
    assert first.classification is ChangeClassification.NEW
    assert second.classification is ChangeClassification.KNOWN_UNCHANGED
    assert len(store.opportunities) == 1
