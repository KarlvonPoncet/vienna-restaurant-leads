"""Command-line interface for the local-only MVP."""

from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import sys

from . import DEFAULT_USER_AGENT, RETENTION_DAYS
from .db import connect_db, open_db
from .drafts import write_eml_draft, write_markdown_draft
from .duplicates import generate_duplicate_candidates
from .exports import export_all
from .scoring import confirm_score, score_records
from .sources import (
    DEFAULT_MAX_BYTES,
    DEFAULT_OVERPASS_ENDPOINT,
    DEFAULT_TIMEOUT_SECONDS,
    ingest_city_bytes,
    ingest_overpass_snapshot,
)
from .suppression import add_suppression, qualify_contact, purge_unqualified_contact_data, is_suppressed

DEFAULT_DB = "data/vienna-leads.sqlite3"


class StructuredArgumentParser(argparse.ArgumentParser):
    """Argparse with agent-readable errors on stdout and no traceback."""

    def error(self, message: str) -> None:  # pragma: no cover - exercised through CLI subprocesses
        self._print_message(f"error: {message}\n", sys.stdout)
        self._print_message("help: run this command with --help\n", sys.stdout)
        raise SystemExit(2)


def _db_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite path (default: {DEFAULT_DB})")


def build_parser() -> StructuredArgumentParser:
    parser = StructuredArgumentParser(
        prog="vienna-leads",
        description="Local-only Vienna restaurant review, scoring, export, and draft tool.",
    )
    subparsers = parser.add_subparsers(dest="command", parser_class=StructuredArgumentParser)

    init = subparsers.add_parser("init-db", help="create or migrate the local SQLite database")
    _db_argument(init)

    overpass = subparsers.add_parser("ingest-overpass", help="ingest one bounded cached Overpass snapshot")
    _db_argument(overpass)
    overpass.add_argument("--cache-dir", default="data/cache", help="snapshot cache directory")
    overpass.add_argument("--endpoint", default=DEFAULT_OVERPASS_ENDPOINT, help="Overpass interpreter endpoint")
    overpass.add_argument("--max-age-hours", type=float, default=24, help="reuse cache up to this age (default: 24)")
    overpass.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES, help="response safety limit")
    overpass.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="request timeout seconds")
    overpass.add_argument("--user-agent", default=DEFAULT_USER_AGENT, help="descriptive User-Agent header")

    city = subparsers.add_parser("ingest-city", help="ingest a reviewed City of Vienna Top Locations CSV")
    _db_argument(city)
    city.add_argument("--csv", required=True, help="path to the reviewed CSV; it is not downloaded")
    city.add_argument("--source-url", default="", help="reviewed CSV landing-page URL for provenance")
    city.add_argument("--attribution", default="Top Locations data: City of Vienna", help="attribution text")
    city.add_argument("--license", default="CC BY 4.0", help="license text")
    city.add_argument("--license-url", default="https://creativecommons.org/licenses/by/4.0/", help="license URL")

    duplicates = subparsers.add_parser("duplicates", help="create conservative duplicate candidates; never merge")
    _db_argument(duplicates)

    score = subparsers.add_parser("score", help="calculate versioned website-opportunity scores")
    _db_argument(score)

    confirm = subparsers.add_parser("confirm-score", help="human-confirm a score, allowing a value above 70")
    _db_argument(confirm)
    confirm.add_argument("--place-id", type=int, required=True, help="record ID")
    confirm.add_argument("--score", type=int, required=True, help="human-confirmed score from 0 to 100")

    suppress = subparsers.add_parser("suppress", help="record an opt-out using hashes only")
    _db_argument(suppress)
    group = suppress.add_mutually_exclusive_group(required=True)
    group.add_argument("--place-id", type=int, help="suppress this business and its stable identifiers")
    group.add_argument("--value", help="value to hash immediately; never stored in suppression table")
    suppress.add_argument("--kind", default="business", choices=("business", "email", "phone", "website_domain"))
    suppress.add_argument("--reason", default="opt-out", help="short suppression reason")

    qualify = subparsers.add_parser("qualify-contact", help="retain derived phone/email beyond the 90-day default")
    _db_argument(qualify)
    qualify.add_argument("--place-id", type=int, required=True, help="record ID")

    purge = subparsers.add_parser("purge-contact-data", help="remove unqualified phone/email after retention period")
    _db_argument(purge)
    purge.add_argument("--retention-days", type=int, default=RETENTION_DAYS, help=f"days (default: {RETENTION_DAYS})")

    export = subparsers.add_parser("export", help="write local HTML, CSV, JSON, and Markdown review exports")
    _db_argument(export)
    export.add_argument("--out-dir", default="exports", help="local output directory")

    draft = subparsers.add_parser("draft", help="write one local Markdown or RFC 5322 .eml proposal draft")
    _db_argument(draft)
    draft.add_argument("--place-id", type=int, required=True, help="record ID")
    draft.add_argument("--out", required=True, help="local output path")
    draft.add_argument("--format", choices=("md", "eml"), default="md", help="draft format (default: md)")
    draft.add_argument("--from", dest="sender", default="local-review@example.invalid", help="From address for .eml")
    draft.add_argument("--to", dest="recipient", help="To address for .eml; no address is guessed")

    records = subparsers.add_parser("list", help="list local review records")
    _db_argument(records)
    records.add_argument("--limit", type=int, default=100, help="maximum rows (default: 100)")

    return parser


