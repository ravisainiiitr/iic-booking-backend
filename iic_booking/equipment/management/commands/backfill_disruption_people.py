"""Fill in who started / resumed recorded disruptions where it can be derived (records only).

- Started by: events without one get the staff member of the latest slot status change (same equipment, a status
  of the same disruption type, overlapping slots, made no later than shortly after the event was recorded).
  Whole-equipment events have no slot changes to derive from and are left as they are.
- Started by / Ended by role: filled from the person's current role on the equipment when it was never stored.

Dry run by default; ``--apply`` writes. Output is counts and ids only (no names).
"""

from __future__ import annotations

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction

MATCH_WINDOW = timedelta(minutes=5)


class Command(BaseCommand):
    help = "Backfill Started by / roles on disruption events (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the changes (default: dry run).")

    def handle(self, *args, **options):
        result = backfill_disruption_people(apply=options["apply"])
        self.stdout.write(f"mode={'APPLY' if options['apply'] else 'DRY RUN'}")
        for key in (
            "events_scanned",
            "missing_started_by",
            "started_by_derived",
            "started_by_not_derivable",
            "started_role_filled",
            "ended_role_filled",
        ):
            self.stdout.write(f"{key}={result[key]}")
        self.stdout.write(f"derived_event_ids={result['derived_event_ids'][:200]}")


def _status_values_for(disruption_type: str) -> set[str]:
    from iic_booking.equipment.disruption_service import disruption_type_for_slot_status
    from iic_booking.equipment.models import SlotStatus

    return {s for s in SlotStatus.values if disruption_type_for_slot_status(s) == disruption_type}


def derive_started_by(event):
    """(user_id, log_id) of the slot status change that most likely started ``event``, or (None, None)."""
    from iic_booking.equipment.models import DisruptionScope, SlotStatusChangeLog

    if event.scope != DisruptionScope.SLOTS:
        return None, None
    slot_ids = set(event.slot_links.exclude(daily_slot_id=None).values_list("daily_slot_id", flat=True))
    if not slot_ids:
        return None, None
    logs = (
        SlotStatusChangeLog.objects.filter(
            equipment_id=event.equipment_id,
            new_status__in=_status_values_for(event.disruption_type),
            changed_by__isnull=False,
            changed_at__lte=event.started_at + MATCH_WINDOW,
        )
        .order_by("-changed_at", "-id")
        .values("id", "changed_by_id", "slot_ids")[:200]
    )
    for log in logs:
        if slot_ids & {int(s) for s in (log["slot_ids"] or []) if str(s).isdigit()}:
            return log["changed_by_id"], log["id"]
    return None, None


def backfill_disruption_people(*, apply: bool) -> dict:
    from iic_booking.equipment.disruption_service import staff_role_for
    from iic_booking.equipment.models import DisruptionEvent

    result = {
        "events_scanned": 0,
        "missing_started_by": 0,
        "started_by_derived": 0,
        "started_by_not_derivable": 0,
        "started_role_filled": 0,
        "ended_role_filled": 0,
        "derived_event_ids": [],
    }
    qs = DisruptionEvent.objects.select_related("started_by", "ended_by").order_by("id")
    with transaction.atomic():
        for event in qs.iterator(chunk_size=200):
            result["events_scanned"] += 1
            fields = []
            if event.started_by_id is None:
                result["missing_started_by"] += 1
                user_id, _ = derive_started_by(event)
                if user_id:
                    event.started_by_id = user_id
                    fields.append("started_by")
                    result["started_by_derived"] += 1
                    result["derived_event_ids"].append(event.pk)
                else:
                    result["started_by_not_derivable"] += 1
            if event.started_by_id and not event.started_by_role:
                from django.contrib.auth import get_user_model

                user = event.started_by if "started_by" not in fields else get_user_model().objects.get(
                    pk=event.started_by_id
                )
                event.started_by_role = staff_role_for(user, event.equipment_id)
                if event.started_by_role:
                    fields.append("started_by_role")
                    result["started_role_filled"] += 1
            if event.ended_by_id and not event.ended_by_role:
                event.ended_by_role = staff_role_for(event.ended_by, event.equipment_id)
                if event.ended_by_role:
                    fields.append("ended_by_role")
                    result["ended_role_filled"] += 1
            if fields and apply:
                event.save(update_fields=fields)
        if not apply:
            transaction.set_rollback(True)
    return result
