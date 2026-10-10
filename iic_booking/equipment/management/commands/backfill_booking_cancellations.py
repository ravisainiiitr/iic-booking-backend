"""Build cancellation records for bookings cancelled before they were logged (records only).

For every booking whose status is cancelled / refunded / lab-disrupted / not utilized and that has no record yet:

- From history: replay its status events (who changed it, when, the refund in the event metadata).
- From an approved cancellation request: the request's user, responded-at time and the paid amount as refund.
- Inferred: the booking status only; the time is the booking's last update, the actor is not recorded.

Released slots come from the booking's remembered slot range (used to tell whether the slots were re-booked).
Dry run by default; ``--apply`` writes; ``--rebuild`` first drops rows that were not recorded at the time.
Output is counts only.
"""

from __future__ import annotations

import logging
from collections import Counter

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Backfill booking cancellation records from booking history (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Write the records (default: dry run).")
        parser.add_argument(
            "--rebuild",
            action="store_true",
            help="Drop rows rebuilt or inferred earlier and build them again (rows recorded at the time are kept).",
        )

    def handle(self, *args, **options):
        result = backfill_booking_cancellations(apply=options["apply"], rebuild=options["rebuild"])
        self.stdout.write(f"mode={'APPLY' if options['apply'] else 'DRY RUN'}")
        for key in ("removed_for_rebuild", "bookings_scanned", "from_history", "from_request", "inferred", "failed"):
            self.stdout.write(f"{key}={result[key]}")
        for reason, count in sorted(result["by_reason"].items()):
            self.stdout.write(f"reason.{reason}={count}")


def backfill_booking_cancellations(*, apply: bool, rebuild: bool = False) -> dict:
    from iic_booking.equipment.booking_cancellation_log import (
        TRACKED_STATUSES,
        record_cancellation,
        released_slots_in_range,
        replay_history,
    )
    from iic_booking.equipment.models import (
        Booking,
        BookingCancellation,
        BookingCancellationRequest,
        BookingCancellationRequestStatus,
        CancellationDataQuality,
        CancellationReason,
    )

    result = {
        "removed_for_rebuild": 0,
        "bookings_scanned": 0,
        "from_history": 0,
        "from_request": 0,
        "inferred": 0,
        "failed": 0,
        "by_reason": Counter(),
    }
    tracked = sorted(TRACKED_STATUSES)
    with transaction.atomic():
        if rebuild:
            result["removed_for_rebuild"] = (
                BookingCancellation.objects.exclude(data_quality=CancellationDataQuality.RECORDED).delete()[0]
            )
        bookings = (
            Booking.objects.filter(status__in=tracked, cancellation_record__isnull=True)
            .select_related("user", "user__supervisor", "equipment")
            .order_by("pk")
        )
        for booking in bookings.iterator(chunk_size=200):
            result["bookings_scanned"] += 1
            try:
                with transaction.atomic():
                    events = list(
                        booking.events.filter(Q(new_status__in=tracked) | Q(previous_status__in=tracked))
                        .select_related("created_by")
                        .order_by("created_at", "event_id")
                    )
                    source = None
                    if replay_history(booking, events):
                        source = "from_history"
                    else:
                        request = (
                            BookingCancellationRequest.objects.filter(
                                booking_id=booking.pk, status=BookingCancellationRequestStatus.APPROVED
                            )
                            .select_related("user")
                            .order_by("-responded_at", "-id")
                            .first()
                        )
                        if request is not None:
                            record_cancellation(
                                booking,
                                previous_status="BOOKED",
                                new_status=booking.status,
                                actor=request.user,
                                reason=CancellationReason.CANCELLATION_REQUEST,
                                note=(request.notes or "").strip(),
                                cancelled_at=request.responded_at or request.requested_at,
                                data_quality=CancellationDataQuality.FROM_HISTORY,
                            )
                            source = "from_request"
                        else:
                            record_cancellation(
                                booking,
                                previous_status="",
                                new_status=booking.status,
                                cancelled_at=booking.updated_at,
                                data_quality=CancellationDataQuality.INFERRED,
                            )
                            source = "inferred"
                    row = BookingCancellation.objects.filter(booking_id=booking.pk).first()
                    if row is not None:
                        fields = []
                        if not row.released_slot_ids:
                            row.released_slot_ids = released_slots_in_range(booking, row.slot_start, row.slot_end)
                            fields.append("released_slot_ids")
                        if source == "inferred" and row.new_status == "CANCELLED":
                            # Whether anything was refunded is not known without history.
                            row.refund_amount = None
                            fields.append("refund_amount")
                        if fields:
                            row.save(update_fields=[*fields, "updated_at"])
                    result[source] += 1
                    if row is not None:
                        result["by_reason"][row.reason] += 1
            except Exception:
                logger.exception("Could not backfill the cancellation of booking %s", booking.pk)
                result["failed"] += 1
        if not apply:
            transaction.set_rollback(True)
    return result
