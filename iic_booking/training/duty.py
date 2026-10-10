"""
Operator duty allocation.

The OIC allocates one slot, a set of slots from the equipment calendar, a date range or a recurring weekly pattern
to an operator on the equipment's roster. Shifts overlay the calendar: they never change slot status or bookings.

DutyAllocation: PENDING (awaiting the operator, when confirmation is required) → CONFIRMED | DECLINED | EXPIRED
(no answer by the deadline: shifts released, OICs told who is next in rotation); CONFIRMED → COMPLETED once every
shift is closed; PENDING/CONFIRMED → CANCELLED by the OIC. The operator answers in the portal or through a signed,
expiring link in the email that opens a confirm/decline page (no login, no state change on a plain GET).

DutyShift: SCHEDULED → CHECKED_IN → COMPLETED (minutes from check-in/out), or COMPLETED from completed bookings in
the window, or COMPLETED/MISSED as entered by the OIC; RELEASED/CANCELLED when the allocation is not taken up.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timedelta

from django.core import signing
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_date, parse_datetime, parse_time

from iic_booking.users.display import get_user_display_name

from . import access, notify, operator_policy, roster
from .audit import audit
from .duty_fairness import rank
from .errors import TrainingError
from .models import (
    AwardStatus,
    CertificationAward,
    DutyAllocation,
    DutyShift,
    DutyStatus,
    HoursSource,
    OperatorRosterEntry,
    RegistrationStatus,
    ShiftStatus,
    TrainingSession,
)

logger = logging.getLogger(__name__)

TOKEN_SALT = "training.duty.respond"
TOKEN_MAX_AGE = 60 * 60 * 24 * 30
MAX_SHIFTS = 200
MAX_RANGE_DAYS = 120
CHECKIN_EARLY = timedelta(minutes=30)
AUTO_CLOSE_AFTER = timedelta(hours=1)
OPEN_ALLOCATION = (DutyStatus.PENDING, DutyStatus.CONFIRMED)
LIVE_SHIFT = (ShiftStatus.SCHEDULED, ShiftStatus.CHECKED_IN)
COMMITTED_ALLOCATION = (DutyStatus.PENDING, DutyStatus.CONFIRMED, DutyStatus.COMPLETED)
UNAVAILABLE_SLOT = ("UNDER_MAINTENANCE", "SCHEDULED_MAINT", "NOT_AVAILABLE")
ACTIVE_BOOKING = ("PENDING", "PENDING_PAYMENT", "BOOKED", "HOLD")


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------
def week_key(dt: datetime) -> str:
    y, w, _ = timezone.localtime(dt).isocalendar()
    return f"{y}-W{w:02d}"


def term_bounds(day: date) -> tuple[datetime, datetime, str]:
    """The active semester covering ``day`` (shortest first), else the half academic year (Jul–Dec / Jan–Jun)."""
    from iic_booking.equipment.models import Semester

    sems = [
        s for s in Semester.objects.filter(is_active=True, start_date__lte=day, end_date__gte=day)
        if s.start_date and s.end_date
    ]
    if sems:
        s = min(sems, key=lambda x: (x.end_date - x.start_date).days)
        start, end, label = s.start_date, s.end_date, s.name
    elif day.month >= 7:
        start, end, label = date(day.year, 7, 1), date(day.year, 12, 31), f"Jul–Dec {day.year}"
    else:
        start, end, label = date(day.year, 1, 1), date(day.year, 6, 30), f"Jan–Jun {day.year}"
    tz = timezone.get_current_timezone()
    return (
        timezone.make_aware(datetime.combine(start, time.min), tz),
        timezone.make_aware(datetime.combine(end + timedelta(days=1), time.min), tz),
        label,
    )


def _aware(value, field: str) -> datetime:
    parsed = value if isinstance(value, datetime) else parse_datetime(str(value or ""))
    if parsed is None:
        raise TrainingError(f"{field} must be an ISO date-time.")
    return timezone.make_aware(parsed) if timezone.is_naive(parsed) else parsed


def _minutes(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() // 60))


# ---------------------------------------------------------------------------
# Windows from the request
# ---------------------------------------------------------------------------
def build_windows(equipment, data: dict) -> tuple[list[dict], list[dict]]:
    """Shifts to create as [{start, end, slot_ids}] and skipped dates [{date, reason}]."""
    mode = (data.get("mode") or "slots").lower()
    windows: list[dict] = []
    skipped: list[dict] = []
    if mode == "slots":
        from iic_booking.equipment.models import DailySlot

        ids = [int(x) for x in (data.get("slot_ids") or []) if str(x).isdigit()]
        if not ids:
            raise TrainingError("Choose at least one slot.")
        slots = list(
            DailySlot.objects.filter(id__in=ids, slot_master__equipment_id=equipment.equipment_id).order_by("start_datetime", "id")
        )
        if len(slots) != len(set(ids)):
            raise TrainingError("Some selected slots do not belong to this equipment.")
        for s in slots:
            if windows and windows[-1]["end"] == s.start_datetime:
                windows[-1]["end"] = s.end_datetime
                windows[-1]["slot_ids"].append(s.id)
            else:
                windows.append({"start": s.start_datetime, "end": s.end_datetime, "slot_ids": [s.id]})
    elif mode in ("range", "recurring"):
        from iic_booking.equipment.models import Holiday

        d_from = parse_date(str(data.get("date_from") or ""))
        d_to = parse_date(str(data.get("date_to") or "")) or d_from
        t_from = parse_time(str(data.get("time_from") or ""))
        t_to = parse_time(str(data.get("time_to") or ""))
        if not d_from or not d_to or d_to < d_from:
            raise TrainingError("Choose a valid date range.")
        if (d_to - d_from).days > MAX_RANGE_DAYS:
            raise TrainingError(f"A range can cover at most {MAX_RANGE_DAYS} days.")
        if not t_from or not t_to or t_to <= t_from:
            raise TrainingError("Choose a daily time window (from before to).")
        default_days = [0, 1, 2, 3, 4, 5]
        weekdays = data.get("weekdays") if mode == "recurring" else (data.get("weekdays") or default_days)
        try:
            weekdays = sorted({int(x) for x in (weekdays or [])})
        except (TypeError, ValueError):
            raise TrainingError("weekdays must be numbers 0 (Monday) to 6 (Sunday).") from None
        if not weekdays or any(d < 0 or d > 6 for d in weekdays):
            raise TrainingError("Choose at least one weekday (0 = Monday … 6 = Sunday).")
        holidays = {
            h.date: h.reason or "Holiday"
            for h in Holiday.objects.filter(date__gte=d_from, date__lte=d_to, is_active=True)
        }
        tz = timezone.get_current_timezone()
        day = d_from
        while day <= d_to:
            if day.weekday() in weekdays:
                if day in holidays:
                    skipped.append({"date": day.isoformat(), "reason": f"Holiday: {holidays[day]}"})
                else:
                    windows.append(
                        {
                            "start": timezone.make_aware(datetime.combine(day, t_from), tz),
                            "end": timezone.make_aware(datetime.combine(day, t_to), tz),
                            "slot_ids": [],
                        }
                    )
            day += timedelta(days=1)
    else:
        raise TrainingError("mode must be slots, range or recurring.")
    if not windows:
        raise TrainingError("No duty windows in that selection.", extra={"skipped": skipped})
    if len(windows) > MAX_SHIFTS:
        raise TrainingError(f"At most {MAX_SHIFTS} shifts can be allocated at once.")
    return windows, skipped


def _proposal(windows: list[dict]) -> dict:
    by_week: dict[str, int] = {}
    total = 0
    for w in windows:
        m = _minutes(w["start"], w["end"])
        total += m
        by_week[week_key(w["start"])] = by_week.get(week_key(w["start"]), 0) + m
    return {"start": windows[0]["start"], "end": windows[-1]["end"], "minutes": total, "minutes_by_week": by_week}


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------
def _active_shifts():
    return DutyShift.objects.filter(status__in=LIVE_SHIFT, allocation__status__in=OPEN_ALLOCATION)


def _overlap_q(start, end, start_field="start_at", end_field="end_at") -> Q:
    return Q(**{f"{start_field}__lt": end, f"{end_field}__gt": start})


def conflicts_for(operator, equipment, windows: list[dict], *, exclude_allocation_id=None) -> list[list[dict]]:
    from iic_booking.equipment.models import DailySlot

    now = timezone.now()
    lo, hi = min(w["start"] for w in windows), max(w["end"] for w in windows)
    own_shifts = list(_active_shifts().filter(operator=operator).filter(_overlap_q(lo, hi)).exclude(allocation_id=exclude_allocation_id).select_related("equipment"))
    eq_shifts = list(
        _active_shifts().filter(equipment=equipment).exclude(operator=operator).filter(_overlap_q(lo, hi)).select_related("operator")
    )
    own_bookings = list(
        DailySlot.objects.filter(booking__user=operator, booking__status__in=ACTIVE_BOOKING)
        .filter(_overlap_q(lo, hi, "start_datetime", "end_datetime"))
        .values("start_datetime", "end_datetime", "booking_id")
    )
    eq_slots = list(
        DailySlot.objects.filter(slot_master__equipment_id=equipment.equipment_id)
        .filter(_overlap_q(lo, hi, "start_datetime", "end_datetime"))
        .values("start_datetime", "end_datetime", "status")
    )
    sessions = list(
        TrainingSession.objects.filter(
            event__registrations__user=operator, event__registrations__status=RegistrationStatus.CONFIRMED
        )
        .exclude(status="CANCELLED")
        .filter(_overlap_q(lo, hi))
        .values("start_at", "end_at", "event__title")
    )
    out = []
    for w in windows:
        s, e = w["start"], w["end"]
        rows = []
        if s < now:
            rows.append({"code": "past", "severity": "block", "message": "Starts in the past"})
        for sh in own_shifts:
            if sh.start_at < e and sh.end_at > s:
                rows.append({"code": "operator_overlap", "severity": "block", "message": f"Already on duty for {sh.equipment.name} at this time"})
                break
        for b in own_bookings:
            if b["start_datetime"] < e and b["end_datetime"] > s:
                rows.append({"code": "own_booking", "severity": "block", "message": "Has their own booking at this time"})
                break
        for sess in sessions:
            if sess["start_at"] < e and sess["end_at"] > s:
                rows.append({"code": "training", "severity": "warn", "message": f"Attending training: {sess['event__title']}"})
                break
        for sh in eq_shifts:
            if sh.start_at < e and sh.end_at > s:
                rows.append({"code": "equipment_overlap", "severity": "warn", "message": f"{get_user_display_name(sh.operator)} is already on duty"})
                break
        slots = [x for x in eq_slots if x["start_datetime"] < e and x["end_datetime"] > s]
        if not slots:
            rows.append({"code": "no_slots", "severity": "warn", "message": "No calendar slots generated for this time yet"})
        elif any(x["status"] in UNAVAILABLE_SLOT for x in slots):
            rows.append({"code": "unavailable", "severity": "warn", "message": "Equipment under maintenance or not available for part of this time"})
        out.append(rows)
    return out


# ---------------------------------------------------------------------------
# Fairness inputs
# ---------------------------------------------------------------------------
def _committed_minutes(shift: DutyShift) -> int:
    if shift.status == ShiftStatus.COMPLETED and shift.operated_minutes is not None:
        return int(shift.operated_minutes)
    return shift.planned_minutes


def policy_inputs(policy) -> dict:
    return {
        "weights": policy.duty_weights(),
        "max_hours_week": policy.duty_max_hours_week,
        "max_hours_term": policy.duty_max_hours_term,
        "cooling_days": policy.duty_cooling_days,
    }


def candidates(equipment, windows: list[dict], *, policy=None) -> tuple[list[dict], dict, dict]:
    """Ranked roster for the windows, the proposal and the policy inputs used."""
    policy = policy or operator_policy.effective(equipment)
    roster.sync([equipment.equipment_id])
    entries = list(roster.entries([equipment.equipment_id]))
    proposal = _proposal(windows)
    term_start, term_end, term_label = term_bounds(timezone.localtime(proposal["start"]).date())
    user_ids = [e.user_id for e in entries]
    shifts = list(
        DutyShift.objects.filter(
            operator_id__in=user_ids,
            allocation__status__in=COMMITTED_ALLOCATION,
        )
        .exclude(status__in=(ShiftStatus.RELEASED, ShiftStatus.CANCELLED, ShiftStatus.MISSED))
        .filter(start_at__gte=term_start - timedelta(days=60), start_at__lt=term_end + timedelta(days=60))
        .values("operator_id", "allocation_id", "start_at", "end_at", "status", "operated_minutes")
    )
    weeks = set(proposal["minutes_by_week"])
    stats: dict[int, dict] = {uid: {"term": 0, "weeks": {}, "allocs": set(), "last_end": None, "next_start": None} for uid in user_ids}
    alloc_span: dict[tuple[int, int], list[datetime]] = {}
    for sh in shifts:
        st = stats[sh["operator_id"]]
        mins = sh["operated_minutes"] if sh["status"] == ShiftStatus.COMPLETED and sh["operated_minutes"] is not None else _minutes(sh["start_at"], sh["end_at"])
        if term_start <= sh["start_at"] < term_end:
            st["term"] += mins
            st["allocs"].add(sh["allocation_id"])
        wk = week_key(sh["start_at"])
        if wk in weeks:
            st["weeks"][wk] = st["weeks"].get(wk, 0) + mins
        span = alloc_span.setdefault((sh["operator_id"], sh["allocation_id"]), [sh["start_at"], sh["end_at"]])
        span[0] = min(span[0], sh["start_at"])
        span[1] = max(span[1], sh["end_at"])
    for (uid, _), (a_start, a_end) in alloc_span.items():
        st = stats[uid]
        if a_end <= proposal["start"] and (st["last_end"] is None or a_end > st["last_end"]):
            st["last_end"] = a_end
        if a_start >= proposal["end"] and (st["next_start"] is None or a_start < st["next_start"]):
            st["next_start"] = a_start
    rows = []
    for e in entries:
        ok, why = roster.basis(e)
        st = stats.get(e.user_id) or {}
        rows.append(
            {
                "user_id": e.user_id,
                "roster_entry_id": e.pk,
                "name": get_user_display_name(e.user),
                "email": e.user.email,
                "faculty_id": e.faculty_id,
                "faculty_name": get_user_display_name(e.faculty) if e.faculty_id else "",
                "department_id": e.department_id,
                "department_name": getattr(e.department, "name", "") or "",
                "source": e.source,
                "eligible_basis": ok,
                "basis_reason": why,
                "term_minutes": st.get("term", 0),
                "week_minutes": st.get("weeks", {}),
                "allocations_term": len(st.get("allocs", ())),
                "last_duty_end": st.get("last_end"),
                "next_duty_start": st.get("next_start"),
                "max_hours_week": e.max_hours_week,
                "conflicts": [],
            }
        )
    inputs = policy_inputs(policy)
    ranked = rank(rows, proposal, inputs, now=timezone.now())
    meta = {"term": {"label": term_label, "start": term_start.isoformat(), "end": term_end.isoformat()}, "policy": inputs}
    return ranked, proposal, meta


def ranking_out(row: dict) -> dict:
    return {
        "user_id": row["user_id"],
        "roster_entry_id": row["roster_entry_id"],
        "name": row["name"],
        "email": row["email"],
        "faculty_name": row["faculty_name"],
        "department_name": row["department_name"],
        "source": row["source"],
        "basis": row["basis_reason"],
        "rank": row["rank"],
        "priority": row["priority"],
        "eligible": not row["hard_blocked"],
        "blocked": row["blocked"],
        "hard_blocked": row["hard_blocked"],
        "reasons": row["reasons"],
        "breakdown": row.get("breakdown", {}),
        "metrics": row.get("metrics", {}),
        "cooling_until": row.get("cooling_until"),
        "last_duty_end": row["last_duty_end"].isoformat() if row.get("last_duty_end") else None,
    }


def suggestions(equipment, windows: list[dict]) -> dict:
    ranked, proposal, meta = candidates(equipment, windows)
    return {
        "proposal": {
            "start": proposal["start"].isoformat(),
            "end": proposal["end"].isoformat(),
            "hours": round(proposal["minutes"] / 60, 2),
            "shifts": len(windows),
        },
        **meta,
        "ranking": [ranking_out(r) for r in ranked],
    }


# ---------------------------------------------------------------------------
# Allocation
# ---------------------------------------------------------------------------
def require_manager(equipment_id: int, actor) -> None:
    if not access.can_manage_equipment(actor, equipment_id):
        raise TrainingError("Only the equipment's OIC can allocate operator duty.", status=403, code="forbidden")


def _confirm_deadline(policy, first_start: datetime, requested) -> datetime:
    now = timezone.now()
    if requested:
        dl = _aware(requested, "confirm_by")
        if dl <= now:
            raise TrainingError("The confirmation deadline must be in the future.")
        return min(dl, first_start)
    dl = now + timedelta(hours=int(policy.duty_confirm_hours or 24))
    dl = min(dl, first_start - timedelta(minutes=30))
    return dl if dl > now + timedelta(minutes=30) else first_start


def plan(actor, data: dict) -> dict:
    """Shifts, conflicts and the fairness ranking for a proposed allocation (nothing is saved)."""
    from iic_booking.equipment.models import Equipment
    from iic_booking.users.models import User

    equipment = Equipment.objects.filter(pk=data.get("equipment_id")).first()
    if equipment is None:
        raise TrainingError("Equipment not found.", status=404)
    require_manager(equipment.equipment_id, actor)
    if not access.equipment_in_pilot(equipment):
        raise TrainingError("Training & Certification is not enabled for this equipment.", code="not_in_pilot")
    windows, skipped = build_windows(equipment, data)
    ranked, proposal, meta = candidates(equipment, windows)
    operator = User.objects.filter(pk=data.get("operator_id")).first() if data.get("operator_id") else None
    chosen = next((r for r in ranked if operator and r["user_id"] == operator.pk), None)
    shift_rows = []
    if operator is not None:
        conflicts = conflicts_for(operator, equipment, windows)
    else:
        conflicts = [[] for _ in windows]
    for w, c in zip(windows, conflicts):
        shift_rows.append(
            {
                "start": w["start"].isoformat(),
                "end": w["end"].isoformat(),
                "minutes": _minutes(w["start"], w["end"]),
                "slot_ids": w["slot_ids"],
                "conflicts": c,
            }
        )
    blocking = [c for row in conflicts for c in row if c["severity"] == "block"]
    top = next((r for r in ranked if r["rank"] == 1), None)
    needs_reason = []
    if chosen is not None:
        needs_reason += chosen["blocked"]
        if top is not None and top["user_id"] != chosen["user_id"]:
            needs_reason.append(f"{top['name']} is next in the fair rotation")
    policy = operator_policy.effective(equipment)
    return {
        "equipment": equipment,
        "operator": operator,
        "windows": windows,
        "skipped": skipped,
        "shifts": shift_rows,
        "ranking": ranked,
        "chosen": chosen,
        "proposal": proposal,
        "meta": meta,
        "blocking": blocking,
        "needs_reason": needs_reason,
        "policy": policy,
        "requires_confirmation_default": bool(policy.duty_confirmation_required),
    }


def plan_out(p: dict) -> dict:
    chosen = p["chosen"]
    return {
        "equipment_id": p["equipment"].equipment_id,
        "operator_id": p["operator"].pk if p["operator"] else None,
        "shifts": p["shifts"],
        "skipped": p["skipped"],
        "total_hours": round(p["proposal"]["minutes"] / 60, 2),
        "blocking": p["blocking"],
        "needs_reason": p["needs_reason"],
        "chosen": ranking_out(chosen) if chosen else None,
        "ranking": [ranking_out(r) for r in p["ranking"]],
        "term": p["meta"]["term"],
        "policy": p["meta"]["policy"],
        "requires_confirmation_default": p["requires_confirmation_default"],
        "hourly_rate": str(p["policy"].duty_hourly_rate),
    }


def create(actor, data: dict) -> DutyAllocation:
    from iic_booking.equipment.academic_years import current_label

    p = plan(actor, data)
    equipment, operator = p["equipment"], p["operator"]
    if operator is None:
        raise TrainingError("Choose an operator.")
    chosen = p["chosen"]
    if chosen is None:
        raise TrainingError("This person is not on the equipment's operator roster.", code="not_on_roster")
    if chosen["hard_blocked"]:
        raise TrainingError(f"{chosen['name']} cannot take duty: {'; '.join(map(str, chosen['hard_blocked']))}.", code="not_eligible")
    if p["blocking"]:
        raise TrainingError(
            "Some shifts conflict: " + "; ".join(sorted({c["message"] for c in p["blocking"]})) + ".",
            code="conflicts",
            extra={"shifts": p["shifts"]},
        )
    reason = (data.get("override_reason") or "").strip()
    if p["needs_reason"] and not reason:
        raise TrainingError(
            "Give a reason to choose this operator: " + "; ".join(p["needs_reason"]) + ".",
            code="override_reason_required",
            extra={"needs_reason": p["needs_reason"]},
        )
    policy = p["policy"]
    requires = data.get("requires_confirmation")
    requires = policy.duty_confirmation_required if requires is None else bool(requires)
    first_start = p["windows"][0]["start"]
    now = timezone.now()
    with transaction.atomic():
        entry = OperatorRosterEntry.objects.select_for_update().get(pk=chosen["roster_entry_id"])
        if any(c["severity"] == "block" for row in conflicts_for(operator, equipment, p["windows"]) for c in row):
            raise TrainingError("The schedule changed while allocating; please review the conflicts again.", code="conflicts")
        alloc = DutyAllocation.objects.create(
            equipment=equipment,
            operator=operator,
            roster_entry=entry,
            status=DutyStatus.PENDING if requires else DutyStatus.CONFIRMED,
            requires_confirmation=requires,
            confirm_by=_confirm_deadline(policy, first_start, data.get("confirm_by")) if requires else None,
            responded_at=None if requires else now,
            response_channel="" if requires else "AUTO",
            title=(data.get("title") or "").strip()[:255],
            note=(data.get("note") or "").strip(),
            suggested_rank=chosen["rank"],
            override_reason=reason,
            fairness_snapshot={
                "generated_at": now.isoformat(),
                "term": p["meta"]["term"],
                "policy": p["meta"]["policy"],
                "proposal_hours": round(p["proposal"]["minutes"] / 60, 2),
                "chosen": ranking_out(chosen),
                "ranking": [ranking_out(r) for r in p["ranking"][:10]],
                "needs_reason": p["needs_reason"],
            },
            academic_year=current_label(timezone.localtime(first_start).date()),
            hourly_rate=policy.duty_hourly_rate,
            planned_minutes=p["proposal"]["minutes"],
            allocated_by=actor,
        )
        DutyShift.objects.bulk_create(
            [
                DutyShift(
                    allocation=alloc,
                    equipment=equipment,
                    operator=operator,
                    start_at=w["start"],
                    end_at=w["end"],
                    daily_slot_ids=w["slot_ids"],
                )
                for w in p["windows"]
            ]
        )
        audit(
            actor,
            "duty.allocated",
            alloc,
            after={"operator": operator.pk, "shifts": len(p["windows"]), "hours": round(p["proposal"]["minutes"] / 60, 2), "rank": chosen["rank"]},
            note=reason,
        )
    _notify_allocated(alloc, actor)
    return alloc


# ---------------------------------------------------------------------------
# Responses
# ---------------------------------------------------------------------------
def make_token(alloc: DutyAllocation) -> str:
    return signing.dumps({"a": alloc.pk, "n": alloc.token_nonce}, salt=TOKEN_SALT, compress=True)


def from_token(token: str) -> DutyAllocation:
    try:
        payload = signing.loads(token or "", salt=TOKEN_SALT, max_age=TOKEN_MAX_AGE)
    except signing.SignatureExpired:
        raise TrainingError("This link has expired. Open My duty in the portal instead.", status=410, code="link_expired") from None
    except signing.BadSignature:
        raise TrainingError("This link is not valid.", status=400, code="link_invalid") from None
    alloc = DutyAllocation.objects.filter(pk=payload.get("a")).select_related("equipment", "operator").first()
    if alloc is None or alloc.token_nonce != payload.get("n"):
        raise TrainingError("This link is no longer valid.", status=410, code="link_invalid")
    return alloc


def respond(alloc: DutyAllocation, user, *, action: str, reason: str = "", channel: str = "PORTAL") -> DutyAllocation:
    if user is not None and user.pk != alloc.operator_id:
        raise TrainingError("This duty is allocated to someone else.", status=403, code="forbidden")
    if alloc.status != DutyStatus.PENDING:
        raise TrainingError(f"This duty is already {alloc.get_status_display().lower()}.", code="already_answered")
    now = timezone.now()
    if alloc.confirm_by and alloc.confirm_by < now:
        raise TrainingError("The confirmation deadline has passed.", code="deadline_passed")
    actor = user or alloc.operator
    with transaction.atomic():
        alloc = DutyAllocation.objects.select_for_update().get(pk=alloc.pk)
        if alloc.status != DutyStatus.PENDING:
            raise TrainingError("This duty has already been answered.", code="already_answered")
        if action == "confirm":
            clashes = [c for row in conflicts_for(alloc.operator, alloc.equipment, _windows_of(alloc), exclude_allocation_id=alloc.pk) for c in row if c["code"] in ("operator_overlap", "own_booking")]
            if clashes:
                raise TrainingError("You now have a clash at one of these times: " + clashes[0]["message"] + ". Decline or ask the OIC.", code="conflicts")
            alloc.status = DutyStatus.CONFIRMED
            label = "Confirmed"
        elif action == "decline":
            reason = (reason or "").strip()
            if not reason:
                raise TrainingError("Please give a reason for declining.", code="reason_required")
            alloc.status = DutyStatus.DECLINED
            alloc.decline_reason = reason
            alloc.shifts.filter(status=ShiftStatus.SCHEDULED).update(status=ShiftStatus.RELEASED, updated_at=now)
            label = "Declined"
        else:
            raise TrainingError("action must be confirm or decline.")
        alloc.responded_at = now
        alloc.response_channel = channel
        alloc.token_nonce = DutyAllocation._meta.get_field("token_nonce").default()
        alloc.save(update_fields=["status", "decline_reason", "responded_at", "response_channel", "token_nonce", "updated_at"])
        audit(actor, f"duty.{action}ed" if action == "confirm" else "duty.declined", alloc, after={"channel": channel}, note=reason)
    _notify_oics(alloc, actor, label, f"{get_user_display_name(alloc.operator)} {label.lower()} duty {alloc.reference}" + (f": {reason}" if reason else "."))
    return alloc


def cancel(alloc: DutyAllocation, actor, *, reason: str) -> DutyAllocation:
    require_manager(alloc.equipment_id, actor)
    reason = (reason or "").strip()
    if not reason:
        raise TrainingError("A reason is required to cancel.", code="reason_required")
    if alloc.status not in OPEN_ALLOCATION:
        raise TrainingError("Only pending or confirmed duty can be cancelled.")
    now = timezone.now()
    with transaction.atomic():
        alloc.shifts.filter(status=ShiftStatus.SCHEDULED).update(status=ShiftStatus.CANCELLED, updated_at=now)
        alloc.status = DutyStatus.CANCELLED
        alloc.cancelled_at = now
        alloc.cancelled_by = actor
        alloc.cancel_reason = reason
        alloc.token_nonce = DutyAllocation._meta.get_field("token_nonce").default()
        alloc.save(update_fields=["status", "cancelled_at", "cancelled_by", "cancel_reason", "token_nonce", "updated_at"])
        audit(actor, "duty.cancelled", alloc, note=reason)
    notify.send(
        "operator_duty_update_email",
        [alloc.operator],
        context=_context(alloc, summary=f"The OIC cancelled duty {alloc.reference} on {alloc.equipment.name}. Reason: {reason}", status="Cancelled"),
        title=f"Duty cancelled: {alloc.equipment.name}",
        message=f"Duty {alloc.reference} was cancelled. Reason: {reason}",
        path="/my-duty",
        actor=actor,
        event="training.duty.cancelled",
    )
    return alloc


def remind(alloc: DutyAllocation, actor=None) -> DutyAllocation:
    if actor is not None:
        require_manager(alloc.equipment_id, actor)
    if alloc.status != DutyStatus.PENDING:
        raise TrainingError("Only duty awaiting confirmation can be reminded.")
    _send_request(alloc, actor, reminder=True)
    alloc.reminder_sent_at = timezone.now()
    alloc.save(update_fields=["reminder_sent_at", "updated_at"])
    audit(actor, "duty.reminded", alloc)
    return alloc


def _windows_of(alloc: DutyAllocation) -> list[dict]:
    return [
        {"start": s.start_at, "end": s.end_at, "slot_ids": s.daily_slot_ids}
        for s in alloc.shifts.filter(status__in=LIVE_SHIFT).order_by("start_at")
    ] or [{"start": timezone.now() + timedelta(days=3650), "end": timezone.now() + timedelta(days=3650, minutes=1), "slot_ids": []}]


# ---------------------------------------------------------------------------
# Shifts: check-in/out, verification, hours
# ---------------------------------------------------------------------------
def _can_staff_shift(user, equipment_id: int) -> bool:
    return access.can_manage_equipment(user, equipment_id) or equipment_id in access.operator_equipment_ids(user)


def check_in(shift: DutyShift, user) -> DutyShift:
    is_self = user.pk == shift.operator_id
    if not (is_self or _can_staff_shift(user, shift.equipment_id)):
        raise TrainingError("Only the operator, the OIC or a Lab Operator can check in.", status=403, code="forbidden")
    if shift.allocation.status != DutyStatus.CONFIRMED:
        raise TrainingError("Check-in opens once the duty is confirmed.")
    if shift.status != ShiftStatus.SCHEDULED:
        raise TrainingError("This shift is not open for check-in.")
    now = timezone.now()
    if now < shift.start_at - CHECKIN_EARLY:
        raise TrainingError("Check-in opens 30 minutes before the shift.")
    if now > shift.end_at:
        raise TrainingError("The shift has ended; ask the OIC to record the hours.")
    shift.status = ShiftStatus.CHECKED_IN
    shift.check_in_at = now
    shift.checked_in_by = user
    shift.save(update_fields=["status", "check_in_at", "checked_in_by", "updated_at"])
    audit(user, "shift.checked_in", shift)
    return shift


def _close(shift: DutyShift, minutes: int, source: str, actor, *, remarks: str = "", status: str = ShiftStatus.COMPLETED) -> DutyShift:
    shift.status = status
    shift.operated_minutes = max(0, int(minutes))
    shift.hours_source = source
    if remarks:
        shift.remarks = remarks
    if source == HoursSource.OIC:
        shift.verified_by = actor
        shift.verified_at = timezone.now()
    shift.save()
    if status == ShiftStatus.COMPLETED and shift.operated_minutes:
        CertificationAward.objects.filter(
            user_id=shift.operator_id, equipment_id=shift.equipment_id, status=AwardStatus.ACTIVE
        ).update(last_used_at=shift.end_at)
    _maybe_complete(shift.allocation)
    return shift


def check_out(shift: DutyShift, user) -> DutyShift:
    if not (user.pk == shift.operator_id or _can_staff_shift(user, shift.equipment_id)):
        raise TrainingError("Only the operator, the OIC or a Lab Operator can check out.", status=403, code="forbidden")
    if shift.status != ShiftStatus.CHECKED_IN:
        raise TrainingError("Check in first.")
    now = timezone.now()
    shift.check_out_at = now
    cap = shift.planned_minutes + 120
    minutes = min(cap, _minutes(max(shift.check_in_at, shift.start_at - CHECKIN_EARLY), now))
    audit(user, "shift.checked_out", shift, after={"minutes": minutes})
    return _close(shift, minutes, HoursSource.CHECKIN, user)


def verify(shift: DutyShift, user, *, minutes, remarks: str = "", missed: bool = False) -> DutyShift:
    if not access.can_manage_equipment(user, shift.equipment_id):
        raise TrainingError("Only the equipment's OIC can record or correct duty hours.", status=403, code="forbidden")
    if shift.allocation.status not in (DutyStatus.CONFIRMED, DutyStatus.COMPLETED):
        raise TrainingError("Hours can be recorded only for confirmed duty.")
    if shift.status in (ShiftStatus.RELEASED, ShiftStatus.CANCELLED):
        raise TrainingError("This shift was not taken up.")
    if shift.start_at > timezone.now():
        raise TrainingError("Hours can be recorded once the shift has started.")
    before = {"status": shift.status, "minutes": shift.operated_minutes, "source": shift.hours_source}
    if missed:
        remarks = (remarks or "").strip()
        if not remarks:
            raise TrainingError("Say why the shift was missed.", code="reason_required")
        result = _close(shift, 0, HoursSource.OIC, user, remarks=remarks, status=ShiftStatus.MISSED)
    else:
        try:
            minutes = int(minutes)
        except (TypeError, ValueError):
            raise TrainingError("operated_minutes must be a whole number of minutes.") from None
        if not 0 <= minutes <= shift.planned_minutes + 240:
            raise TrainingError("Operated minutes must be between 0 and the shift length plus 4 hours.")
        result = _close(shift, minutes, HoursSource.OIC, user, remarks=(remarks or "").strip())
    audit(user, "shift.verified", shift, before=before, after={"status": result.status, "minutes": result.operated_minutes}, note=remarks)
    return result


def _booking_minutes(shift: DutyShift) -> int:
    from iic_booking.equipment.models import DailySlot

    total = 0
    for s in DailySlot.objects.filter(
        slot_master__equipment_id=shift.equipment_id, booking__status="COMPLETED"
    ).filter(_overlap_q(shift.start_at, shift.end_at, "start_datetime", "end_datetime")):
        total += _minutes(max(s.start_datetime, shift.start_at), min(s.end_datetime, shift.end_at))
    return total


def _maybe_complete(alloc: DutyAllocation) -> None:
    if alloc.shifts.filter(status__in=LIVE_SHIFT).exists():
        return
    if alloc.status == DutyStatus.CONFIRMED:
        DutyAllocation.objects.filter(pk=alloc.pk).update(status=DutyStatus.COMPLETED, completed_at=timezone.now(), updated_at=timezone.now())
        alloc.status = DutyStatus.COMPLETED


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------
def housekeeping() -> dict:
    now = timezone.now()
    out = {"reminded": 0, "released": 0, "closed_checkin": 0, "closed_booking": 0}
    for alloc in DutyAllocation.objects.filter(status=DutyStatus.PENDING, reminder_sent_at__isnull=True, confirm_by__gt=now).select_related("equipment", "operator"):
        hours = operator_policy.effective(alloc.equipment).duty_reminder_hours
        if hours and alloc.confirm_by - timedelta(hours=hours) <= now:
            try:
                remind(alloc)
                out["reminded"] += 1
            except Exception:
                logger.exception("duty reminder failed allocation=%s", alloc.pk)
    for alloc in DutyAllocation.objects.filter(status=DutyStatus.PENDING, confirm_by__lte=now).select_related("equipment", "operator"):
        with transaction.atomic():
            alloc.shifts.filter(status=ShiftStatus.SCHEDULED).update(status=ShiftStatus.RELEASED, updated_at=now)
            alloc.status = DutyStatus.EXPIRED
            alloc.escalated_at = now
            alloc.response_channel = "AUTO"
            alloc.save(update_fields=["status", "escalated_at", "response_channel", "updated_at"])
            audit(None, "duty.expired", alloc)
        nxt = ""
        try:
            windows = [{"start": s.start_at, "end": s.end_at, "slot_ids": s.daily_slot_ids} for s in alloc.shifts.filter(start_at__gt=now).order_by("start_at")]
            if windows:
                ranked, _, _ = candidates(alloc.equipment, windows)
                names = [r["name"] for r in ranked if r["rank"] and r["user_id"] != alloc.operator_id][:3]
                nxt = f" Next in rotation: {', '.join(names)}." if names else ""
        except Exception:
            logger.exception("duty next-in-rotation failed allocation=%s", alloc.pk)
        _notify_oics(alloc, None, "Released", f"{get_user_display_name(alloc.operator)} did not confirm duty {alloc.reference} by {notify.fmt_dt(alloc.confirm_by)}; the shifts were released.{nxt}")
        out["released"] += 1
    stale = now - AUTO_CLOSE_AFTER
    for shift in DutyShift.objects.filter(status=ShiftStatus.CHECKED_IN, end_at__lte=stale).select_related("allocation"):
        shift.check_out_at = shift.end_at
        minutes = _minutes(max(shift.check_in_at, shift.start_at - CHECKIN_EARLY), shift.end_at)
        _close(shift, minutes, HoursSource.CHECKIN, None, remarks="Checked out automatically at the end of the shift")
        out["closed_checkin"] += 1
    for shift in DutyShift.objects.filter(
        status=ShiftStatus.SCHEDULED, end_at__lte=stale, allocation__status=DutyStatus.CONFIRMED
    ).select_related("allocation"):
        minutes = _booking_minutes(shift)
        if minutes:
            _close(shift, minutes, HoursSource.BOOKING, None, remarks="From completed bookings in the shift")
            out["closed_booking"] += 1
    return out


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
def _context(alloc: DutyAllocation, **extra) -> dict:
    shifts = list(alloc.shifts.exclude(status__in=(ShiftStatus.CANCELLED,)).order_by("start_at")[:50])
    first, last = (shifts[0], shifts[-1]) if shifts else (None, None)
    when = notify.fmt_window(first.start_at, last.end_at) if first else ""
    return {
        "reference": alloc.reference,
        "equipment_name": alloc.equipment.name,
        "equipment_code": alloc.equipment.code,
        "title": alloc.title or f"Operator duty – {alloc.equipment.name}",
        "when": when + (f" ({len(shifts)} shifts)" if len(shifts) > 1 else ""),
        "duration": notify.fmt_minutes(alloc.planned_minutes),
        "remarks": alloc.note,
        **extra,
    }


def _send_request(alloc: DutyAllocation, actor, *, reminder: bool = False) -> None:
    token = make_token(alloc)
    path = f"/duty/respond?token={token}"
    deadline = notify.fmt_dt(alloc.confirm_by)
    summary = (
        f"{'Reminder: please' if reminder else 'Please'} confirm or decline operator duty on {alloc.equipment.name} by {deadline}. "
        "Unconfirmed duty is released to the next operator in the rotation."
    )
    notify.send(
        "operator_duty_reminder_email" if reminder else "operator_duty_allocated_email",
        [alloc.operator],
        context=_context(alloc, summary=summary, deadline=deadline, status="Awaiting your confirmation"),
        title="Confirm your operator duty" if not reminder else "Reminder: confirm your operator duty",
        message=f"Duty {alloc.reference} on {alloc.equipment.name}: confirm by {deadline}.",
        path="/my-duty",
        email_path=path,
        actor=actor,
        event="training.duty.request",
    )


def _notify_allocated(alloc: DutyAllocation, actor) -> None:
    if alloc.requires_confirmation:
        _send_request(alloc, actor)
        return
    notify.send(
        "operator_duty_allocated_email",
        [alloc.operator],
        context=_context(alloc, summary=f"You have been allocated operator duty on {alloc.equipment.name}.", status="Confirmed"),
        title=f"Operator duty: {alloc.equipment.name}",
        message=f"Duty {alloc.reference} on {alloc.equipment.name} has been allocated to you.",
        path="/my-duty",
        actor=actor,
        event="training.duty.allocated",
    )


def _notify_oics(alloc: DutyAllocation, actor, label: str, summary: str) -> None:
    from iic_booking.communication.in_app import equipment_oic_users

    recipients = list(equipment_oic_users(alloc.equipment))
    if alloc.allocated_by_id and alloc.allocated_by not in recipients:
        recipients.append(alloc.allocated_by)
    notify.send(
        "operator_duty_update_email",
        recipients,
        context=_context(alloc, summary=summary, status=label),
        title=f"Duty {label.lower()}: {alloc.equipment.name}",
        message=summary,
        path=f"/training/duty?allocation={alloc.pk}",
        actor=actor,
        event=f"training.duty.{label.lower()}",
    )
