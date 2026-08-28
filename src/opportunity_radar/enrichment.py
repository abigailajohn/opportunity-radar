from __future__ import annotations

from copy import deepcopy
from typing import Any

from opportunity_radar.models import CoverageStatus, ExtractionDiagnostics, Opportunity
from opportunity_radar.providers import OpportunityExtractor, PageFetcher
from opportunity_radar.normalization import derive_status


_MATERIAL_PATHS = (
    "deadline", "opening_date", "program_start_date", "program_end_date",
    "eligibility.requirements_complete", "eligibility.nationalities_allowed",
    "eligibility.regions_allowed",
    "eligibility.residence_requirements", "eligibility.minimum_age",
    "eligibility.maximum_age", "eligibility.student_required",
    "eligibility.undergraduate_eligible", "eligibility.graduate_eligible",
    "eligibility.graduation_years", "eligibility.minimum_years_experience",
    "eligibility.required_skills", "eligibility.gender_requirements",
    "eligibility.language_requirements", "eligibility.founder_required",
    "funding.paid", "funding.salary", "funding.stipend", "funding.grant",
    "funding.prize_money", "funding.flights", "funding.accommodation",
    "funding.visa_support", "funding.visa_fees", "funding.other_benefits",
    "application.application_fee", "application.fee_amount", "application.cv_required",
    "application.cover_letter_required", "application.transcript_required",
    "application.portfolio_required", "application.other_requirements",
)


def _get(data: dict[str, Any], path: str) -> Any:
    value: Any = data
    for part in path.split("."):
        value = value[part]
    return value


def _set(data: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    target = data
    for part in parts[:-1]:
        target = target[part]
    target[parts[-1]] = value


def _unknown(value: Any) -> bool:
    if value is None or value == [] or value == "":
        return True
    if isinstance(value, dict) and value.get("status") == CoverageStatus.UNKNOWN:
        return True
    return False


def _same_program(primary: Opportunity, enrichment: Opportunity) -> bool:
    stop = {"the", "and", "for", "programme", "program", "application", "2026", "2027"}
    left = {word.strip("-–—:,").casefold() for word in primary.title.split()} - stop
    right = {word.strip("-–—:,").casefold() for word in enrichment.title.split()} - stop
    return bool(left & right) or bool(primary.organization and enrichment.organization and primary.organization.casefold() == enrichment.organization.casefold())


def _needs_enrichment(opportunity: Opportunity) -> bool:
    diagnostics = opportunity.extraction_diagnostics
    return bool(set(diagnostics.material_fields_unknown) & {"deadline", "eligibility", "funding", "application_requirements"})


def merge_enrichment(primary: Opportunity, enrichment: Opportunity) -> Opportunity:
    data = primary.model_dump(mode="python")
    other = enrichment.model_dump(mode="python")
    conflicts: list[str] = list(primary.extraction_diagnostics.conflicts)
    for path in _MATERIAL_PATHS:
        current, candidate = _get(data, path), _get(other, path)
        if _unknown(candidate):
            continue
        if _unknown(current):
            _set(data, path, deepcopy(candidate))
        elif isinstance(current, list) and isinstance(candidate, list):
            _set(data, path, list(dict.fromkeys([*current, *candidate])))
        elif isinstance(current, dict) and isinstance(candidate, dict) and "status" in current and current.get("status") == candidate.get("status"):
            merged_component = deepcopy(candidate)
            notes = list(dict.fromkeys(value for value in (current.get("notes"), candidate.get("notes")) if value))
            merged_component["notes"] = " | ".join(notes) if notes else None
            merged_component["amount"] = candidate.get("amount") or current.get("amount")
            _set(data, path, merged_component)
        elif current != candidate:
            conflicts.append(f"{path}: primary={current!r}; application_page={candidate!r}")
            _set(data, path, deepcopy(candidate))

    for bound, exclusive in (("minimum_age", "minimum_age_exclusive"), ("maximum_age", "maximum_age_exclusive")):
        primary_bound = primary.model_dump(mode="python")["eligibility"][bound]
        candidate_bound = other["eligibility"][bound]
        if primary_bound is None and candidate_bound is not None:
            data["eligibility"][exclusive] = other["eligibility"][exclusive]
        elif primary_bound == candidate_bound and primary_bound is not None:
            data["eligibility"][exclusive] = bool(data["eligibility"][exclusive] or other["eligibility"][exclusive])

    primary_raw = data["eligibility"].get("raw_text")
    enrichment_raw = other["eligibility"].get("raw_text")
    if enrichment_raw and enrichment_raw not in (primary_raw or ""):
        data["eligibility"]["raw_text"] = "\n\nApplication page:\n".join(value for value in (primary_raw, enrichment_raw) if value)
    data["evidence"] = [*data["evidence"], *other["evidence"]]
    diagnostics = ExtractionDiagnostics.model_validate(data["extraction_diagnostics"])
    diagnostics.enrichment_attempted = True
    diagnostics.enrichment_source_url = enrichment.official_url or enrichment.source_url
    diagnostics.conflicts = list(dict.fromkeys(conflicts))
    found = set(diagnostics.material_fields_found) | set(enrichment.extraction_diagnostics.material_fields_found)
    unknown = (set(diagnostics.material_fields_unknown) | set(enrichment.extraction_diagnostics.material_fields_unknown)) - found
    diagnostics.material_fields_found = sorted(found)
    diagnostics.material_fields_unknown = sorted(unknown)
    diagnostics.warnings = list(dict.fromkeys([*diagnostics.warnings, *enrichment.extraction_diagnostics.warnings]))
    data["extraction_diagnostics"] = diagnostics.model_dump(mode="python")
    data["status"] = derive_status(
        deadline=data["deadline"],
        as_of=primary.last_verified_at,
        opening_date=data["opening_date"],
        rolling_application=bool(data["rolling_application"]),
        confirmed_accepting=primary.status.value in {"open", "closing_soon"},
    )
    return Opportunity.model_validate(data)


class OneHopOpportunityExtractor:
    """Fetch at most the explicit application URL and never traverse from it."""

    def __init__(self, extractor: OpportunityExtractor, fetcher: PageFetcher) -> None:
        self.extractor = extractor
        self.fetcher = fetcher

    def extract(self, page: Any) -> Opportunity:
        primary = self.extractor.extract(page)
        if primary.application_url is None or not _needs_enrichment(primary):
            return primary
        diagnostics = primary.extraction_diagnostics.model_copy(deep=True)
        diagnostics.enrichment_attempted = True
        try:
            enrichment_page = self.fetcher.fetch(str(primary.application_url))
            enrichment = self.extractor.extract(enrichment_page)
        except Exception as exc:
            diagnostics.warnings.append(f"Application-page enrichment failed: {exc}")
            return primary.model_copy(update={"extraction_diagnostics": diagnostics})
        diagnostics.enrichment_source_url = enrichment_page.final_url
        if not _same_program(primary, enrichment):
            diagnostics.warnings.append("Application page was not merged because same-program identity was not sufficiently clear.")
            return primary.model_copy(update={"extraction_diagnostics": diagnostics})
        return merge_enrichment(primary, enrichment)
