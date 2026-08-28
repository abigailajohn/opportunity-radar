from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

import httpx
import pytest

from opportunity_radar.deterministic_assessment import DeterministicSemanticAssessor
from opportunity_radar.deterministic_extraction import DeterministicOpportunityExtractor
from opportunity_radar.discovery_models import ChangeClassification, SourceType, TrustedSource
from opportunity_radar.fetching import FetchedPage
from opportunity_radar.html_preparation import prepare_html
from opportunity_radar.milestone3 import run_search_pipeline
from opportunity_radar.persistence import InMemoryOpportunityStore, POSTGRES_SCHEMA
from opportunity_radar.providers import FakePageFetcher
from opportunity_radar.search import (
    FakeSearchProvider, JsonReplaySearchProvider, TavilySearchProvider,
    collect_search_candidates, generate_search_queries,
)
from opportunity_radar.search_models import SearchConfiguration, SearchQueryMode, SearchResult


FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
URL = "https://trusted.example/security-fellowship-2027"


def page(url: str = URL) -> FetchedPage:
    html = (FIXTURES / "discovery_fellowship.html").read_text(encoding="utf-8")
    return FetchedPage(
        requested_url=url, final_url=url, raw_html=html,
        cleaned_text=prepare_html(html, base_url=url), fetched_at=NOW,
    )


def result(query: str, *, url: str = URL, rank: int = 1, title: str = "Security Fellowship 2027 — Apply") -> SearchResult:
    return SearchResult(title=title, url=url, snippet="Applications open for a funded security fellowship.", rank=rank, query=query, provider="fake")


def small_config(**updates) -> SearchConfiguration:
    values = {"max_queries_per_run": 2, "max_results_per_query": 5, "global_candidate_cap": 10, "fetch_cap": 10, "match_share": 0.5}
    values.update(updates)
    return SearchConfiguration(**values)


def test_query_generation_is_profile_based_bounded_and_separates_modes(profile) -> None:
    queries = generate_search_queries(profile, SearchConfiguration(max_queries_per_run=6, match_share=0.5), rotation_date=NOW.date())
    assert len(queries) == 6
    assert [item.mode for item in queries].count(SearchQueryMode.MATCH) == 3
    assert [item.mode for item in queries].count(SearchQueryMode.DISCOVERY) == 3
    profile_focuses = {item.casefold() for item in profile.professional_identity.primary + profile.professional_identity.secondary}
    assert any(any(focus in item.text.casefold() for focus in profile_focuses) for item in queries if item.mode is SearchQueryMode.MATCH)
    assert all(item.family for item in queries if item.mode is SearchQueryMode.DISCOVERY)


def test_default_budget_is_twelve_queries_and_query_families_rotate(profile) -> None:
    config = SearchConfiguration()
    first = generate_search_queries(profile, config, rotation_date=date(2026, 8, 28))
    repeat = generate_search_queries(profile, config, rotation_date=date(2026, 8, 28))
    next_day = generate_search_queries(profile, config, rotation_date=date(2026, 8, 29))
    assert len(first) == 12
    assert config.max_results_per_query == 10 and config.global_candidate_cap == 80 and config.fetch_cap == 30
    assert first == repeat
    assert {item.family for item in first if item.family in {"internships", "fellowships"}} == {"internships", "fellowships"}
    assert [item.family for item in first] != [item.family for item in next_day]


def test_search_results_are_canonicalized_and_deduplicated_across_queries(profile) -> None:
    config = small_config()
    queries = generate_search_queries(profile, config, rotation_date=NOW.date())
    provider = FakeSearchProvider({
        queries[0].text: [result(queries[0].text, url=URL + "?utm_source=one#apply")],
        queries[1].text: [result(queries[1].text, url=URL + "/?utm_campaign=two")],
    })
    candidates, failures = collect_search_candidates(queries, provider, config, now=NOW)
    assert not failures and len(candidates) == 1
    assert str(candidates[0].url) == URL
    assert len(candidates[0].provenance) == 2


def test_candidate_filter_removes_social_and_navigation_noise(profile) -> None:
    config = small_config(max_queries_per_run=1)
    query = generate_search_queries(profile, config, rotation_date=NOW.date())[0]
    provider = FakeSearchProvider({query.text: [
        result(query.text, url="https://facebook.com/example", title="Fellowship"),
        result(query.text, url="https://trusted.example/privacy", title="Privacy policy"),
        result(query.text),
    ]})
    candidates, _ = collect_search_candidates([query], provider, config, now=NOW)
    assert [str(item.url) for item in candidates] == [URL]


def test_query_result_and_global_candidate_limits_are_enforced(profile) -> None:
    config = small_config(max_queries_per_run=1, max_results_per_query=2, global_candidate_cap=1)
    query = generate_search_queries(profile, config, rotation_date=NOW.date())[0]
    provider = FakeSearchProvider({query.text: [
        result(query.text, url="https://trusted.example/one"),
        result(query.text, url="https://trusted.example/two", rank=2),
    ]})
    candidates, _ = collect_search_candidates([query], provider, config, now=NOW)
    assert len(provider.queries) == 1 and provider.queries[0][1] == 2
    assert len(candidates) == 1


