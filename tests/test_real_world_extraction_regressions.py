from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from conftest import make_opportunity
from opportunity_radar.deterministic_extraction import DeterministicOpportunityExtractor
from opportunity_radar.eligibility import evaluate_eligibility
from opportunity_radar.enrichment import OneHopOpportunityExtractor
from opportunity_radar.extraction import NotOpportunityPageError
from opportunity_radar.fetching import FetchedPage
from opportunity_radar.html_preparation import prepare_html
from opportunity_radar.models import CoverageStatus, EligibilityRequirements, EligibilityStatus


FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)


def page(name: str, url: str) -> FetchedPage:
    html = (FIXTURES / name).read_text(encoding="utf-8")
    return FetchedPage(requested_url=url, final_url=url, raw_html=html, cleaned_text=prepare_html(html, base_url=url), page_title=None, fetched_at=NOW, byte_count=len(html.encode()), character_count=len(html))


def test_table_and_definition_list_label_value_pairs_are_preserved() -> None:
    cyber = page("cybersafe_sans.html", "https://official.example/cybersafe")
    libra = page("libra_primary.html", "https://official.example/libra")
    assert "Applications close | September 10, 2026" in cyber.cleaned_text
    assert "Orientation | June 4, 2027" in libra.cleaned_text
    assert "Internship begins | June 7, 2027" in libra.cleaned_text


def test_strong_label_is_used_as_a_bounded_section_heading() -> None:
    html = "<html><body><h1>Security Fellowship</h1><p><strong>Requirements</strong></p><ul><li>Applicants must be at least 18.</li></ul><p><strong>Benefits</strong></p><p>Mentorship is provided.</p></body></html>"
    fetched = FetchedPage(requested_url="https://example.test/fellowship", final_url="https://example.test/fellowship", raw_html=html, cleaned_text=prepare_html(html, base_url="https://example.test/fellowship"), fetched_at=NOW)
    opportunity = DeterministicOpportunityExtractor().extract(fetched)
    assert opportunity.eligibility.raw_text and "at least 18" in opportunity.eligibility.raw_text
    assert "Mentorship" in opportunity.funding.other_benefits


def test_cybersafe_regression_extracts_only_grounded_facts() -> None:
    opportunity = DeterministicOpportunityExtractor().extract(page("cybersafe_sans.html", "https://official.example/cybersafe"))
    assert opportunity.category == "Fellowship"
    assert opportunity.opening_date == date(2026, 8, 19)
    assert opportunity.deadline and opportunity.deadline.date() == date(2026, 9, 10)
    assert opportunity.program_start_date == date(2026, 10, 26)
    assert opportunity.eligibility.raw_text and "African women" in opportunity.eligibility.raw_text
    assert opportunity.eligibility.minimum_age == 21
    assert opportunity.eligibility.minimum_age_exclusive is True
    assert opportunity.eligibility.nationalities_allowed == []
    assert opportunity.eligibility.regions_allowed == ["Africa"]
    assert opportunity.eligibility.residence_requirements == ["Africa"]
    assert opportunity.eligibility.gender_requirements == ["woman"]
    assert opportunity.eligibility.minimum_years_experience == 3
    assert "Fully funded training" in opportunity.funding.other_benefits
    assert "Certification included" in opportunity.funding.other_benefits
    assert opportunity.funding.flights.status is CoverageStatus.UNKNOWN
    assert str(opportunity.application_url) == "https://apply.example.test/cybersafe-2026"
    assert all(item.evidence_text and item.evidence_text in opportunity.eligibility.raw_text or item.evidence_text in page("cybersafe_sans.html", "https://official.example/cybersafe").cleaned_text for item in opportunity.evidence)


@pytest.mark.parametrize(
    ("wording", "minimum", "exclusive", "age", "expected"),
    (("older than 21", 21, True, 21, EligibilityStatus.NOT_ELIGIBLE), ("21 or older", 21, False, 21, EligibilityStatus.ELIGIBLE), ("at least 18", 18, False, 18, EligibilityStatus.ELIGIBLE)),
)
def test_strict_and_inclusive_age_semantics(profile, wording, minimum, exclusive, age, expected) -> None:
    requirements = EligibilityRequirements(minimum_age=minimum, minimum_age_exclusive=exclusive, requirements_complete=True)
    opportunity = make_opportunity(eligibility=requirements)
    identity = profile.identity.model_copy(update={"age": profile.identity.age.model_copy(update={"current": age})})
    candidate = profile.model_copy(update={"identity": identity})
    decision = evaluate_eligibility(opportunity, candidate, date(2026, 8, 28))
    assert decision.status is expected


class ApplicationFetcher:
    def __init__(self, application_page: FetchedPage) -> None:
        self.application_page = application_page
        self.urls: list[str] = []

    def fetch(self, url: str) -> FetchedPage:
        self.urls.append(url)
        return self.application_page


def test_libra_one_hop_enrichment_and_date_classification() -> None:
    primary_page = page("libra_primary.html", "https://official.example/libra")
    application_page = page("libra_application.html", "https://jobs.example.test/libra-internship-2027")
    base = DeterministicOpportunityExtractor()
    primary = base.extract(primary_page)
    assert primary.deadline is None
    assert primary.program_start_date == date(2027, 6, 7)
    assert primary.program_end_date == date(2027, 8, 6)
    fetcher = ApplicationFetcher(application_page)
    opportunity = OneHopOpportunityExtractor(base, fetcher).extract(primary_page)
    assert fetcher.urls == ["https://jobs.example.test/libra-internship-2027"]
    assert opportunity.deadline and opportunity.deadline.date() == date(2026, 11, 15)
    assert opportunity.eligibility.student_required is True
    assert opportunity.eligibility.minimum_age == 18 and not opportunity.eligibility.minimum_age_exclusive
    assert opportunity.eligibility.regions_allowed == ["International"]
    assert opportunity.funding.paid is True
    assert opportunity.funding.flights.status is CoverageStatus.COVERED
    assert opportunity.funding.visa_fees.status is CoverageStatus.COVERED
    assert opportunity.funding.accommodation.status is CoverageStatus.UNKNOWN
    assert opportunity.application.cv_required is True
    assert opportunity.application.transcript_required is True
    assert opportunity.extraction_diagnostics.enrichment_attempted is True
    assert str(opportunity.extraction_diagnostics.enrichment_source_url) == "https://jobs.example.test/libra-internship-2027"
    deadline_evidence = next(item for item in opportunity.evidence if item.field == "deadline")
    assert str(deadline_evidence.source_url) == "https://jobs.example.test/libra-internship-2027"


def test_generic_homepage_is_rejected() -> None:
    html = "<html><head><title>Example Foundation</title><meta property='og:site_name' content='Example Foundation'></head><body><h1>Example Foundation</h1><p>We build a better future.</p></body></html>"
    fetched = FetchedPage(requested_url="https://example.test", final_url="https://example.test", raw_html=html, cleaned_text=prepare_html(html, base_url="https://example.test"), fetched_at=NOW)
    with pytest.raises(NotOpportunityPageError) as error:
        DeterministicOpportunityExtractor().extract(fetched)
    assert error.value.kind.value == "not_opportunity_page"
