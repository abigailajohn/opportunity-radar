from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from opportunity_radar.deduplication import canonical_url
from opportunity_radar.models import ManualOverrideRecord, Opportunity, OpportunityStatus
from opportunity_radar.normalization import derive_status


ALLOWED_FIELDS: dict[str, set[str] | None] = {
    "title": None, "organization": None, "category": None, "subcategories": None,
    "official_url": None, "application_url": None, "country": None, "city": None,
    "region": None, "participation_mode": None, "geographic_restrictions": None,
    "opening_date": None, "deadline": None, "deadline_timezone": None,
    "program_start_date": None, "program_end_date": None, "rolling_application": None,
    "summary": None,
    "eligibility": {
        "raw_text", "requirements_complete", "nationalities_allowed", "nationalities_excluded",
        "residence_requirements", "minimum_age", "maximum_age", "student_required",
        "undergraduate_eligible", "graduate_eligible", "graduation_years",
        "minimum_university_year", "required_fields_of_study", "minimum_years_experience",
        "required_skills", "preferred_skills", "gender_requirements", "founder_required",
        "startup_stage_requirements", "geographic_restrictions", "other_requirements",
    },
    "funding": {
        "paid", "salary", "stipend", "grant", "prize_money", "honorarium", "flights",
        "accommodation", "visa_support", "visa_fees", "meals", "local_transport",
        "registration", "cloud_credits", "developer_credits", "other_benefits",
    },
    "application": {
        "application_fee", "fee_amount", "fee_waiver_available", "cv_required",
        "cover_letter_required", "transcript_required", "essays", "recommendation_letters",
        "portfolio_required", "github_required", "video_required", "technical_assessment",
        "coding_challenge", "proposal_required", "pitch_deck_required", "nomination_required",
        "other_requirements",
    },
}


class OverrideConfiguration(BaseModel):
    overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)


def load_overrides(path: str | Path) -> OverrideConfiguration:
    source = Path(path)
    if not source.exists():
        return OverrideConfiguration()
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    return OverrideConfiguration.model_validate(data or {})


def _validate_fields(values: dict[str, Any]) -> None:
    for field, value in values.items():
        if field not in ALLOWED_FIELDS:
            raise ValueError(f"unsupported opportunity override field: {field}")
        nested = ALLOWED_FIELDS[field]
        if nested is not None:
            if not isinstance(value, dict):
                raise ValueError(f"override field {field} must be a mapping")
            unknown = set(value) - nested
            if unknown:
                raise ValueError(f"unsupported {field} override fields: {sorted(unknown)}")


def _merge(base: dict[str, Any], updates: dict[str, Any], prefix: str = "") -> tuple[dict[str, Any], list[tuple[str, Any, Any]]]:
    merged = dict(base)
    changes: list[tuple[str, Any, Any]] = []
    for key, value in updates.items():
        path = f"{prefix}.{key}" if prefix else key
        previous = merged.get(key)
        if isinstance(value, dict) and isinstance(previous, dict):
            merged[key], nested = _merge(previous, value, path)
            changes.extend(nested)
        else:
            merged[key] = value
            changes.append((path, previous, value))
    return merged, changes


class OpportunityOverrideApplier:
    def __init__(self, configuration: OverrideConfiguration, *, source_file: str) -> None:
        self.source_file = source_file
        self.overrides: dict[str, dict[str, Any]] = {}
        for url, values in configuration.overrides.items():
            _validate_fields(values)
            self.overrides[canonical_url(url)] = values

    def apply(self, opportunity: Opportunity) -> Opportunity:
        keys = {
            canonical_url(opportunity.source_url),
            canonical_url(opportunity.official_url or opportunity.source_url),
        }
        values = next((self.overrides[key] for key in keys if key in self.overrides), None)
        if values is None:
            return opportunity
        base = opportunity.model_dump(mode="python")
        existing = list(base.pop("manual_overrides", []))
        merged, changes = _merge(base, values)
        merged["manual_overrides"] = [
            *existing,
            *[
                ManualOverrideRecord(
                    field=field,
                    previous_value=previous,
                    override_value=value,
                    source_file=self.source_file,
                ).model_dump(mode="python")
                for field, previous, value in changes
            ],
        ]
        candidate = Opportunity.model_validate(merged)
        if "deadline" in values:
            candidate = candidate.model_copy(
                update={
                    "status": derive_status(
                        deadline=candidate.deadline,
                        as_of=opportunity.last_verified_at,
                        confirmed_accepting=opportunity.status in {OpportunityStatus.OPEN, OpportunityStatus.CLOSING_SOON},
                    )
                }
            )
        return candidate
