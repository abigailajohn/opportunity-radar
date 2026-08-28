from __future__ import annotations

from datetime import date, datetime, timezone
import json

import httpx
import pytest

from opportunity_radar.composition import (
    ProviderMode,
    build_provider_bundle,
    provider_mode_from_environment,
)
from opportunity_radar.deterministic_assessment import DeterministicSemanticAssessor
from opportunity_radar.deterministic_extraction import DeterministicOpportunityExtractor, _extract_sections
from opportunity_radar.fetching import FetchedPage
from opportunity_radar.models import (
    CoverageStatus,
    EligibilityStatus,
    MatchMode,
    OpportunityStatus,
)
from opportunity_radar.overrides import (
    OpportunityOverrideApplier,
    OverrideConfiguration,
    load_overrides,
)
from opportunity_radar.pipeline import run_pipeline, write_outputs
from opportunity_radar.evaluation import evaluate_opportunity


NOW = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)


def rich_html(index: int = 1) -> str:
    return f"""
    <html><head>
      <title>Fallback title</title>
      <meta property="og:title" content="AppSec Fellowship {index}">
      <meta property="og:site_name" content="Secure Foundation">
    </head><body>
      <h1>AppSec Fellowship {index}</h1>
      <p>Location: Paris, France</p>
      <p>This is an in-person application security training programme.</p>
      <h2>Eligibility</h2>
      <p>Applicants must be a current undergraduate student aged 18 to 30.</p>
      <p>Open to citizens from Nigeria or Mauritius.</p>
      <p>Requires 2 years of professional experience. Required skills: Application Security, Python.</p>
      <h2>Important Dates</h2>
      <p>Applications open August 1, 2026.</p>
      <p>Application deadline: September 30, 2026.</p>
      <p>Programme dates: October 20, 2026 to October 25, 2026.</p>
      <h2>Funding and Benefits</h2>
      <p>Flights are covered. Accommodation is provided. Visa support is provided.</p>
      <p>A USD 2,000 stipend is provided. Meals are included. Registration is covered.</p>
      <h2>How to Apply</h2>
      <p>No application fee is charged. Submit a CV and cover letter.</p>
      <a href="/apply-{index}">Apply now</a>
    </body></html>
    """


def fetched_page(html: str, url: str = "https://example.test/programme-1") -> FetchedPage:
    from opportunity_radar.html_preparation import prepare_html

    cleaned = prepare_html(html, base_url=url)
    return FetchedPage(
        requested_url=url,
        final_url=url,
        raw_html=html,
        cleaned_text=cleaned,
        page_title="Fallback title",
        fetched_at=NOW,
        byte_count=len(html.encode()),
        character_count=len(html),
    )


def test_heading_sections_are_detected() -> None:
    sections = _extract_sections(rich_html())
    assert "eligibility" in sections
    assert "important dates" in sections
    assert "funding and benefits" in sections
    assert "how to apply" in sections


def test_deterministic_fact_extraction_and_evidence() -> None:
    opportunity = DeterministicOpportunityExtractor().extract(fetched_page(rich_html()))
    assert opportunity.title == "AppSec Fellowship 1"
    assert opportunity.organization == "Secure Foundation"
    assert opportunity.category == "Fellowship"
    assert str(opportunity.application_url) == "https://example.test/apply-1"
    assert opportunity.deadline.date().isoformat() == "2026-09-30"
    assert opportunity.opening_date.isoformat() == "2026-08-01"
    assert opportunity.program_start_date.isoformat() == "2026-10-20"
    assert opportunity.program_end_date.isoformat() == "2026-10-25"
    assert opportunity.country == "France" and opportunity.city == "Paris"
    assert opportunity.eligibility.raw_text
    assert opportunity.eligibility.requirements_complete is True
    assert opportunity.eligibility.minimum_age == 18
    assert opportunity.eligibility.maximum_age == 30
    assert opportunity.eligibility.student_required is True
    assert opportunity.eligibility.undergraduate_eligible is True
    assert opportunity.eligibility.nationalities_allowed == ["Nigeria", "Mauritius"]
    assert opportunity.eligibility.minimum_years_experience == 2
    assert "Application Security" in opportunity.eligibility.required_skills
    assert opportunity.funding.flights.status is CoverageStatus.COVERED
    assert opportunity.funding.accommodation.status is CoverageStatus.COVERED
    assert opportunity.funding.visa_support.status is CoverageStatus.COVERED
    assert opportunity.funding.stipend == "USD 2,000"
    assert opportunity.application.application_fee is False
    assert opportunity.application.cv_required is True
    assert opportunity.application.cover_letter_required is True
    required_fields = {item.field for item in opportunity.evidence}
    assert {"deadline", "eligibility", "funding.flights", "funding.accommodation", "funding.visa_support", "funding.stipend", "application.application_fee", "application_url"} <= required_fields
    assert all(item.evidence_text in opportunity.eligibility.raw_text or item.evidence_text in fetched_page(rich_html()).cleaned_text for item in opportunity.evidence)


