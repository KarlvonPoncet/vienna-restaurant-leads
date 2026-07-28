"""Opt-out suppression and minimal contact retention helpers."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
from typing import Any, Mapping
from urllib.parse import urlsplit

from .db import utc_now
from .normalize import identity_key, normalize_email, normalize_phone, website_domain


def suppression_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _record_keys(record: Mapping[str, Any]) -> list[tuple[str, str]]:
    record = dict(record)
    keys: list[tuple[str, str]] = []
    business = identity_key(record)
    if business != "|":
        keys.append(("business", business))
    website = website_domain(record.get("website", ""))
    if website:
        keys.append(("website_domain", website))
    email = normalize_email(record.get("email", ""))
    if email:
        keys.append(("email", email))
    phone = normalize_phone(record.get("phone", ""))
    if phone:
        keys.append(("phone", phone))
    return keys


def add_suppression(
    connection: sqlite3.Connection,
    *,
    record: Mapping[str, Any] | None = None,
    value: str | None = None,
    kind: str = "business",
    reason: str = "opt-out",
) -> int:
    """Store only hashes; return the number of newly inserted suppression rows."""
    if record is None and not value:
        raise ValueError("a record or value is required for suppression")
    pairs = _record_keys(record) if record is not None else [(kind, value.strip().casefold())]
    inserted = 0
    for key_kind, raw_value in pairs:
        if not raw_value:
            continue
        cursor = connection.execute(
            """INSERT OR IGNORE INTO suppression_records
               (kind, value_hash, reason, created_at) VALUES (?, ?, ?, ?)""",
            (key_kind, suppression_hash(raw_value), reason, utc_now()),
        )
        inserted += cursor.rowcount
    if inserted == 0:
        raise ValueError("the record has no stable business or contact identity to suppress")
    return inserted


def is_suppressed(connection: sqlite3.Connection, record: Mapping[str, Any]) -> bool:
    for kind, raw_value in _record_keys(record):
        found = connection.execute(
            "SELECT 1 FROM suppression_records WHERE kind = ? AND value_hash = ? LIMIT 1",
            (kind, suppression_hash(raw_value)),
        ).fetchone()
        if found:
            return True
    return False


def purge_unqualified_contact_data(
    connection: sqlite3.Connection,
    *,
    now: datetime | None = None,
    retention_days: int = 90,
) -> int:
    """Remove derived phone/email after 90 days unless a reviewer qualified it.

    Raw source payloads and provenance remain immutable.  Suppression rows are
    hashes only and are never removed by this cleanup, so an opt-out still
    applies when a fresh snapshot is imported.
    """
    if retention_days < 0:
        raise ValueError("retention_days cannot be negative")
    current = now or datetime.now(timezone.utc)
    cutoff = (current - timedelta(days=retention_days)).replace(microsecond=0).isoformat()
    rows = connection.execute(
        "SELECT record_id, normalized_json FROM records WHERE imported_at < ? AND contact_qualified_at IS NULL AND (phone <> '' OR email <> '')",
        (cutoff,),
    ).fetchall()
    for row in rows:
        try:
            normalized = json.loads(row["normalized_json"])
        except (TypeError, json.JSONDecodeError):
            normalized = {}
        normalized.pop("phone", None)
        normalized.pop("email", None)
        connection.execute(
            """UPDATE records SET phone = '', email = '', normalized_json = ?
               WHERE record_id = ?""",
            (json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")), row["record_id"]),
        )
    return len(rows)


def qualify_contact(connection: sqlite3.Connection, record_id: int) -> None:
    if connection.execute("SELECT 1 FROM records WHERE record_id = ?", (record_id,)).fetchone() is None:
        raise ValueError(f"unknown record: {record_id}")
    connection.execute(
        "UPDATE records SET contact_qualified_at = ? WHERE record_id = ?",
        (utc_now(), record_id),
    )
