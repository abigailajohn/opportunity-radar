from __future__ import annotations

from dataclasses import dataclass

from opportunity_radar.deduplication import canonical_url, normalize_identity_text
from opportunity_radar.models import CoverageStatus, Opportunity, OpportunityAssessment, PriorityBand


@dataclass(frozen=True)
class DigestResult:
    markdown: str
    selected_ids: frozenset[object]


def _funding(opportunity: Opportunity) -> str:
    details: list[str] = []
    for name in ("salary", "stipend", "grant", "prize_money"):
        value = getattr(opportunity.funding, name)
        if value:
            details.append(f"{name.replace('_', ' ').title()}: {value}")
    for name in ("flights", "accommodation", "visa_support"):
        component = getattr(opportunity.funding, name)
        if component.status is not CoverageStatus.UNKNOWN:
            details.append(f"{name.replace('_', ' ').title()}: {component.status.value.replace('_', ' ')}")
    return "; ".join(details) or "Unknown"


def _location(opportunity: Opportunity) -> str:
    place = ", ".join(item for item in (opportunity.city, opportunity.country) if item)
    mode = opportunity.participation_mode.value.replace("_", " ").title()
    return f"{place} — {mode}" if place else mode


def _card(opportunity: Opportunity, assessment: OpportunityAssessment) -> str:
    concern = assessment.concerns[0] if assessment.concerns else "None identified"
    deadline = opportunity.deadline.isoformat() if opportunity.deadline else "Unknown"
    organization = opportunity.organization or "Unknown"
    link = opportunity.official_url or opportunity.application_url or opportunity.source_url
    return "\n".join(
        [
            f"### {opportunity.title}",
            f"- Organization: {organization}",
            f"- Priority: {assessment.priority_band.value.replace('_', ' ').title()} ({assessment.total_score:g}/100)",
            f"- Category: {opportunity.category}",
            f"- Location / participation: {_location(opportunity)}",
            f"- Eligibility: {assessment.eligibility_status.value.replace('_', ' ').title()} — {assessment.eligibility_reason}",
            f"- Why you: {assessment.why_you}",
            f"- Why it matters: {assessment.why_it_matters}",
            f"- Funding: {_funding(opportunity)}",
            f"- Deadline: {deadline}",
            f"- Concern: {concern}",
            f"- Recommended action: {assessment.recommended_action.value.replace('_', ' ').title()}",
            f"- Link: {link}",
        ]
    )


def _stable_rank_key(
    assessment: OpportunityAssessment,
    opportunity_by_id: dict[object, Opportunity],
) -> tuple[object, ...]:
    opportunity = opportunity_by_id[assessment.opportunity_id]
    deadline = opportunity.deadline.isoformat() if opportunity.deadline else "9999-12-31"
    source = opportunity.official_url or opportunity.application_url or opportunity.source_url
    return (
        -assessment.total_score,
        normalize_identity_text(opportunity.organization),
        normalize_identity_text(opportunity.title),
        deadline,
        canonical_url(source),
    )


def render_digest(
    opportunities: list[Opportunity],
    assessments: list[OpportunityAssessment],
) -> DigestResult:
    opportunity_by_id = {item.id: item for item in opportunities}
    ranked = sorted(assessments, key=lambda item: _stable_rank_key(item, opportunity_by_id))
    sections: list[tuple[str, list[OpportunityAssessment]]] = []
    exceptional = [a for a in ranked if a.priority_band is PriorityBand.EXCEPTIONAL][:3]
    strong = [a for a in ranked if a.priority_band is PriorityBand.STRONG_MATCH][:4]
    discovery = [a for a in ranked if a.priority_band is PriorityBand.DISCOVERY][:2]
    selected = [*exceptional, *strong, *discovery]
    if len(selected) < 5:
        worth = [a for a in ranked if a.priority_band is PriorityBand.WORTH_CHECKING][: 5 - len(selected)]
    else:
        worth = []
    selected.extend(worth)
    sections.extend(
        [
            ("Immediate Attention", exceptional),
            ("Strong Matches", strong),
            ("Discovery", discovery),
            ("Worth Checking", worth),
        ]
    )
    selected_ids = frozenset(a.opportunity_id for a in selected)
    lines = ["# Opportunity Radar Digest", ""]
    for heading, items in sections:
        if not items:
            continue
        lines.extend([f"## {heading}", ""])
        for assessment in items:
            lines.extend([_card(opportunity_by_id[assessment.opportunity_id], assessment), ""])

    lines.extend(["## Not selected", ""])
    omitted = [a for a in ranked if a.opportunity_id not in selected_ids]
    if not omitted:
        lines.extend(["No extracted opportunities were omitted.", ""])
    else:
        for assessment in omitted:
            opportunity = opportunity_by_id[assessment.opportunity_id]
            reason = assessment.concerns[0] if assessment.concerns else assessment.eligibility_reason
            lines.append(
                f"- {opportunity.title} — {assessment.priority_band.value.replace('_', ' ').title()}: {reason}"
            )
        lines.append("")
    return DigestResult("\n".join(lines), selected_ids)
