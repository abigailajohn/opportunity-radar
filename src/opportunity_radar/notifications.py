from __future__ import annotations

import os
import hashlib
from typing import Protocol

import httpx

from opportunity_radar.notification_models import (
    DeliveryStatus, NotificationChunk, NotificationDeliveryResult, NotificationPayload,
)


TELEGRAM_MESSAGE_LIMIT = 4096
DEFAULT_MESSAGE_LIMIT = 3900


class NotificationDeliveryError(RuntimeError):
    pass


class NotificationProvider(Protocol):
    name: str

    def prepare(self, payload: NotificationPayload, notification_fingerprint: str) -> list[NotificationChunk]: ...
    def send_chunk(self, chunk: NotificationChunk) -> NotificationDeliveryResult: ...
    def send(self, payload: NotificationPayload) -> NotificationDeliveryResult: ...


def split_message(text: str, *, maximum: int = DEFAULT_MESSAGE_LIMIT) -> list[str]:
    if maximum < 100:
        raise ValueError("message maximum must be at least 100 characters")
    normalized = text.strip()
    if not normalized:
        return []
    chunks: list[str] = []
    current = ""
    for line in normalized.splitlines():
        pieces = [line[index:index + maximum] for index in range(0, len(line), maximum)] or [""]
        for piece in pieces:
            candidate = f"{current}\n{piece}" if current else piece
            if len(candidate) <= maximum:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = piece
    if current:
        chunks.append(current.strip())
    return chunks


def prepare_chunks(text: str, notification_fingerprint: str, *, maximum: int) -> list[NotificationChunk]:
    values = split_message(text, maximum=maximum)
    count = len(values)
    return [
        NotificationChunk(
            notification_fingerprint=notification_fingerprint,
            chunk_index=index,
            chunk_count=count,
            chunk_fingerprint=hashlib.sha256(value.encode("utf-8")).hexdigest(),
            text=value,
        )
        for index, value in enumerate(values)
    ]


class TelegramNotificationProvider:
    name = "telegram"

    def __init__(
        self, bot_token: str, chat_id: str, *, client: httpx.Client | None = None,
        message_limit: int = DEFAULT_MESSAGE_LIMIT,
    ) -> None:
        if not bot_token.strip():
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required for Telegram delivery")
        if not chat_id.strip():
            raise RuntimeError("TELEGRAM_CHAT_ID is required for Telegram delivery")
        if message_limit > TELEGRAM_MESSAGE_LIMIT:
            raise ValueError(f"message_limit cannot exceed {TELEGRAM_MESSAGE_LIMIT}")
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.client = client or httpx.Client(timeout=httpx.Timeout(20.0))
        self.message_limit = message_limit

    @classmethod
    def from_environment(cls, *, client: httpx.Client | None = None) -> TelegramNotificationProvider:
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        if not token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required for Telegram delivery")
        if not chat_id:
            raise RuntimeError("TELEGRAM_CHAT_ID is required for Telegram delivery")
        return cls(token, chat_id, client=client)

    def prepare(self, payload: NotificationPayload, notification_fingerprint: str) -> list[NotificationChunk]:
        text = f"{payload.title}\n\n{payload.body}".strip()
        return prepare_chunks(text, notification_fingerprint, maximum=self.message_limit)

    def send_chunk(self, chunk: NotificationChunk) -> NotificationDeliveryResult:
        try:
            response = self.client.post(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                json={"chat_id": self.chat_id, "text": chunk.text, "disable_web_page_preview": True},
            )
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise NotificationDeliveryError(f"Telegram delivery failed: {type(exc).__name__}") from exc
        if not isinstance(data, dict) or data.get("ok") is not True:
            raise NotificationDeliveryError("Telegram delivery failed: API response was not successful")
        result = data.get("result")
        external_ids = [str(result["message_id"])] if isinstance(result, dict) and result.get("message_id") is not None else []
        return NotificationDeliveryResult(provider=self.name, status=DeliveryStatus.DELIVERED, chunks_sent=1, external_ids=external_ids)

    def send(self, payload: NotificationPayload) -> NotificationDeliveryResult:
        identity = hashlib.sha256(payload.model_dump_json(exclude={"generated_at"}).encode("utf-8")).hexdigest()
        chunks = self.prepare(payload, identity)
        results = [self.send_chunk(chunk) for chunk in chunks]
        return NotificationDeliveryResult(
            provider=self.name, status=DeliveryStatus.DELIVERED,
            chunks_sent=sum(item.chunks_sent for item in results),
            external_ids=[external_id for item in results for external_id in item.external_ids],
        )


class FakeNotificationProvider:
    name = "fake"

    def __init__(self, failure: Exception | None = None, *, message_limit: int = DEFAULT_MESSAGE_LIMIT, fail_chunk_indices: set[int] | None = None) -> None:
        self.failure = failure
        self.message_limit = message_limit
        self.fail_chunk_indices = set(fail_chunk_indices or set())
        self.sent: list[NotificationPayload] = []
        self.sent_chunks: list[NotificationChunk] = []

    def prepare(self, payload: NotificationPayload, notification_fingerprint: str) -> list[NotificationChunk]:
        self.sent.append(payload.model_copy(deep=True))
        return prepare_chunks(f"{payload.title}\n\n{payload.body}", notification_fingerprint, maximum=self.message_limit)

    def send_chunk(self, chunk: NotificationChunk) -> NotificationDeliveryResult:
        self.sent_chunks.append(chunk.model_copy(deep=True))
        if self.failure or chunk.chunk_index in self.fail_chunk_indices:
            raise self.failure or NotificationDeliveryError(f"fake chunk {chunk.chunk_index} failed")
        return NotificationDeliveryResult(provider=self.name, status=DeliveryStatus.DELIVERED, chunks_sent=1, external_ids=[str(len(self.sent_chunks))])

    def send(self, payload: NotificationPayload) -> NotificationDeliveryResult:
        identity = hashlib.sha256(payload.model_dump_json(exclude={"generated_at"}).encode("utf-8")).hexdigest()
        results = [self.send_chunk(chunk) for chunk in self.prepare(payload, identity)]
        return NotificationDeliveryResult(provider=self.name, status=DeliveryStatus.DELIVERED, chunks_sent=len(results), external_ids=[value for item in results for value in item.external_ids])
