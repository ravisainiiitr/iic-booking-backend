"""
Delete all sub-wallet transactions and communication logs (production fresh start).

Also zeroes all sub-wallet balances so ledgers and balances stay consistent.

Usage (dry-run counts only):
  python manage.py clear_wallet_txns_and_comms

Actually delete (irreversible):
  python manage.py clear_wallet_txns_and_comms --confirm DELETE_WALLET_TXNS_AND_COMMS
"""

from __future__ import annotations

from decimal import Decimal

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


CONFIRM_TOKEN = "DELETE_WALLET_TXNS_AND_COMMS"


class Command(BaseCommand):
    help = (
        "Wipe all SubWalletTransaction rows and CommunicationLog rows, "
        "and set every sub-wallet balance to 0."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            type=str,
            default="",
            help=f'Must be exactly "{CONFIRM_TOKEN}" to perform deletes.',
        )
        parser.add_argument(
            "--keep-balances",
            action="store_true",
            help="Do not zero sub-wallet balances (usually inconsistent after wiping ledger).",
        )

    def handle(self, *args, **options):
        from iic_booking.communication.models import CommunicationLog
        from iic_booking.users.models.wallet import SubWallet, SubWalletTransaction

        txn_qs = SubWalletTransaction.objects.all()
        log_qs = CommunicationLog.objects.all()
        wallet_qs = SubWallet.objects.all()

        self.stdout.write(self.style.WARNING("=== clear_wallet_txns_and_comms preview ==="))
        self.stdout.write(f"  database: {connection.settings_dict.get('NAME')}")
        self.stdout.write(f"  host:     {connection.settings_dict.get('HOST')}")
        self.stdout.write(f"  sub_wallet_transactions: {txn_qs.count()}")
        self.stdout.write(f"  communication_logs:      {log_qs.count()}")
        self.stdout.write(f"  sub_wallets:             {wallet_qs.count()}")
        if options.get("keep_balances"):
            self.stdout.write("  balances: KEEP (unchanged)")
        else:
            self.stdout.write("  balances: will be set to ₹0.00")
        self.stdout.write("")
        self.stdout.write(
            "Preserved: users, wallets/sub-wallets themselves, equipment, recharge request rows "
            "(unless none), bookings, CMS."
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
            deleted_txns, txn_details = txn_qs.delete()
            self.stdout.write(f"Deleted sub_wallet_transactions: {deleted_txns} ({txn_details})")

            deleted_logs, log_details = log_qs.delete()
            self.stdout.write(f"Deleted communication_logs: {deleted_logs} ({log_details})")

            if not options.get("keep_balances"):
                n = wallet_qs.update(balance=Decimal("0.00"))
                self.stdout.write(f"Zeroed sub_wallet balances: {n}")

        remaining_txns = SubWalletTransaction.objects.count()
        remaining_logs = CommunicationLog.objects.count()
        if remaining_txns or remaining_logs:
            raise CommandError(
                f"Wipe incomplete — transactions left={remaining_txns}, logs left={remaining_logs}"
            )

        self.stdout.write(
            self.style.SUCCESS(
                "Wallet transactions and communication logs cleared."
            )
        )
