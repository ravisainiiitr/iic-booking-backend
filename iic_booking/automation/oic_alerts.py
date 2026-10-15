"""
Weekly anomaly alerts to Officers in Charge (off by default).

For the last full week (Monday-Sunday) and each equipment: booked hours against the average of the four weeks
before, cancellations against their weekly average, and disruption events (maintenance, analysis not possible,
operator unavailable, awaiting disruption choice). Each OIC gets one email listing only their flagged equipment,
at most once per week, through the normal email service (notification category "Lab operations", so quiet
hours, the daily digest and unsubscribe apply). A dry run computes the same list and sends nothing.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from html import escape
from typing import Any

from django.db.models import Count, Min, Q, Sum
from django.utils import timezone

logger = logging.getLogger(__name__)

TEMPLATE_CODE = "automation_oic_weekly_alert_email"
BASELINE_WEEKS = 4
MIN_BASELINE_HOURS = 4.0
ACTIVE_STATUSES = ("BOOKED", "COMPLETED", "PROCESSING", "HOLD")
CANCEL_STATUSES = ("CANCELLED", "REFUNDED")
DISRUPTION_STATUSES = ("UNDER_MAINTENANCE", "OTHER_DISRUPTION", "ABSENT", "DISRUPTION_PENDING")


@dataclass
class EquipmentAlert:
    equipment_id: int
    name: str
    code: str
    flags: list[dict[str, str]] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)


def last_full_week(today: date | None = None) -> date:
    today = today or timezone.localdate()
    return today - timedelta(days=today.weekday() + 7)


def week_key(monday: date) -> str:
    y, w, _ = monday.isocalendar()
    return f"{y}-W{w:02d}"


def _hours_by_equipment(start: date, end: date) -> dict[int, float]:
    from iic_booking.equipment.models import Booking

    rows = (
        Booking.objects.filter(status__in=ACTIVE_STATUSES)
        .annotate(first_day=Min("daily_slots__date"))
        .filter(first_day__gte=start, first_day__lte=end)
        .values("equipment_id")
        .annotate(minutes=Sum("total_time_minutes"))
    )
    return {int(r["equipment_id"]): float(r["minutes"] or 0) / 60.0 for r in rows}


def _events_by_equipment(start: date, end: date, statuses: tuple[str, ...]) -> dict[int, int]:
    from iic_booking.equipment.models import BookingEvent

    rows = (
        BookingEvent.objects.filter(
            created_at__date__gte=start, created_at__date__lte=end, new_status__in=statuses
        )
        .values("booking__equipment_id")
        .annotate(n=Count("booking_id", distinct=True))
    )
    return {int(r["booking__equipment_id"]): int(r["n"]) for r in rows}


def compute_alerts(monday: date | None = None, *, cfg=None) -> tuple[date, list[EquipmentAlert]]:
    from iic_booking.equipment.models import Equipment

    from .audit import settings_row

    cfg = cfg or settings_row()
    monday = monday or last_full_week()
    sunday = monday + timedelta(days=6)
    base_start = monday - timedelta(days=7 * BASELINE_WEEKS)
    base_end = monday - timedelta(days=1)

    hours = _hours_by_equipment(monday, sunday)
    base_hours = _hours_by_equipment(base_start, base_end)
    cancels = _events_by_equipment(monday, sunday, CANCEL_STATUSES)
    base_cancels = _events_by_equipment(base_start, base_end, CANCEL_STATUSES)
    disruptions = _events_by_equipment(monday, sunday, DISRUPTION_STATUSES)

    drop_pct = max(1, min(int(cfg.oic_alert_utilisation_drop_pct or 40), 100))
    min_cancel = max(1, int(cfg.oic_alert_min_cancellations or 3))
    min_disr = max(1, int(cfg.oic_alert_min_disruptions or 2))

    ids = set(hours) | set(base_hours) | set(cancels) | set(disruptions)
    eqs = {e.pk: e for e in Equipment.objects.filter(pk__in=ids, status="ACTIVE").only("pk", "name", "code")}
    alerts: list[EquipmentAlert] = []
    for pk, eq in eqs.items():
        a = EquipmentAlert(int(pk), eq.name or "", eq.code or "")
        h = round(hours.get(pk, 0.0), 1)
        base = round(base_hours.get(pk, 0.0) / BASELINE_WEEKS, 1)
        c, base_c = cancels.get(pk, 0), base_cancels.get(pk, 0) / BASELINE_WEEKS
        d = disruptions.get(pk, 0)
        a.metrics = {"booked_hours": h, "baseline_hours": base, "cancellations": c,
                     "baseline_cancellations": round(base_c, 1), "disruptions": d}
        if base >= MIN_BASELINE_HOURS and h <= base * (1 - drop_pct / 100):
            pct = round(100 * (1 - h / base)) if base else 100
            a.flags.append({"kind": "utilisation_drop",
                            "text": f"Booked hours fell {pct}% ({h:g} h against a {base:g} h weekly average)."})
        if c >= min_cancel and c >= 2 * max(base_c, 0.5):
            a.flags.append({"kind": "cancellation_spike",
                            "text": f"{c} bookings cancelled or refunded (weekly average {base_c:.1f})."})
        if d >= min_disr:
            a.flags.append({"kind": "repeated_disruptions",
                            "text": f"{d} bookings disrupted (maintenance, analysis not possible or operator unavailable)."})
        if a.flags:
            alerts.append(a)
    alerts.sort(key=lambda a: (a.name, a.equipment_id))
    return monday, alerts


def recipients_for(alerts: list[EquipmentAlert]) -> dict[Any, list[EquipmentAlert]]:
    from iic_booking.equipment.models import EquipmentManager

    by_eq = defaultdict(list)
    for m in EquipmentManager.objects.filter(
        equipment_id__in=[a.equipment_id for a in alerts], manager__is_active=True
    ).select_related("manager"):
        if m.manager and m.manager.email:
            by_eq[m.equipment_id].append(m.manager)
    out: dict[Any, list[EquipmentAlert]] = {}
    users = {}
    for a in alerts:
        for u in by_eq.get(a.equipment_id, []):
            users[u.pk] = u
            out.setdefault(u.pk, []).append(a)
    return {users[pk]: rows for pk, rows in out.items()}


def render(user, monday: date, rows: list[EquipmentAlert]) -> dict[str, str]:
    from django.conf import settings

    base = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    label = f"{monday:%d %b} \u2013 {monday + timedelta(days=6):%d %b %Y}"
    html_rows, text_rows = [], []
    for a in rows:
        items = "".join(f"<li>{escape(f['text'])}</li>" for f in a.flags)
        html_rows.append(f"<p><strong>{escape(a.name)}</strong> ({escape(a.code)})</p><ul>{items}</ul>")
        text_rows.append(f"{a.name} ({a.code})\n" + "\n".join(f"  - {f['text']}" for f in a.flags))
    return {
        "recipient_name": user.get_display_name() if hasattr(user, "get_display_name") else "",
        "week_label": label,
        "report_html": "".join(html_rows),
        "report_text": "\n\n".join(text_rows),
        "link": f"{base}/reports" if base else "/reports",
    }


def run(*, dry_run: bool = True, monday: date | None = None, force: bool = False) -> dict[str, Any]:
    """Compute (and unless ``dry_run``, send) this week's alerts. Returns counts and per-equipment flags."""
    from iic_booking.communication.service import CommunicationService

    from .audit import log_action, settings_row
    from .models import AutomationActionLog

    cfg = settings_row()
    monday, alerts = compute_alerts(monday, cfg=cfg)
    by_user = recipients_for(alerts)
    key = week_key(monday)
    summary: dict[str, Any] = {
        "week": key,
        "dry_run": dry_run,
        "enabled": bool(cfg.oic_alerts_enabled),
        "equipment_flagged": len(alerts),
        "flags": {k: sum(1 for a in alerts for f in a.flags if f["kind"] == k)
                  for k in ("utilisation_drop", "cancellation_spike", "repeated_disruptions")},
        "recipients": len(by_user),
        "sent": 0,
        "skipped": 0,
        "equipment": [{"equipment_id": a.equipment_id, "name": a.name, "code": a.code, "flags": a.flags,
                       "metrics": a.metrics} for a in alerts],
    }
    if dry_run:
        return summary
    if not cfg.oic_alerts_enabled and not force:
        summary["skipped"] = len(by_user)
        return summary
    for user, rows in by_user.items():
        target = f"{key}:{user.pk}"
        if AutomationActionLog.objects.filter(kind="oic_alert", action="sent", target_type="oic_week", target_id=target).exists():
            summary["skipped"] += 1
            continue
        try:
            CommunicationService.send_email(
                recipient=user,
                template=TEMPLATE_CODE,
                template_context=render(user, monday, rows),
                metadata={"notification_category": "staff", "staff_recipient": True, "automation": "oic_alert",
                          "week": key},
            )
            summary["sent"] += 1
            log_action("oic_alert", "sent", target_type="oic_week", target_id=target, recipient=user,
                       summary=f"{len(rows)} equipment flagged", detail={"equipment_ids": [a.equipment_id for a in rows]})
        except Exception as exc:  # noqa: BLE001
            logger.exception("oic weekly alert failed user_id=%s", user.pk)
            log_action("oic_alert", "failed", target_type="oic_week", target_id=target, recipient=user,
                       summary=type(exc).__name__)
    return summary
