from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from opportunity_radar.discovery_models import ChangeClassification, SourceConfiguration
from opportunity_radar.milestone2 import Milestone2Result, run_discovery_pipeline
from opportunity_radar.milestone3 import Milestone3Result, run_search_pipeline
from opportunity_radar.models import OpportunityProfile, OpportunityStatus, PriorityBand
from opportunity_radar.notification_models import (
    DeliveryStatus, NotificationDeliveryResult, NotificationPayload, NotificationSeverity,
    NotificationType,
)
from opportunity_radar.notification_policy import NotificationItem, PlannedNotification, plan_notifications
from opportunity_radar.notifications import NotificationProvider
from opportunity_radar.persistence import PersistenceStore
from opportunity_radar.providers import OpportunityExtractor, PageFetcher, SemanticAssessor
from opportunity_radar.search import SearchProvider
from opportunity_radar.search_models import SearchConfiguration


@dataclass(frozen=True)
class DailyRunSummary:
    known_sources_checked: int
    search_queries_executed: int
    search_results_returned: int
    candidates_checked: int
    opportunities_evaluated: int
    new_opportunities: int
    new_worthwhile: int
    changed_opportunities: int
    closing_soon: int
    isolated_failures: int


@dataclass(frozen=True)
class DailyRunResult:
    source_result: Milestone2Result | None
    search_result: Milestone3Result
    planned_notifications: list[NotificationPayload]
    deliveries: list[NotificationDeliveryResult]
    notification_failures: list[str]
    summary: DailyRunSummary


def _items(source_result: Milestone2Result | None, search_result: Milestone3Result) -> list[NotificationItem]:
    candidates: list[NotificationItem] = []
    for result in [source_result, search_result]:
        if result is None:
            continue
        assessments = {item.opportunity_id: item for item in result.assessments}
        candidates.extend(
            NotificationItem(outcome, assessments[outcome.opportunity.id])
            for outcome in result.persistence if outcome.opportunity.id in assessments
        )
    precedence = {
        ChangeClassification.KNOWN_UNCHANGED: 0,
        ChangeClassification.CHANGED: 1,
        ChangeClassification.NEW: 2,
    }
    merged: dict[int, NotificationItem] = {}
    for item in candidates:
        previous = merged.get(item.outcome.database_id)
        if previous is None or precedence[item.outcome.classification] > precedence[previous.outcome.classification]:
            merged[item.outcome.database_id] = item
    return list(merged.values())


def _record_plan(
    plan: PlannedNotification, store: PersistenceStore, status: DeliveryStatus,
    *, attempted_at: datetime, error: str | None,
) -> list[str]:
    failures: list[str] = []
    for item in plan.items:
        try:
            store.record_notification_delivery(
                item.outcome.database_id, plan.payload.notification_type, item.fingerprint,
                status, attempted_at=attempted_at, error=error,
            )
        except Exception as exc:
            failures.append(f"notification memory failed for opportunity {item.outcome.database_id}: {exc}")
    return failures


def deliver_notifications(
    plans: list[PlannedNotification], notification_provider: NotificationProvider,
    store: PersistenceStore, *, attempted_at: datetime,
) -> tuple[list[NotificationDeliveryResult], list[str]]:
    deliveries: list[NotificationDeliveryResult] = []
    failures: list[str] = []
    for plan in plans:
        if store.notification_fingerprint_was_delivered(plan.fingerprint):
            deliveries.append(NotificationDeliveryResult(
                provider=notification_provider.name, status=DeliveryStatus.DELIVERED,
                chunks_sent=0,
            ))
            continue
        external_ids: list[str] = []
        sent_now = 0
        errors: list[str] = []
        try:
            chunks = notification_provider.prepare(plan.payload, plan.fingerprint)
        except Exception as exc:
            chunks = []
            errors.append(str(exc))
        if not chunks and not errors:
            errors.append("notification rendered no message chunks")

        for chunk in chunks:
            if store.notification_chunk_was_sent(
                plan.fingerprint, chunk.chunk_index, chunk.chunk_fingerprint,
            ):
                continue
            try:
                result = notification_provider.send_chunk(chunk)
                sent_now += result.chunks_sent
                external_ids.extend(result.external_ids)
                store.record_notification_chunk(
                    plan.fingerprint, plan.payload.notification_type, chunk.chunk_index,
                    chunk.chunk_count, chunk.chunk_fingerprint, DeliveryStatus.DELIVERED,
                    attempted_at=attempted_at,
                )
            except Exception as exc:
                error = str(exc)
                errors.append(f"chunk {chunk.chunk_index + 1}/{chunk.chunk_count}: {error}")
                try:
                    store.record_notification_chunk(
                        plan.fingerprint, plan.payload.notification_type, chunk.chunk_index,
                        chunk.chunk_count, chunk.chunk_fingerprint, DeliveryStatus.FAILED,
                        attempted_at=attempted_at, error=error,
                    )
                except Exception as memory_exc:
                    errors.append(f"chunk {chunk.chunk_index + 1} memory failed: {memory_exc}")

        complete = bool(chunks) and all(
            store.notification_chunk_was_sent(
                plan.fingerprint, chunk.chunk_index, chunk.chunk_fingerprint,
            )
            for chunk in chunks
        )
        status = DeliveryStatus.DELIVERED if complete else DeliveryStatus.FAILED
        aggregate_error = "; ".join(errors) or None
        deliveries.append(NotificationDeliveryResult(
            provider=notification_provider.name, status=status, chunks_sent=sent_now,
            external_ids=external_ids, error=aggregate_error,
        ))
        if errors:
            failures.append(f"{plan.payload.notification_type.value} delivery failed: {aggregate_error}")
        failures.extend(_record_plan(
            plan, store, status, attempted_at=attempted_at,
            error=None if complete else aggregate_error or "one or more chunks remain undelivered",
        ))
    return deliveries, failures


