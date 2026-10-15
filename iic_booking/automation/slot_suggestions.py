"""
"Best slots for you" on the booking page.

Free slots come from the portal's own daily-slots view run as the user (booking windows, holidays, maintenance,
department reservations, home-department rules) via the Booking Assistant's ``find_bookable_slots``. Runs of the
required number of back-to-back slots are ranked by how soon they are and how well they match the start times the
user usually books. When nothing fits soon, similar instruments with earlier free runs are offered. Read-only.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime

WINDOW_DAYS = 14
MAX_SUGGESTIONS = 5
HISTORY_DAYS = 180
ALTERNATIVE_IF_LATER_THAN_DAYS = 3


def _local(value: Any) -> datetime | None:
    dt = parse_datetime(str(value or ""))
    if dt is None:
        return None
    return timezone.localtime(dt) if timezone.is_aware(dt) else dt


def usual_start_hours(user, *, today: date | None = None) -> list[int]:
    """Most common local start hours of the user's recent bookings (any equipment), most frequent first."""
    from django.db.models import Min

    from iic_booking.equipment.models import Booking

    today = today or timezone.localdate()
    since = today - timedelta(days=HISTORY_DAYS)
    starts = (
        Booking.objects.filter(user=user, status__in=["BOOKED", "COMPLETED", "PROCESSING"], created_at__date__gte=since)
        .annotate(first_start=Min("daily_slots__start_datetime"))
        .exclude(first_start__isnull=True)
        .values_list("first_start", flat=True)[:200]
    )
    counts = Counter(timezone.localtime(s).hour for s in starts if s)
    return [h for h, n in counts.most_common(3) if n >= 2]


def _runs(rows: list[dict[str, Any]], needed: int) -> list[list[dict[str, Any]]]:
    from iic_booking.research_copilot.services.intelligence.flows import contiguous_runs

    if needed <= 1:
        return [[r] for r in sorted(rows, key=lambda r: r.get("start") or "")]
    return contiguous_runs(rows, needed)


def _run_payload(run: list[dict[str, Any]]) -> dict[str, Any]:
    s, e = _local(run[0].get("start")), _local(run[-1].get("end"))
    label = f"{s:%a %d %b}, {s:%H:%M}" if s else str(run[0].get("date") or "")
    if e:
        label += f"\u2013{e:%H:%M}"
    return {
        "slot_ids": [int(r["slot_id"]) for r in run],
        "date": s.date().isoformat() if s else str(run[0].get("date") or ""),
        "start": s.isoformat() if s else None,
        "end": e.isoformat() if e else None,
        "label": label,
    }


def rank_runs(runs: list[list[dict[str, Any]]], usual_hours: list[int], *, today: date) -> list[dict[str, Any]]:
    scored: list[dict[str, Any]] = []
    for run in runs:
        p = _run_payload(run)
        s = _local(run[0].get("start"))
        if s is None:
            continue
        days_out = max(0, (s.date() - today).days)
        soon = 1.0 - min(days_out, WINDOW_DAYS) / WINDOW_DAYS
        pref = 0.0
        if usual_hours:
            dist = min(abs(s.hour - h) for h in usual_hours)
            pref = {0: 1.0, 1: 0.6, 2: 0.3}.get(dist, 0.0)
        p["score"] = round(0.65 * soon + 0.35 * pref, 3)
        p["_pref"] = pref
        p["_days"] = days_out
        scored.append(p)
    if not scored:
        return []
    picked: list[dict[str, Any]] = []
    earliest = min(scored, key=lambda p: p["start"] or "")
    earliest["reasons"] = ["Earliest free time"]
    picked.append(earliest)
    if usual_hours:
        matching = [p for p in scored if p["_pref"] >= 1.0 and p is not earliest]
        if matching:
            best = min(matching, key=lambda p: p["start"] or "")
            best["reasons"] = [f"Starts at your usual time ({best['start'][11:16]})"]
            picked.append(best)
        elif earliest["_pref"] >= 1.0:
            earliest["reasons"].append("Starts at your usual time")
    used_days = {p["date"] for p in picked}
    for p in sorted(scored, key=lambda p: (-p["score"], p["start"] or "")):
        if len(picked) >= MAX_SUGGESTIONS:
            break
        if p in picked or p["date"] in used_days:
            continue
        reasons = []
        if p["_pref"] >= 0.6:
            reasons.append("Close to your usual time")
        reasons.append("Next free day" if p["_days"] <= 2 else f"In {p['_days']} days")
        p["reasons"] = reasons
        picked.append(p)
        used_days.add(p["date"])
    picked.sort(key=lambda p: p["start"] or "")
    for p in picked:
        p.pop("_pref", None)
        p.pop("_days", None)
    return picked


def _first_run(user, eq, needed: int, start: date, end: date) -> dict[str, Any] | None:
    from iic_booking.research_copilot.services.v2.slot_availability import find_bookable_slots

    lookup = find_bookable_slots(user=user, equipment_id=int(eq.pk), start_date=start, end_date=end, limit=300)
    if not lookup.ok or not lookup.rows:
        return None
    runs = _runs(lookup.rows, needed)
    return _run_payload(runs[0]) if runs else None


def suggest(user, equipment, slot_count: int, *, today: date | None = None, with_alternatives: bool = True) -> dict[str, Any]:
    from iic_booking.research_copilot.services.v2.slot_availability import find_bookable_slots

    today = today or timezone.localdate()
    needed = max(1, min(int(slot_count or 1), 24))
    end = today + timedelta(days=WINDOW_DAYS - 1)
    lookup = find_bookable_slots(user=user, equipment_id=int(equipment.pk), start_date=today, end_date=end, limit=500)
    out: dict[str, Any] = {
        "equipment_id": int(equipment.pk),
        "slot_count": needed,
        "window_days": WINDOW_DAYS,
        "suggestions": [],
        "usual_hours": [],
        "alternatives": [],
        "message": "",
        "slot_window_max_date": getattr(lookup, "slot_window_max_date", None),
    }
    if not lookup.ok:
        out["message"] = lookup.message or "Free slots could not be loaded."
        return out
    usual = usual_start_hours(user, today=today)
    out["usual_hours"] = usual
    out["suggestions"] = rank_runs(_runs(lookup.rows, needed), usual, today=today)
    first_days = None
    if out["suggestions"]:
        first_days = (date.fromisoformat(out["suggestions"][0]["date"]) - today).days
    else:
        out["message"] = (
            f"No {needed} back-to-back free slots in the next {WINDOW_DAYS} days."
            if needed > 1 else f"No free slots in the next {WINDOW_DAYS} days."
        )
    if with_alternatives and (first_days is None or first_days > ALTERNATIVE_IF_LATER_THAN_DAYS):
        from iic_booking.research_copilot.services.assistant import matching

        for cand in matching.similar_equipment(user, equipment, limit=3):
            run = _first_run(user, cand.eq, needed, today, end)
            if run is None:
                continue
            if first_days is not None and (date.fromisoformat(run["date"]) - today).days >= first_days:
                continue
            out["alternatives"].append(
                {
                    "equipment_id": int(cand.eq.pk),
                    "name": cand.eq.name,
                    "code": cand.eq.code or "",
                    "first_label": run["label"],
                    "first_date": run["date"],
                    "href": f"/book-equipment?equipment_id={int(cand.eq.pk)}&date={run['date']}",
                    "reason": "Similar instrument with an earlier free time",
                }
            )
    return out
