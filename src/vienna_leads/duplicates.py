"""Conservative duplicate candidate generation; never merges records."""

from __future__ import annotations

from difflib import SequenceMatcher
import json
import math
import sqlite3
from typing import Any

from .db import utc_now
from .normalize import normalized_address, normalized_name, website_domain


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _distance_meters(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    # Equirectangular approximation is sufficiently conservative at Vienna's
    # latitude and avoids a dependency for this review-only candidate pass.
    lat = math.radians((lat_a + lat_b) / 2)
    x = math.radians(lon_b - lon_a) * math.cos(lat)
    y = math.radians(lat_b - lat_a)
    return 6_371_000 * math.sqrt(x * x + y * y)


def candidate_for_pair(left: sqlite3.Row, right: sqlite3.Row) -> tuple[str, float, list[str]] | None:
    name_a = normalized_name(left["name"])
    name_b = normalized_name(right["name"])
    address_a = normalized_address(left["address"])
    address_b = normalized_address(right["address"])
    reasons: list[str] = []
    confidence = 0.0
    method = ""

    if name_a and address_a and name_a == name_b and address_a == address_b:
        method, confidence = "exact_name_address", 0.99
        reasons.append("same normalized name and address")
    else:
        phone_a = left["phone"] or ""
        phone_b = right["phone"] or ""
        domain_a = website_domain(left["website"])
        domain_b = website_domain(right["website"])
        name_similarity = _similarity(name_a, name_b)
        if phone_a and phone_a == phone_b and name_similarity >= 0.82:
            method, confidence = "same_phone_similar_name", 0.96
            reasons.append("same normalized phone and similar name")
        elif domain_a and domain_a == domain_b and name_similarity >= 0.82:
            method, confidence = "same_website_similar_name", 0.94
            reasons.append("same website domain and similar name")
        elif (
            left["latitude"] is not None
            and left["longitude"] is not None
            and right["latitude"] is not None
            and right["longitude"] is not None
            and name_similarity >= 0.88
            and _distance_meters(left["latitude"], left["longitude"], right["latitude"], right["longitude"]) <= 100
        ):
            method, confidence = "nearby_similar_name", 0.90
            reasons.append("similar name within 100 metres")
        elif name_a and name_a == name_b and address_a and address_b:
            address_similarity = _similarity(address_a, address_b)
            if address_similarity >= 0.88:
                method, confidence = "same_name_similar_address", 0.91
                reasons.append("same normalized name and similar address")
    if method:
        if left["source_id"] != right["source_id"]:
            reasons.append("records come from different source runs")
        return method, confidence, reasons
    return None


def generate_duplicate_candidates(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        """SELECT record_id, source_id, name, address, phone, website, latitude, longitude
           FROM records ORDER BY record_id"""
    ).fetchall()
    created = 0
    for index, left in enumerate(rows):
        for right in rows[index + 1 :]:
            if left["source_id"] == right["source_id"]:
                continue
            result = candidate_for_pair(left, right)
            if result is None:
                continue
            method, confidence, reasons = result
            a, b = sorted((int(left["record_id"]), int(right["record_id"])))
            cursor = connection.execute(
                """INSERT OR IGNORE INTO duplicate_candidates
                   (record_a, record_b, method, confidence, reasons_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (a, b, method, confidence, json.dumps(reasons, ensure_ascii=False), utc_now()),
            )
            created += cursor.rowcount
    return created
