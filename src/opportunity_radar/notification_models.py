from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


class NotificationType(StrEnum):
    IMMEDIATE_ALERT = "immediate_alert"
    DAILY_DIGEST = "daily_digest"
    DAILY_HEARTBEAT = "daily_heartbeat"


class NotificationSeverity(StrEnum):
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class DeliveryStatus(StrEnum):
    DELIVERED = "delivered"
    FAILED = "failed"


class NotificationPayload(BaseModel):
    notification_type: NotificationType
    title: str
    body: str
    opportunity_ids: list[int] = Field(default_factory=list)
    canonical_urls: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    severity: NotificationSeverity = NotificationSeverity.NORMAL


class NotificationDeliveryResult(BaseModel):
    provider: str
    status: DeliveryStatus
    chunks_sent: int = 0
    external_ids: list[str] = Field(default_factory=list)
    error: str | None = None


class NotificationChunk(BaseModel):
    notification_fingerprint: str
    chunk_index: int = Field(ge=0)
    chunk_count: int = Field(ge=1)
    chunk_fingerprint: str
    text: str
