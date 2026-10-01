"""
Availability answers: real bookable slots for a window, a charge estimate, and alternatives.

Slots come from `find_bookable_slots`, which calls the portal's own `equipment_daily_slots` view as the
user (internal/external booking windows, holidays, weekends, maintenance, multi-mode schedules,
department reservations) and then the booking endpoint's home-department filter. Nothing here books.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from iic_booking.research_copilot.services.assistant import cards as C
from iic_booking.research_copilot.services.assistant import matching
from iic_booking.research_copilot.services.assistant.dates import When, day_window, describe

MAX_DAYS_SHOWN = 4
MAX_CHIPS_PER_DAY = 8
MAX_CHIPS_TOTAL = 20


def _local(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = parse_datetime(str(value))
    if dt is None:
        return None
    return timezone.localtime(dt) if timezone.is_aware(dt) else dt


def slots_needed(eq, total_time_minutes: Any) -> int:
    """Booking page rule: ceil((analysis time - tolerance) / slot duration) on whole minutes, minimum one slot."""
    from iic_booking.research_copilot.services.v2.mutations.booking import portal_slots_needed

    return max(1, portal_slots_needed(
        total_time_minutes,
        getattr(eq, "slot_duration_minutes", 0) or 0,
        getattr(eq, "slot_tolerance_minutes", 0) or 0,
    ))


def estimate_one_sample(user, eq) -> dict[str, Any]:
    from iic_booking.research_copilot.services.intelligence import flows

    data = None
    try:
        data = flows.estimate(user, eq, 1)
    except Exception:  # noqa: BLE001
        data = None
    if not data or data.get("estimate") is None:
        return {"charge": None, "gst_percent": None, "gst_amount": None, "total": None, "samples": 1, "slots_needed": 1}
    breakdown = flows.charge_breakdown(user, data.get("estimate"))
    return {
        **breakdown,
        "samples": 1,
        "total_time_minutes": data.get("total_time_minutes"),
        "slots_needed": slots_needed(eq, data.get("total_time_minutes")),
    }


def _lookup(user, eq, start: date, end: date):
    from iic_booking.research_copilot.services.v2.slot_availability import find_bookable_slots

    return find_bookable_slots(user=user, equipment_id=int(eq.pk), start_date=start, end_date=end, limit=500)


def _runs(rows: list[dict[str, Any]], needed: int) -> list[list[dict[str, Any]]]:
    from iic_booking.research_copilot.services.intelligence import flows

    if needed <= 1:
        return [[r] for r in sorted(rows, key=lambda r: r.get("start") or "")]
    return flows.contiguous_runs(rows, needed)


def _filter_rows(rows: list[dict[str, Any]], when: When) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        start = _local(r.get("start"))
        if start is None:
            continue
        if when.start_date <= start.date() <= when.end_date and when.accepts(start.time()):
            out.append(r)
    return out


def chip(run: list[dict[str, Any]]) -> dict[str, Any]:
    s, e = _local(run[0].get("start")), _local(run[-1].get("end"))
    label = f"{s:%H:%M}" if s else str(run[0].get("date") or "")
    if e:
        label += f"–{e:%H:%M}"
    return {
        "slot_ids": [int(r["slot_id"]) for r in run],
        "label": label,
        "date": s.date().isoformat() if s else run[0].get("date"),
        "start": s.isoformat() if s else None,
        "end": e.isoformat() if e else None,
    }


def _group_days(runs: list[list[dict[str, Any]]], when: When) -> list[dict[str, Any]]:
    by_day: dict[str, list[dict[str, Any]]] = {}
    seen: set[tuple[str, str]] = set()
    for run in runs:
        c = chip(run)
        if (str(c["date"]), c["label"]) in seen:
            continue
        seen.add((str(c["date"]), c["label"]))
        by_day.setdefault(str(c["date"]), []).append(c)
    days = []
    total = 0
    today = timezone.localdate()
    for d in sorted(by_day):
        chips = by_day[d]
        if when.at:
            target = when.at.hour * 60 + when.at.minute

            def _dist(c, target=target):
                s = _local(c["start"])
                return abs((s.hour * 60 + s.minute) - target) if s else 10_000

            best = min(chips, key=_dist)
            best["best"] = True
            chips = sorted(chips, key=_dist)[:MAX_CHIPS_PER_DAY]
            chips.sort(key=lambda c: c["start"] or "")
        shown = chips[:MAX_CHIPS_PER_DAY]
        room = MAX_CHIPS_TOTAL - total
        if room <= 0 or len(days) >= MAX_DAYS_SHOWN:
            break
        shown = shown[:room]
        total += len(shown)
        try:
            label = describe(day_window(date.fromisoformat(d), today=today), today=today)
        except ValueError:
            label = d
        days.append({"date": d, "label": label, "slots": shown, "more": max(0, len(by_day[d]) - len(shown))})
    return days


def _nearest_days(user, eq, when: When, needed: int) -> list[dict[str, Any]]:
    today = timezone.localdate()
    lookup = _lookup(user, eq, today, today + timedelta(days=13))
    if not lookup.ok or not lookup.rows:
        return []
    outside = [
        r for r in lookup.rows
        if (_local(r.get("start")) is not None)
        and not (when.start_date <= _local(r.get("start")).date() <= when.end_date)
    ]

    def per_day(rows):
        counts: dict[str, int] = {}
        for run in _runs(rows, needed):
            d = str(chip(run)["date"])
            counts[d] = counts.get(d, 0) + 1
        return counts

    timed = [r for r in outside if when.accepts(_local(r["start"]).time())]
    counts = per_day(timed) or per_day(outside)
    keep_time = bool(per_day(timed))
    out = []
    for d in sorted(counts)[:3]:
        day = date.fromisoformat(d)
        window = day_window(day, today=today)
        if keep_time:
            window.after, window.before, window.at, window.period = when.after, when.before, when.at, when.period
            window.explicit_time = when.explicit_time
            window.label = describe(window, today=today)
        out.append({"date": d, "label": window.label, "count": counts[d], "when": window.to_payload()})
    return out


def _similar_with_slots(user, eq, when: When) -> list[dict[str, Any]]:
    out = []
    for cand in matching.similar_equipment(user, eq, limit=3):
        lookup = _lookup(user, cand.eq, when.start_date, when.end_date)
        rows = _filter_rows(lookup.rows, when) if lookup.ok else []
        first = chip([rows[0]]) if rows else None
        out.append(
            {
                **matching.option_row(cand),
                "free_slots": len(rows),
                "first_slot": first["label"] if first else None,
                "first_date": first["date"] if first else None,
            }
        )
    out.sort(key=lambda r: (0 if r["free_slots"] else 1, r["name"]))
    return out


def booking_href(eq_id: int, d: str | date | None = None) -> str:
    href = f"/book-equipment?equipment_id={int(eq_id)}"
    if d:
        href += f"&date={d if isinstance(d, str) else d.isoformat()}"
    return href


def availability_reply(user, eq, when: When, *, booking_intent: bool = False) -> dict[str, Any]:
    from iic_booking.equipment.models import EquipmentStatus

    today = timezone.localdate()
    name = eq.name
    detail_action = C.link("Equipment details", f"/equipment/{eq.pk}")
    if when.past:
        return C.reply(
            f"That date has already passed. Here are the next free **{name}** slots instead.",
            actions=[C.assistant_action("Next 7 days", "ba_availability", {"equipment_id": int(eq.pk), "when": _next7(today)})],
            intent="availability",
        )
    if (eq.status or "").strip() != EquipmentStatus.ACTIVE:
        similar = _similar_with_slots(user, eq, when)
        content = f"**{name}** is currently **{eq.get_status_display()}** and cannot be booked."
        if similar:
            content += " These similar instruments may help:"
        return C.reply(
            content,
            cards=[C.equipment_options_card(similar, title="Similar equipment", intent="availability", when=when)] if similar else [],
            actions=[detail_action],
            intent="availability",
            title_hint=f"{name} availability",
        )

    estimate = estimate_one_sample(user, eq)
    needed = int(estimate.get("slots_needed") or 1)
    lookup = _lookup(user, eq, when.start_date, when.end_date)
    if not lookup.ok:
        return C.reply(
            lookup.message or "Slot availability could not be loaded right now. Open the booking page for the live calendar.",
            actions=[C.link("Open booking page", booking_href(eq.pk, when.start_date)), detail_action],
            intent="availability",
        )
    rows = _filter_rows(lookup.rows, when)
    runs = _runs(rows, needed)
    days = _group_days(runs, when)

    notes: list[str] = []
    locked, lock_message = _booking_lock(user)
    if locked:
        notes.append(lock_message or "Booking is temporarily locked for your account.")
    max_date = lookup.slot_window_max_date
    if max_date and str(when.start_date) > str(max_date):
        notes.append(f"Your account can currently book up to **{_fmt_date(max_date)}**; later dates open as the booking window moves.")
    if needed > 1:
        notes.append(f"One sample needs about {needed} back-to-back slots, so times below are {needed}-slot blocks.")

    nearest: list[dict[str, Any]] = []
    similar: list[dict[str, Any]] = []
    waitlist = int(getattr(eq, "waitlist_queue_depth", 0) or 0) > 0
    if not days:
        nearest = _nearest_days(user, eq, when, needed)
        similar = _similar_with_slots(user, eq, when)

    count = sum(len(d["slots"]) + d["more"] for d in days)
    price = _price_text(estimate)
    if days:
        lead = f"**{name}** has {count} free {'time' if count == 1 else 'times'} for {when.label}."
        if when.at and not any(c.get("best") and _local(c["start"]) and _local(c["start"]).time() == when.at for d in days for c in d["slots"]):
            lead += f" Nothing starts exactly at {when.at:%H:%M}; the closest times are marked."
        lead += " " + ("Pick a time to book it." if not locked else "")
    else:
        lead = f"No free **{name}** slots for {when.label}."
        if nearest:
            lead += " Nearest free days: " + ", ".join(f"{n['label']} ({n['count']})" for n in nearest) + "."
        elif not similar:
            lead += " There are no free slots in the next 14 days."
        if waitlist:
            lead += " You can also join the waitlist from the booking page."
    content = lead.strip()
    if price:
        content += f"\n\n{price}"
    if notes:
        content += "\n\n" + "\n".join(f"- {n}" for n in notes)

    card = {
        "type": "ba_slots",
        "equipment_id": int(eq.pk),
        "equipment_name": name,
        "window_label": when.label,
        "when": when.to_payload(),
        "days": days,
        "estimate": estimate,
        "slots_needed": needed,
        "can_book": not locked,
        "booking_intent": booking_intent,
        "nearest_days": nearest,
        "similar": similar,
        "waitlist_href": booking_href(eq.pk, (nearest[0]["date"] if nearest else when.start_date)) if waitlist and not days else None,
        "booking_href": booking_href(eq.pk, days[0]["date"] if days else when.start_date),
    }
    actions = [C.link("Open booking page", card["booking_href"]), detail_action]
    if not when.explicit_date or when.single_day:
        actions.insert(0, C.assistant_action(
            "Next 7 days" if when.single_day else "Next week",
            "ba_availability",
            {"equipment_id": int(eq.pk), "when": _next7(today) if when.single_day else _next_week(today)},
        ))
    return C.reply(
        content,
        cards=[card],
        actions=actions,
        intent="availability",
        title_hint=f"{name} availability",
        extra={"equipment_id": int(eq.pk)},
    )


def _booking_lock(user) -> tuple[bool, str]:
    try:
        from iic_booking.users.legacy_ledger.booking_lock import booking_is_locked

        locked, message = booking_is_locked(user)
        return bool(locked), str(message or "")
    except Exception:  # noqa: BLE001
        return False, ""


def _fmt_date(value: Any) -> str:
    try:
        return f"{date.fromisoformat(str(value)[:10]):%a %d %b %Y}"
    except ValueError:
        return str(value)


def _price_text(estimate: dict[str, Any]) -> str:
    if estimate.get("charge") is None:
        return ""
    text = f"Estimated charge for 1 sample: **₹{estimate['charge']:,.2f}**"
    if estimate.get("gst_amount"):
        text += f" + GST {estimate['gst_percent']:g}% = **₹{estimate['total']:,.2f}**"
    return text + " (default inputs; the booking page recalculates for your actual inputs)."


def _next7(today: date) -> dict[str, Any]:
    return {"start": today.isoformat(), "end": (today + timedelta(days=6)).isoformat()}


def _next_week(today: date) -> dict[str, Any]:
    start = today + timedelta(days=7 - today.weekday())
    return {"start": start.isoformat(), "end": (start + timedelta(days=6)).isoformat()}
