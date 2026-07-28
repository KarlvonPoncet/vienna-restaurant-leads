"""Static local HTML and tabular export generation."""

from __future__ import annotations

import csv
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import sqlite3
from typing import Any

from . import __version__
from .db import source_attributions
from .suppression import is_suppressed


def _json_list(value: str | None) -> list[Any]:
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return []
    return decoded if isinstance(decoded, list) else []


def _score(connection: sqlite3.Connection, record_id: int) -> dict[str, Any]:
    row = connection.execute("SELECT * FROM scores WHERE record_id = ?", (record_id,)).fetchone()
    if row is None:
        return {
            "model_version": "",
            "automated_score": None,
            "score": None,
            "reason_codes": [],
            "explanations": [],
            "confidence": None,
            "human_confirmed": False,
        }
    return {
        "model_version": row["model_version"],
        "automated_score": row["automated_score"],
        "score": row["score"],
        "reason_codes": _json_list(row["reason_codes_json"]),
        "explanations": _json_list(row["explanation_json"]),
        "confidence": row["confidence"],
        "human_confirmed": bool(row["human_confirmed"]),
    }


def export_data(connection: sqlite3.Connection) -> dict[str, Any]:
    attributions = source_attributions(connection)
    records: list[dict[str, Any]] = []
    for row in connection.execute(
        """SELECT r.*, s.source_kind, s.source_url, s.attribution, s.license,
                  s.license_url, s.captured_at AS source_captured_at
           FROM records r JOIN source_runs s ON s.source_id = r.source_id
           ORDER BY r.record_id"""
    ):
        record = {
            "record_id": row["record_id"],
            "source_id": row["source_id"],
            "source_kind": row["source_kind"],
            "source_record_key": row["source_record_key"],
            "name": row["name"],
            "address": row["address"],
            "district": row["district"],
            "category": row["category"],
            "phone": row["phone"],
            "email": row["email"],
            "website": row["website"],
            "latitude": row["latitude"],
            "longitude": row["longitude"],
            "imported_at": row["imported_at"],
            "contact_qualified_at": row["contact_qualified_at"],
            "source_url": row["source_url"],
            "attribution": row["attribution"],
            "license": row["license"],
            "license_url": row["license_url"],
            "source_captured_at": row["source_captured_at"],
            "suppressed": is_suppressed(connection, row),
        }
        record.update(_score(connection, int(row["record_id"])))
        records.append(record)

    duplicates = [
        {
            "candidate_id": row["candidate_id"],
            "record_a": row["record_a"],
            "record_b": row["record_b"],
            "method": row["method"],
            "confidence": row["confidence"],
            "reasons": _json_list(row["reasons_json"]),
            "status": row["status"],
        }
        for row in connection.execute("SELECT * FROM duplicate_candidates ORDER BY candidate_id")
    ]
    return {
        "metadata": {
            "application": "Vienna Restaurant Leads",
            "version": __version__,
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "local_only": True,
            "automated_score_cap": 70,
            "attributions": attributions,
            "safety": [
                "No SMTP, hosted email, tracking pixels, remote model calls, or automated outreach are used.",
                "Duplicate candidates are review suggestions only; records are never automatically merged.",
            ],
        },
        "records": records,
        "duplicate_candidates": duplicates,
    }


