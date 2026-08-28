from __future__ import annotations

from datetime import date, datetime, timezone

from conftest import make_opportunity
from opportunity_radar.deterministic_assessment import DeterministicSemanticAssessor
from opportunity_radar.deterministic_extraction import DeterministicOpportunityExtractor
from opportunity_radar.digest import render_digest
from opportunity_radar.eligibility import evaluate_eligibility
from opportunity_radar.evaluation import evaluate_opportunity
from opportunity_radar.fetching import FetchedPage
from opportunity_radar.html_preparation import prepare_html
from opportunity_radar.models import EligibilityRequirements, EligibilityStatus, FundingDetails, OpportunityStatus, PriorityBand
from opportunity_radar.normalization import derive_status
from opportunity_radar.scoring import ValueLevel


NOW = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)


def fetched(html: str, url: str = "https://example.test/program") -> FetchedPage:
    return FetchedPage(requested_url=url, final_url=url, raw_html=html, cleaned_text=prepare_html(html, base_url=url), fetched_at=NOW)


def test_african_region_accepts_nigerian_nationality(profile) -> None:
    requirements = EligibilityRequirements(regions_allowed=["Africa"], requirements_complete=True)
    opportunity = make_opportunity(eligibility=requirements)
    identity = profile.identity.model_copy(update={"nationality": ["Nigerian"]})
    decision = evaluate_eligibility(opportunity, profile.model_copy(update={"identity": identity}), date(2026, 8, 28))
    assert decision.status is EligibilityStatus.ELIGIBLE
    assert not decision.hard_blockers


def judgment_for_benefits(profile, benefits: list[str]):
    opportunity = make_opportunity().model_copy(update={"funding": FundingDetails(other_benefits=benefits)})
    return DeterministicSemanticAssessor().assess(opportunity, profile)


def test_other_benefits_drive_coarse_value_levels(profile) -> None:
    funded_training = judgment_for_benefits(profile, ["Fully funded training", "Certification included", "Training included"])
    mentorship = judgment_for_benefits(profile, ["Mentorship"])
    paid = judgment_for_benefits(profile, ["Paid placement"])
    assert funded_training.value_level is not ValueLevel.MINIMAL
    assert mentorship.value_level is ValueLevel.LIMITED
    assert paid.value_level is ValueLevel.STRONG


def test_labelled_application_window_derives_dates_and_closed_status() -> None:
    html = """<html><body><h1>Innovation Challenge 2026</h1><h2>Timeline</h2><p>Applications: May 1 - May 31, 2026</p><h2>Eligibility</h2><p>Open to university students.</p></body></html>"""
    opportunity = DeterministicOpportunityExtractor().extract(fetched(html))
    assert opportunity.opening_date == date(2026, 5, 1)
    assert opportunity.deadline and opportunity.deadline.date() == date(2026, 5, 31)
    assert opportunity.status is OpportunityStatus.CLOSED


def test_rolling_application_is_open_without_unknown_deadline_concern(profile) -> None:
    html = """<html><body><h1>Z Fellows Program</h1><p>Applications are rolling and cohorts run throughout the year.</p><a href='/apply'>Apply now</a></body></html>"""
    opportunity = DeterministicOpportunityExtractor().extract(fetched(html, "https://example.test/z-fellows"))
    assert opportunity.rolling_application is True
    assert opportunity.deadline is None
    assert opportunity.status is OpportunityStatus.OPEN
    assessment = evaluate_opportunity(opportunity, profile, DeterministicSemanticAssessor(), as_of=date(2026, 8, 28))
    assert "Deadline is unknown." not in assessment.concerns
    selected = assessment.model_copy(update={"priority_band": PriorityBand.WORTH_CHECKING})
    assert "Deadline: Rolling" in render_digest([opportunity], [selected]).markdown


def test_explicit_date_lifecycle_derivation() -> None:
    before = datetime(2026, 4, 1, tzinfo=timezone.utc)
    opening = date(2026, 5, 1)
    deadline = datetime(2026, 5, 31, 23, 59, tzinfo=timezone.utc)
    assert derive_status(deadline=deadline, opening_date=opening, as_of=before) is OpportunityStatus.OPENING_SOON
    assert derive_status(deadline=deadline, opening_date=opening, as_of=datetime(2026, 5, 10, tzinfo=timezone.utc)) is OpportunityStatus.OPEN
    assert derive_status(deadline=deadline, opening_date=opening, as_of=datetime(2026, 5, 27, tzinfo=timezone.utc)) is OpportunityStatus.CLOSING_SOON
    assert derive_status(deadline=deadline, opening_date=opening, as_of=datetime(2026, 6, 1, tzinfo=timezone.utc)) is OpportunityStatus.CLOSED
    assert derive_status(deadline=None, opening_date=None, as_of=before) is OpportunityStatus.UNKNOWN
