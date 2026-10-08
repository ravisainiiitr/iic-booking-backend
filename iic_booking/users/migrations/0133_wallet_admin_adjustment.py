import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    """Wallet ledger: manual credit / debit of a sub-wallet by the Main Administrator (new table only)."""

    dependencies = [
        ("users", "0132_faculty_wallet_daily_sync"),
    ]

    operations = [
        migrations.CreateModel(
            name="WalletAdminAdjustment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("reference", models.CharField(blank=True, db_index=True, max_length=32)),
                ("client_request_id", models.CharField(max_length=64, unique=True)),
                ("direction", models.CharField(choices=[("credit", "Credit"), ("debit", "Debit")], max_length=8)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=12)),
                (
                    "reason",
                    models.CharField(
                        choices=[
                            ("manual_adjustment", "Manual adjustment"),
                            ("correction", "Correction"),
                            ("refund_outside_system", "Refund outside system"),
                            ("grant_top_up", "Grant top-up"),
                            ("other", "Other"),
                        ],
                        max_length=32,
                    ),
                ),
                ("remarks", models.TextField()),
                ("external_reference", models.CharField(blank=True, max_length=120)),
                ("balance_before", models.DecimalField(decimal_places=2, max_digits=12)),
                ("balance_after", models.DecimalField(decimal_places=2, max_digits=12)),
                ("notify_owner", models.BooleanField(default=True)),
                ("email_sent_at", models.DateTimeField(blank=True, null=True)),
                ("ip_address", models.GenericIPAddressField(blank=True, null=True)),
                ("user_agent", models.CharField(blank=True, max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "performed_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, related_name="+", to=settings.AUTH_USER_MODEL
                    ),
                ),
                (
                    "sub_wallet",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="admin_adjustments",
                        to="users.subwallet",
                    ),
                ),
                (
                    "sub_wallet_transaction",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="admin_adjustment",
                        to="users.subwallettransaction",
                    ),
                ),
                (
                    "wallet",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="admin_adjustments",
                        to="users.wallet",
                    ),
                ),
            ],
            options={
                "verbose_name": "Wallet admin adjustment",
                "verbose_name_plural": "Wallet admin adjustments",
                "ordering": ["-created_at"],
            },
        ),
    ]