def _run_summary(
    source_result: Milestone2Result | None, search_result: Milestone3Result,
    items: list[NotificationItem], search_configuration: SearchConfiguration,
    source_configuration: SourceConfiguration | None,
) -> DailyRunSummary:
    worthwhile_bands = {
        PriorityBand.DISCOVERY, PriorityBand.WORTH_CHECKING,
        PriorityBand.STRONG_MATCH, PriorityBand.EXCEPTIONAL,
    }
    new_worthwhile = sum(
        item.outcome.classification is ChangeClassification.NEW
        and item.outcome.opportunity.status is not OpportunityStatus.CLOSED
        and item.assessment.priority_band in worthwhile_bands
        for item in items
    )
    return DailyRunSummary(
        known_sources_checked=sum(item.enabled for item in source_configuration.sources) if source_configuration else 0,
        search_queries_executed=len(search_result.queries),
        search_results_returned=len(search_result.candidates),
        candidates_checked=min(len(search_result.candidates), search_configuration.fetch_cap),
        opportunities_evaluated=len(items),
        new_opportunities=sum(item.outcome.classification is ChangeClassification.NEW for item in items),
        new_worthwhile=new_worthwhile,
        changed_opportunities=sum(item.outcome.classification is ChangeClassification.CHANGED for item in items),
        closing_soon=sum(item.outcome.opportunity.status is OpportunityStatus.CLOSING_SOON for item in items),
        isolated_failures=len(search_result.failures) + (len(source_result.failures) if source_result else 0),
    )


def _heartbeat_plan(summary: DailyRunSummary, *, now: datetime, run_id: str | None) -> PlannedNotification:
    lines = [
        "Today's scan completed.", "",
        f"Search results: {summary.search_results_returned}",
        f"Candidates checked: {summary.candidates_checked}",
        f"Opportunities evaluated: {summary.opportunities_evaluated}", "",
        f"New worthwhile: {summary.new_worthwhile}",
        f"Changed: {summary.changed_opportunities}",
        f"Closing soon: {summary.closing_soon}",
    ]
    if summary.isolated_failures:
        lines.append(f"Isolated failures: {summary.isolated_failures}")
    lines.extend(["", "Nothing needs your attention today."])
    payload = NotificationPayload(
        notification_type=NotificationType.DAILY_HEARTBEAT,
        title="Opportunity Radar — Daily Check ✅",
        body="\n".join(lines), generated_at=now,
        severity=NotificationSeverity.NORMAL,
    )
    identity = run_id or now.date().isoformat()
    return PlannedNotification(payload, (), identity_key=f"daily-heartbeat:{identity}")


def run_daily(
    profile: OpportunityProfile, search_provider: SearchProvider, notification_provider: NotificationProvider,
    fetcher: PageFetcher, extractor: OpportunityExtractor, assessor: SemanticAssessor,
    store: PersistenceStore, *, source_configuration: SourceConfiguration | None = None,
    search_configuration: SearchConfiguration | None = None, as_of: date,
    opportunity_transform=None, now: datetime | None = None, run_id: str | None = None,
) -> DailyRunResult:
    timestamp = now or datetime.now(timezone.utc)
    source_result = None
    if source_configuration is not None:
        source_result = run_discovery_pipeline(
            source_configuration, profile, fetcher, extractor, assessor, store,
            as_of=as_of, opportunity_transform=opportunity_transform, now=timestamp,
        )
    effective_search_configuration = search_configuration or SearchConfiguration()
    search_result = run_search_pipeline(
        profile, search_provider, fetcher, extractor, assessor, store,
        configuration=effective_search_configuration, as_of=as_of,
        opportunity_transform=opportunity_transform, now=timestamp,
    )
    notification_items = _items(source_result, search_result)
    summary = _run_summary(
        source_result, search_result, notification_items, effective_search_configuration,
        source_configuration,
    )
    plans = plan_notifications(notification_items, store, now=timestamp)
    if not plans:
        plans = [_heartbeat_plan(summary, now=timestamp, run_id=run_id)]
    deliveries, failures = deliver_notifications(
        plans, notification_provider, store, attempted_at=timestamp,
    )
    return DailyRunResult(
        source_result, search_result, [item.payload for item in plans], deliveries, failures,
        summary,
    )


def write_daily_outputs(result: DailyRunResult, output_dir: str | Path) -> None:
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    payloads = {
        "notifications.json": [item.model_dump(mode="json") for item in result.planned_notifications],
        "deliveries.json": [item.model_dump(mode="json") for item in result.deliveries],
        "notification_failures.json": result.notification_failures,
    }
    for name, payload in payloads.items():
        (destination / name).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
