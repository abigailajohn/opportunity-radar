from __future__ import annotations

from collections.abc import Iterable

from opportunity_radar.models import (
    CoverageStatus,
    MatchMode,
    Opportunity,
    OpportunityProfile,
    OpportunityStatus,
    ParticipationMode,
)
from opportunity_radar.providers import SemanticJudgment
from opportunity_radar.scoring import (
    FeasibilityLevel,
    FrictionLevel,
    InformationConfidenceLevel,
    RelevanceLevel,
    TimingLevel,
    ValueLevel,
)


TERM_ALIASES = {
    "application security": {"application security", "appsec", "product security"},
    "security engineering": {"security engineering", "product security", "devsecops"},
    "artificial intelligence": {"artificial intelligence", "ai", "machine learning"},
    "software engineering": {"software engineering", "swe", "software development"},
    "api security": {"api security", "api penetration testing", "web api security"},
    "cloud security": {"cloud security", "cloud native security", "devsecops"},
    "open source": {"open source", "oss"},
    "cybersecurity": {"cybersecurity", "cyber security", "information security"},
    "offensive security": {"offensive security", "penetration testing", "red teaming"},
    "threat modeling": {"threat modeling", "threat modelling"},
    "entrepreneurship": {"entrepreneurship", "technical entrepreneurship", "startup", "founder", "accelerator", "incubator"},
    "public speaking": {"public speaking", "speaking", "speaker", "call for papers", "cfp", "conference talk"},
    "security competition": {"security competition", "ctf", "capture the flag", "hackathon", "cyber competition"},
}
DISCOVERY_TERMS = {
    "emerging technology", "quantum", "robotics", "biotechnology", "climate technology",
    "space technology", "deep tech", "web3", "blockchain",
}


def _norm(value: str) -> str:
    return " ".join("".join(character if character.isalnum() else " " for character in value.casefold()).split())


def _canonical_term(value: str) -> str:
    normalized = _norm(value)
    for canonical, aliases in TERM_ALIASES.items():
        if normalized == canonical or normalized in {_norm(alias) for alias in aliases}:
            return canonical
    return normalized


def _contains_concept(text: str, concept: str) -> bool:
    aliases = TERM_ALIASES.get(concept, {concept})
    normalized = f" {_norm(text)} "
    return any(f" {_norm(alias)} " in normalized for alias in aliases)


def _extra(profile: OpportunityProfile, key: str, default):
    return (profile.model_extra or {}).get(key, default)


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _profile_signals(profile: OpportunityProfile) -> list[tuple[str, int, str]]:
    signals: list[tuple[str, int, str]] = []
    for area, weight in (("primary", 4), ("secondary", 3), ("adjacent", 2), ("discovery", 1)):
        for term in getattr(profile.professional_identity, area):
            signals.append((_canonical_term(term), weight, f"professional_identity.{area}: {term}"))
    career_scope = _extra(profile, "career_scope", {})
    if isinstance(career_scope, dict):
        for term in career_scope.get("primary", []):
            signals.append((_canonical_term(str(term)), 3, f"career_scope.primary: {term}"))
    technical_skills = _extra(profile, "technical_skills", {})
    if isinstance(technical_skills, dict):
        for term in _strings(technical_skills):
            signals.append((_canonical_term(term), 3, f"technical_skills: {term}"))
    for experience in profile.experience:
        for term in [experience.role, *experience.domains]:
            signals.append((_canonical_term(term), 4, f"experience at {experience.organization}: {term}"))
    for project in profile.projects:
        for term in [project.name, *project.domains]:
            signals.append((_canonical_term(term), 3, f"project {project.name}: {term}"))
    for key, weight in (
        ("leadership_and_community", 3),
        ("public_speaking", 3),
        ("competitive_experience", 3),
        ("open_source", 2),
    ):
        for term in _strings(_extra(profile, key, [])):
            if len(term) <= 80:
                signals.append((_canonical_term(term), weight, f"{key}: {term}"))
    return signals


def _opportunity_text(opportunity: Opportunity) -> str:
    values = [
        opportunity.title,
        opportunity.organization or "",
        opportunity.category,
        *opportunity.subcategories,
        opportunity.summary or "",
        opportunity.eligibility.raw_text or "",
        *opportunity.eligibility.required_skills,
        *opportunity.eligibility.preferred_skills,
    ]
    return "\n".join(values)


