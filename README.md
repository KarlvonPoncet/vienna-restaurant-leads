# Vienna Restaurant Leads

A small **local-only** Python 3 + SQLite MVP for reviewing Vienna restaurant business/web-presence signals. It keeps source payloads and provenance, suggests possible duplicates without merging them, scores website opportunities for human review, and writes static exports and unsent proposal drafts.

There are no third-party runtime dependencies. The application does not crawl Google Maps or other directories, use Nominatim for bulk discovery, bypass access controls, call remote models, send SMTP, host email, add tracking pixels, or infer personal traits.

## Quick start

From the repository root:

```sh
export PYTHONPATH="$PWD/src"
python3 -m vienna_leads init-db --db data/vienna-leads.sqlite3
python3 -m unittest discover -s tests -v
```

`data/`, `cache/`, `exports/`, `drafts/`, SQLite files, and contact artifacts are ignored by Git. Use a disposable local database and cache for real data.

## Local commands

### 1. Import a bounded Overpass snapshot

The importer sends one explicit Vienna bounding box query for `amenity=restaurant`, uses a descriptive User-Agent, limits response size, and reuses a local cache for 24 hours by default. It does not discover businesses through a directory crawl.

```sh
export PYTHONPATH="$PWD/src"
python3 -m vienna_leads ingest-overpass \
  --db data/vienna-leads.sqlite3 \
  --cache-dir data/cache \
  --max-age-hours 24
```

The default endpoint is `https://overpass-api.de/api/interpreter`. A cached JSON response is preferred while fresh; the exact payload is also stored in SQLite with SHA-256 and request provenance. To use a different Overpass instance, pass `--endpoint` explicitly. The request remains bounded.

### 2. Import the reviewed City of Vienna CSV

Download/review the CSV yourself, then pass its local path. The importer detects UTF-8/BOM, UTF-8, Windows-1252, and Latin-1, and sniffs comma/semicolon/tab delimiters. It never downloads a CSV on its own.

```sh
python3 -m vienna_leads ingest-city \
  --db data/vienna-leads.sqlite3 \
  --csv /path/to/reviewed-top-locations.csv \
  --source-url 'https://www.data.gv.at/'
```

The default metadata is `Top Locations data: City of Vienna`, `CC BY 4.0`, and the Creative Commons license URL. Override `--source-url` with the reviewed dataset landing page if known; only change attribution/license flags when the reviewed source's terms require it.

### 3. Review duplicates and score opportunities

```sh
python3 -m vienna_leads duplicates --db data/vienna-leads.sqlite3
python3 -m vienna_leads score --db data/vienna-leads.sqlite3
python3 -m vienna_leads list --db data/vienna-leads.sqlite3 --limit 100
```

Duplicate candidates are deliberately conservative (`exact_name_address`, same phone/domain with a similar name, or a similar name within 100 metres). They remain `pending` review records. No automatic merge or destructive update exists.

### 4. Suppress opt-outs and apply retention

```sh
# Suppress a business and its stable identifiers; only hashes are stored.
python3 -m vienna_leads suppress --db data/vienna-leads.sqlite3 --place-id 123 --reason 'requested opt-out'

# Or hash a value immediately without storing the value itself.
python3 -m vienna_leads suppress --db data/vienna-leads.sqlite3 \
  --kind email --value 'contact@example.invalid'

# Keep derived phone/email only for explicitly qualified records.
python3 -m vienna_leads qualify-contact --db data/vienna-leads.sqlite3 --place-id 123
python3 -m vienna_leads purge-contact-data --db data/vienna-leads.sqlite3 --retention-days 90
```

The default retention period is 90 days for unqualified derived contact data. Purging removes derived phone/email and leaves provenance/raw source payloads available for audit. Suppression records contain only a hash, kind, reason, and timestamp, so an opt-out can continue to match future imports without retaining the opted-out value.

### 5. Generate local review artifacts

```sh
python3 -m vienna_leads export \
  --db data/vienna-leads.sqlite3 \
  --out-dir exports/vienna-review
```

This writes:

- `review.html`: self-contained static HTML with no remote assets or scripts;
- `leads.csv`: tabular review data and attribution comment lines;
- `leads.json`: metadata, provenance, records, scores, and candidate links;
- `review.md`: a readable local Markdown review.

Every generated format includes both required notices: OpenStreetMap contributors and ODbL 1.0, plus City of Vienna and CC BY 4.0 metadata (including license URLs).

