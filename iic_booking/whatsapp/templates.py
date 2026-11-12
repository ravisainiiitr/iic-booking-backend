"""Compare the catalogue with Meta, submit what is missing, track statuses and keep an audit trail. Sends no messages.

Rules: a name already at Meta is never submitted again. A rejected template is corrected only on request, only when its
wording changed since it was last submitted, and only MAX_CORRECTIONS times. A rate limit stops the batch.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field

from django.db import DatabaseError, transaction
from django.utils import timezone

from . import conf
from .models import WhatsAppTemplate, WhatsAppTemplateAudit
from .template_catalogue import BY_EVENT, CATALOGUE, Template, validate
from .template_client import TemplateApiError, TemplateClient, clean

logger = logging.getLogger(__name__)

DEFAULT_BATCH = 5
MAX_BATCH = 20
DEFAULT_DELAY_SECONDS = 2.0
MAX_CORRECTIONS = 3
_SENDABLE = WhatsAppTemplate.Status.APPROVED
_SUGGESTIONS = {
    "INVALID_FORMAT": "Check the variables: in order from {{1}}, not at the very start or end, and with enough words around them.",
    "TAG_CONTENT_MISMATCH": "The wording does not fit its category. Keep it purely about this booking, or use the category Meta suggests.",
    "PROMOTIONAL": "Remove anything that reads as an offer or encouragement. Utility wording only states what happened.",
    "ABUSIVE_CONTENT": "Review the wording against WhatsApp's policies, then edit it or appeal in WhatsApp Manager.",
    "SCAM": "Make the sender and purpose clear, and avoid urgent or payment-pressure wording. An appeal may be needed.",
    "INCORRECT_CATEGORY": "Use the category Meta indicates, or reword the template to fit Utility.",
}
_DEFAULT_SUGGESTION = "Read the reason in WhatsApp Manager or Business Support Home, then change the wording in the catalogue."


class TemplateConfigError(Exception):
    pass


@dataclass
class Plan:
    meta_rows: list[dict]
    to_submit: list[Template] = field(default_factory=list)
    existing: list[tuple[Template, dict]] = field(default_factory=list)
    not_in_catalogue: list[dict] = field(default_factory=list)


def audit(action: str, *, name: str = "", language: str = "", actor: str = "", ok: bool = True, error: TemplateApiError | None = None,
          detail: dict | None = None) -> None:
    """The audit trail must not stop the work, or hide it: a failure to write is logged instead."""
    try:
        with transaction.atomic():
            WhatsAppTemplateAudit.objects.create(
                action=action, template_name=name, language=language, actor=actor[:100], ok=ok,
                http_status=error.http_status if error else None, meta_code=str(error.code) if error and error.code is not None else "",
                detail={**(detail or {}), **({"kind": error.kind, "message": str(error)} if error else {})},
            )
    except DatabaseError as exc:
        logger.warning("whatsapp template audit not stored: %s", type(exc).__name__)


def check_ready(client: TemplateClient) -> None:
    """Refuses a wrong account before anything else: templates must go to the institutional WABA."""
    problems = validate()
    if problems:
        raise TemplateConfigError("The catalogue breaks Meta's rules: " + "; ".join(problems))
    if not conf.waba_id():
        raise TemplateConfigError("WHATSAPP_WABA_ID is not set")
    if not conf.access_token():
        raise TemplateConfigError("WHATSAPP_ACCESS_TOKEN is not set")
    expected = conf.expected_waba_name()
    actual = client.waba_name()
    if actual != expected:
        raise TemplateConfigError("WHATSAPP_WABA_ID is not the expected account")


def build_plan(client: TemplateClient) -> Plan:
    rows = client.list_templates()
    by_key = {(r.get("name"), r.get("language")): r for r in rows}
    catalogue_keys = {(t.name, t.language) for t in CATALOGUE}
    plan = Plan(meta_rows=rows, not_in_catalogue=[r for r in rows if (r.get("name"), r.get("language")) not in catalogue_keys])
    for template in CATALOGUE:
        row = by_key.get((template.name, template.language))
        if row is None:
            plan.to_submit.append(template)
        else:
            plan.existing.append((template, row))
    return plan


def _meta_wording(row: dict) -> dict:
    parts = {str(c.get("type") or "").upper(): c.get("text") or "" for c in row.get("components") or [] if isinstance(c, dict)}
    return {"header": parts.get("HEADER", ""), "body": parts.get("BODY", ""), "footer": parts.get("FOOTER", "")}


def wording_differs(template: Template, row: dict) -> bool:
    if template.is_authentication:  # Meta writes this wording itself, so there is nothing of ours to compare
        return False
    meta = _meta_wording(row)
    return (meta["header"], meta["body"], meta["footer"]) != (template.header, template.body, template.footer)


def _normal_status(value) -> str:
    status = str(value or "").upper()
    return status if status in WhatsAppTemplate.Status.values else WhatsAppTemplate.Status.UNKNOWN


def _store_row(template: Template | None, row: dict, *, actor: str, now) -> WhatsAppTemplate:
    name, language = str(row.get("name") or ""), str(row.get("language") or "")
    record, created = WhatsAppTemplate.objects.get_or_create(waba_id=conf.waba_id(), name=name, language=language)
    previous = record.status
    record.status = _normal_status(row.get("status"))
    record.category = str(row.get("category") or "")[:30]
    reason = str(row.get("rejected_reason") or "")
    record.rejected_reason = "" if reason.upper() == "NONE" else clean(reason)[:200]
    if row.get("id"):
        record.meta_id = str(row["id"])
    record.event = template.event if template else record.event
    record.last_synced_at = now
    record.save()
    if created or previous != record.status:
        audit("status_changed", name=name, language=language, actor=actor,
              detail={"from": "" if created else previous, "to": record.status, "reason": record.rejected_reason})
    return record


def sync_statuses(client: TemplateClient, *, actor: str = "") -> Plan:
    """Fetch from Meta and store status, category, language, rejection reason and the sync time."""
    plan = build_plan(client)
    now = timezone.now()
    known = {(t.name, t.language): t for t in CATALOGUE}
    for row in plan.meta_rows:
        _store_row(known.get((row.get("name"), row.get("language"))), row, actor=actor, now=now)
    audit("synced", actor=actor, detail={"at_meta": len(plan.meta_rows), "missing": len(plan.to_submit)})
    return plan


def submit_missing(client: TemplateClient, *, batch_size: int = DEFAULT_BATCH, delay: float = DEFAULT_DELAY_SECONDS, actor: str = "",
                   sleep=time.sleep) -> dict:
    """Submit up to batch_size missing templates. Fetches from Meta first, so pending ones are never sent again."""
    batch_size = max(1, min(int(batch_size), MAX_BATCH))
    plan = sync_statuses(client, actor=actor)
    result = {"submitted": [], "failed": [], "skipped_existing": [t.name for t, _ in plan.existing], "stopped": "",
              "remaining": max(0, len(plan.to_submit) - batch_size)}
    for index, template in enumerate(plan.to_submit[:batch_size]):
        if index:
            sleep(delay)
        record, _ = WhatsAppTemplate.objects.get_or_create(waba_id=conf.waba_id(), name=template.name, language=template.language)
        record.event, record.category = template.event, template.category
        record.wording_hash = template.wording_hash()
        record.submission_attempts += 1
        record.last_error = ""
        try:
            answer = client.create_template(template.payload())
        except TemplateApiError as exc:
            record.last_error = str(exc)[:500]
            record.save()
            audit("submit_failed", name=template.name, language=template.language, actor=actor, ok=False, error=exc)
            result["failed"].append({"name": template.name, "kind": exc.kind, "message": str(exc)})
            if exc.kind in ("rate_limit", "token_invalid", "permission", "network", "server_error"):
                result["stopped"] = exc.kind
                result["remaining"] = len(plan.to_submit) - index - 1
                break
            continue
        record.meta_id = str(answer.get("id") or "")
        record.status = _normal_status(answer.get("status") or WhatsAppTemplate.Status.PENDING)
        record.category = str(answer.get("category") or template.category)[:30]
        record.submitted_at = record.last_synced_at = timezone.now()
        record.save()
        audit("submitted", name=template.name, language=template.language, actor=actor,
              detail={"status": record.status, "category": record.category})
        result["submitted"].append(template.name)
    return result


def correct_rejected(client: TemplateClient, event_or_name: str, *, actor: str = "") -> dict:
    """One controlled correction of one rejected template. Never automatic: someone must ask, after changing the wording."""
    template = BY_EVENT.get(event_or_name) or next((t for t in CATALOGUE if t.name == event_or_name), None)
    if template is None:
        raise TemplateConfigError("That template is not in the catalogue")
    sync_statuses(client, actor=actor)
    record = WhatsAppTemplate.objects.filter(waba_id=conf.waba_id(), name=template.name, language=template.language).first()
    refusal = ""
    if record is None or record.status != WhatsAppTemplate.Status.REJECTED:
        refusal = "It is not rejected at Meta"
    elif not record.meta_id:
        refusal = "Its Meta template ID is not known"
    elif record.wording_hash and record.wording_hash == template.wording_hash():
        refusal = "Its wording has not changed since it was submitted"
    elif record.correction_attempts >= MAX_CORRECTIONS:
        refusal = f"It has already been corrected {MAX_CORRECTIONS} times; review it in WhatsApp Manager"
    if refusal:
        audit("correction_refused", name=template.name, language=template.language, actor=actor, ok=False, detail={"reason": refusal})
        raise TemplateConfigError(refusal)
    record.correction_attempts += 1
    try:
        client.edit_template(record.meta_id, template.payload()["components"])
    except TemplateApiError as exc:
        record.last_error = str(exc)[:500]
        record.save()
        audit("correction_failed", name=template.name, language=template.language, actor=actor, ok=False, error=exc)
        raise
    record.wording_hash, record.status, record.last_error = template.wording_hash(), WhatsAppTemplate.Status.PENDING, ""
    record.submitted_at = timezone.now()
    record.save()
    audit("corrected", name=template.name, language=template.language, actor=actor, detail={"attempt": record.correction_attempts})
    return {"name": template.name, "attempt": record.correction_attempts}


def suggestion_for(reason: str) -> str:
    key = re.sub(r"[^A-Z_]", "", str(reason or "").upper())
    return _SUGGESTIONS.get(key, _DEFAULT_SUGGESTION)


def build_report(*, meta_rows: list[dict] | None = None) -> dict:
    """Group every catalogue template. Uses the stored statuses, plus wording checks when Meta's rows are given."""
    records = {(r.name, r.language): r for r in WhatsAppTemplate.objects.filter(waba_id=conf.waba_id())}
    rows = {(r.get("name"), r.get("language")): r for r in meta_rows or []}
    report: dict = {"approved": [], "pending": [], "rejected": [], "paused_or_disabled": [], "missing": [], "requires_correction": [],
                    "not_wired": [], "last_synced_at": None}
    for template in CATALOGUE:
        record = records.get((template.name, template.language))
        status = record.status if record else WhatsAppTemplate.Status.NOT_SUBMITTED
        item = {"event": template.event, "name": template.name, "language": template.language, "status": status,
                "category": (record.category if record and record.category else template.category),
                "rejected_reason": record.rejected_reason if record else "", "wired": bool(template.trigger),
                "last_synced_at": record.last_synced_at if record else None, "submitted_at": record.submitted_at if record else None}
        if record and record.last_synced_at and (report["last_synced_at"] is None or record.last_synced_at > report["last_synced_at"]):
            report["last_synced_at"] = record.last_synced_at
        if status == WhatsAppTemplate.Status.NOT_SUBMITTED:
            report["missing"].append(item)
        elif status == WhatsAppTemplate.Status.APPROVED:
            report["approved"].append(item)
        elif status == WhatsAppTemplate.Status.REJECTED:
            item["suggestion"] = suggestion_for(item["rejected_reason"])
            report["rejected"].append(item)
            report["requires_correction"].append({**item, "why": "rejected"})
        elif status in (WhatsAppTemplate.Status.PAUSED, WhatsAppTemplate.Status.DISABLED):
            report["paused_or_disabled"].append(item)
            report["requires_correction"].append({**item, "why": status.lower(), "suggestion": "Check its quality and wording in WhatsApp Manager."})
        else:
            report["pending"].append(item)
        row = rows.get((template.name, template.language))
        if row is not None and wording_differs(template, row):
            report["requires_correction"].append({**item, "why": "wording at Meta differs from the catalogue",
                                                  "suggestion": "Decide which wording is right. Nothing is changed automatically."})
        if item["category"] != template.category:
            report["requires_correction"].append({**item, "why": f"Meta categorised it {item['category']}, the catalogue says {template.category}",
                                                  "suggestion": "Reword it for Utility, or accept the category if it is correct."})
        if not template.trigger:
            report["not_wired"].append(item)
    return report


def is_approved(template_name: str, language: str) -> bool:
    """The gate in front of every send. Unknown, pending, rejected, paused or disabled all mean no."""
    if not conf.waba_id():
        return False
    return WhatsAppTemplate.objects.filter(waba_id=conf.waba_id(), name=template_name, language=language, status=_SENDABLE).exists()
