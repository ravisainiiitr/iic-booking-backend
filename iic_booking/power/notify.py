"""Telling the Officers in Charge about their UPS.

Who: the Officers in Charge (and active temporary OICs) of every instrument mapped to the UPS. If no instrument or
OIC is mapped, the Department Administrators of the UPS's department, so a problem is never told to nobody.

When:
- a power alert opens (on battery, low battery, fault / bypass): at once, email + bell; "power back" on the bell when
  the UPS returns to mains;
- the hourly health check finds something that needs attention: a new finding, or a finding that got worse, at
  once; the same finding again after 12 hours (urgent) or 7 days (attention). "Keep an eye on" findings are only shown
  on the UPS health page.

Only for UPS units whose register map is verified and with "notify officers" on; ``UPS_HEALTH_NOTIFY`` (default on)
switches all of it off.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from html import escape

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

REALTIME_RULES = ("ups_on_battery", "ups_low_battery", "ups_fault")
REALTIME_CODES = {r.removeprefix("ups_") for r in REALTIME_RULES}
REPEAT_AFTER = {"urgent": timedelta(hours=12), "attention": timedelta(days=7)}
LEVEL_LABEL = {"ok": "Healthy", "watch": "Keep an eye on", "attention": "Needs attention", "urgent": "Urgent"}


def enabled_for(device) -> bool:
    return bool(getattr(settings, "UPS_HEALTH_NOTIFY", True) and device.enabled and device.map_verified
                and device.notify_officers)


def recipients(device) -> list:
    from iic_booking.communication.in_app import _unique_active, equipment_oic_users

    users = []
    for eq in device.equipment.all():
        users += equipment_oic_users(eq)
    if not users and device.department_id:
        from iic_booking.users.models import User
        from iic_booking.users.models.user_type import UserType

        users = list(User.objects.filter(user_type=UserType.DEPT_ADMIN, department_id=device.department_id,
                                         is_active=True))
    return _unique_active(users)


def link(device) -> str:
    base = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    return f"{base}/ups-health?ups={device.pk}"


def _send(device, users, *, template: str, context: dict, title: str, message: str, kind: str) -> list[int]:
    from iic_booking.communication.in_app import notify_in_app
    from iic_booking.communication.service import CommunicationService

    sent = []
    names = ", ".join(e.name for e in device.equipment.all())
    for user in users:
        ctx = {"recipient_name": user.get_display_name() if hasattr(user, "get_display_name") else "",
               "ups_name": device.name, "equipment_names": names or "-",
               "department_name": device.department.name if device.department_id else "-", "link": link(device),
               **context}
        try:
            if user.email:
                CommunicationService.send_email(recipient=user, template=template, template_context=ctx,
                                                metadata={"ups_id": device.pk, "kind": kind})
            sent.append(user.pk)
        except Exception:  # noqa: BLE001 - one failed email must not stop the others
            logger.exception("UPS notice email failed ups=%s user=%s", device.pk, user.pk)
    notify_in_app(users, title=title, message=message, link=f"/ups-health?ups={device.pk}",
                  notification_type="warning", event=f"ups_{kind}", extra={"ups_id": device.pk})
    return sent


def alert_opened(device, rule: str, title: str, message: str) -> None:
    """A power alert just opened (called from ``service.sync_alerts``)."""
    if rule not in REALTIME_RULES or not enabled_for(device):
        return
    users = recipients(device)
    if not users:
        return
    action = {
        "ups_on_battery": "Save running work and be ready to shut the instruments down safely before the backup "
                          "time runs out.",
        "ups_low_battery": "Shut sensitive instruments down safely now.",
        "ups_fault": "The instruments are not protected against a power cut. Call the UPS service engineer.",
    }[rule]
    _send(device, users, template="ups_power_alert_oic",
          context={"alert_title": title, "alert_message": message, "action": action,
                   "time": timezone.localtime().strftime("%d %b %Y %H:%M")},
          title=title, message=f"{message} {action}", kind="alert")


def alert_resolved(device, rule: str) -> None:
    if rule != "ups_on_battery" or not enabled_for(device):
        return
    from iic_booking.communication.in_app import notify_in_app

    notify_in_app(recipients(device), title=f"{device.name}: mains power back",
                  message="The UPS is running on mains again and recharging its battery.",
                  link=f"/ups-health?ups={device.pk}", notification_type="info", event="ups_power_back",
                  extra={"ups_id": device.pk})


def _last_told(device, now) -> dict[str, tuple]:
    """{finding code: (level, when)} told in the last 7 days."""
    from .models import UpsHealthAssessment

    out: dict[str, tuple] = {}
    rows = UpsHealthAssessment.objects.filter(device=device, notified_at__gte=now - timedelta(days=7)).order_by(
        "notified_at")
    for a in rows:
        levels = {f["code"]: f["level"] for f in a.findings}
        for code in a.notified_codes:
            out[code] = (levels.get(code, "attention"), a.notified_at)
    return out


def due_findings(device, findings: list[dict], now=None) -> list[dict]:
    """Findings of an assessment the officers should be told about now."""
    from .analysis import LEVEL_ORDER

    now = now or timezone.now()
    told = _last_told(device, now)
    due = []
    for f in findings:
        if f["level"] not in REPEAT_AFTER or f["code"] in REALTIME_CODES:
            continue
        before = told.get(f["code"])
        if before is None or LEVEL_ORDER[f["level"]] > LEVEL_ORDER.get(before[0], 0) \
                or now - before[1] >= REPEAT_AFTER[f["level"]]:
            due.append(f)
    return due


def health_notice(device, assessment_row, now=None) -> list[int]:
    """Send the health notice for a stored assessment if something is due; records who was told."""
    now = now or timezone.now()
    if not enabled_for(device):
        return []
    due = due_findings(device, assessment_row.findings, now)
    if not due:
        return []
    users = recipients(device)
    if not users:
        return []
    level = "urgent" if any(f["level"] == "urgent" for f in due) else "attention"
    items_html = "".join(
        f"<li><b>{escape(f['title'])}</b> ({escape(LEVEL_LABEL[f['level']])}): {escape(f['detail'])} "
        f"<i>{escape(f['action'])}</i></li>" for f in due)
    items_text = "\n".join(f"- {f['title']} ({LEVEL_LABEL[f['level']]}): {f['detail']} {f['action']}" for f in due)
    note = ("Summary written by the portal's AI assistant from the UPS readings."
            if assessment_row.summary_source == "ai" else "Summary composed from the UPS readings.")
    sent = _send(device, users, template="ups_health_attention_oic",
                 context={"level_label": LEVEL_LABEL[level], "summary": assessment_row.summary,
                          "findings_html": f"<ul>{items_html}</ul>", "findings_text": items_text,
                          "summary_note": note, "risk_score": str(assessment_row.risk_score)},
                 title=f"{device.name}: {LEVEL_LABEL[level].lower()}",
                 message=assessment_row.summary[:500], kind="health")
    assessment_row.notified_codes = [f["code"] for f in due]
    assessment_row.notified_at = now
    assessment_row.notified_to = sorted({u.pk for u in users})
    assessment_row.save(update_fields=["notified_codes", "notified_at", "notified_to"])
    return sent
