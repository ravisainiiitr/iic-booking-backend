# Booking Event History: add the missing "entered disruption" entries (Operator Absent / Under Maintenance /
# Analysis Not Possible) for bookings marked before the audit trail existed, and the reason on the user's
# later refund / reschedule entries. Who and when come from the slot status log or the disruption log; when
# neither matches, the entry is placed just before the user's decision and says the details were not stored.
# APPLY=1 writes; otherwise dry run. Run via `manage.py shell`.
import os
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from iic_booking.equipment import booking_audit as ba
from iic_booking.equipment.disruption_models import DisruptionEventSlot, SlotStatusChangeLog
from iic_booking.equipment.models import Booking, BookingEvent, BookingEventType, BookingSlotRange, BookingStatus

apply = os.environ.get("APPLY") == "1"
print(f"mode={'APPLY' if apply else 'DRY RUN'}")

NOTE_TAGS = (
    ("[Operator Unavailable]", "OPERATOR_ABSENT", False),
    ("[Under maintenance — Admin/OIC]", "MAINTENANCE", False),
    ("[Auto disruption: operator absent", "OPERATOR_ABSENT", True),
    ("[Backfill reconcile_stale_booked_bookings", "OPERATOR_ABSENT", True),
)
FINAL_STATUS_KINDS = {
    BookingStatus.ABSENT: "OPERATOR_ABSENT",
    BookingStatus.UNDER_MAINTENANCE: "MAINTENANCE",
    BookingStatus.OTHER_DISRUPTION: "OTHER_DISRUPTION",
}
KIND_DISRUPTION_TYPES = {
    "OPERATOR_ABSENT": ["OPERATOR_ABSENT"],
    "MAINTENANCE": ["UNDER_MAINTENANCE", "SCHEDULED_MAINTENANCE"],
    "OTHER_DISRUPTION": ["OTHER"],
}
DISRUPTION_TYPE_KINDS = {
    "OPERATOR_ABSENT": "OPERATOR_ABSENT",
    "UNDER_MAINTENANCE": "MAINTENANCE",
    "SCHEDULED_MAINTENANCE": "MAINTENANCE",
    "OTHER": "OTHER_DISRUPTION",
}
SLOT_STATUS_KINDS = {"OPERATOR_ABSENT": "OPERATOR_ABSENT", "UNDER_MAINTENANCE": "MAINTENANCE"}
ENTRY_CODES = {ba.REASON_OPERATOR_ABSENT, ba.REASON_UNDER_MAINTENANCE, ba.REASON_ANALYSIS_NOT_POSSIBLE}


def fmt(dt):
    return timezone.localtime(dt).strftime("%Y-%m-%d %H:%M") if dt else "-"


def has_entry(events):
    for e in events:
        audit = (e.metadata or {}).get(ba.AUDIT_KEY) or {}
        if e.new_status == BookingStatus.DISRUPTION_PENDING or audit.get("reason_code") in ENTRY_CODES:
            return True
    return False


def note_hints(booking):
    notes = booking.notes or ""
    for tag, kind, system in NOTE_TAGS:
        idx = notes.find(tag)
        if idx < 0:
            continue
        line = notes[idx:].split("\n", 1)[0]
        text = line.split("]", 1)[1].lstrip(": ").strip() if "]" in line else ""
        if system:
            text = line.strip("[]").strip()
        return kind, system, text
    return None, None, ""


def slot_scope(booking):
    rows = list(booking.daily_slots.values_list("id", "start_datetime", "end_datetime"))
    ids = [r[0] for r in rows]
    windows = [(r[1], r[2]) for r in rows if r[1] and r[2]]
    rng = BookingSlotRange.objects.filter(booking=booking).first()
    if rng and rng.start_datetime and rng.end_datetime:
        windows.append((rng.start_datetime, rng.end_datetime))
    return ids, windows