def test_full_search_pipeline_first_run_new_second_unchanged_and_not_resurfaced(profile, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = small_config()
    queries = generate_search_queries(profile, config, rotation_date=NOW.date())
    results = {queries[0].text: [result(queries[0].text)]}
    store = InMemoryOpportunityStore()
    arguments = (profile, FakeSearchProvider(results), FakePageFetcher({URL: page()}), DeterministicOpportunityExtractor(), DeterministicSemanticAssessor(), store)
    first = run_search_pipeline(*arguments, configuration=config, as_of=date(2026, 8, 28), now=NOW)
    second = run_search_pipeline(profile, FakeSearchProvider(results), FakePageFetcher({URL: page()}), DeterministicOpportunityExtractor(), DeterministicSemanticAssessor(), store, configuration=config, as_of=date(2026, 8, 28), now=NOW)
    assert first.counts["new_opportunities"] == 1
    assert second.counts["new_opportunities"] == 0
    assert second.counts["unchanged_opportunities"] == 1
    assert "## New Opportunities\n\nNone." in second.digest
    assert "Security Fellowship" not in second.digest
    assert store.search_provenance
    assert all(item.page_shape is not None for item in store.search_candidates)
    assert all(item.page_shape_signals for item in store.search_candidates)


def test_search_rediscovery_preserves_original_source_and_merges_provenance(profile) -> None:
    config = small_config()
    query = generate_search_queries(profile, config, rotation_date=NOW.date())[0]
    opportunity = DeterministicOpportunityExtractor().extract(page())
    original = TrustedSource(id="known-source", name="Known", url="https://trusted.example/", source_type=SourceType.OPPORTUNITY_HUB)
    store = InMemoryOpportunityStore()
    store.persist_sources([original], checked_at=NOW)
    store.persist_opportunity(opportunity, source_id=original.id, seen_at=NOW)
    run = run_search_pipeline(profile, FakeSearchProvider({query.text: [result(query.text)]}), FakePageFetcher({URL: page()}), DeterministicOpportunityExtractor(), DeterministicSemanticAssessor(), store, configuration=config, as_of=date(2026, 8, 28), now=NOW)
    assert run.persistence[0].classification is ChangeClassification.KNOWN_UNCHANGED
    record = next(iter(store.opportunities.values()))
    assert record["source_id"] == "known-source"
    assert store.search_provenance[0]["key"][2] == query.text


def test_one_search_or_fetch_failure_does_not_abort_other_candidates(profile) -> None:
    config = small_config()
    queries = generate_search_queries(profile, config, rotation_date=NOW.date())

    class PartialProvider(FakeSearchProvider):
        def search(self, query: str, *, limit: int):
            if query == queries[1].text:
                raise TimeoutError("search timed out")
            return [result(query), result(query, url="https://trusted.example/missing", rank=2)]

    run = run_search_pipeline(profile, PartialProvider({}), FakePageFetcher({URL: page()}), DeterministicOpportunityExtractor(), DeterministicSemanticAssessor(), InMemoryOpportunityStore(), configuration=config, as_of=date(2026, 8, 28), now=NOW)
    assert run.counts["specific_opportunities_evaluated"] == 1
    assert {item.stage.value for item in run.failures} == {"search", "candidate_fetch"}


def test_postgres_schema_contains_search_provenance_tables() -> None:
    assert "CREATE TABLE IF NOT EXISTS search_candidates" in POSTGRES_SCHEMA
    assert "CREATE TABLE IF NOT EXISTS opportunity_search_provenance" in POSTGRES_SCHEMA
    assert "query_mode TEXT NOT NULL" in POSTGRES_SCHEMA
    assert "page_shape_signals JSONB" in POSTGRES_SCHEMA
    assert "proceeded_to_extraction BOOLEAN" in POSTGRES_SCHEMA


def test_tavily_normalizes_mocked_search_response_without_live_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.tavily.com/search"
        assert request.headers["Authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["search_depth"] == "basic"
        assert body["max_results"] == 2
        assert body["include_answer"] is False
        return httpx.Response(200, json={"results": [
            {"title": "Security Fellowship", "url": URL, "content": "Applications are open."},
            {"title": "Research Programme", "url": "https://trusted.example/research", "content": "Student research."},
        ]})

    provider = TavilySearchProvider("test-key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    results = provider.search("security fellowship", limit=2)
    assert [(item.rank, item.provider) for item in results] == [(1, "tavily"), (2, "tavily")]
    assert results[0].snippet == "Applications are open."


def test_tavily_missing_api_key_is_clear(monkeypatch) -> None:
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="TAVILY_API_KEY is required"):
        TavilySearchProvider.from_environment()


def test_fake_and_json_replay_providers_remain_offline(tmp_path) -> None:
    query = "security fellowship"
    item = result(query)
    fake = FakeSearchProvider({query: [item]})
    assert fake.search(query, limit=1)[0].provider == "fake"
    replay_path = tmp_path / "results.json"
    replay_path.write_text(json.dumps([item.model_dump(mode="json")]), encoding="utf-8")
    replay = JsonReplaySearchProvider.from_file(replay_path)
    assert replay.search(query, limit=1)[0].provider == "json-replay"
