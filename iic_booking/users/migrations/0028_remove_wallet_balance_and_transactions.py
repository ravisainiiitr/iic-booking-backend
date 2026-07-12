# Migration: Remove Wallet.balance and WalletTransaction; migrate to sub-wallets.

from decimal import Decimal
from django.db import migrations, models


def migrate_wallet_to_subwallets(apps, schema_editor):
    """Move each wallet's balance to General sub-wallet; create sub-wallets for departments with equipment; delete wallet transactions."""
    Wallet = apps.get_model("users", "Wallet")
    WalletTransaction = apps.get_model("users", "WalletTransaction")
    SubWallet = apps.get_model("users", "SubWallet")
    Department = apps.get_model("users", "Department")
    Equipment = apps.get_model("equipment", "Equipment")

    # Get or create General department (internal)
    general, _ = Department.objects.get_or_create(
        name="General",
        defaults={
            "department_type": "internal",
            "code": "GENERAL",
            "description": "Default department for equipment without assignment",
        },
    )

    # Departments that have at least one equipment
    dept_ids_with_equipment = set(
        Equipment.objects.filter(internal_department_id__isnull=False).values_list(
            "internal_department_id", flat=True
        )
    )
    dept_ids_with_equipment.add(general.id)
    departments_with_equipment = list(Department.objects.filter(id__in=dept_ids_with_equipment))

    for wallet in Wallet.objects.all():
        # Migrate balance to General sub-wallet
        balance = getattr(wallet, "balance", None) or Decimal("0.00")
        if balance > 0:
            sub, created = SubWallet.objects.get_or_create(
                wallet=wallet,
                department=general,
                defaults={"balance": balance},
            )
            if not created:
                sub.balance += balance
                sub.save(update_fields=["balance"])

        # Create sub-wallets for other departments-with-equipment with balance 0
        for dept in departments_with_equipment:
            if dept.id == general.id and balance > 0:
                continue
            SubWallet.objects.get_or_create(
                wallet=wallet,
                department=dept,
                defaults={"balance": Decimal("0.00")},
            )

    # Delete all wallet transactions
    WalletTransaction.objects.all().delete()


def noop_reverse(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0027_wallet_subwallet_only_and_razorpay_order"),
        ("equipment", "0017_add_internal_department"),
    ]

    operations = [
        migrations.RunPython(migrate_wallet_to_subwallets, noop_reverse),
        migrations.RemoveField(model_name="wallet", name="balance"),
        migrations.DeleteModel(name="WalletTransaction"),
    ]
