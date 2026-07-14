"""
Delete all User Groups (and memberships) and Wallet Recharge Requests on this database.

Usage (dry-run):
  python manage.py clear_user_groups_and_recharge_requests

Delete (irreversible):
  python manage.py clear_user_groups_and_recharge_requests --confirm DELETE_USER_GROUPS_AND_RECHARGE_REQUESTS
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction


CONFIRM_TOKEN = "DELETE_USER_GROUPS_AND_RECHARGE_REQUESTS"


class Command(BaseCommand):
    help = "Wipe User Groups / members and all Wallet Recharge Requests (and linked SRIC/payment rows)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            type=str,
            default="",
            help=f'Must be exactly "{CONFIRM_TOKEN}" to perform deletes.',
        )

    def handle(self, *args, **options):
        from iic_booking.equipment.models import Equipment, EquipmentUserGroup
        from iic_booking.users.models.payment import (
            DepartmentPaymentReceipt,
            SricTransferRequest,
        )
        from iic_booking.users.models.user_group import UserGroup, UserGroupMember
        from iic_booking.users.models.wallet import WalletRechargeRequest

        eq_user_group_qs = EquipmentUserGroup.objects.all()
        visibility_bound = Equipment.objects.filter(visibility_group__isnull=False)
        member_qs = UserGroupMember.objects.all()
        group_qs = UserGroup.objects.all()
        recharge_qs = WalletRechargeRequest.objects.all()
        sric_qs = SricTransferRequest.objects.all()
        receipt_qs = DepartmentPaymentReceipt.objects.filter(
            wallet_recharge_request__isnull=False
        )

        self.stdout.write(self.style.WARNING("=== clear_user_groups_and_recharge_requests preview ==="))
        self.stdout.write(f"  database: {connection.settings_dict.get('NAME')}")
        self.stdout.write(f"  host:     {connection.settings_dict.get('HOST')}")
        self.stdout.write(f"  equipment_user_group links: {eq_user_group_qs.count()}")
        self.stdout.write(f"  equipment with visibility_group: {visibility_bound.count()}")
        self.stdout.write(f"  user_group_members: {member_qs.count()}")
        self.stdout.write(f"  user_groups: {group_qs.count()}")
        self.stdout.write(f"  wallet_recharge_requests: {recharge_qs.count()}")
        self.stdout.write(f"  sric_transfer_requests: {sric_qs.count()}")
        self.stdout.write(f"  payment_receipts (recharge-linked): {receipt_qs.count()}")
        self.stdout.write("")
        self.stdout.write("Preserved: users, equipment itself, departments, wallets.")

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
            # User groups: break PROTECT / SET_NULL references first.
            n, details = eq_user_group_qs.delete()
            self.stdout.write(f"Deleted equipment_user_group links: {n} ({details})")

            n = visibility_bound.update(visibility_group=None)
            self.stdout.write(f"Cleared equipment.visibility_group: {n}")

            n, details = member_qs.delete()
            self.stdout.write(f"Deleted user_group_members: {n} ({details})")

            n, details = group_qs.delete()
            self.stdout.write(f"Deleted user_groups: {n} ({details})")

            # Recharge requests: linked SRIC rows / receipts CASCADE, but delete explicitly for clarity.
            n, details = sric_qs.delete()
            self.stdout.write(f"Deleted sric_transfer_requests: {n} ({details})")

            n, details = receipt_qs.delete()
            self.stdout.write(f"Deleted payment_receipts (recharge-linked): {n} ({details})")

            n, details = recharge_qs.delete()
            self.stdout.write(f"Deleted wallet_recharge_requests: {n} ({details})")

        remaining_groups = UserGroup.objects.count()
        remaining_recharges = WalletRechargeRequest.objects.count()
        if remaining_groups or remaining_recharges:
            raise CommandError(
                f"Wipe incomplete — groups left={remaining_groups}, recharges left={remaining_recharges}"
            )

        self.stdout.write(
            self.style.SUCCESS("User groups and wallet recharge requests cleared.")
        )
