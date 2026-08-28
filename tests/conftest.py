from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from opportunity_radar.models import (
    ConfidenceLevel,
    EligibilityRequirements,
    Opportunity,
    OpportunityProfile,
    OpportunityStatus,
    SourceEvidence,
)
from opportunity_radar.profile import load_profile
from opportunity_radar.providers import SemanticJudgment
from opportunity_radar.scoring import (
    FeasibilityLevel,
    FrictionLevel,
    InformationConfidenceLevel,
    RelevanceLevel,
    TimingLevel,
    ValueLevel,
)
from opportunity_radar.models import MatchMode


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def profile() -> OpportunityProfile:
    return load_profile(ROOT / "config" / "profile.yaml")


def make_opportunity(
    *,
    title: str = "Security Fellowship 2026",
    url: str = "https://example.com/security-fellowship",
    organization: str | None = "Example Foundation",
    status: OpportunityStatus = OpportunityStatus.OPEN,
    eligibility: EligibilityRequirements | None = None,
) -> Opportunity:
    evidence = []
    if eligibility and any(
        (
            eligibility.nationalities_allowed,
            eligibility.nationalities_excluded,
            eligibility.regions_allowed,
            eligibility.residence_requirements,
            eligibility.minimum_age is not None,
            eligibility.maximum_age is not None,
            eligibility.student_required is not None,
            eligibility.undergraduate_eligible is not None,
            eligibility.graduation_years,
            eligibility.minimum_university_year is not None,
            eligibility.minimum_years_experience is not None,
            eligibility.geographic_restrictions,
        )
    ):
        evidence.append(
            SourceEvidence(
                field="eligibility",
                value="structured eligibility",
                source_url=url,
                confidence=ConfidenceLevel.HIGH,
                evidence_text="Applicants must meet the listed requirements.",
            )
        )
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    return Opportunity(
        title=title,
        organization=organization,
        category="Fellowship",
        source_url=url,
        status=status,
        eligibility=eligibility or EligibilityRequirements(),
        evidence=evidence,
        discovered_at=now,
        last_verified_at=now,
    )


def make_judgment(
    *,
    relevance: RelevanceLevel = RelevanceLevel.STRONG_DIRECT_FIT,
    mode: MatchMode = MatchMode.MATCH,
    discovery_reason: str | None = None,
) -> SemanticJudgment:
    return SemanticJudgment(
        relevance_level=relevance,
        relevance_reason="Profile has professional application-security experience.",
        value_level=ValueLevel.STRONG,
        value_reason="Provides meaningful learning and network value.",
        feasibility_level=FeasibilityLevel.MOSTLY_FEASIBLE,
        feasibility_reason="Participation requirements appear manageable.",
        timing_level=TimingLevel.HEALTHY_WINDOW,
        timing_reason="Applications are open with time to prepare.",
        friction_level=FrictionLevel.LOW,
        friction_reason="Only standard materials are required.",
        confidence_level=InformationConfidenceLevel.HIGH,
        confidence_reason="Material facts are confirmed.",
        match_mode=mode,
        why_you="The profile records Application Security Engineer roles at Klas and MerkleFence.",
        why_it_matters="It provides relevant technical and career growth.",
        discovery_reason=discovery_reason,
    )
