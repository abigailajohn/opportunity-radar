import pytest

from opportunity_radar.models import EligibilityStatus, MatchMode, OpportunityStatus, PriorityBand, RecommendedAction
from opportunity_radar.scoring import (
    CONFIDENCE_SCORES,
    FEASIBILITY_SCORES,
    FRICTION_SCORES,
    RELEVANCE_SCORES,
    TIMING_SCORES,
    VALUE_SCORES,
    FeasibilityLevel,
    FrictionLevel,
    InformationConfidenceLevel,
    RelevanceLevel,
    TimingLevel,
    ValueLevel,
    priority_for_score,
    recommended_action,
    resolve_priority,
    total_score,
)


def test_fixed_level_mappings() -> None:
    assert list(RELEVANCE_SCORES.values()) == [20, 17, 13, 8, 3, 0]
    assert list(VALUE_SCORES.values()) == [25, 21, 17, 12, 6, 0]
    assert list(FEASIBILITY_SCORES.values()) == [15, 12, 9, 5, 2, 0]
    assert list(TIMING_SCORES.values()) == [10, 8, 6, 5, 3, 0]
    assert list(FRICTION_SCORES.values()) == [5, 4, 3, 2, 1, 0]
    assert list(CONFIDENCE_SCORES.values()) == [5, 3, 1]
    assert RELEVANCE_SCORES[RelevanceLevel.EXCEPTIONAL_DIRECT_FIT] == 20
    assert VALUE_SCORES[ValueLevel.EXCEPTIONAL_MULTI_DIMENSIONAL] == 25
    assert FEASIBILITY_SCORES[FeasibilityLevel.IMPOSSIBLE] == 0
    assert TIMING_SCORES[TimingLevel.CLOSED] == 0
    assert FRICTION_SCORES[FrictionLevel.PROHIBITIVE] == 0
    assert CONFIDENCE_SCORES[InformationConfidenceLevel.LOW] == 1


@pytest.mark.parametrize(
    ("score", "expected"),
    [(86, PriorityBand.EXCEPTIONAL), (76, PriorityBand.STRONG_MATCH), (61, PriorityBand.WORTH_CHECKING), (47, PriorityBand.LOW_PRIORITY), (32, PriorityBand.NOT_ACTIONABLE)],
)
def test_priority_contract(score, expected) -> None:
    assert priority_for_score(score) is expected


def test_total_arithmetic_and_bounds() -> None:
    assert total_score([20, 17, 17, 12, 10, 4, 5]) == 85
    with pytest.raises(ValueError):
        total_score([101])


def test_hard_blocker_and_action_overrides() -> None:
    priority = resolve_priority(95, EligibilityStatus.NOT_ELIGIBLE, ["blocked"], MatchMode.MATCH, False)
    assert priority is PriorityBand.NOT_ACTIONABLE
    assert recommended_action(EligibilityStatus.NOT_ELIGIBLE, priority) is RecommendedAction.IGNORE
    assert recommended_action(EligibilityStatus.FUTURE_ELIGIBLE, PriorityBand.DISCOVERY) is RecommendedAction.TRACK


@pytest.mark.parametrize(
    ("priority", "expected"),
    [
        (PriorityBand.EXCEPTIONAL, RecommendedAction.APPLY_NOW),
        (PriorityBand.STRONG_MATCH, RecommendedAction.CHECK_NOW),
        (PriorityBand.WORTH_CHECKING, RecommendedAction.CHECK_NOW),
        (PriorityBand.DISCOVERY, RecommendedAction.SAVE),
        (PriorityBand.LOW_PRIORITY, RecommendedAction.IGNORE),
        (PriorityBand.NOT_ACTIONABLE, RecommendedAction.IGNORE),
    ],
)
def test_recommended_action_defaults(priority, expected) -> None:
    assert recommended_action(EligibilityStatus.ELIGIBLE, priority) is expected


def test_future_availability_action_is_separate_from_eligibility() -> None:
    assert recommended_action(
        EligibilityStatus.ELIGIBLE,
        PriorityBand.STRONG_MATCH,
        OpportunityStatus.OPENING_SOON,
    ) is RecommendedAction.PREPARE
    assert recommended_action(
        EligibilityStatus.ELIGIBLE,
        PriorityBand.STRONG_MATCH,
        OpportunityStatus.FUTURE_CYCLE,
    ) is RecommendedAction.TRACK


def test_discovery_contract() -> None:
    assert resolve_priority(60, EligibilityStatus.ELIGIBLE, [], MatchMode.DISCOVERY, True) is PriorityBand.DISCOVERY
    with pytest.raises(ValueError):
        resolve_priority(39, EligibilityStatus.ELIGIBLE, [], MatchMode.DISCOVERY, True)
