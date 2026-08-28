from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import hashlib
import json

from opportunity_radar.deduplication import canonical_url
from opportunity_radar.discovery_models import ChangeClassification
from opportunity_radar.models import OpportunityAssessment, OpportunityStatus, PriorityBand
from opportunity_radar.notification_models import NotificationPayload, NotificationSeverity, NotificationType
from opportunity_radar.persistence import PersistenceOutcome, content_fingerprint, meaningful_snapshot


@dataclass(frozen=True)
class NotificationItem:
    outcome: PersistenceOutcome
    assessment: OpportunityAssessment

    @property
    def fingerprint(self) -> str:
        return content_fingerprint(meaningful_snapshot(self.outcome.opportunity))


@dataclass(frozen=True)
class PlannedNotification:
    payload: NotificationPayload
    items: tuple[NotificationItem, ...]

    @property
    def fingerprint(self) -> str:
        identity = {
            "notification_type": self.payload.notification_type.value,
            "title": self.payload.title,
            "body": self.payload.body,
            "items": sorted((item.outcome.database_id, item.fingerprint) for item in self.items),
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _actionable(item: NotificationItem) -> bool:
    return item.outcome.opportunity.status is not OpportunityStatus.CLOSED and item.assessment.priority_band is not PriorityBand.NOT_ACTIONABLE


def _immediate(item: NotificationItem) -> bool:
    if item.outcome.classification not in {ChangeClassification.NEW, ChangeClassification.CHANGED} or not _actionable(item):
        return False
    if item.assessment.priority_band is PriorityBand.EXCEPTIONAL:
        return True
    if item.assessment.priority_band is not PriorityBand.STRONG_MATCH:
        return False
    opportunity = item.outcome.opportunity
    urgent = opportunity.status is OpportunityStatus.CLOSING_SOON
    newly_opened = opportunity.status is OpportunityStatus.OPEN and (
        item.outcome.classification is ChangeClassification.NEW or "status" in item.outcome.changed_fields
    )
    deadline_changed = "deadline" in item.outcome.changed_fields and opportunity.status is OpportunityStatus.CLOSING_SOON
    return urgent or newly_opened or deadline_changed


def _daily_worthy(item: NotificationItem) -> bool:
    if not _actionable(item) or item.assessment.priority_band is PriorityBand.LOW_PRIORITY:
        return False
    if item.outcome.opportunity.status is OpportunityStatus.CLOSING_SOON:
        return True
    if item.assessment.priority_band in {PriorityBand.EXCEPTIONAL, PriorityBand.STRONG_MATCH}:
        return True
    return item.outcome.classification in {ChangeClassification.NEW, ChangeClassification.CHANGED} and item.assessment.priority_band in {PriorityBand.WORTH_CHECKING, PriorityBand.DISCOVERY}


def _deadline(item: NotificationItem) -> str:
    opportunity = item.outcome.opportunity
    if opportunity.deadline:
        return opportunity.deadline.date().isoformat()
    return "Rolling" if opportunity.rolling_application else "Unknown"


def _immediate_payload(item: NotificationItem, now: datetime) -> NotificationPayload:
    opportunity, assessment = item.outcome.opportunity, item.assessment
    body = "\n".join([
        opportunity.title,
        f"Score: {assessment.total_score:g}/100 — {assessment.priority_band.value.replace('_', ' ').title()}",
        f"Deadline: {_deadline(item)}",
        f"Why it matters: {assessment.why_it_matters}",
        "",
        "Apply:",
        str(opportunity.application_url or opportunity.official_url or opportunity.source_url),
    ])
    return NotificationPayload(
        notification_type=NotificationType.IMMEDIATE_ALERT,
        title="🚨 Strong Opportunity",
        body=body,
        opportunity_ids=[item.outcome.database_id],
        canonical_urls=[canonical_url(opportunity.official_url or opportunity.source_url)],
        generated_at=now,
        severity=NotificationSeverity.URGENT if opportunity.status is OpportunityStatus.CLOSING_SOON else NotificationSeverity.HIGH,
    )


def _digest_payload(items: list[NotificationItem], now: datetime) -> NotificationPayload:
    new = sum(item.outcome.classification is ChangeClassification.NEW for item in items)
    changed = sum(item.outcome.classification is ChangeClassification.CHANGED for item in items)
    closing = sum(item.outcome.opportunity.status is OpportunityStatus.CLOSING_SOON for item in items)
    lines = [f"New: {new}", f"Changed: {changed}", f"Closing soon: {closing}", ""]
    for index, item in enumerate(items, start=1):
        opportunity, assessment = item.outcome.opportunity, item.assessment
        lines.extend([
            f"{index}. {opportunity.title}",
            f"   {assessment.total_score:g}/100 — {assessment.priority_band.value.replace('_', ' ').title()}",
            f"   Deadline: {_deadline(item)}",
            f"   Why: {assessment.why_it_matters}",
            f"   {opportunity.application_url or opportunity.official_url or opportunity.source_url}", "",
        ])
    return NotificationPayload(
        notification_type=NotificationType.DAILY_DIGEST,
        title="Opportunity Radar — Daily Digest",
        body="\n".join(lines).strip(),
        opportunity_ids=[item.outcome.database_id for item in items],
        canonical_urls=[canonical_url(item.outcome.opportunity.official_url or item.outcome.opportunity.source_url) for item in items],
        generated_at=now,
    )


def plan_notifications(items: list[NotificationItem], store, *, now: datetime) -> list[PlannedNotification]:
    immediate_items = [
        item for item in items if _immediate(item)
        and not store.notification_was_sent(item.outcome.database_id, NotificationType.IMMEDIATE_ALERT, item.fingerprint)
    ]
    plans = [PlannedNotification(_immediate_payload(item, now), (item,)) for item in immediate_items]
    immediate_ids = {item.outcome.database_id for item in immediate_items}
    daily_items = [
        item for item in items if item.outcome.database_id not in immediate_ids and _daily_worthy(item)
        and not store.notification_was_sent(item.outcome.database_id, NotificationType.DAILY_DIGEST, item.fingerprint)
        and not store.notification_was_sent(item.outcome.database_id, NotificationType.IMMEDIATE_ALERT, item.fingerprint)
    ]
    if daily_items:
        daily_items.sort(key=lambda item: (-item.assessment.total_score, item.outcome.opportunity.title.casefold()))
        plans.append(PlannedNotification(_digest_payload(daily_items, now), tuple(daily_items)))
    return plans
