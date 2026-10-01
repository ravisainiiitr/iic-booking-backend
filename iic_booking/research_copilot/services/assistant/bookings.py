"""The signed-in user's own bookings: upcoming list and status by booking ID (never anyone else's)."""

from __future__ import annotations

import re
from typing import Any

from django.utils import timezone

from iic_booking.research_copilot.services.assistant import cards as C

_ACTIVE_EXCLUDED = ("CANCELLED", "REFUNDED", "COMPLETED", "ABSENT")
_NUMERIC_REF = re.compile(r"(?:\bbooking\s*(?:id|no\.?|number|ref)?\s*[#:]?\s*|#\s*)(\d{1,9})\b", re.IGNORECASE)


def booking_ref(text: str) -> str | None:
    from iic_booking.research_copilot.services.booking_refs import find_virtual_ref

    virtual = find_virtual_ref(text or "")
    if virtual:
        return virtual
    m = _NUMERIC_REF.search(text or "")
    return m.group(1) if m else None


def _row(b) -> dict[str, Any]:
    from iic_booking.research_copilot.services.booking_refs import display_ref

    slots = sorted(b.daily_slots.all(), key=lambda s: s.start_datetime or timezone.now())
    start = slots[0].start_datetime if slots else None
    end = slots[-1].end_datetime if slots else None
    ls = timezone.localtime(start) if start else None
    le = timezone.localtime(end) if end else None
    return {
        "booking_id": int(b.pk),
        "reference": display_ref(b),
        "equipment": getattr(b.equipment, "name", ""),
        "equipment_id": int(b.equipment_id) if b.equipment_id else None,
        "status": b.status,
        "status_label": b.get_status_display() if hasattr(b, "get_status_display") else b.status,
        "when": f"{ls:%a %d %b %Y, %H:%M}" + (f"–{le:%H:%M}" if le else "") if ls else "",
        "start": ls.isoformat() if ls else None,
        "charge": float(b.total_charge) if getattr(b, "total_charge", None) is not None else None,
        "href": f"/my-bookings?booking={b.pk}",
    }


def upcoming_reply(user, *, limit: int = 8) -> dict[str, Any]:
    from iic_booking.equipment.models import Booking

    now = timezone.now()
    qs = (
        Booking.objects.filter(user=user, daily_slots__end_datetime__gte=now)
        .exclude(status__in=_ACTIVE_EXCLUDED)
        .select_related("equipment")
        .prefetch_related("daily_slots")
        .distinct()
    )
    rows = sorted((_row(b) for b in qs[:60]), key=lambda r: r["start"] or "")[:limit]
    if not rows:
        return C.reply(
            "You have no upcoming bookings. Tell me the equipment and a day (for example \"XRD next Monday\") and I'll find free slots.",
            actions=[C.link("My Bookings", "/my-bookings")],
            intent="upcoming",
            title_hint="Upcoming bookings",
        )
    lines = [f"You have {len(rows)} upcoming booking{'s' if len(rows) != 1 else ''}:"]
    for r in rows:
        lines.append(f"- **{r['equipment']}** — {r['when']} · {r['status_label']} ({r['reference']})")
    return C.reply(
        "\n".join(lines),
        cards=[{"type": "ba_bookings", "title": "Upcoming bookings", "items": rows}],
        actions=[C.link("My Bookings", "/my-bookings")],
        intent="upcoming",
        title_hint="Upcoming bookings",
    )


def status_reply(user, ref: str) -> dict[str, Any]:
    from django.db.models import Q

    from iic_booking.equipment.models import Booking

    filt = Q(virtual_booking_id__iexact=ref)
    if ref.isdigit():
        filt |= Q(booking_id=int(ref))
    b = (
        Booking.objects.filter(filt, user=user)
        .select_related("equipment")
        .prefetch_related("daily_slots")
        .first()
    )
    if b is None:
        return C.reply(
            f"I couldn't find booking **{ref}** among your bookings. Check the ID in My Bookings.",
            actions=[C.link("My Bookings", "/my-bookings")],
            intent="booking_status",
        )
    r = _row(b)
    content = f"Booking **{r['reference']}** for **{r['equipment']}** is **{r['status_label']}**."
    if r["when"]:
        content += f"\n\nSlot: {r['when']}."
    return C.reply(
        content,
        cards=[{"type": "ba_bookings", "title": "Booking status", "items": [r]}],
        actions=[C.link("Open booking", r["href"], primary=True)],
        intent="booking_status",
        title_hint=f"Booking {r['reference']}",
    )
