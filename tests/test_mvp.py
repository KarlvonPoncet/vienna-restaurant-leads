from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from vienna_leads import OSM_ATTRIBUTION
from vienna_leads.db import connect_db
from vienna_leads.drafts import render_eml, render_markdown
from vienna_leads.duplicates import generate_duplicate_candidates
from vienna_leads.exports import export_all
from vienna_leads.normalize import normalize_row, normalized_address, normalized_name
from vienna_leads.scoring import confirm_score, score_record, score_records
from vienna_leads.sources import (
    city_records,
    ingest_city_bytes,
    ingest_overpass_bytes,
    parse_city_csv,
    parse_overpass_payload,
)
from vienna_leads.suppression import (
    add_suppression,
    is_suppressed,
    purge_unqualified_contact_data,
)


OVERPASS_FIXTURE = json.dumps(
    {
        "version": 0.6,
        "elements": [
            {
                "type": "node",
                "id": 101,
                "lat": 48.2085,
                "lon": 16.3721,
                "tags": {
                    "amenity": "restaurant",
                    "name": "Café Schön",
                    "addr:street": "Ringstraße",
                    "addr:housenumber": "7",
                    "phone": "0043 1 555 0101",
                },
            },
            {
                "type": "way",
                "id": 202,
                "center": {"lat": 48.21, "lon": 16.38},
                "tags": {
                    "amenity": "restaurant",
                    "name": "Secure Bistro",
                    "website": "https://secure.example",
                },
            },
        ],
    }
).encode()


class MvpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = connect_db(":memory:")
        self.addCleanup(self.connection.close)

    def test_overpass_parsing_and_normalization(self) -> None:
        records = parse_overpass_payload(OVERPASS_FIXTURE)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].source_record_key, "node/101")
        self.assertEqual(records[0].name, "Café Schön")
        self.assertEqual(records[0].phone, "+4315550101")
        self.assertEqual(records[0].address, "Ringstraße 7")
        self.assertEqual(records[1].latitude, 48.21)
        self.assertEqual(normalized_name("Café Schön"), "cafe schon")
        self.assertEqual(normalized_address("Ringstraße 7"), "ringstrasse 7")

    def test_city_csv_encoding_detection_and_normalization(self) -> None:
        payload = "Name;Adresse;Bezirk;Kategorie\nCafé Schön;Ringstraße 7;1;Restaurant\n".encode("cp1252")
        encoding, rows = parse_city_csv(payload)
        self.assertEqual(encoding, "cp1252")
        self.assertEqual(rows[0]["Name"], "Café Schön")
        detected, records = city_records(payload)
        self.assertEqual(detected, "cp1252")
        self.assertEqual(records[0].category, "Restaurant")
        self.assertEqual(records[0].address, "Ringstraße 7")

    def test_provenance_and_raw_payload_are_preserved(self) -> None:
        ids = ingest_overpass_bytes(self.connection, OVERPASS_FIXTURE, captured_at="2024-01-01T00:00:00+00:00")
        self.assertEqual(len(ids), 2)
        payload = self.connection.execute("SELECT raw_payload FROM source_payloads").fetchone()[0]
        self.assertEqual(payload, OVERPASS_FIXTURE)
        provenance = self.connection.execute(
            "SELECT source_kind, attribution, license, source_record_key FROM provenance WHERE record_id = ?",
            (ids[0],),
        ).fetchone()
        self.assertEqual(provenance["source_kind"], "overpass")
        self.assertIn("OpenStreetMap", provenance["attribution"])
        self.assertEqual(provenance["license"], "ODbL 1.0")
        self.assertEqual(provenance["source_record_key"], "node/101")

    def test_conservative_duplicates_are_candidates_not_merges(self) -> None:
        city = "Name,Address,Category\nCafe Schon,Ringstrasse 7,Restaurant\n".encode()
        ingest_overpass_bytes(self.connection, OVERPASS_FIXTURE)
        ingest_city_bytes(self.connection, city)
        created = generate_duplicate_candidates(self.connection)
        self.assertEqual(created, 1)
        candidate = self.connection.execute("SELECT * FROM duplicate_candidates").fetchone()
        self.assertEqual(candidate["status"], "pending")
        self.assertEqual(candidate["method"], "exact_name_address")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM records").fetchone()[0], 3)

    def test_scoring_is_explainable_capped_and_human_confirmable(self) -> None:
        result = score_record({"name": "A", "address": "B", "category": "restaurant", "website": ""})
        self.assertEqual(result.automated_score, 70)
        self.assertIn("website_missing", result.reason_codes)
        self.assertIn("restaurant_business", result.reason_codes)
        self.assertGreaterEqual(result.confidence, 0)
        self.assertLessEqual(result.confidence, 1)

        ids = ingest_overpass_bytes(self.connection, OVERPASS_FIXTURE)
        score_records(self.connection)
        confirm_score(self.connection, ids[0], 88)
        row = self.connection.execute("SELECT * FROM scores WHERE record_id = ?", (ids[0],)).fetchone()
        self.assertEqual(row["automated_score"], 70)
        self.assertEqual(row["score"], 88)
        self.assertEqual(row["human_confirmed"], 1)
        self.assertTrue(json.loads(row["reason_codes_json"]))

    def test_suppression_hashes_and_retention(self) -> None:
        ids = ingest_overpass_bytes(self.connection, OVERPASS_FIXTURE, captured_at="2020-01-01T00:00:00+00:00")
        record = self.connection.execute("SELECT * FROM records WHERE record_id = ?", (ids[0],)).fetchone()
        added = add_suppression(self.connection, record=record, reason="requested")
        self.assertGreaterEqual(added, 1)
        self.assertTrue(is_suppressed(self.connection, record))
        hashes = [row["value_hash"] for row in self.connection.execute("SELECT * FROM suppression_records")]
        self.assertNotIn(record["name"], hashes)

        removed = purge_unqualified_contact_data(
            self.connection,
            now=datetime(2020, 4, 1, tzinfo=timezone.utc),
            retention_days=90,
        )
        self.assertEqual(removed, 1)
        after = self.connection.execute("SELECT phone, email FROM records WHERE record_id = ?", (ids[0],)).fetchone()
        self.assertEqual(after["phone"], "")
        self.assertEqual(after["email"], "")
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM suppression_records").fetchone()[0], len(hashes))

    def test_exports_and_local_drafts_include_safety_and_attribution(self) -> None:
        ids = ingest_overpass_bytes(self.connection, OVERPASS_FIXTURE)
        ingest_city_bytes(self.connection, b"Name,Address,Category\nCity Place,Somewhere,Restaurant\n")
        score_records(self.connection)
        with tempfile.TemporaryDirectory() as directory:
            paths = export_all(self.connection, directory)
            html = paths["html"].read_text(encoding="utf-8")
            csv_text = paths["csv"].read_text(encoding="utf-8")
            markdown = paths["markdown"].read_text(encoding="utf-8")
            data = json.loads(paths["json"].read_text(encoding="utf-8"))
            for text in (html, csv_text, markdown, json.dumps(data)):
                self.assertIn("OpenStreetMap", text)
                self.assertIn("ODbL", text)
                self.assertIn("City of Vienna", text)
                self.assertIn("CC BY 4.0", text)
            record = dict(self.connection.execute("SELECT * FROM records WHERE record_id = ?", (ids[0],)).fetchone())
            score = dict(self.connection.execute("SELECT * FROM scores WHERE record_id = ?", (ids[0],)).fetchone())
            score["reason_codes"] = json.loads(score.pop("reason_codes_json"))
            draft = render_markdown(record, score=score)
            self.assertIn("not sent automatically", draft)
            self.assertNotIn("tracking pixel", draft.casefold())
            eml = render_eml(record, sender="review@example.invalid", recipient="restaurant@example.invalid", score=score)
            self.assertIn("From: review@example.invalid", eml)
            self.assertIn("X-Vienna-Restaurant-Leads-Draft: local-only; not sent", eml)


if __name__ == "__main__":
    unittest.main()
