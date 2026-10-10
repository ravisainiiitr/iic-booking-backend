"""Compose and send an email to one or more facility user groups.

Privacy: every recipient gets their own email (only their address in To), so recipients never see each other.

CC / BCC (``cc_mode``):
* ``summary`` (default): the CC and BCC addresses get ONE copy of the message, headed with the groups and
  recipient count, instead of one copy per recipient.
* ``each``: CC and BCC are added to every recipient's email. Limited to ``EACH_MODE_MAX_RECIPIENTS`` recipients
  so a CC'd address is not flooded.

No double sends: recipient rows are created when the email is queued (unique per address) and each row is claimed
(pending -> sending) before it is sent. A retried / duplicated task skips rows already claimed; "Retry failed"
only re-queues rows that failed. A rare row left in "sending" by a crashed worker is never re-sent automatically.
Submitting the same compose form twice is caught by the client's idempotency key.
"""

from __future__ import annotations

import colorsys
import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives, get_connection
from django.core.validators import validate_email
from django.db import IntegrityError, transaction
from django.db.models import Count, F
from django.utils import timezone
from django.utils.html import escape

from iic_booking.communication.email_branding import (
    COLOR_MUTED,
    COLOR_SOFT_PANEL_BG,
    COLOR_SOFT_PANEL_BORDER,
    COLOR_TEXT,
    branded_plain_footer,
    wrap_email_html,
)
from iic_booking.equipment.rich_text import looks_like_html, rich_text_to_plain, sanitize_rich_text
from iic_booking.users.display import get_user_display_name
from iic_booking.users.models import User

from .audience import AudienceFilters, audience_of, recipient_user_ids
from .models import (
    CampaignStatus,
    CcMode,
    FacilityUserGroup,
    GroupEmailAttachment,
    GroupEmailCampaign,
    GroupEmailRecipient,
    RecipientStatus,
)

logger = logging.getLogger(__name__)

SUBJECT_MAX = 200
BODY_MAX = 100_000
MAX_CC = 20
MAX_GROUPS = 50
EACH_MODE_MAX_RECIPIENTS = 200
MAX_ATTACHMENTS = 5
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_TOTAL_ATTACHMENT_BYTES = 10 * 1024 * 1024
BLOCKED_EXTENSIONS = {
    ".exe", ".bat", ".cmd", ".com", ".js", ".jse", ".vbs", ".vbe", ".msi", ".scr", ".ps1", ".sh", ".jar", ".dll",
    ".hta", ".lnk", ".reg",
}


def max_recipients() -> int:
    return int(getattr(settings, "FACILITY_GROUP_EMAIL_MAX_RECIPIENTS", 5000))


def batch_size() -> int:
    return max(1, int(getattr(settings, "FACILITY_GROUP_EMAIL_BATCH_SIZE", 50)))


def batch_pause_seconds() -> int:
    return max(0, int(getattr(settings, "FACILITY_GROUP_EMAIL_BATCH_PAUSE_SECONDS", 5)))


class GroupEmailError(ValueError):
    def __init__(self, message: str, *, field_name: str = "", status: int = 400, extra: Optional[dict] = None):
        super().__init__(message)
        self.field = field_name
        self.status = status
        self.extra = extra or {}


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def parse_addresses(value: Any, field_name: str) -> list[str]:
    if value is None or value == "":
        return []
    items = value if isinstance(value, (list, tuple)) else re.split(r"[,;\s]+", str(value))
    out: list[str] = []
    for item in items:
        addr = str(item or "").strip().strip("<>").lower()
        if not addr:
            continue
        try:
            validate_email(addr)
        except ValidationError as exc:
            raise GroupEmailError(f"{addr} is not a valid email address.", field_name=field_name) from exc
        if addr not in out:
            out.append(addr)
    if len(out) > MAX_CC:
        raise GroupEmailError(f"At most {MAX_CC} {field_name.upper()} addresses.", field_name=field_name)
    return out


