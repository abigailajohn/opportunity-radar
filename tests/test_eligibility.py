from datetime import date

import pytest

from opportunity_radar.eligibility import evaluate_eligibility, years_of_experience
from opportunity_radar.models import EligibilityRequirements, EligibilityStatus, OpportunityStatus
from conftest import make_opportunity


def test_all_five_eligibility_states(profile) -> None:
    eligible = make_opportunity(
        eligibility=EligibilityRequirements(
            requirements_complete=True,
            student_required=True,
            undergraduate_eligible=True,
        )
    )
    likely = make_opportunity(eligibility=EligibilityRequirements(nationalities_allowed=["Nigerian"]))
    unknown = make_opportunity()
    future = make_opportunity(
        eligibility=EligibilityRequirements(
            requirements_complete=True,
            minimum_university_year=2,
        ),
    )
    blocked = make_opportunity(eligibility=EligibilityRequirements(maximum_age=20))
    as_of = date(2026, 8, 27)
    assert evaluate_eligibility(eligible, profile, as_of).status is EligibilityStatus.ELIGIBLE
    assert evaluate_eligibility(likely, profile, as_of).status is EligibilityStatus.LIKELY_ELIGIBLE
    assert evaluate_eligibility(unknown, profile, as_of).status is EligibilityStatus.NEEDS_VERIFICATION
    assert evaluate_eligibility(future, profile, as_of).status is EligibilityStatus.FUTURE_ELIGIBLE
    assert evaluate_eligibility(blocked, profile, as_of).status is EligibilityStatus.NOT_ELIGIBLE


def test_future_cycle_status_does_not_change_personal_eligibility(profile) -> None:
    opportunity = make_opportunity(
        status=OpportunityStatus.FUTURE_CYCLE,
        eligibility=EligibilityRequirements(
            requirements_complete=True,
            student_required=True,
            undergraduate_eligible=True,
        ),
    )
    assert evaluate_eligibility(opportunity, profile, date(2026, 8, 27)).status is EligibilityStatus.ELIGIBLE


def test_matching_constraints_do_not_imply_eligible_when_requirements_are_incomplete(profile) -> None:
    opportunity = make_opportunity(
        eligibility=EligibilityRequirements(student_required=True, undergraduate_eligible=True)
    )
    assert evaluate_eligibility(opportunity, profile, date(2026, 8, 27)).status is EligibilityStatus.LIKELY_ELIGIBLE


@pytest.mark.parametrize(
    "requirements",
    [
        EligibilityRequirements(nationalities_allowed=["Canadian"]),
        EligibilityRequirements(nationalities_excluded=["Nigerian"]),
        EligibilityRequirements(residence_requirements=["Kenya"]),
        EligibilityRequirements(minimum_age=22),
        EligibilityRequirements(student_required=False, undergraduate_eligible=False),
        EligibilityRequirements(graduation_years=[2027]),
        EligibilityRequirements(minimum_years_experience=10),
        EligibilityRequirements(geographic_restrictions=["United States"]),
    ],
)
def test_requested_hard_rules_detect_blockers(profile, requirements) -> None:
    decision = evaluate_eligibility(make_opportunity(eligibility=requirements), profile, date(2026, 8, 27))
    assert decision.status is EligibilityStatus.NOT_ELIGIBLE
    assert decision.hard_blockers


def test_age_current_is_authoritative_and_experience_does_not_double_count(profile) -> None:
    assert evaluate_eligibility(
        make_opportunity(eligibility=EligibilityRequirements(maximum_age=21)),
        profile,
        date(2026, 8, 27),
    ).status is EligibilityStatus.LIKELY_ELIGIBLE
    assert 2.6 < years_of_experience(profile, date(2026, 8, 27)) < 2.8
