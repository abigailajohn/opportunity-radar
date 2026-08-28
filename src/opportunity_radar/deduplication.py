from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from opportunity_radar.models import ConfidenceLevel, Opportunity, SourceEvidence


def normalize_identity_text(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").casefold()).strip()


def canonical_url(value: object) -> str:
    parts = urlsplit(str(value))
    tracking = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid"}
    query = urlencode(sorted((k, v) for k, v in parse_qsl(parts.query) if not k.casefold().startswith("utm_") and k.casefold() not in tracking))
    return urlunsplit((parts.scheme.casefold(), parts.netloc.casefold(), parts.path.rstrip("/"), query, ""))


def _cycle(opportunity: Opportunity) -> str:
    for candidate in (opportunity.deadline, opportunity.program_start_date, opportunity.opening_date):
        if candidate is not None:
            return str(candidate.year)
    match = re.search(r"\b(20\d{2})\b", opportunity.title)
    return match.group(1) if match else "unknown"


def deduplication_key(opportunity: Opportunity) -> tuple[str, ...]:
    if opportunity.organization:
        return ("identity", normalize_identity_text(opportunity.organization), normalize_identity_text(opportunity.title), _cycle(opportunity))
    preferred_url = opportunity.application_url or opportunity.official_url or opportunity.source_url
    return ("url", canonical_url(preferred_url))


def deduplicate(opportunities: list[Opportunity]) -> list[Opportunity]:
    unique: dict[tuple[str, ...], Opportunity] = {}
    for opportunity in opportunities:
        key = deduplication_key(opportunity)
        if key not in unique:
            unique[key] = opportunity.model_copy(deep=True)
            continue
        retained = unique[key]
        duplicate_url = str(opportunity.source_url)
        known_urls = {str(item.source_url) for item in retained.evidence if item.field == "duplicate_source_url"}
        if duplicate_url != str(retained.source_url) and duplicate_url not in known_urls:
            retained.evidence.append(
                SourceEvidence(
                    field="duplicate_source_url",
                    value=duplicate_url,
                    source_url=opportunity.source_url,
                    confidence=ConfidenceLevel.HIGH,
                    evidence_text="Duplicate input for the same programme cycle.",
                )
            )
    return list(unique.values())