def parse_group_ids(value: Any) -> list[int]:
    if isinstance(value, str):
        value = [v for v in value.split(",") if v.strip()]
    try:
        ids = [int(v) for v in (value or [])]
    except (TypeError, ValueError) as exc:
        raise GroupEmailError("Groups must be ids.", field_name="group_ids") from exc
    if not ids:
        raise GroupEmailError("Select at least one group.", field_name="group_ids")
    if len(ids) > MAX_GROUPS:
        raise GroupEmailError(f"Select at most {MAX_GROUPS} groups.", field_name="group_ids")
    return list(dict.fromkeys(ids))


def load_groups(ids: list[int]) -> list[FacilityUserGroup]:
    groups = list(FacilityUserGroup.objects.filter(pk__in=ids))
    if len(groups) != len(ids):
        raise GroupEmailError("One of the selected groups no longer exists.", field_name="group_ids", status=404)
    order = {pk: i for i, pk in enumerate(ids)}
    return sorted(groups, key=lambda g: order[g.pk])


@dataclass
class Draft:
    subject: str
    body_html: str
    body_text: str
    cc: list[str]
    bcc: list[str]
    cc_mode: str
    reply_to: str


def parse_draft(data: Any, *, require_body: bool = True) -> Draft:
    subject = re.sub(r"\s+", " ", str(data.get("subject") or "")).strip()
    if not subject:
        raise GroupEmailError("Enter a subject.", field_name="subject")
    if len(subject) > SUBJECT_MAX:
        raise GroupEmailError(f"Keep the subject under {SUBJECT_MAX} characters.", field_name="subject")
    raw_body = str(data.get("body_html") or data.get("body") or "")
    if len(raw_body) > BODY_MAX:
        raise GroupEmailError("The message is too long.", field_name="body_html")
    body_html = sanitize_rich_text(raw_body)
    body_text = rich_text_to_plain(body_html) if looks_like_html(body_html) else body_html
    if require_body and not body_text.strip():
        raise GroupEmailError("Write the message.", field_name="body_html")
    cc_mode = str(data.get("cc_mode") or CcMode.SUMMARY).strip().lower()
    if cc_mode not in CcMode.values:
        raise GroupEmailError("Choose how CC / BCC are sent.", field_name="cc_mode")
    reply_to = parse_addresses(data.get("reply_to"), "reply_to")
    return Draft(
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        cc=parse_addresses(data.get("cc"), "cc"),
        bcc=parse_addresses(data.get("bcc"), "bcc"),
        cc_mode=cc_mode,
        reply_to=reply_to[0] if reply_to else "",
    )


def validate_attachments(files: Iterable[Any]) -> list[Any]:
    files = [f for f in files if f]
    if len(files) > MAX_ATTACHMENTS:
        raise GroupEmailError(f"Attach at most {MAX_ATTACHMENTS} files.", field_name="attachments")
    total = 0
    for f in files:
        name = str(getattr(f, "name", "") or "attachment")
        ext = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
        if ext in BLOCKED_EXTENSIONS:
            raise GroupEmailError(f"{name}: this file type cannot be emailed.", field_name="attachments")
        size = int(getattr(f, "size", 0) or 0)
        if size > MAX_ATTACHMENT_BYTES:
            raise GroupEmailError(f"{name} is larger than 5 MB.", field_name="attachments")
        total += size
    if total > MAX_TOTAL_ATTACHMENT_BYTES:
        raise GroupEmailError("Attachments add up to more than 10 MB.", field_name="attachments")
    return files


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------


def _recipient_users(groups: list[FacilityUserGroup], f: AudienceFilters):
    ids = recipient_user_ids(groups, f)
    return User.objects.filter(pk__in=ids).select_related("department").order_by("name", "email")


@dataclass
class RecipientPreview:
    total: int
    without_email: int
    recipients: list[dict]