def match_slot_log(booking, ids, windows, kind, before):
    statuses = [s for s, k in SLOT_STATUS_KINDS.items() if kind in (None, k)]
    if not statuses:
        return None
    qs = SlotStatusChangeLog.objects.filter(equipment_id=booking.equipment_id, new_status__in=statuses)
    if before:
        qs = qs.filter(changed_at__lte=before + timedelta(minutes=1))
    for log in qs.order_by("changed_at"):
        if ids and set(log.slot_ids or []) & set(ids):
            return log
        if log.first_start and log.last_end and any(log.first_start < e and log.last_end > s for s, e in windows):
            return log
    return None


def match_disruption(booking, ids, windows, kind, before):
    overlap = Q(daily_slot_id__in=ids) if ids else Q(pk__in=[])
    for s, e in windows:
        overlap |= Q(start_datetime__lt=e, end_datetime__gt=s)
    qs = DisruptionEventSlot.objects.filter(overlap, event__equipment_id=booking.equipment_id, event__is_deleted=False)
    if kind:
        qs = qs.filter(event__disruption_type__in=KIND_DISRUPTION_TYPES[kind])
    if before:
        qs = qs.filter(created_at__lte=before + timedelta(minutes=1))
    return qs.select_related("event", "event__started_by").order_by("created_at").first()


def decision_choice(event):
    comment = str(event.comment or "").lower()
    if comment.startswith(("auto-", "auto ")):
        return None
    if event.event_type == BookingEventType.RESCHEDULED:
        return "reschedule"
    if event.new_status == BookingStatus.CANCELLED:
        return "cancel"
    return "refund"


candidates = (
    Booking.objects.filter(
        Q(status=BookingStatus.DISRUPTION_PENDING)
        | Q(maintenance_disruption_flag=True)
        | Q(events__previous_status=BookingStatus.DISRUPTION_PENDING)
        | Q(notes__contains="[Operator Unavailable]")
        | Q(notes__contains="[Under maintenance — Admin/OIC]")
        | Q(notes__contains="[Auto disruption: operator absent")
        | Q(notes__contains="[Backfill reconcile_stale_booked_bookings")
    )
    .distinct()
    .select_related("user", "equipment")
    .order_by("booking_id")
)

