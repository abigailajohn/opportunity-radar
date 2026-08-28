from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag
from pydantic import HttpUrl, TypeAdapter

from opportunity_radar.deduplication import canonical_url
from opportunity_radar.discovery_models import (
    CandidateClassification, DiscoveryCandidate, DiscoveryFailure,
    DiscoveryFailureStage, SourceConfiguration, SourceType, TrustedSource,
)
from opportunity_radar.fetching import FetchedPage
from opportunity_radar.providers import PageFetcher


OPPORTUNITY_TERMS = {
    "fellowship", "scholarship", "internship", "grant", "accelerator", "hackathon",
    "competition", "challenge", "programme", "program", "studentship", "call for papers",
}
HUB_TERMS = {"opportunities", "programmes", "programs", "careers", "jobs", "fellowships", "scholarships", "events"}
NEGATIVE_TERMS = {
    "privacy": CandidateClassification.NON_OPPORTUNITY,
    "terms": CandidateClassification.NON_OPPORTUNITY,
    "login": CandidateClassification.NAVIGATION,
    "sign in": CandidateClassification.NAVIGATION,
    "facebook": CandidateClassification.NAVIGATION,
    "linkedin": CandidateClassification.NAVIGATION,
    "instagram": CandidateClassification.NAVIGATION,
    "about": CandidateClassification.ORGANIZATION_PAGE,
    "contact": CandidateClassification.ORGANIZATION_PAGE,
    "team": CandidateClassification.ORGANIZATION_PAGE,
    "blog": CandidateClassification.NAVIGATION,
    "news": CandidateClassification.NAVIGATION,
}
HTTP_URL_ADAPTER = TypeAdapter(HttpUrl)


@dataclass
class DiscoveryResult:
    candidates: list[DiscoveryCandidate] = field(default_factory=list)
    failures: list[DiscoveryFailure] = field(default_factory=list)
    pages: dict[str, FetchedPage] = field(default_factory=dict)
    sources_fetched: int = 0

    @property
    def specific_urls(self) -> list[str]:
        return list(dict.fromkeys(canonical_url(item.url) for item in self.candidates if item.classification is CandidateClassification.SPECIFIC_OPPORTUNITY))


def _norm(value: str) -> str:
    return " ".join("".join(character if character.isalnum() else " " for character in value.casefold()).split())


def _same_domain(left: str, right: str) -> bool:
    a, b = urlsplit(left).hostname or "", urlsplit(right).hostname or ""
    a, b = a.casefold(), b.casefold()
    return a == b or a.endswith(f".{b}") or b.endswith(f".{a}")


def _canonical_from_page(page: FetchedPage) -> str:
    soup = BeautifulSoup(page.raw_html, "html.parser")
    link = soup.find("link", rel=lambda value: value and "canonical" in value)
    if link and link.get("href"):
        candidate = urljoin(page.final_url, str(link["href"]))
        if urlsplit(candidate).scheme in {"http", "https"}:
            return canonical_url(candidate)
    return canonical_url(page.final_url)


def _nearby_context(anchor: Tag) -> str:
    heading = anchor.find_previous(["h1", "h2", "h3", "h4"])
    parent = anchor.find_parent(["li", "article", "section", "div", "p"])
    values = []
    if heading: values.append(heading.get_text(" ", strip=True))
    if parent: values.append(parent.get_text(" ", strip=True))
    return " ".join(" ".join(values).split())[:500]