def resolve_recipients(groups: list[FacilityUserGroup], f: AudienceFilters) -> RecipientPreview:
    seen: set[str] = set()
    rows: list[dict] = []
    without_email = 0
    for user in _recipient_users(groups, f):
        email = (user.email or "").strip().lower()
        if not email:
            without_email += 1
            continue
        if email in seen:
            continue
        seen.add(email)
        department = user.department
        rows.append(
            {
                "user_id": user.pk,
                "name": get_user_display_name(user),
                "email": email,
                "department_id": department.pk if department else None,
                "department_name": department.name if department else "",
                "audience": audience_of(user),
                "user_type": user.user_type or "",
                "user_type_label": user.get_user_type_display_label() or "",
            }
        )
    return RecipientPreview(total=len(rows), without_email=without_email, recipients=rows)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _hsl(h: float, s: float, light: float) -> str:
    r, g, b = colorsys.hls_to_rgb(h / 360, light / 100, s / 100)
    return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))


TEXT_COLOURS = {
    "red": _hsl(0, 74, 42), "orange": _hsl(17, 88, 40), "amber": _hsl(35, 92, 33), "green": _hsl(142, 72, 29),
    "blue": _hsl(224, 76, 48), "purple": _hsl(272, 72, 47), "pink": _hsl(336, 78, 42), "gray": _hsl(215, 14, 34),
}
HIGHLIGHT_COLOURS = {
    "yellow": _hsl(53, 98, 77), "orange": _hsl(32, 98, 83), "green": _hsl(141, 79, 85), "blue": _hsl(214, 95, 87),
    "pink": _hsl(326, 85, 90),
}
FONTS = {
    "sans": "Arial, Helvetica, sans-serif",
    "serif": "'Times New Roman', Times, Georgia, serif",
    "mono": "Consolas, 'Courier New', monospace",
    "verdana": "Verdana, Geneva, sans-serif",
    "tahoma": "Tahoma, Verdana, sans-serif",
    "trebuchet": "'Trebuchet MS', sans-serif",
    "georgia": "Georgia, 'Times New Roman', serif",
    "garamond": "Garamond, 'Times New Roman', serif",
    "courier": "'Courier New', Courier, monospace",
    "devanagari": "'Nirmala UI', Mangal, 'Noto Sans Devanagari', sans-serif",
}
_TOKEN = re.compile(r"var\(\s*--rt-(hl-)?(font-|size-)?([a-z0-9]+)\s*(?:,[^)]*)?\)")


def _token_value(match: re.Match) -> str:
    is_hl, kind, name = match.group(1), match.group(2), match.group(3)
    if kind == "font-":
        return FONTS.get(name, FONTS["sans"])
    if kind == "size-":
        return f"{int(name)}pt" if name.isdigit() else "inherit"
    if is_hl:
        return HIGHLIGHT_COLOURS.get(name, "transparent")
    return TEXT_COLOURS.get(name, "inherit")


def email_safe_html(html: str) -> str:
    """Sanitised editor HTML with palette tokens replaced by real colours / fonts / sizes for mail clients."""
    if not html:
        return ""
    if not looks_like_html(html):
        paragraphs = [p for p in re.split(r"\n\s*\n", html) if p.strip()]
        return "".join(f"<p>{escape(p).replace(chr(10), '<br/>')}</p>" for p in paragraphs)
    out = _TOKEN.sub(_token_value, html)
    out = out.replace("<p>", '<p style="margin:0 0 12px 0;">').replace(
        "<mark>", f'<mark style="background-color:{HIGHLIGHT_COLOURS["yellow"]};">'
    )
    return out


_PLACEHOLDER = re.compile(r"\{\{\s*(name|email|department)\s*\}\}", re.I)


def _fill(text: str, values: dict[str, str], *, html: bool) -> str:
    def repl(m: re.Match) -> str:
        value = values.get(m.group(1).lower(), "")
        return escape(value) if html else value

    return _PLACEHOLDER.sub(repl, text)