def _emit(lines: list[str]) -> None:
    sys.stdout.write("\n".join(lines) + "\n")


def _home(db_path: str) -> int:
    connection = connect_db(db_path)
    try:
        record_count = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        score_count = connection.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
        duplicate_count = connection.execute(
            "SELECT COUNT(*) FROM duplicate_candidates WHERE status = 'pending'"
        ).fetchone()[0]
    finally:
        connection.close()
    executable = str(Path(sys.argv[0]).resolve()).replace(str(Path.home()), "~", 1)
    _emit(
        [
            f"bin: {executable}",
            "description: Review Vienna restaurant business/web signals locally; never send outreach.",
            f"records: {record_count}",
            f"scored: {score_count}",
            f"pending_duplicate_candidates: {duplicate_count}",
            "help: vienna-leads ingest-overpass --help",
            "help: vienna-leads ingest-city --help",
            "help: vienna-leads export --help",
        ]
    )
    return 0


def _record_with_score(connection: sqlite3.Connection, record_id: int):
    row = connection.execute("SELECT * FROM records WHERE record_id = ?", (record_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown record: {record_id}")
    score = connection.execute("SELECT * FROM scores WHERE record_id = ?", (record_id,)).fetchone()
    return row, score


def run(args: argparse.Namespace) -> int:
    if not args.command:
        return _home(DEFAULT_DB)
    if args.command == "init-db":
        connection = connect_db(args.db)
        connection.close()
        _emit([f"status: initialized", f"db: {Path(args.db).resolve()}"])
        return 0

    if args.command == "ingest-overpass":
        with open_db(args.db) as connection:
            ids = ingest_overpass_snapshot(
                connection,
                cache_dir=args.cache_dir,
                endpoint=args.endpoint,
                max_age_seconds=int(args.max_age_hours * 3600),
                max_bytes=args.max_bytes,
                timeout=args.timeout,
                user_agent=args.user_agent,
            )
        _emit(["status: ingested", "source: overpass", f"records: {len(ids)}", f"db: {Path(args.db).resolve()}"])
        return 0

    if args.command == "ingest-city":
        payload = Path(args.csv).read_bytes()
        with open_db(args.db) as connection:
            ids = ingest_city_bytes(
                connection,
                payload,
                source_url=args.source_url,
                attribution=args.attribution,
                license=args.license,
                license_url=args.license_url,
            )
        _emit(["status: ingested", "source: city_top_locations", f"records: {len(ids)}", f"db: {Path(args.db).resolve()}"])
        return 0

    if args.command == "duplicates":
        with open_db(args.db) as connection:
            created = generate_duplicate_candidates(connection)
            pending = connection.execute("SELECT COUNT(*) FROM duplicate_candidates WHERE status = 'pending'").fetchone()[0]
        _emit(["status: candidates_updated", f"new_candidates: {created}", f"pending_candidates: {pending}"])
        return 0

    if args.command == "score":
        with open_db(args.db) as connection:
            count = score_records(connection)
        _emit(["status: scored", f"records: {count}", "model: website-opportunity-v1", "automated_cap: 70"])
        return 0

    if args.command == "confirm-score":
        with open_db(args.db) as connection:
            confirm_score(connection, args.place_id, args.score)
        _emit(["status: score_confirmed", f"place_id: {args.place_id}", f"score: {args.score}"])
        return 0

    if args.command == "suppress":
        with open_db(args.db) as connection:
            if args.place_id is not None:
                row = connection.execute("SELECT * FROM records WHERE record_id = ?", (args.place_id,)).fetchone()
                if row is None:
                    raise ValueError(f"unknown record: {args.place_id}")
                count = add_suppression(connection, record=row, reason=args.reason)
            else:
                count = add_suppression(connection, value=args.value, kind=args.kind, reason=args.reason)
        _emit(["status: suppressed", f"new_hashes: {count}", "stored_value: hash_only"])
        return 0

    if args.command == "qualify-contact":
        with open_db(args.db) as connection:
            qualify_contact(connection, args.place_id)
        _emit(["status: contact_qualified", f"place_id: {args.place_id}"])
        return 0

    if args.command == "purge-contact-data":
        with open_db(args.db) as connection:
            count = purge_unqualified_contact_data(connection, retention_days=args.retention_days)
        _emit(["status: contact_data_purged", f"records: {count}", f"retention_days: {args.retention_days}"])
        return 0

    if args.command == "export":
        with open_db(args.db) as connection:
            paths = export_all(connection, args.out_dir)
        _emit(["status: exported"] + [f"{kind}: {path}" for kind, path in paths.items()])
        return 0

    if args.command == "draft":
        with open_db(args.db) as connection:
            row, score = _record_with_score(connection, args.place_id)
            if is_suppressed(connection, row):
                raise ValueError("record is suppressed; no draft will be generated")
            record = dict(row)
            score_dict = None
            if score is not None:
                score_dict = dict(score)
                import json

                score_dict["reason_codes"] = json.loads(score_dict.pop("reason_codes_json"))
            if args.format == "md":
                output = write_markdown_draft(args.out, record, score=score_dict)
            else:
                recipient = args.recipient or record.get("email", "")
                if not recipient:
                    raise ValueError("--to is required for .eml; no recipient is guessed")
                output = write_eml_draft(
                    args.out,
                    record,
                    sender=args.sender,
                    recipient=recipient,
                    score=score_dict,
                )
        _emit(["status: draft_written", f"format: {args.format}", f"path: {output}", "delivery: none"])
        return 0

    if args.command == "list":
        if args.limit <= 0:
            raise ValueError("--limit must be positive")
        connection = connect_db(args.db)
        try:
            rows = connection.execute(
                """SELECT r.record_id, r.name, r.address, s.score, s.automated_score
                   FROM records r LEFT JOIN scores s ON s.record_id = r.record_id
                   ORDER BY r.record_id LIMIT ?""",
                (args.limit,),
            ).fetchall()
        finally:
            connection.close()
        _emit([f"records: {len(rows)}"] + [
            f"record: {row['record_id']} | {row['name'] or 'Unnamed'} | score={row['score'] if row['score'] is not None else 'unscored'} | automated={row['automated_score'] if row['automated_score'] is not None else '—'}"
            for row in rows
        ])
        return 0

    raise ValueError(f"unknown command: {args.command}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
        return run(args)
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        _emit([f"error: {exc}", "help: inspect the command's --help and verify local paths/inputs"])
        return 1
