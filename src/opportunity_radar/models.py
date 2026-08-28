from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator


class OpportunityStatus(StrEnum):
    EXPECTED = "expected"
    OPENING_SOON = "opening_soon"
    OPEN = "open"
    CLOSING_SOON = "closing_soon"
    CLOSED = "closed"
    FUTURE_CYCLE = "future_cycle"
    UNKNOWN = "unknown"


class ParticipationMode(StrEnum):
    REMOTE = "remote"
    IN_PERSON = "in_person"
    HYBRID = "hybrid"
    UNKNOWN = "unknown"


class CoverageStatus(StrEnum):
    COVERED = "covered"
    PARTIALLY_COVERED = "partially_covered"
    REIMBURSED = "reimbursed"
    NOT_COVERED = "not_covered"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class ConfidenceLevel(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class FundingComponent(BaseModel):
    status: CoverageStatus = CoverageStatus.UNKNOWN
    amount: str | None = None
    notes: str | None = None


class FundingDetails(BaseModel):
    paid: bool | None = None
    salary: str | None = None
    stipend: str | None = None
    grant: str | None = None
    prize_money: str | None = None
    honorarium: str | None = None
    flights: FundingComponent = Field(default_factory=FundingComponent)
    accommodation: FundingComponent = Field(default_factory=FundingComponent)
    visa_support: FundingComponent = Field(default_factory=FundingComponent)
    visa_fees: FundingComponent = Field(default_factory=FundingComponent)
    meals: FundingComponent = Field(default_factory=FundingComponent)
    local_transport: FundingComponent = Field(default_factory=FundingComponent)
    registration: FundingComponent = Field(default_factory=FundingComponent)
    cloud_credits: str | None = None
    developer_credits: str | None = None
    other_benefits: list[str] = Field(default_factory=list)


class EligibilityRequirements(BaseModel):
    raw_text: str | None = None
    requirements_complete: bool | None = None
    nationalities_allowed: list[str] = Field(default_factory=list)
    nationalities_excluded: list[str] = Field(default_factory=list)
    regions_allowed: list[str] = Field(default_factory=list)
    residence_requirements: list[str] = Field(default_factory=list)
    minimum_age: int | None = None
    minimum_age_exclusive: bool = False
    maximum_age: int | None = None
    maximum_age_exclusive: bool = False
    student_required: bool | None = None
    undergraduate_eligible: bool | None = None
    graduate_eligible: bool | None = None
    graduation_years: list[int] = Field(default_factory=list)
    minimum_university_year: int | None = None
    required_fields_of_study: list[str] = Field(default_factory=list)
    minimum_years_experience: float | None = None
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    gender_requirements: list[str] = Field(default_factory=list)
    language_requirements: list[str] = Field(default_factory=list)
    founder_required: bool | None = None
    startup_stage_requirements: list[str] = Field(default_factory=list)
    geographic_restrictions: list[str] = Field(default_factory=list)
    other_requirements: list[str] = Field(default_factory=list)


class ApplicationRequirements(BaseModel):
    application_fee: bool | None = None
    fee_amount: str | None = None
    fee_waiver_available: bool | None = None
    cv_required: bool | None = None
    cover_letter_required: bool | None = None
    transcript_required: bool | None = None
    essays: list[str] = Field(default_factory=list)
    recommendation_letters: int | None = None
    portfolio_required: bool | None = None
    github_required: bool | None = None
    video_required: bool | None = None
    technical_assessment: bool | None = None
    coding_challenge: bool | None = None
    proposal_required: bool | None = None
    pitch_deck_required: bool | None = None
    nomination_required: bool | None = None
    other_requirements: list[str] = Field(default_factory=list)


class SourceEvidence(BaseModel):
    field: str
    value: str
    source_url: HttpUrl
    confidence: ConfidenceLevel
    evidence_text: str | None = None


class ManualOverrideRecord(BaseModel):
    field: str
    previous_value: Any = None
    override_value: Any
    source_file: str


class ExtractionDiagnostics(BaseModel):
    material_fields_found: list[str] = Field(default_factory=list)
    material_fields_unknown: list[str] = Field(default_factory=list)
    enrichment_attempted: bool = False
    primary_source_url: HttpUrl | None = None
    enrichment_source_url: HttpUrl | None = None
    conflicts: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class Opportunity(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    title: str
    organization: str | None = None
    category: str = "Unknown"
    subcategories: list[str] = Field(default_factory=list)
    program_family: str | None = None
    cycle_label: str | None = None
    cycle_year: int | None = Field(default=None, ge=2000, le=2200)
    source_url: HttpUrl
    official_url: HttpUrl | None = None
    application_url: HttpUrl | None = None
    status: OpportunityStatus = OpportunityStatus.UNKNOWN
    country: str | None = None
    city: str | None = None
    region: str | None = None
    participation_mode: ParticipationMode = ParticipationMode.UNKNOWN
    geographic_restrictions: list[str] = Field(default_factory=list)
    opening_date: date | None = None
    deadline: datetime | None = None
    deadline_timezone: str | None = None
    program_start_date: date | None = None
    program_end_date: date | None = None
    rolling_application: bool | None = None
    summary: str | None = None
    eligibility: EligibilityRequirements = Field(default_factory=EligibilityRequirements)
    funding: FundingDetails = Field(default_factory=FundingDetails)
    application: ApplicationRequirements = Field(default_factory=ApplicationRequirements)
    evidence: list[SourceEvidence] = Field(default_factory=list)
    discovered_at: datetime
    last_verified_at: datetime
    extraction_confidence: ConfidenceLevel = ConfidenceLevel.MEDIUM
    semantic_input_truncated: bool = False
    semantic_input_character_count: int = Field(default=0, ge=0)
    semantic_input_limit: int | None = Field(default=None, ge=1)
    manual_overrides: list[ManualOverrideRecord] = Field(default_factory=list)
    extraction_diagnostics: ExtractionDiagnostics = Field(default_factory=ExtractionDiagnostics)

    @model_validator(mode="after")
    def material_facts_have_evidence(self) -> Opportunity:
        evidence_fields = {item.field for item in self.evidence}
        override_fields = {item.field for item in self.manual_overrides}

        def require(condition: bool, accepted_fields: set[str], label: str) -> None:
            manually_overridden = any(
                field in override_fields or any(item.startswith(f"{field}.") for item in override_fields)
                for field in accepted_fields
            )
            if condition and not manually_overridden and not evidence_fields.intersection(accepted_fields):
                raise ValueError(f"known {label} requires SourceEvidence")

        hard_eligibility_known = any(
            (
                self.eligibility.nationalities_allowed,
                self.eligibility.nationalities_excluded,
                self.eligibility.regions_allowed,
                self.eligibility.residence_requirements,
                self.eligibility.minimum_age is not None,
                self.eligibility.maximum_age is not None,
                self.eligibility.student_required is not None,
                self.eligibility.undergraduate_eligible is not None,
                self.eligibility.graduation_years,
                self.eligibility.minimum_university_year is not None,
                self.eligibility.minimum_years_experience is not None,
                self.eligibility.founder_required is not None,
                self.eligibility.gender_requirements,
                self.eligibility.language_requirements,
                self.eligibility.geographic_restrictions,
                self.geographic_restrictions,
            )
        )
        require(self.deadline is not None, {"deadline"}, "deadline")
        require(hard_eligibility_known, {"eligibility", "hard_eligibility"}, "hard eligibility")
        for name in ("flights", "accommodation", "visa_support"):
            known = getattr(self.funding, name).status is not CoverageStatus.UNKNOWN
            require(known, {f"funding.{name}", name}, name)
        for name in ("salary", "stipend", "grant", "prize_money"):
            require(
                getattr(self.funding, name) is not None,
                {f"funding.{name}", name, "compensation"},
                name,
            )
        require(
            self.application.application_fee is not None,
            {"application.application_fee", "application_fee"},
            "application fee",
        )
        require(
            self.application_url is not None,
            {"application_url"},
            "application URL",
        )
        return self


class EligibilityStatus(StrEnum):
    ELIGIBLE = "eligible"
    LIKELY_ELIGIBLE = "likely_eligible"
    NEEDS_VERIFICATION = "needs_verification"
    FUTURE_ELIGIBLE = "future_eligible"
    NOT_ELIGIBLE = "not_eligible"


class PriorityBand(StrEnum):
    EXCEPTIONAL = "exceptional"
    STRONG_MATCH = "strong_match"
    WORTH_CHECKING = "worth_checking"
    DISCOVERY = "discovery"
    LOW_PRIORITY = "low_priority"
    NOT_ACTIONABLE = "not_actionable"


class MatchMode(StrEnum):
    MATCH = "match"
    DISCOVERY = "discovery"


class RecommendedAction(StrEnum):
    APPLY_NOW = "apply_now"
    CHECK_NOW = "check_now"
    PREPARE = "prepare"
    TRACK = "track"
    SAVE = "save"
    IGNORE = "ignore"


class DimensionScore(BaseModel):
    score: float
    maximum: float
    reason: str

    @model_validator(mode="after")
    def score_within_maximum(self) -> DimensionScore:
        if self.score < 0 or self.score > self.maximum:
            raise ValueError("dimension score must be between zero and its maximum")
        return self


class OpportunityAssessment(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    opportunity_id: UUID
    eligibility_status: EligibilityStatus
    eligibility_confidence: ConfidenceLevel
    eligibility_reason: str
    hard_blockers: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    supporting_profile_evidence: list[str] = Field(default_factory=list)
    eligibility_score: DimensionScore
    relevance_score: DimensionScore
    value_score: DimensionScore
    feasibility_score: DimensionScore
    timing_score: DimensionScore
    friction_score: DimensionScore
    confidence_score: DimensionScore
    total_score: float = Field(ge=0, le=100)
    priority_band: PriorityBand
    match_mode: MatchMode
    why_you: str
    why_it_matters: str
    top_positive_signals: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    recommended_action: RecommendedAction
    evaluated_at: datetime

    @model_validator(mode="after")
    def total_matches_components(self) -> OpportunityAssessment:
        component_total = sum(
            score.score
            for score in (
                self.eligibility_score,
                self.relevance_score,
                self.value_score,
                self.feasibility_score,
                self.timing_score,
                self.friction_score,
                self.confidence_score,
            )
        )
        if abs(self.total_score - component_total) > 1e-9:
            raise ValueError("total_score must equal the seven component scores")
        return self


class ProcessingStage(StrEnum):
    INPUT = "input"
    FETCH = "fetch"
    EXTRACT = "extract"
    EVALUATE = "evaluate"
    SERIALIZE = "serialize"


class ProcessingFailure(BaseModel):
    url: str
    stage: ProcessingStage
    reason: str


class FlexibleProfileModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class AgeProfile(FlexibleProfileModel):
    current: int = Field(ge=0)
    birthday: dict[str, int] = Field(default_factory=dict)


class IdentityProfile(FlexibleProfileModel):
    nationality: list[str]
    age: AgeProfile
    residence: dict[str, str]
    languages: list[str] = Field(default_factory=list)


class CurrentStage(FlexibleProfileModel):
    year: int
    trimester: int | None = None
    description: str | None = None


class EducationProfile(FlexibleProfileModel):
    university: str
    degree: str
    start_year: int
    graduation_year: int
    current_stage: CurrentStage
    year_2_start_date: date | None = None


class ExperienceEntry(FlexibleProfileModel):
    organization: str
    role: str
    start_date: str
    end_date: str | None = None
    evidence: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)


class ProjectEntry(FlexibleProfileModel):
    name: str
    role: str | None = None
    description: str | None = None
    evidence: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)


class ProfessionalIdentity(FlexibleProfileModel):
    primary: list[str] = Field(default_factory=list)
    secondary: list[str] = Field(default_factory=list)
    adjacent: list[str] = Field(default_factory=list)
    discovery: list[str] = Field(default_factory=list)


class OpportunityProfile(FlexibleProfileModel):
    profile_version: str
    identity: IdentityProfile
    education: EducationProfile
    professional_identity: ProfessionalIdentity
    experience: list[ExperienceEntry] = Field(default_factory=list)
    projects: list[ProjectEntry] = Field(default_factory=list)
