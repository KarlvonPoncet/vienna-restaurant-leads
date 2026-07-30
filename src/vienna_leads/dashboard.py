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
<title>Vienna Restaurant Leads — local review workspace</title>
<style>
:root{color-scheme:light;--ink:#292321;--muted:#746a64;--faint:#a49a92;--paper:#f4f0eb;--surface:#fffdfa;--line:#ded6ce;--strong:#c9beb4;--accent:#8f2943;--dark:#6f1e33;--soft:#f4e5e9;--gold:#b56d22;--gold-soft:#fbefdc;--green:#2f6654;--green-soft:#e7f0eb;--danger:#9a3030;--danger-soft:#fae8e5;--shadow:0 14px 34px rgba(55,39,29,.08);font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
*{box-sizing:border-box}html{min-width:0;background:var(--paper)}body{min-width:0;margin:0;overflow-x:hidden;color:var(--ink);background:var(--paper);font-size:15px;line-height:1.5}button,input,select{font:inherit}button{cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible,summary:focus-visible,a:focus-visible{outline:3px solid #e5a348;outline-offset:2px}a{color:var(--dark)}
.skip-link{position:fixed;z-index:3;left:1rem;top:-4rem;padding:.65rem .9rem;color:#fff;background:var(--dark);border-radius:0 0 8px 8px;font-weight:750}.skip-link:focus{top:0}.shell{width:min(100% - 2rem,1440px);margin:auto;padding:1.25rem 0 3rem}
.masthead{display:flex;align-items:flex-end;justify-content:space-between;gap:1.5rem;padding:1rem 0 1.5rem;border-bottom:1px solid var(--strong)}.eyebrow{margin:0 0 .5rem;color:var(--accent);font-size:.7rem;font-weight:800;letter-spacing:.16em;text-transform:uppercase}h1,h2,h3,h4,p{margin-top:0}h1{max-width:700px;margin-bottom:.35rem;font-family:Georgia,"Times New Roman",serif;font-size:clamp(2.1rem,4vw,3.8rem);font-weight:600;letter-spacing:-.045em;line-height:.98}h2{margin-bottom:.3rem;font-size:1.25rem;letter-spacing:-.015em}h3{margin-bottom:.5rem;font-size:1rem}.subtitle{max-width:720px;margin:0;color:var(--muted);font-size:1rem}.muted,.field-help{color:var(--muted)}.field-help{font-size:.7rem}
.trust-list{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:.45rem;margin:0;padding:0;list-style:none}.trust-list li,.tag{display:inline-flex;align-items:center;gap:.35rem;padding:.4rem .65rem;color:var(--green);background:var(--green-soft);border:1px solid #c9dfd3;border-radius:999px;font-size:.76rem;font-weight:750;white-space:nowrap}.trust-list li::before{content:"•";font-size:1.1em}.safety-note{display:grid;grid-template-columns:auto minmax(0,1fr);gap:.85rem;align-items:start;margin:1.25rem 0;padding:1rem 1.1rem;color:#5d401c;background:var(--gold-soft);border:1px solid #ead2a9;border-left:4px solid var(--gold);border-radius:14px}.notice-mark{display:grid;width:1.7rem;height:1.7rem;place-items:center;color:#fff;background:var(--gold);border-radius:50%;font-weight:900}.safety-note p{margin:0}.safety-note strong{color:#4c3217}
.summary-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:.75rem;margin-bottom:1rem}.stat-card{padding:1rem 1.1rem;background:var(--surface);border:1px solid var(--line);border-top:3px solid var(--accent);border-radius:14px;box-shadow:var(--shadow)}.stat-card:nth-child(2){border-top-color:var(--gold)}.stat-card:nth-child(3){border-top-color:var(--green)}.stat-card:nth-child(4){border-top-color:var(--strong)}.stat-label{margin-bottom:.3rem;color:var(--muted);font-size:.72rem;font-weight:800;letter-spacing:.08em;text-transform:uppercase}.stat-value{margin:0;font-family:Georgia,"Times New Roman",serif;font-size:2rem;line-height:1}.stat-note{margin:.35rem 0 0;color:var(--muted);font-size:.78rem}
.panel{min-width:0;background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow)}.filters-panel,.lead-panel,.detail-panel,.duplicates-panel{padding:1.1rem}.filters-panel{margin-bottom:1rem}.panel-heading,.filter-heading{display:flex;align-items:baseline;justify-content:space-between;gap:1rem;margin-bottom:.9rem}.panel-heading p,.filter-heading p{margin:0;color:var(--muted);font-size:.83rem}.filter-form{display:grid;grid-template-columns:minmax(210px,2fr) repeat(5,minmax(125px,1fr));gap:.7rem;align-items:end}.field{display:flex;min-width:0;flex-direction:column;gap:.35rem}.field-label{color:var(--muted);font-size:.72rem;font-weight:800;letter-spacing:.06em;text-transform:uppercase}.field input,.field select{width:100%;min-height:2.65rem;padding:.55rem .7rem;color:var(--ink);background:#fff;border:1px solid var(--strong);border-radius:8px}.field input:hover,.field select:hover{border-color:var(--accent)}.field input::placeholder{color:var(--faint)}.filter-actions{display:flex;flex-wrap:wrap;align-items:center;gap:.5rem;margin-top:.85rem}.button{min-height:2.65rem;padding:.55rem .85rem;color:var(--dark);background:#fff;border:1px solid var(--strong);border-radius:8px;font-weight:800}.button:hover{background:var(--soft);border-color:var(--accent)}.button-primary{color:#fff;background:var(--accent);border-color:var(--accent)}.button-primary:hover{color:#fff;background:var(--dark)}.button-quiet{color:var(--muted);border-color:transparent;background:transparent}.button-quiet:hover{color:var(--dark);background:var(--soft)}.button-small{min-height:2.25rem;padding:.4rem .7rem;font-size:.82rem}.button:disabled{cursor:wait;opacity:.55}.status-line{min-height:1.35rem;margin:.65rem 0 0;color:var(--muted);font-size:.82rem}.status-line[data-tone=error],.error-message{color:var(--danger)}.status-line[data-tone=success]{color:var(--green)}
.workspace{display:grid;grid-template-columns:minmax(0,1.1fr) minmax(350px,.9fr);gap:1rem;align-items:start}.detail-panel{position:sticky;top:1rem;max-height:calc(100vh - 2rem);overflow:auto}.count{color:var(--muted);font-size:.8rem;font-weight:500;white-space:nowrap}.lead-list{display:grid;gap:.65rem}.lead-card{display:grid;grid-template-columns:minmax(0,1fr) minmax(130px,.45fr) auto;gap:1rem;align-items:center;min-width:0;padding:.85rem .9rem;background:#fff;border:1px solid var(--line);border-left:3px solid transparent;border-radius:10px;transition:border-color .15s ease,box-shadow .15s ease,transform .15s ease}.lead-card:hover{border-color:var(--strong);box-shadow:0 8px 20px rgba(55,39,29,.08);transform:translateY(-1px)}.lead-card.is-selected{border-left-color:var(--accent);box-shadow:0 0 0 2px var(--soft)}.lead-card-top{display:flex;min-width:0;align-items:start;gap:.55rem}.lead-identity{min-width:0}.lead-name{margin:0;overflow:hidden;font-size:1rem;font-weight:800;text-overflow:ellipsis;white-space:nowrap}.lead-id{margin:.12rem 0 0;color:var(--faint);font-size:.72rem}.lead-meta{display:flex;flex-wrap:wrap;gap:.25rem .6rem;margin:.45rem 0 0;color:var(--muted);font-size:.78rem}.lead-meta span{overflow-wrap:anywhere}.score-line{display:flex;align-items:baseline;justify-content:space-between;gap:.45rem;margin-bottom:.3rem}.score-number{color:var(--dark);font-family:Georgia,"Times New Roman",serif;font-size:1.25rem;font-weight:700}.score-denom{color:var(--muted);font-size:.7rem}.score-meter{width:100%;height:.45rem;overflow:hidden;background:#ece6df;border-radius:99px}.score-fill{height:100%;background:var(--accent);border-radius:inherit}.score-fill.watch{background:var(--gold)}.score-fill.stable{background:var(--green)}.score-help{margin:.3rem 0 0;color:var(--muted);font-size:.7rem}.status-badge{display:inline-flex;align-items:center;width:fit-content;padding:.25rem .45rem;color:var(--muted);background:#f2eeea;border:1px solid var(--line);border-radius:999px;font-size:.68rem;font-weight:800;letter-spacing:.025em;text-transform:uppercase}.status-badge.confirmed{color:var(--green);background:var(--green-soft);border-color:#c9dfd3}.status-badge.suppressed{color:var(--danger);background:var(--danger-soft);border-color:#e7c1bb}.status-badge.automated{color:var(--dark);background:var(--soft);border-color:#e7c5ce}
.empty-state,.loading-state,.error-state{padding:2.5rem 1.25rem;color:var(--muted);text-align:center;border:1px dashed var(--strong);border-radius:10px}.empty-state h3,.error-state h3{margin-bottom:.35rem;color:var(--ink)}.empty-state p,.error-state p{max-width:34rem;margin:0 auto .9rem}.empty-state.compact{padding:1.5rem}.skeleton-card{height:6.2rem;background:#f1ece6;border:1px solid var(--line);border-radius:10px}
.detail-header{display:flex;align-items:start;justify-content:space-between;gap:.75rem;padding-bottom:.9rem;border-bottom:1px solid var(--line)}.detail-title{margin-bottom:.3rem;font-family:Georgia,"Times New Roman",serif;font-size:1.6rem;line-height:1.05}.detail-subtitle{margin:0;color:var(--muted);font-size:.8rem}.detail-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.7rem;margin:1rem 0}.detail-item{min-width:0;padding:.65rem .7rem;background:#faf7f3;border:1px solid var(--line);border-radius:8px}.detail-item dt{margin-bottom:.18rem;color:var(--muted);font-size:.68rem;font-weight:800;letter-spacing:.07em;text-transform:uppercase}.detail-item dd{margin:0;overflow-wrap:anywhere;font-size:.88rem}.score-card{margin:.9rem 0;padding:.85rem;background:var(--soft);border:1px solid #e7c5ce;border-radius:10px}.score-card h3{margin-bottom:.5rem}.score-card-row{display:flex;align-items:end;justify-content:space-between;gap:1rem}.score-card .score-number{font-size:2.15rem}.score-card-note{margin:.5rem 0 0;color:var(--muted);font-size:.76rem}.content-section{margin-top:1.05rem;padding-top:1.05rem;border-top:1px solid var(--line)}.explanation-list{display:grid;gap:.5rem;margin:0;padding:0;list-style:none}.explanation-item{padding:.6rem .7rem;background:#faf7f3;border:1px solid var(--line);border-radius:8px}.explanation-item strong{display:block;color:var(--dark);font-size:.82rem}.explanation-item p{margin:.2rem 0 0;color:var(--muted);font-size:.78rem}.evidence-disclosure{margin-top:.6rem;border:1px solid var(--line);border-radius:8px}.evidence-disclosure summary{padding:.7rem .8rem;color:var(--dark);cursor:pointer;font-weight:800}.evidence-list{display:grid;gap:.55rem;padding:0 .7rem .7rem}.evidence-card{padding:.65rem;background:#faf7f3;border:1px solid var(--line);border-radius:7px}.evidence-card dl{display:grid;grid-template-columns:max-content minmax(0,1fr);gap:.18rem .6rem;margin:0;font-size:.76rem}.evidence-card dt{color:var(--muted);font-weight:800}.evidence-card dd{min-width:0;margin:0;overflow-wrap:anywhere}
.draft-builder{margin-top:.7rem;padding:.85rem;background:#f7f1ed;border:1px solid var(--line);border-radius:10px}.draft-intro{margin-bottom:.75rem;color:var(--muted);font-size:.78rem}.draft-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:.65rem}.draft-grid .field:last-child{grid-column:1/-1}.draft-actions{display:flex;flex-wrap:wrap;gap:.5rem;margin-top:.75rem}.draft-result{min-height:1.3rem;margin:.65rem 0 0;color:var(--green);font-size:.8rem}.draft-result[data-tone=error]{color:var(--danger)}.preview-box{margin-top:.8rem;padding-top:.8rem;border-top:1px solid var(--line)}.preview-box iframe{display:block;width:100%;height:430px;margin-top:.65rem;background:#fff;border:1px solid var(--strong);border-radius:7px}.preview-box pre{max-height:430px;margin:.65rem 0 0;padding:.8rem;overflow:auto;color:var(--ink);background:#fff;border:1px solid var(--line);border-radius:7px;white-space:pre-wrap;overflow-wrap:anywhere;font:.78rem/1.5 ui-monospace,SFMono-Regular,Menlo,monospace}
.duplicates-panel{margin-top:1rem}.data-table-wrap{overflow-x:auto}.data-table{width:100%;border-collapse:collapse;font-size:.82rem}.data-table caption{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}.data-table th,.data-table td{padding:.65rem .5rem;text-align:left;vertical-align:top;border-bottom:1px solid var(--line)}.data-table th{color:var(--muted);font-size:.68rem;letter-spacing:.07em;text-transform:uppercase}.data-table td{overflow-wrap:anywhere}.footer-note{margin-top:1.25rem;color:var(--muted);font-size:.75rem;text-align:center}code{padding:.1rem .25rem;color:var(--dark);background:var(--soft);border-radius:4px;font-size:.9em}
@media(max-width:1120px){.filter-form{grid-template-columns:repeat(3,minmax(150px,1fr))}.filter-form .search-field{grid-column:span 2}}@media(max-width:900px){.summary-grid{grid-template-columns:repeat(2,minmax(0,1fr))}.workspace{grid-template-columns:1fr}.detail-panel{position:static;max-height:none}}@media(max-width:640px){.shell{width:min(100% - 1rem,1440px);padding-top:.5rem}.masthead{display:block;padding-bottom:1rem}.trust-list{justify-content:flex-start;margin-top:1rem}h1{font-size:2.35rem}.safety-note{margin:.8rem 0;padding:.85rem}.summary-grid{gap:.5rem}.stat-card{padding:.75rem}.stat-value{font-size:1.6rem}.stat-note{font-size:.7rem}.filters-panel,.lead-panel,.detail-panel,.duplicates-panel{padding:.8rem}.filter-form{grid-template-columns:1fr 1fr;gap:.55rem}.filter-form .search-field{grid-column:1/-1}.lead-card{grid-template-columns:minmax(0,1fr) auto;gap:.7rem}.lead-card .lead-score{grid-column:1/-1;grid-row:2}.lead-card .lead-action{grid-column:2;grid-row:1}.detail-grid,.draft-grid{grid-template-columns:1fr}.draft-grid .field:last-child{grid-column:auto}.preview-box iframe{height:360px}.data-table{min-width:560px}}@media(prefers-reduced-motion:reduce){*,*::before,*::after{scroll-behavior:auto!important;transition-duration:.01ms!important}}
</style>
</head>
<body>
<a class="skip-link" href="#main-content">Skip to review workspace</a>
<div class="shell">
<header class="masthead"><div><p class="eyebrow">Local review workspace · Vienna</p><h1>Restaurant leads, ready for a careful look.</h1><p class="subtitle">A focused place to inspect web-presence signals, verify evidence, and prepare unsent local drafts — without leaving this machine.</p></div><ul class="trust-list" aria-label="Local safety status"><li>127.0.0.1 only</li><li>No telemetry</li><li>Drafts never sent</li></ul></header>
<aside class="safety-note" aria-labelledby="safety-title"><div class="notice-mark" aria-hidden="true">!</div><p><strong id="safety-title">Safety boundary.</strong> This workspace reads the local SQLite database and writes local drafts only. It never sends email. Automated scores stay capped at 70 until human confirmation; duplicate candidates are suggestions, not merges. Review attribution, provenance, suppression, and recipient details before any action.</p></aside>
<main id="main-content">
<section class="summary-grid" aria-label="Review summary"><article class="stat-card"><p class="stat-label">Leads in view</p><p class="stat-value" id="lead-total">—</p><p class="stat-note">Matching the current filters</p></article><article class="stat-card"><p class="stat-label">Worth a closer look</p><p class="stat-value" id="priority-total">—</p><p class="stat-note">Score 50 or higher</p></article><article class="stat-card"><p class="stat-label">Human confirmed</p><p class="stat-value" id="confirmed-total">—</p><p class="stat-note">Explicitly reviewed scores</p></article><article class="stat-card"><p class="stat-label">Evidence sources</p><p class="stat-value" id="source-total">—</p><p class="stat-note">Distinct source kinds in view</p></article></section>
<section class="panel filters-panel" aria-labelledby="filters-title"><div class="filter-heading"><div><h2 id="filters-title">Shape the review queue</h2><p>Start broad, then narrow to the evidence you want to inspect.</p></div><span class="tag">local data</span></div><form class="filter-form" id="filter-form" aria-label="Lead filters"><label class="field search-field" for="q"><span class="field-label">Search leads</span><input id="q" type="search" placeholder="Name, address, website, category" autocomplete="off"></label><label class="field" for="min-score"><span class="field-label">Minimum score</span><input id="min-score" type="number" min="0" max="100" inputmode="numeric"></label><label class="field" for="max-score"><span class="field-label">Maximum score</span><input id="max-score" type="number" min="0" max="100" inputmode="numeric"></label><label class="field" for="source"><span class="field-label">Source</span><select id="source"><option value="">All sources</option><option value="overpass">Overpass</option><option value="city_top_locations">City Top Locations</option></select></label><label class="field" for="review"><span class="field-label">Review state</span><select id="review"><option value="all">All review states</option><option value="human_confirmed">Human confirmed</option><option value="automated_review">Automated review</option><option value="unscored">Unscored</option></select></label><label class="field" for="suppressed"><span class="field-label">Suppression</span><select id="suppressed"><option value="all">All suppression states</option><option value="0">Not suppressed</option><option value="1">Suppressed</option></select></label><label class="field" for="sort"><span class="field-label">Sort by</span><select id="sort"><option value="score">Score</option><option value="name">Name</option><option value="confidence">Confidence</option><option value="source">Source</option><option value="id">ID</option></select></label><label class="field" for="direction"><span class="field-label">Direction</span><select id="direction"><option value="desc">Highest first</option><option value="asc">Lowest first</option></select></label></form><div class="filter-actions"><button class="button button-primary" id="refresh" type="button">Refresh queue</button><button class="button button-quiet" id="clear-filters" type="button">Clear filters</button><p class="status-line" id="filter-status" role="status" aria-live="polite">Loading local leads…</p></div></section>
<div class="workspace"><section class="panel lead-panel" aria-labelledby="leads-title"><div class="panel-heading"><div><h2 id="leads-title">Review queue <span class="count" id="count"></span></h2><p>Choose a lead to inspect its score, evidence, and draft controls.</p></div></div><div class="lead-list" id="leads" role="list" aria-live="polite" aria-busy="true"><div class="skeleton-card" aria-hidden="true"></div><div class="skeleton-card" aria-hidden="true"></div><div class="skeleton-card" aria-hidden="true"></div></div></section><aside class="panel detail-panel" id="detail" aria-labelledby="detail-heading" aria-live="polite"><h2 id="detail-heading">Select a lead</h2><p class="muted">Lead score explanations, evidence, suppression status, and local drafts will appear here.</p></aside></div>
<section class="panel duplicates-panel" aria-labelledby="duplicates-title"><div class="panel-heading"><div><h2 id="duplicates-title">Duplicate candidates</h2><p>Suggestions only — records are never merged automatically.</p></div></div><div id="duplicates" class="data-table-wrap" aria-live="polite"><div class="loading-state">Loading duplicate suggestions…</div></div></section>
</main><p class="footer-note">Local review only · provenance stays attached to the lead · delivery: none</p>
</div>
<script>
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = { selectedLead: null, templates: [], leadBody: null };
  const filterIds = ["q", "min-score", "max-score", "source", "review", "suppressed", "sort", "direction"];
  const text = (element, value) => { element.textContent = value == null ? "" : String(value); return element; };
  const el = (tag, className, value) => { const element = document.createElement(tag); if (className) element.className = className; if (value !== undefined) text(element, value); return element; };
  const reviewLabel = (value) => ({ human_confirmed: "Human confirmed", automated_review: "Automated review", unscored: "Unscored", suppressed: "Suppressed" }[value] || value || "Unscored");
  const sourceLabel = (value) => ({ city_top_locations: "City Top Locations", overpass: "Overpass" }[value] || value || "Unknown source");
  const scoreTone = (score) => score >= 50 ? "priority" : score >= 30 ? "watch" : "stable";
  const scoreLabel = (score) => score == null ? "Unscored" : `${score}/100`;
  const params = () => { const result = new URLSearchParams(); filterIds.forEach((id) => { const value = $(id).value; if (value) result.set(id.replace("-", "_"), value); }); return result; };
  async function json(url, options) { const response = await fetch(url, options); let body; try { body = await response.json(); } catch (error) { throw new Error("The local dashboard returned an unreadable response."); } if (!response.ok) throw new Error(body.error || "Dashboard request failed"); return body; }
  function status(message, tone) { const target = $("filter-status"); text(target, message); if (tone) target.dataset.tone = tone; else delete target.dataset.tone; }
  function overview(body) { const leads = body.leads || []; text($("lead-total"), body.count); text($("priority-total"), leads.filter((lead) => lead.score != null && lead.score >= 50).length); text($("confirmed-total"), leads.filter((lead) => lead.human_confirmed).length); text($("source-total"), new Set(leads.map((lead) => lead.source_kind).filter(Boolean)).size); text($("count"), `${body.count} shown`); }
  function loadingLeads() { const target = $("leads"); target.setAttribute("aria-busy", "true"); target.replaceChildren(); [1,2,3].forEach(() => target.appendChild(el("div", "skeleton-card"))); }
  function leadError(error) { const target = $("leads"); target.setAttribute("aria-busy", "false"); target.replaceChildren(); const box = el("div", "error-state"); box.append(el("h3", null, "The review queue is unavailable"), el("p", null, error.message)); const retry = el("button", "button button-primary", "Try again"); retry.type = "button"; retry.addEventListener("click", refresh); box.appendChild(retry); target.appendChild(box); text($("count"), "could not load"); }
  function renderLeads(body) { state.leadBody = body; const target = $("leads"); target.setAttribute("aria-busy", "false"); target.replaceChildren(); overview(body); if (!body.leads.length) { const box = el("div", "empty-state"); box.append(el("h3", null, "No leads match these filters."), el("p", null, "Try widening the score range or clearing one of the review filters.")); const clear = el("button", "button button-primary", "Clear filters"); clear.type = "button"; clear.addEventListener("click", clearFilters); box.appendChild(clear); target.appendChild(box); return; } body.leads.forEach((lead) => { const card = el("article", "lead-card" + (state.selectedLead === lead.record_id ? " is-selected" : "")); card.setAttribute("role", "listitem"); const identity = el("div", "lead-identity"); const top = el("div", "lead-card-top"); top.append(el("h3", "lead-name", lead.name || "Unnamed business"), el("span", "status-badge " + (lead.suppressed ? "suppressed" : lead.human_confirmed ? "confirmed" : lead.score == null ? "" : "automated"), reviewLabel(lead.review_status))); identity.append(top, el("p", "lead-id", `Lead #${lead.record_id}`)); const meta = el("p", "lead-meta"); meta.append(el("span", null, lead.address || "Address not recorded"), el("span", null, sourceLabel(lead.source_kind))); identity.appendChild(meta); const scoreBox = el("div", "lead-score"); const scoreLine = el("div", "score-line"); scoreLine.append(el("span", "score-number", scoreLabel(lead.score)), el("span", "score-denom", lead.score == null ? "needs scoring" : lead.human_confirmed ? "confirmed" : "automated")); scoreBox.appendChild(scoreLine); if (lead.score != null) { const meter = el("div", "score-meter"); meter.setAttribute("role", "progressbar"); meter.setAttribute("aria-label", `Opportunity score ${lead.score} out of 100`); meter.setAttribute("aria-valuemin", "0"); meter.setAttribute("aria-valuemax", "100"); meter.setAttribute("aria-valuenow", String(lead.score)); const fill = el("div", "score-fill " + scoreTone(lead.score)); fill.style.width = `${Math.max(0, Math.min(100, Number(lead.score)))}%`; meter.appendChild(fill); scoreBox.appendChild(meter); } scoreBox.appendChild(el("p", "score-help", lead.score == null ? "No automated score yet" : "Explainable signal, not a verdict")); const action = el("div", "lead-action"); const open = el("button", "button button-small", "Open details"); open.type = "button"; open.setAttribute("aria-label", `Open details for ${lead.name || "unnamed business"}`); open.setAttribute("aria-pressed", String(state.selectedLead === lead.record_id)); open.addEventListener("click", () => loadDetail(lead.record_id)); action.appendChild(open); card.append(identity, scoreBox, action); target.appendChild(card); }); }
  function addDetailValue(list, label, value) { const item = el("div", "detail-item"); const displayed = value == null || value === "" ? "Not recorded" : value; item.append(el("dt", null, label), el("dd", null, displayed)); list.appendChild(item); }
  function evidenceCard(fields) { const card = el("article", "evidence-card"); const list = el("dl"); fields.forEach(([label, value]) => addDetailValue(list, label, value)); card.appendChild(list); return card; }
  function disclosure(title, items, renderItem) { const details = el("details", "evidence-disclosure"); details.open = true; details.appendChild(el("summary", null, `${title} (${items.length})`)); const list = el("div", "evidence-list"); if (!items.length) list.appendChild(el("p", "muted", "None recorded.")); else items.forEach((item) => list.appendChild(renderItem(item))); details.appendChild(list); return details; }
  function draftLinks(parent, items) { if (!items || !items.length) { parent.appendChild(el("p", "muted", "No local drafts saved for this lead.")); return; } items.forEach((item) => { const link = el("a", null, `${item.name} · ${item.format}`); link.href = item.url; link.target = "_blank"; link.rel = "noreferrer"; parent.append(link, document.createTextNode(" ")); }); }
  function templateControls(detail) { const section = el("section", "draft-builder"); section.setAttribute("aria-labelledby", "draft-builder-title"); const title = el("h3", null, "Prepare an unsent local draft"); title.id = "draft-builder-title"; section.appendChild(title); section.appendChild(el("p", "draft-intro", "Choose one of the three fixed templates. Preview stays sandboxed; saving writes a local file and delivery remains none.")); const grid = el("div", "draft-grid"); const templateField = el("label", "field"); templateField.appendChild(el("span", "field-label", "Template")); const select = document.createElement("select"); select.id = "template"; select.setAttribute("aria-label", "Draft template"); state.templates.forEach((template) => { const option = document.createElement("option"); option.value = template.template_id; text(option, template.name); select.appendChild(option); }); templateField.appendChild(select); const formatField = el("label", "field"); formatField.appendChild(el("span", "field-label", "Output format")); const format = document.createElement("select"); format.id = "draft-format"; format.setAttribute("aria-label", "Draft output format"); [["md", "Markdown source"], ["eml", "RFC 5322 .eml"]].forEach(([value, label]) => { const option = document.createElement("option"); option.value = value; text(option, label); format.appendChild(option); }); formatField.appendChild(format); const recipientField = el("label", "field"); recipientField.appendChild(el("span", "field-label", "Explicit .eml recipient")); const recipient = document.createElement("input"); recipient.id = "recipient"; recipient.type = "email"; recipient.placeholder = "Only needed for .eml"; recipient.autocomplete = "off"; recipient.setAttribute("aria-describedby", "recipient-help"); recipientField.appendChild(recipient); const help = el("span", "field-help", "No address is guessed; Markdown does not need one."); help.id = "recipient-help"; recipientField.appendChild(help); grid.append(templateField, formatField, recipientField); section.appendChild(grid); const actions = el("div", "draft-actions"); const preview = el("button", "button button-primary", "Preview"); preview.type = "button"; preview.addEventListener("click", async () => { preview.disabled = true; try { const body = await json(`/api/draft-preview/${detail.record_id}?template=${encodeURIComponent(select.value)}`); const frame = $("draft-html-preview"); frame.srcdoc = body.html; frame.hidden = false; text($("draft-source"), body.markdown); $("draft-source").hidden = true; text($("draft-preview-toggle"), "Show plain-text/source"); text($("draft-result"), "Preview refreshed locally."); $("draft-result").dataset.tone = "success"; } catch (error) { text($("draft-result"), error.message); $("draft-result").dataset.tone = "error"; } finally { preview.disabled = false; } }); const save = el("button", "button", "Save local draft"); save.type = "button"; save.addEventListener("click", async () => { save.disabled = true; try { const body = await json("/api/drafts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ lead_id: detail.record_id, template: select.value, format: format.value, recipient: recipient.value }) }); await loadDetail(detail.record_id); text($("draft-result"), `Saved locally: ${body.name} · delivery: none`); $("draft-result").dataset.tone = "success"; } catch (error) { text($("draft-result"), error.message); $("draft-result").dataset.tone = "error"; } finally { save.disabled = false; } }); actions.append(preview, save); section.appendChild(actions); const previewBox = el("section", "preview-box"); previewBox.setAttribute("aria-labelledby", "preview-title"); const previewTitle = el("h4", null, "Sandboxed rendered preview"); previewTitle.id = "preview-title"; previewBox.appendChild(previewTitle); const frame = document.createElement("iframe"); frame.id = "draft-html-preview"; frame.title = "Sandboxed rendered email preview"; frame.setAttribute("sandbox", ""); frame.srcdoc = '<p style="font-family:system-ui,sans-serif;padding:16px;color:#746a64">Choose Preview to render the selected email template.</p>'; previewBox.appendChild(frame); const toggle = el("button", "button button-quiet button-small", "Show plain-text/source"); toggle.id = "draft-preview-toggle"; toggle.type = "button"; toggle.addEventListener("click", () => { const source = $("draft-source"); source.hidden = !source.hidden; frame.hidden = !source.hidden; text($("draft-preview-toggle"), source.hidden ? "Show plain-text/source" : "Show rendered HTML"); }); previewBox.appendChild(toggle); const source = el("pre", null, ""); source.id = "draft-source"; source.hidden = true; previewBox.appendChild(source); section.appendChild(previewBox); const result = el("p", "draft-result", ""); result.id = "draft-result"; section.appendChild(result); return section; }
  function detailError(error, id) { const target = $("detail"); target.replaceChildren(); target.setAttribute("aria-busy", "false"); const heading = el("h2", null, "Lead details unavailable"); heading.id = "detail-heading"; const retry = el("button", "button button-primary", "Try again"); retry.type = "button"; retry.addEventListener("click", () => loadDetail(id)); target.append(heading, el("p", "error-message", error.message), retry); }
  async function loadDetail(id) { state.selectedLead = id; if (state.leadBody) renderLeads(state.leadBody); const target = $("detail"); target.setAttribute("aria-busy", "true"); target.replaceChildren(el("div", "loading-state", "Loading lead evidence…")); try { const detail = await json(`/api/leads/${id}`); target.setAttribute("aria-busy", "false"); target.replaceChildren(); const header = el("div", "detail-header"); const title = el("h2", "detail-title", detail.name || "Unnamed business"); title.id = "detail-heading"; const wrap = el("div"); wrap.append(title, el("p", "detail-subtitle", `Lead #${detail.record_id} · ${sourceLabel(detail.source_kind)}`)); header.append(wrap, el("span", "status-badge " + (detail.suppressed ? "suppressed" : detail.human_confirmed ? "confirmed" : detail.score == null ? "" : "automated"), reviewLabel(detail.review_status))); target.appendChild(header); const grid = el("dl", "detail-grid"); [["Review status", reviewLabel(detail.review_status)], ["Automated score", detail.automated_score == null ? null : `${detail.automated_score}/70`], ["Confidence", detail.confidence], ["Address", detail.address], ["Website", detail.website], ["Phone", detail.phone], ["Email", detail.email], ["Category", detail.category]].forEach(([label, value]) => addDetailValue(grid, label, value)); target.appendChild(grid); const scoreCard = el("section", "score-card"); const scoreTitle = el("h3", null, "Opportunity score"); scoreTitle.id = "score-title"; scoreCard.appendChild(scoreTitle); const scoreRow = el("div", "score-card-row"); scoreRow.append(el("span", "score-number", scoreLabel(detail.score)), el("span", "score-denom", detail.human_confirmed ? "human confirmed" : detail.score == null ? "not scored" : "automated · max 70")); scoreCard.appendChild(scoreRow); if (detail.score != null) { const meter = el("div", "score-meter"); meter.setAttribute("role", "progressbar"); meter.setAttribute("aria-label", `Opportunity score ${detail.score} out of 100`); meter.setAttribute("aria-valuemin", "0"); meter.setAttribute("aria-valuemax", "100"); meter.setAttribute("aria-valuenow", String(detail.score)); const fill = el("div", "score-fill " + scoreTone(detail.score)); fill.style.width = `${Math.max(0, Math.min(100, Number(detail.score)))}%`; meter.appendChild(fill); scoreCard.appendChild(meter); } scoreCard.appendChild(el("p", "score-card-note", `Model: ${detail.model_version || "not available"}. Confidence describes observable field completeness and source coverage, not response likelihood.`)); target.appendChild(scoreCard); const why = el("section", "content-section"); why.appendChild(el("h3", null, "Why this score")); const reasons = el("ul", "explanation-list"); if (!(detail.explanations || []).length) reasons.appendChild(el("li", "empty-state compact", "No score explanation is recorded.")); else detail.explanations.forEach((item) => { const reason = el("li", "explanation-item"); reason.append(el("strong", null, `${item.points > 0 ? "+" : ""}${item.points} · ${item.code}`), el("p", null, item.explanation)); reasons.appendChild(reason); }); why.appendChild(reasons); target.appendChild(why); const evidence = el("section", "content-section"); evidence.appendChild(el("h3", null, "Evidence & review trail")); evidence.appendChild(disclosure("Provenance", detail.provenance || [], (item) => evidenceCard([["Source", sourceLabel(item.source_kind)], ["Record key", item.source_record_key], ["Captured", item.captured_at], ["Attribution", item.attribution], ["License", item.license], ["Source reference", item.source_url]]))); evidence.appendChild(disclosure("Duplicate candidates", detail.duplicate_candidates || [], (item) => { let reasons = item.reasons_json || ""; try { reasons = (JSON.parse(reasons) || []).join("; "); } catch (error) { /* keep source text */ } return evidenceCard([["Candidate", `${item.record_a} ↔ ${item.record_b}`], ["Method", item.method], ["Confidence", item.confidence], ["Status", item.status], ["Reasons", reasons]]); })); target.appendChild(evidence); const drafts = el("section", "content-section"); drafts.appendChild(el("h3", null, "Local proposal drafts")); const links = el("div"); draftLinks(links, detail.drafts); drafts.appendChild(links); drafts.appendChild(templateControls(detail)); target.appendChild(drafts); } catch (error) { detailError(error, id); } }
  function renderDuplicates(body) { const target = $("duplicates"); target.replaceChildren(); if (!body.duplicates.length) { target.appendChild(el("div", "empty-state compact", "No duplicate suggestions are recorded.")); return; } const table = el("table", "data-table"); table.appendChild(el("caption", null, "Duplicate candidate suggestions")); const head = el("thead"); const headRow = el("tr"); ["ID","Records","Method","Confidence","Status"].forEach((label) => headRow.appendChild(el("th", null, label))); head.appendChild(headRow); table.appendChild(head); const rows = el("tbody"); body.duplicates.forEach((item) => { const row = el("tr"); [item.candidate_id, `${item.record_a} ↔ ${item.record_b}`, item.method, item.confidence, item.status].forEach((value) => row.appendChild(el("td", null, value))); rows.appendChild(row); }); table.appendChild(rows); const wrap = el("div", "data-table-wrap"); wrap.appendChild(table); target.appendChild(wrap); }
  function duplicateError(error) { const target = $("duplicates"); target.replaceChildren(); const box = el("div", "error-state"); box.append(el("h3", null, "Duplicate suggestions unavailable"), el("p", null, error.message)); const retry = el("button", "button button-quiet", "Retry suggestions"); retry.type = "button"; retry.addEventListener("click", loadDuplicates); box.appendChild(retry); target.appendChild(box); }
  async function loadDuplicates() { try { renderDuplicates(await json("/api/duplicates")); } catch (error) { duplicateError(error); } }
  async function refresh() { loadingLeads(); status("Refreshing local review queue…"); try { const body = await json(`/api/leads?${params().toString()}`); renderLeads(body); status(`${body.count} lead${body.count === 1 ? "" : "s"} ready to review.`, "success"); } catch (error) { leadError(error); status(error.message, "error"); } await loadDuplicates(); }
  function clearFilters() { filterIds.forEach((id) => { if (["source","review","suppressed"].includes(id)) $(id).value = id === "review" || id === "suppressed" ? "all" : ""; else if (id === "sort") $(id).value = "score"; else if (id === "direction") $(id).value = "desc"; else $(id).value = ""; }); refresh(); }
  async function init() { try { const body = await json("/api/templates"); state.templates = body.templates; await refresh(); } catch (error) { leadError(error); status(error.message, "error"); } }
  $("refresh").addEventListener("click", refresh); $("clear-filters").addEventListener("click", clearFilters); $("filter-form").addEventListener("submit", (event) => { event.preventDefault(); refresh(); }); $("q").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); refresh(); } }); ["min-score","max-score","source","review","suppressed","sort","direction"].forEach((id) => $(id).addEventListener("change", refresh)); init();
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
