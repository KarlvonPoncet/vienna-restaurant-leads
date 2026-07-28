from __future__ import annotations

from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from vienna_leads.dashboard import create_server
from vienna_leads.db import connect_db
from vienna_leads.drafts import TEMPLATE_IDS, render_eml, render_template_html
from vienna_leads.scoring import score_records
from vienna_leads.sources import ingest_city_bytes
from vienna_leads.suppression import add_suppression


class DashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.db_path = root / "review.sqlite3"
        self.draft_dir = root / "drafts"
        connection = connect_db(self.db_path)
        ingest_city_bytes(
            connection,
            b"Name,Address,Category,Website\nTest Bistro,Example Street 1,Restaurant,https://example.invalid\n",
        )
        score_records(connection)
        connection.commit()
        connection.close()
        self.server = create_server(self.db_path, self.draft_dir, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self._stop_server)

    def _stop_server(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.temp_dir.cleanup()

    def _get(self, path: str):
        return urlopen(self.base_url + path, timeout=2)

    def _json_get(self, path: str) -> dict:
        with self._get(path) as response:
            self.assertEqual(response.status, 200)
            return json.loads(response.read().decode("utf-8"))

    def _post(self, path: str, payload: dict):
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        return urlopen(request, timeout=2)

    def test_dashboard_html_is_self_contained_and_local_only(self) -> None:
        with self._get("/") as response:
            html = response.read().decode("utf-8")
        self.assertIn("Vienna Restaurant Leads", html)
        self.assertIn("127.0.0.1", html)
        self.assertIn("Safety boundary", html)
        self.assertNotIn("<script src=", html)
        self.assertNotIn("https://", html)
        self.assertIn("/api/templates", html)
        self.assertIn("draft-html-preview", html)
        self.assertIn("Show plain-text/source", html)
        self.assertIn("Save local draft", html)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    def test_key_routes_filter_detail_provenance_and_drafts(self) -> None:
        health = self._json_get("/api/health")
        self.assertEqual(health["local_only"], True)
        self.assertEqual(health["telemetry"], False)

        leads = self._json_get("/api/leads?q=Test%20Bistro&sort=name&direction=asc")
        self.assertEqual(leads["count"], 1)
        self.assertEqual(leads["leads"][0]["name"], "Test Bistro")
        self.assertEqual(leads["leads"][0]["review_status"], "automated_review")

        detail = self._json_get("/api/leads/1")
        self.assertEqual(detail["record_id"], 1)
        self.assertTrue(detail["explanations"])
        self.assertEqual(detail["provenance"][0]["source_kind"], "city_top_locations")
        self.assertIn("duplicate_candidates", detail)
        self.assertEqual(detail["drafts"], [])

        duplicates = self._json_get("/api/duplicates")
        self.assertEqual(duplicates["count"], 0)

    def test_exactly_three_templates_are_selectable_and_generate_distinct_drafts(self) -> None:
        catalog = self._json_get("/api/templates")
        self.assertEqual(catalog["count"], 3)
        self.assertEqual(len(catalog["templates"]), 3)
        self.assertEqual(
            {item["template_id"] for item in catalog["templates"]},
            {"friendly-refresh", "practical-visibility", "premium-concept"},
        )
        self.assertEqual(len({item["subject"] for item in catalog["templates"]}), 3)

        drafts = []
        for template_id in sorted({item["template_id"] for item in catalog["templates"]}):
            preview = self._json_get(f"/api/draft-preview/1?template={quote(template_id)}")
            self.assertIn("Test Bistro", preview["markdown"])
            self.assertIn("https://example.invalid", preview["markdown"])
            self.assertIn("opt out", preview["markdown"])
            self.assertIn("## Explicit review evidence", preview["markdown"])
            self.assertNotIn("<!doctype html>", preview["markdown"])
            self.assertEqual(preview["preview_format"], "text/html")
            self.assertIn("<!doctype html>", preview["html"])
            self.assertIn("Test Bistro", preview["html"])
            self.assertNotIn("<script", preview["html"].casefold())
            drafts.append(preview["markdown"])
            with self._post("/api/drafts", {"lead_id": 1, "template": template_id, "format": "md"}) as response:
                self.assertEqual(response.status, 201)
                result = json.loads(response.read().decode("utf-8"))
                self.assertEqual(result["delivery"], "none")
        self.assertEqual(len(set(drafts)), 3)
        self.assertEqual(len(list(self.draft_dir.glob("*.md"))), 3)

        with self._post(
            "/api/drafts",
            {
                "lead_id": 1,
                "template": "premium-concept",
                "format": "eml",
                "recipient": "reviewed@example.invalid",
            },
        ) as response:
            self.assertEqual(response.status, 201)
            eml_result = json.loads(response.read().decode("utf-8"))
        eml = (self.draft_dir / eml_result["name"]).read_text(encoding="utf-8")
        self.assertIn("To: reviewed@example.invalid", eml)
        self.assertIn("X-Vienna-Restaurant-Leads-Draft: local-only; not sent", eml)
        listed = self._json_get("/api/drafts")
        self.assertEqual(listed["count"], 4)
        with self._get(eml_result["url"]) as response:
            self.assertEqual(response.status, 200)
            self.assertIn("local-only; not sent", response.read().decode("utf-8"))

    def test_html_escapes_lead_values_and_eml_is_multipart_alternative(self) -> None:
        record = {
            "name": '<img src=x onerror="alert(1)">',
            "address": '<b>unsafe address</b>',
            "website": 'https://example.invalid/?q=1&x=2',
        }
        score = {"score": 70, "reason_codes": ["<derived-code>"]}
        html = render_template_html(TEMPLATE_IDS[0], record, score=score)
        self.assertIn("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;", html)
        self.assertIn("&lt;b&gt;unsafe address&lt;/b&gt;", html)
        self.assertNotIn('<img src=x onerror="alert(1)">', html)
        self.assertNotIn("<script", html.casefold())
        self.assertNotIn("<style", html.casefold())
        self.assertNotIn("url(", html.casefold())

        eml = render_eml(
            record,
            sender="reviewer@example.invalid",
            recipient="restaurant@example.invalid",
            score=score,
        )
        message = BytesParser(policy=policy.default).parsebytes(eml.encode("utf-8"))
        self.assertTrue(message.is_multipart())
        self.assertEqual(message.get_content_subtype(), "alternative")
        self.assertEqual(
            {part.get_content_type() for part in message.iter_parts()},
            {"text/plain", "text/html"},
        )
        html_part = next(part for part in message.iter_parts() if part.get_content_type() == "text/html")
        self.assertIn("&lt;img", html_part.get_content())
        self.assertIn("local-only; not sent", message["X-Vienna-Restaurant-Leads-Draft"])

    def test_safety_boundary_rejects_traversal_and_suppressed_drafts(self) -> None:
        with self.assertRaises(HTTPError) as context:
            self._get("/drafts/%2e%2e%2fREADME.md")
        self.assertEqual(context.exception.code, 404)

        connection = connect_db(self.db_path)
        record = connection.execute("SELECT * FROM records WHERE record_id = 1").fetchone()
        add_suppression(connection, record=record, reason="dashboard test opt-out")
        connection.commit()
        connection.close()
        with self.assertRaises(HTTPError) as context:
            self._post("/api/drafts", {"lead_id": 1, "template": "friendly-refresh", "format": "md"})
        self.assertEqual(context.exception.code, 409)
        self.assertFalse(self.draft_dir.exists())

        with self.assertRaises(HTTPError) as context:
            self._get("/api/draft-preview/1?template=not-a-template")
        self.assertEqual(context.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
