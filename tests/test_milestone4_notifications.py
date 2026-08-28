from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from conftest import make_judgment, make_opportunity
from opportunity_radar.deterministic_extraction import DeterministicOpportunityExtractor
from opportunity_radar.discovery_models import ChangeClassification, SourceConfiguration, SourceType, TrustedSource
from opportunity_radar.evaluation import evaluate_opportunity
from opportunity_radar.fetching import FetchedPage
from opportunity_radar.html_preparation import prepare_html
from opportunity_radar.milestone4 import deliver_notifications, run_daily
from opportunity_radar.models import OpportunityStatus, PriorityBand
from opportunity_radar.notification_models import DeliveryStatus, NotificationType
from opportunity_radar.notification_policy import NotificationItem, PlannedNotification, plan_notifications
from opportunity_radar.notifications import (
    FakeNotificationProvider, NotificationDeliveryError, TelegramNotificationProvider, split_message,
)
from opportunity_radar.persistence import InMemoryOpportunityStore, POSTGRES_SCHEMA, PersistenceOutcome
from opportunity_radar.providers import FakePageFetcher, FakeSemanticAssessor
from opportunity_radar.search import FakeSearchProvider, generate_search_queries
from opportunity_radar.search_models import SearchConfiguration, SearchResult


NOW = datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures"


def assessment(profile, opportunity, band: PriorityBand):
    value = evaluate_opportunity(
        opportunity, profile, FakeSemanticAssessor({str(opportunity.source_url): make_judgment()}),
        as_of=NOW.date(),
    )
    return value.model_copy(update={"priority_band": band})


def item(profile, *, database_id: int, band: PriorityBand, classification: ChangeClassification = ChangeClassification.NEW, status: OpportunityStatus = OpportunityStatus.OPEN, changed_fields: tuple[str, ...] = ()) -> NotificationItem:
    opportunity = make_opportunity(title=f"Opportunity {database_id}", url=f"https://example.test/{database_id}", status=status)
    outcome = PersistenceOutcome(opportunity, classification, changed_fields, database_id)
    return NotificationItem(outcome, assessment(profile, opportunity, band))


def test_telegram_request_normalization_uses_plain_text_and_bot_api() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 42}})

    provider = TelegramNotificationProvider("test-token", "12345", client=httpx.Client(transport=httpx.MockTransport(handler)))
    plan = item_payload()
    result = provider.send(plan)
    body = json.loads(requests[0].content)
    assert requests[0].url == "https://api.telegram.org/bottest-token/sendMessage"
    assert body["chat_id"] == "12345" and body["disable_web_page_preview"] is True
    assert body["text"].startswith("Test title\n\nTest body")
    assert "parse_mode" not in body
    assert result.status is DeliveryStatus.DELIVERED and result.external_ids == ["42"]


def item_payload():
    from opportunity_radar.notification_models import NotificationPayload
    return NotificationPayload(notification_type=NotificationType.DAILY_DIGEST, title="Test title", body="Test body", generated_at=NOW)