def render(
    draft_or_campaign: Any,
    *,
    name: str = "",
    email: str = "",
    department: str = "",
    banner_html: str = "",
    banner_text: str = "",
) -> tuple[str, str, str]:
    """(subject, text, html) for one recipient. ``{{ name }}``, ``{{ email }}`` and ``{{ department }}`` are filled in."""
    values = {"name": name or "Sir/Madam", "email": email, "department": department}
    subject = draft_or_campaign.subject
    body_html = _fill(email_safe_html(draft_or_campaign.body_html), values, html=True)
    body_text = _fill(draft_or_campaign.body_text, values, html=False)
    inner = (
        f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;line-height:1.65;color:{COLOR_TEXT};">'
        f"{banner_html}{body_html}</div>"
    )
    html = wrap_email_html(title=subject, body_inner_html=inner, preheader=body_text[:120])
    text = "\n".join(part for part in (banner_text, body_text, branded_plain_footer()) if part)
    return subject, text, html


def _summary_banner(campaign: GroupEmailCampaign) -> tuple[str, str]:
    groups = ", ".join(campaign.group_names) or "selected groups"
    line = (
        f"Copy for CC / BCC: this message is being sent individually to {campaign.total_recipients} "
        f"recipient{'s' if campaign.total_recipients != 1 else ''} in {groups}."
    )
    html = (
        f'<div style="margin:0 0 18px 0;padding:10px 14px;border:1px solid {COLOR_SOFT_PANEL_BORDER};'
        f'background:{COLOR_SOFT_PANEL_BG};border-radius:10px;font-size:13px;color:{COLOR_MUTED};">{escape(line)}</div>'
    )
    return html, line + "\n"


def _deliveries(address: str, subject: str) -> tuple[list[str], str]:
    from iic_booking.users.test_accounts import redirect_email_address

    to, new_subject = redirect_email_address(address, subject=subject)
    return list(to or []), new_subject or subject


def _message(
    *,
    subject: str,
    text: str,
    html: str,
    to: list[str],
    cc: Optional[list[str]] = None,
    bcc: Optional[list[str]] = None,
    reply_to: str = "",
    attachments: Iterable[tuple[str, bytes, str]] = (),
    connection=None,
) -> EmailMultiAlternatives:
    msg = EmailMultiAlternatives(
        subject=subject,
        body=text,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=to,
        cc=cc or None,
        bcc=bcc or None,
        reply_to=[reply_to] if reply_to else None,
        connection=connection,
    )
    msg.attach_alternative(html, "text/html")
    for filename, content, content_type in attachments:
        msg.attach(filename, content, content_type or None)
    return msg


def _read_uploads(files: Iterable[Any]) -> list[tuple[str, bytes, str]]:
    out = []
    for f in files:
        f.seek(0)
        out.append((str(f.name), f.read(), getattr(f, "content_type", "") or ""))
        f.seek(0)
    return out


# ---------------------------------------------------------------------------
# Public operations
# ---------------------------------------------------------------------------


def send_test(user: User, data: Any, files: Iterable[Any] = ()) -> str:
    """Send the draft to the signed-in administrator only (no CC / BCC)."""
    draft = parse_draft(data)
    uploads = validate_attachments(files)
    address = (user.email or "").strip()
    if not address:
        raise GroupEmailError("Your account has no email address.")
    department = getattr(user, "department", None)
    subject, text, html = render(
        draft, name=get_user_display_name(user), email=address, department=department.name if department else ""
    )
    to, subject = _deliveries(address, f"[TEST] {subject}")
    if not to:
        raise GroupEmailError("Test emails for this account are not delivered (no test inbox configured).")
    _message(subject=subject, text=text, html=html, to=to, reply_to=draft.reply_to, attachments=_read_uploads(uploads)).send(
        fail_silently=False
    )
    return to[0]


