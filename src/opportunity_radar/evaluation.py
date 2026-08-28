from __future__ import annotations

from datetime import date, datetime, timezone

from opportunity_radar.eligibility import evaluate_eligibility
from opportunity_radar.models import DimensionScore, MatchMode, Opportunity, OpportunityAssessment, OpportunityProfile
from opportunity_radar.providers import SemanticAssessor
from opportunity_radar.scoring import (
    CONFIDENCE_SCORES,
    ELIGIBILITY_SCORES,
    FEASIBILITY_SCORES,
    FRICTION_SCORES,
    RELEVANCE_SCORES,
    TIMING_SCORES,
    VALUE_SCORES,
    RelevanceLevel,
    recommended_action,
    resolve_priority,
    total_score,
)


def evaluate_opportunity(
    opportunity: Opportunity,
    profile: OpportunityProfile,
    semantic_assessor: SemanticAssessor,
    *,
    as_of: date,
    evaluated_at: datetime | None = None,
) -> OpportunityAssessment:
    eligibility = evaluate_eligibility(opportunity, profile, as_of)
    judgment = semantic_assessor.assess(opportunity, profile)
    if judgment.match_mode is MatchMode.DISCOVERY and judgment.relevance_level in {
        RelevanceLevel.EXCEPTIONAL_DIRECT_FIT,
        RelevanceLevel.STRONG_DIRECT_FIT,
    }:
        raise ValueError("Discovery cannot claim exceptional or strong direct relevance")

    values = [
        ELIGIBILITY_SCORES[eligibility.status],
        RELEVANCE_SCORES[judgment.relevance_level],
        VALUE_SCORES[judgment.value_level],
        FEASIBILITY_SCORES[judgment.feasibility_level],
        TIMING_SCORES[judgment.timing_level],
        FRICTION_SCORES[judgment.friction_level],
        CONFIDENCE_SCORES[judgment.confidence_level],
    ]
    total = total_score(values)
    priority = resolve_priority(
        total,
        eligibility.status,
        eligibility.hard_blockers,
        judgment.match_mode,
        bool(judgment.discovery_reason),
    )
    concerns = [*judgment.concerns, *eligibility.uncertainties]
    return OpportunityAssessment(
        opportunity_id=opportunity.id,
        eligibility_status=eligibility.status,
        eligibility_confidence=eligibility.confidence,
        eligibility_reason=eligibility.reason,
        hard_blockers=eligibility.hard_blockers,
        uncertainties=eligibility.uncertainties,
        supporting_profile_evidence=eligibility.profile_evidence,
        eligibility_score=DimensionScore(score=values[0], maximum=20, reason=eligibility.reason),
        relevance_score=DimensionScore(score=values[1], maximum=20, reason=judgment.relevance_reason),
        value_score=DimensionScore(score=values[2], maximum=25, reason=judgment.value_reason),
        feasibility_score=DimensionScore(score=values[3], maximum=15, reason=judgment.feasibility_reason),
        timing_score=DimensionScore(score=values[4], maximum=10, reason=judgment.timing_reason),
        friction_score=DimensionScore(score=values[5], maximum=5, reason=judgment.friction_reason),
        confidence_score=DimensionScore(score=values[6], maximum=5, reason=judgment.confidence_reason),
        total_score=total,
        priority_band=priority,
        match_mode=judgment.match_mode,
        why_you=judgment.why_you,
        why_it_matters=judgment.discovery_reason or judgment.why_it_matters,
        top_positive_signals=list(judgment.top_positive_signals),
        concerns=concerns,
        recommended_action=recommended_action(eligibility.status, priority, opportunity.status),
        evaluated_at=evaluated_at or datetime.now(timezone.utc),
    )
