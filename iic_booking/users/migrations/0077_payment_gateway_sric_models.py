# Payment gateway, UTR receipts, SRIC transfer API

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0076_department_grant_code_payments"),
        ("equipment", "0143_booking_payment_fields"),
    ]

    operations = [
        migrations.CreateModel(
            name="PaymentGatewayTransaction",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("gateway", models.CharField(choices=[("SBIEPAY", "SBIePay")], default="SBIEPAY", max_length=20)),
                ("merchant_order_ref", models.CharField(db_index=True, max_length=64, unique=True)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10)),
                (
                    "purpose",
                    models.CharField(
                        choices=[
                            ("WALLET_RECHARGE", "Wallet recharge"),
                            ("BOOKING_SHORTFALL", "Booking balance payment"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("SUCCESS", "Success"),
                            ("FAILED", "Failed"),
                            ("CANCELLED", "Cancelled"),
                        ],
                        default="PENDING",
                        max_length=16,
                    ),
                ),
                ("gateway_transaction_id", models.CharField(blank=True, max_length=128, verbose_name="Gateway transaction reference")),
                ("raw_response", models.JSONField(blank=True, default=dict)),
                ("verified_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "booking",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="payment_gateway_transactions",
                        to="equipment.booking",
                    ),
                ),
                (
                    "department",
                    models.ForeignKey(
                        limit_choices_to={"department_type": "internal"},
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="payment_gateway_transactions",
                        to="users.department",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="payment_gateway_transactions",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "wallet",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="payment_gateway_transactions",
                        to="users.wallet",
                    ),
                ),
            ],
            options={
                "verbose_name": "Payment gateway transaction",
                "verbose_name_plural": "Payment gateway transactions",
                "ordering": ["-created_at"],
            },
        ),
        migrations.CreateModel(
            name="DepartmentPaymentReceipt",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("utr_reference", models.CharField(db_index=True, max_length=64)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10)),
                (
                    "purpose",
                    models.CharField(
                        choices=[
                            ("WALLET_RECHARGE", "Wallet recharge (offline)"),
                            ("BOOKING_SHORTFALL", "Booking balance (offline)"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending finance verification"),
                            ("PROCESSED", "Processed"),
                            ("REJECTED", "Rejected"),
                        ],
                        default="PENDING",
                        max_length=16,
                    ),
                ),
                ("payment_date", models.DateField(blank=True, null=True)),
                ("finance_processed_at", models.DateTimeField(blank=True, null=True)),
                ("finance_remarks", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "booking",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="payment_receipts",
                        to="equipment.booking",
                    ),
                ),
                (
                    "department",
                    models.ForeignKey(
                        limit_choices_to={"department_type": "internal"},
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="payment_receipts",
                        to="users.department",
                    ),
                ),
                (
                    "finance_processed_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="processed_payment_receipts",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="department_payment_receipts",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "wallet_recharge_request",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="payment_receipts",
                        to="users.walletrechargerequest",
                    ),
                ),
            ],
            options={
                "verbose_name": "Department payment receipt (UTR)",
                "verbose_name_plural": "Department payment receipts (UTR)",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="departmentpaymentreceipt",
            constraint=models.UniqueConstraint(
                fields=("utr_reference", "department"),
                name="unique_utr_per_department",
            ),
        ),
        migrations.CreateModel(
            name="SricTransferRequest",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("grant_code", models.CharField(max_length=80)),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10)),
                ("faculty_emp_id", models.CharField(blank=True, max_length=50)),
                ("faculty_email", models.EmailField(max_length=254)),
                ("faculty_name", models.CharField(blank=True, max_length=255)),
                ("project_code", models.CharField(blank=True, max_length=80)),
                ("project_name", models.CharField(blank=True, max_length=255)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending SRIC transfer"),
                            ("TRANSFERRED", "Transferred"),
                            ("REJECTED", "Rejected"),
                            ("CANCELLED", "Cancelled"),
                        ],
                        default="PENDING",
                        max_length=16,
                    ),
                ),
                ("sric_reference", models.CharField(blank=True, max_length=128)),
                ("transferred_at", models.DateTimeField(blank=True, null=True)),
                ("rejection_reason", models.TextField(blank=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "department",
                    models.ForeignKey(
                        limit_choices_to={"department_type": "internal"},
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="sric_transfer_requests",
                        to="users.department",
                    ),
                ),
                (
                    "wallet_recharge_request",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="sric_transfer_request",
                        to="users.walletrechargerequest",
                    ),
                ),
            ],
            options={
                "verbose_name": "SRIC transfer request",
                "verbose_name_plural": "SRIC transfer requests",
                "ordering": ["-created_at"],
            },
        ),
    ]