def _attribution_lines(data: dict[str, Any]) -> list[str]:
    lines = []
    for item in data["metadata"]["attributions"]:
        license_url = item.get("license_url") or ""
        suffix = f" ({license_url})" if license_url else ""
        lines.append(f"{item['attribution']} — {item['license']}{suffix}")
    return lines


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_csv(path: Path, data: dict[str, Any]) -> None:
    fields = [
        "record_id", "source_kind", "source_record_key", "name", "address", "district",
        "category", "phone", "email", "website", "latitude", "longitude", "score",
        "automated_score", "model_version", "reason_codes", "confidence", "human_confirmed",
        "suppressed", "attribution", "license", "source_url",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        for line in _attribution_lines(data):
            handle.write(f"# Attribution: {line}\n")
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for record in data["records"]:
            row = dict(record)
            row["reason_codes"] = ",".join(row.get("reason_codes", []))
            writer.writerow(row)


def write_markdown(path: Path, data: dict[str, Any]) -> None:
    lines = [
        "# Vienna Restaurant Leads review",
        "",
        "> Local-only review export. Duplicate candidates are not merges; proposal drafts are not sent automatically.",
        "",
        "## Attribution and licenses",
        "",
    ]
    lines.extend(f"- {line}" for line in _attribution_lines(data))
    lines.extend(["", "## Records", ""])
    visible = [record for record in data["records"] if not record["suppressed"]]
    if not visible:
        lines.append("No unsuppressed records.")
    for record in visible:
        score = record["score"] if record["score"] is not None else "unscored"
        reasons = ", ".join(record["reason_codes"]) or "none"
        lines.extend(
            [
                f"### {record['record_id']}: {record['name'] or 'Unnamed'}",
                f"- Score: **{score}/100** (automated: {record['automated_score']}, confidence: {record['confidence']})",
                f"- Address: {record['address'] or '—'}",
                f"- Website: {record['website'] or 'none listed'}",
                f"- Reasons: `{reasons}`",
                f"- Provenance: {record['source_kind']} / {record['attribution']} / {record['license']}",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")


def write_html(path: Path, data: dict[str, Any]) -> None:
    title = "Vienna Restaurant Leads — local review"
    attribution_html = "".join(f"<li>{html.escape(line)}</li>" for line in _attribution_lines(data))
    rows: list[str] = []
    for record in data["records"]:
        if record["suppressed"]:
            continue
        reasons = ", ".join(record["reason_codes"]) or "unscored"
        score = "—" if record["score"] is None else str(record["score"])
        website = record["website"] or "none listed"
        rows.append(
            "<tr>"
            f"<td>{record['record_id']}</td>"
            f"<td>{html.escape(record['name'] or 'Unnamed')}</td>"
            f"<td>{html.escape(record['address'] or '—')}</td>"
            f"<td>{html.escape(score)}/100</td>"
            f"<td>{html.escape(reasons)}</td>"
            f"<td>{html.escape(website)}</td>"
            f"<td>{html.escape(record['source_kind'])}</td>"
            "</tr>"
        )
    body_rows = "\n".join(rows) or '<tr><td colspan="7">No unsuppressed records.</td></tr>'
    document = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>body{{font:16px system-ui,sans-serif;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#222}}table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #ccc;padding:.5rem;text-align:left;vertical-align:top}}th{{background:#eee}}.note{{background:#fff8dd;padding:1rem}}</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
<div class="note">Automated scores are capped at 70 until a human confirms them. Suppressed records are omitted. Duplicate candidates never merge records.</div>
<h2>Attribution and licenses</h2><ul>{attribution_html}</ul>
<table><thead><tr><th>ID</th><th>Name</th><th>Address</th><th>Score</th><th>Reason codes</th><th>Website</th><th>Source</th></tr></thead>
<tbody>{body_rows}</tbody></table>
<footer><p>Generated locally by Vienna Restaurant Leads {html.escape(__version__)}. No remote assets, tracking pixels, SMTP, or automated outreach.</p></footer>
</body></html>
"""
    path.write_text(document, encoding="utf-8")


def export_all(connection: sqlite3.Connection, output_dir: str | Path) -> dict[str, Path]:
    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    data = export_data(connection)
    paths = {
        "html": directory / "review.html",
        "csv": directory / "leads.csv",
        "json": directory / "leads.json",
        "markdown": directory / "review.md",
    }
    write_html(paths["html"], data)
    write_csv(paths["csv"], data)
    write_json(paths["json"], data)
    write_markdown(paths["markdown"], data)
    return paths
