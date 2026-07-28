"""Local proposal templates and draft rendering; never delivers mail."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from email.message import EmailMessage
from email.utils import formatdate
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True)
class ProposalTemplate:
    """One of the deliberately small, human-reviewable proposal templates."""

    template_id: str
    name: str
    description: str
    subject: str
    approach: str


# Keep this tuple exactly three templates.  The dashboard exposes this catalog
# directly, rather than allowing arbitrary remotely supplied prompt content.
TEMPLATES: tuple[ProposalTemplate, ...] = (
    ProposalTemplate(
        template_id="friendly-refresh",
        name="Friendly website refresh",
        description="A warm, low-pressure refresh for a clearer mobile experience.",
        subject="A friendly website refresh idea for {name}",
        approach="A friendly, mobile-first refresh could make the essential restaurant information easier to browse while keeping the existing character of the business.",
    ),
    ProposalTemplate(
        template_id="practical-visibility",
        name="Practical visibility and booking",
        description="A practical improvement focused on online visibility and clear booking steps.",
        subject="A practical online visibility idea for {name}",
        approach="A practical update could make the listed business easier to find and make the next step for a booking or enquiry clear, without adding unnecessary complexity.",
    ),
    ProposalTemplate(
        template_id="premium-concept",
        name="Premium custom website concept",
        description="A considered custom concept for a distinctive, polished restaurant presence.",
        subject="A premium custom website concept for {name}",
        approach="A premium custom website concept could create a distinctive, polished presentation around the information the restaurant chooses to publish, with a carefully structured mobile experience.",
    ),
)
TEMPLATE_IDS = tuple(template.template_id for template in TEMPLATES)


def template_catalog() -> list[dict[str, str]]:
    return [asdict(template) for template in TEMPLATES]


def _template(template_id: str) -> ProposalTemplate:
    for template in TEMPLATES:
        if template.template_id == template_id:
            return template
    raise ValueError(f"unknown proposal template: {template_id}")


def _value(record: Mapping[str, Any], key: str, fallback: str = "") -> str:
    values = dict(record)
    value = values.get(key, fallback)
    return str(value) if value is not None else fallback


def _score_values(score: Mapping[str, Any] | None) -> tuple[str, list[str]]:
    values = dict(score or {})
    score_value = values.get("score", "unscored")
    reasons = values.get("reason_codes", [])
    if isinstance(reasons, str):
        reasons = [reasons]
    return str(score_value), [str(reason) for reason in reasons]


def _evidence(record: Mapping[str, Any], score: Mapping[str, Any] | None) -> list[str]:
    """Render only fields that are explicitly present in the reviewed record."""
    name = _value(record, "name")
    address = _value(record, "address")
    website = _value(record, "website")
    score_value, reasons = _score_values(score)
    evidence = []
    if name:
        evidence.append(f'- Business name in the reviewed record: **{name}**.')
    if address:
        evidence.append(f'- Address in the reviewed record: **{address}**.')
    if website:
        evidence.append(f"- Website listed in the reviewed record: `{website}`.")
    else:
        evidence.append("- Website field in the reviewed record: **none listed**.")
    if score is not None:
        evidence.append(f"- Automated review score: **{score_value}/100**; reason codes: `{', '.join(reasons) or 'none'}`.")
    return evidence


def render_template(
    template_id: str,
    record: Mapping[str, Any],
    *,
    score: Mapping[str, Any] | None = None,
) -> str:
    """Render a selected template using explicit record fields only."""
    template = _template(template_id)
    name = _value(record, "name") or "the restaurant"
    subject = template.subject.format(name=name)
    evidence = "\n".join(_evidence(record, score))
    return f"""# Local proposal draft: {template.name}

> **Local review only — draft, not sent automatically.**

**Template:** {template.name}
**Subject:** {subject}

Hello,

This is a concise, human-reviewable proposal based only on the explicit fields and evidence shown below. It is addressed to **{name}** only because that name appears in the reviewed record.

{template.approach}

## Explicit review evidence

{evidence}

If this is not relevant, or if you prefer not to receive further proposals, please disregard this note or say **opt out**. That preference will be respected; this tool does not send mail or perform follow-up outreach.

Please review the wording, recipient, and all claims locally before considering any contact.
"""


def render_markdown(record: Mapping[str, Any], *, score: Mapping[str, Any] | None = None) -> str:
    """Backward-compatible default: the friendly website refresh template."""
    return render_template("friendly-refresh", record, score=score)


def write_template_markdown_draft(
    path: str | Path,
    template_id: str,
    record: Mapping[str, Any],
    *,
    score: Mapping[str, Any] | None = None,
) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(render_template(template_id, record, score=score), encoding="utf-8")
    return output


def write_markdown_draft(
    path: str | Path,
    record: Mapping[str, Any],
    *,
    score: Mapping[str, Any] | None = None,
) -> Path:
    return write_template_markdown_draft(path, "friendly-refresh", record, score=score)


def render_eml(
    record: Mapping[str, Any],
    *,
    sender: str,
    recipient: str,
    score: Mapping[str, Any] | None = None,
    template_id: str = "friendly-refresh",
) -> str:
    if not sender or not recipient:
        raise ValueError("sender and recipient are required for an .eml draft")
    template = _template(template_id)
    name = _value(record, "name") or "the restaurant"
    message = EmailMessage()
    message["From"] = sender
    message["To"] = recipient
    message["Subject"] = template.subject.format(name=name)
    message["Date"] = formatdate(localtime=False)
    message["X-Vienna-Restaurant-Leads-Draft"] = "local-only; not sent"
    message.set_content(render_template(template_id, record, score=score))
    return message.as_string()


def write_template_eml_draft(
    path: str | Path,
    template_id: str,
    record: Mapping[str, Any],
    *,
    sender: str,
    recipient: str,
    score: Mapping[str, Any] | None = None,
) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        render_eml(record, sender=sender, recipient=recipient, score=score, template_id=template_id),
        encoding="utf-8",
    )
    return output


def write_eml_draft(
    path: str | Path,
    record: Mapping[str, Any],
    *,
    sender: str,
    recipient: str,
    score: Mapping[str, Any] | None = None,
) -> Path:
    return write_template_eml_draft(
        path,
        "friendly-refresh",
        record,
        sender=sender,
        recipient=recipient,
        score=score,
    )
