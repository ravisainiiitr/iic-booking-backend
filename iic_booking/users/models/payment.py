"""Payment gateway, UTR receipts, and SRIC transfer API models."""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import UniqueConstraint
from django.utils.translation import gettext_lazy as _

from .department import Department, DepartmentType


class PaymentGateway(models.TextChoices):
    SBIEPAY = "SBIEPAY", _("SBIePay")
    RAZORPAY = "RAZORPAY", _("Razorpay")


class PaymentPurpose(models.TextChoices):
    WALLET_RECHARGE = "WALLET_RECHARGE", _("Wallet recharge")
    BOOKING_SHORTFALL = "BOOKING_SHORTFALL", _("Booking balance payment")


class PaymentGatewayStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    SUCCESS = "SUCCESS", _("Success")
    FAILED = "FAILED", _("Failed")
    CANCELLED = "CANCELLED", _("Cancelled")


class PaymentGatewayTransaction(models.Model):
    """SBIePay (or future gateway) transaction — wallet top-up or booking shortfall."""

    gateway = models.CharField(
        max_length=20,
        choices=PaymentGateway.choices,
        default=PaymentGateway.SBIEPAY,
    )
    merchant_order_ref = models.CharField(max_length=64, unique=True, db_index=True)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    purpose = models.CharField(max_length=32, choices=PaymentPurpose.choices)
    status = models.CharField(
        max_length=16,
        choices=PaymentGatewayStatus.choices,
        default=PaymentGatewayStatus.PENDING,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="payment_gateway_transactions",
    )
    wallet = models.ForeignKey(
        "users.Wallet",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="payment_gateway_transactions",
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name="payment_gateway_transactions",
        limit_choices_to={"department_type": DepartmentType.INTERNAL},
    )
    booking = models.ForeignKey(
        "equipment.Booking",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="payment_gateway_transactions",
    )
    gateway_transaction_id = models.CharField(
        _("Gateway transaction reference"),
        max_length=128,
        blank=True,
        help_text=_("SBIePay ATRN / bank reference after payment"),
    )
    raw_response = models.JSONField(default=dict, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Payment gateway transaction")
        verbose_name_plural = _("Payment gateway transactions")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.merchant_order_ref} ₹{self.amount} ({self.status})"


class DepartmentPaymentReceiptPurpose(models.TextChoices):
    WALLET_RECHARGE = "WALLET_RECHARGE", _("Wallet recharge (offline)")
    BOOKING_SHORTFALL = "BOOKING_SHORTFALL", _("Booking balance (offline)")


class DepartmentPaymentReceiptStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending finance verification")
    PROCESSED = "PROCESSED", _("Processed")
    REJECTED = "REJECTED", _("Rejected")


class DepartmentPaymentReceipt(models.Model):
    """UTR / bank reference submitted by user — tracked per internal department for Finance."""

    utr_reference = models.CharField(max_length=64, db_index=True)
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name="payment_receipts",
        limit_choices_to={"department_type": DepartmentType.INTERNAL},
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="department_payment_receipts",
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    purpose = models.CharField(max_length=32, choices=DepartmentPaymentReceiptPurpose.choices)
    status = models.CharField(
        max_length=16,
        choices=DepartmentPaymentReceiptStatus.choices,
        default=DepartmentPaymentReceiptStatus.PENDING,
    )
    booking = models.ForeignKey(
        "equipment.Booking",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="payment_receipts",
    )
    wallet_recharge_request = models.ForeignKey(
        "users.WalletRechargeRequest",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="payment_receipts",
    )
    payment_date = models.DateField(null=True, blank=True)
    receipt_file = models.FileField(
        _("Payment receipt file"),
        upload_to="wallet_payment_receipts/%Y/%m/%d/",
        null=True,
        blank=True,
        help_text=_("Scanned / photographed payment receipt (required for IITR Student offline recharge)."),
    )
    finance_processed_at = models.DateTimeField(null=True, blank=True)
    finance_processed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="processed_payment_receipts",
    )
    finance_remarks = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Department payment receipt (UTR)")
        verbose_name_plural = _("Department payment receipts (UTR)")
        ordering = ["-created_at"]
        constraints = [
            UniqueConstraint(
                fields=["utr_reference", "department"],
                name="unique_utr_per_department",
            ),
        ]

    def __str__(self) -> str:
        return f"UTR {self.utr_reference} — {self.department} ₹{self.amount}"


class SricTransferRequestStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending SRIC transfer")
    TRANSFERRED = "TRANSFERRED", _("Transferred")
    REJECTED = "REJECTED", _("Rejected")
    CANCELLED = "CANCELLED", _("Cancelled")


class SricTransferRequest(models.Model):
    """Faculty wallet recharge amount transfer request exposed to SRIC office via API."""

    wallet_recharge_request = models.OneToOneField(
        "users.WalletRechargeRequest",
        on_delete=models.CASCADE,
        related_name="sric_transfer_request",
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name="sric_transfer_requests",
        limit_choices_to={"department_type": DepartmentType.INTERNAL},
    )
    grant_code = models.CharField(max_length=80)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    faculty_emp_id = models.CharField(max_length=50, blank=True)
    faculty_email = models.EmailField()
    faculty_name = models.CharField(max_length=255, blank=True)
    project_code = models.CharField(max_length=80, blank=True)
    project_name = models.CharField(max_length=255, blank=True)
    status = models.CharField(
        max_length=16,
        choices=SricTransferRequestStatus.choices,
        default=SricTransferRequestStatus.PENDING,
    )
    sric_reference = models.CharField(max_length=128, blank=True)
    transferred_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("SRIC transfer request")
        verbose_name_plural = _("SRIC transfer requests")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"SRIC {self.grant_code} ₹{self.amount} ({self.status})"
