"""Monthly usage report emails: recipients, preview, and the idempotent, throttled sender.

One email per recipient per month combines every section that applies to them (faculty wallet, Officer in Charge,
Head of Department). ``MonthlyReportSendLog`` (unique per user and month) is claimed before sending, so re-runs and
continuation batches never send twice. Nothing goes out inside the Wednesday slot-opening peak window, and the whole
run is off until the Main Administrator switches on "Send monthly usage reports".
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, time, timedelta
from typing import Any, Iterable, Optional

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from . import monthly_report_data as data
from . import monthly_report_email as render
from .monthly_report_data import IST, ReportMonth

logger = logging.getLogger(__name__)

TEMPLATE_CODE = "monthly_usage_report_email"
AUDIENCES = ("faculty", "oic", "hod")
AUDIENCE_LABELS = {"faculty": "Faculty", "oic": "Officers in Charge", "hod": "Heads of Department"}
MAX_ATTEMPTS = 3
BATCH_SECONDS = 40
BATCH_SENDS = 40
SEND_GAP_SECONDS = 0.25
REQUEUE_SECONDS = 60
SEND_DAYS = 10
HARD_BLACKOUT_WEEKDAY = 2
HARD_BLACKOUT = (time(20, 55), time(21, 15))


# --------------------------------------------------------------------------- recipients


def active_hod_user_ids() -> set[int]:
    from iic_booking.users.models.channel_i_identity import HeadOfDepartmentAssignment

    today = timezone.localdate()
    return set(
        HeadOfDepartmentAssignment.objects.filter(active=True)
        .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=today))
        .values_list("user_id", flat=True)
    )


def candidate_ids(audiences: Iterable[str] = AUDIENCES) -> dict[str, set[int]]:
    """Active, non-test users per audience (activity is checked later, per report)."""
    from iic_booking.equipment.models import EquipmentManager
    from iic_booking.users.models import User
    from iic_booking.users.models.user_type import UserType

    real = User.objects.filter(is_active=True, is_test_account=False)
    out: dict[str, set[int]] = {}
    if "faculty" in audiences:
        out["faculty"] = set(real.filter(user_type=UserType.FACULTY, wallet__isnull=False).values_list("pk", flat=True))
    if "oic" in audiences:
        managers = EquipmentManager.objects.values_list("manager_id", flat=True)
        out["oic"] = set(real.filter(pk__in=managers).values_list("pk", flat=True))
    if "hod" in audiences:
        out["hod"] = set(real.filter(pk__in=active_hod_user_ids()).values_list("pk", flat=True))
    return out


def recipients_by_user(audiences: Iterable[str]) -> dict[int, set[str]]:
    users: dict[int, set[str]] = {}
    for audience, ids in candidate_ids(audiences).items():
        for uid in ids:
            users.setdefault(uid, set()).add(audience)
    return users


def enabled_audiences(row) -> list[str]:
    return [a for a in AUDIENCES if getattr(row, f"{a}_enabled", False)]


# --------------------------------------------------------------------------- report


_SECTIONS = {
    "faculty": (data.faculty_section, render.faculty_html, render.faculty_text),
    "oic": (data.oic_section, render.oic_html, render.oic_text),
    "hod": (data.hod_section, render.hod_html, render.hod_text),
}


def build_report(user, month: ReportMonth, *, audiences: Iterable[str] = AUDIENCES,
                 include_test: Optional[bool] = None) -> dict[str, Any]:
    sections = {}
    for audience in AUDIENCES:
        if audience not in audiences:
            continue
        section = _SECTIONS[audience][0](user, month, include_test=include_test)
        if section is not None:
            sections[audience] = section
    active = [a for a, s in sections.items() if s.get("has_activity")]
    return {"sections": sections, "active": active, "has_activity": bool(active)}


def resolve_template(*, create: bool = False):
    """The template row (created from the code default when missing, if ``create``), else an unsaved default."""
    from iic_booking.communication.default_email_templates import get_default_email_template

    from .models import CommunicationTemplate

    if create:
        from iic_booking.users.supervisor_invites import _ensure_template

        return _ensure_template(TEMPLATE_CODE)
    row = CommunicationTemplate.objects.filter(
        code=TEMPLATE_CODE, communication_type=CommunicationTemplate.CommunicationType.EMAIL, is_active=True
    ).first()
    if row is not None:
        return row
    spec = get_default_email_template(TEMPLATE_CODE)
    return CommunicationTemplate(
        code=TEMPLATE_CODE,
        communication_type=CommunicationTemplate.CommunicationType.EMAIL,
        **{k: spec[k] for k in ("name", "subject", "body_text", "body_html")},
    )


def _unsubscribe_url(user) -> str:
    from .notification_preferences import MONTHLY_REPORTS, unsubscribe_token

    return render.link(f"/unsubscribe?t={unsubscribe_token(user, MONTHLY_REPORTS)}")


def render_report(user, month: ReportMonth, report: dict[str, Any], *, template=None,
                  show_empty: bool = False) -> dict[str, str]:
    """Subject, plain text and HTML of the email (sections without activity are left out unless ``show_empty``)."""
    from iic_booking.users.display import get_user_display_name

    from .service import CommunicationService

    html_parts, text_parts = [], []
    for audience in AUDIENCES:
        section = report["sections"].get(audience)
        if section is None or not (section.get("has_activity") or show_empty):
            continue
        _, to_html, to_text = _SECTIONS[audience]
        html_parts.append(to_html(section, month))
        text_parts.append(to_text(section, month))
    if not html_parts:
        html_parts.append(render.note_html("Nothing to report for this month."))
        text_parts.append("Nothing to report for this month.")
    context = {
        "user_name": render._e(get_user_display_name(user) or "there"),
        "month_label": month.label,
        "previous_month_label": month.previous.label,
        "report_html": "".join(html_parts),
        "report_text": "\n\n".join(text_parts),
        "settings_url": render.link("/notifications?tab=settings"),
        "unsubscribe_url": _unsubscribe_url(user),
    }
    rendered = CommunicationService.render_template(template or resolve_template(), context)
    return {
        "subject": rendered.get("subject", ""),
        "message": rendered.get("message", ""),
        "html_message": rendered.get("html_message", ""),
    }


def excel_attachment(report: dict[str, Any], month: ReportMonth) -> Optional[tuple[str, bytes, str]]:
    content = render.build_excel(report["sections"], month)
    if not content:
        return None
    return (
        f"monthly-usage-report-{month.key}.xlsx",
        content,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def preview(user, month: ReportMonth, *, audiences: Iterable[str] = AUDIENCES) -> dict[str, Any]:
    """What ``user`` would receive for ``month`` (never sends)."""
    from .notification_preferences import EMAIL, MONTHLY_REPORTS, allows

    report = build_report(user, month, audiences=audiences)
    rendered = render_report(user, month, report, show_empty=not report["has_activity"])
    if getattr(user, "is_test_account", False):
        reason = "Test account: excluded from scheduled sends (use the test send)."
    elif not report["sections"]:
        reason = "Not a faculty wallet owner, Officer in Charge or Head of Department."
    elif not report["has_activity"]:
        reason = "Nothing to report: this user would be skipped."
    elif not allows(user, EMAIL, MONTHLY_REPORTS):
        reason = "This user switched monthly reports off."
    else:
        reason = ""
    return {
        "month": month.key,
        "month_label": month.label,
        "subject": rendered["subject"],
        "html": rendered["html_message"],
        "text": rendered["message"],
        "audiences": list(report["sections"]),
        "active_audiences": report["active"],
        "has_activity": report["has_activity"],
        "would_send": not reason,
        "reason": reason,
        "excel": bool(render.build_excel(report["sections"], month)) if report["has_activity"] else False,
    }


# --------------------------------------------------------------------------- blackout


def blackout_until(now: Optional[datetime] = None) -> Optional[datetime]:
    """End of the current no-send window (Wednesday 20:55–21:15 IST or the slot-opening peak window), else None."""
    now = now or timezone.now()
    local = now.astimezone(IST)
    start, end = HARD_BLACKOUT
    if local.weekday() == HARD_BLACKOUT_WEEKDAY and start <= local.time() < end:
        return local.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    try:
        from iic_booking.equipment.peak_window import current_peak_window

        window = current_peak_window(now)
    except Exception:
        logger.exception("Monthly reports: peak window check failed")
        window = None
    return window.ends_at if window is not None else None


# --------------------------------------------------------------------------- delivery


def deliver(user, rendered: dict[str, str], *, month: ReportMonth, audiences: Iterable[str],
            attachment=None, test: bool = False, created_by=None):
    """Send one report email (one-click unsubscribe headers, test-account redirect) and log it."""
    from iic_booking.users.test_accounts import redirect_email_for_user

    from .models import CommunicationLog
    from .notification_preferences import MONTHLY_REPORTS, unsubscribe_headers

    subject = rendered["subject"]
    if test:
        subject = f"[TEST] {subject}"
    original = getattr(user, "email", "") or ""
    delivery_emails, subject = redirect_email_for_user(user, original_email=original, subject=subject)
    delivery_emails = [e for e in (delivery_emails or [original]) if e]
    meta = {
        "notification_category": MONTHLY_REPORTS,
        "monthly_report_month": month.key,
        "monthly_report_audiences": sorted(audiences),
        "test_send": test,
        "excel_attached": bool(attachment),
        "email_backend": getattr(settings, "EMAIL_BACKEND", ""),
        "email_provider": "aws_ses" if getattr(settings, "USE_AWS_SES_API", False) else "smtp",
    }
    if delivery_emails and delivery_emails[0].lower() != original.lower():
        meta["test_account_email_redirect"] = ", ".join(delivery_emails)
        meta["original_recipient_email"] = original
    template = resolve_template()
    log = CommunicationLog.objects.create(
        communication_type=CommunicationLog.CommunicationType.EMAIL,
        recipient=user,
        recipient_email=", ".join(delivery_emails)[:255] or original,
        template=template if template.pk else None,
        subject=subject,
        message=rendered["message"],
        status=CommunicationLog.CommunicationStatus.PENDING,
        metadata=meta,
        created_by=created_by,
    )
    if not delivery_emails:
        log.status = CommunicationLog.CommunicationStatus.FAILED
        log.error_message = "No email address"
        log.save(update_fields=["status", "error_message"])
        return log
    try:
        msg = EmailMultiAlternatives(
            subject=subject,
            body=rendered["message"],
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=delivery_emails,
            headers=unsubscribe_headers(user, MONTHLY_REPORTS) or None,
        )
        if rendered.get("html_message"):
            msg.attach_alternative(rendered["html_message"], "text/html")
        if attachment:
            msg.attach(*attachment)
        msg.send(fail_silently=False)
    except Exception as exc:
        log.status = CommunicationLog.CommunicationStatus.FAILED
        log.error_message = str(exc)[:2000]
        log.save(update_fields=["status", "error_message"])
        logger.warning("Monthly report email to user %s failed: %s", user.pk, type(exc).__name__)
        return log
    log.status = CommunicationLog.CommunicationStatus.SENT
    log.sent_at = timezone.now()
    log.save(update_fields=["status", "sent_at"])
    return log


def _claim(user, month: ReportMonth):
    """The send-log row for (user, month) when this run may send it now, else None."""
    from .models import MonthlyReportSendLog as Log

    try:
        with transaction.atomic():
            row, created = Log.objects.get_or_create(user=user, month=month.start)
    except IntegrityError:
        return None
    if created:
        return row
    if row.status == Log.Status.FAILED and row.attempts < MAX_ATTEMPTS:
        if Log.objects.filter(pk=row.pk, status=Log.Status.FAILED).update(status=Log.Status.SENDING):
            row.status = Log.Status.SENDING
            return row
    return None


def _finished_user_ids(month: ReportMonth) -> set[int]:
    from .models import MonthlyReportSendLog as Log

    rows = Log.objects.filter(month=month.start).values_list("user_id", "status", "attempts")
    return {uid for uid, status, attempts in rows if not (status == Log.Status.FAILED and attempts < MAX_ATTEMPTS)}


def run_monthly_reports(month: Optional[ReportMonth] = None, *, now: Optional[datetime] = None,
                        time_budget: float = BATCH_SECONDS, max_sends: int = BATCH_SENDS,
                        gap_seconds: float = SEND_GAP_SECONDS) -> dict[str, Any]:
    """One time-bounded batch of the scheduled run. ``more`` / ``retry_at`` tell the task when to continue."""
    from iic_booking.users.models import User

    from .models import MonthlyReportSendLog as Log
    from .models import MonthlyReportSettings
    from .notification_preferences import EMAIL, MONTHLY_REPORTS, allows, delivery_settings, in_quiet_hours, quiet_hours_end

    fixed_now = now
    now = now or timezone.now()
    month = month or ReportMonth.previous_to(now)
    row = MonthlyReportSettings.get_singleton()
    result: dict[str, Any] = {"month": month.key, "sent": 0, "failed": 0, "skipped_empty": 0, "skipped_opted_out": 0,
                              "quiet": 0, "more": False, "retry_at": None}
    if not row.enabled:
        return {**result, "status": "disabled"}
    audiences = enabled_audiences(row)
    if not audiences:
        return {**result, "status": "no_audiences"}
    if now >= month.end_at + timedelta(days=SEND_DAYS):
        return {**result, "status": "expired"}
    blocked = blackout_until(now)
    if blocked:
        return {**result, "status": "deferred", "retry_at": blocked}

    template = resolve_template(create=True)
    if template is None:
        return {**result, "status": "template_inactive"}
    users = recipients_by_user(audiences)
    pending = sorted(set(users) - _finished_user_ids(month))
    started = _time.monotonic()
    quiet_until: list[datetime] = []
    attempts = 0
    for index, uid in enumerate(pending):
        if attempts >= max_sends or _time.monotonic() - started > time_budget:
            result["more"] = True
            break
        blocked = blackout_until(fixed_now)
        if blocked:
            result.update(status="deferred", retry_at=blocked)
            return result
        user = User.objects.filter(pk=uid, is_active=True).first()
        if user is None:
            continue
        if not allows(user, EMAIL, MONTHLY_REPORTS):
            claimed = _claim(user, month)
            if claimed:
                claimed.status = Log.Status.SKIPPED_OPTED_OUT
                claimed.save(update_fields=["status", "updated_at"])
                result["skipped_opted_out"] += 1
            continue
        delivery = delivery_settings(user)
        if in_quiet_hours(delivery, fixed_now):
            quiet_until.append(quiet_hours_end(delivery, fixed_now))
            result["quiet"] += 1
            continue
        claimed = _claim(user, month)
        if claimed is None:
            continue
        try:
            report = build_report(user, month, audiences=users[uid], include_test=False)
        except Exception as exc:
            logger.exception("Monthly report for user %s could not be built", uid)
            claimed.status, claimed.error = Log.Status.FAILED, f"build: {type(exc).__name__}"
            claimed.attempts += 1
            claimed.save(update_fields=["status", "error", "attempts", "updated_at"])
            result["failed"] += 1
            continue
        if not report["has_activity"]:
            claimed.status = Log.Status.SKIPPED_EMPTY
            claimed.save(update_fields=["status", "updated_at"])
            result["skipped_empty"] += 1
            continue
        attempts += 1
        rendered = render_report(user, month, report, template=template)
        attachment = None
        if row.attach_excel and ({"oic", "hod"} & set(report["active"])):
            try:
                attachment = excel_attachment(report, month)
            except Exception:
                logger.exception("Monthly report Excel for user %s failed; sending without it", uid)
        log = deliver(user, rendered, month=month, audiences=report["active"], attachment=attachment)
        claimed.audiences = ",".join(report["active"])[:60]
        claimed.attempts += 1
        claimed.communication_log = log
        if log.status == log.CommunicationStatus.SENT:
            claimed.status, claimed.sent_at, claimed.error = Log.Status.SENT, timezone.now(), ""
            result["sent"] += 1
        else:
            claimed.status, claimed.error = Log.Status.FAILED, (log.error_message or "")[:2000]
            result["failed"] += 1
        claimed.save(update_fields=["audiences", "attempts", "communication_log", "status", "sent_at", "error",
                                    "updated_at"])
        if gap_seconds and index + 1 < len(pending):
            _time.sleep(gap_seconds)
    if not result["more"]:
        retryable = Log.objects.filter(month=month.start, status=Log.Status.FAILED, attempts__lt=MAX_ATTEMPTS).exists()
        if retryable:
            result["retry_at"] = timezone.now() + timedelta(minutes=15)
        if quiet_until:
            earliest = min(quiet_until)
            result["retry_at"] = min(result["retry_at"], earliest) if result["retry_at"] else earliest
    result["status"] = "ok"
    return result


# --------------------------------------------------------------------------- dry run and test send


def dry_run_counts(month: ReportMonth, audiences: Iterable[str] = AUDIENCES) -> dict[str, Any]:
    """Counts only: candidates, recipients with something to report, empty, opted out, already logged."""
    from iic_booking.users.models import User

    from .notification_preferences import EMAIL, MONTHLY_REPORTS, allows

    audiences = [a for a in AUDIENCES if a in audiences]
    users = recipients_by_user(audiences)
    finished = _finished_user_ids(month)
    per = {a: {"candidates": 0, "with_activity": 0, "empty": 0} for a in audiences}
    totals = {"candidates": len(users), "would_send": 0, "skipped_empty": 0, "opted_out": 0, "already_logged": 0,
              "with_excel": 0}
    for uid, user_audiences in sorted(users.items()):
        user = User.objects.filter(pk=uid).first()
        if user is None:
            continue
        report = build_report(user, month, audiences=user_audiences, include_test=False)
        for audience in user_audiences:
            per[audience]["candidates"] += 1
            section = report["sections"].get(audience)
            per[audience]["with_activity" if section and section.get("has_activity") else "empty"] += 1
        if not report["has_activity"]:
            totals["skipped_empty"] += 1
        elif not allows(user, EMAIL, MONTHLY_REPORTS):
            totals["opted_out"] += 1
        elif uid in finished:
            totals["already_logged"] += 1
        else:
            totals["would_send"] += 1
            if {"oic", "hod"} & set(report["active"]):
                totals["with_excel"] += 1
    return {"month": month.key, "audiences": per, **totals}


def test_recipients(user_ids: Optional[Iterable[int]] = None) -> list:
    """Test accounts only: the given ids, else the test faculty wallet owners and test Officers in Charge."""
    from iic_booking.equipment.models import EquipmentManager
    from iic_booking.users.models import User
    from iic_booking.users.models.user_type import UserType

    qs = User.objects.filter(is_active=True, is_test_account=True)
    if user_ids:
        return list(qs.filter(pk__in=list(user_ids)).order_by("pk"))
    managers = EquipmentManager.objects.values_list("manager_id", flat=True)
    return list(qs.filter(Q(user_type=UserType.FACULTY, wallet__isnull=False) | Q(pk__in=managers)).distinct().order_by("pk"))


def send_test_reports(month: ReportMonth, user_ids: Optional[Iterable[int]] = None, *, created_by=None) -> dict[str, Any]:
    """"[TEST]" reports to test accounts only (their own test data included; sent even when empty; not logged as the
    month's send, ignores the master switch). Refuses inside the no-send window."""
    from .models import MonthlyReportSettings

    blocked = blackout_until()
    if blocked:
        return {"status": "deferred", "retry_at": blocked.isoformat(), "sent": 0, "failed": 0, "users": []}
    row = MonthlyReportSettings.get_singleton()
    template = resolve_template(create=True) or resolve_template()
    out = {"status": "ok", "month": month.key, "sent": 0, "failed": 0, "users": []}
    for user in test_recipients(user_ids):
        report = build_report(user, month, include_test=True)
        rendered = render_report(user, month, report, template=template, show_empty=True)
        attachment = excel_attachment(report, month) if row.attach_excel and ({"oic", "hod"} & set(report["active"])) else None
        log = deliver(user, rendered, month=month, audiences=list(report["sections"]), attachment=attachment, test=True,
                      created_by=created_by)
        ok = log.status == log.CommunicationStatus.SENT
        out["sent" if ok else "failed"] += 1
        out["users"].append({"id": user.pk, "audiences": list(report["sections"]), "active": report["active"],
                             "sent": ok, "excel": bool(attachment)})
    return out
