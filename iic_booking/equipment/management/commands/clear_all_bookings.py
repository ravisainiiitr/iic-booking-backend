"""
Delete all equipment bookings and related operational data so calendars can start fresh.

Keeps: users, equipment config, wallets/balances, recharge history, CMS, etc.

Usage (dry-run counts only):
  python manage.py clear_all_bookings

Actually delete (irreversible):
  python manage.py clear_all_bookings --confirm DELETE_ALL_BOOKINGS
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


CONFIRM_TOKEN = "DELETE_ALL_BOOKINGS"


class Command(BaseCommand):
    help = (
        "Wipe all bookings and booking-related records, and free BOOKED / "
        "BOOKING_NOT_UTILIZED daily slots. Does not touch wallets or users."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            type=str,
            default="",
            help=f'Must be exactly "{CONFIRM_TOKEN}" to perform deletes.',
        )

    def handle(self, *args, **options):
        from iic_booking.equipment.models import (
            Booking,
            BookingAttemptLog,
            BookingCancellationRequest,
            BookingEvent,
            BookingResultFile,
            BookingRewardRedemption,
            BookingSampleTrace,
            DailySlot,
            PrintAnalysis,
            PrintAnalysisBatch,
            RepeatSampleRequest,
            SlotStatus,
            TAAssignment,
            UrgentBookingRequest,
            WaitlistEntry,
        )
        from iic_booking.users.models.payment import (
            DepartmentPaymentReceipt,
            PaymentGatewayTransaction,
        )

        # Tables / querysets to clear (order matters for PROTECT FKs).
        steps: list[tuple[str, object]] = [
            ("booking_reward_redemptions", BookingRewardRedemption.objects.all()),
            ("ta_assignments", TAAssignment.objects.all()),
            ("booking_result_files", BookingResultFile.objects.all()),
            ("booking_sample_traces", BookingSampleTrace.objects.all()),
            ("booking_events", BookingEvent.objects.all()),
            ("booking_cancellation_requests", BookingCancellationRequest.objects.all()),
            ("repeat_sample_requests", RepeatSampleRequest.objects.all()),
            ("urgent_booking_requests", UrgentBookingRequest.objects.all()),
            ("waitlist_entries", WaitlistEntry.objects.all()),
            ("booking_attempt_logs", BookingAttemptLog.objects.all()),
            (
                "payment_gateway_txns (booking-linked)",
                PaymentGatewayTransaction.objects.filter(booking__isnull=False),
            ),
            (
                "payment_receipts (booking-linked)",
                DepartmentPaymentReceipt.objects.filter(booking__isnull=False),
            ),
        ]

        slot_qs = DailySlot.objects.filter(
            booking__isnull=False
        ) | DailySlot.objects.filter(
            status__in=[SlotStatus.BOOKED, SlotStatus.BOOKING_NOT_UTILIZED]
        )
        slot_qs = slot_qs.distinct()

        print_analysis_linked = PrintAnalysis.objects.filter(booking__isnull=False)
        print_batch_linked = PrintAnalysisBatch.objects.filter(booking__isnull=False)
        booking_count = Booking.objects.count()

        self.stdout.write(self.style.WARNING("=== clear_all_bookings preview ==="))
        self.stdout.write(f"  database: {connection.settings_dict.get('NAME')}")
        self.stdout.write(f"  host:     {connection.settings_dict.get('HOST')}")
        self.stdout.write(f"  bookings: {booking_count}")
        for label, qs in steps:
            self.stdout.write(f"  {label}: {qs.count()}")
        self.stdout.write(f"  daily_slots to free: {slot_qs.count()}")
        self.stdout.write(f"  print_analyses (booking link clear): {print_analysis_linked.count()}")
        self.stdout.write(f"  print_batches (booking link clear): {print_batch_linked.count()}")
        self.stdout.write("")
        self.stdout.write(
            "Preserved: users, equipment, charge profiles, wallets/balances, "
            "recharge history, slot templates, maintenance blocks."
        )

        confirm = (options.get("confirm") or "").strip()
        if confirm != CONFIRM_TOKEN:
            self.stdout.write("")
            self.stdout.write(
                self.style.NOTICE(
                    f'Dry-run only. To delete, re-run with: --confirm {CONFIRM_TOKEN}'
                )
            )
            return

        with transaction.atomic():
            # Detach print analyses before booking delete (SET_NULL also works; explicit is clearer).
            n = print_analysis_linked.update(booking=None)
            self.stdout.write(f"Cleared booking FK on print_analyses: {n}")
            n = print_batch_linked.update(booking=None)
            self.stdout.write(f"Cleared booking FK on print_batches: {n}")

            # Free calendar slots that were tied to bookings.
            n = slot_qs.update(
                booking=None,
                status=SlotStatus.AVAILABLE,
                blocked_label=None,
            )
            self.stdout.write(f"Freed daily_slots: {n}")

            for label, qs in steps:
                deleted, details = qs.delete()
                self.stdout.write(f"Deleted {label}: {deleted} ({details})")

            # Break self-FK, then wipe bookings.
            Booking.objects.update(source_booking=None)
            deleted, details = Booking.objects.all().delete()
            self.stdout.write(f"Deleted bookings: {deleted} ({details})")

        remaining = Booking.objects.count()
        if remaining:
            raise CommandError(f"Wipe incomplete — {remaining} bookings remain")

        self.stdout.write(self.style.SUCCESS("All bookings cleared. Calendars are free for a fresh start."))
