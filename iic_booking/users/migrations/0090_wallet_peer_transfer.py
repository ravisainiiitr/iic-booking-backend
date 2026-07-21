# Generated manually for faculty wallet-to-wallet peer transfers

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("users", "0089_user_joining_graduation_dates"),
    ]

    operations = [
        migrations.CreateModel(
            name="WalletPeerTransfer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "transaction_id",
                    models.CharField(
                        db_index=True,
                        help_text="Public unique transfer reference (e.g. W2W-…)",
                        max_length=40,
                        unique=True,
                        verbose_name="Transaction ID",
                    ),
                ),
                ("grant_code", models.CharField(blank=True, max_length=100, verbose_name="Grant Code")),
                ("amount", models.DecimalField(decimal_places=2, max_digits=10, verbose_name="Transfer Amount")),
                ("remarks", models.TextField(blank=True, verbose_name="Remarks")),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING_OTP", "Pending OTP"),
                            ("COMPLETED", "Completed"),
                            ("FAILED", "Failed"),
                            ("CANCELLED", "Cancelled"),
                            ("EXPIRED", "Expired"),
                        ],
                        db_index=True,
                        default="PENDING_OTP",
                        max_length=20,
                        verbose_name="Transaction Status",
                    ),
                ),
                ("otp_code", models.CharField(blank=True, max_length=6, verbose_name="OTP Code")),
                ("otp_expires_at", models.DateTimeField(blank=True, null=True, verbose_name="OTP Expires At")),
                ("otp_verified", models.BooleanField(default=False, verbose_name="OTP Verification Status")),
                ("otp_verified_at", models.DateTimeField(blank=True, null=True, verbose_name="OTP Verified At")),
                (
                    "sender_balance_after",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        max_digits=10,
                        null=True,
                        verbose_name="Sender Sub-wallet Balance After",
                    ),
                ),
                (
                    "recipient_balance_after",
                    models.DecimalField(
                        blank=True,
                        decimal_places=2,
                        max_digits=10,
                        null=True,
                        verbose_name="Recipient Sub-wallet Balance After",
                    ),
                ),
                ("failure_reason", models.TextField(blank=True, verbose_name="Failure Reason")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Created at")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Updated at")),
                ("completed_at", models.DateTimeField(blank=True, null=True, verbose_name="Completed at")),
                (
                    "department",
                    models.ForeignKey(
                        help_text="Department sub-wallet grant used for debit and credit",
                        limit_choices_to={"department_type": "internal"},
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="wallet_peer_transfers",
                        to="users.department",
                        verbose_name="Department (Grant)",
                    ),
                ),
                (
                    "initiated_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="wallet_peer_transfers_initiated",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Initiated By",
                    ),
                ),
                (
                    "recipient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="wallet_peer_transfers_received",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Recipient",
                    ),
                ),
                (
                    "sender",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="wallet_peer_transfers_sent",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Sender",
                    ),
                ),
            ],
            options={
                "verbose_name": "Wallet Peer Transfer",
                "verbose_name_plural": "Wallet Peer Transfers",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="walletpeertransfer",
            index=models.Index(fields=["sender", "status"], name="users_walle_sender__a8c1e1_idx"),
        ),
        migrations.AddIndex(
            model_name="walletpeertransfer",
            index=models.Index(fields=["recipient", "status"], name="users_walle_recipie_b2f4c2_idx"),
        ),
        migrations.AddIndex(
            model_name="walletpeertransfer",
            index=models.Index(fields=["status", "created_at"], name="users_walle_status__c3d5e3_idx"),
        ),
    ]
