from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

from opportunity_radar.deduplication import canonical_url
from opportunity_radar.discovery_models import ChangeClassification, DiscoveryCandidate, TrustedSource
from opportunity_radar.models import Opportunity
from opportunity_radar.notification_models import DeliveryStatus, NotificationType
from opportunity_radar.search_models import SearchCandidate


DATABASE_URL_ENV = "OPPORTUNITY_RADAR_DATABASE_URL"

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  source_type TEXT NOT NULL,
  enabled BOOLEAN NOT NULL,
  category_focus JSONB NOT NULL DEFAULT '[]'::jsonb,
  check_frequency TEXT NOT NULL,
  max_links_per_run INTEGER NOT NULL CHECK (max_links_per_run > 0),
  notes TEXT,
  last_checked_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS discovery_candidates (
  id BIGSERIAL PRIMARY KEY,
  canonical_url TEXT NOT NULL,
  source_id TEXT NOT NULL REFERENCES sources(id),
  discovered_from_url TEXT NOT NULL,
  depth SMALLINT NOT NULL CHECK (depth BETWEEN 1 AND 2),
  anchor_text TEXT NOT NULL,
  nearby_context TEXT NOT NULL,
  discovery_score INTEGER NOT NULL,
  discovery_signals JSONB NOT NULL DEFAULT '[]'::jsonb,
  classification TEXT NOT NULL,
  discovered_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_discovery_candidates_url ON discovery_candidates(canonical_url);
CREATE INDEX IF NOT EXISTS idx_discovery_candidates_source_time ON discovery_candidates(source_id, discovered_at DESC);

CREATE TABLE IF NOT EXISTS opportunities (
  id BIGSERIAL PRIMARY KEY,
  identity_key TEXT NOT NULL UNIQUE,
  canonical_url TEXT NOT NULL,
  title TEXT NOT NULL,
  organization TEXT,
  category TEXT NOT NULL,
  first_seen_at TIMESTAMPTZ NOT NULL,
  last_seen_at TIMESTAMPTZ NOT NULL,
  last_verified_at TIMESTAMPTZ NOT NULL,
  status TEXT NOT NULL,
  deadline TIMESTAMPTZ,
  source_id TEXT NOT NULL REFERENCES sources(id),
  program_family TEXT,
  cycle_label TEXT,
  cycle_year INTEGER,
  content_fingerprint TEXT NOT NULL,
  last_digest_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_opportunities_url ON opportunities(canonical_url);
CREATE INDEX IF NOT EXISTS idx_opportunities_status_deadline ON opportunities(status, deadline);

CREATE TABLE IF NOT EXISTS opportunity_versions (
  id BIGSERIAL PRIMARY KEY,
  opportunity_id BIGINT NOT NULL REFERENCES opportunities(id),
  version_number INTEGER NOT NULL CHECK (version_number > 0),
  recorded_at TIMESTAMPTZ NOT NULL,
  content_fingerprint TEXT NOT NULL,
  structured_data JSONB NOT NULL,
  changed_fields JSONB NOT NULL DEFAULT '[]'::jsonb,
  UNIQUE(opportunity_id, version_number)
);
CREATE INDEX IF NOT EXISTS idx_opportunity_versions_latest ON opportunity_versions(opportunity_id, version_number DESC);

CREATE TABLE IF NOT EXISTS search_candidates (
  id BIGSERIAL PRIMARY KEY,
  canonical_url TEXT NOT NULL,
  title TEXT NOT NULL,
  snippet TEXT NOT NULL,
  query TEXT NOT NULL,
  query_mode TEXT NOT NULL,
  search_rank INTEGER NOT NULL CHECK (search_rank > 0),
  provider TEXT NOT NULL,
  discovered_at TIMESTAMPTZ NOT NULL,
  search_score INTEGER NOT NULL,
  search_signals JSONB NOT NULL DEFAULT '[]'::jsonb,
  page_shape TEXT,
  page_shape_signals JSONB NOT NULL DEFAULT '[]'::jsonb,
  proceeded_to_extraction BOOLEAN,
  rejection_reason TEXT
);
ALTER TABLE search_candidates ADD COLUMN IF NOT EXISTS page_shape TEXT;
ALTER TABLE search_candidates ADD COLUMN IF NOT EXISTS page_shape_signals JSONB NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE search_candidates ADD COLUMN IF NOT EXISTS proceeded_to_extraction BOOLEAN;
ALTER TABLE search_candidates ADD COLUMN IF NOT EXISTS rejection_reason TEXT;
CREATE INDEX IF NOT EXISTS idx_search_candidates_url ON search_candidates(canonical_url);
CREATE INDEX IF NOT EXISTS idx_search_candidates_discovered ON search_candidates(discovered_at DESC);

CREATE TABLE IF NOT EXISTS opportunity_search_provenance (
  id BIGSERIAL PRIMARY KEY,
  opportunity_id BIGINT NOT NULL REFERENCES opportunities(id),
  canonical_url TEXT NOT NULL,
  query TEXT NOT NULL,
  query_mode TEXT NOT NULL,
  provider TEXT NOT NULL,
  first_discovered_at TIMESTAMPTZ NOT NULL,
  last_discovered_at TIMESTAMPTZ NOT NULL,
  UNIQUE(opportunity_id, canonical_url, query, query_mode, provider)
);
CREATE INDEX IF NOT EXISTS idx_search_provenance_opportunity ON opportunity_search_provenance(opportunity_id);

CREATE TABLE IF NOT EXISTS notification_deliveries (
  id BIGSERIAL PRIMARY KEY,
  opportunity_id BIGINT NOT NULL REFERENCES opportunities(id),
  notification_type TEXT NOT NULL,
  triggering_fingerprint TEXT NOT NULL,
  first_sent_at TIMESTAMPTZ,
  last_sent_at TIMESTAMPTZ,
  last_attempted_at TIMESTAMPTZ NOT NULL,
  delivery_status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 1 CHECK (attempts > 0),
  error TEXT,
  UNIQUE(opportunity_id, notification_type, triggering_fingerprint)
);
CREATE INDEX IF NOT EXISTS idx_notification_deliveries_lookup
ON notification_deliveries(opportunity_id, notification_type, triggering_fingerprint, delivery_status);

CREATE TABLE IF NOT EXISTS notification_chunks (
  id BIGSERIAL PRIMARY KEY,
  notification_fingerprint TEXT NOT NULL,
  notification_type TEXT NOT NULL,
  chunk_index INTEGER NOT NULL CHECK (chunk_index >= 0),
  chunk_count INTEGER NOT NULL CHECK (chunk_count > 0),
  chunk_fingerprint TEXT NOT NULL,
  delivery_status TEXT NOT NULL,
  attempts INTEGER NOT NULL DEFAULT 1 CHECK (attempts > 0),
  first_sent_at TIMESTAMPTZ,
  last_sent_at TIMESTAMPTZ,
  last_attempted_at TIMESTAMPTZ NOT NULL,
  error TEXT,
  UNIQUE(notification_fingerprint, chunk_index)
);
CREATE INDEX IF NOT EXISTS idx_notification_chunks_lookup
ON notification_chunks(notification_fingerprint, chunk_index, chunk_fingerprint, delivery_status);
"""

MEANINGFUL_FIELDS = (
    "status", "opening_date", "deadline", "application_url", "eligibility", "funding",
    "participation_mode", "program_family", "cycle_label", "cycle_year",
)


@dataclass(frozen=True)
class PersistenceOutcome:
    opportunity: Opportunity
    classification: ChangeClassification
    changed_fields: tuple[str, ...]
    database_id: int


class PersistenceStore(Protocol):
    def persist_sources(self, sources: list[TrustedSource], *, checked_at: datetime) -> None: ...
    def persist_candidates(self, candidates: list[DiscoveryCandidate]) -> None: ...
    def persist_opportunity(self, opportunity: Opportunity, *, source_id: str, seen_at: datetime | None = None) -> PersistenceOutcome: ...
    def mark_digested(self, database_ids: list[int], *, at: datetime | None = None) -> None: ...
    def persist_search_candidates(self, candidates: list[SearchCandidate]) -> None: ...
    def persist_search_provenance(self, database_id: int, candidate: SearchCandidate) -> None: ...
    def notification_was_sent(self, database_id: int, notification_type: NotificationType, fingerprint: str) -> bool: ...
    def record_notification_delivery(self, database_id: int, notification_type: NotificationType, fingerprint: str, status: DeliveryStatus, *, attempted_at: datetime, error: str | None = None) -> None: ...
    def notification_chunk_was_sent(self, notification_fingerprint: str, chunk_index: int, chunk_fingerprint: str) -> bool: ...
    def record_notification_chunk(self, notification_fingerprint: str, notification_type: NotificationType, chunk_index: int, chunk_count: int, chunk_fingerprint: str, status: DeliveryStatus, *, attempted_at: datetime, error: str | None = None) -> None: ...
    def notification_fingerprint_was_delivered(self, notification_fingerprint: str) -> bool: ...


def meaningful_snapshot(opportunity: Opportunity) -> dict[str, Any]:
    data = opportunity.model_dump(mode="json")
    eligibility = dict(data["eligibility"])
    eligibility.pop("raw_text", None)
    return {
        "status": data["status"], "opening_date": data["opening_date"], "deadline": data["deadline"],
        "application_url": data["application_url"], "eligibility": eligibility, "funding": data["funding"],
        "participation_mode": data["participation_mode"], "program_family": data["program_family"],
        "cycle_label": data["cycle_label"], "cycle_year": data["cycle_year"],
    }


def content_fingerprint(snapshot: dict[str, Any]) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _identity(opportunity: Opportunity) -> tuple[str, str]:
    canonical = canonical_url(opportunity.official_url or opportunity.source_url)
    cycle = opportunity.cycle_year or opportunity.cycle_label or "unknown"
    return canonical, f"{canonical}::{cycle}"


def _classification(previous: dict[str, Any] | None, current: dict[str, Any]) -> tuple[ChangeClassification, tuple[str, ...]]:
    if previous is None:
        return ChangeClassification.NEW, tuple(MEANINGFUL_FIELDS)
    changed = tuple(field for field in MEANINGFUL_FIELDS if previous.get(field) != current.get(field))
    return (ChangeClassification.CHANGED if changed else ChangeClassification.KNOWN_UNCHANGED), changed


class PostgresOpportunityStore:
    """PostgreSQL persistence; compatible with Supabase connection strings."""

    def __init__(self, connection: Any, *, initialize_schema: bool = True) -> None:
        self.connection = connection
        if initialize_schema:
            with self.connection.cursor() as cursor:
                cursor.execute(POSTGRES_SCHEMA)
            self.connection.commit()

    @classmethod
    def from_environment(cls, *, variable: str = DATABASE_URL_ENV, initialize_schema: bool = True) -> PostgresOpportunityStore:
        database_url = os.getenv(variable)
        if not database_url:
            raise RuntimeError(f"{variable} is required for discovery persistence")
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:
            raise RuntimeError("psycopg is required for PostgreSQL persistence") from exc
        return cls(psycopg.connect(database_url, row_factory=dict_row), initialize_schema=initialize_schema)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> PostgresOpportunityStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def persist_sources(self, sources: list[TrustedSource], *, checked_at: datetime) -> None:
        statement = """INSERT INTO sources(id,name,url,source_type,enabled,category_focus,check_frequency,max_links_per_run,notes,last_checked_at)
        VALUES(%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s)
        ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,url=EXCLUDED.url,source_type=EXCLUDED.source_type,
        enabled=EXCLUDED.enabled,category_focus=EXCLUDED.category_focus,check_frequency=EXCLUDED.check_frequency,
        max_links_per_run=EXCLUDED.max_links_per_run,notes=EXCLUDED.notes,last_checked_at=EXCLUDED.last_checked_at"""
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.executemany(statement, [(item.id, item.name, canonical_url(item.url), item.source_type.value, item.enabled, json.dumps(item.category_focus), item.check_frequency, item.max_links_per_run, item.notes, checked_at) for item in sources])

    def persist_candidates(self, candidates: list[DiscoveryCandidate]) -> None:
        if not candidates:
            return
        statement = """INSERT INTO discovery_candidates(canonical_url,source_id,discovered_from_url,depth,anchor_text,nearby_context,discovery_score,discovery_signals,classification,discovered_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s)"""
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.executemany(statement, [(canonical_url(item.url), item.source_id, str(item.discovered_from_url), item.depth, item.anchor_text, item.nearby_context, item.discovery_score, json.dumps(item.discovery_signals), item.classification.value, item.discovered_at) for item in candidates])

    def persist_opportunity(self, opportunity: Opportunity, *, source_id: str, seen_at: datetime | None = None) -> PersistenceOutcome:
        now = seen_at or datetime.now(timezone.utc); snapshot = meaningful_snapshot(opportunity); fingerprint = content_fingerprint(snapshot)
        canonical, identity_key = _identity(opportunity)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute("SELECT id FROM opportunities WHERE identity_key=%s FOR UPDATE", (identity_key,))
            row = cursor.fetchone()
            if row is None:
                classification, changed = _classification(None, snapshot)
                cursor.execute("""INSERT INTO opportunities(identity_key,canonical_url,title,organization,category,first_seen_at,last_seen_at,last_verified_at,status,deadline,source_id,program_family,cycle_label,cycle_year,content_fingerprint)
                VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (identity_key, canonical, opportunity.title, opportunity.organization, opportunity.category, now, now, opportunity.last_verified_at, opportunity.status.value, opportunity.deadline, source_id, opportunity.program_family, opportunity.cycle_label, opportunity.cycle_year, fingerprint))
                database_id = int(cursor.fetchone()["id"])
                self._insert_version(cursor, database_id, 1, now, fingerprint, snapshot, changed)
                return PersistenceOutcome(opportunity, classification, changed, database_id)
            database_id = int(row["id"])
            cursor.execute("SELECT structured_data FROM opportunity_versions WHERE opportunity_id=%s ORDER BY version_number DESC LIMIT 1", (database_id,))
            version = cursor.fetchone(); previous = version["structured_data"] if version else None
            if isinstance(previous, str): previous = json.loads(previous)
            classification, changed = _classification(previous, snapshot)
            cursor.execute("""UPDATE opportunities SET title=%s,organization=%s,category=%s,last_seen_at=%s,last_verified_at=%s,status=%s,deadline=%s,program_family=%s,cycle_label=%s,cycle_year=%s,content_fingerprint=%s WHERE id=%s""",
            (opportunity.title, opportunity.organization, opportunity.category, now, opportunity.last_verified_at, opportunity.status.value, opportunity.deadline, opportunity.program_family, opportunity.cycle_label, opportunity.cycle_year, fingerprint, database_id))
            if changed:
                cursor.execute("SELECT COALESCE(MAX(version_number),0)+1 AS next_version FROM opportunity_versions WHERE opportunity_id=%s", (database_id,))
                self._insert_version(cursor, database_id, int(cursor.fetchone()["next_version"]), now, fingerprint, snapshot, changed)
        return PersistenceOutcome(opportunity, classification, changed, database_id)

    @staticmethod
    def _insert_version(cursor: Any, opportunity_id: int, version: int, now: datetime, fingerprint: str, snapshot: dict[str, Any], changed: tuple[str, ...]) -> None:
        cursor.execute("INSERT INTO opportunity_versions(opportunity_id,version_number,recorded_at,content_fingerprint,structured_data,changed_fields) VALUES(%s,%s,%s,%s,%s::jsonb,%s::jsonb)", (opportunity_id, version, now, fingerprint, json.dumps(snapshot, sort_keys=True), json.dumps(changed)))

    def mark_digested(self, database_ids: list[int], *, at: datetime | None = None) -> None:
        if not database_ids: return
        timestamp = at or datetime.now(timezone.utc)
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.executemany("UPDATE opportunities SET last_digest_at=%s WHERE id=%s", [(timestamp, item) for item in database_ids])

    def persist_search_candidates(self, candidates: list[SearchCandidate]) -> None:
        if not candidates:
            return
        statement = """INSERT INTO search_candidates(canonical_url,title,snippet,query,query_mode,search_rank,provider,discovered_at,search_score,search_signals,page_shape,page_shape_signals,proceeded_to_extraction,rejection_reason)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb,%s,%s)"""
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.executemany(statement, [
                (canonical_url(item.url), item.title, item.snippet, item.query, item.query_mode.value,
                 item.search_rank, item.provider, item.discovered_at, item.search_score,
                 json.dumps(item.search_signals), item.page_shape.value if item.page_shape else None,
                 json.dumps(item.page_shape_signals), item.proceeded_to_extraction,
                 item.rejection_reason) for item in candidates
            ])

    def persist_search_provenance(self, database_id: int, candidate: SearchCandidate) -> None:
        statement = """INSERT INTO opportunity_search_provenance(opportunity_id,canonical_url,query,query_mode,provider,first_discovered_at,last_discovered_at)
        VALUES(%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT(opportunity_id,canonical_url,query,query_mode,provider)
        DO UPDATE SET last_discovered_at=EXCLUDED.last_discovered_at"""
        with self.connection.transaction(), self.connection.cursor() as cursor:
            provenance = candidate.provenance or []
            rows = [(database_id, canonical_url(candidate.url), item.query, item.query_mode.value,
                     item.provider, candidate.discovered_at, candidate.discovered_at) for item in provenance]
            if not rows:
                rows = [(database_id, canonical_url(candidate.url), candidate.query,
                         candidate.query_mode.value, candidate.provider, candidate.discovered_at,
                         candidate.discovered_at)]
            cursor.executemany(statement, rows)

    def notification_was_sent(self, database_id: int, notification_type: NotificationType, fingerprint: str) -> bool:
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM notification_deliveries WHERE opportunity_id=%s AND notification_type=%s AND triggering_fingerprint=%s AND delivery_status=%s LIMIT 1",
                (database_id, notification_type.value, fingerprint, DeliveryStatus.DELIVERED.value),
            )
            return cursor.fetchone() is not None

    def record_notification_delivery(
        self, database_id: int, notification_type: NotificationType, fingerprint: str,
        status: DeliveryStatus, *, attempted_at: datetime, error: str | None = None,
    ) -> None:
        sent_at = attempted_at if status is DeliveryStatus.DELIVERED else None
        statement = """INSERT INTO notification_deliveries(opportunity_id,notification_type,triggering_fingerprint,first_sent_at,last_sent_at,last_attempted_at,delivery_status,attempts,error)
        VALUES(%s,%s,%s,%s,%s,%s,%s,1,%s)
        ON CONFLICT(opportunity_id,notification_type,triggering_fingerprint) DO UPDATE SET
        first_sent_at=COALESCE(notification_deliveries.first_sent_at,EXCLUDED.first_sent_at),
        last_sent_at=COALESCE(EXCLUDED.last_sent_at,notification_deliveries.last_sent_at),
        last_attempted_at=EXCLUDED.last_attempted_at,delivery_status=EXCLUDED.delivery_status,
        attempts=notification_deliveries.attempts+1,error=EXCLUDED.error"""
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(statement, (database_id, notification_type.value, fingerprint, sent_at, sent_at, attempted_at, status.value, error))

    def notification_chunk_was_sent(self, notification_fingerprint: str, chunk_index: int, chunk_fingerprint: str) -> bool:
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                "SELECT 1 FROM notification_chunks WHERE notification_fingerprint=%s AND chunk_index=%s AND chunk_fingerprint=%s AND delivery_status=%s LIMIT 1",
                (notification_fingerprint, chunk_index, chunk_fingerprint, DeliveryStatus.DELIVERED.value),
            )
            return cursor.fetchone() is not None

    def record_notification_chunk(
        self, notification_fingerprint: str, notification_type: NotificationType,
        chunk_index: int, chunk_count: int, chunk_fingerprint: str,
        status: DeliveryStatus, *, attempted_at: datetime, error: str | None = None,
    ) -> None:
        sent_at = attempted_at if status is DeliveryStatus.DELIVERED else None
        statement = """INSERT INTO notification_chunks(notification_fingerprint,notification_type,chunk_index,chunk_count,chunk_fingerprint,delivery_status,attempts,first_sent_at,last_sent_at,last_attempted_at,error)
        VALUES(%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s)
        ON CONFLICT(notification_fingerprint,chunk_index) DO UPDATE SET
        notification_type=EXCLUDED.notification_type,chunk_count=EXCLUDED.chunk_count,
        chunk_fingerprint=EXCLUDED.chunk_fingerprint,delivery_status=EXCLUDED.delivery_status,
        attempts=notification_chunks.attempts+1,
        first_sent_at=COALESCE(notification_chunks.first_sent_at,EXCLUDED.first_sent_at),
        last_sent_at=COALESCE(EXCLUDED.last_sent_at,notification_chunks.last_sent_at),
        last_attempted_at=EXCLUDED.last_attempted_at,error=EXCLUDED.error"""
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(statement, (
                notification_fingerprint, notification_type.value, chunk_index, chunk_count,
                chunk_fingerprint, status.value, sent_at, sent_at, attempted_at, error,
            ))

    def notification_fingerprint_was_delivered(self, notification_fingerprint: str) -> bool:
        with self.connection.transaction(), self.connection.cursor() as cursor:
            cursor.execute(
                """SELECT COUNT(*) FILTER (WHERE delivery_status=%s) AS delivered,
                          COALESCE(MAX(chunk_count), 0) AS expected
                   FROM notification_chunks WHERE notification_fingerprint=%s""",
                (DeliveryStatus.DELIVERED.value, notification_fingerprint),
            )
            row = cursor.fetchone()
            return bool(row and int(row["expected"]) > 0 and int(row["delivered"]) >= int(row["expected"]))


