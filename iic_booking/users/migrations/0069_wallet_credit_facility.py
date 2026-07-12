# Generated manually for wallet recharge credit facility

from decimal import Decimal

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0068_user_equipment_supply_chain_role"),
    ]

    operations = [
        migrations.CreateModel(
            name="WalletCreditFacilitySettings",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "balance_threshold_inr",
                    models.DecimalField(
                        decimal_places=2,
                        default=Decimal("1000"),
                        help_text="If the selected department sub-wallet balance is below this amount when raising a recharge request, the faculty may be offered the credit facility popup.",
                        max_digits=10,
                        verbose_name="Balance threshold (₹)",
                    ),
                ),
                (
                    "credit_window_days",
                    models.PositiveSmallIntegerField(
                        default=7,
                        help_text="Parse confirmation is expected within this many days from OTP verification. If not credited via parse in time, bookings for that department are blocked.",
                        verbose_name="Credit window (days)",
                    ),
                ),
                (
                    "max_credit_inr",
                    models.DecimalField(
                        decimal_places=2,
                        default=Decimal("1000"),
                        help_text="Upper cap for the temporary credit line. Actual line is min(this, requested recharge amount).",
                        max_digits=10,
                        verbose_name="Maximum credit line (₹)",
                    ),
                ),
            ],
            options={
                "verbose_name": "Wallet credit facility settings",
                "verbose_name_plural": "Wallet credit facility settings",
                "db_table": "users_walletcreditfacilitysettings",
            },
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="credit_expiry_notified_at",
            field=models.DateTimeField(blank=True, help_text="When the booking-hold email was sent after the window expired.", null=True, verbose_name="Credit expiry notification sent at"),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="credit_facility_opted_in",
            field=models.BooleanField(
                default=False,
                help_text="Faculty chose temporary credit line when balance was below threshold at OTP send time.",
                verbose_name="Credit facility opted in",
            ),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="credit_facility_status",
            field=models.CharField(
                choices=[
                    ("inactive", "Inactive"),
                    ("active", "Active — credit window open"),
                    ("expired_unpaid", "Window ended without parse credit — bookings on hold"),
                ],
                default="inactive",
                help_text="Tracks temporary overdraft lifecycle for this request.",
                max_length=20,
                verbose_name="Credit facility status",
            ),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="credit_limit_amount",
            field=models.DecimalField(
                blank=True,
                decimal_places=2,
                help_text="Max overdraft: min(admin cap, requested amount). Set when OTP is verified.",
                max_digits=10,
                null=True,
                verbose_name="Credit facility limit (₹)",
            ),
        ),
        migrations.AddField(
            model_name="walletrechargerequest",
            name="credit_window_ends_at",
            field=models.DateTimeField(
                blank=True,
                help_text="Parse credit expected before this time (from settings at activation).",
                null=True,
                verbose_name="Credit facility window ends at",
            ),
        ),
    ]
