from __future__ import annotations

import re
import json
from collections.abc import Iterable
from datetime import date, datetime, timezone
import os
from typing import Protocol
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import HttpUrl, TypeAdapter, ValidationError

from opportunity_radar.deduplication import canonical_url
from opportunity_radar.models import OpportunityProfile
from opportunity_radar.search_models import (
    SearchCandidate, SearchConfiguration, SearchProvenance, SearchQuery, SearchQueryMode, SearchResult,
)


class SearchProvider(Protocol):
    name: str

    def search(self, query: str, *, limit: int) -> list[SearchResult]: ...


class FakeSearchProvider:
    """Offline deterministic provider used by tests and local replay tools."""

    def __init__(self, results: dict[str, list[SearchResult]], *, name: str = "fake") -> None:
        self.name = name
        self.results = results
        self.queries: list[tuple[str, int]] = []

    def search(self, query: str, *, limit: int) -> list[SearchResult]:
        self.queries.append((query, limit))
        return [item.model_copy(update={"query": query, "provider": self.name}) for item in self.results.get(query, [])[:limit]]


class UnavailableSearchProvider:
    """Explicit production boundary until a reliable credentialed provider is configured."""

    name = "unavailable"

    def search(self, query: str, *, limit: int) -> list[SearchResult]:
        del query, limit
        raise RuntimeError(
            "no reliable no-credential web search provider is configured; "
            "supply a SearchProvider implementation rather than scraping a search engine"
        )


class TavilySearchProvider:
    """Production search provider using Tavily's search-only HTTP endpoint."""

    name = "tavily"
    endpoint = "https://api.tavily.com/search"

    def __init__(self, api_key: str, *, client: httpx.Client | None = None) -> None:
        if not api_key.strip():
            raise RuntimeError("TAVILY_API_KEY is required for the Tavily search provider")
        self.api_key = api_key
        self.client = client or httpx.Client(timeout=httpx.Timeout(20.0))

    @classmethod
    def from_environment(cls, *, client: httpx.Client | None = None) -> TavilySearchProvider:
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            raise RuntimeError("TAVILY_API_KEY is required for the Tavily search provider")
        return cls(api_key, client=client)

    def search(self, query: str, *, limit: int) -> list[SearchResult]:
        try:
            response = self.client.post(
                self.endpoint,
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "query": query,
                    "topic": "general",
                    "search_depth": "basic",
                    "max_results": limit,
                    "include_answer": False,
                    "include_raw_content": False,
                    "include_images": False,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"Tavily search failed: {exc}") from exc
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise RuntimeError("Tavily search failed: response did not contain a results list")
        normalized: list[SearchResult] = []
        for rank, item in enumerate(results[:limit], start=1):
            if not isinstance(item, dict) or not item.get("title") or not item.get("url"):
                continue
            try:
                normalized.append(SearchResult(
                    title=str(item["title"]), url=item["url"], snippet=str(item.get("content") or ""),
                    rank=rank, query=query, provider=self.name,
                ))
            except ValidationError:
                continue
        return normalized


class JsonReplaySearchProvider:
    """Deterministic file-backed provider for local experiments and reproducible runs."""

    name = "json-replay"

    def __init__(self, results: list[SearchResult]) -> None:
        self.results = results

    @classmethod
    def from_file(cls, path: str | Path) -> JsonReplaySearchProvider:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, list):
            raise ValueError("search replay file must contain a JSON list")
        return cls([SearchResult.model_validate(item) for item in payload])

    def search(self, query: str, *, limit: int) -> list[SearchResult]:
        matching = [item for item in self.results if item.query == query]
        return [item.model_copy(update={"provider": self.name}) for item in matching[:limit]]


_MATCH_FAMILIES = (
    ("internships", "{focus} internship {student}"),
    ("fellowships", "{focus} fellowship {international} funded"),
    ("research", "{focus} research programme {student}"),
    ("open_source", "{focus} open source mentorship programme"),
    ("competitions", "{focus} hackathon competition {international}"),
    ("grants", "{focus} grant scholarship {international}"),
    ("speaking", "{focus} conference call for papers travel grant"),
    ("student_programmes", "{focus} student ambassador programme"),
    ("technical_roles", "{focus} early career technical role remote"),
    ("scholarships", "{focus} scholarship programme {student}"),
    ("travel", "{focus} funded conference travel opportunity"),
    ("training", "{focus} funded training certification programme"),
)
_DISCOVERY_FAMILIES = (
    ("emerging", "emerging technology programme fellowship {international}"),
    ("founder", "technical founder accelerator startup grant {international}"),
    ("ai", "artificial intelligence security research challenge {student}"),
    ("cloud", "cloud DevSecOps training credits programme {international}"),
    ("global_learning", "fully funded technical conference scholarship {international}"),
    ("innovation", "student innovation challenge global programme"),
    ("deep_tech", "deep technology builder fellowship programme"),
    ("research_access", "international technical research access programme"),
    ("community", "developer community leadership ambassador programme"),
    ("entrepreneurship", "emerging technology entrepreneurship residency"),
)

_CORE_MATCH_FAMILY_NAMES = {"internships", "fellowships"}


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    if isinstance(value, dict):
        result: list[str] = []
        for nested in value.values():
            result.extend(_strings(nested))
        return result
    return []


def _rotated(items: tuple[tuple[str, str], ...], count: int, offset: int) -> list[tuple[str, str]]:
    if not items or count <= 0:
        return []
    return [items[(offset + index) % len(items)] for index in range(min(count, len(items)))]