class InMemoryOpportunityStore:
    """Offline test double implementing the production persistence contract."""

    def __init__(self) -> None:
        self.sources: dict[str, TrustedSource] = {}; self.candidates: list[DiscoveryCandidate] = []
        self.opportunities: dict[str, dict[str, Any]] = {}; self.versions: dict[int, list[dict[str, Any]]] = {}
        self._next_id = 1
        self.search_candidates: list[SearchCandidate] = []
        self.search_provenance: list[dict[str, Any]] = []
        self.notification_deliveries: dict[tuple[int, str, str], dict[str, Any]] = {}
        self.notification_chunks: dict[tuple[str, int], dict[str, Any]] = {}

    @property
    def version_count(self) -> int:
        return sum(len(items) for items in self.versions.values())

    def persist_sources(self, sources: list[TrustedSource], *, checked_at: datetime) -> None:
        del checked_at
        self.sources.update({item.id: item for item in sources})

    def persist_candidates(self, candidates: list[DiscoveryCandidate]) -> None:
        self.candidates.extend(item.model_copy(deep=True) for item in candidates)

    def persist_opportunity(self, opportunity: Opportunity, *, source_id: str, seen_at: datetime | None = None) -> PersistenceOutcome:
        if source_id not in self.sources: raise ValueError(f"unknown source id: {source_id}")
        now = seen_at or datetime.now(timezone.utc); snapshot = meaningful_snapshot(opportunity); fingerprint = content_fingerprint(snapshot)
        canonical, identity_key = _identity(opportunity); record = self.opportunities.get(identity_key)
        previous = self.versions[record["id"]][-1]["snapshot"] if record else None
        classification, changed = _classification(previous, snapshot)
        if record is None:
            database_id = self._next_id; self._next_id += 1
            record = {"id": database_id, "canonical_url": canonical, "first_seen_at": now, "last_digest_at": None}
            self.opportunities[identity_key] = record; self.versions[database_id] = []
        else: database_id = record["id"]
        record.update({"opportunity": opportunity.model_copy(deep=True), "source_id": record.get("source_id", source_id), "last_seen_at": now, "last_verified_at": opportunity.last_verified_at, "status": opportunity.status.value, "deadline": opportunity.deadline, "content_fingerprint": fingerprint})
        if changed: self.versions[database_id].append({"version_number": len(self.versions[database_id]) + 1, "recorded_at": now, "fingerprint": fingerprint, "snapshot": snapshot, "changed_fields": changed})
        return PersistenceOutcome(opportunity, classification, changed, database_id)

    def mark_digested(self, database_ids: list[int], *, at: datetime | None = None) -> None:
        timestamp = at or datetime.now(timezone.utc)
        selected = set(database_ids)
        for record in self.opportunities.values():
            if record["id"] in selected: record["last_digest_at"] = timestamp

    def persist_search_candidates(self, candidates: list[SearchCandidate]) -> None:
        self.search_candidates.extend(item.model_copy(deep=True) for item in candidates)

    def persist_search_provenance(self, database_id: int, candidate: SearchCandidate) -> None:
        provenance = candidate.provenance or []
        keys = [(database_id, canonical_url(candidate.url), item.query, item.query_mode.value, item.provider) for item in provenance]
        if not keys:
            keys = [(database_id, canonical_url(candidate.url), candidate.query, candidate.query_mode.value, candidate.provider)]
        for key in keys:
            existing = next((item for item in self.search_provenance if item["key"] == key), None)
            if existing:
                existing["last_discovered_at"] = candidate.discovered_at
            else:
                self.search_provenance.append({"key": key, "database_id": database_id, "candidate": candidate.model_copy(deep=True), "first_discovered_at": candidate.discovered_at, "last_discovered_at": candidate.discovered_at})

    def notification_was_sent(self, database_id: int, notification_type: NotificationType, fingerprint: str) -> bool:
        record = self.notification_deliveries.get((database_id, notification_type.value, fingerprint))
        return bool(record and record["status"] is DeliveryStatus.DELIVERED)

    def record_notification_delivery(
        self, database_id: int, notification_type: NotificationType, fingerprint: str,
        status: DeliveryStatus, *, attempted_at: datetime, error: str | None = None,
    ) -> None:
        key = (database_id, notification_type.value, fingerprint)
        record = self.notification_deliveries.get(key)
        if record is None:
            record = {"first_sent_at": None, "last_sent_at": None, "attempts": 0}
            self.notification_deliveries[key] = record
        record["attempts"] += 1
        record["last_attempted_at"] = attempted_at
        record["status"] = status
        record["error"] = error
        if status is DeliveryStatus.DELIVERED:
            record["first_sent_at"] = record["first_sent_at"] or attempted_at
            record["last_sent_at"] = attempted_at

    def notification_chunk_was_sent(self, notification_fingerprint: str, chunk_index: int, chunk_fingerprint: str) -> bool:
        record = self.notification_chunks.get((notification_fingerprint, chunk_index))
        return bool(
            record and record["chunk_fingerprint"] == chunk_fingerprint
            and record["status"] is DeliveryStatus.DELIVERED
        )

    def record_notification_chunk(
        self, notification_fingerprint: str, notification_type: NotificationType,
        chunk_index: int, chunk_count: int, chunk_fingerprint: str,
        status: DeliveryStatus, *, attempted_at: datetime, error: str | None = None,
    ) -> None:
        key = (notification_fingerprint, chunk_index)
        record = self.notification_chunks.get(key)
        if record is None:
            record = {"first_sent_at": None, "last_sent_at": None, "attempts": 0}
            self.notification_chunks[key] = record
        record.update({
            "notification_type": notification_type, "chunk_index": chunk_index,
            "chunk_count": chunk_count, "chunk_fingerprint": chunk_fingerprint,
            "status": status, "last_attempted_at": attempted_at, "error": error,
        })
        record["attempts"] += 1
        if status is DeliveryStatus.DELIVERED:
            record["first_sent_at"] = record["first_sent_at"] or attempted_at
            record["last_sent_at"] = attempted_at

    def notification_fingerprint_was_delivered(self, notification_fingerprint: str) -> bool:
        records = [
            record for (fingerprint, _), record in self.notification_chunks.items()
            if fingerprint == notification_fingerprint
        ]
        if not records:
            return False
        expected = max(int(record["chunk_count"]) for record in records)
        delivered = sum(record["status"] is DeliveryStatus.DELIVERED for record in records)
        return delivered >= expected
