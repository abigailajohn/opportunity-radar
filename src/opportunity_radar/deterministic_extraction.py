from __future__ import annotations

import json
import re
from datetime import date, datetime, time
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from opportunity_radar.extraction import NotOpportunityPageError
from opportunity_radar.fetching import FetchedPage
from opportunity_radar.models import (
    ApplicationRequirements, ConfidenceLevel, CoverageStatus, EligibilityRequirements,
    ExtractionDiagnostics, FundingComponent, FundingDetails, Opportunity,
    ParticipationMode, SourceEvidence,
)
from opportunity_radar.normalization import derive_status

SECTION_ALIASES = {
    "eligibility": ("eligibility", "eligibility criteria", "who is this for", "who can apply", "who is eligible", "requirements", "applicant requirements"),
    "dates": ("deadline", "important dates", "key dates", "timeline"),
    "funding": ("funding", "benefits", "what we cover", "scholarship", "travel support", "what you'll gain", "financial support", "programme features", "program features"),
    "application": ("application", "how to apply", "everything you need to apply", "application process"),
}
HEADINGS = {f"h{level}" for level in range(1, 7)}
MONTHS = {name.casefold(): number for number, name in enumerate(("", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")) if name}
DATE_PATTERN = re.compile(r"\b(?:(?P<month>January|February|March|April|May|June|July|August|September|October|November|December)\s+(?P<day>\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(?P<year>20\d{2})|(?P<iso>20\d{2}-\d{2}-\d{2}))\b", re.I)
DEADLINE_LABELS = ("application deadline", "applications close", "application closes", "closing date", "apply by", "submit by", "final deadline", "deadline")
OPENING_LABELS = ("applications open", "application opens", "opening date", "opens on")
START_LABELS = ("programme start", "program start", "programme begins", "program begins", "internship begins", "internship starts", "training begins", "start date")
END_LABELS = ("programme end", "program end", "programme completion", "program completion", "internship ends", "training ends", "end date", "completion date")
RANGE_LABELS = ("programme dates", "program dates", "internship dates", "programme runs", "program runs")
DEADLINE_EXCLUSIONS = ("orientation", "internship dates", "programme dates", "program dates", "training", "interview", "selection", "decision", "completion")
APPLICATION_WINDOW_PATTERN = re.compile(
    r"\b(?P<start_month>January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(?P<start_day>\d{1,2})(?:st|nd|rd|th)?\s*(?:-|–|—|to)\s*"
    r"(?:(?P<end_month>January|February|March|April|May|June|July|August|September|October|November|December)\s+)?"
    r"(?P<end_day>\d{1,2})(?:st|nd|rd|th)?(?:,)?\s+(?P<year>20\d{2})\b",
    re.I,
)


def _normalize(value: str) -> str:
    return " ".join(value.casefold().replace("’", "'").split())


def _sentences(text: str) -> list[str]:
    return [item.strip(" -•\t") for item in re.split(r"(?<=[.!?])\s+|\n+", text) if item.strip()]


def _block_text(tag: Tag) -> str:
    if tag.name == "tr":
        return " | ".join(value for value in (" ".join(cell.get_text(" ", strip=True).split()) for cell in tag.find_all(["th", "td"])) if value)
    if tag.name == "dt":
        definition = tag.find_next_sibling("dd")
        if definition:
            return f"{tag.get_text(' ', strip=True)} | {definition.get_text(' ', strip=True)}"
    return " ".join(tag.get_text(" ", strip=True).split())


def _extract_sections(html: str) -> dict[str, list[str]]:
    soup = BeautifulSoup(html, "html.parser")
    sections: dict[str, list[str]] = {}
    known_labels = {alias for aliases in SECTION_ALIASES.values() for alias in aliases}

    def pseudo_heading(node: Tag) -> bool:
        if node.name not in {"strong", "b", "p", "div"}:
            return False
        value = _normalize(node.get_text(" ", strip=True)).strip("?:")
        return value in known_labels and len(value) <= 60

    candidates = [
        node for node in soup.find_all([*sorted(HEADINGS), "strong", "b", "p", "div"])
        if node.name in HEADINGS or pseudo_heading(node)
    ]
    for heading in candidates:
        name = _normalize(heading.get_text(" ", strip=True)).strip("?:")
        level = int(heading.name[1]) if heading.name in HEADINGS else 6
        is_pseudo = heading.name not in HEADINGS
        content: list[str] = []
        for node in heading.find_all_next():
            if node in candidates and node is not heading and (is_pseudo or node.name not in HEADINGS or int(node.name[1]) <= level):
                break
            if node.name in HEADINGS:
                value = _block_text(node)
                if value and value not in content:
                    content.append(value)
                continue
            if node.name not in {"p", "li", "tr", "dt"}:
                continue
            value = _block_text(node)
            if value and value not in content:
                content.append(value)
        if content:
            sections[name] = content
    return sections


def _section_text(sections: dict[str, list[str]], kind: str) -> str | None:
    content = [item for heading, items in sections.items() if any(alias == heading or alias in heading for alias in SECTION_ALIASES[kind]) for item in items]
    return "\n".join(dict.fromkeys(content)) if content else None


def _parse_date(text: str) -> date | None:
    match = DATE_PATTERN.search(text)
    if not match:
        return None
    if match.group("iso"):
        return date.fromisoformat(match.group("iso"))
    return date(int(match.group("year")), MONTHS[match.group("month").casefold()], int(match.group("day")))


def _structural_units(soup: BeautifulSoup, cleaned_text: str) -> list[str]:
    units = [_block_text(node) for node in soup.find_all(["tr", "dt", "li", "p"])]
    return list(dict.fromkeys([value for value in [*units, *_sentences(cleaned_text)] if value]))


def _labeled_date(units: list[str], labels: tuple[str, ...], exclusions: tuple[str, ...] = ()) -> tuple[date | None, str | None]:
    for unit in units:
        normalized = _normalize(unit)
        if any(re.search(rf"\b{re.escape(label)}\b", normalized) for label in labels) and not any(term in normalized for term in exclusions):
            parsed = _parse_date(unit)
            if parsed:
                return parsed, unit
    return None, None


def _labeled_range(units: list[str]) -> tuple[date | None, date | None, str | None]:
    for unit in units:
        normalized = _normalize(unit)
        if not any(label in normalized for label in RANGE_LABELS) or "orientation" in normalized:
            continue
        values = [_parse_date(match.group(0)) for match in DATE_PATTERN.finditer(unit)]
        values = [value for value in values if value]
        if values:
            return values[0], values[1] if len(values) > 1 else None, unit
    return None, None, None


def _application_window(units: list[str]) -> tuple[date | None, date | None, str | None]:
    for unit in units:
        normalized = _normalize(unit)
        if not re.search(r"\bapplications?\b", normalized) or not (re.match(r"applications?\s*:", unit, re.I) or normalized.startswith(("application ", "applications ")) or any(term in normalized for term in ("window", "timeline", "application period", "applications open"))):
            continue
        dates = [_parse_date(match.group(0)) for match in DATE_PATTERN.finditer(unit)]
        dates = [value for value in dates if value]
        if len(dates) >= 2:
            return dates[0], dates[1], unit
        match = APPLICATION_WINDOW_PATTERN.search(unit)
        if match:
            year = int(match.group("year")); start_month = MONTHS[match.group("start_month").casefold()]
            end_month = MONTHS[(match.group("end_month") or match.group("start_month")).casefold()]
            return date(year, start_month, int(match.group("start_day"))), date(year, end_month, int(match.group("end_day"))), unit
    return None, None, None


def _json_ld(soup: BeautifulSoup) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for node in soup.find_all("script", type="application/ld+json"):
        try:
            value = json.loads(node.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for candidate in value if isinstance(value, list) else [value]:
            if isinstance(candidate, dict):
                graph = candidate.get("@graph")
                if isinstance(graph, list): records.extend(item for item in graph if isinstance(item, dict))
                records.append(candidate)
    return records


def _organization(records: list[dict[str, object]], soup: BeautifulSoup, text: str) -> str | None:
    for record in records:
        for key in ("hiringOrganization", "organizer", "provider"):
            value = record.get(key)
            if isinstance(value, dict) and isinstance(value.get("name"), str): return value["name"].strip()
    site = soup.find("meta", property="og:site_name")
    if site and site.get("content"): return " ".join(str(site["content"]).split())
    match = re.search(r"\b(?:organized|offered|hosted|presented) by\s+([^\n.]{2,100})", text, re.I)
    return match.group(1).strip() if match else None


def _evidence(field: str, value: object, snippet: str, page: FetchedPage) -> SourceEvidence:
    return SourceEvidence(field=field, value=str(value), source_url=page.final_url, confidence=ConfidenceLevel.HIGH, evidence_text=snippet.strip()[:300])


def _coverage(text: str, nouns: tuple[str, ...]) -> tuple[FundingComponent, str | None]:
    inclusion_context = any(term in _normalize(text) for term in ("package includes", "benefits include", "we provide the following", "what we cover"))
    for sentence in _sentences(text):
        normalized = _normalize(sentence)
        if not any(noun in normalized for noun in nouns): continue
        if any(term in normalized for term in ("not covered", "not provided", "at your own expense", "self-funded")): return FundingComponent(status=CoverageStatus.NOT_COVERED, notes=sentence[:240]), sentence
        if "reimburse" in normalized: return FundingComponent(status=CoverageStatus.REIMBURSED, notes=sentence[:240]), sentence
        if any(term in normalized for term in ("partially covered", "partial funding", "contribution toward")): return FundingComponent(status=CoverageStatus.PARTIALLY_COVERED, notes=sentence[:240]), sentence
        if any(term in normalized for term in ("covered", "covers", "provided", "included", "paid", "pay for", "book and pay", "we cover", "we pay", "work with")): return FundingComponent(status=CoverageStatus.COVERED, notes=sentence[:240]), sentence
        if inclusion_context:
            return FundingComponent(status=CoverageStatus.COVERED, notes="Listed in an explicit included-benefits package."), sentence
    return FundingComponent(), None


def _money(text: str, label: str) -> tuple[str | None, str | None]:
    currency = r"(?:USD|EUR|GBP|MUR|NGN|\$|€|£)\s?[\d,]+(?:\.\d+)?(?:\s?(?:per month|monthly|per year|annually))?"
    for pattern in (re.compile(rf"[^.\n]*\b{label}\b[^.\n]*?({currency})[^.\n]*", re.I), re.compile(rf"[^.\n]*?({currency})[^.\n]*\b{label}\b[^.\n]*", re.I)):
        match = pattern.search(text)
        if match: return match.group(1).strip(), match.group(0).strip()
    return None, None


def _category(title: str, soup: BeautifulSoup) -> str:
    strong = _normalize("\n".join([title, *[node.get_text(" ", strip=True) for node in soup.find_all(["h1", "h2"])]]))
    body = _normalize(soup.get_text(" ", strip=True))
    rules = (
        ("Student Programme", r"\b(?:summer )?student programme\b|\bstudent program\b"),
        ("Internship", r"\binternship\b|\bintern programme\b|\bintern program\b"),
        ("Fellowship", r"\bfellowship\b|\bfellows programme\b|\bfellows program\b"), ("Hackathon / Competition", r"\bhackathon\b"),
        ("CTF / Competition", r"\bctf\b|capture the flag"),
        ("Startup Competition", r"\b(?:startup|innovation) challenge\b"),
        ("Scholarship", r"\bscholarship\b"), ("Grant", r"\bgrant\b"),
        ("Accelerator", r"\baccelerator\b|\bincubator\b"),
    )
    for category, pattern in rules:
        if re.search(pattern, strong): return category
    if "challenge" in strong and re.search(r"\bstartup\b|\bentrepreneur", body): return "Startup Competition"
    return "Unknown"


def _eligibility(section: str | None, sections: dict[str, list[str]]) -> tuple[EligibilityRequirements, list[str]]:
    result = EligibilityRequirements(raw_text=section)
    if not section: return result, []
    normalized = _normalize(section)
    deferred = any(phrase in normalized for phrase in ("see full eligibility", "refer to", "application portal for eligibility", "additional eligibility requirements", "full requirements"))
    bounded = any(any(alias == heading or alias in heading for alias in SECTION_ALIASES["eligibility"]) for heading in sections)
    result.requirements_complete = False if deferred else True if bounded and len(section) >= 20 else None
    snippets: list[str] = []
    age_range = re.search(r"(?:aged?|ages?)\s+(\d{1,2})\s*(?:-|to|and)\s*(\d{1,2})", section, re.I)
    strict_min = re.search(r"(?:strictly\s+)?older than\s+(\d{1,2})", section, re.I)
    inclusive_min = re.search(r"(?:at least\s+|minimum age(?: of)?\s*|aged?\s+)?(\d{1,2})\s*(?:or older|and above|\+)|at least\s+(\d{1,2})", section, re.I)
    max_age = re.search(r"(?:under|younger than)\s+(\d{1,2})|(?:maximum age(?: of)?|no older than)\s*(\d{1,2})", section, re.I)
    if age_range: result.minimum_age, result.maximum_age = int(age_range.group(1)), int(age_range.group(2)); snippets.append(age_range.group(0))
    elif strict_min: result.minimum_age = int(strict_min.group(1)); result.minimum_age_exclusive = True; snippets.append(strict_min.group(0))
    elif inclusive_min: result.minimum_age = int(inclusive_min.group(1) or inclusive_min.group(2)); snippets.append(inclusive_min.group(0))
    if max_age: result.maximum_age = int(max_age.group(1) or max_age.group(2)); result.maximum_age_exclusive = bool(max_age.group(1)); snippets.append(max_age.group(0))
    for pattern in (r"(?:citizens?|nationals?)\s+(?:of|from)\s+([^.;\n]{3,100})", r"(?:must be|open to|eligible for)\s+(?:an?\s+)?(African|European|EU|EMEA|APAC|international)\s+(?:citizen|national|applicant|student|woman|women)"):
        match = re.search(pattern, section, re.I)
        if match:
            values = [item.strip() for item in re.split(r",|\bor\b|/", match.group(1)) if item.strip()]
            regions = {"african": "Africa", "european": "Europe", "asian": "APAC", "eu": "EU", "emea": "EMEA", "apac": "APAC", "international": "International"}
            if all(_normalize(value) in regions for value in values): result.regions_allowed = [regions[_normalize(value)] for value in values]
            else: result.nationalities_allowed = values
            snippets.append(match.group(0)); break
    if not result.nationalities_allowed and not result.regions_allowed:
        regional = re.search(r"\b(?:you are|applicants? must be)\s+(African|European|Asian)\b", section, re.I)
        if regional: result.regions_allowed = [{"african": "Africa", "european": "Europe", "asian": "APAC"}[regional.group(1).casefold()]]; snippets.append(regional.group(0))
    if not result.nationalities_allowed and not result.regions_allowed:
        international = re.search(r"\binternational students? (?:are )?eligible\b", section, re.I)
        if international: result.regions_allowed = ["International"]; snippets.append(international.group(0))
    residence = re.search(r"(?:resident|reside|living|based)\s+(?:in|of)\s+(?:an?\s+)?([^.;\n]{3,100})", section, re.I)
    if residence:
        raw = "Africa" if re.search(r"African countr", residence.group(1), re.I) else residence.group(1).strip()
        result.residence_requirements = [raw]; snippets.append(residence.group(0))
    gender = re.search(r"\b(?:you are (?:a )?)?(women|woman|female|girl)(?:\s+(?:only|are eligible|who))?\b", section, re.I)
    if gender: result.gender_requirements = ["woman"]; snippets.append(gender.group(0))
    student = re.search(r"\b(?:currently enrolled|enrolled (?:university|college)|current (?:undergraduate |graduate |university )?student|must be (?:a )?(?:(?:current|currently) )?(?:enrolled )?(?:undergraduate |graduate |university )?student)\b", section, re.I)
    if student: result.student_required = True; snippets.append(student.group(0))
    if re.search(r"\bundergraduate\b|\bbachelor'?s? student\b", normalized): result.undergraduate_eligible = True
    if re.search(r"\bgraduate student\b|\bpostgraduate\b|\bmaster'?s student\b|\bphd student\b", normalized): result.graduate_eligible = True
    graduation = re.search(r"graduat(?:e|ing|ion)[^\n.]{0,40}\b(20\d{2})(?:\s*(?:,|or|to|-)\s*(20\d{2}))?", section, re.I)
    if graduation:
        first, second = int(graduation.group(1)), int(graduation.group(2) or graduation.group(1)); result.graduation_years = list(range(first, second + 1)); snippets.append(graduation.group(0))
    experience = re.search(r"(?:at least|minimum(?: of)?)?\s*(\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten)\+?\s+years?(?: of)?[^.\n]{0,50}?experience", section, re.I)
    if experience:
        word_numbers = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
        raw = experience.group(1).casefold(); result.minimum_years_experience = float(word_numbers.get(raw, raw)); snippets.append(experience.group(0))
    language = re.search(r"(?:(?:must )?(?:speak|be fluent in)|proficient in|fluency in)\s+(English|French|Spanish|Arabic|Portuguese)", section, re.I)
    if language: result.language_requirements = [language.group(1)]; snippets.append(language.group(0))
    skills = re.search(r"(?:required skills?|skills required|experience (?:with|in)|interest in)\s*[:\-]?\s*([^\n.]{3,160})", section, re.I)
    if skills: result.required_skills = [item.strip() for item in re.split(r",|;|\band\b", skills.group(1)) if item.strip()]; snippets.append(skills.group(0))
    founder = re.search(r"(?:must be|open to)\s+(?:a )?(?:startup )?founder", section, re.I)
    if founder: result.founder_required = True; snippets.append(founder.group(0))
    return result, list(dict.fromkeys(snippets))


def _benefits(text: str) -> tuple[list[str], list[tuple[str, str]]]:
    patterns = (("Fully funded training", r"\bfully funded(?:\s+(?:training|programme|program|course|fellowship))?\b"), ("Certification included", r"\b(?:certification|certificate)[^.\n]{0,45}\b(?:included|provided|covered)\b|\bincludes?\s+(?:an?\s+)?certification\b|\b(?:sans|industry-recognised|industry-recognized)[^.\n]{0,30}certification\b"), ("Training included", r"\btraining\s+(?:is\s+)?(?:included|provided)\b|\b(?:sans|professional) training\b"), ("Mentorship", r"\bmentorship\b"), ("Professional development", r"\bprofessional development\b"), ("Paid placement", r"\bpaid (?:placement|internship)\b"), ("Travel support", r"\btravel (?:support|grant|allowance)\b"))
    found: list[str] = []; evidence: list[tuple[str, str]] = []
    for label, pattern in patterns:
        sentence = next((item for item in _sentences(text) if re.search(pattern, item, re.I)), None)
        if sentence: found.append(label); evidence.append((label, sentence))
    return found, evidence


class DeterministicOpportunityExtractor:
    """Conservative, page-local factual extraction without semantic inference."""

    def extract(self, page: FetchedPage) -> Opportunity:
        soup = BeautifulSoup(page.raw_html, "html.parser"); text = page.cleaned_text.strip(); sections = _extract_sections(page.raw_html); records = _json_ld(soup)
        meta = soup.find("meta", property="og:title"); h1 = soup.find("h1")
        title = " ".join(str((meta.get("content") if meta else None) or (h1.get_text(" ", strip=True) if h1 else None) or page.page_title or "Unknown opportunity").split())
        organization = _organization(records, soup, text); category = _category(title, soup)
        application_url = application_snippet = None
        for anchor in soup.find_all("a", href=True):
            label = _normalize(anchor.get_text(" ", strip=True))
            if label in {"apply", "apply now", "application", "application form", "submit application", "start application"} or "apply now" in label:
                application_url = urljoin(page.final_url, str(anchor["href"])); application_snippet = anchor.get_text(" ", strip=True) or application_url; break

        eligibility_text = _section_text(sections, "eligibility"); eligibility, eligibility_snippets = _eligibility(eligibility_text, sections)
        units = _structural_units(soup, text)
        deadline_date, deadline_snippet = _labeled_date(units, DEADLINE_LABELS, DEADLINE_EXCLUSIONS)
        opening_date, opening_snippet = _labeled_date(units, OPENING_LABELS)
        window_opening, window_deadline, window_snippet = _application_window(units)
        if opening_date is None and window_opening is not None: opening_date, opening_snippet = window_opening, window_snippet
        if deadline_date is None and window_deadline is not None: deadline_date, deadline_snippet = window_deadline, window_snippet
        start_date, start_snippet = _labeled_date(units, START_LABELS); end_date, end_snippet = _labeled_date(units, END_LABELS)
        if start_date is None and end_date is None:
            start_date, end_date, range_snippet = _labeled_range(units)
            start_snippet = end_snippet = range_snippet
        deadline = datetime.combine(deadline_date, time(23, 59, 59)) if deadline_date else None

        funding_text = _section_text(sections, "funding") or text
        flights, flights_snippet = _coverage(funding_text, ("flight", "airfare", "air travel")); accommodation, accommodation_snippet = _coverage(funding_text, ("accommodation", "hotel", "lodging", "housing")); visa_support, visa_snippet = _coverage(funding_text, ("visa support", "visa assistance", "visa process")); visa_fees, visa_fees_snippet = _coverage(funding_text, ("visa fee", "visa cost"))
        if visa_support.status is CoverageStatus.UNKNOWN:
            visa_support, visa_snippet = _coverage(text, ("visa support", "visa assistance", "visa process"))
        meals, _ = _coverage(funding_text, ("meals", "per diem", "daily allowance")); registration, _ = _coverage(funding_text, ("registration", "conference pass", "event fee"))
        salary, salary_snippet = _money(funding_text, "salary"); stipend, stipend_snippet = _money(funding_text, "stipend"); grant, grant_snippet = _money(funding_text, "grant"); prize, prize_snippet = _money(funding_text, "prize(?: money)?")
        benefits, benefit_evidence = _benefits(funding_text); paid = True if salary or stipend or "Paid placement" in benefits else None
        funding = FundingDetails(paid=paid, salary=salary, stipend=stipend, grant=grant, prize_money=prize, flights=flights, accommodation=accommodation, visa_support=visa_support, visa_fees=visa_fees, meals=meals, registration=registration, other_benefits=benefits)

        application_text = _section_text(sections, "application") or ""
        fee_snippet = next((s for s in _sentences(application_text) if any(p in _normalize(s) for p in ("application fee", "no fee", "free to apply"))), None); fee = None if not fee_snippet else not any(term in _normalize(fee_snippet) for term in ("no application fee", "no fee", "free to apply")); amount = re.search(r"(?:USD|EUR|GBP|MUR|NGN|\$|€|£)\s?[\d,]+(?:\.\d+)?", fee_snippet or "")
        application_scope = application_text or text
        application = ApplicationRequirements(
            application_fee=fee,
            fee_amount=amount.group(0) if amount else None,
            cv_required=True if re.search(r"(?:submit|attach|upload)[^\n.]{0,30}\b(?:cv|résumé|resume)\b|\bresume/cv\s*\*", application_scope, re.I) else None,
            cover_letter_required=True if re.search(r"(?:submit|attach|upload)[^\n.]{0,30}\bcover letter\b", application_scope, re.I) else None,
            transcript_required=True if re.search(r"(?:submit|attach|upload)[^\n.]{0,30}\btranscript\b|\btranscript\s*\*", application_scope, re.I) else None,
            portfolio_required=True if re.search(r"(?:submit|attach|upload)[^\n.]{0,30}\bportfolio\b", application_scope, re.I) else None,
        )
        location_match = re.search(r"\bLocation\s*:\s*([^\n]{2,100})", text, re.I); parts = [part.strip() for part in location_match.group(1).split(",") if part.strip()] if location_match else []
        country, city = (parts[-1] if parts else None), (parts[0] if len(parts) > 1 else None)
        mode = ParticipationMode.REMOTE if re.search(r"\bfully remote\b|\bremote opportunity\b", text, re.I) else ParticipationMode.HYBRID if re.search(r"\bhybrid\b", text, re.I) else ParticipationMode.IN_PERSON if re.search(r"\bin-person\b|\bin person\b|\bon-?site\b", text, re.I) else ParticipationMode.UNKNOWN

        title_specific = bool(re.search(r"\b(?:20\d{2}|fellowship|fellows|internship|programme|program|grant|ctf|hackathon|challenge|accelerator|scholarship)\b", _normalize(title)))
        specificity = (title_specific, application_url is not None, eligibility_text is not None, any((deadline, opening_date, start_date, end_date)), any((paid, salary, stipend, grant, prize, benefits)), category != "Unknown")
        if sum(specificity) < 2: raise NotOpportunityPageError()
        evidence: list[SourceEvidence] = []
        for field, value, snippet in (("deadline", deadline.isoformat() if deadline else None, deadline_snippet), ("opening_date", opening_date, opening_snippet), ("program_start_date", start_date, start_snippet), ("program_end_date", end_date, end_snippet), ("funding.salary", salary, salary_snippet), ("funding.stipend", stipend, stipend_snippet), ("funding.grant", grant, grant_snippet), ("funding.prize_money", prize, prize_snippet)):
            if value is not None and snippet: evidence.append(_evidence(field, value, snippet, page))
        hard_known = any((eligibility.nationalities_allowed, eligibility.regions_allowed, eligibility.residence_requirements, eligibility.minimum_age is not None, eligibility.student_required is not None, eligibility.minimum_years_experience is not None, eligibility.gender_requirements, eligibility.language_requirements))
        if hard_known:
            evidence.extend(_evidence("eligibility", "structured requirement", snippet, page) for snippet in eligibility_snippets)
        for field, component, snippet in (("funding.flights", flights, flights_snippet), ("funding.accommodation", accommodation, accommodation_snippet), ("funding.visa_support", visa_support, visa_snippet), ("funding.visa_fees", visa_fees, visa_fees_snippet)):
            if component.status is not CoverageStatus.UNKNOWN and snippet: evidence.append(_evidence(field, component.status.value, snippet, page))
        for label, snippet in benefit_evidence: evidence.append(_evidence("funding.other_benefits", label, snippet, page))
        if fee is not None and fee_snippet: evidence.append(_evidence("application.application_fee", fee, fee_snippet, page))
        if application_url and application_snippet: evidence.append(_evidence("application_url", application_url, application_snippet, page))
        rolling = bool(re.search(r"\b(?:rolling applications?|applications? (?:are )?rolling|no (?:application )?deadline|apply anytime|cohorts? run throughout the year|year-round applications?)\b", text, re.I))
        fields = {"deadline": deadline or rolling, "eligibility": eligibility.raw_text, "funding": any((paid, salary, stipend, grant, prize, benefits, flights.status is not CoverageStatus.UNKNOWN, accommodation.status is not CoverageStatus.UNKNOWN, visa_support.status is not CoverageStatus.UNKNOWN)), "application_requirements": any(value is not None for value in (application.application_fee, application.cv_required, application.cover_letter_required, application.transcript_required)), "application_url": application_url}
        diagnostics = ExtractionDiagnostics(material_fields_found=[name for name, value in fields.items() if value], material_fields_unknown=[name for name, value in fields.items() if not value], primary_source_url=page.final_url, warnings=["No bounded eligibility section found."] if not eligibility_text else [])
        accepting = bool(application_url and re.search(r"\bapply now\b|\bapplications? (?:are )?open\b", text, re.I))
        return Opportunity(title=title, organization=organization, category=category, source_url=page.requested_url, official_url=page.final_url, application_url=application_url, status=derive_status(deadline=deadline, as_of=page.fetched_at, opening_date=opening_date, rolling_application=rolling, confirmed_accepting=accepting), country=country, city=city, participation_mode=mode, opening_date=opening_date, deadline=deadline, rolling_application=rolling, program_start_date=start_date, program_end_date=end_date, eligibility=eligibility, funding=funding, application=application, evidence=evidence, discovered_at=page.fetched_at, last_verified_at=page.fetched_at, extraction_confidence=ConfidenceLevel.MEDIUM, semantic_input_character_count=0, semantic_input_limit=None, extraction_diagnostics=diagnostics)
