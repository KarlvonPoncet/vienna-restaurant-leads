"""Conservative normalization shared by all source adapters.

Normalization is deliberately lossy only for matching fields.  The complete
source row is stored separately as JSON, so a reviewer can always inspect the
original spelling and values.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
import unicodedata
from urllib.parse import urlsplit
from typing import Any, Mapping


@dataclass(frozen=True)
class NormalizedRecord:
    source_record_key: str
    name: str
    address: str = ""
    district: str = ""
    category: str = ""
    phone: str = ""
    email: str = ""
    website: str = ""
    latitude: float | None = None
    longitude: float | None = None
    raw: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value).replace("\x00", " ")).strip()


def fold_text(value: Any) -> str:
    """Return a stable, accent-insensitive value for comparisons."""
    text = unicodedata.normalize("NFKD", clean_text(value))
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return text.casefold()


def match_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", fold_text(value))


def normalized_name(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", fold_text(value)).strip()


def normalized_address(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", " ", fold_text(value)).strip()


def normalize_phone(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    plus = text.startswith("+")
    digits = re.sub(r"\D", "", text)
    if digits.startswith("00"):
        digits = digits[2:]
        plus = True
    if len(digits) < 6:
        return ""
    return ("+" if plus else "") + digits


def normalize_email(value: Any) -> str:
    text = clean_text(value).casefold()
    if not text or "@" not in text or any(ch.isspace() for ch in text):
        return ""
    local, _, domain = text.rpartition("@")
    if not local or "." not in domain or domain.startswith("."):
        return ""
    return f"{local}@{domain}"


def normalize_website(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    # Do not accept values that could become a dangerous non-HTTP link in an
    # export.  A missing scheme is common in reviewed CSVs.
    candidate = text if "://" in text else f"https://{text}"
    parsed = urlsplit(candidate)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return ""
    return candidate


def parse_coordinate(value: Any) -> float | None:
    text = clean_text(value)
    if not text:
        return None
    try:
        number = float(text.replace(",", "."))
    except ValueError:
        return None
    return number if -180 <= number <= 180 else None


def _first(row: Mapping[str, Any], *aliases: str) -> str:
    keyed = {match_key(key): value for key, value in row.items()}
    for alias in aliases:
        value = keyed.get(match_key(alias))
        if clean_text(value):
            return clean_text(value)
    return ""


def normalize_row(row: Mapping[str, Any], source_record_key: str | None = None) -> NormalizedRecord:
    """Normalize a generic source row while retaining all source values in ``raw``."""
    raw = {str(key): value for key, value in row.items()}
    key = source_record_key or _first(row, "id", "osm_id", "object_id", "record_id", "schluessel", "key")
    if not key:
        key = normalized_name(_first(row, "name", "restaurant", "title", "bezeichnung")) or "row"
    street = _first(row, "addr:street", "street", "strasse", "straße", "address_street")
    house = _first(row, "addr:housenumber", "housenumber", "house_number", "hausnummer")
    address = _first(row, "address", "adresse", "addr", "full_address")
    if not address:
        address = " ".join(part for part in (street, house) if part)
    lat = parse_coordinate(_first(row, "latitude", "lat", "y"))
    lon = parse_coordinate(_first(row, "longitude", "lon", "lng", "x"))
    # Latitude/longitude may be passed as numbers and therefore be consumed by
    # clean_text just fine.  Coordinate ranges are checked separately below.
    if lat is not None and not -90 <= lat <= 90:
        lat = None
    return NormalizedRecord(
        source_record_key=clean_text(key),
        name=_first(row, "name", "restaurant", "title", "bezeichnung", "lokalname"),
        address=address,
        district=_first(row, "district", "bezirk", "suburb", "addr:suburb"),
        category=_first(row, "category", "type", "amenity", "branche", "rubrik", "kategorie"),
        phone=normalize_phone(_first(row, "phone", "telephone", "tel", "telefon")),
        email=normalize_email(_first(row, "email", "e-mail", "mail")),
        website=normalize_website(_first(row, "website", "url", "homepage", "webseite")),
        latitude=lat,
        longitude=lon,
        raw=raw,
    )


def identity_key(record: Mapping[str, Any] | NormalizedRecord) -> str:
    """Build a durable business identity key without retaining contact data."""
    if isinstance(record, NormalizedRecord):
        name, address = record.name, record.address
    else:
        keys = record.keys()
        name = record["name"] if "name" in keys else ""
        address = record["address"] if "address" in keys else ""
    return f"{normalized_name(name)}|{normalized_address(address)}"


def website_domain(value: Any) -> str:
    text = normalize_website(value)
    if not text:
        return ""
    return (urlsplit(text).hostname or "").casefold().removeprefix("www.")
