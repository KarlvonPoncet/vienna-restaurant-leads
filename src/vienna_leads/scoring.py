"""Versioned, explainable website-opportunity scoring."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from urllib.parse import urlsplit
import sqlite3
from typing import Any, Mapping, Sequence

from . import SCORE_MODEL_VERSION
from .db import utc_now
from .normalize import website_domain

SOCIAL_DOMAINS = {
    "facebook.com",
    "instagram.com",
    "tiktok.com",
    "linkedin.com",
    "youtube.com",
}


@dataclass(frozen=True)
class ScoreResult:
    automated_score: int
    reason_codes: tuple[str, ...]
    explanations: tuple[dict[str, Any], ...]
    confidence: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _is_restaurant(category: str) -> bool:
    value = (category or "").casefold()
    return any(token in value for token in ("restaurant", "restaurant", "gastronomie", "gaststätte", "gaststaette"))


def score_record(record: Mapping[str, Any], *, source_count: int = 1) -> ScoreResult:
    """Score only business/web-presence fields, with an automated ceiling of 70.

    The rules intentionally reward an observable opportunity rather than
    claiming a site's quality.  No personal or inferred traits enter this
    function, and no URL is fetched.
    """
    record = dict(record)
    website = str(record.get("website") or "").strip()
    category = str(record.get("category") or "").strip()
    score = 0
    codes: list[str] = []
    explanations: list[dict[str, Any]] = []

    def add(code: str, points: int, explanation: str) -> None:
        nonlocal score
        score += points
        codes.append(code)
        explanations.append({"code": code, "points": points, "explanation": explanation})

    if not website:
        add("website_missing", 60, "No website was present in the reviewed business fields.")
    else:
        domain = website_domain(website)
        if domain in SOCIAL_DOMAINS or any(domain.endswith("." + social) for social in SOCIAL_DOMAINS):
            add("social_only_presence", 45, "The only web presence is a social profile, not a standalone site.")
        else:
            scheme = urlsplit(website if "://" in website else f"https://{website}").scheme.casefold()
            if scheme == "http":
                add("http_only_presence", 20, "A website is listed but uses HTTP rather than HTTPS.")
            else:
                add("website_present", 5, "A standalone HTTPS website is listed; opportunity is therefore lower.")

    if _is_restaurant(category):
        add("restaurant_business", 10, "The source classifies this as a restaurant business.")
    elif not category:
        add("category_missing", -5, "No business category was present in the reviewed record.")
    else:
        codes.append("business_category_unconfirmed")
        explanations.append(
            {
                "code": "business_category_unconfirmed",
                "points": 0,
                "explanation": "The business category was not explicitly identified as a restaurant.",
            }
        )

    for field in ("name", "address", "phone", "email"):
        if not str(record.get(field) or "").strip():
            add(f"{field}_missing", -5, f"No {field} was present in the reviewed business fields.")

    # Confidence describes the completeness of the observable fields, not the
    # likelihood that a person will respond.
    observed = sum(bool(str(record.get(field) or "").strip()) for field in ("name", "address", "category", "phone", "email"))
    confidence = 0.25 + observed * 0.12 + min(max(source_count, 1), 3) * 0.08
    if website:
        confidence += 0.12
    confidence = round(min(confidence, 0.95), 2)
    return ScoreResult(min(max(score, 0), 70), tuple(codes), tuple(explanations), confidence)


def score_records(connection: sqlite3.Connection, record_ids: Sequence[int] | None = None) -> int:
    if record_ids:
        placeholders = ",".join("?" for _ in record_ids)
        rows = connection.execute(
            f"SELECT * FROM records WHERE record_id IN ({placeholders}) ORDER BY record_id",
            tuple(record_ids),
        ).fetchall()
    else:
        rows = connection.execute("SELECT * FROM records ORDER BY record_id").fetchall()
    count = 0
    for row in rows:
        source_count = connection.execute(
            "SELECT COUNT(*) FROM provenance WHERE record_id = ?", (row["record_id"],)
        ).fetchone()[0]
        result = score_record(row, source_count=int(source_count))
        existing = connection.execute(
            "SELECT human_confirmed, score FROM scores WHERE record_id = ?", (row["record_id"],)
        ).fetchone()
        human_confirmed = int(existing["human_confirmed"]) if existing else 0
        final_score = int(existing["score"]) if existing and human_confirmed else result.automated_score
        connection.execute(
            """INSERT INTO scores
               (record_id, model_version, automated_score, score,
                reason_codes_json, explanation_json, confidence, human_confirmed, scored_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(record_id) DO UPDATE SET
                 model_version = excluded.model_version,
                 automated_score = excluded.automated_score,
                 score = excluded.score,
                 reason_codes_json = excluded.reason_codes_json,
                 explanation_json = excluded.explanation_json,
                 confidence = excluded.confidence,
                 scored_at = excluded.scored_at""",
            (
                row["record_id"],
                SCORE_MODEL_VERSION,
                result.automated_score,
                final_score,
                json.dumps(result.reason_codes, ensure_ascii=False),
                json.dumps(result.explanations, ensure_ascii=False),
                result.confidence,
                human_confirmed,
                utc_now(),
            ),
        )
        count += 1
    return count


def confirm_score(connection: sqlite3.Connection, record_id: int, score: int) -> None:
    if not 0 <= score <= 100:
        raise ValueError("confirmed score must be between 0 and 100")
    row = connection.execute("SELECT record_id FROM records WHERE record_id = ?", (record_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown record: {record_id}")
    if connection.execute("SELECT 1 FROM scores WHERE record_id = ?", (record_id,)).fetchone() is None:
        score_records(connection, [record_id])
    connection.execute(
        "UPDATE scores SET score = ?, human_confirmed = 1, scored_at = ? WHERE record_id = ?",
        (score, utc_now(), record_id),
    )
