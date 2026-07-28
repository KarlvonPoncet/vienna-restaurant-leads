"""SQLite schema and persistence for the local-only MVP."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator, Mapping

from . import (
    CITY_ATTRIBUTION,
    CITY_LICENSE,
    CITY_LICENSE_URL,
    OSM_ATTRIBUTION,
    OSM_LICENSE,
    OSM_LICENSE_URL,
)
from .normalize import NormalizedRecord

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS source_payloads (
    payload_id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE,
    content_type TEXT NOT NULL DEFAULT 'application/octet-stream',
    encoding TEXT,
    raw_payload BLOB NOT NULL,
    captured_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_runs (
    source_id INTEGER PRIMARY KEY,
    source_kind TEXT NOT NULL,
    source_url TEXT,
    attribution TEXT NOT NULL,
    license TEXT NOT NULL,
    license_url TEXT,
    captured_at TEXT NOT NULL,
    user_agent TEXT,
    cache_path TEXT,
    payload_id INTEGER NOT NULL REFERENCES source_payloads(payload_id),
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS records (
    record_id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES source_runs(source_id),
    source_record_key TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    normalized_json TEXT NOT NULL,
    name TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    district TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL DEFAULT '',
    phone TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    website TEXT NOT NULL DEFAULT '',
    latitude REAL,
    longitude REAL,
    imported_at TEXT NOT NULL,
    contact_qualified_at TEXT,
    UNIQUE(source_id, source_record_key)
);

CREATE TABLE IF NOT EXISTS provenance (
    record_id INTEGER NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES source_runs(source_id),
    source_record_key TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_url TEXT,
    attribution TEXT NOT NULL,
    license TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    PRIMARY KEY(record_id, source_id)
);

CREATE TABLE IF NOT EXISTS duplicate_candidates (
    candidate_id INTEGER PRIMARY KEY,
    record_a INTEGER NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
    record_b INTEGER NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
    method TEXT NOT NULL,
    confidence REAL NOT NULL,
    reasons_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK(status IN ('pending', 'confirmed', 'dismissed')),
    created_at TEXT NOT NULL,
    UNIQUE(record_a, record_b)
);

CREATE TABLE IF NOT EXISTS scores (
    record_id INTEGER PRIMARY KEY REFERENCES records(record_id) ON DELETE CASCADE,
    model_version TEXT NOT NULL,
    automated_score INTEGER NOT NULL CHECK(automated_score BETWEEN 0 AND 70),
    score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
    reason_codes_json TEXT NOT NULL,
    explanation_json TEXT NOT NULL,
    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
    human_confirmed INTEGER NOT NULL DEFAULT 0 CHECK(human_confirmed IN (0, 1)),
    scored_at TEXT NOT NULL
);

-- This table intentionally contains only hashes and an opt-out reason.  The
-- original value is never retained here.
CREATE TABLE IF NOT EXISTS suppression_records (
    suppression_id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    value_hash TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT 'opt-out',
    created_at TEXT NOT NULL,
    UNIQUE(kind, value_hash)
);

CREATE INDEX IF NOT EXISTS idx_records_source ON records(source_id);
CREATE INDEX IF NOT EXISTS idx_records_imported ON records(imported_at);
CREATE INDEX IF NOT EXISTS idx_duplicate_status ON duplicate_candidates(status);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect_db(path: str | Path) -> sqlite3.Connection:
    db_path = Path(path)
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.executescript(SCHEMA)
    return connection


@contextmanager
def open_db(path: str | Path) -> Iterator[sqlite3.Connection]:
    connection = connect_db(path)
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def store_payload(
    connection: sqlite3.Connection,
    payload: bytes,
    *,
    content_type: str,
    encoding: str | None,
    captured_at: str | None = None,
) -> int:
    digest = hashlib.sha256(payload).hexdigest()
    captured_at = captured_at or utc_now()
    connection.execute(
        """INSERT OR IGNORE INTO source_payloads
           (sha256, content_type, encoding, raw_payload, captured_at)
           VALUES (?, ?, ?, ?, ?)""",
        (digest, content_type, encoding, payload, captured_at),
    )
    row = connection.execute(
        "SELECT payload_id FROM source_payloads WHERE sha256 = ?", (digest,)
    ).fetchone()
    assert row is not None
    return int(row["payload_id"])


def insert_source_run(
    connection: sqlite3.Connection,
    *,
    source_kind: str,
    payload_id: int,
    source_url: str,
    attribution: str,
    license: str,
    license_url: str = "",
    user_agent: str = "",
    cache_path: str = "",
    metadata: Mapping[str, Any] | None = None,
    captured_at: str | None = None,
) -> int:
    cursor = connection.execute(
        """INSERT INTO source_runs
           (source_kind, source_url, attribution, license, license_url,
            captured_at, user_agent, cache_path, payload_id, metadata_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            source_kind,
            source_url,
            attribution,
            license,
            license_url,
            captured_at or utc_now(),
            user_agent,
            cache_path,
            payload_id,
            json_dumps(metadata or {}),
        ),
    )
    return int(cursor.lastrowid)