### 6. Launch the local interactive dashboard

The dependency-free dashboard reads the SQLite database and binds **only** to `127.0.0.1`. It provides lead search/filter/sort, score explanations, provenance, duplicate candidates, suppression/review state, existing draft links, and three selectable draft templates. It has no telemetry, external assets, or hosted mode.

```sh
export PYTHONPATH="$PWD/src"
python3 -m vienna_leads dashboard \
  --db data/vienna-leads.sqlite3 \
  --draft-dir drafts \
  --port 8765
```

Then review `http://127.0.0.1:8765/` in a local browser. Stop the server with `Ctrl-C`. The port may be `0` for an ephemeral local test port; the host is intentionally not configurable.

The dashboard exposes only local review routes such as `/api/leads`, `/api/leads/<id>`, `/api/duplicates`, `/api/templates`, `/api/draft-preview/<id>?template=<id>`, and `/api/drafts`. Selecting Preview shows an email-safe rendered HTML alternative in a sandboxed local frame; the plain-text/Markdown source is available via a toggle. Lead values are escaped, and the preview uses inline styles only with no scripts, remote assets, tracking pixels, or external fonts. A draft POST writes a deterministic local Markdown or RFC 5322 `.eml` file and reports `delivery: none`; it cannot send mail. The exactly three templates are:

- `friendly-refresh`: friendly website refresh;
- `practical-visibility`: practical online visibility and booking improvement;
- `premium-concept`: premium custom website concept.

### 7. Write an unsent proposal draft

Score first, inspect locally, and confirm any score manually if a value over 70 is appropriate. The `--template` flag accepts exactly the three dashboard templates above and defaults to `friendly-refresh`:

```sh
python3 -m vienna_leads confirm-score \
  --db data/vienna-leads.sqlite3 --place-id 123 --score 82

python3 -m vienna_leads draft \
  --db data/vienna-leads.sqlite3 --place-id 123 \
  --out drafts/123.md --format md --template friendly-refresh

python3 -m vienna_leads draft \
  --db data/vienna-leads.sqlite3 --place-id 123 \
  --out drafts/123.eml --format eml \
  --from 'reviewer@example.invalid' --to 'explicit-recipient@example.invalid'
```

Markdown and RFC 5322 `.eml` files are written locally only. Existing Markdown draft content remains the plain-text/source draft. `.eml` files contain a directly usable RFC 5322 `multipart/alternative` message with both `text/plain` and inline-only `text/html` parts. There is no mail transport, queue, recipient discovery, tracking, or send command. `.eml` generation requires an explicit recipient; no address is guessed. Suppressed records cannot produce drafts.

## Data and scoring model

SQLite tables are created by `src/vienna_leads/db.py`:

- `source_payloads` stores exact raw bytes, content type, encoding, capture time, and SHA-256;
- `source_runs` stores endpoint/cache/request metadata and attribution/license;
- `records` stores normalized business fields plus the original row JSON;
- `provenance` links every record to its source run and source key;
- `duplicate_candidates` stores review suggestions and status, never merged records;
- `scores` stores model version, automated score, final score, reason codes, explanations, confidence, and human-confirmation state;
- `suppression_records` stores only hashed opt-out identities.

The current model is `website-opportunity-v1`. It uses only observed business/web fields and never fetches a website:

| Signal | Automated points |
| --- | ---: |
| no website listed | +60 |
| social profile only | +45 |
| HTTP-only website | +20 |
| HTTPS website present | +5 |
| explicit restaurant category | +10 |

Automated values are clamped to 0–70. A human can explicitly confirm a 0–100 final score with `confirm-score`; rescoring preserves that confirmed value while refreshing the automated score and explanations. Confidence describes completeness of observable business fields and source coverage, not a person or response likelihood.

## Safety and attribution boundaries

- Source payloads and provenance are preserved for reproducibility; raw payloads are not used to infer personal traits.
- A source's terms should be reviewed before import. The generated artifacts carry ODbL and CC BY 4.0 notices even when a database is empty.
- Duplicate matching is a suggestion only. Reviewers decide whether records represent the same business.
- All proposal wording is a generic review starting point and must be checked by a human. Nothing in this repository sends cold outreach.
- The MVP is intentionally local and bounded; it does not provide a crawler, bulk geocoder, directory scraper, hosted (non-local) review UI, CRM, or email delivery system.
