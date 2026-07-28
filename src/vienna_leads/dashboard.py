"""Dependency-free local review dashboard.

The server is intentionally hard-bound to 127.0.0.1.  It serves a small
inline HTML/JavaScript application and JSON routes from the local SQLite
file; it has no network integrations, telemetry, or external assets.
"""

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .db import connect_db
from .drafts import (
    TEMPLATE_IDS,
    render_template,
    render_template_html,
    template_catalog,
    write_template_eml_draft,
    write_template_markdown_draft,
)
from .exports import export_data

HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_POST_BYTES = 64 * 1024
SORT_FIELDS = {"id", "name", "score", "confidence", "source"}


class DashboardNotFound(LookupError):
    pass


class DashboardConflict(ValueError):
    pass


def _record_score(connection: sqlite3.Connection, record_id: int) -> dict[str, Any] | None:
    row = connection.execute("SELECT * FROM scores WHERE record_id = ?", (record_id,)).fetchone()
    if row is None:
        return None
    try:
        reasons = json.loads(row["reason_codes_json"])
    except json.JSONDecodeError:
        reasons = []
    try:
        explanations = json.loads(row["explanation_json"])
    except json.JSONDecodeError:
        explanations = []
    return {
        "model_version": row["model_version"],
        "automated_score": row["automated_score"],
        "score": row["score"],
        "reason_codes": reasons,
        "explanations": explanations,
        "confidence": row["confidence"],
        "human_confirmed": bool(row["human_confirmed"]),
        "scored_at": row["scored_at"],
    }


