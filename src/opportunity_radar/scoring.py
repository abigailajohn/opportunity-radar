from __future__ import annotations

from enum import StrEnum

from opportunity_radar.models import (
    EligibilityStatus,
    MatchMode,
    OpportunityStatus,
    PriorityBand,
    RecommendedAction,
)


class RelevanceLevel(StrEnum):
    EXCEPTIONAL_DIRECT_FIT = "exceptional_direct_fit"
    STRONG_DIRECT_FIT = "strong_direct_fit"
    MODERATE_ADJACENT_FIT = "moderate_adjacent_fit"
    CREDIBLE_DISCOVERY = "credible_discovery"
    LITTLE_MEANINGFUL_FIT = "little_meaningful_fit"
    NONE = "none"


class ValueLevel(StrEnum):
    EXCEPTIONAL_MULTI_DIMENSIONAL = "exceptional_multi_dimensional"
    VERY_HIGH = "very_high"
    STRONG = "strong"
    MODERATE = "moderate"
    LIMITED = "limited"
    MINIMAL = "minimal"


class FeasibilityLevel(StrEnum):
    VERY_FEASIBLE = "very_feasible"
    MOSTLY_FEASIBLE = "mostly_feasible"
    FEASIBLE_WITH_UNKNOWNS = "feasible_with_unknowns"
    SIGNIFICANT_BARRIERS = "significant_practical_barriers"
    VERY_DIFFICULT = "very_difficult"
    IMPOSSIBLE = "impossible"


class TimingLevel(StrEnum):
    HEALTHY_WINDOW = "open_healthy_window"
    MODERATELY_URGENT = "open_moderately_urgent"
    CLOSING_SOON = "closing_soon"
    FUTURE_PREPARATION = "future_preparation_window"
    UNCLEAR = "date_status_unclear"
    CLOSED = "closed_expired"


class FrictionLevel(StrEnum):
    VERY_LOW = "very_low"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    VERY_HIGH = "very_high"
    PROHIBITIVE = "prohibitive_relative_to_value"


class InformationConfidenceLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


ELIGIBILITY_SCORES = {
    EligibilityStatus.ELIGIBLE: 20,
    EligibilityStatus.LIKELY_ELIGIBLE: 17,
    EligibilityStatus.NEEDS_VERIFICATION: 12,
    EligibilityStatus.FUTURE_ELIGIBLE: 8,
    EligibilityStatus.NOT_ELIGIBLE: 0,
}
RELEVANCE_SCORES = dict(zip(RelevanceLevel, (20, 17, 13, 8, 3, 0), strict=True))
VALUE_SCORES = dict(zip(ValueLevel, (25, 21, 17, 12, 6, 0), strict=True))
FEASIBILITY_SCORES = dict(zip(FeasibilityLevel, (15, 12, 9, 5, 2, 0), strict=True))
TIMING_SCORES = dict(zip(TimingLevel, (10, 8, 6, 5, 3, 0), strict=True))
FRICTION_SCORES = dict(zip(FrictionLevel, (5, 4, 3, 2, 1, 0), strict=True))
CONFIDENCE_SCORES = dict(zip(InformationConfidenceLevel, (5, 3, 1), strict=True))


def total_score(component_scores: list[float]) -> float:
    total = sum(component_scores)
    if not 0 <= total <= 100:
        raise ValueError("total score must be between 0 and 100")
    return total


def priority_for_score(score: float) -> PriorityBand:
    if not 0 <= score <= 100:
        raise ValueError("score must be between 0 and 100")
    if score >= 85:
        return PriorityBand.EXCEPTIONAL
    if score >= 70:
        return PriorityBand.STRONG_MATCH
    if score >= 55:
        return PriorityBand.WORTH_CHECKING
    if score >= 40:
        return PriorityBand.LOW_PRIORITY
    return PriorityBand.NOT_ACTIONABLE


def resolve_priority(
    score: float,
    eligibility: EligibilityStatus,
    hard_blockers: list[str],
    match_mode: MatchMode,
    has_discovery_reason: bool,
) -> PriorityBand:
    if hard_blockers or eligibility is EligibilityStatus.NOT_ELIGIBLE:
        return PriorityBand.NOT_ACTIONABLE
    if match_mode is MatchMode.DISCOVERY:
        actionable = eligibility in {
            EligibilityStatus.ELIGIBLE,
            EligibilityStatus.LIKELY_ELIGIBLE,
            EligibilityStatus.NEEDS_VERIFICATION,
            EligibilityStatus.FUTURE_ELIGIBLE,
        }
        if score >= 40 and actionable and has_discovery_reason:
            return PriorityBand.DISCOVERY
        raise ValueError("assessment does not satisfy the Discovery rule")
    return priority_for_score(score)


def recommended_action(
    eligibility: EligibilityStatus,
    priority: PriorityBand,
    opportunity_status: OpportunityStatus = OpportunityStatus.OPEN,
) -> RecommendedAction:
    if eligibility is EligibilityStatus.NOT_ELIGIBLE:
        return RecommendedAction.IGNORE
    if eligibility is EligibilityStatus.FUTURE_ELIGIBLE:
        return RecommendedAction.TRACK
    if opportunity_status is OpportunityStatus.FUTURE_CYCLE:
        return RecommendedAction.TRACK
    if opportunity_status is OpportunityStatus.OPENING_SOON and priority in {
        PriorityBand.EXCEPTIONAL,
        PriorityBand.STRONG_MATCH,
        PriorityBand.WORTH_CHECKING,
    }:
        return RecommendedAction.PREPARE
    if priority is PriorityBand.EXCEPTIONAL:
        return RecommendedAction.APPLY_NOW
    if priority in {PriorityBand.STRONG_MATCH, PriorityBand.WORTH_CHECKING}:
        return RecommendedAction.CHECK_NOW
    if priority is PriorityBand.DISCOVERY:
        return RecommendedAction.SAVE
    if priority is PriorityBand.LOW_PRIORITY:
        return RecommendedAction.IGNORE
    return RecommendedAction.IGNORE