def test_unknowns_remain_unknown_and_completeness_can_be_false() -> None:
    html = """
    <html><body><h1>Community Programme</h1>
    <h2>Eligibility</h2><p>Some criteria apply. See full eligibility criteria on the application portal.</p>
    <p>University partners support the programme. The event is fully funded.</p></body></html>
    """
    opportunity = DeterministicOpportunityExtractor().extract(fetched_page(html, "https://example.test/unknown"))
    assert opportunity.category == "Unknown"
    assert opportunity.deadline is None
    assert opportunity.eligibility.student_required is None
    assert opportunity.eligibility.requirements_complete is False
    assert opportunity.funding.flights.status is CoverageStatus.UNKNOWN
    assert opportunity.funding.accommodation.status is CoverageStatus.UNKNOWN
    assert opportunity.funding.visa_support.status is CoverageStatus.UNKNOWN


def test_deterministic_relevance_match_and_profile_backed_explanation(profile) -> None:
    opportunity = DeterministicOpportunityExtractor().extract(fetched_page(rich_html()))
    judgment = DeterministicSemanticAssessor().assess(opportunity, profile)
    assert judgment.match_mode is MatchMode.MATCH
    assert "application security" in judgment.relevance_reason.casefold()
    assert "profile evidence" in judgment.why_you.casefold()
    assert "Klas" in judgment.why_you or "professional_identity" in judgment.why_you


def test_deterministic_discovery(profile) -> None:
    html = """
    <html><body><h1>Quantum Computing Exploration Programme</h1>
    <p>Emerging technology learning and mentorship for curious builders.</p>
    <h2>Eligibility</h2><p>Applicants must be a current student.</p>
    <a href="/apply">Apply now</a></body></html>
    """
    opportunity = DeterministicOpportunityExtractor().extract(fetched_page(html, "https://example.test/quantum"))
    assessment = evaluate_opportunity(
        opportunity,
        profile,
        DeterministicSemanticAssessor(),
        as_of=date(2026, 8, 28),
    )
    assert assessment.match_mode is MatchMode.DISCOVERY
    assert assessment.why_it_matters.startswith("Discovery value")


def test_manual_overrides_are_optional_explicit_and_traceable(tmp_path) -> None:
    missing = load_overrides(tmp_path / "missing.yaml")
    assert not missing.overrides
    opportunity = DeterministicOpportunityExtractor().extract(fetched_page(rich_html()))
    configuration = OverrideConfiguration(
        overrides={
            "https://example.test/programme-1": {
                "category": "Conference",
                "eligibility": {"minimum_age": 22},
                "funding": {"flights": {"status": "not_covered"}},
            }
        }
    )
    updated = OpportunityOverrideApplier(configuration, source_file="config/test.yaml").apply(opportunity)
    assert updated.category == "Conference"
    assert updated.eligibility.minimum_age == 22
    assert updated.funding.flights.status is CoverageStatus.NOT_COVERED
    assert {record.field for record in updated.manual_overrides} == {
        "category", "eligibility.minimum_age", "funding.flights.status"
    }
    assert updated.evidence == opportunity.evidence


def test_provider_mode_selection_defaults_deterministic(monkeypatch) -> None:
    monkeypatch.delenv("OPPORTUNITY_RADAR_MODE", raising=False)
    assert provider_mode_from_environment() is ProviderMode.DETERMINISTIC
    monkeypatch.setenv("OPPORTUNITY_RADAR_MODE", "openai")
    assert provider_mode_from_environment() is ProviderMode.OPENAI
    assert build_provider_bundle(ProviderMode.DETERMINISTIC).extractor.__class__.__name__ == "DeterministicOpportunityExtractor"


def test_full_mocked_ten_url_pipeline_has_zero_llm_calls(tmp_path, profile) -> None:
    urls = [f"https://example.test/programme-{index}" for index in range(10)]

    def handler(request: httpx.Request) -> httpx.Response:
        index = int(request.url.path.rsplit("-", 1)[1])
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html"},
            text=rich_html(index),
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        providers = build_provider_bundle(ProviderMode.DETERMINISTIC, http_client=client)
        result = run_pipeline(
            urls,
            profile,
            providers.fetcher,
            providers.extractor,
            providers.assessor,
            as_of=date(2026, 8, 28),
        )
    assert result.input_count == 10
    assert result.fetched_count == 10
    assert len(result.opportunities) == 10
    assert len(result.assessments) == 10
    assert not result.failures
    write_outputs(result, tmp_path)
    assert len(json.loads((tmp_path / "opportunities.json").read_text())) == 10
    assert len(json.loads((tmp_path / "assessments.json").read_text())) == 10
    assert json.loads((tmp_path / "failures.json").read_text()) == []
    assert "# Opportunity Radar Digest" in (tmp_path / "digest.md").read_text()
