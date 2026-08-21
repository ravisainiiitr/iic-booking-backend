# Generated manually for portal migration ledger + Employee-ID mapping

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0095_initial_payment_models"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="PortalMigrationState",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("singleton_key", models.CharField(default="default", max_length=16, unique=True)),
                (
                    "phase",
                    models.CharField(
                        choices=[
                            ("PREPARATION", "Preparation"),
                            ("PARALLEL_OPERATION", "Parallel operation"),
                            ("FINANCIAL_FREEZE", "Financial freeze"),
                            ("FINAL_SYNC", "Final sync"),
                            ("RECONCILIATION", "Reconciliation"),
                            ("NEW_PORTAL_ACTIVE", "New portal active"),
                            ("OLD_PORTAL_READ_ONLY", "Old portal read-only"),
                            ("OLD_PORTAL_REDIRECT", "Old portal redirect"),
                        ],
                        default="PREPARATION",
                        max_length=32,
                    ),
                ),
                ("end_user_booking_enabled", models.BooleanField(default=True)),
                ("booking_opens_at", models.DateTimeField(blank=True, null=True)),
                (
                    "booking_lock_message",
                    models.TextField(
                        blank=True,
                        default=(
                            "The new IIC Equipment Booking Portal is currently under preparation.\n"
                            "Online booking will be available from: {date} {time}\n"
                            "Until then, please continue using the existing IIC Booking Portal."
                        ),
                    ),
                ),
                ("last_wallet_txn_watermark", models.PositiveBigIntegerField(default=0)),
                ("last_sync_at", models.DateTimeField(blank=True, null=True)),
                ("last_sync_error", models.TextField(blank=True, default="")),
                ("legacy_ledger_frozen", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Portal migration state",
                "verbose_name_plural": "Portal migration state",
            },
        ),
        migrations.CreateModel(
            name="LegacyWalletAccountMapping",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("employee_id", models.CharField(db_index=True, max_length=50, unique=True)),
                ("old_user_id", models.IntegerField(blank=True, null=True)),
                ("old_wallet_id", models.IntegerField(blank=True, null=True)),
                ("old_name", models.CharField(blank=True, default="", max_length=255)),
                ("old_email", models.CharField(blank=True, default="", max_length=255)),
                ("channel_i_employee_id", models.CharField(blank=True, default="", max_length=50)),
                ("channel_i_email", models.CharField(blank=True, default="", max_length=255)),
                ("channel_i_name", models.CharField(blank=True, default="", max_length=255)),
                (
                    "mapping_status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("VALID", "Valid"),
                            ("MAPPED", "Mapped"),
                            ("IMPORTED", "Imported"),
                            ("RECONCILED", "Reconciled"),
                            ("MISMATCH", "Mismatch"),
                            ("WALLET_MAPPING_EXCEPTION", "Wallet mapping exception"),
                            ("MISSING_EMPLOYEE_ID", "Missing employee ID"),
                            ("DUPLICATE_EMPLOYEE_ID", "Duplicate employee ID"),
                            ("CHANNEL_I_NOT_FOUND", "Channel-I / new user not found"),
                            ("MULTIPLE_MATCH", "Multiple matches"),
                            ("MANUAL_REVIEW", "Manual review"),
                        ],
                        db_index=True,
                        default="PENDING",
                        max_length=40,
                    ),
                ),
                ("reconciliation_status", models.CharField(blank=True, default="", max_length=32)),
                ("exception_reason", models.TextField(blank=True, default="")),
                ("old_credits", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("old_debits", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("old_wallet_balance", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True)),
                ("imported_credits", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("imported_debits", models.DecimalField(decimal_places=2, default=0, max_digits=14)),
                ("migration_batch", models.CharField(blank=True, default="", max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "new_user",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="legacy_wallet_mappings",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "verbose_name": "Legacy wallet account mapping",
                "verbose_name_plural": "Legacy wallet account mappings",
            },
        ),
        migrations.CreateModel(
            name="LegacyWalletLedgerEntry",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("employee_id", models.CharField(db_index=True, max_length=50)),
                ("source_system", models.CharField(default="OLD_PORTAL", max_length=32)),
                ("source_transaction_id", models.PositiveBigIntegerField()),
                ("source_wallet_id", models.IntegerField(blank=True, null=True)),
                ("source_user_id", models.IntegerField()),
                ("occurred_at", models.DateTimeField()),
                ("direction", models.CharField(choices=[("CREDIT", "Credit"), ("DEBIT", "Debit")], max_length=8)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14)),
                ("running_balance_source", models.DecimalField(blank=True, decimal_places=2, max_digits=14, null=True)),
                ("description", models.TextField(blank=True, default="")),
                ("reference", models.CharField(blank=True, default="", max_length=255)),
                ("utr", models.CharField(blank=True, default="", max_length=64)),
                ("migration_batch", models.CharField(db_index=True, max_length=64)),
                ("checksum", models.CharField(max_length=64)),
                ("imported_at", models.DateTimeField(auto_now_add=True)),
                (
                    "mapping",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="ledger_entries",
                        to="users.legacywalletaccountmapping",
                    ),
                ),
            ],
            options={
                "verbose_name": "Legacy wallet ledger entry",
                "verbose_name_plural": "Legacy wallet ledger entries",
                "ordering": ["-occurred_at", "-source_transaction_id"],
            },
        ),
        migrations.CreateModel(
            name="LegacyWalletSyncDeadLetter",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("source_transaction_id", models.PositiveBigIntegerField(unique=True)),
                ("source_user_id", models.IntegerField(blank=True, null=True)),
                ("employee_id", models.CharField(blank=True, default="", max_length=50)),
                ("reason", models.CharField(max_length=64)),
                ("detail", models.TextField(blank=True, default="")),
                ("payload", models.JSONField(blank=True, default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "verbose_name": "Legacy wallet sync dead letter",
                "verbose_name_plural": "Legacy wallet sync dead letters",
            },
        ),
        migrations.AddIndex(
            model_name="legacywalletaccountmapping",
            index=models.Index(fields=["mapping_status", "employee_id"], name="users_legac_mapping_2d1c4a_idx"),
        ),
        migrations.AddConstraint(
            model_name="legacywalletledgerentry",
            constraint=models.UniqueConstraint(
                fields=("source_system", "source_transaction_id"),
                name="uniq_legacy_wallet_source_txn",
            ),
        ),
        migrations.AddIndex(
            model_name="legacywalletledgerentry",
            index=models.Index(fields=["employee_id", "occurred_at"], name="users_legac_employe_7a91b2_idx"),
        ),
        migrations.AddIndex(
            model_name="legacywalletledgerentry",
            index=models.Index(fields=["source_transaction_id"], name="users_legac_source__9c12e3_idx"),
        ),
    ]
