from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from opportunity_radar.discovery_models import ChangeClassification, SourceConfiguration
from opportunity_radar.milestone2 import Milestone2Result, run_discovery_pipeline
from opportunity_radar.milestone3 import Milestone3Result, run_search_pipeline
from opportunity_radar.models import OpportunityProfile
from opportunity_radar.notification_models import DeliveryStatus, NotificationDeliveryResult, NotificationPayload
from opportunity_radar.notification_policy import NotificationItem, PlannedNotification, plan_notifications
from opportunity_radar.notifications import NotificationProvider
from opportunity_radar.persistence import PersistenceStore
from opportunity_radar.providers import OpportunityExtractor, PageFetcher, SemanticAssessor
from opportunity_radar.search import SearchProvider
from opportunity_radar.search_models import SearchConfiguration


@dataclass(frozen=True)
class DailyRunResult:
    source_result: Milestone2Result | None
    search_result: Milestone3Result
    planned_notifications: list[NotificationPayload]
    deliveries: list[NotificationDeliveryResult]
    notification_failures: list[str]


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


def run_daily(
    profile: OpportunityProfile, search_provider: SearchProvider, notification_provider: NotificationProvider,
    fetcher: PageFetcher, extractor: OpportunityExtractor, assessor: SemanticAssessor,
    store: PersistenceStore, *, source_configuration: SourceConfiguration | None = None,
    search_configuration: SearchConfiguration | None = None, as_of: date,
    opportunity_transform=None, now: datetime | None = None,
) -> DailyRunResult:
    timestamp = now or datetime.now(timezone.utc)
    source_result = None
    if source_configuration is not None:
        source_result = run_discovery_pipeline(
            source_configuration, profile, fetcher, extractor, assessor, store,
            as_of=as_of, opportunity_transform=opportunity_transform, now=timestamp,
        )
    search_result = run_search_pipeline(
        profile, search_provider, fetcher, extractor, assessor, store,
        configuration=search_configuration, as_of=as_of,
        opportunity_transform=opportunity_transform, now=timestamp,
    )
    plans = plan_notifications(_items(source_result, search_result), store, now=timestamp)
    deliveries, failures = deliver_notifications(
        plans, notification_provider, store, attempted_at=timestamp,
    )
    return DailyRunResult(source_result, search_result, [item.payload for item in plans], deliveries, failures)


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
