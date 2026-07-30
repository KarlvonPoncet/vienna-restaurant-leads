# Vienna Restaurant Leads

## Why this exists

Small web agencies often know a local business may benefit from a clearer web presence, but turning bounded public business data into a useful, reviewable opportunity takes careful evidence handling. Vienna Restaurant Leads is a **local-only, dependency-free Python + SQLite MVP** for that narrow job: preserve where a business record came from, surface a transparent website-opportunity signal, let a person review it, and produce local draft artifacts.

The project does not claim that a restaurant needs a new website or that anyone will respond. It makes an observable record easier to inspect: bounded source snapshots become normalized business records; provenance and conservative duplicate candidates remain attached; an explainable 0–70 automated score highlights website absence without judging site quality; a human can confirm a final 0–100 score; and exports or unsent proposal drafts provide a starting point for local review.

### Principles and boundaries

- **Local and bounded:** source access is explicit and limited; the app binds the dashboard only to `127.0.0.1` and has no hosted mode, telemetry, remote models, or third-party runtime dependencies.
- **Evidence over inference:** raw payloads, provenance, attribution, and licenses are preserved. The scorer uses observed business/web fields, never personal or inferred traits, and never fetches a website.
- **Human review over automation:** duplicate matches are suggestions, not merges; scores are signals, not verdicts; proposal wording needs review.
- **Draft-only/no-send:** generated Markdown and RFC 5322 `.eml` files are local artifacts. There is no SMTP, queue, recipient discovery, tracking pixel, or send command.
- **Privacy and attribution:** suppression stores hashes, contact retention is explicit, and generated artifacts carry source attribution and license notices.

## Current MVP and future roadmap

The current MVP imports a bounded Overpass snapshot or a locally reviewed City of Vienna CSV, normalizes records, preserves source evidence, suggests duplicates, scores opportunities, supports suppression/retention, and creates local exports, a local dashboard, and unsent drafts.

The future vision (not implemented) is an AI sales assistant for small web agencies: it might inspect websites for mobile usability, reservations, SEO, or outdated design, find contact information, personalize pitches, and manage a pipeline. This repository currently does **not** scrape directories, bulk-geocode, inspect websites, call remote models, host a CRM, or send outreach.

## Quick start

From the repository root:

```sh
export PYTHONPATH="$PWD/src"
python3 -m vienna_leads init-db --db data/vienna-leads.sqlite3
python3 -m unittest discover -s tests -v
```

Use disposable local databases and caches. `data/`, `cache/`, `exports/`, `drafts/`, SQLite files, and contact artifacts are ignored by Git.

## Workflow and operating reference

### 1. Import bounded public data

```sh
python3 -m vienna_leads ingest-overpass --db data/vienna-leads.sqlite3 --cache-dir data/cache --max-age-hours 24
python3 -m vienna_leads ingest-city --db data/vienna-leads.sqlite3 --csv /path/to/reviewed-top-locations.csv --source-url 'https://www.data.gv.at/'
```

Overpass uses one explicit Vienna bounding-box query for `amenity=restaurant`, a descriptive User-Agent, a response-size limit, and a 24-hour local cache by default. The exact response, SHA-256, request metadata, and attribution are stored. The City CSV is never downloaded automatically; encoding and delimiter detection support UTF-8/BOM, Windows-1252, Latin-1, comma, semicolon, and tab files. Default City metadata is `Top Locations data: City of Vienna`, `CC BY 4.0`, with its license URL.

### 2. Find candidates and score

```sh
python3 -m vienna_leads duplicates --db data/vienna-leads.sqlite3
python3 -m vienna_leads score --db data/vienna-leads.sqlite3
python3 -m vienna_leads list --db data/vienna-leads.sqlite3 --limit 100
```

Duplicate candidates remain pending review and are never automatically merged.

### 3. Suppress and retain responsibly

```sh
python3 -m vienna_leads suppress --db data/vienna-leads.sqlite3 --place-id 123 --reason 'requested opt-out'
python3 -m vienna_leads suppress --db data/vienna-leads.sqlite3 --kind email --value 'contact@example.invalid'
python3 -m vienna_leads qualify-contact --db data/vienna-leads.sqlite3 --place-id 123
python3 -m vienna_leads purge-contact-data --db data/vienna-leads.sqlite3 --retention-days 90
```

Suppression stores only kind, reason, timestamp, and a hash. Unqualified derived phone/email data defaults to 90-day retention; purging leaves raw payloads and provenance for audit.

### 4. Export and review locally

```sh
python3 -m vienna_leads export --db data/vienna-leads.sqlite3 --out-dir exports/vienna-review
python3 -m vienna_leads dashboard --db data/vienna-leads.sqlite3 --draft-dir drafts --port 8765
```

Exports include self-contained `review.html`, `leads.csv`, `leads.json`, and `review.md`, with OpenStreetMap/ODbL and City of Vienna/CC BY 4.0 notices and license URLs. The dashboard offers local filtering, score explanations, provenance, duplicate candidates, suppression state, and three draft templates. It has no remote assets; stop it with `Ctrl-C`.

### 5. Write an unsent draft

```sh
python3 -m vienna_leads confirm-score --db data/vienna-leads.sqlite3 --place-id 123 --score 82
python3 -m vienna_leads draft --db data/vienna-leads.sqlite3 --place-id 123 --out drafts/123.md --format md --template friendly-refresh
python3 -m vienna_leads draft --db data/vienna-leads.sqlite3 --place-id 123 --out drafts/123.eml --format eml --from 'reviewer@example.invalid' --to 'explicit-recipient@example.invalid'
```

Templates are `friendly-refresh`, `practical-visibility`, and `premium-concept`. Suppressed records cannot produce drafts; `.eml` generation requires an explicit recipient and performs no delivery.

## Scoring model

The current model is `website-opportunity-v2`. It is versioned because missing qualification data now reduces the opportunity signal. It uses no coordinates or source/provenance metadata for points and never fetches URLs.

| Signal | Automated points |
| --- | ---: |
| no website listed | +60 |
| social profile only | +45 |
| HTTP-only website | +20 |
| HTTPS website present | +5 |
| explicit restaurant category | +10 |
| missing `name` | -5 |
| missing `address` | -5 |
| missing `category` | -5 |
| missing `phone` | -5 |
| missing `email` | -5 |

Applicable signals are summed and clamped to **0–70**. A missing website remains positive even when other fields are missing. A missing category gets the -5 penalty and does not also receive the restaurant +10. Rescoring refreshes the automated score, explanations, and confidence while preserving an existing human-confirmed final score. `confirm-score` permits an explicitly reviewed final value from 0–100.

## Data model and safety

SQLite tables (defined in `src/vienna_leads/db.py`) store raw source payloads, source runs, normalized records, provenance, duplicate candidates, versioned scores, and hashed suppression records. Generated artifacts are local review material only. Review source terms before import, verify attribution and recipient details, honor suppression, and treat every proposal as a human-edited draft.
