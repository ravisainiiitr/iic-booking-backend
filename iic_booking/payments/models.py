"""Razorpay payment module: Orders, Payments, Refunds, Settlements."""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class PaymentPurpose(models.TextChoices):
    BOOKING_SHORTFALL = "BOOKING_SHORTFALL", _("Booking balance payment")
    WALLET_RECHARGE = "WALLET_RECHARGE", _("Wallet recharge")


class PaymentOrderStatus(models.TextChoices):
    CREATED = "CREATED", _("Created")
    PAID = "PAID", _("Paid")
    FAILED = "FAILED", _("Failed")
    CANCELLED = "CANCELLED", _("Cancelled")
    EXPIRED = "EXPIRED", _("Expired")


class PaymentStatus(models.TextChoices):
    CAPTURED = "CAPTURED", _("Captured")
    FAILED = "FAILED", _("Failed")
    REFUNDED = "REFUNDED", _("Refunded")
    PARTIALLY_REFUNDED = "PARTIALLY_REFUNDED", _("Partially refunded")


class PaymentVerifiedVia(models.TextChoices):
    CHECKOUT = "CHECKOUT", _("Checkout verify")
    WEBHOOK = "WEBHOOK", _("Webhook")


class RefundStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    PROCESSED = "PROCESSED", _("Processed")
    FAILED = "FAILED", _("Failed")


class PaymentOrder(models.Model):
    """Razorpay order created before Checkout."""

    razorpay_order_id = models.CharField(max_length=64, unique=True, db_index=True)
    receipt = models.CharField(max_length=64, db_index=True)
    purpose = models.CharField(max_length=32, choices=PaymentPurpose.choices)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="razorpay_payment_orders",
    )
    booking = models.ForeignKey(
        "equipment.Booking",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="razorpay_payment_orders",
    )
    wallet = models.ForeignKey(
        "users.Wallet",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="razorpay_payment_orders",
    )
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="razorpay_payment_orders",
    )
    base_amount = models.DecimalField(max_digits=12, decimal_places=2)
    convenience_fee = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    fee_gst = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    currency = models.CharField(max_length=8, default="INR")
    status = models.CharField(
        max_length=16,
        choices=PaymentOrderStatus.choices,
        default=PaymentOrderStatus.CREATED,
        db_index=True,
    )
    idempotency_key = models.CharField(max_length=128, unique=True, db_index=True)
    fee_percent_snapshot = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal("0"))
    fee_gst_percent_snapshot = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal("0"))
    raw_create_response = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["purpose", "status"]),
            models.Index(fields=["booking", "status"]),
        ]

    def __str__(self):
        return f"{self.razorpay_order_id} ({self.purpose} {self.status})"


class Payment(models.Model):
    """Captured Razorpay payment linked to an order."""

    payment_order = models.ForeignKey(
        PaymentOrder,
        on_delete=models.PROTECT,
        related_name="payments",
    )
    razorpay_payment_id = models.CharField(max_length=64, unique=True, db_index=True)
    razorpay_signature = models.CharField(max_length=255, blank=True)
    method = models.CharField(max_length=64, blank=True)
    gateway_reference = models.CharField(
        max_length=128,
        blank=True,
        help_text=_("Bank / RRN / gateway reference when available"),
    )
    customer_txn_ref = models.CharField(max_length=128, blank=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(
        max_length=24,
        choices=PaymentStatus.choices,
        default=PaymentStatus.CAPTURED,
        db_index=True,
    )
    verified_via = models.CharField(
        max_length=16,
        choices=PaymentVerifiedVia.choices,
        default=PaymentVerifiedVia.CHECKOUT,
    )
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.razorpay_payment_id} ({self.status})"


class PaymentRefund(models.Model):
    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="refunds")
    razorpay_refund_id = models.CharField(max_length=64, unique=True, db_index=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(
        max_length=16,
        choices=RefundStatus.choices,
        default=RefundStatus.PENDING,
    )
    reason = models.CharField(max_length=255, blank=True)
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="razorpay_refunds_initiated",
    )
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.razorpay_refund_id} ({self.amount})"


class PaymentSettlement(models.Model):
    """Razorpay settlement row for bank UTR / SBI reconciliation."""

    settlement_id = models.CharField(max_length=64, unique=True, db_index=True)
    bank_utr = models.CharField(max_length=128, blank=True, db_index=True)
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    settled_on = models.DateField(null=True, blank=True)
    payments = models.ManyToManyField(Payment, blank=True, related_name="settlements")
    raw_payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-settled_on", "-created_at"]

    def __str__(self):
        return f"{self.settlement_id} UTR={self.bank_utr or '-'}"