def create_campaign(user: User, data: Any, files: Iterable[Any] = ()) -> tuple[GroupEmailCampaign, bool]:
    """Queue a group email. Returns (campaign, created); a repeated idempotency key returns the first campaign."""
    key = str(data.get("idempotency_key") or "").strip()[:64] or None
    if key:
        existing = GroupEmailCampaign.objects.filter(idempotency_key=key).first()
        if existing is not None:
            return existing, False
    draft = parse_draft(data)
    groups = load_groups(parse_group_ids(data.get("group_ids")))
    filters = AudienceFilters.from_data(data.get("filters") or {})
    uploads = validate_attachments(files)
    preview = resolve_recipients(groups, filters)
    if preview.total == 0:
        raise GroupEmailError("No recipients match the selected groups and filters.", field_name="group_ids")
    if preview.total > max_recipients():
        raise GroupEmailError(
            f"{preview.total} recipients is more than the limit of {max_recipients()}; narrow the filters.",
            field_name="group_ids",
        )
    if draft.cc_mode == CcMode.EACH and (draft.cc or draft.bcc) and preview.total > EACH_MODE_MAX_RECIPIENTS:
        raise GroupEmailError(
            f"CC / BCC on every email is allowed up to {EACH_MODE_MAX_RECIPIENTS} recipients; "
            "use the single summary copy instead.",
            field_name="cc_mode",
        )
    expected = data.get("expected_recipients")
    try:
        expected = int(expected) if expected not in (None, "") else None
    except (TypeError, ValueError):
        expected = None
    if expected is not None and expected != preview.total:
        raise GroupEmailError(
            f"The recipient list changed to {preview.total}; review the preview and send again.",
            status=409,
            extra={"total": preview.total},
        )
    try:
        with transaction.atomic():
            campaign = GroupEmailCampaign.objects.create(
                subject=draft.subject,
                body_html=draft.body_html,
                body_text=draft.body_text,
                group_names=[g.name for g in groups],
                filters=filters.to_json(),
                cc=draft.cc,
                bcc=draft.bcc,
                cc_mode=draft.cc_mode,
                reply_to=draft.reply_to,
                total_recipients=preview.total,
                idempotency_key=key,
                created_by=user,
            )
            campaign.groups.set(groups)
            GroupEmailRecipient.objects.bulk_create(
                [
                    GroupEmailRecipient(
                        campaign=campaign,
                        user_id=r["user_id"],
                        email=r["email"],
                        name=r["name"][:255],
                        department_name=(r["department_name"] or "")[:255],
                    )
                    for r in preview.recipients
                ],
                batch_size=500,
            )
            for f in uploads:
                attachment = GroupEmailAttachment(
                    campaign=campaign,
                    filename=str(f.name)[:255],
                    content_type=(getattr(f, "content_type", "") or "")[:120],
                    size=int(getattr(f, "size", 0) or 0),
                )
                attachment.file.save(str(f.name), f, save=False)
                attachment.save()
    except IntegrityError:
        if key:
            existing = GroupEmailCampaign.objects.filter(idempotency_key=key).first()
            if existing is not None:
                return existing, False
        raise
    transaction.on_commit(lambda: dispatch(campaign.pk))
    return campaign, True


def dispatch(campaign_id: int, *, countdown: int = 0) -> bool:
    from .tasks import send_group_email

    try:
        if countdown:
            send_group_email.apply_async((campaign_id,), countdown=countdown)
        else:
            send_group_email.delay(campaign_id)
        return True
    except Exception as exc:
        logger.warning("group email %s: could not queue the send task", campaign_id, exc_info=True)
        GroupEmailCampaign.objects.filter(pk=campaign_id).update(
            last_error=f"Could not queue sending ({exc.__class__.__name__}); use Resume to try again."
        )
        return False


