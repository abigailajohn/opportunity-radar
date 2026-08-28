from datetime import date
from uuid import uuid4

import pytest

from opportunity_radar.digest import render_digest
from opportunity_radar.evaluation import evaluate_opportunity
from opportunity_radar.models import EligibilityRequirements, MatchMode, PriorityBand, RecommendedAction
from opportunity_radar.providers import FakeSemanticAssessor
from opportunity_radar.scoring import RelevanceLevel
from conftest import make_judgment, make_opportunity


def _evaluate(opportunity, profile, judgment):
    assessor = FakeSemanticAssessor({str(opportunity.source_url): judgment})
    return evaluate_opportunity(opportunity, profile, assessor, as_of=date(2026, 8, 27))


def test_profile_backed_assessment_and_score_arithmetic(profile) -> None:
    opportunity = make_opportunity(eligibility=EligibilityRequirements(student_required=True, undergraduate_eligible=True))
    assessment = _evaluate(opportunity, profile, make_judgment())
    assert "Klas" in assessment.why_you and "MerkleFence" in assessment.why_you
    scores = [
        assessment.eligibility_score.score, assessment.relevance_score.score,
        assessment.value_score.score, assessment.feasibility_score.score,
        assessment.timing_score.score, assessment.friction_score.score,
        assessment.confidence_score.score,
    ]
    assert assessment.total_score == sum(scores)


def test_high_relevance_hard_blocker_is_not_actionable(profile) -> None:
    opportunity = make_opportunity(eligibility=EligibilityRequirements(maximum_age=20))
    assessment = _evaluate(
        opportunity,
        profile,
        make_judgment(relevance=RelevanceLevel.EXCEPTIONAL_DIRECT_FIT),
    )
    assert assessment.priority_band is PriorityBand.NOT_ACTIONABLE
    assert assessment.recommended_action is RecommendedAction.IGNORE


def test_discovery_is_explicit_and_supported(profile) -> None:
    opportunity = make_opportunity(eligibility=EligibilityRequirements(student_required=True))
    assessment = _evaluate(
        opportunity,
        profile,
        make_judgment(
            relevance=RelevanceLevel.CREDIBLE_DISCOVERY,
            mode=MatchMode.DISCOVERY,
            discovery_reason="Emerging technology exposure matches the profile's Discovery preference.",
        ),
    )
    assert assessment.priority_band is PriorityBand.DISCOVERY
    assert "Emerging technology" in assessment.why_it_matters


def test_discovery_cannot_claim_strong_direct_fit(profile) -> None:
    opportunity = make_opportunity(eligibility=EligibilityRequirements(student_required=True))
    with pytest.raises(ValueError, match="Discovery"):
        _evaluate(
            opportunity,
            profile,
            make_judgment(
                mode=MatchMode.DISCOVERY,
                discovery_reason="Strategic exposure.",
            ),
        )


def test_digest_sections_cards_and_not_selected_appendix(profile) -> None:
    opportunities = []
    assessments = []
    for index in range(6):
        opportunity = make_opportunity(
            title=f"Programme {index}",
            url=f"https://example.com/programme-{index}",
            eligibility=EligibilityRequirements(student_required=True, undergraduate_eligible=True),
        )
        judgment = make_judgment()
        assessment = _evaluate(opportunity, profile, judgment)
        opportunities.append(opportunity)
        assessments.append(assessment)
    blocked = make_opportunity(
        title="Blocked Programme",
        url="https://example.com/blocked",
        eligibility=EligibilityRequirements(maximum_age=20),
    )
    opportunities.append(blocked)
    assessments.append(_evaluate(blocked, profile, make_judgment()))
    digest = render_digest(opportunities, assessments).markdown
    assert "## Strong Matches" in digest
    assert "## Not selected" in digest
    assert "Blocked Programme — Not Actionable" in digest
    assert "Organization:" in digest and "Recommended action:" in digest and "Link:" in digest
    main = digest.split("## Not selected", maxsplit=1)[0]
    assert "Blocked Programme" not in main


def test_tied_digest_order_is_stable_when_ids_change(profile) -> None:
    alpha = make_opportunity(
        title="Alpha Programme",
        organization="Beta Organization",
        url="https://example.com/alpha",
        eligibility=EligibilityRequirements(requirements_complete=True, student_required=True),
    )
    beta = make_opportunity(
        title="Beta Programme",
        organization="Alpha Organization",
        url="https://example.com/beta",
        eligibility=EligibilityRequirements(requirements_complete=True, student_required=True),
    )
    first_assessments = [_evaluate(alpha, profile, make_judgment()), _evaluate(beta, profile, make_judgment())]
    first = render_digest([alpha, beta], first_assessments).markdown

    alpha_copy = alpha.model_copy(update={"id": uuid4()})
    beta_copy = beta.model_copy(update={"id": uuid4()})
    second_assessments = [_evaluate(beta_copy, profile, make_judgment()), _evaluate(alpha_copy, profile, make_judgment())]
    second = render_digest([beta_copy, alpha_copy], second_assessments).markdown
    assert first == second
