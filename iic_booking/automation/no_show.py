"""
Likely no-show prompts to lab operators (off by default).

Once a day: bookings still Booked whose slots were today and ended at least the grace period ago, with no sample
recorded beyond "sample sent", on equipment that receives samples (walk-in and 3D printing / laser cutting are
excluded, as for Booking Not Utilized). Each Lab operator of the equipment (or the OIC when it has none) gets one
email listing them, so they can record the sample or mark the booking Not Utilized. The prompt never changes a
booking; the existing Not Utilized rule still runs on its own window. Each booking is prompted once.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import date, timedelta
from html import escape
from typing import Any

from django.db.models import Count, Exists, F, Max, Min, OuterRef, Q
from django.utils import timezone

logger = logging.getLogger(__name__)

TEMPLATE_CODE = "automation_no_show_prompt_email"


def candidates(*, day: date | None = None, now=None, grace_minutes: int = 60):
    from iic_booking.equipment.models import (
        FABRICATION_PROFILE_TYPES,
        Booking,
        BookingSampleTrace,
        BookingStatus,
        SampleTraceStatus,
        SlotStatus,
    )
    from iic_booking.equipment.sample_lifecycle_policy import walk_in_sample_equipment_q

    now = now or timezone.now()
    day = day or timezone.localdate(now)
    cutoff = now - timedelta(minutes=max(0, int(grace_minutes)))
    bad_trace = BookingSampleTrace.objects.filter(booking_id=OuterRef("pk")).exclude(status=SampleTraceStatus.SAMPLE_SENT)
    return (
        Booking.objects.filter(status=BookingStatus.BOOKED)
        .annotate(
            first_day=Min("daily_slots__date"),
            last_end=Max("daily_slots__end_datetime", filter=Q(daily_slots__status=SlotStatus.BOOKED)),
            n_slots=Count("daily_slots", distinct=True),
            n_booked=Count("daily_slots", filter=Q(daily_slots__status=SlotStatus.BOOKED), distinct=True),
            has_progress=Exists(bad_trace),
        )
        .filter(first_day=day, last_end__isnull=False, last_end__lte=cutoff, n_slots__gt=0,
                n_booked=F("n_slots"), has_progress=False)
        .exclude(walk_in_sample_equipment_q("equipment__"))
        .exclude(equipment__profile_type__in=FABRICATION_PROFILE_TYPES)
        .select_related("equipment")
        .order_by("equipment_id", "last_end")
    )


def recipients_for(bookings) -> dict[Any, list]:
    from iic_booking.equipment.models import EquipmentManager, EquipmentOperator

    eq_ids = {b.equipment_id for b in bookings}
    people: dict[int, list] = defaultdict(list)
    for o in EquipmentOperator.objects.filter(equipment_id__in=eq_ids, operator__is_active=True).select_related("operator"):
        if o.operator and o.operator.email and all(u.pk != o.operator.pk for u in people[o.equipment_id]):
            people[o.equipment_id].append(o.operator)
    missing = eq_ids - set(people)
    if missing:
        for m in EquipmentManager.objects.filter(equipment_id__in=missing, manager__is_active=True).select_related("manager"):
            if m.manager and m.manager.email and all(u.pk != m.manager.pk for u in people[m.equipment_id]):
                people[m.equipment_id].append(m.manager)
    users: dict[int, Any] = {}
    out: dict[int, list] = defaultdict(list)
    for b in bookings:
        for u in people.get(b.equipment_id, []):
            users[u.pk] = u
            out[u.pk].append(b)
    return {users[pk]: rows for pk, rows in out.items()}


def _row(b) -> dict[str, Any]:
    from iic_booking.communication.utils import booking_display_id_for_email

    try:
        ref = booking_display_id_for_email(b)
    except Exception:  # noqa: BLE001
        ref = f"#{b.booking_id}"
    end = timezone.localtime(b.last_end).strftime("%H:%M") if getattr(b, "last_end", None) else ""
    return {"booking_id": int(b.booking_id), "ref": str(ref), "equipment": b.equipment.name, "code": b.equipment.code or "",
            "ended": end}


def render(user, rows: list[dict[str, Any]]) -> dict[str, str]:
    from django.conf import settings

    base = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    html = "".join(
        f"<li>{escape(r['ref'])} \u2013 {escape(r['equipment'])} (slot ended {escape(r['ended'])})</li>" for r in rows
    )
    text = "\n".join(f"- {r['ref']} - {r['equipment']} (slot ended {r['ended']})" for r in rows)
    return {
        "recipient_name": user.get_display_name() if hasattr(user, "get_display_name") else "",
        "report_html": f"<ul>{html}</ul>",
        "report_text": text,
        "count": str(len(rows)),
        "link": f"{base}/booking-management" if base else "/booking-management",
    }


def run(*, dry_run: bool = True, day: date | None = None, now=None, force: bool = False) -> dict[str, Any]:
    from iic_booking.communication.service import CommunicationService

    from .audit import log_action, settings_row
    from .models import AutomationActionLog

    cfg = settings_row()
    bookings = list(candidates(day=day, now=now, grace_minutes=cfg.no_show_grace_minutes))
    already = set(
        AutomationActionLog.objects.filter(
            kind="no_show_prompt", action="sent", target_type="booking",
            target_id__in=[str(b.booking_id) for b in bookings],
        ).values_list("target_id", flat=True)
    )
    fresh = [b for b in bookings if str(b.booking_id) not in already]
    by_user = recipients_for(fresh)
    summary: dict[str, Any] = {
        "dry_run": dry_run,
        "enabled": bool(cfg.no_show_prompts_enabled),
        "bookings": len(fresh),
        "already_prompted": len(bookings) - len(fresh),
        "recipients": len(by_user),
        "emails_sent": 0,
        "booking_ids": [int(b.booking_id) for b in fresh][:200],
    }
    if dry_run or (not cfg.no_show_prompts_enabled and not force):
        return summary
    for user, rows in by_user.items():
        payload = [_row(b) for b in rows]
        try:
            CommunicationService.send_email(
                recipient=user,
                template=TEMPLATE_CODE,
                template_context=render(user, payload),
                metadata={"notification_category": "staff", "staff_recipient": True, "automation": "no_show_prompt"},
            )
            summary["emails_sent"] += 1
        except Exception as exc:  # noqa: BLE001
            logger.exception("no-show prompt failed user_id=%s", user.pk)
            log_action("no_show_prompt", "failed", target_type="user", target_id=user.pk, recipient=user,
                       summary=type(exc).__name__)
            continue
        for b in rows:
            log_action("no_show_prompt", "sent", target_type="booking", target_id=b.booking_id, recipient=user,
                       summary=f"{b.equipment.code or b.equipment.name}")
    return summary