def test_missing_telegram_environment_is_clear(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    with pytest.raises(RuntimeError, match="TELEGRAM_BOT_TOKEN"):
        TelegramNotificationProvider.from_environment()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    with pytest.raises(RuntimeError, match="TELEGRAM_CHAT_ID"):
        TelegramNotificationProvider.from_environment()


def test_message_splitting_stays_under_limit_and_preserves_content() -> None:
    text = "\n".join(f"Opportunity {index}: " + "x" * 80 for index in range(20))
    chunks = split_message(text, maximum=200)
    assert len(chunks) > 1 and all(len(chunk) <= 200 for chunk in chunks)
    assert "Opportunity 0" in chunks[0] and "Opportunity 19" in chunks[-1]


def test_immediate_alert_policy_and_daily_deduplication(profile) -> None:
    store = InMemoryOpportunityStore()
    exceptional = item(profile, database_id=1, band=PriorityBand.EXCEPTIONAL)
    strong_urgent = item(profile, database_id=2, band=PriorityBand.STRONG_MATCH, status=OpportunityStatus.CLOSING_SOON)
    worthwhile = item(profile, database_id=3, band=PriorityBand.WORTH_CHECKING)
    plans = plan_notifications([exceptional, strong_urgent, worthwhile], store, now=NOW)
    assert [plan.payload.notification_type for plan in plans] == [NotificationType.IMMEDIATE_ALERT, NotificationType.IMMEDIATE_ALERT, NotificationType.DAILY_DIGEST]
    assert plans[-1].payload.opportunity_ids == [3]
    assert "New: 1" in plans[-1].payload.body


def test_no_alert_for_closed_low_or_not_actionable(profile) -> None:
    store = InMemoryOpportunityStore()
    cases = [
        item(profile, database_id=1, band=PriorityBand.EXCEPTIONAL, status=OpportunityStatus.CLOSED),
        item(profile, database_id=2, band=PriorityBand.LOW_PRIORITY),
        item(profile, database_id=3, band=PriorityBand.NOT_ACTIONABLE),
    ]
    assert plan_notifications(cases, store, now=NOW) == []


def test_duplicate_suppression_and_changed_fingerprint_renotification(profile) -> None:
    store = InMemoryOpportunityStore()
    original = item(profile, database_id=1, band=PriorityBand.EXCEPTIONAL)
    first = plan_notifications([original], store, now=NOW)[0]
    store.record_notification_delivery(1, first.payload.notification_type, original.fingerprint, DeliveryStatus.DELIVERED, attempted_at=NOW)
    assert plan_notifications([original], store, now=NOW) == []
    changed_opportunity = original.outcome.opportunity.model_copy(update={"status": OpportunityStatus.CLOSING_SOON})
    changed = NotificationItem(
        PersistenceOutcome(changed_opportunity, ChangeClassification.CHANGED, ("status",), 1),
        original.assessment.model_copy(update={"opportunity_id": changed_opportunity.id}),
    )
    replanned = plan_notifications([changed], store, now=NOW)
    assert replanned and replanned[0].payload.notification_type is NotificationType.IMMEDIATE_ALERT
    assert changed.fingerprint != original.fingerprint


def test_failed_delivery_is_retryable_and_records_attempt(profile) -> None:
    store = InMemoryOpportunityStore()
    candidate = item(profile, database_id=4, band=PriorityBand.EXCEPTIONAL)
    store.record_notification_delivery(4, NotificationType.IMMEDIATE_ALERT, candidate.fingerprint, DeliveryStatus.FAILED, attempted_at=NOW, error="timeout")
    assert store.notification_was_sent(4, NotificationType.IMMEDIATE_ALERT, candidate.fingerprint) is False
    assert plan_notifications([candidate], store, now=NOW)


def fixture_page(url: str) -> FetchedPage:
    html = (FIXTURES / "discovery_fellowship.html").read_text(encoding="utf-8")
    return FetchedPage(requested_url=url, final_url=url, raw_html=html, cleaned_text=prepare_html(html, base_url=url), fetched_at=NOW)


def test_full_daily_orchestration_uses_known_source_search_and_isolates_delivery_failure(profile) -> None:
    url = "https://trusted.example/security-fellowship-2027"
    page = fixture_page(url)
    source = TrustedSource(id="daily-source", name="Daily source", url=url, source_type=SourceType.RECURRING_PROGRAMME)
    source_config = SourceConfiguration(sources=[source], global_max_candidate_pages=2)
    search_config = SearchConfiguration(max_queries_per_run=1, max_results_per_query=1, global_candidate_cap=1, fetch_cap=1)
    query = generate_search_queries(profile, search_config, rotation_date=NOW.date())[0]
    search = FakeSearchProvider({query.text: [SearchResult(title="Security Fellowship 2027", url=url, snippet="Applications open", rank=1, query=query.text, provider="fake")]})
    fetcher = FakePageFetcher({url: page})
    assessor = FakeSemanticAssessor({url: make_judgment()})
    store = InMemoryOpportunityStore()
    notifier = FakeNotificationProvider(NotificationDeliveryError("Telegram unavailable"))
    run = run_daily(
        profile, search, notifier, fetcher, DeterministicOpportunityExtractor(), assessor, store,
        source_configuration=source_config, search_configuration=search_config,
        as_of=NOW.date(), now=NOW,
    )
    assert run.source_result is not None and run.source_result.persistence
    assert run.search_result.persistence
    assert len(store.opportunities) == 1
    assert run.planned_notifications and run.deliveries[0].status is DeliveryStatus.FAILED
    assert run.notification_failures
    assert store.notification_deliveries


def test_notification_schema_is_postgresql_and_offline() -> None:
    assert "CREATE TABLE IF NOT EXISTS notification_deliveries" in POSTGRES_SCHEMA
    assert "triggering_fingerprint TEXT NOT NULL" in POSTGRES_SCHEMA
    assert "UNIQUE(opportunity_id, notification_type, triggering_fingerprint)" in POSTGRES_SCHEMA
    assert "CREATE TABLE IF NOT EXISTS notification_chunks" in POSTGRES_SCHEMA
    assert "UNIQUE(notification_fingerprint, chunk_index)" in POSTGRES_SCHEMA


def split_plan(profile, *, body_suffix: str = "") -> PlannedNotification:
    candidate = item(profile, database_id=90, band=PriorityBand.WORTH_CHECKING)
    payload = item_payload().model_copy(update={
        "body": "\n".join(f"Opportunity detail {index}: {'x' * 60}" for index in range(8)) + body_suffix,
        "opportunity_ids": [90],
        "canonical_urls": [str(candidate.outcome.opportunity.source_url)],
    })
    return PlannedNotification(payload, (candidate,))


def test_split_retry_sends_only_failed_chunk_and_eventually_completes(profile) -> None:
    store = InMemoryOpportunityStore()
    provider = FakeNotificationProvider(message_limit=180, fail_chunk_indices={1})
    plan = split_plan(profile)

    first, first_failures = deliver_notifications([plan], provider, store, attempted_at=NOW)
    assert first[0].status is DeliveryStatus.FAILED
    assert first_failures
    assert store.notification_chunks[(plan.fingerprint, 0)]["status"] is DeliveryStatus.DELIVERED
    assert store.notification_chunks[(plan.fingerprint, 1)]["status"] is DeliveryStatus.FAILED
    first_attempted_indices = [chunk.chunk_index for chunk in provider.sent_chunks]
    assert first_attempted_indices.count(0) == 1

    provider.fail_chunk_indices.clear()
    before_retry = len(provider.sent_chunks)
    second, second_failures = deliver_notifications([plan], provider, store, attempted_at=NOW)
    retried_indices = [chunk.chunk_index for chunk in provider.sent_chunks[before_retry:]]
    assert retried_indices == [1]
    assert second[0].status is DeliveryStatus.DELIVERED
    assert second_failures == []
    assert all(
        store.notification_chunk_was_sent(plan.fingerprint, chunk.chunk_index, chunk.chunk_fingerprint)
        for chunk in provider.prepare(plan.payload, plan.fingerprint)
    )
    assert store.notification_was_sent(90, NotificationType.DAILY_DIGEST, plan.items[0].fingerprint)


def test_single_chunk_delivery_and_duplicate_suppression_are_unchanged(profile) -> None:
    store = InMemoryOpportunityStore()
    provider = FakeNotificationProvider()
    candidate = item(profile, database_id=91, band=PriorityBand.WORTH_CHECKING)
    plan = PlannedNotification(item_payload(), (candidate,))
    deliveries, failures = deliver_notifications([plan], provider, store, attempted_at=NOW)
    assert deliveries[0].status is DeliveryStatus.DELIVERED
    assert deliveries[0].chunks_sent == 1 and failures == []
    assert len(provider.sent_chunks) == 1
    assert plan_notifications([candidate], store, now=NOW) == []


def test_changed_notification_fingerprint_is_delivered_again(profile) -> None:
    store = InMemoryOpportunityStore()
    provider = FakeNotificationProvider(message_limit=180)
    original = split_plan(profile)
    deliver_notifications([original], provider, store, attempted_at=NOW)
    original_count = len(provider.sent_chunks)
    changed = PlannedNotification(
        original.payload.model_copy(update={"body": original.payload.body + "\nDeadline changed."}),
        original.items,
    )
    assert changed.fingerprint != original.fingerprint
    deliveries, failures = deliver_notifications([changed], provider, store, attempted_at=NOW)
    assert deliveries[0].status is DeliveryStatus.DELIVERED and failures == []
    assert len(provider.sent_chunks) > original_count