class DeterministicSemanticAssessor:
    """Transparent zero-LLM replacement for semantic assessment judgments."""

    def assess(self, opportunity: Opportunity, profile: OpportunityProfile) -> SemanticJudgment:
        text = _opportunity_text(opportunity)
        matches: dict[str, tuple[int, str]] = {}
        for concept, weight, source in _profile_signals(profile):
            if concept and _contains_concept(text, concept):
                previous = matches.get(concept)
                if previous is None or weight > previous[0]:
                    matches[concept] = (weight, source)
        ordered = sorted(matches.items(), key=lambda item: (-item[1][0], item[0]))
        strongest = ordered[0][1][0] if ordered else 0
        strong_count = sum(1 for _, (weight, _) in ordered if weight >= 3)
        if strongest >= 4 and strong_count >= 2:
            relevance = RelevanceLevel.EXCEPTIONAL_DIRECT_FIT
        elif strongest >= 4:
            relevance = RelevanceLevel.STRONG_DIRECT_FIT
        elif strongest >= 2:
            relevance = RelevanceLevel.MODERATE_ADJACENT_FIT
        else:
            relevance = RelevanceLevel.LITTLE_MEANINGFUL_FIT

        normalized_text = _norm(text)
        discovery_terms = sorted(term for term in DISCOVERY_TERMS if _norm(term) in normalized_text)
        match_mode = MatchMode.MATCH
        discovery_reason: str | None = None
        if relevance in {RelevanceLevel.LITTLE_MEANINGFUL_FIT, RelevanceLevel.NONE} and discovery_terms:
            relevance = RelevanceLevel.CREDIBLE_DISCOVERY
            match_mode = MatchMode.DISCOVERY
            discovery_reason = (
                f"Discovery value from explicit {discovery_terms[0]} exposure, consistent with the profile's "
                "emerging-technology discovery preference."
            )

        evidence_items = [source for _, (_, source) in ordered[:3]]
        concepts = [concept for concept, _ in ordered[:3]]
        if evidence_items:
            relevance_reason = f"Matched {', '.join(concepts)} using {', '.join(evidence_items)}."
            why_you = f"Alignment with {', '.join(concepts)}. Profile evidence: {', '.join(evidence_items)}."
        elif discovery_reason:
            relevance_reason = discovery_reason
            why_you = "No strong direct profile match was found; this is surfaced through the configured discovery path."
        else:
            relevance_reason = "No strong normalized profile-domain match was found in the extracted opportunity facts."
            why_you = "The extracted facts do not provide enough evidence for a strong direct profile match."

        positives: list[str] = []
        if opportunity.funding.paid or opportunity.funding.salary:
            positives.append("paid compensation")
        if opportunity.funding.stipend:
            positives.append("stipend")
        if opportunity.funding.grant:
            positives.append("grant funding")
        if opportunity.funding.prize_money:
            positives.append("prize money")
        for label, component in (
            ("flights covered", opportunity.funding.flights),
            ("accommodation covered", opportunity.funding.accommodation),
            ("visa support", opportunity.funding.visa_support),
            ("registration covered", opportunity.funding.registration),
        ):
            if component.status in {CoverageStatus.COVERED, CoverageStatus.REIMBURSED}:
                positives.append(label)
        if opportunity.funding.cloud_credits or opportunity.funding.developer_credits:
            positives.append("developer/cloud credits")
        if relevance in {RelevanceLevel.EXCEPTIONAL_DIRECT_FIT, RelevanceLevel.STRONG_DIRECT_FIT}:
            positives.append("strong technical alignment")
        category_text = _norm(opportunity.category)
        if "founder" in category_text or "accelerator" in category_text or opportunity.eligibility.founder_required:
            positives.append("founder support")
        if "cfp" in category_text or "speaking" in category_text:
            positives.append("speaking visibility")
        if "research" in normalized_text:
            positives.append("research exposure")
        if any(term in normalized_text for term in ("training", "certification", "mentorship")):
            positives.append("structured learning")
        if opportunity.country and opportunity.country.casefold() not in {
            item.casefold() for item in profile.identity.nationality + [profile.identity.residence.get("country", "")]
        }:
            positives.append("international exposure")

        unique_positives = list(dict.fromkeys(positives))
        if len(unique_positives) >= 5:
            value = ValueLevel.EXCEPTIONAL_MULTI_DIMENSIONAL
        elif len(unique_positives) == 4:
            value = ValueLevel.VERY_HIGH
        elif len(unique_positives) == 3:
            value = ValueLevel.STRONG
        elif len(unique_positives) == 2:
            value = ValueLevel.MODERATE
        elif unique_positives:
            value = ValueLevel.LIMITED
        else:
            value = ValueLevel.MINIMAL
        value_reason = (
            f"Evidence-backed value signals: {', '.join(unique_positives)}."
            if unique_positives
            else "No explicit high-value funding, access, learning, or visibility signal was extracted."
        )

        if opportunity.participation_mode is ParticipationMode.REMOTE:
            feasibility = FeasibilityLevel.VERY_FEASIBLE
            feasibility_reason = "The opportunity is explicitly remote."
        elif opportunity.participation_mode is ParticipationMode.IN_PERSON:
            covered = sum(
                component.status in {CoverageStatus.COVERED, CoverageStatus.REIMBURSED}
                for component in (opportunity.funding.flights, opportunity.funding.accommodation, opportunity.funding.visa_support)
            )
            if covered == 3:
                feasibility = FeasibilityLevel.MOSTLY_FEASIBLE
                feasibility_reason = "In-person participation has explicit flights, accommodation, and visa support."
            elif any(
                component.status is CoverageStatus.NOT_COVERED
                for component in (opportunity.funding.flights, opportunity.funding.accommodation)
            ):
                feasibility = FeasibilityLevel.SIGNIFICANT_BARRIERS
                feasibility_reason = "In-person participation has explicit unfunded travel or accommodation."
            else:
                feasibility = FeasibilityLevel.FEASIBLE_WITH_UNKNOWNS
                feasibility_reason = "In-person participation is possible, but material travel support is unknown."
        else:
            feasibility = FeasibilityLevel.FEASIBLE_WITH_UNKNOWNS
            feasibility_reason = "Participation mode or material logistics remain unknown."

        timing_map = {
            OpportunityStatus.OPEN: (TimingLevel.HEALTHY_WINDOW, "Applications are confirmed open."),
            OpportunityStatus.CLOSING_SOON: (TimingLevel.CLOSING_SOON, "The deterministic deadline rule marks this as closing soon."),
            OpportunityStatus.CLOSED: (TimingLevel.CLOSED, "The deterministic deadline rule marks this as closed."),
            OpportunityStatus.OPENING_SOON: (TimingLevel.FUTURE_PREPARATION, "The opportunity is confirmed opening soon."),
            OpportunityStatus.FUTURE_CYCLE: (TimingLevel.FUTURE_PREPARATION, "This is a confirmed future cycle."),
            OpportunityStatus.EXPECTED: (TimingLevel.FUTURE_PREPARATION, "This is an expected future opportunity."),
            OpportunityStatus.UNKNOWN: (TimingLevel.UNCLEAR, "Opening status and timing are unclear."),
        }
        timing, timing_reason = timing_map[opportunity.status]

        requirements = opportunity.application
        friction_count = sum(
            value is True
            for value in (
                requirements.cv_required,
                requirements.cover_letter_required,
                requirements.transcript_required,
                requirements.portfolio_required,
                requirements.github_required,
                requirements.video_required,
                requirements.technical_assessment,
                requirements.coding_challenge,
                requirements.proposal_required,
                requirements.pitch_deck_required,
                requirements.nomination_required,
            )
        ) + len(requirements.essays)
        if requirements.application_fee:
            friction = FrictionLevel.HIGH if friction_count < 4 else FrictionLevel.VERY_HIGH
            friction_reason = "An application fee is required in addition to published materials."
        elif friction_count == 0:
            friction = FrictionLevel.VERY_LOW
            friction_reason = "No substantial application requirements were extracted."
        elif friction_count <= 2:
            friction = FrictionLevel.LOW
            friction_reason = f"Only {friction_count} standard application requirements were extracted."
        elif friction_count <= 5:
            friction = FrictionLevel.MEDIUM
            friction_reason = f"The application has {friction_count} material requirements."
        else:
            friction = FrictionLevel.HIGH
            friction_reason = f"The application has {friction_count} material requirements."

        concerns: list[str] = []
        if opportunity.deadline is None:
            concerns.append("Deadline is unknown.")
        if opportunity.eligibility.requirements_complete is not True:
            concerns.append("Published eligibility requirements are not confirmed complete.")
        if opportunity.participation_mode is ParticipationMode.IN_PERSON:
            for label, component in (
                ("flights", opportunity.funding.flights),
                ("accommodation", opportunity.funding.accommodation),
                ("visa support", opportunity.funding.visa_support),
            ):
                if component.status is CoverageStatus.UNKNOWN:
                    concerns.append(f"In-person {label} information is unknown.")
        if opportunity.semantic_input_truncated:
            concerns.append("Semantic extraction input was truncated.")

        if opportunity.extraction_confidence.value == "high" and not opportunity.semantic_input_truncated:
            confidence = InformationConfidenceLevel.HIGH
            confidence_reason = "Extraction confidence is high and input was not truncated."
        elif opportunity.extraction_confidence.value == "low" or opportunity.semantic_input_truncated:
            confidence = InformationConfidenceLevel.LOW
            confidence_reason = "Extraction confidence is low or source input was truncated."
        else:
            confidence = InformationConfidenceLevel.MEDIUM
            confidence_reason = "Extraction confidence is medium with conservative unknown handling."

        why_it_matters = discovery_reason or value_reason
        return SemanticJudgment(
            relevance_level=relevance,
            relevance_reason=relevance_reason,
            value_level=value,
            value_reason=value_reason,
            feasibility_level=feasibility,
            feasibility_reason=feasibility_reason,
            timing_level=timing,
            timing_reason=timing_reason,
            friction_level=friction,
            friction_reason=friction_reason,
            confidence_level=confidence,
            confidence_reason=confidence_reason,
            match_mode=match_mode,
            why_you=why_you,
            why_it_matters=why_it_matters,
            discovery_reason=discovery_reason,
            top_positive_signals=tuple(unique_positives),
            concerns=tuple(concerns),
        )
