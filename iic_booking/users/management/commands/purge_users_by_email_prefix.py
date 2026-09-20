"""Purge users by email prefix and associated operational data.

Usage (dry-run):
  python manage.py purge_users_by_email_prefix --prefix n1.sat

Delete (irreversible):
  python manage.py purge_users_by_email_prefix --prefix n1.sat --confirm PURGE_USERS_BY_EMAIL_PREFIX
"""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.db.models import Q


CONFIRM_TOKEN = "PURGE_USERS_BY_EMAIL_PREFIX"


class Command(BaseCommand):
    help = (
        "Delete users whose email starts with a given prefix (case-insensitive) "
        "and wipe their bookings, wallet activity, RA sessions, and related rows."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--prefix",
            type=str,
            default="n1.sat",
            help='Email prefix to match (default: "n1.sat").',
        )
        parser.add_argument(
            "--confirm",
            type=str,
            default="",
            help=f'Must be exactly "{CONFIRM_TOKEN}" to perform deletes.',
        )

    def handle(self, *args, **options):
        prefix = (options.get("prefix") or "").strip().lower()
        if not prefix or len(prefix) < 3:
            raise CommandError("Refusing: --prefix must be at least 3 characters.")

        User = get_user_model()
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
            TADutyLog,
            TARewardLedger,
            UrgentBookingRequest,
            WaitlistEntry,
            EquipmentManager,
            EquipmentOperator,
            EquipmentPI,
        )
        from iic_booking.users.models.payment import (
            DepartmentPaymentReceipt,
            PaymentGatewayTransaction,
            SricTransferRequest,
        )
        from iic_booking.users.models.wallet import (
            SubWallet,
            SubWalletTransaction,
            Wallet,
            WalletJoinRequest,
            WalletPeerTransfer,
            WalletRechargeImportRecord,
            WalletRechargeRequest,
        )

        users = User.objects.filter(email__istartswith=prefix)
        user_ids = list(users.values_list("pk", flat=True))
        emails = list(users.order_by("email").values_list("email", flat=True))
        booking_qs = Booking.objects.filter(user_id__in=user_ids)
        booking_ids = list(booking_qs.values_list("pk", flat=True))

        steps: list[tuple[str, object]] = []

        def add(label, qs):
            steps.append((label, qs))

        add(
            "booking_reward_redemptions",
            BookingRewardRedemption.objects.filter(
                Q(booking_id__in=booking_ids) | Q(student_id__in=user_ids)
            ),
        )
        add(
            "ta_assignments",
            TAAssignment.objects.filter(
                Q(booking_id__in=booking_ids) | Q(ta_student_id__in=user_ids)
            ),
        )
        add("ta_duty_logs", TADutyLog.objects.filter(student_id__in=user_ids))
        add("ta_reward_ledgers", TARewardLedger.objects.filter(student_id__in=user_ids))
        add("booking_result_files", BookingResultFile.objects.filter(booking_id__in=booking_ids))
        add("booking_sample_traces", BookingSampleTrace.objects.filter(booking_id__in=booking_ids))
        add("booking_events", BookingEvent.objects.filter(booking_id__in=booking_ids))
        add(
            "booking_cancellation_requests",
            BookingCancellationRequest.objects.filter(booking_id__in=booking_ids),
        )
        add("repeat_sample_requests", RepeatSampleRequest.objects.filter(booking_id__in=booking_ids))
        add(
            "urgent_booking_requests",
            UrgentBookingRequest.objects.filter(
                Q(user_id__in=user_ids) | Q(hold_booking_id__in=booking_ids)
            ),
        )
        add("waitlist_entries", WaitlistEntry.objects.filter(user_id__in=user_ids))
        add(
            "booking_attempt_logs",
            BookingAttemptLog.objects.filter(
                Q(user_id__in=user_ids) | Q(booking_id__in=booking_ids)
            ),
        )
        add(
            "payment_gateway_txns",
            PaymentGatewayTransaction.objects.filter(
                Q(user_id__in=user_ids) | Q(booking_id__in=booking_ids)
            ),
        )
        add(
            "payment_receipts",
            DepartmentPaymentReceipt.objects.filter(
                Q(user_id__in=user_ids) | Q(booking_id__in=booking_ids)
            ),
        )
        add("equipment_managers", EquipmentManager.objects.filter(manager_id__in=user_ids))
        add("equipment_operators", EquipmentOperator.objects.filter(operator_id__in=user_ids))
        add("equipment_pis", EquipmentPI.objects.filter(faculty_id__in=user_ids))

        # Optional modules
        for label, importer in [
            (
                "analysis_reservations",
                lambda: __import__(
                    "iic_booking.remote_analysis.models", fromlist=["AnalysisReservation"]
                ).AnalysisReservation.objects.filter(user_id__in=user_ids),
            ),
            (
                "analysis_workspaces",
                lambda: __import__(
                    "iic_booking.remote_analysis.models", fromlist=["AnalysisWorkspace"]
                ).AnalysisWorkspace.objects.filter(user_id__in=user_ids),
            ),
            (
                "remote_desktop_sessions",
                lambda: __import__(
                    "iic_booking.remote_analysis.models", fromlist=["RemoteDesktopSession"]
                ).RemoteDesktopSession.objects.filter(user_id__in=user_ids),
            ),
            (
                "tunnel_sessions",
                lambda: __import__(
                    "iic_booking.remote_analysis.models", fromlist=["TunnelSession"]
                ).TunnelSession.objects.filter(user_id__in=user_ids),
            ),
            (
                "portal_feedback",
                lambda: __import__(
                    "iic_booking.support.models", fromlist=["PortalFeedback"]
                ).PortalFeedback.objects.filter(user_id__in=user_ids),
            ),
            (
                "channel_i_profiles",
                lambda: __import__(
                    "iic_booking.users.models.channel_i_identity",
                    fromlist=["ChannelIIdentityProfile"],
                ).ChannelIIdentityProfile.objects.filter(user_id__in=user_ids),
            ),
            (
                "wallet_credit_facilities",
                lambda: __import__(
                    "iic_booking.users.models.wallet_credit_facility",
                    fromlist=["WalletCreditFacility"],
                ).WalletCreditFacility.objects.filter(user_id__in=user_ids),
            ),
            (
                "payment_orders",
                lambda: __import__(
                    "iic_booking.payments.models", fromlist=["PaymentOrder"]
                ).PaymentOrder.objects.filter(user_id__in=user_ids),
            ),
        ]:
            try:
                add(label, importer())
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(self.style.WARNING(f"skip {label}: {exc}"))

        from iic_booking.users import models as um

        for label, model_name, filt in [
            ("user_group_members", "UserGroupMember", lambda M: M.objects.filter(user_id__in=user_ids)),
            ("user_affiliations", "UserAffiliation", lambda M: M.objects.filter(user_id__in=user_ids)),
            (
                "hod_assignments",
                "HeadOfDepartmentAssignment",
                lambda M: M.objects.filter(user_id__in=user_ids),
            ),
            (
                "student_validity_extensions",
                "StudentValidityExtension",
                lambda M: M.objects.filter(Q(student_id__in=user_ids) | Q(requested_by_id__in=user_ids)),
            ),
            ("projects_as_faculty", "Project", lambda M: M.objects.filter(faculty_id__in=user_ids)),
        ]:
            M = getattr(um, model_name, None)
            if M is not None:
                try:
                    add(label, filt(M))
                except Exception as exc:  # noqa: BLE001
                    self.stdout.write(self.style.WARNING(f"skip {label}: {exc}"))

        slot_qs = DailySlot.objects.filter(booking_id__in=booking_ids)
        print_analysis_linked = PrintAnalysis.objects.filter(booking_id__in=booking_ids)
        print_batch_linked = PrintAnalysisBatch.objects.filter(booking_id__in=booking_ids)
        recharge_qs = WalletRechargeRequest.objects.filter(user_id__in=user_ids)
        sric_qs = SricTransferRequest.objects.filter(wallet_recharge_request__user_id__in=user_ids)
        join_qs = WalletJoinRequest.objects.filter(
            Q(student_id__in=user_ids) | Q(faculty_id__in=user_ids)
        )
        peer_qs = WalletPeerTransfer.objects.filter(
            Q(sender_id__in=user_ids) | Q(recipient_id__in=user_ids) | Q(initiated_by_id__in=user_ids)
        )
        import_qs = WalletRechargeImportRecord.objects.filter(user_id__in=user_ids)
        txn_qs = SubWalletTransaction.objects.filter(
            Q(sub_wallet__wallet__user_id__in=user_ids) | Q(related_user_id__in=user_ids)
        )
        sub_wallet_qs = SubWallet.objects.filter(wallet__user_id__in=user_ids)
        wallet_qs = Wallet.objects.filter(user_id__in=user_ids)

        try:
            from rest_framework.authtoken.models import Token

            token_qs = Token.objects.filter(user_id__in=user_ids)
        except Exception:
            token_qs = None

        self.stdout.write(self.style.WARNING("=== purge_users_by_email_prefix preview ==="))
        self.stdout.write(f"  database: {connection.settings_dict.get('NAME')}")
        self.stdout.write(f"  host:     {connection.settings_dict.get('HOST')}")
        self.stdout.write(f"  prefix:   {prefix!r}")
        self.stdout.write(f"  users:    {len(user_ids)}")
        for e in emails[:100]:
            self.stdout.write(f"    - {e}")
        if len(emails) > 100:
            self.stdout.write(f"    ... +{len(emails) - 100} more")
        self.stdout.write(f"  bookings: {len(booking_ids)}")
        for label, qs in steps:
            self.stdout.write(f"  {label}: {qs.count()}")
        self.stdout.write(f"  daily_slots to free: {slot_qs.count()}")
        self.stdout.write(f"  wallet_recharge_requests: {recharge_qs.count()}")
        self.stdout.write(f"  sric_transfer_requests: {sric_qs.count()}")
        self.stdout.write(f"  wallet_join_requests: {join_qs.count()}")
        self.stdout.write(f"  wallet_peer_transfers: {peer_qs.count()}")
        self.stdout.write(f"  wallet_recharge_imports: {import_qs.count()}")
        self.stdout.write(f"  sub_wallet_transactions: {txn_qs.count()}")
        self.stdout.write(f"  sub_wallets: {sub_wallet_qs.count()}")
        self.stdout.write(f"  wallets: {wallet_qs.count()}")
        if token_qs is not None:
            self.stdout.write(f"  auth_tokens: {token_qs.count()}")

        confirm = (options.get("confirm") or "").strip()
        if confirm != CONFIRM_TOKEN:
            self.stdout.write("")
            self.stdout.write(
                self.style.NOTICE(
                    f"Dry-run only. To delete, re-run with: --confirm {CONFIRM_TOKEN}"
                )
            )
            return

        if not user_ids:
            self.stdout.write(self.style.SUCCESS("No matching users — nothing to do."))
            return

        with transaction.atomic():
            self.stdout.write(f"Cleared booking FK on print_analyses: {print_analysis_linked.update(booking=None)}")
            self.stdout.write(f"Cleared booking FK on print_batches: {print_batch_linked.update(booking=None)}")
            self.stdout.write(
                f"Freed daily_slots: {slot_qs.update(booking=None, status=SlotStatus.AVAILABLE, blocked_label=None)}"
            )

            for label, qs in steps:
                deleted, details = qs.delete()
                self.stdout.write(f"Deleted {label}: {deleted} ({details})")

            booking_qs.update(source_booking=None)
            deleted, details = booking_qs.delete()
            self.stdout.write(f"Deleted bookings: {deleted} ({details})")

            for label, qs in [
                ("sric_transfer_requests", sric_qs),
                ("wallet_recharge_requests", recharge_qs),
                ("wallet_join_requests", join_qs),
                ("wallet_peer_transfers", peer_qs),
                ("wallet_recharge_imports", import_qs),
                ("sub_wallet_transactions", txn_qs),
                ("sub_wallets", sub_wallet_qs),
                ("wallets", wallet_qs),
            ]:
                deleted, details = qs.delete()
                self.stdout.write(f"Deleted {label}: {deleted} ({details})")

            if token_qs is not None:
                deleted, details = token_qs.delete()
                self.stdout.write(f"Deleted auth_tokens: {deleted} ({details})")

            User.objects.filter(pk__in=user_ids, supervisor_id__in=user_ids).update(supervisor=None)
            deleted, details = users.delete()
            self.stdout.write(f"Deleted users: {deleted} ({details})")

        remaining = User.objects.filter(email__istartswith=prefix).count()
        if remaining:
            raise CommandError(f"Wipe incomplete — {remaining} users still match prefix")

        self.stdout.write(self.style.SUCCESS(f"Purged all users with email starting {prefix!r}."))
