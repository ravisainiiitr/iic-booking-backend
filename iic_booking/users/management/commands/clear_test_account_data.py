"""
Wipe bookings, wallet activity, and recharge/SRIC rows for is_test_account users.

Keeps test users by default (re-seed stays simple). Pass --delete-users to remove them.

Usage (dry-run counts only):
  python manage.py clear_test_account_data

Actually delete (irreversible):
  python manage.py clear_test_account_data --confirm CLEAR_TEST_ACCOUNT_DATA
  python manage.py clear_test_account_data --confirm CLEAR_TEST_ACCOUNT_DATA --delete-users
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Q


CONFIRM_TOKEN = "CLEAR_TEST_ACCOUNT_DATA"


class Command(BaseCommand):
    help = (
        "Wipe activity for User.is_test_account=True: bookings (and PROTECT children), "
        "wallet recharge/SRIC rows, sub-wallet transactions; zero their sub-wallet balances. "
        "Optionally delete the test users themselves."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            type=str,
            default="",
            help=f'Must be exactly "{CONFIRM_TOKEN}" to perform deletes.',
        )
        parser.add_argument(
            "--delete-users",
            action="store_true",
            help="After wiping activity, delete the test user accounts themselves.",
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
        from iic_booking.users.models import User
        from iic_booking.users.models.payment import (
            DepartmentPaymentReceipt,
            PaymentGatewayTransaction,
            SricTransferRequest,
        )
        from iic_booking.users.models.wallet import (
            SubWallet,
            SubWalletTransaction,
            WalletJoinRequest,
            WalletRechargeRequest,
        )

        test_users = User.objects.filter(is_test_account=True)
        test_user_ids = list(test_users.values_list("pk", flat=True))
        booking_qs = Booking.objects.filter(user_id__in=test_user_ids)
        booking_ids = list(booking_qs.values_list("pk", flat=True))

        steps: list[tuple[str, object]] = [
            (
                "booking_reward_redemptions",
                BookingRewardRedemption.objects.filter(booking_id__in=booking_ids),
            ),
            ("ta_assignments", TAAssignment.objects.filter(booking_id__in=booking_ids)),
            (
                "booking_result_files",
                BookingResultFile.objects.filter(booking_id__in=booking_ids),
            ),
            (
                "booking_sample_traces",
                BookingSampleTrace.objects.filter(booking_id__in=booking_ids),
            ),
            ("booking_events", BookingEvent.objects.filter(booking_id__in=booking_ids)),
            (
                "booking_cancellation_requests",
                BookingCancellationRequest.objects.filter(booking_id__in=booking_ids),
            ),
            (
                "repeat_sample_requests",
                RepeatSampleRequest.objects.filter(booking_id__in=booking_ids),
            ),
            (
                "urgent_booking_requests",
                UrgentBookingRequest.objects.filter(
                    Q(user_id__in=test_user_ids) | Q(hold_booking_id__in=booking_ids)
                ),
            ),
            (
                "waitlist_entries",
                WaitlistEntry.objects.filter(user_id__in=test_user_ids),
            ),
            (
                "booking_attempt_logs",
                BookingAttemptLog.objects.filter(
                    Q(user_id__in=test_user_ids) | Q(booking_id__in=booking_ids)
                ),
            ),
            (
                "payment_gateway_txns (booking-linked)",
                PaymentGatewayTransaction.objects.filter(booking_id__in=booking_ids),
            ),
            (
                "payment_receipts (booking-linked)",
                DepartmentPaymentReceipt.objects.filter(booking_id__in=booking_ids),
            ),
        ]

        slot_qs = DailySlot.objects.filter(booking_id__in=booking_ids)
        print_analysis_linked = PrintAnalysis.objects.filter(booking_id__in=booking_ids)
        print_batch_linked = PrintAnalysisBatch.objects.filter(booking_id__in=booking_ids)

        recharge_qs = WalletRechargeRequest.objects.filter(user_id__in=test_user_ids)
        sric_qs = SricTransferRequest.objects.filter(
            wallet_recharge_request__user_id__in=test_user_ids
        )
        join_qs = WalletJoinRequest.objects.filter(
            Q(student_id__in=test_user_ids) | Q(faculty_id__in=test_user_ids)
        )
        txn_qs = SubWalletTransaction.objects.filter(
            Q(sub_wallet__wallet__user_id__in=test_user_ids)
            | Q(related_user_id__in=test_user_ids)
        )
        sub_wallet_qs = SubWallet.objects.filter(wallet__user_id__in=test_user_ids)

        self.stdout.write(self.style.WARNING("=== clear_test_account_data preview ==="))
        self.stdout.write(f"  database: {connection.settings_dict.get('NAME')}")
        self.stdout.write(f"  host:     {connection.settings_dict.get('HOST')}")
        self.stdout.write(f"  test users: {len(test_user_ids)}")
        self.stdout.write(f"  bookings (owned by test users): {len(booking_ids)}")
        for label, qs in steps:
            self.stdout.write(f"  {label}: {qs.count()}")
        self.stdout.write(f"  daily_slots to free: {slot_qs.count()}")
        self.stdout.write(f"  print_analyses (booking link clear): {print_analysis_linked.count()}")
        self.stdout.write(f"  print_batches (booking link clear): {print_batch_linked.count()}")
        self.stdout.write(f"  wallet_recharge_requests: {recharge_qs.count()}")
        self.stdout.write(f"  sric_transfer_requests: {sric_qs.count()}")
        self.stdout.write(f"  wallet_join_requests: {join_qs.count()}")
        self.stdout.write(f"  sub_wallet_transactions: {txn_qs.count()}")
        self.stdout.write(f"  sub_wallets to zero: {sub_wallet_qs.count()}")
        if options.get("delete_users"):
            self.stdout.write("  test users: WILL BE DELETED after wipe")
        else:
            self.stdout.write("  test users: kept (re-run seed_test_users to refresh)")
        self.stdout.write("")
        self.stdout.write(
            "Preserved: non-test users, equipment, production wallet activity, CMS."
        )

        confirm = (options.get("confirm") or "").strip()
        if confirm != CONFIRM_TOKEN:
            self.stdout.write("")
            self.stdout.write(
                self.style.NOTICE(
                    f"Dry-run only. To delete, re-run with: --confirm {CONFIRM_TOKEN}"
                )
            )
            return

        with transaction.atomic():
            n = print_analysis_linked.update(booking=None)
            self.stdout.write(f"Cleared booking FK on print_analyses: {n}")
            n = print_batch_linked.update(booking=None)
            self.stdout.write(f"Cleared booking FK on print_batches: {n}")

            n = slot_qs.update(
                booking=None,
                status=SlotStatus.AVAILABLE,
                blocked_label=None,
            )
            self.stdout.write(f"Freed daily_slots: {n}")

            for label, qs in steps:
                deleted, details = qs.delete()
                self.stdout.write(f"Deleted {label}: {deleted} ({details})")

            # Break self-FK among test bookings only
            booking_qs.update(source_booking=None)
            deleted, details = booking_qs.delete()
            self.stdout.write(f"Deleted bookings: {deleted} ({details})")

            deleted, details = sric_qs.delete()
            self.stdout.write(f"Deleted sric_transfer_requests: {deleted} ({details})")
            deleted, details = recharge_qs.delete()
            self.stdout.write(f"Deleted wallet_recharge_requests: {deleted} ({details})")
            deleted, details = join_qs.delete()
            self.stdout.write(f"Deleted wallet_join_requests: {deleted} ({details})")

            deleted, details = txn_qs.delete()
            self.stdout.write(f"Deleted sub_wallet_transactions: {deleted} ({details})")
            n = sub_wallet_qs.update(balance=Decimal("0.00"))
            self.stdout.write(f"Zeroed sub_wallet balances: {n}")

            if options.get("delete_users"):
                deleted, details = test_users.delete()
                self.stdout.write(f"Deleted test users: {deleted} ({details})")

        remaining_bookings = Booking.objects.filter(user__is_test_account=True).count()
        if remaining_bookings:
            raise CommandError(
                f"Wipe incomplete — {remaining_bookings} test-user bookings remain"
            )

        self.stdout.write(
            self.style.SUCCESS(
                "Test account data cleared."
                + (" Users deleted." if options.get("delete_users") else " Users kept.")
            )
        )
