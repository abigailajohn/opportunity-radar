from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from opportunity_radar.deduplication import canonical_url
from opportunity_radar.fetching import FetchedPage


class PageShape(StrEnum):
    SPECIFIC_OPPORTUNITY = "specific_opportunity"
    MULTI_OPPORTUNITY_LISTING = "multi_opportunity_listing"
    GENERIC_ADVICE_OR_ARTICLE = "generic_advice_or_article"
    RECURRING_PROGRAMME_LANDING = "recurring_programme_landing"
    JOB_BOARD_OR_CAREERS_LANDING = "job_board_or_careers_landing"
    ORGANIZATION_PAGE = "organization_page"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class PageShapeClassification:
    shape: PageShape
    signals: tuple[str, ...] = ()
    child_opportunity_urls: tuple[str, ...] = ()


OPPORTUNITY_PATTERN = re.compile(
    r"\b(?:fellowships?|interns?|internships?|scholarships?|grants?|hackathons?|competitions?|"
    r"accelerators?|programmes?|programs?|bootcamps?|studentships?|jobs?|roles?)\b", re.I,
)
ADVICE_PATTERN = re.compile(
    r"\b(?:how to|guide to|your (?:20\d{2} )?guide|explainer|career advice|tips for|"
    r"what is|everything you need to know|educational resources?)\b", re.I,
)
CURRENT_ACTION_PATTERN = re.compile(
    r"\b(?:apply now|call for applications?|applications? (?:are )?open|now accepting applications?|"
    r"applications? close|application deadline|apply by|submit by|current openings?)\b", re.I,
)


def _json_ld_types(soup: BeautifulSoup) -> set[str]:
    types: set[str] = set()
    for node in soup.find_all("script", type="application/ld+json"):
        try:
            payload = json.loads(node.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = payload if isinstance(payload, list) else [payload]
        while stack:
            item = stack.pop()
            if not isinstance(item, dict):
                continue
            value = item.get("@type")
            if isinstance(value, str):
                types.add(value.casefold())
            elif isinstance(value, list):
                types.update(str(entry).casefold() for entry in value)
            graph = item.get("@graph")
            if isinstance(graph, list):
                stack.extend(graph)
    return types


def _explicit_child_links(soup: BeautifulSoup, base_url: str) -> tuple[str, ...]:
    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        label = " ".join(anchor.get_text(" ", strip=True).split())
        href = urljoin(base_url, str(anchor["href"]))
        if urlsplit(href).scheme not in {"http", "https"}:
            continue
        context = " ".join((anchor.find_parent(["article", "li", "section", "div"]) or anchor).get_text(" ", strip=True).split())[:400]
        if OPPORTUNITY_PATTERN.search(f"{label} {context}") and re.search(r"\b(?:apply|details|learn more|view|fellowship|internship|scholarship|grant)\b", label, re.I):
            links.append(canonical_url(href))
    return tuple(dict.fromkeys(links))


def classify_page_shape(page: FetchedPage) -> PageShapeClassification:
    soup = BeautifulSoup(page.raw_html, "html.parser")
    heading = soup.find("h1")
    title = " ".join(((heading.get_text(" ", strip=True) if heading else None) or page.page_title or (soup.title.get_text(" ", strip=True) if soup.title else "")).split())
    path = urlsplit(page.final_url).path.casefold()
    text = " ".join(page.cleaned_text.split())
    types = _json_ld_types(soup)
    signals: list[str] = []

    if "jobposting" in types:
        return PageShapeClassification(PageShape.SPECIFIC_OPPORTUNITY, ("JobPosting structured metadata",))

    child_links = _explicit_child_links(soup, page.final_url)
    item_list = "itemlist" in types
    opportunity_headings = {
        " ".join(node.get_text(" ", strip=True).split()).casefold()
        for node in soup.find_all(["h2", "h3", "h4"])
        if OPPORTUNITY_PATTERN.search(node.get_text(" ", strip=True))
    }
    repeated_cards = sum(
        1 for node in soup.find_all(["article", "li"])
        if OPPORTUNITY_PATTERN.search(node.get_text(" ", strip=True))
        and re.search(r"\b(?:deadline|amount|award|apply|eligibility)\b", node.get_text(" ", strip=True), re.I)
    )
    plural_listing_title = bool(re.search(r"\b(?:scholarships|fellowships|internships|grants|opportunities|jobs)\b", title, re.I))
    if item_list or (plural_listing_title and (len(opportunity_headings) >= 3 or repeated_cards >= 3 or len(child_links) >= 3)):
        signals.extend(filter(None, ("ItemList structured metadata" if item_list else None, "multiple opportunity blocks", "plural listing title")))
        return PageShapeClassification(PageShape.MULTI_OPPORTUNITY_LISTING, tuple(signals), child_links)

    advice_title = bool(ADVICE_PATTERN.search(title))
    article_path = bool(re.search(r"/(?:articles?|blog|guides?|resources?)/", path))
    article_type = bool(types & {"article", "newsarticle", "blogposting"})
    if advice_title or (article_path and ADVICE_PATTERN.search(text[:1000])):
        signals.extend(filter(None, ("advice/explainer title" if advice_title else None, "article/resource URL" if article_path else None, "Article structured metadata" if article_type else None)))
        return PageShapeClassification(PageShape.GENERIC_ADVICE_OR_ARTICLE, tuple(signals))

    current_action = bool(CURRENT_ACTION_PATTERN.search(text))
    application_links = [
        node for node in soup.find_all("a", href=True)
        if re.search(r"\b(?:apply|application|view role|job details|view (?:current )?(?:openings|opportunities|jobs))\b", node.get_text(" ", strip=True), re.I)
    ]
    current_cycle = bool(re.search(r"\b20\d{2}(?:/\d{2,4})?\b", f"{title} {text[:4000]}") and (current_action or application_links))
    if re.search(r"\b(?:careers?|jobs?|open positions?|vacancies)\b", title, re.I) and not current_action and not current_cycle:
        return PageShapeClassification(PageShape.JOB_BOARD_OR_CAREERS_LANDING, ("generic careers/jobs title",))

    plural_programme = bool(re.search(r"\b(?:internships|fellowships|programmes|programs)\b", title, re.I))
    if plural_programme and not current_action and not current_cycle and not application_links:
        return PageShapeClassification(PageShape.RECURRING_PROGRAMME_LANDING, ("programme-family title without current-cycle evidence",), child_links)

    title_specific = bool(OPPORTUNITY_PATTERN.search(title))
    factual_sections = sum(bool(re.search(rf"\b{label}\b", text, re.I)) for label in ("eligibility", "deadline", "requirements", "benefits", "funding"))
    if title_specific and (current_action or application_links or current_cycle or factual_sections >= 2):
        signals.append("specific opportunity title with actionable/factual evidence")
        return PageShapeClassification(PageShape.SPECIFIC_OPPORTUNITY, tuple(signals), child_links)

    if re.search(r"\b(?:about us|our mission|who we are)\b", text[:2500], re.I) and not title_specific:
        return PageShapeClassification(PageShape.ORGANIZATION_PAGE, ("organization-level content",))
    return PageShapeClassification(PageShape.UNCERTAIN, ("page shape not deterministically resolved",), child_links)