def generate_search_queries(
    profile: OpportunityProfile, configuration: SearchConfiguration, *, rotation_date: date | None = None,
) -> list[SearchQuery]:
    """Generate bounded, stable queries from typed fields and optional profile extensions."""
    primary = profile.professional_identity.primary or profile.professional_identity.secondary
    focuses = list(dict.fromkeys(primary[:4] + profile.professional_identity.secondary[:2]))
    if not focuses:
        focuses = ["technology"]
    student = "undergraduate student" if profile.education.current_stage.year else "student"
    nationalities = " ".join(profile.identity.nationality).casefold()
    residence = " ".join(_strings(profile.identity.residence)).casefold()
    regional = "Africa global" if any(term in f"{nationalities} {residence}" for term in ("nigeria", "mauritius", "africa")) else "international global"
    values = {"student": student, "international": regional}

    max_match = max(1, round(configuration.max_queries_per_run * configuration.match_share))
    max_discovery = configuration.max_queries_per_run - max_match
    rotation_key = (rotation_date or date.today()).toordinal()
    core = tuple(item for item in _MATCH_FAMILIES if item[0] in _CORE_MATCH_FAMILY_NAMES)
    rotating_match = tuple(item for item in _MATCH_FAMILIES if item[0] not in _CORE_MATCH_FAMILY_NAMES)
    match_families = list(core[:max_match])
    match_families.extend(_rotated(rotating_match, max_match - len(match_families), rotation_key % len(rotating_match)))
    discovery_families = _rotated(_DISCOVERY_FAMILIES, max_discovery, rotation_key % len(_DISCOVERY_FAMILIES))
    queries: list[SearchQuery] = []
    for index, (family, template) in enumerate(match_families):
        focus = focuses[(rotation_key + index) % len(focuses)]
        text = " ".join(template.format(focus=focus, **values).split())
        queries.append(SearchQuery(text=text, mode=SearchQueryMode.MATCH, family=family))
    for family, template in discovery_families:
        text = " ".join(template.format(**values).split())
        queries.append(SearchQuery(text=text, mode=SearchQueryMode.DISCOVERY, family=family))
    return queries[: configuration.max_queries_per_run]


_OPPORTUNITY_TERMS = {
    "apply", "application", "fellowship", "internship", "scholarship", "grant",
    "programme", "program", "hackathon", "competition", "challenge", "accelerator",
    "call for papers", "cfp", "studentship", "conference",
}
_NOISE_HOSTS = {"facebook.com", "instagram.com", "linkedin.com", "x.com", "twitter.com", "youtube.com"}
_NOISE_PATHS = re.compile(r"(?:^|/)(?:privacy|terms|login|signin|contact|about|team)(?:/|$)", re.I)


def score_search_result(result: SearchResult) -> tuple[int, list[str]]:
    parts = urlsplit(str(result.url)); hostname = (parts.hostname or "").casefold()
    text = " ".join(f"{result.title} {result.snippet} {parts.path}".casefold().split())
    score = 0
    signals: list[str] = []
    terms = sorted(term for term in _OPPORTUNITY_TERMS if term in text)
    if terms:
        score += 3
        signals.append(f"opportunity phrase: {terms[0]}")
    if re.search(r"\bapply\b|\bapplication\b|\bdeadline\b", text):
        score += 2
        signals.append("application/deadline signal")
    if re.search(r"\b20\d{2}\b|\bcohort\b|\bcycle\b", text):
        score += 1
        signals.append("year/cycle signal")
    if result.rank <= 3:
        score += 1
        signals.append("high search rank")
    if hostname in _NOISE_HOSTS or any(hostname.endswith(f".{item}") for item in _NOISE_HOSTS):
        score -= 10
        signals.append("social/media host")
    if _NOISE_PATHS.search(parts.path):
        score -= 6
        signals.append("navigation/legal path")
    return score, signals


def collect_search_candidates(
    queries: Iterable[SearchQuery], provider: SearchProvider, configuration: SearchConfiguration,
    *, now: datetime | None = None,
) -> tuple[list[SearchCandidate], list[tuple[SearchQuery, Exception]]]:
    timestamp = now or datetime.now(timezone.utc)
    by_url: dict[str, SearchCandidate] = {}
    failures: list[tuple[SearchQuery, Exception]] = []
    adapter = TypeAdapter(HttpUrl)
    for query in list(queries)[: configuration.max_queries_per_run]:
        try:
            results = provider.search(query.text, limit=configuration.max_results_per_query)
        except Exception as exc:
            failures.append((query, exc))
            continue
        for result in results[: configuration.max_results_per_query]:
            try:
                normalized = canonical_url(result.url)
                url = adapter.validate_python(normalized)
            except (ValueError, ValidationError):
                continue
            score, signals = score_search_result(result)
            if score < 0:
                continue
            candidate = SearchCandidate(
                url=url, title=result.title.strip(), snippet=" ".join(result.snippet.split())[:1000],
                query=query.text, query_mode=query.mode, search_rank=result.rank,
                provider=result.provider or provider.name, discovered_at=timestamp,
                search_score=score, search_signals=signals,
                provenance=[SearchProvenance(query=query.text, query_mode=query.mode, provider=result.provider or provider.name, search_rank=result.rank)],
            )
            previous = by_url.get(normalized)
            if previous is not None:
                combined = previous.provenance + [item for item in candidate.provenance if item not in previous.provenance]
                candidate.provenance = combined
            if previous is None or (candidate.search_score, -candidate.search_rank) > (previous.search_score, -previous.search_rank):
                by_url[normalized] = candidate
            elif previous is not None:
                previous.provenance = candidate.provenance
            if len(by_url) >= configuration.global_candidate_cap:
                break
        if len(by_url) >= configuration.global_candidate_cap:
            break
    return list(by_url.values()), failures
