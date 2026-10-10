"""Lab Operator and Officer in Charge proficiency (Equipment overview).

Who keeps their queue short. Per person, over the equipment they are assigned to in scope (test equipment, test
users' bookings and test staff left out; Disposed equipment ignored):

- Pending: work waiting on them now. Lab Operators: bookings awaiting completion (slot over, sample received, not
  yet marked Completed — ``completion_reminders``) and repeat-sample requests. Officers in Charge: the same, plus
  urgent booking requests ready for their decision and I-STEM FBR numbers to verify. Everyone assigned to an
  equipment shares its queue.
- Overdue: pending completions past the equipment's results overdue time; requests waiting over
  ``DECISION_OVERDUE_HOURS`` hours.
- Handled: in the period, on their equipment — bookings they marked Completed, urgent and repeat-sample requests they
  decided, I-STEM FBRs they verified.
- Average response: completion time minus the later of the last slot end and sample receipt; decision time minus
  the request (or the supervisor's approval, when the request needed it). I-STEM verifications have no start time
  and are left out of the average.
- Proficiency score = handled ÷ (handled + pending + overdue) × 100, so overdue items weigh twice. Ranked by score
  (highest first), then faster average response, then fewer pending. People with nothing handled and nothing
  pending have no score and are listed last.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import timedelta
from typing import Any

from django.db.models import Max, Q
from django.utils import timezone

from .common import bounds, int_values, iso, scope_for, scope_payload

DEFAULT_DAYS = 30
MAX_DAYS = 365
DECISION_OVERDUE_HOURS = 48
ROLES = {"operator": "Lab Operator", "oic": "Officer in Charge"}
SORTS = ("proficiency", "pending", "response")
KINDS = {
    "completion": "Awaiting completion",
    "repeat_sample": "Repeat sample request",
    "urgent": "Urgent booking request",
    "istem_fbr": "I-STEM FBR to verify",
}
OIC_ONLY_KINDS = ("urgent", "istem_fbr")
PROFICIENCY_FORMULA = (
    "Handled ÷ (handled + pending + overdue) × 100 over the period — overdue items count twice. "
    "Ties go to the faster average response, then fewer pending."
)


def _days(params) -> int:
    raw = str(params.get("days") or "").strip()
    return max(1, min(int(raw), MAX_DAYS)) if raw.isdigit() else DEFAULT_DAYS


def _hours(delta) -> float:
    return max(delta.total_seconds(), 0.0) / 3600


def _equipment(scope) -> dict[int, dict[str, Any]]:
    from iic_booking.equipment.models import Equipment
    from iic_booking.equipment.testdata import exclude_test_equipment

    qs = scope.by_department(
        exclude_test_equipment(Equipment.objects.exclude(status="DISPOSED")), "internal_department_id"
    )
    return {r["equipment_id"]: r for r in qs.order_by().values("equipment_id", "name", "code")}


def _assignments(ids) -> dict[str, dict[int, set[int]]]:
    from iic_booking.equipment.models import EquipmentManager, EquipmentOperator

    out: dict[str, dict[int, set[int]]] = {"operator": defaultdict(set), "oic": defaultdict(set)}
    operators = EquipmentOperator.objects.filter(
        equipment_id__in=ids, operator__is_test_account=False, operator__is_active=True
    ).values_list("operator_id", "equipment_id")
    for person, equipment_id in operators:
        out["operator"][person].add(equipment_id)
    managers = EquipmentManager.objects.filter(
        equipment_id__in=ids, manager__is_test_account=False, manager__is_active=True
    ).values_list("manager_id", "equipment_id")
    for person, equipment_id in managers:
        out["oic"][person].add(equipment_id)
    return out


def _item(kind: str, key: str, equipment_id: int, since, overdue: bool, now, *, booking=None, ref="", link="",
          user=None) -> dict[str, Any]:
    from iic_booking.communication.in_app import person_label

    return {
        "key": key,
        "kind": kind,
        "kind_display": KINDS[kind],
        "equipment_id": equipment_id,
        "booking_pk": booking.pk if booking is not None else None,
        "booking_ref": ref or ((booking.virtual_booking_id or str(booking.pk)) if booking is not None else ""),
        "link": link or (f"/booking-management?expand={booking.pk}" if booking is not None else ""),
        "user_name": person_label(user) if user is not None else "",
        "since": iso(since),
        "waiting_hours": round(_hours(now - since), 1) if since else None,
        "overdue": bool(overdue),
    }


def _pending(ids, now) -> dict[int, list[dict[str, Any]]]:
    """Equipment id -> pending items (all kinds; ``OIC_ONLY_KINDS`` are dropped for Lab Operators)."""
    from iic_booking.equipment.completion_reminders import bookings_awaiting_completion, with_results_due
    from iic_booking.equipment.models import (
        Booking,
        IstemFbrStatus,
        RepeatSampleRequest,
        RepeatSampleRequestStatus,
        UrgentBookingRequest,
        UrgentBookingRequestStatus,
    )
    from iic_booking.equipment.results_overdue import is_results_overdue

    late = now - timedelta(hours=DECISION_OVERDUE_HOURS)
    items: dict[int, list[dict[str, Any]]] = defaultdict(list)
    awaiting = bookings_awaiting_completion(ids, now=now).exclude(user__is_test_account=True)
    for b, due in with_results_due(awaiting):
        items[b.equipment_id].append(
            _item("completion", f"completion:{b.pk}", b.equipment_id, b.completion_anchor,
                  is_results_overdue(b, due, now), now, booking=b, user=b.user)
        )
    repeats = (
        RepeatSampleRequest.objects.filter(status=RepeatSampleRequestStatus.PENDING, booking__equipment_id__in=ids)
        .exclude(booking__user__is_test_account=True)
        .select_related("booking", "booking__user")
    )
    for r in repeats:
        items[r.booking.equipment_id].append(
            _item("repeat_sample", f"repeat:{r.pk}", r.booking.equipment_id, r.requested_at, r.requested_at <= late,
                  now, booking=r.booking, user=r.booking.user)
        )
    urgent = (
        UrgentBookingRequest.objects.filter(status=UrgentBookingRequestStatus.PENDING, equipment_id__in=ids)
        .exclude(supervisor_approval_required=True, supervisor_decision="")
        .exclude(user__is_test_account=True)
        .select_related("user")
    )
    for r in urgent:
        since = (r.supervisor_decided_at if r.supervisor_approval_required else None) or r.requested_at
        items[r.equipment_id].append(
            _item("urgent", f"urgent:{r.pk}", r.equipment_id, since, since <= late, now,
                  ref=f"Urgent request #{r.pk}", link="/urgent-requests", user=r.user)
        )
    fbr = (
        Booking.objects.filter(istem_fbr_status=IstemFbrStatus.PENDING_OIC, equipment_id__in=ids)
        .exclude(user__is_test_account=True)
        .select_related("user")
    )
    for b in fbr:
        items[b.equipment_id].append(
            _item("istem_fbr", f"istem:{b.pk}", b.equipment_id, b.updated_at, b.updated_at <= late, now,
                  booking=b, user=b.user)
        )
    return items


def _handled(ids, people, start_at, end_at) -> dict[int, list[tuple[int, float | None]]]:
    """Person id -> [(equipment id, response hours or None)] for the work they finished in the period."""
    from iic_booking.equipment.models import (
        Booking,
        BookingEvent,
        BookingEventType,
        BookingStatus,
        RepeatSampleRequest,
        UrgentBookingRequest,
    )
    from iic_booking.equipment.results_deadline import annotate_sample_receipt

    out: dict[int, list[tuple[int, float | None]]] = defaultdict(list)
    if not people:
        return out
    completed: dict[tuple[int, int], Any] = {}
    events = (
        BookingEvent.objects.filter(
            created_at__gte=start_at, created_at__lt=end_at, created_by_id__in=people, booking__equipment_id__in=ids
        )
        .filter(Q(event_type=BookingEventType.COMPLETED) | Q(new_status=BookingStatus.COMPLETED))
        .exclude(booking__user__is_test_account=True)
        .order_by("created_at")
        .values_list("created_by_id", "booking_id", "created_at")
    )
    for person, booking_id, at in events:
        completed.setdefault((person, booking_id), at)
    booking_ids = sorted({b for _, b in completed})
    anchors: dict[int, tuple[int, Any]] = {}
    for i in range(0, len(booking_ids), 2000):
        rows = (
            annotate_sample_receipt(Booking.objects.filter(pk__in=booking_ids[i : i + 2000]))
            .annotate(last_slot_end=Max("daily_slots__end_datetime"))
            .values_list("pk", "equipment_id", "last_slot_end", "_sample_received_at")
        )
        for pk, equipment_id, last_end, received in rows:
            anchors[pk] = (equipment_id, max(v for v in (last_end, received) if v) if (last_end or received) else None)
    for (person, booking_id), at in completed.items():
        equipment_id, anchor = anchors.get(booking_id, (None, None))
        if equipment_id is not None:
            out[person].append((equipment_id, _hours(at - anchor) if anchor else None))

    urgent = (
        UrgentBookingRequest.objects.filter(
            decided_by_id__in=people, decided_at__gte=start_at, decided_at__lt=end_at, equipment_id__in=ids
        )
        .exclude(user__is_test_account=True)
        .values_list("decided_by_id", "equipment_id", "decided_at", "requested_at", "supervisor_decided_at")
    )
    for person, equipment_id, decided, requested, supervised in urgent:
        start = max(v for v in (requested, supervised) if v)
        out[person].append((equipment_id, _hours(decided - start)))
    repeats = (
        RepeatSampleRequest.objects.filter(
            responded_by_id__in=people, responded_at__gte=start_at, responded_at__lt=end_at,
            booking__equipment_id__in=ids,
        )
        .exclude(booking__user__is_test_account=True)
        .values_list("responded_by_id", "booking__equipment_id", "responded_at", "requested_at")
    )
    for person, equipment_id, responded, requested in repeats:
        out[person].append((equipment_id, _hours(responded - requested)))
    verified = (
        Booking.objects.filter(
            istem_fbr_verified_by_id__in=people,
            istem_fbr_executed_at__gte=start_at,
            istem_fbr_executed_at__lt=end_at,
            equipment_id__in=ids,
        )
        .exclude(user__is_test_account=True)
        .values_list("istem_fbr_verified_by_id", "equipment_id")
    )
    for person, equipment_id in verified:
        out[person].append((equipment_id, None))
    return out


def proficiency_score(handled: int, pending: int, overdue: int) -> int | None:
    denominator = handled + pending + overdue
    return round(100 * handled / denominator) if denominator else None


def rank_key(row: dict[str, Any]):
    avg = row["avg_response_hours"]
    return (
        row["score"] is None,
        -(row["score"] or 0),
        avg is None,
        avg or 0.0,
        row["pending"],
        (row["name"] or "").lower(),
    )


def _sort_key(sort: str):
    if sort == "pending":
        return lambda r: (r["pending"], r["overdue"], rank_key(r))
    if sort == "response":
        return lambda r: (r["avg_response_hours"] is None, r["avg_response_hours"] or 0.0, rank_key(r))
    return rank_key


def _person_rows(role, assignments, pending, handled, names, equipment) -> list[dict[str, Any]]:
    rows = []
    for person, equipment_ids in assignments.items():
        items = {
            item["key"]: item
            for eid in equipment_ids
            for item in pending.get(eid, [])
            if role == "oic" or item["kind"] not in OIC_ONLY_KINDS
        }
        done = [h for h in handled.get(person, []) if h[0] in equipment_ids]
        responses = [h[1] for h in done if h[1] is not None]
        overdue = sum(1 for item in items.values() if item["overdue"])
        by_kind = Counter(item["kind"] for item in items.values())
        rows.append(
            {
                "id": person,
                "name": names.get(person, ""),
                "role": role,
                "equipment": sorted(
                    ({"id": eid, "name": equipment[eid]["name"], "code": equipment[eid]["code"]}
                     for eid in equipment_ids if eid in equipment),
                    key=lambda e: (e["name"] or "").lower(),
                ),
                "pending": len(items),
                "overdue": overdue,
                "pending_by_kind": [
                    {"kind": k, "label": label, "count": by_kind[k]} for k, label in KINDS.items() if by_kind.get(k)
                ],
                "handled": len(done),
                "avg_response_hours": round(sum(responses) / len(responses), 1) if responses else None,
                "score": proficiency_score(len(done), len(items), overdue),
            }
        )
    rows.sort(key=rank_key)
    for i, row in enumerate(rows, start=1):
        row["rank"] = i if row["score"] is not None else None
    return rows


def _names(ids) -> dict[int, str]:
    from django.contrib.auth import get_user_model

    from iic_booking.users.display import get_user_display_name

    return {u.pk: get_user_display_name(u) for u in get_user_model().objects.filter(pk__in=ids)}


def build_staff_proficiency(user, params) -> dict[str, Any]:
    scope = scope_for(user, params)
    now = timezone.now()
    days = _days(params)
    end = timezone.localdate(now)
    start = end - timedelta(days=days - 1)
    start_at, end_at = bounds(start, end)
    equipment = _equipment(scope)
    ids = list(equipment)
    assignments = _assignments(ids)
    people = set(assignments["operator"]) | set(assignments["oic"])
    pending = _pending(ids, now) if ids else {}
    handled = _handled(ids, people, start_at, end_at)
    names = _names(people)
    sort = str(params.get("sort") or "proficiency").strip().lower()
    sort = sort if sort in SORTS else "proficiency"
    roles = {}
    for role in ROLES:
        rows = _person_rows(role, assignments[role], pending, handled, names, equipment)
        rows.sort(key=_sort_key(sort))
        roles[role] = rows
    payload: dict[str, Any] = {
        **scope_payload(scope),
        "generated_at": now.isoformat(),
        "date_from": start.isoformat(),
        "date_to": end.isoformat(),
        "days": days,
        "sort": sort,
        "formula": PROFICIENCY_FORMULA,
        "decision_overdue_hours": DECISION_OVERDUE_HOURS,
        "operators": roles["operator"],
        "oics": roles["oic"],
    }
    person = int_values([str(params.get("person") or "")])
    role = str(params.get("role") or "").strip().lower()
    if person and role in ROLES:
        mine = assignments[role].get(person[0], set())
        items = {
            item["key"]: {**item, "equipment_name": equipment[item["equipment_id"]]["name"],
                          "equipment_code": equipment[item["equipment_id"]]["code"]}
            for eid in mine
            for item in pending.get(eid, [])
            if role == "oic" or item["kind"] not in OIC_ONLY_KINDS
        }
        payload["person"] = {
            "id": person[0],
            "name": names.get(person[0], ""),
            "role": role,
            "pending": sorted(items.values(), key=lambda i: (not i["overdue"], i["since"] or "", i["key"])),
        }
    return payload
