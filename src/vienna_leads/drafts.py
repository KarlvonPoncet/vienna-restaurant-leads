"""Local proposal draft rendering; this module has no delivery capability."""

from __future__ import annotations

from email.message import EmailMessage
from email.utils import formatdate
from html import escape
from pathlib import Path
from typing import Any, Mapping


def _value(record: Mapping[str, Any], key: str, fallback: str = "") -> str:
    value = record.get(key, fallback)
    return str(value) if value is not None else fallback


def render_markdown(record: Mapping[str, Any], *, score: Mapping[str, Any] | None = None) -> str:
    name = _value(record, "name", "Restaurant")
    score_value = _value(score or {}, "score", "unscored")
    reasons = (score or {}).get("reason_codes", [])
    if isinstance(reasons, str):
        reasons = [reasons]
    reason_text = ", ".join(str(reason) for reason in reasons) or "manual review"
    website = _value(record, "website") or "No website was listed in the reviewed sources."
    address = _value(record, "address") or "Vienna"
    return f"""# Local proposal draft: {name}

> **Local review only — not sent automatically.**

**Subject:** A website idea for {name}

Hello {name} team,

I am preparing a local, human-reviewed proposal for a simple restaurant website. The public business listing used for this draft shows **{name}** at **{address}**. A reviewer can adapt this note to the restaurant's actual needs before any contact is considered.

## Review context

- Website opportunity score: **{score_value}/100**
- Reason codes: `{reason_text}`
- Current listed website: {website}

Possible scope: a fast mobile page with opening information, menu and location details, accessibility-friendly structure, and an easy-to-maintain contact path. This is a proposal starting point, not a claim about the business or its people.

Please review all wording and recipients locally. This file is a draft only; Vienna Restaurant Leads does not send email or perform outreach.
"""


def write_markdown_draft(path: str | Path, record: Mapping[str, Any], *, score: Mapping[str, Any] | None = None) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_markdown(record, score=score), encoding="utf-8")
    return output


def render_eml(
    record: Mapping[str, Any],
    *,
    sender: str,
    recipient: str,
    score: Mapping[str, Any] | None = None,
) -> str:
    if not sender or not recipient:
        raise ValueError("sender and recipient are required for an .eml draft")
    message = EmailMessage()
    name = _value(record, "name", "Restaurant")
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = f"A website idea for {name}"
    message["Date"] = formatdate(localtime=False)
    message["X-Vienna-Restaurant-Leads-Draft"] = "local-only; not sent"
    message.set_content(render_markdown(record, score=score))
    return message.as_string()


def write_eml_draft(
    path: str | Path,
    record: Mapping[str, Any],
    *,
    sender: str,
    recipient: str,
    score: Mapping[str, Any] | None = None,
) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_eml(record, sender=sender, recipient=recipient, score=score),
        encoding="utf-8",
    )
    return output