def _load_attachments(campaign: GroupEmailCampaign) -> list[tuple[str, bytes, str]]:
    out = []
    for attachment in campaign.attachments.all():
        with attachment.file.open("rb") as fh:
            out.append((attachment.filename, fh.read(), attachment.content_type))
    return out


def refresh_counts(campaign: GroupEmailCampaign) -> GroupEmailCampaign:
    counts = dict(campaign.recipients.values_list("status").annotate(n=Count("pk")).order_by())
    pending = counts.get(RecipientStatus.PENDING, 0)
    sending = counts.get(RecipientStatus.SENDING, 0)
    campaign.sent_count = counts.get(RecipientStatus.SENT, 0)
    campaign.failed_count = counts.get(RecipientStatus.FAILED, 0)
    campaign.skipped_count = counts.get(RecipientStatus.SKIPPED, 0)
    fields = ["sent_count", "failed_count", "skipped_count"]
    if not pending and not sending and campaign.status in (CampaignStatus.QUEUED, CampaignStatus.SENDING):
        if campaign.failed_count == 0:
            campaign.status = CampaignStatus.SENT
        elif campaign.sent_count == 0:
            campaign.status = CampaignStatus.FAILED
        else:
            campaign.status = CampaignStatus.PARTIAL
        campaign.finished_at = timezone.now()
        fields += ["status", "finished_at"]
    campaign.save(update_fields=fields)
    return campaign


def _log(campaign: GroupEmailCampaign, recipient: GroupEmailRecipient, *, ok: bool, error: str = "") -> None:
    try:
        from iic_booking.communication.models import CommunicationLog

        CommunicationLog.objects.create(
            communication_type=CommunicationLog.CommunicationType.EMAIL,
            recipient_id=recipient.user_id,
            recipient_email=recipient.email,
            subject=campaign.subject[:255],
            message=campaign.body_text[:2000],
            status=CommunicationLog.CommunicationStatus.SENT if ok else CommunicationLog.CommunicationStatus.FAILED,
            sent_at=timezone.now() if ok else None,
            error_message=error,
            metadata={"group_email_campaign": campaign.pk},
            created_by_id=campaign.created_by_id,
        )
    except Exception:
        logger.debug("group email: communication log not written", exc_info=True)


def _send_summary(campaign: GroupEmailCampaign, attachments, connection) -> None:
    if campaign.cc_mode != CcMode.SUMMARY or not (campaign.cc or campaign.bcc) or campaign.summary_sent_at:
        return
    claimed = GroupEmailCampaign.objects.filter(pk=campaign.pk, summary_sent_at__isnull=True).update(
        summary_sent_at=timezone.now()
    )
    if not claimed:
        return
    banner_html, banner_text = _summary_banner(campaign)
    subject, text, html = render(campaign, name="", banner_html=banner_html, banner_text=banner_text)
    try:
        _message(
            subject=subject,
            text=text,
            html=html,
            to=list(campaign.cc),
            bcc=list(campaign.bcc),
            reply_to=campaign.reply_to,
            attachments=attachments,
            connection=connection,
        ).send(fail_silently=False)
    except Exception as exc:
        logger.warning("group email %s: CC/BCC summary copy failed", campaign.pk, exc_info=True)
        GroupEmailCampaign.objects.filter(pk=campaign.pk).update(
            last_error=f"CC / BCC copy failed: {str(exc)[:300]}"
        )


