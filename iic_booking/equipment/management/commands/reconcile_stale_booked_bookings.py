"""
One-off / periodic backfill: BOOKED bookings whose latest BOOKED slot end_datetime is over 24 hours ago,
with all slots still BOOKED.

Applies the same routing as scheduled tasks:
  1) Booking Not Utilized (no refund) — empty or only Sample Sent lifecycle
  2) Operator Absent disruption — latest trace Forwarded / Sample Accepted / Processing
  3) Operator Unavailable (full refund) — other non-terminal lifecycle beyond Sample Sent

Skipped (logged): latest Held / Sample Rejected, or terminal traces, or apply_* failures.

Usage:
  python manage.py reconcile_stale_booked_bookings --dry-run
  python manage.py reconcile_stale_booked_bookings
  python manage.py reconcile_stale_booked_bookings --booking-id 12345
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from iic_booking.equipment.booking_not_utilized_service import (
    apply_booking_not_utilized,
    latest_booked_slot_end_datetime,
)
from iic_booking.equipment.maintenance_policy import apply_operator_absent_disruption_for_booking
from iic_booking.equipment.models import Booking, BookingSampleTrace, BookingStatus, SampleTraceStatus, SlotStatus
from iic_booking.equipment.operator_unavailable import apply_operator_unavailable_booking
from iic_booking.equipment.sample_trace_policy import (
    OPERATOR_UNAVAILABLE_AUTO_REFUND_EXCLUDED_LATEST_STATUSES,
    SAMPLE_TRACE_IN_LAB_OR_ANALYSIS_STATUSES,
    latest_sample_trace,
    trace_allows_booking_not_utilized,
)


TERMINAL_SAMPLE_STATUSES = frozenset(
    {
        SampleTraceStatus.COMPLETED,
        SampleTraceStatus.RETURNED,
        SampleTraceStatus.ARCHIVED,
        SampleTraceStatus.DISPOSED,
        SampleTraceStatus.NOT_UTILIZED,
        SampleTraceStatus.OP_UNAVAILABLE,
    }
)


class Command(BaseCommand):
    help = (
        "Update stale BOOKED bookings (>24h after latest booked slot end): Not Utilized, disruption, or "
        "Operator Unavailable (full refund), matching automated job rules."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Print actions only; do not change data.",
        )
        parser.add_argument(
            "--booking-id",
            type=int,
            default=None,
            help="Process only this booking_id (if it qualifies).",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        only_id = options["booking_id"]
        now = timezone.now()
        hours = 24
        deadline_delta = timedelta(hours=hours)

        qs = Booking.objects.filter(status=BookingStatus.BOOKED).select_related("user", "equipment")
        if only_id is not None:
            qs = qs.filter(booking_id=only_id)

        counts = {"not_util": 0, "disruption": 0, "absent_refund": 0, "skipped": 0, "dry_seen": 0}

        for booking in qs.prefetch_related("daily_slots").iterator(chunk_size=100):
            slots = list(booking.daily_slots.all())
            if not slots:
                counts["skipped"] += 1
                continue
            if any(getattr(s, "status", None) != SlotStatus.BOOKED for s in slots):
                if only_id is not None:
                    self.stdout.write(
                        self.style.WARNING(
                            f"booking_id={booking.booking_id}: skip (not all DailySlots are BOOKED)"
                        )
                    )
                counts["skipped"] += 1
                continue

            latest_end = latest_booked_slot_end_datetime(booking)
            if latest_end is None:
                counts["skipped"] += 1
                continue
            if now < latest_end + deadline_delta:
                if only_id is not None:
                    self.stdout.write(
                        f"booking_id={booking.booking_id}: skip (latest slot end + {hours}h not reached yet)"
                    )
                counts["skipped"] += 1
                continue

            bid = booking.booking_id
            if trace_allows_booking_not_utilized(bid):
                if dry_run:
                    self.stdout.write(f"DRY booking_id={bid} -> Booking Not Utilized")
                    counts["dry_seen"] += 1
                    continue
                if apply_booking_not_utilized(
                    booking,
                    actor=None,
                    automated=True,
                    hours_after_last_slot_end=hours,
                ):
                    counts["not_util"] += 1
                    self.stdout.write(self.style.SUCCESS(f"booking_id={bid} -> Booking Not Utilized"))
                else:
                    counts["skipped"] += 1
                    self.stdout.write(self.style.WARNING(f"booking_id={bid} -> not util apply failed or race"))
                continue

            if BookingSampleTrace.objects.filter(booking_id=bid, status__in=TERMINAL_SAMPLE_STATUSES).exists():
                counts["skipped"] += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"booking_id={bid}: skip (data inconsistency: BOOKED but terminal sample trace exists)"
                    )
                )
                continue

            latest_ev = latest_sample_trace(bid)
            latest_st = getattr(latest_ev, "status", None)

            if latest_st in SAMPLE_TRACE_IN_LAB_OR_ANALYSIS_STATUSES:
                if dry_run:
                    self.stdout.write(
                        f"DRY booking_id={bid} -> Operator Absent disruption (latest={latest_st})"
                    )
                    counts["dry_seen"] += 1
                    continue
                try:
                    tag = "[Backfill reconcile_stale_booked_bookings: operator absent disruption]"
                    booking.notes = f"{(booking.notes or '').strip()}\n{tag}".strip()
                    booking.save(update_fields=["notes"])
                except Exception:
                    pass
                apply_operator_absent_disruption_for_booking(booking, triggered_at=now)
                counts["disruption"] += 1
                self.stdout.write(
                    self.style.SUCCESS(f"booking_id={bid} -> Operator Absent disruption (latest={latest_st})")
                )
                continue

            if latest_st in OPERATOR_UNAVAILABLE_AUTO_REFUND_EXCLUDED_LATEST_STATUSES:
                counts["skipped"] += 1
                self.stdout.write(
                    self.style.WARNING(
                        f"booking_id={bid}: skip (latest trace {latest_st} — use manual / other workflow)"
                    )
                )
                continue

            non_sample_sent = BookingSampleTrace.objects.filter(booking_id=bid).exclude(
                status=SampleTraceStatus.SAMPLE_SENT
            ).exists()
            if not non_sample_sent:
                counts["skipped"] += 1
                continue

            if dry_run:
                self.stdout.write(f"DRY booking_id={bid} -> Operator Unavailable (full refund)")
                counts["dry_seen"] += 1
                continue
            try:
                apply_operator_unavailable_booking(
                    booking,
                    notes="Backfill: stale BOOKED booking over 24h after slot end (management command).",
                    actor=None,
                )
                counts["absent_refund"] += 1
                self.stdout.write(self.style.SUCCESS(f"booking_id={bid} -> Operator Unavailable (full refund)"))
            except ValueError as e:
                counts["skipped"] += 1
                self.stdout.write(self.style.ERROR(f"booking_id={bid}: Operator Unavailable failed: {e}"))

        self.stdout.write("")
        if dry_run:
            self.stdout.write(f"Dry run: would touch {counts['dry_seen']} booking(s).")
        else:
            self.stdout.write(
                f"Done: not_util={counts['not_util']}, disruption={counts['disruption']}, "
                f"absent_refund={counts['absent_refund']}, skipped={counts['skipped']}."
            )