def insert_records(
    connection: sqlite3.Connection,
    source_id: int,
    records: Iterable[NormalizedRecord],
    *,
    imported_at: str | None = None,
) -> list[int]:
    source = connection.execute(
        "SELECT source_kind, source_url, attribution, license, captured_at FROM source_runs WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    if source is None:
        raise ValueError(f"unknown source run: {source_id}")
    imported_at = imported_at or utc_now()
    record_ids: list[int] = []
    for record in records:
        raw = record.raw if record.raw is not None else record.as_dict()
        normalized = record.as_dict()
        connection.execute(
            """INSERT INTO records
               (source_id, source_record_key, raw_json, normalized_json, name,
                address, district, category, phone, email, website, latitude,
                longitude, imported_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(source_id, source_record_key) DO UPDATE SET
                 raw_json = excluded.raw_json,
                 normalized_json = excluded.normalized_json,
                 name = excluded.name, address = excluded.address,
                 district = excluded.district, category = excluded.category,
                 phone = excluded.phone, email = excluded.email,
                 website = excluded.website, latitude = excluded.latitude,
                 longitude = excluded.longitude""",
            (
                source_id,
                record.source_record_key,
                json_dumps(raw),
                json_dumps(normalized),
                record.name,
                record.address,
                record.district,
                record.category,
                record.phone,
                record.email,
                record.website,
                record.latitude,
                record.longitude,
                imported_at,
            ),
        )
        row = connection.execute(
            "SELECT record_id FROM records WHERE source_id = ? AND source_record_key = ?",
            (source_id, record.source_record_key),
        ).fetchone()
        assert row is not None
        record_id = int(row["record_id"])
        record_ids.append(record_id)
        connection.execute(
            """INSERT OR REPLACE INTO provenance
               (record_id, source_id, source_record_key, source_kind,
                source_url, attribution, license, captured_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                record_id,
                source_id,
                record.source_record_key,
                source["source_kind"],
                source["source_url"],
                source["attribution"],
                source["license"],
                source["captured_at"],
            ),
        )
    return record_ids


def source_attributions(connection: sqlite3.Connection) -> list[dict[str, str]]:
    rows = connection.execute(
        """SELECT DISTINCT source_kind, attribution, license, license_url, source_url
           FROM source_runs ORDER BY source_kind"""
    ).fetchall()
    result = [dict(row) for row in rows]
    # Generated output must retain both required notices even when one source
    # has not yet been imported into a fresh database.
    has_osm_notice = any(row["license"] == OSM_LICENSE and "OpenStreetMap" in row["attribution"] for row in result)
    has_city_notice = any(row["license"] == CITY_LICENSE and "City of Vienna" in row["attribution"] for row in result)
    if not has_osm_notice:
        result.append(
            {
                "source_kind": "overpass",
                "attribution": OSM_ATTRIBUTION,
                "license": OSM_LICENSE,
                "license_url": OSM_LICENSE_URL,
                "source_url": "",
            }
        )
    if not has_city_notice:
        result.append(
            {
                "source_kind": "city_top_locations",
                "attribution": CITY_ATTRIBUTION,
                "license": CITY_LICENSE,
                "license_url": CITY_LICENSE_URL,
                "source_url": "",
            }
        )
    return result