def score_link(url: str, anchor_text: str, context: str, source: TrustedSource) -> tuple[int, list[str], CandidateClassification | None]:
    anchor, nearby, path = _norm(anchor_text), _norm(context), _norm(urlsplit(url).path)
    score = 0; signals: list[str] = []
    explicit = [term for term in OPPORTUNITY_TERMS if term in anchor]
    if explicit: score += 3; signals.append(f"anchor opportunity phrase: {sorted(explicit)[0]}")
    contextual = [term for term in OPPORTUNITY_TERMS if term in nearby]
    if contextual: score += 2; signals.append(f"nearby opportunity context: {sorted(contextual)[0]}")
    if any(term in path for term in OPPORTUNITY_TERMS | {"apply", "application", "career", "job", "event"}): score += 2; signals.append("opportunity-like URL path")
    if source.category_focus and any(_norm(focus) in f"{anchor} {nearby} {path}" for focus in source.category_focus): score += 1; signals.append("source category-focus match")
    if re.search(r"\b20\d{2}\b|\bcycle\b|\bcohort\b", f"{anchor} {nearby} {path}"): score += 1; signals.append("date/year/cycle signal")
    if re.search(r"\bapply\b|\bapplication\b", f"{anchor} {nearby} {path}"): score += 2; signals.append("apply/application signal")
    if _same_domain(url, str(source.url)): score += 1; signals.append("same-domain signal")
    negative = next((term for term in NEGATIVE_TERMS if term in anchor or re.search(rf"(?:^|/){re.escape(term)}(?:/|$)", path)), None)
    if negative:
        score -= 5; signals.append(f"negative navigation signal: {negative}")
        return score, signals, NEGATIVE_TERMS[negative]
    if anchor in {"home", "read more", "learn more", "more"}: score -= 2; signals.append("generic navigation anchor")
    return score, signals, None


def _page_classification(page: FetchedPage, candidate: DiscoveryCandidate) -> CandidateClassification:
    soup = BeautifulSoup(page.raw_html, "html.parser")
    title = " ".join(((soup.find("h1") or soup.title).get_text(" ", strip=True) if (soup.find("h1") or soup.title) else "").split())
    strong = _norm(" ".join([title, *[node.get_text(" ", strip=True) for node in soup.find_all(["h2", "h3"])]]))
    links = [node.get_text(" ", strip=True) for node in soup.find_all("a", href=True)]
    opportunity_links = sum(any(term in _norm(label) for term in OPPORTUNITY_TERMS) for label in links)
    normalized_title = _norm(title)
    has_hub_title = any(f" {term} " in f" {normalized_title} " for term in HUB_TERMS)
    has_specific_title = any(term in normalized_title for term in OPPORTUNITY_TERMS) and not has_hub_title
    has_application = any(re.search(r"\bapply\b|\bapplication\b", _norm(label)) for label in links)
    has_fact_section = any(term in strong for term in ("eligibility", "deadline", "timeline", "benefits", "requirements", "how to apply"))
    if has_specific_title and candidate.discovery_score >= 6: return CandidateClassification.SPECIFIC_OPPORTUNITY
    if opportunity_links >= 2 and not (has_specific_title and has_fact_section): return CandidateClassification.OPPORTUNITY_HUB
    if has_specific_title and (has_application or has_fact_section): return CandidateClassification.SPECIFIC_OPPORTUNITY
    if candidate.discovery_score >= 6 and (has_application or has_fact_section): return CandidateClassification.SPECIFIC_OPPORTUNITY
    if opportunity_links >= 2: return CandidateClassification.OPPORTUNITY_HUB
    if candidate.discovery_score >= 3: return CandidateClassification.UNCERTAIN
    return CandidateClassification.NON_OPPORTUNITY


def _link_candidates(page: FetchedPage, source: TrustedSource, *, depth: int, now: datetime) -> list[DiscoveryCandidate]:
    soup = BeautifulSoup(page.raw_html, "html.parser")
    candidates: list[DiscoveryCandidate] = []
    for anchor in soup.find_all("a", href=True):
        raw = urljoin(page.final_url, str(anchor["href"]))
        if urlsplit(raw).scheme not in {"http", "https"}: continue
        url = canonical_url(raw); label = " ".join(anchor.get_text(" ", strip=True).split()); context = _nearby_context(anchor)
        score, signals, forced = score_link(url, label, context, source)
        external_allowed = _same_domain(url, str(source.url)) or bool(re.search(r"\bapply\b|\bapplication\b|\bofficial\b", _norm(f"{label} {context}")))
        if not external_allowed: forced = CandidateClassification.NON_OPPORTUNITY; signals.append("external link not explicitly application/official")
        classification = forced or (CandidateClassification.SPECIFIC_OPPORTUNITY if score >= 6 else CandidateClassification.UNCERTAIN if score >= 3 else CandidateClassification.NON_OPPORTUNITY)
        candidates.append(DiscoveryCandidate(url=url, source_id=source.id, discovered_from_url=page.final_url, depth=depth, anchor_text=label, nearby_context=context, discovery_score=score, discovery_signals=signals, classification=classification, discovered_at=now))
    return candidates