def process_campaign(campaign_id: int, *, limit: Optional[int] = None) -> dict:
    """Send the next batch of pending recipients. Returns ``{"remaining": bool, ...}``."""
    campaign = GroupEmailCampaign.objects.filter(pk=campaign_id).first()
    if campaign is None or campaign.status == CampaignStatus.CANCELLED:
        return {"remaining": False, "sent": 0, "failed": 0}
    if campaign.started_at is None or campaign.status == CampaignStatus.QUEUED:
        GroupEmailCampaign.objects.filter(pk=campaign.pk).update(
            status=CampaignStatus.SENDING, started_at=campaign.started_at or timezone.now()
        )
        campaign.refresh_from_db()
    attachments = _load_attachments(campaign)
    batch = list(campaign.recipients.filter(status=RecipientStatus.PENDING).order_by("pk")[: limit or batch_size()])
    sent = failed = 0
    connection = get_connection()
    try:
        connection.open()
    except Exception:
        logger.debug("group email: connection pre-open failed; sending will retry per message", exc_info=True)
    try:
        _send_summary(campaign, attachments, connection)
        each_cc = campaign.cc_mode == CcMode.EACH
        for recipient in batch:
            claimed = GroupEmailRecipient.objects.filter(pk=recipient.pk, status=RecipientStatus.PENDING).update(
                status=RecipientStatus.SENDING, attempts=F("attempts") + 1
            )
            if not claimed:
                continue
            subject, text, html = render(
                campaign, name=recipient.name, email=recipient.email, department=recipient.department_name
            )
            to, subject = _deliveries(recipient.email, subject)
            if not to:
                GroupEmailRecipient.objects.filter(pk=recipient.pk).update(
                    status=RecipientStatus.SKIPPED, error="Test account with no test inbox configured."
                )
                continue
            try:
                _message(
                    subject=subject,
                    text=text,
                    html=html,
                    to=to,
                    cc=list(campaign.cc) if each_cc else None,
                    bcc=list(campaign.bcc) if each_cc else None,
                    reply_to=campaign.reply_to,
                    attachments=attachments,
                    connection=connection,
                ).send(fail_silently=False)
            except Exception as exc:
                failed += 1
                error = f"{exc.__class__.__name__}: {str(exc)[:500]}"
                GroupEmailRecipient.objects.filter(pk=recipient.pk).update(status=RecipientStatus.FAILED, error=error)
                _log(campaign, recipient, ok=False, error=error)
                continue
            sent += 1
            GroupEmailRecipient.objects.filter(pk=recipient.pk).update(
                status=RecipientStatus.SENT, sent_at=timezone.now(), error=""
            )
            _log(campaign, recipient, ok=True)
    finally:
        try:
            connection.close()
        except Exception:
            pass
    campaign.refresh_from_db()
    refresh_counts(campaign)
    remaining = campaign.recipients.filter(status=RecipientStatus.PENDING).exists()
    return {"remaining": remaining, "sent": sent, "failed": failed}


def resume(campaign: GroupEmailCampaign, *, retry_failed: bool) -> GroupEmailCampaign:
    if campaign.status == CampaignStatus.CANCELLED:
        raise GroupEmailError("This email was cancelled.", status=409)
    if retry_failed:
        campaign.recipients.filter(status=RecipientStatus.FAILED).update(status=RecipientStatus.PENDING, error="")
    if not campaign.recipients.filter(status=RecipientStatus.PENDING).exists():
        raise GroupEmailError("Nothing left to send.", status=409)
    GroupEmailCampaign.objects.filter(pk=campaign.pk).update(
        status=CampaignStatus.SENDING if campaign.started_at else CampaignStatus.QUEUED,
        finished_at=None,
        last_error="",
    )
    campaign.refresh_from_db()
    transaction.on_commit(lambda: dispatch(campaign.pk))
    return campaign


def cancel(campaign: GroupEmailCampaign) -> GroupEmailCampaign:
    if campaign.status not in (CampaignStatus.QUEUED, CampaignStatus.SENDING):
        raise GroupEmailError("Only an email that is still sending can be cancelled.", status=409)
    campaign.recipients.filter(status=RecipientStatus.PENDING).update(
        status=RecipientStatus.SKIPPED, error="Cancelled before sending."
    )
    GroupEmailCampaign.objects.filter(pk=campaign.pk).update(status=CampaignStatus.CANCELLED, finished_at=timezone.now())
    campaign.refresh_from_db()
    return refresh_counts(campaign)