def _draft_root(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def _relative_draft(root: Path, relative_name: str) -> Path:
    """Resolve a draft name while rejecting absolute and traversal paths."""
    relative = Path(unquote(relative_name))
    if not relative_name or relative.is_absolute() or ".." in relative.parts:
        raise DashboardNotFound("draft path is outside the local draft directory")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise DashboardNotFound("draft path is outside the local draft directory") from exc
    return candidate


def list_local_drafts(draft_dir: str | Path) -> list[dict[str, Any]]:
    root = _draft_root(draft_dir)
    if not root.exists():
        return []
    drafts: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in {".md", ".eml"}:
            continue
        relative = path.relative_to(root).as_posix()
        drafts.append(
            {
                "name": relative,
                "format": path.suffix.casefold().removeprefix("."),
                "bytes": path.stat().st_size,
                "url": "/drafts/" + quote(relative, safe="/"),
            }
        )
    return drafts


def _record_status(record: Mapping[str, Any]) -> str:
    if record.get("suppressed"):
        return "suppressed"
    if record.get("human_confirmed"):
        return "human_confirmed"
    if record.get("score") is not None:
        return "automated_review"
    return "unscored"


def _with_review_status(record: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(record)
    result["review_status"] = _record_status(result)
    return result


def _sorted_leads(records: list[dict[str, Any]], sort_field: str, descending: bool) -> list[dict[str, Any]]:
    if sort_field not in SORT_FIELDS:
        raise ValueError(f"sort must be one of: {', '.join(sorted(SORT_FIELDS))}")
    field_map = {"id": "record_id", "source": "source_kind"}
    field = field_map.get(sort_field, sort_field)
    present = [record for record in records if record.get(field) is not None]
    missing = [record for record in records if record.get(field) is None]
    present.sort(key=lambda record: (str(record.get(field, "")).casefold() if field in {"name", "source_kind"} else record.get(field)), reverse=descending)
    # Missing scores/confidence stay at the bottom in either direction.
    return present + missing


def list_dashboard_leads(connection: sqlite3.Connection, params: Mapping[str, list[str]]) -> list[dict[str, Any]]:
    data = export_data(connection)
    records = [_with_review_status(record) for record in data["records"]]

    def first(key: str, default: str = "") -> str:
        values = params.get(key, [default])
        return values[0] if values else default

    query = first("q").strip().casefold()
    source = first("source").strip().casefold()
    suppressed = first("suppressed", "all").strip().casefold()
    review = first("review", "all").strip().casefold()
    if suppressed not in {"all", "0", "1"}:
        raise ValueError("suppressed must be all, 0, or 1")
    if review not in {"all", "human_confirmed", "automated_review", "unscored"}:
        raise ValueError("review must be all, human_confirmed, automated_review, or unscored")
    min_score_text = first("min_score").strip()
    max_score_text = first("max_score").strip()
    min_score = float(min_score_text) if min_score_text else None
    max_score = float(max_score_text) if max_score_text else None

    filtered: list[dict[str, Any]] = []
    for record in records:
        haystack = " ".join(str(record.get(key) or "") for key in ("name", "address", "website", "category")).casefold()
        if query and query not in haystack:
            continue
        if source and str(record.get("source_kind") or "").casefold() != source:
            continue
        if suppressed != "all" and bool(record.get("suppressed")) != (suppressed == "1"):
            continue
        if review != "all" and record["review_status"] != review:
            continue
        score = record.get("score")
        if min_score is not None and (score is None or score < min_score):
            continue
        if max_score is not None and (score is None or score > max_score):
            continue
        filtered.append(record)

    sort_field = first("sort", "score")
    direction = first("direction", "desc").casefold()
    if direction not in {"asc", "desc"}:
        raise ValueError("direction must be asc or desc")
    filtered = _sorted_leads(filtered, sort_field, direction == "desc")
    limit_text = first("limit", "200")
    try:
        limit = int(limit_text)
    except ValueError as exc:
        raise ValueError("limit must be an integer") from exc
    if not 1 <= limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    return filtered[:limit]


def _lead_detail(connection: sqlite3.Connection, draft_dir: str | Path, record_id: int) -> dict[str, Any]:
    data = export_data(connection)
    record = next((item for item in data["records"] if int(item["record_id"]) == record_id), None)
    if record is None:
        raise DashboardNotFound(f"unknown lead: {record_id}")
    detail = _with_review_status(record)
    detail["provenance"] = [
        dict(row)
        for row in connection.execute(
            """SELECT source_id, source_record_key, source_kind, source_url,
                      attribution, license, captured_at
               FROM provenance WHERE record_id = ? ORDER BY source_id""",
            (record_id,),
        )
    ]
    detail["duplicate_candidates"] = [
        dict(row)
        for row in connection.execute(
            """SELECT candidate_id, record_a, record_b, method, confidence,
                      reasons_json, status, created_at
               FROM duplicate_candidates
               WHERE record_a = ? OR record_b = ? ORDER BY candidate_id""",
            (record_id, record_id),
        )
    ]
    detail["drafts"] = list_local_drafts(draft_dir)
    return detail


def dashboard_templates() -> list[dict[str, str]]:
    catalog = template_catalog()
    if len(catalog) != 3 or tuple(item["template_id"] for item in catalog) != TEMPLATE_IDS:
        raise RuntimeError("dashboard template catalog must contain exactly three templates")
    return catalog


def _load_lead_for_draft(connection: sqlite3.Connection, record_id: int) -> tuple[dict[str, Any], dict[str, Any] | None]:
    data = export_data(connection)
    record = next((item for item in data["records"] if int(item["record_id"]) == record_id), None)
    if record is None:
        raise DashboardNotFound(f"unknown lead: {record_id}")
    if record["suppressed"]:
        raise DashboardConflict("suppressed leads cannot produce proposal drafts")
    score = _record_score(connection, record_id)
    return record, score


def save_dashboard_draft(
    connection: sqlite3.Connection,
    draft_dir: str | Path,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        record_id = int(payload.get("lead_id"))
    except (TypeError, ValueError) as exc:
        raise ValueError("lead_id must be an integer") from exc
    template_id = str(payload.get("template") or "")
    if template_id not in TEMPLATE_IDS:
        raise ValueError(f"template must be one of: {', '.join(TEMPLATE_IDS)}")
    output_format = str(payload.get("format") or "md").casefold()
    if output_format not in {"md", "eml"}:
        raise ValueError("format must be md or eml")
    record, score = _load_lead_for_draft(connection, record_id)
    root = _draft_root(draft_dir)
    root.mkdir(parents=True, exist_ok=True)
    output = root / f"lead-{record_id}-{template_id}.{output_format}"
    if output_format == "md":
        write_template_markdown_draft(output, template_id, record, score=score)
    else:
        recipient = str(payload.get("recipient") or "").strip()
        sender = str(payload.get("sender") or "local-review@example.invalid").strip()
        if not recipient:
            raise ValueError("recipient is required for an .eml draft; no address is guessed")
        write_template_eml_draft(
            output,
            template_id,
            record,
            sender=sender,
            recipient=recipient,
            score=score,
        )
    relative = output.relative_to(root).as_posix()
    return {
        "name": relative,
        "format": output_format,
        "path": str(output),
        "url": "/drafts/" + quote(relative, safe="/"),
        "delivery": "none",
        "local_only": True,
    }


def dashboard_html() -> str:
    """Return the self-contained dashboard shell (no data or external URLs)."""
    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Vienna Restaurant Leads — local dashboard</title>
<style>
:root{color-scheme:light;--ink:#202124;--muted:#5f6368;--line:#d9dce1;--accent:#174ea6;--warn:#fff4ce}
body{font:15px system-ui,sans-serif;line-height:1.4;max-width:1440px;margin:0 auto;padding:1.25rem;color:var(--ink)}
h1{margin:.2rem 0}.subtitle,.muted{color:var(--muted)}.notice{background:var(--warn);border:1px solid #e4c65c;padding:.8rem;margin:1rem 0;border-radius:5px}
.controls{display:flex;flex-wrap:wrap;gap:.5rem;align-items:end;padding:.75rem 0}.controls label{display:flex;flex-direction:column;font-size:.85rem;color:var(--muted)}input,select,button{font:inherit;padding:.35rem .5rem;border:1px solid #aeb4bc;border-radius:4px;background:#fff}button{cursor:pointer;color:var(--accent);font-weight:600}button:hover{background:#eef3fd}
.layout{display:grid;grid-template-columns:minmax(0,1.25fr) minmax(320px,.75fr);gap:1rem}.panel{border:1px solid var(--line);border-radius:5px;padding:1rem;min-width:0}table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid var(--line);padding:.45rem;text-align:left;vertical-align:top}th{background:#f5f6f7;position:sticky;top:0}.table-wrap{max-height:66vh;overflow:auto}.score{font-weight:700}.suppressed{color:#8a1c1c}.tag{display:inline-block;border-radius:3px;background:#eef0f2;padding:.1rem .3rem;margin:.1rem}.detail-grid{display:grid;grid-template-columns:max-content 1fr;gap:.25rem .75rem}.detail-grid dt{font-weight:700}.detail-grid dd{margin:0;overflow-wrap:anywhere}pre{white-space:pre-wrap;background:#f5f6f7;padding:.75rem;max-height:30rem;overflow:auto}.draft-actions{display:flex;flex-wrap:wrap;gap:.4rem;align-items:end}.draft-actions label{display:flex;flex-direction:column;font-size:.8rem;color:var(--muted)}.email-preview{border:1px solid var(--line);background:#eef0f2;padding:.75rem;margin-top:.75rem}.email-preview iframe{display:block;width:100%;min-height:460px;border:1px solid var(--line);background:#fff}.email-preview pre{background:#fff;max-height:460px;margin:.75rem 0 0 0}
@media(max-width:900px){.layout{grid-template-columns:1fr}}
</style>
</head>
<body>
<header><h1>Vienna Restaurant Leads</h1><p class="subtitle">Interactive local review dashboard — bound to <code>127.0.0.1</code>, with no telemetry or remote assets.</p></header>
<div class="notice"><strong>Safety boundary:</strong> this dashboard reads the local SQLite database and writes local drafts only. It never sends email. Automated scores remain capped at 70 until human confirmation; duplicate candidates are not merges. Review attribution, provenance, suppression, and recipient details before any action.</div>
<section class="controls" aria-label="Lead filters">
<label>Search <input id="q" type="search" placeholder="name, address, website"></label>
<label>Minimum score <input id="min-score" type="number" min="0" max="100"></label>
<label>Maximum score <input id="max-score" type="number" min="0" max="100"></label>
<label>Source <select id="source"><option value="">All sources</option><option value="overpass">Overpass</option><option value="city_top_locations">City Top Locations</option></select></label>
<label>Review <select id="review"><option value="all">All review states</option><option value="human_confirmed">Human confirmed</option><option value="automated_review">Automated review</option><option value="unscored">Unscored</option></select></label>
<label>Suppression <select id="suppressed"><option value="all">All suppression states</option><option value="0">Not suppressed</option><option value="1">Suppressed</option></select></label>
<label>Sort <select id="sort"><option value="score">Score</option><option value="name">Name</option><option value="confidence">Confidence</option><option value="source">Source</option><option value="id">ID</option></select></label>
<label>Direction <select id="direction"><option value="desc">Descending</option><option value="asc">Ascending</option></select></label>
<button id="refresh" type="button">Refresh</button>
</section>
<div class="layout">
<section class="panel"><h2>Leads <span id="count" class="muted"></span></h2><div class="table-wrap"><table><thead><tr><th>ID</th><th>Name</th><th>Address</th><th>Score</th><th>Review</th><th>Source</th><th></th></tr></thead><tbody id="leads"></tbody></table></div></section>
<aside class="panel" id="detail"><h2>Select a lead</h2><p class="muted">Lead score explanations, provenance, duplicate candidates, suppression status, and local drafts will appear here.</p></aside>
</div>
<section class="panel"><h2>Duplicate candidates</h2><p class="muted">Suggestions only; no records are merged automatically.</p><div class="table-wrap"><table><thead><tr><th>ID</th><th>Records</th><th>Method</th><th>Confidence</th><th>Status</th></tr></thead><tbody id="duplicates"></tbody></table></div></section>
<script>
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let selectedLead = null;
  let templates = [];
  const text = (node, value) => { node.textContent = value == null ? "" : String(value); return node; };
  const cell = (row, value) => row.appendChild(text(document.createElement("td"), value));
  const params = () => {
    const p = new URLSearchParams();
    ["q", "min-score", "max-score", "source", "review", "suppressed", "sort", "direction"].forEach((id) => {
      const value = $(id).value;
      if (value) p.set(id.replace("-", "_"), value);
    });
    return p;
  };
  async function json(url, options) {
    const response = await fetch(url, options);
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || "Dashboard request failed");
    return body;
  }
  function renderLeads(body) {
    const target = $("leads"); target.replaceChildren(); text($("count"), `(${body.count})`);
    body.leads.forEach((lead) => {
      const row = document.createElement("tr");
      cell(row, lead.record_id); cell(row, lead.name || "Unnamed"); cell(row, lead.address || "—");
      cell(row, lead.score == null ? "—" : `${lead.score}/100`); cell(row, lead.review_status);
      cell(row, lead.source_kind);
      const action = document.createElement("td"); const button = document.createElement("button");
      button.type = "button"; text(button, "View"); button.addEventListener("click", () => loadDetail(lead.record_id)); action.appendChild(button); row.appendChild(action); target.appendChild(row);
    });
    if (!body.leads.length) { const row = document.createElement("tr"); const empty = document.createElement("td"); empty.colSpan = 7; text(empty, "No leads match these filters."); row.appendChild(empty); target.appendChild(row); }
  }
  function addDetailValue(list, label, value) { const dt = document.createElement("dt"); text(dt, label); const dd = document.createElement("dd"); text(dd, value || "—"); list.append(dt, dd); }
  function linkList(parent, items) { (items || []).forEach((item) => { const a = document.createElement("a"); a.href = item.url; a.target = "_blank"; a.rel = "noreferrer"; text(a, `${item.name} (${item.format})`); parent.appendChild(a); parent.appendChild(document.createTextNode(" ")); }); }
  function renderTemplateControls(detail) {
    const section = document.createElement("section"); section.className = "draft-actions";
    const templateLabel = document.createElement("label"); text(templateLabel, "Template"); const select = document.createElement("select"); select.id = "template";
    templates.forEach((template) => { const option = document.createElement("option"); option.value = template.template_id; text(option, template.name); select.appendChild(option); }); templateLabel.appendChild(select); section.appendChild(templateLabel);
    const formatLabel = document.createElement("label"); text(formatLabel, "Format"); const format = document.createElement("select"); format.id = "draft-format";
    [["md", "Markdown"], ["eml", "RFC 5322 .eml"]].forEach(([value, label]) => { const option = document.createElement("option"); option.value = value; text(option, label); format.appendChild(option); }); formatLabel.appendChild(format); section.appendChild(formatLabel);
    const recipientLabel = document.createElement("label"); text(recipientLabel, "Explicit .eml recipient (optional for Markdown)"); const recipient = document.createElement("input"); recipient.id = "recipient"; recipient.placeholder = "name@example.invalid"; recipientLabel.appendChild(recipient); section.appendChild(recipientLabel);
    const preview = document.createElement("button"); preview.type = "button"; text(preview, "Preview"); preview.addEventListener("click", async () => { try { const body = await json(`/api/draft-preview/${detail.record_id}?template=${encodeURIComponent(select.value)}`); const frame = $("draft-html-preview"); frame.srcdoc = body.html; text($("draft-source"), body.markdown); frame.hidden = false; $("draft-source").hidden = true; } catch (error) { text($("draft-source"), error.message); $("draft-source").hidden = false; } }); section.appendChild(preview);
    const save = document.createElement("button"); save.type = "button"; text(save, "Save local draft"); save.addEventListener("click", async () => { try { const body = await json("/api/drafts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lead_id: detail.record_id, template: select.value, format: format.value, recipient: recipient.value }) }); text($("draft-result"), `Saved locally: ${body.name}`); await loadDetail(detail.record_id); } catch (error) { text($("draft-result"), error.message); } }); section.appendChild(save);
    return section;
  }
  async function loadDetail(id) {
    try {
      const detail = await json(`/api/leads/${id}`); selectedLead = detail; const target = $("detail"); target.replaceChildren();
      const heading = document.createElement("h2"); text(heading, `${detail.name || "Unnamed"} (#${detail.record_id})`); target.appendChild(heading);
      const list = document.createElement("dl"); list.className = "detail-grid";
      addDetailValue(list, "Review status", detail.review_status); addDetailValue(list, "Score", detail.score == null ? "unscored" : `${detail.score}/100`); addDetailValue(list, "Automated score", detail.automated_score == null ? "—" : `${detail.automated_score}/70`); addDetailValue(list, "Confidence", detail.confidence); addDetailValue(list, "Website", detail.website); addDetailValue(list, "Phone", detail.phone); addDetailValue(list, "Email", detail.email); addDetailValue(list, "Address", detail.address); target.appendChild(list);
      const h3 = document.createElement("h3"); text(h3, "Score explanation"); target.appendChild(h3); const reasons = document.createElement("pre"); text(reasons, JSON.stringify({ reason_codes: detail.reason_codes, explanations: detail.explanations }, null, 2)); target.appendChild(reasons);
      const provH = document.createElement("h3"); text(provH, "Provenance"); target.appendChild(provH); const prov = document.createElement("pre"); text(prov, JSON.stringify(detail.provenance, null, 2)); target.appendChild(prov);
      const dupH = document.createElement("h3"); text(dupH, "Duplicate candidates"); target.appendChild(dupH); const dup = document.createElement("pre"); text(dup, JSON.stringify(detail.duplicate_candidates, null, 2)); target.appendChild(dup);
      const draftH = document.createElement("h3"); text(draftH, "Local proposal drafts"); target.appendChild(draftH); const draftLinks = document.createElement("p"); linkList(draftLinks, detail.drafts); target.appendChild(draftLinks);
      target.appendChild(renderTemplateControls(detail));
      const previewBox = document.createElement("section"); previewBox.id = "draft-preview"; previewBox.className = "email-preview";
      const previewHeading = document.createElement("h4"); text(previewHeading, "Rendered email preview"); previewBox.appendChild(previewHeading);
      const frame = document.createElement("iframe"); frame.id = "draft-html-preview"; frame.title = "Rendered email preview"; frame.setAttribute("sandbox", ""); frame.srcdoc = '<p style="font-family:Arial,sans-serif;padding:16px">Choose Preview to render the selected email template.</p>'; previewBox.appendChild(frame);
      const toggle = document.createElement("button"); toggle.type = "button"; text(toggle, "Show plain-text/source"); toggle.addEventListener("click", () => { const source = $("draft-source"); source.hidden = !source.hidden; frame.hidden = !source.hidden; toggle.textContent = source.hidden ? "Show plain-text/source" : "Show rendered HTML"; }); previewBox.appendChild(toggle);
      const source = document.createElement("pre"); source.id = "draft-source"; source.hidden = true; text(source, ""); previewBox.appendChild(source); target.appendChild(previewBox);
      const result = document.createElement("p"); result.id = "draft-result"; target.appendChild(result);
    } catch (error) { text($("detail"), error.message); }
  }
  async function loadDuplicates() { const body = await json("/api/duplicates"); const target = $("duplicates"); target.replaceChildren(); body.duplicates.forEach((item) => { const row = document.createElement("tr"); cell(row, item.candidate_id); cell(row, `${item.record_a} ↔ ${item.record_b}`); cell(row, item.method); cell(row, item.confidence); cell(row, item.status); target.appendChild(row); }); }
  async function refresh() { try { renderLeads(await json(`/api/leads?${params().toString()}`)); await loadDuplicates(); } catch (error) { text($("count"), error.message); } }
  async function init() { templates = (await json("/api/templates")).templates; await refresh(); }
  $("refresh").addEventListener("click", refresh); ["q", "min-score", "max-score", "source", "review", "suppressed", "sort", "direction"].forEach((id) => $(id).addEventListener("change", refresh));
  init().catch((error) => text($("count"), error.message));
})();
</script>
</body>
</html>
"""


class DashboardHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, db_path: str | Path, draft_dir: str | Path, port: int = DEFAULT_PORT):
        # The host is intentionally not configurable: this must never become a
        # LAN-facing or hosted review service.
        super().__init__((HOST, port), DashboardRequestHandler)
        self.db_path = str(db_path)
        self.draft_dir = str(draft_dir)


def create_server(db_path: str | Path, draft_dir: str | Path = "drafts", port: int = DEFAULT_PORT) -> DashboardHTTPServer:
    if not 0 <= port <= 65535:
        raise ValueError("port must be between 0 and 65535")
    return DashboardHTTPServer(db_path, draft_dir, port)


class DashboardRequestHandler(BaseHTTPRequestHandler):
    server: DashboardHTTPServer
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the dashboard quiet; it is a local review UI, not an access log.
        return

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: Mapping[str, Any]) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"error": message, "local_only": True})

    def _connection(self) -> sqlite3.Connection:
        return connect_db(self.server.db_path)

    @staticmethod
    def _path_id(path: str, prefix: str) -> int:
        value = path[len(prefix):]
        if not value or "/" in value or not value.isdecimal():
            raise DashboardNotFound("lead path must contain a numeric ID")
        return int(value)

    def do_GET(self) -> None:  # noqa: N802 - standard library handler API
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if path == "/":
                self._send(HTTPStatus.OK, dashboard_html().encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/health":
                self._json(HTTPStatus.OK, {"status": "ok", "local_only": True, "host": HOST, "telemetry": False})
                return
            if path == "/api/templates":
                self._json(HTTPStatus.OK, {"templates": dashboard_templates(), "count": 3, "local_only": True})
                return
            if path == "/api/leads":
                connection = self._connection()
                try:
                    leads = list_dashboard_leads(connection, query)
                finally:
                    connection.close()
                self._json(HTTPStatus.OK, {"leads": leads, "count": len(leads), "local_only": True})
                return
            if path.startswith("/api/leads/"):
                record_id = self._path_id(path, "/api/leads/")
                connection = self._connection()
                try:
                    detail = _lead_detail(connection, self.server.draft_dir, record_id)
                finally:
                    connection.close()
                self._json(HTTPStatus.OK, detail)
                return
            if path == "/api/duplicates":
                connection = self._connection()
                try:
                    rows = connection.execute(
                        """SELECT candidate_id, record_a, record_b, method, confidence,
                                  reasons_json, status, created_at
                           FROM duplicate_candidates ORDER BY candidate_id"""
                    ).fetchall()
                    duplicates = []
                    for row in rows:
                        item = dict(row)
                        try:
                            item["reasons"] = json.loads(item.pop("reasons_json"))
                        except json.JSONDecodeError:
                            item["reasons"] = []
                        duplicates.append(item)
                finally:
                    connection.close()
                self._json(HTTPStatus.OK, {"duplicates": duplicates, "count": len(duplicates), "local_only": True})
                return
            if path == "/api/draft-preview":
                self._error(HTTPStatus.BAD_REQUEST, "draft preview requires a lead path: /api/draft-preview/<id>")
                return
            if path.startswith("/api/draft-preview/"):
                record_id = self._path_id(path, "/api/draft-preview/")
                template_id = (query.get("template") or [""])[0]
                if template_id not in TEMPLATE_IDS:
                    raise ValueError(f"template must be one of: {', '.join(TEMPLATE_IDS)}")
                connection = self._connection()
                try:
                    record, score = _load_lead_for_draft(connection, record_id)
                    markdown = render_template(template_id, record, score=score)
                    html = render_template_html(template_id, record, score=score)
                finally:
                    connection.close()
                self._json(HTTPStatus.OK, {"template": template_id, "lead_id": record_id, "markdown": markdown, "html": html, "preview_format": "text/html", "delivery": "none", "local_only": True})
                return
            if path == "/api/drafts":
                drafts = list_local_drafts(self.server.draft_dir)
                self._json(HTTPStatus.OK, {"drafts": drafts, "count": len(drafts), "local_only": True})
                return
            if path.startswith("/drafts/"):
                relative_name = path[len("/drafts/"):]
                candidate = _relative_draft(_draft_root(self.server.draft_dir), relative_name)
                if not candidate.is_file():
                    raise DashboardNotFound("draft not found")
                self._send(HTTPStatus.OK, candidate.read_bytes(), "text/plain; charset=utf-8")
                return
            raise DashboardNotFound("route not found")
        except DashboardNotFound as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc))
        except DashboardConflict as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
        except (ValueError, OSError, sqlite3.Error) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))

    def do_POST(self) -> None:  # noqa: N802 - standard library handler API
        if urlsplit(self.path).path != "/api/drafts":
            self._error(HTTPStatus.NOT_FOUND, "route not found")
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "Content-Length must be an integer")
            return
        if length < 0 or length > MAX_POST_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "draft request is too large")
            return
        try:
            raw = self.rfile.read(length)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("draft request must be a JSON object")
            connection = self._connection()
            try:
                result = save_dashboard_draft(connection, self.server.draft_dir, payload)
                connection.commit()
            finally:
                connection.close()
            self._json(HTTPStatus.CREATED, result)
        except DashboardNotFound as exc:
            self._error(HTTPStatus.NOT_FOUND, str(exc))
        except DashboardConflict as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, OSError, sqlite3.Error) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))


def serve_dashboard(
    db_path: str | Path,
    draft_dir: str | Path = "drafts",
    *,
    port: int = DEFAULT_PORT,
) -> None:
    server = create_server(db_path, draft_dir, port)
    print(f"dashboard: http://{HOST}:{server.server_port}/")
    print("delivery: none; press Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