def discover(configuration: SourceConfiguration, fetcher: PageFetcher, *, now: datetime | None = None) -> DiscoveryResult:
    result = DiscoveryResult(); current = now or datetime.now(timezone.utc); seen: set[str] = set(); fetched_pages = 0
    for source in configuration.sources:
        if not source.enabled: continue
        try:
            source_page = fetcher.fetch(str(source.url)); result.sources_fetched += 1
            result.pages[canonical_url(source.url)] = source_page; result.pages[_canonical_from_page(source_page)] = source_page
        except Exception as exc:
            result.failures.append(DiscoveryFailure(stage=DiscoveryFailureStage.SOURCE_FETCH, source_id=source.id, url=str(source.url), reason=str(exc))); continue
        if source.source_type is SourceType.RECURRING_PROGRAMME:
            candidate = DiscoveryCandidate(url=_canonical_from_page(source_page), source_id=source.id, discovered_from_url=source.url, depth=1, anchor_text=source.name, nearby_context=source.notes or "Configured recurring programme", discovery_score=6, discovery_signals=["configured recurring programme"], classification=CandidateClassification.SPECIFIC_OPPORTUNITY, discovered_at=current)
            result.candidates.append(candidate); seen.add(canonical_url(candidate.url)); continue
        frontier = _link_candidates(source_page, source, depth=1, now=current)
        if not frontier:
            result.failures.append(DiscoveryFailure(stage=DiscoveryFailureStage.NO_CANDIDATES, source_id=source.id, url=str(source.url), reason="source contained no HTTP candidate links")); continue
        per_source = 0
        for candidate in frontier:
            key = canonical_url(candidate.url)
            if key in seen: continue
            seen.add(key); result.candidates.append(candidate)
            if candidate.discovery_score < 3 or candidate.classification in {CandidateClassification.NAVIGATION, CandidateClassification.NON_OPPORTUNITY, CandidateClassification.ORGANIZATION_PAGE}: continue
            if per_source >= source.max_links_per_run or fetched_pages >= configuration.global_max_candidate_pages: continue
            try:
                page = fetcher.fetch(key); fetched_pages += 1; per_source += 1
                canonical = _canonical_from_page(page); result.pages[key] = page; result.pages[canonical] = page
                candidate.url = HTTP_URL_ADAPTER.validate_python(canonical); candidate.classification = _page_classification(page, candidate)
            except Exception as exc:
                result.failures.append(DiscoveryFailure(stage=DiscoveryFailureStage.CANDIDATE_FETCH, source_id=source.id, url=key, reason=str(exc))); continue
            if candidate.classification is not CandidateClassification.OPPORTUNITY_HUB or candidate.depth >= 2: continue
            for child in _link_candidates(page, source, depth=2, now=current):
                child_key = canonical_url(child.url)
                if child_key in seen: continue
                seen.add(child_key); result.candidates.append(child)
                if child.discovery_score < 3 or child.classification in {CandidateClassification.NAVIGATION, CandidateClassification.NON_OPPORTUNITY, CandidateClassification.ORGANIZATION_PAGE}: continue
                if per_source >= source.max_links_per_run or fetched_pages >= configuration.global_max_candidate_pages: continue
                try:
                    child_page = fetcher.fetch(child_key); fetched_pages += 1; per_source += 1
                    canonical = _canonical_from_page(child_page); result.pages[child_key] = child_page; result.pages[canonical] = child_page
                    child.url = HTTP_URL_ADAPTER.validate_python(canonical); child.classification = _page_classification(child_page, child)
                except Exception as exc:
                    result.failures.append(DiscoveryFailure(stage=DiscoveryFailureStage.CANDIDATE_FETCH, source_id=source.id, url=child_key, reason=str(exc)))
    return result
