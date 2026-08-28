from __future__ import annotations

import json
import re
from datetime import date, datetime, time
from typing import Iterable
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from opportunity_radar.fetching import FetchedPage
from opportunity_radar.models import (
    ApplicationRequirements,
    ConfidenceLevel,
    CoverageStatus,
    EligibilityRequirements,
    FundingComponent,
    FundingDetails,
    Opportunity,
    ParticipationMode,
    SourceEvidence,
)
from opportunity_radar.normalization import derive_status


SECTION_ALIASES = {
    "eligibility": ("eligibility", "requirements", "who can apply", "who is eligible", "applicant requirements"),
    "dates": ("deadline", "important dates", "key dates", "timeline"),
    "funding": ("funding", "benefits", "what we cover", "scholarship", "travel support"),
    "application": ("application", "how to apply", "apply"),
}
CATEGORY_PATTERNS = (
    ("CFP", ("call for papers", "call for proposals", "submit a talk", "speaker proposal")),
    ("Internship", ("internship", "intern programme", "intern program")),
    ("Fellowship", ("fellowship", "fellow programme", "fellow program")),
    ("Scholarship", ("scholarship",)),
    ("Startup Grant", ("startup grant", "non-dilutive grant", "founder grant")),
    ("Grant", ("grant programme", "grant program", "grant opportunity")),
    ("Accelerator", ("accelerator", "incubator")),
    ("CTF / Competition", ("ctf", "capture the flag", "hackathon", "competition", "challenge")),
)
MONTHS = {
    name.casefold(): number
    for number, name in enumerate(
        ("", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
    )
    if name
}
DATE_PATTERN = re.compile(
    r"\b(?:"
    r"(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(?P<year>20\d{2})"
    r"|(?P<iso>20\d{2}-\d{2}-\d{2})"
    r")\b",
    re.IGNORECASE,
)
URL_PATTERN = re.compile(r"https?://[^\s)>\]]+", re.IGNORECASE)


def _normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def _sentences(text: str) -> list[str]:
    return [item.strip(" -•\t") for item in re.split(r"(?<=[.!?])\s+|\n+", text) if item.strip()]


def _extract_sections(html: str) -> dict[str, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    sections: dict[str, list[str]] = {}
    headings = soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6"])
    for heading in headings:
        name = _normalize(heading.get_text(" ", strip=True))
        content: list[str] = []
        for sibling in heading.next_siblings:
            if isinstance(sibling, Tag) and sibling.name in {"h1", "h2", "h3", "h4", "h5", "h6"}:
                break
            if isinstance(sibling, Tag):
                value = sibling.get_text(" ", strip=True)
            else:
                value = str(sibling).strip()
            if value:
                content.append(" ".join(value.split()))
        if content:
            sections[name] = content
    return sections


def _section_text(sections: dict[str, list[str]], kind: str) -> str | None:
    aliases = SECTION_ALIASES[kind]
    matches: list[str] = []
    for heading, content in sections.items():
        if any(alias in heading for alias in aliases):
            matches.extend(content)
    return "\n".join(matches) if matches else None


def _first_matching_sentence(text: str, patterns: Iterable[str]) -> str | None:
    for sentence in _sentences(text):
        normalized = _normalize(sentence)
        if any(pattern in normalized for pattern in patterns):
            return sentence
    return None


def _parse_date(text: str) -> tuple[date, str] | None:
    match = DATE_PATTERN.search(text)
    if not match:
        return None
    if match.group("iso"):
        parsed = date.fromisoformat(match.group("iso"))
    else:
        parsed = date(
            int(match.group("year")),
            MONTHS[match.group("month").casefold()],
            int(match.group("day")),
        )
    return parsed, match.group(0)


def _json_ld(soup: BeautifulSoup) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for node in soup.find_all("script", type="application/ld+json"):
        try:
            value = json.loads(node.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if isinstance(candidate, dict):
                graph = candidate.get("@graph")
                if isinstance(graph, list):
                    records.extend(item for item in graph if isinstance(item, dict))
                records.append(candidate)
    return records


def _organization_from_json_ld(records: list[dict[str, object]]) -> str | None:
    for record in records:
        for key in ("hiringOrganization", "organizer", "provider"):
            value = record.get(key)
            if isinstance(value, dict) and isinstance(value.get("name"), str):
                return value["name"].strip()
    return None


def _evidence(field: str, value: object, snippet: str, page: FetchedPage) -> SourceEvidence:
    return SourceEvidence(
        field=field,
        value=str(value),
        source_url=page.final_url,
        confidence=ConfidenceLevel.HIGH,
        evidence_text=snippet.strip()[:300],
    )


def _coverage(text: str, noun_patterns: tuple[str, ...]) -> tuple[FundingComponent, str | None]:
    for sentence in _sentences(text):
        normalized = _normalize(sentence)
        if not any(noun in normalized for noun in noun_patterns):
            continue
        if any(term in normalized for term in ("not covered", "not provided", "at your own expense", "self-funded")):
            return FundingComponent(status=CoverageStatus.NOT_COVERED), sentence
        if "reimburse" in normalized:
            return FundingComponent(status=CoverageStatus.REIMBURSED), sentence
        if any(term in normalized for term in ("partially covered", "partial funding", "contribution toward")):
            return FundingComponent(status=CoverageStatus.PARTIALLY_COVERED), sentence
        if any(term in normalized for term in ("covered", "provided", "included", "paid for", "we cover")):
            return FundingComponent(status=CoverageStatus.COVERED), sentence
    return FundingComponent(), None


def _money_value(text: str, label: str) -> tuple[str | None, str | None]:
    currency = r"(?:USD|EUR|GBP|MUR|NGN|\$|€|£)\s?[\d,]+(?:\.\d+)?(?:\s?(?:per month|monthly|per year|annually))?"
    patterns = (
        re.compile(rf"[^.\n]*\b{label}\b[^.\n]*?({currency})[^.\n]*", re.IGNORECASE),
        re.compile(rf"[^.\n]*?({currency})[^.\n]*\b{label}\b[^.\n]*", re.IGNORECASE),
    )
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            return match.group(1).strip(), match.group(0).strip()
    return None, None


class DeterministicOpportunityExtractor:
    """Conservative page-local factual extractor with exact source evidence."""

    def extract(self, page: FetchedPage) -> Opportunity:
        soup = BeautifulSoup(page.raw_html, "html.parser")
        text = page.cleaned_text.strip()
        sections = _extract_sections(page.raw_html)
        records = _json_ld(soup)
        title_node = soup.find("meta", property="og:title")
        h1 = soup.find("h1")
        title = (
            (title_node.get("content") if title_node else None)
            or (h1.get_text(" ", strip=True) if h1 else None)
            or page.page_title
            or "Unknown opportunity"
        )
        title = " ".join(str(title).split())

        site_name = soup.find("meta", property="og:site_name")
        organization = _organization_from_json_ld(records)
        if not organization and site_name and site_name.get("content"):
            organization = " ".join(str(site_name["content"]).split())
        if not organization:
            match = re.search(r"\b(?:organized|offered|hosted|presented) by\s+([^\n.]{2,100})", text, re.IGNORECASE)
            organization = match.group(1).strip() if match else None

        normalized_all = _normalize(f"{title}\n{text}")
        category = "Unknown"
        for candidate, patterns in CATEGORY_PATTERNS:
            if any(pattern in normalized_all for pattern in patterns):
                category = candidate
                break

        application_url: str | None = None
        application_snippet: str | None = None
        for anchor in soup.find_all("a", href=True):
            label = _normalize(anchor.get_text(" ", strip=True))
            if label in {"apply", "apply now", "application", "application form", "submit application"} or "apply now" in label:
                application_url = urljoin(page.final_url, str(anchor["href"]))
                application_snippet = anchor.get_text(" ", strip=True) or application_url
                break

        eligibility_text = _section_text(sections, "eligibility")
        eligibility_source = eligibility_text or ""
        eligibility = EligibilityRequirements(raw_text=eligibility_text)
        eligibility_evidence: str | None = None
        if eligibility_text:
            eligibility_evidence = eligibility_text[:300]
            deferred = any(
                phrase in _normalize(eligibility_text)
                for phrase in ("see full eligibility", "see eligibility criteria", "refer to", "available on the application portal", "additional requirements")
            )
            explicit_eligibility_heading = any(
                heading in {"eligibility", "who can apply", "who is eligible", "applicant requirements"}
                for heading in sections
            )
            eligibility.requirements_complete = (
                False if deferred else True if explicit_eligibility_heading and len(eligibility_text) >= 20 else None
            )
        requirement_text = eligibility_text or text

        nationality_match = re.search(
            r"(?:must be|open to|eligible (?:to|for))\s+(?:citizens|nationals|applicants|students)?\s*(?:of|from)\s+([A-Za-z ,/&-]{3,100})",
            requirement_text,
            re.IGNORECASE,
        )
        if nationality_match:
            raw = nationality_match.group(1).split(".", 1)[0]
            eligibility.nationalities_allowed = [
                item.strip() for item in re.split(r",|\bor\b|/", raw) if item.strip()
            ]
            eligibility_evidence = nationality_match.group(0)

        age_range = re.search(r"(?:aged?|ages?)\s+(\d{1,2})\s*(?:-|to|and)\s*(\d{1,2})", requirement_text, re.IGNORECASE)
        min_age = re.search(r"(?:at least|minimum age(?: of)?|aged? over)\s*(\d{1,2})", requirement_text, re.IGNORECASE)
        max_age = re.search(r"(?:under|maximum age(?: of)?|no older than)\s*(\d{1,2})", requirement_text, re.IGNORECASE)
        if age_range:
            eligibility.minimum_age, eligibility.maximum_age = int(age_range.group(1)), int(age_range.group(2))
            eligibility_evidence = age_range.group(0)
        else:
            if min_age:
                eligibility.minimum_age = int(min_age.group(1))
                eligibility_evidence = min_age.group(0)
            if max_age:
                eligibility.maximum_age = int(max_age.group(1))
                eligibility_evidence = max_age.group(0)

        normalized_requirements = _normalize(requirement_text)
        if re.search(r"must be (?:a )?(?:current )?(?:undergraduate |graduate )?student", normalized_requirements) or any(
            phrase in normalized_requirements
            for phrase in ("must be currently enrolled", "current students are eligible", "open to current students")
        ):
            eligibility.student_required = True
        if "undergraduate" in normalized_requirements:
            eligibility.undergraduate_eligible = True
        if any(term in normalized_requirements for term in ("graduate student", "postgraduate", "master's student", "phd student")):
            eligibility.graduate_eligible = True
        graduation_match = re.search(r"graduat(?:e|ing|ion)[^\n.]{0,40}\b(20\d{2})(?:\s*(?:,|or|to|-)\s*(20\d{2}))?", requirement_text, re.IGNORECASE)
        if graduation_match:
            first = int(graduation_match.group(1))
            second = int(graduation_match.group(2)) if graduation_match.group(2) else first
            eligibility.graduation_years = list(range(first, second + 1))
            eligibility_evidence = graduation_match.group(0)
        experience_match = re.search(r"(\d+(?:\.\d+)?)\+?\s+years?(?: of)?(?: relevant| professional| work)? experience", requirement_text, re.IGNORECASE)
        if experience_match:
            eligibility.minimum_years_experience = float(experience_match.group(1))
            eligibility_evidence = experience_match.group(0)
        skills_match = re.search(r"(?:required skills?|skills required|experience (?:with|in))\s*[:\-]?\s*([^\n.]{3,160})", requirement_text, re.IGNORECASE)
        if skills_match:
            eligibility.required_skills = [item.strip() for item in re.split(r",|;|\band\b", skills_match.group(1)) if item.strip()]
            eligibility_evidence = skills_match.group(0)
        if re.search(r"(?:must be|open to) (?:a )?(?:startup )?founder", requirement_text, re.IGNORECASE):
            eligibility.founder_required = True
            eligibility_evidence = _first_matching_sentence(requirement_text, ("founder",))

        date_text = _section_text(sections, "dates") or text
        deadline: datetime | None = None
        deadline_snippet = _first_matching_sentence(date_text, ("deadline", "applications close", "apply by", "closing date"))
        if deadline_snippet:
            parsed = _parse_date(deadline_snippet)
            if parsed:
                deadline = datetime.combine(parsed[0], time(23, 59, 59))
        opening_date: date | None = None
        opening_snippet = _first_matching_sentence(date_text, ("applications open", "opening date", "opens on"))
        if opening_snippet:
            parsed = _parse_date(opening_snippet)
            opening_date = parsed[0] if parsed else None
        start_date: date | None = None
        end_date: date | None = None
        programme_snippet = _first_matching_sentence(date_text, ("programme dates", "program dates", "starts on", "runs from"))
        if programme_snippet:
            dates = list(DATE_PATTERN.finditer(programme_snippet))
            parsed_dates = [_parse_date(match.group(0))[0] for match in dates if _parse_date(match.group(0))]
            if parsed_dates:
                start_date = parsed_dates[0]
                end_date = parsed_dates[1] if len(parsed_dates) > 1 else None

        funding_text = _section_text(sections, "funding") or text
        flights, flights_snippet = _coverage(funding_text, ("flight", "airfare", "air travel"))
        accommodation, accommodation_snippet = _coverage(funding_text, ("accommodation", "hotel", "lodging"))
        visa_support, visa_snippet = _coverage(funding_text, ("visa support", "visa assistance", "visa fees"))
        meals, meals_snippet = _coverage(funding_text, ("meals", "per diem", "daily allowance"))
        registration, registration_snippet = _coverage(funding_text, ("registration", "conference pass", "event fee"))
        salary, salary_snippet = _money_value(funding_text, "salary")
        stipend, stipend_snippet = _money_value(funding_text, "stipend")
        grant, grant_snippet = _money_value(funding_text, "grant")
        prize, prize_snippet = _money_value(funding_text, "prize(?: money)?")
        funding = FundingDetails(
            paid=True if salary or stipend else None,
            salary=salary,
            stipend=stipend,
            grant=grant,
            prize_money=prize,
            flights=flights,
            accommodation=accommodation,
            visa_support=visa_support,
            meals=meals,
            registration=registration,
        )

        application_text = _section_text(sections, "application") or text
        fee_snippet = _first_matching_sentence(application_text, ("application fee", "no fee", "free to apply"))
        fee: bool | None = None
        fee_amount: str | None = None
        if fee_snippet:
            normalized_fee = _normalize(fee_snippet)
            fee = not any(term in normalized_fee for term in ("no application fee", "no fee", "free to apply"))
            amount_match = re.search(r"(?:USD|EUR|GBP|MUR|NGN|\$|€|£)\s?[\d,]+(?:\.\d+)?", fee_snippet)
            fee_amount = amount_match.group(0) if amount_match else None
        application = ApplicationRequirements(
            application_fee=fee,
            fee_amount=fee_amount,
            cv_required=True if re.search(r"\b(?:cv|curriculum vitae|résumé|resume)\b", application_text, re.IGNORECASE) else None,
            cover_letter_required=True if re.search(r"\bcover letter\b", application_text, re.IGNORECASE) else None,
            transcript_required=True if re.search(r"\btranscript\b", application_text, re.IGNORECASE) else None,
            portfolio_required=True if re.search(r"\bportfolio\b", application_text, re.IGNORECASE) else None,
            github_required=True if re.search(r"\bgithub\b", application_text, re.IGNORECASE) else None,
            video_required=True if re.search(r"\bvideo (?:submission|application)\b", application_text, re.IGNORECASE) else None,
            technical_assessment=True if re.search(r"\btechnical assessment\b", application_text, re.IGNORECASE) else None,
            coding_challenge=True if re.search(r"\bcoding challenge\b", application_text, re.IGNORECASE) else None,
            proposal_required=True if re.search(r"\b(?:research|talk) proposal\b", application_text, re.IGNORECASE) else None,
            pitch_deck_required=True if re.search(r"\bpitch deck\b", application_text, re.IGNORECASE) else None,
            nomination_required=True if re.search(r"\bnomination required\b", application_text, re.IGNORECASE) else None,
        )

        location_match = re.search(r"\bLocation\s*:\s*([^\n]{2,100})", text, re.IGNORECASE)
        location = location_match.group(1).strip() if location_match else None
        city: str | None = None
        country: str | None = None
        if location:
            parts = [part.strip() for part in location.split(",") if part.strip()]
            country = parts[-1]
            city = parts[0] if len(parts) > 1 else None
        participation_mode = ParticipationMode.UNKNOWN
        if re.search(r"\bfully remote\b|\bremote opportunity\b|\bremote programme\b", text, re.IGNORECASE):
            participation_mode = ParticipationMode.REMOTE
        elif re.search(r"\bhybrid\b", text, re.IGNORECASE):
            participation_mode = ParticipationMode.HYBRID
        elif re.search(r"\bin-person\b|\bin person\b|\bon-site\b|\bonsite\b", text, re.IGNORECASE):
            participation_mode = ParticipationMode.IN_PERSON

        evidence: list[SourceEvidence] = []
        if deadline and deadline_snippet:
            evidence.append(_evidence("deadline", deadline.isoformat(), deadline_snippet, page))
        if eligibility_evidence and any((eligibility.nationalities_allowed, eligibility.minimum_age is not None, eligibility.maximum_age is not None, eligibility.student_required is not None, eligibility.undergraduate_eligible is not None, eligibility.graduation_years, eligibility.minimum_years_experience is not None, eligibility.founder_required is not None)):
            evidence.append(_evidence("eligibility", "structured requirements", eligibility_evidence, page))
        for field, component, snippet in (
            ("funding.flights", flights, flights_snippet),
            ("funding.accommodation", accommodation, accommodation_snippet),
            ("funding.visa_support", visa_support, visa_snippet),
        ):
            if component.status is not CoverageStatus.UNKNOWN and snippet:
                evidence.append(_evidence(field, component.status.value, snippet, page))
        for field, value, snippet in (
            ("funding.salary", salary, salary_snippet),
            ("funding.stipend", stipend, stipend_snippet),
            ("funding.grant", grant, grant_snippet),
            ("funding.prize_money", prize, prize_snippet),
        ):
            if value and snippet:
                evidence.append(_evidence(field, value, snippet, page))
        if fee is not None and fee_snippet:
            evidence.append(_evidence("application.application_fee", fee, fee_snippet, page))
        if application_url and application_snippet:
            evidence.append(_evidence("application_url", application_url, application_snippet, page))

        accepting = bool(application_url and re.search(r"\bapply now\b|\bapplications? (?:are )?open\b", text, re.IGNORECASE))
        return Opportunity(
            title=title,
            organization=organization,
            category=category,
            source_url=page.requested_url,
            official_url=page.final_url,
            application_url=application_url,
            status=derive_status(deadline=deadline, as_of=page.fetched_at, confirmed_accepting=accepting),
            country=country,
            city=city,
            participation_mode=participation_mode,
            opening_date=opening_date,
            deadline=deadline,
            program_start_date=start_date,
            program_end_date=end_date,
            summary=None,
            eligibility=eligibility,
            funding=funding,
            application=application,
            evidence=evidence,
            discovered_at=page.fetched_at,
            last_verified_at=page.fetched_at,
            extraction_confidence=ConfidenceLevel.MEDIUM,
            semantic_input_character_count=0,
            semantic_input_limit=None,
        )
