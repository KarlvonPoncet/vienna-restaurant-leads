"""Bounded Overpass and reviewed City of Vienna CSV source adapters."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import time
from typing import Any, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import (
    CITY_ATTRIBUTION,
    CITY_LICENSE,
    CITY_LICENSE_URL,
    DEFAULT_USER_AGENT,
    OSM_ATTRIBUTION,
    OSM_LICENSE,
    OSM_LICENSE_URL,
)
from .db import insert_records, insert_source_run, store_payload, utc_now
from .normalize import NormalizedRecord, normalize_row

# This bbox intentionally covers Vienna but does not permit an unbounded query.
VIENNA_BBOX = (48.095, 16.18, 48.34, 16.62)
DEFAULT_OVERPASS_ENDPOINT = "https://overpass-api.de/api/interpreter"
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 45


def overpass_query(bbox: tuple[float, float, float, float] = VIENNA_BBOX) -> str:
    south, west, north, east = bbox
    # nwr is bounded by the explicit bbox and the output is capped to tags and
    # center coordinates; no other directory or geocoder is queried.
    return (
        "[out:json][timeout:40];"
        f'nwr["amenity"="restaurant"]({south},{west},{north},{east});'
        "out center tags;"
    )


def parse_overpass_payload(payload: bytes | str) -> list[NormalizedRecord]:
    """Parse an Overpass JSON response into normalized records."""
    if isinstance(payload, bytes):
        data = json.loads(payload.decode("utf-8-sig"))
    else:
        data = json.loads(payload)
    if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
        raise ValueError("Overpass payload is missing an elements list")
    records: list[NormalizedRecord] = []
    for element in data["elements"]:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags") or {}
        if not isinstance(tags, dict):
            continue
        row: dict[str, Any] = {str(key): value for key, value in tags.items()}
        row["id"] = f"{element.get('type', 'element')}/{element.get('id', len(records))}"
        if "lat" in element:
            row["lat"] = element["lat"]
        if "lon" in element:
            row["lon"] = element["lon"]
        center = element.get("center")
        if isinstance(center, dict):
            row.setdefault("lat", center.get("lat"))
            row.setdefault("lon", center.get("lon"))
        record = normalize_row(row, source_record_key=row["id"])
        # Unnamed OSM objects are not actionable restaurant leads.  Keeping
        # unnamed raw payloads would be useful for a full data warehouse, but
        # the MVP only normalizes bounded named records.
        if record.name:
            records.append(record)
    return records


# Short alias useful to callers and tests.
parse_overpass = parse_overpass_payload


def detect_csv_encoding(payload: bytes) -> str:
    if payload.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    for encoding in ("utf-8", "cp1252", "latin-1"):
        try:
            payload.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    # latin-1 accepts every byte, so this is defensive only.
    return "utf-8"


def parse_city_csv(payload: bytes | str) -> tuple[str, list[dict[str, str]]]:
    """Return the detected encoding and rows from a reviewed CSV payload."""
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    encoding = detect_csv_encoding(raw)
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError as exc:
        raise ValueError("City CSV could not be decoded") from exc
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text, newline=""), dialect=dialect)
    if not reader.fieldnames:
        raise ValueError("City CSV has no header row")
    rows: list[dict[str, str]] = []
    for row in reader:
        rows.append({str(key): (value or "") for key, value in row.items() if key is not None})
    return encoding, rows


# Alias emphasizing that the CSV is not fetched or discovered automatically.
parse_city_top_locations_csv = parse_city_csv


def city_records(payload: bytes | str) -> tuple[str, list[NormalizedRecord]]:
    encoding, rows = parse_city_csv(payload)
    result = []
    for index, row in enumerate(rows, start=2):
        key = next(
            (str(row[key]).strip() for key in row if key and key.casefold() in {"id", "key", "record_id", "schluessel"} and str(row[key]).strip()),
            f"csv-row-{index}",
        )
        record = normalize_row(row, source_record_key=key)
        if record.name:
            result.append(record)
    return encoding, result


def ingest_overpass_bytes(
    connection,
    payload: bytes,
    *,
    source_url: str = DEFAULT_OVERPASS_ENDPOINT,
    user_agent: str = DEFAULT_USER_AGENT,
    cache_path: str = "",
    captured_at: str | None = None,
) -> list[int]:
    records = parse_overpass_payload(payload)
    payload_id = store_payload(
        connection,
        payload,
        content_type="application/json",
        encoding="utf-8",
        captured_at=captured_at,
    )
    source_id = insert_source_run(
        connection,
        source_kind="overpass",
        payload_id=payload_id,
        source_url=source_url,
        attribution=OSM_ATTRIBUTION,
        license=OSM_LICENSE,
        license_url=OSM_LICENSE_URL,
        user_agent=user_agent,
        cache_path=cache_path,
        metadata={"query": overpass_query(), "bounded": True},
        captured_at=captured_at,
    )
    return insert_records(connection, source_id, records, imported_at=captured_at)


def ingest_city_bytes(
    connection,
    payload: bytes,
    *,
    source_url: str = "",
    attribution: str = CITY_ATTRIBUTION,
    license: str = CITY_LICENSE,
    license_url: str = CITY_LICENSE_URL,
    captured_at: str | None = None,
) -> list[int]:
    encoding, records = city_records(payload)
    payload_id = store_payload(
        connection,
        payload,
        content_type="text/csv",
        encoding=encoding,
        captured_at=captured_at,
    )
    source_id = insert_source_run(
        connection,
        source_kind="city_top_locations",
        payload_id=payload_id,
        source_url=source_url,
        attribution=attribution,
        license=license,
        license_url=license_url,
        metadata={"encoding": encoding, "reviewed": True},
        captured_at=captured_at,
    )
    return insert_records(connection, source_id, records, imported_at=captured_at)


def _cache_file(cache_dir: Path, query: str, endpoint: str) -> Path:
    digest = hashlib.sha256(f"{endpoint}\n{query}".encode("utf-8")).hexdigest()[:20]
    return cache_dir / f"overpass-vienna-{digest}.json"


def fetch_overpass_snapshot(
    *,
    cache_dir: str | Path,
    endpoint: str = DEFAULT_OVERPASS_ENDPOINT,
    max_age_seconds: int = 24 * 60 * 60,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> tuple[bytes, Path, bool]:
    """Fetch one bounded, cached snapshot, returning ``(bytes, path, cached)``."""
    if max_bytes <= 0 or timeout <= 0 or max_age_seconds < 0:
        raise ValueError("max_bytes and timeout must be positive; max_age_seconds cannot be negative")
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    query = overpass_query()
    path = _cache_file(directory, query, endpoint)
    if path.exists() and max_age_seconds >= 0:
        age = time.time() - path.stat().st_mtime
        if age <= max_age_seconds:
            payload = path.read_bytes()
            if len(payload) <= max_bytes:
                return payload, path, True
    request = Request(
        endpoint,
        data=query.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
            "User-Agent": user_agent,
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = response.read(min(1024 * 1024, max_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > max_bytes:
                    raise ValueError(f"Overpass response exceeds the {max_bytes}-byte safety limit")
    except HTTPError as exc:
        raise RuntimeError(f"Overpass request failed with HTTP {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError("Overpass request failed; use a cached snapshot or retry later") from exc
    payload = b"".join(chunks)
    if not payload:
        raise RuntimeError("Overpass returned an empty response")
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_bytes(payload)
    temporary.replace(path)
    return payload, path, False


def ingest_overpass_snapshot(
    connection,
    *,
    cache_dir: str | Path,
    endpoint: str = DEFAULT_OVERPASS_ENDPOINT,
    max_age_seconds: int = 24 * 60 * 60,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    user_agent: str = DEFAULT_USER_AGENT,
) -> list[int]:
    payload, path, cached = fetch_overpass_snapshot(
        cache_dir=cache_dir,
        endpoint=endpoint,
        max_age_seconds=max_age_seconds,
        max_bytes=max_bytes,
        timeout=timeout,
        user_agent=user_agent,
    )
    return ingest_overpass_bytes(
        connection,
        payload,
        source_url=endpoint,
        user_agent=user_agent,
        cache_path=str(path),
        captured_at=utc_now(),
    )