added = enriched = 0
summary = {"slot status log": 0, "disruption log": 0, "not stored": 0}
with transaction.atomic():
    for booking in candidates:
        events = list(BookingEvent.objects.filter(booking=booking).order_by("created_at"))
        decision = next((e for e in events if e.previous_status == BookingStatus.DISRUPTION_PENDING), None)
        note_kind, note_system, note_text = note_hints(booking)
        kind = (
            booking.disruption_kind
            or (FINAL_STATUS_KINDS.get(decision.new_status) if decision else None)
            or note_kind
        )
        label = booking.virtual_booking_id or booking.pk

        if not has_entry(events):
            before = decision.created_at if decision else None
            ids, windows = slot_scope(booking)
            log = match_slot_log(booking, ids, windows, kind, before)
            link = None if log else match_disruption(booking, ids, windows, kind, before)
            actor, system, reason, evidence = None, bool(note_system), note_text, "not stored"
            if log:
                kind = kind or SLOT_STATUS_KINDS.get(log.new_status)
                actor, system, at, evidence = log.changed_by, log.changed_by_id is None, log.changed_at, "slot status log"
                reason = reason or (log.label or "")
            elif link:
                ev = link.event
                kind = kind or DISRUPTION_TYPE_KINDS.get(ev.disruption_type)
                actor, system, at, evidence = ev.started_by, ev.started_by_id is None, link.created_at, "disruption log"
                reason = reason or (ev.reason or "")
            elif decision:
                at = decision.created_at - timedelta(seconds=1)
            elif booking.maintenance_decision_deadline_at:
                at = booking.maintenance_decision_deadline_at - timedelta(days=5)
            else:
                at = booking.updated_at
            if evidence != "not stored":
                system = system and actor is None
            summary[evidence] += 1

            code = ba.DISRUPTION_KIND_REASONS.get(str(kind or ""), ba.REASON_STATUS_CHANGE)
            reason_label = ba.REASON_LABELS.get(code, "Lab disruption")
            if system:
                actor_fields = {"actor_kind": "system", "actor_role": ba.ROLE_SYSTEM,
                                "actor_role_label": ba.ROLE_LABELS[ba.ROLE_SYSTEM], "actor_name": "System"}
            elif actor is not None:
                role = ba.actor_role_for_booking(actor, booking)
                actor_fields = {"actor_kind": "user" if role in ba.USER_SIDE_ROLES else "staff", "actor_role": role,
                                "actor_role_label": ba.ROLE_LABELS[role], "actor_name": ba._display_name(actor)}
            else:
                actor_fields = {"actor_kind": "staff", "actor_role": ba.ROLE_OTHER_STAFF,
                                "actor_role_label": "Staff (name not recorded)", "actor_name": ""}
            comment = (
                f"Marked {reason_label}."
                + (f" Reason: {reason}." if reason else "")
                + " Awaiting the user's choice (reschedule or full refund)."
                + (" Recorded later from the " + evidence + "." if evidence != "not stored"
                   else " Recorded later; the exact time was not stored.")
            )
            audit = {
                **actor_fields,
                "source": "BACKFILL",
                "source_label": f"Recorded later from the {evidence}" if evidence != "not stored" else "Recorded later",
                **ba.reason_fields(code, reason, disruption_kind=kind or ""),
                "backfilled": True,
            }
            print(
                f"  ADD {label} | {reason_label} | at {fmt(at)} | by {actor_fields['actor_name'] or actor_fields['actor_role_label']}"
                f" ({actor_fields['actor_role_label']}) | {evidence} | reason={reason or '-'}"
            )
            added += 1
            if apply:
                event = BookingEvent.objects.create(
                    booking=booking,
                    event_type=BookingEventType.STATUS_CHANGED,
                    previous_status=BookingStatus.BOOKED,
                    new_status=BookingStatus.DISRUPTION_PENDING,
                    comment=comment,
                    created_by=None if system else actor,
                    metadata={ba.AUDIT_KEY: audit},
                    notification_sent=False,
                )
                BookingEvent.objects.filter(pk=event.pk).update(created_at=at)

        for e in events:
            if e.previous_status != BookingStatus.DISRUPTION_PENDING:
                continue
            audit = dict((e.metadata or {}).get(ba.AUDIT_KEY) or {})
            if audit.get("reason_code"):
                continue
            choice = decision_choice(e)
            kind_label = ba.disruption_kind_label(kind)
            if choice is None:
                fields = ba.reason_fields(
                    ba.REASON_DISRUPTION_DEADLINE, f"No refund/reschedule choice in time after {kind_label}",
                    disruption_kind=kind or "",
                )
                fields.update({"actor_kind": "system", "actor_role": ba.ROLE_SYSTEM,
                               "actor_role_label": ba.ROLE_LABELS[ba.ROLE_SYSTEM], "actor_name": "System"})
            else:
                booking.disruption_kind = kind
                booking.maintenance_disruption_flag = True
                fields = ba.disruption_choice_audit(booking, e.previous_status, e.created_by, choice=choice)[ba.AUDIT_KEY]
            print(f"  REASON {label} | {e.get_event_type_display()} at {fmt(e.created_at)} | {fields['reason']}")
            enriched += 1
            if apply:
                metadata = dict(e.metadata or {})
                metadata[ba.AUDIT_KEY] = {**audit, **fields, "reason_backfilled": True}
                BookingEvent.objects.filter(pk=e.pk).update(metadata=metadata)

print(f"entries {'added' if apply else 'to add'}: {added} {summary}")
print(f"decision entries {'given' if apply else 'to give'} a reason: {enriched}")
