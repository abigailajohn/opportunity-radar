from __future__ import annotations

from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date

from opportunity_radar.models import (
    ConfidenceLevel,
    EligibilityStatus,
    Opportunity,
    OpportunityProfile,
)
from opportunity_radar.geography import location_matches_restrictions


@dataclass(frozen=True)
class EligibilityDecision:
    status: EligibilityStatus
    confidence: ConfidenceLevel
    reason: str
    hard_blockers: list[str] = field(default_factory=list)
    uncertainties: list[str] = field(default_factory=list)
    profile_evidence: list[str] = field(default_factory=list)


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())


def _matches(value: str, choices: list[str]) -> bool:
    normalized = _norm(value)
    return any(normalized == _norm(choice) for choice in choices)


def _month_value(value: str, end: bool = False) -> int:
    parts = value.split("-")
    year, month = int(parts[0]), int(parts[1])
    day = monthrange(year, month)[1] if end else 1
    return date(year, month, day).toordinal()


def years_of_experience(profile: OpportunityProfile, as_of: date) -> float:
    intervals: list[tuple[int, int]] = []
    for entry in profile.experience:
        start = _month_value(entry.start_date)
        end = _month_value(entry.end_date, end=True) if entry.end_date else as_of.toordinal()
        intervals.append((start, min(end, as_of.toordinal())))
    if not intervals:
        return 0.0
    intervals.sort()
    merged: list[list[int]] = []
    for start, end in intervals:
        if not merged or start > merged[-1][1] + 1:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    days = sum(end - start + 1 for start, end in merged)
    return days / 365.2425


def evaluate_eligibility(
    opportunity: Opportunity,
    profile: OpportunityProfile,
    as_of: date,
) -> EligibilityDecision:
    requirements = opportunity.eligibility
    blockers: list[str] = []
    uncertainties: list[str] = []
    evidence: list[str] = []
    checked = 0
    future_changes: list[str] = []

    nationalities = profile.identity.nationality
    if requirements.nationalities_allowed:
        checked += 1
        if not any(_matches(n, requirements.nationalities_allowed) for n in nationalities):
            blockers.append("Profile nationality is not in the allowed nationality list.")
        else:
            evidence.append(f"Nationality: {', '.join(nationalities)}")
    if requirements.nationalities_excluded:
        checked += 1
        excluded = [n for n in nationalities if _matches(n, requirements.nationalities_excluded)]
        if excluded:
            blockers.append(f"Nationality explicitly excluded: {', '.join(excluded)}.")

    residence = profile.identity.residence.get("country")
    if requirements.residence_requirements:
        checked += 1
        if not residence:
            uncertainties.append("Profile residence is unavailable.")
        elif not _matches(residence, requirements.residence_requirements):
            blockers.append(f"Residence requirement does not include {residence}.")
        else:
            evidence.append(f"Residence: {residence}")

    age = profile.identity.age.current
    if requirements.minimum_age is not None:
        checked += 1
        if age < requirements.minimum_age:
            blockers.append(f"Minimum age is {requirements.minimum_age}; profile age is {age}.")
        else:
            evidence.append(f"Profile age {age} meets the minimum age.")
    if requirements.maximum_age is not None:
        checked += 1
        if age > requirements.maximum_age:
            blockers.append(f"Maximum age is {requirements.maximum_age}; profile age is {age}.")
        else:
            evidence.append(f"Profile age {age} meets the maximum age.")

    is_student = profile.education.current_stage.year >= 1
    if requirements.student_required is not None:
        checked += 1
        if requirements.student_required and not is_student:
            blockers.append("Current student status is required.")
        elif requirements.student_required:
            evidence.append(f"Current student at {profile.education.university}.")

    if requirements.undergraduate_eligible is not None:
        checked += 1
        is_undergraduate = "bsc" in _norm(profile.education.degree) or "bachelor" in _norm(profile.education.degree)
        if requirements.undergraduate_eligible is False and is_undergraduate:
            blockers.append("Undergraduate applicants are explicitly ineligible.")
        elif requirements.undergraduate_eligible and not is_undergraduate:
            blockers.append("Opportunity requires undergraduate status.")
        elif requirements.undergraduate_eligible:
            evidence.append(f"Undergraduate degree: {profile.education.degree}.")

    if requirements.graduation_years:
        checked += 1
        graduation_year = profile.education.graduation_year
        if graduation_year not in requirements.graduation_years:
            blockers.append(
                f"Graduation year {graduation_year} is outside the allowed years "
                f"{requirements.graduation_years}."
            )
        else:
            evidence.append(f"Graduation year: {graduation_year}.")

    if requirements.minimum_university_year is not None:
        checked += 1
        current_year = profile.education.current_stage.year
        required_year = requirements.minimum_university_year
        if current_year >= required_year:
            evidence.append(f"University year {current_year} meets the minimum year {required_year}.")
        elif required_year == 2 and profile.education.year_2_start_date is not None:
            future_changes.append(
                f"University year requirement will be met on {profile.education.year_2_start_date.isoformat()}."
            )
        else:
            blockers.append(
                f"Requires university year {required_year} or later; profile is currently year {current_year}."
            )

    if requirements.minimum_years_experience is not None:
        checked += 1
        experience = years_of_experience(profile, as_of)
        if experience < requirements.minimum_years_experience:
            blockers.append(
                f"Requires {requirements.minimum_years_experience:g} years of experience; "
                f"profile has {experience:.1f} non-overlapping years."
            )
        else:
            evidence.append(f"At least {experience:.1f} non-overlapping years of experience.")

    restrictions = opportunity.geographic_restrictions + requirements.geographic_restrictions
    if restrictions:
        checked += 1
        locations = [*nationalities]
        if residence:
            locations.append(residence)
        if not location_matches_restrictions(locations, restrictions):
            blockers.append("Explicit geographic restrictions do not include nationality or residence.")
        else:
            evidence.append("Profile nationality or residence meets the geographic restriction.")

    if blockers:
        return EligibilityDecision(
            EligibilityStatus.NOT_ELIGIBLE,
            ConfidenceLevel.HIGH,
            blockers[0],
            blockers,
            uncertainties,
            evidence,
        )
    if future_changes:
        return EligibilityDecision(
            EligibilityStatus.FUTURE_ELIGIBLE,
            ConfidenceLevel.MEDIUM,
            future_changes[0],
            [],
            [*uncertainties, *future_changes],
            evidence,
        )
    if uncertainties or requirements.other_requirements or checked == 0:
        uncertainties.extend(requirements.other_requirements)
        if checked == 0:
            uncertainties.append("No material eligibility requirements were verified.")
        return EligibilityDecision(
            EligibilityStatus.NEEDS_VERIFICATION,
            ConfidenceLevel.LOW,
            uncertainties[0],
            [],
            uncertainties,
            evidence,
        )
    if requirements.requirements_complete is not True:
        return EligibilityDecision(
            EligibilityStatus.LIKELY_ELIGIBLE,
            ConfidenceLevel.MEDIUM,
            "Known requirements match the profile, but the published eligibility information is not confirmed complete.",
            [],
            [],
            evidence,
        )
    return EligibilityDecision(
        EligibilityStatus.ELIGIBLE,
        ConfidenceLevel.HIGH,
        "The source provides complete explicit eligibility requirements and every applicable hard requirement is satisfied.",
        [],
        [],
        evidence,
    )
