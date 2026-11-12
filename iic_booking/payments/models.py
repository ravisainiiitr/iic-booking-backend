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
    RECONCILIATION = "RECONCILIATION", _("Recovered by reconciliation (Razorpay API)")


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
    mode = models.CharField(
        max_length=8,
        blank=True,
        default="",
        db_index=True,
        help_text=_("Razorpay mode the order was created in: test or live. Blank for orders made before this was recorded."),
    )
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
    gateway_fee = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text=_("Razorpay fee for this payment, as Razorpay reports it (blank until known)."),
    )
    gateway_tax = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text=_("GST on the Razorpay fee, as Razorpay reports it (blank until known)."),
    )
    raw_payload = models.JSONField(default=dict, blank=True)
    # Printed receipt number, IITR/<department>/<year>/<5-digit serial> (receipt_numbers.py). Given once, when the
    # payment is confirmed, and never changed.
    receipt_number = models.CharField(max_length=48, unique=True, null=True, blank=True)
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


class WebhookEventStatus(models.TextChoices):
    PROCESSING = "PROCESSING", _("Processing")
    PROCESSED = "PROCESSED", _("Processed")
    IGNORED = "IGNORED", _("Received, nothing to apply")
    FAILED = "FAILED", _("Failed, Razorpay will retry")


class RazorpayWebhookEvent(models.Model):
    """One signed webhook event from Razorpay, keyed by its event ID so a repeat is never processed twice.

    Only identifiers and amounts are kept, never the full payload (it can hold payer email, phone and card details).
    """

    event_id = models.CharField(max_length=64, unique=True)
    event_type = models.CharField(max_length=64, db_index=True)
    razorpay_order_id = models.CharField(max_length=64, blank=True, db_index=True)
    razorpay_payment_id = models.CharField(max_length=64, blank=True, db_index=True)
    razorpay_refund_id = models.CharField(max_length=64, blank=True)
    razorpay_settlement_id = models.CharField(max_length=64, blank=True)
    amount_paise = models.BigIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=8, blank=True)
    entity_status = models.CharField(max_length=32, blank=True)
    status = models.CharField(max_length=16, choices=WebhookEventStatus.choices, default=WebhookEventStatus.PROCESSING)
    result = models.CharField(max_length=255, blank=True)
    attempts = models.PositiveIntegerField(default=1)
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"{self.event_type} {self.event_id} ({self.status})"


class PaymentIssueKind(models.TextChoices):
    DUPLICATE_CAPTURE = "DUPLICATE_CAPTURE", _("Second payment captured on an already paid order")
    CANCELLED_BOOKING = "CANCELLED_BOOKING", _("Payment received for a cancelled or refunded booking")
    NOT_APPLIED = "NOT_APPLIED", _("Payment captured but could not be applied")
    UNKNOWN_ORDER = "UNKNOWN_ORDER", _("Payment for an order the portal does not know")
    MISMATCH = "MISMATCH", _("Payment amount or currency differs from the order")
    NOT_CAPTURED = "NOT_CAPTURED", _("Authorised payment could not be captured")
    ORDER_WITHOUT_PAYMENT = "ORDER_WITHOUT_PAYMENT", _("Order marked paid with no captured payment")
    UNSETTLED = "UNSETTLED", _("Captured payment not yet in any Razorpay settlement")
    SETTLEMENT_MISMATCH = "SETTLEMENT_MISMATCH", _("Settlement amount differs from the payment")
    SETTLEMENT_UNKNOWN = "SETTLEMENT_UNKNOWN", _("Settlement line for a payment the portal does not have")


class PaymentIssueStatus(models.TextChoices):
    OPEN = "OPEN", _("Open")
    RESOLVED = "RESOLVED", _("Resolved")


class PaymentIssue(models.Model):
    """Money that reached Razorpay but was not applied normally. Finance resolves each one with a note."""

    kind = models.CharField(max_length=24, choices=PaymentIssueKind.choices, db_index=True)
    key = models.CharField(max_length=160, help_text=_("Payment ID, or order ID when there is no payment."))
    status = models.CharField(max_length=12, choices=PaymentIssueStatus.choices, default=PaymentIssueStatus.OPEN, db_index=True)
    razorpay_order_id = models.CharField(max_length=64, blank=True)
    razorpay_payment_id = models.CharField(max_length=64, blank=True)
    payment_order = models.ForeignKey(PaymentOrder, on_delete=models.SET_NULL, null=True, blank=True, related_name="issues")
    amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    mode = models.CharField(max_length=8, blank=True)
    detail = models.CharField(max_length=500)
    raised_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="resolved_payment_issues"
    )
    resolution_note = models.TextField(blank=True)

    class Meta:
        ordering = ["-raised_at"]
        constraints = [
            models.UniqueConstraint(fields=["kind", "key"], condition=models.Q(status="OPEN"), name="payments_one_open_issue_per_key")
        ]

    def __str__(self):
        return f"{self.kind} {self.key} ({self.status})"


class SettlementLine(models.Model):
    """One line of Razorpay's settlement reconciliation report: a payment, refund or adjustment in a settlement.

    Kept separately from PaymentSettlement so gross, fee, tax and net are never confused. Amounts are in rupees.
    """

    settlement_id = models.CharField(max_length=64, db_index=True)
    settlement_utr = models.CharField(max_length=128, blank=True)
    entity_id = models.CharField(max_length=64, db_index=True)
    entity_type = models.CharField(max_length=24)
    razorpay_order_id = models.CharField(max_length=64, blank=True)
    payment = models.ForeignKey(Payment, on_delete=models.SET_NULL, null=True, blank=True, related_name="settlement_lines")
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    fee = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    tax = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    credit = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    debit = models.DecimalField(max_digits=14, decimal_places=2, default=Decimal("0.00"))
    settled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-settled_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["settlement_id", "entity_id"], name="payments_one_line_per_settlement_entity")
        ]

    def __str__(self):
        return f"{self.settlement_id} {self.entity_type} {self.entity_id}"


class ReceiptTemplate(models.Model):
    """The editable parts of the online-payment receipt, kept as one row (pk=1) and edited by the Main Admin.

    A blank field means "use the built-in wording" (see ``receipts.TEMPLATE_DEFAULTS``), so clearing a field restores
    the default. Amounts, payer details and payment references always come from the payment itself and are not editable.
    """

    institution_name = models.CharField(max_length=160, blank=True)
    office_name = models.CharField(max_length=160, blank=True)
    address = models.CharField(max_length=240, blank=True)
    institute_gstin = models.CharField(max_length=15, blank=True)
    contact_email = models.EmailField(max_length=120, blank=True)
    website = models.CharField(max_length=120, blank=True)
    receipt_heading = models.CharField(max_length=60, blank=True)
    service_name = models.CharField(max_length=120, blank=True)
    not_a_tax_invoice = models.TextField(max_length=400, blank=True)
    refund_note = models.TextField(max_length=400, blank=True)
    extra_note = models.TextField(max_length=400, blank=True)
    computer_generated = models.CharField(max_length=160, blank=True)
    brand_color = models.CharField(max_length=7, blank=True)
    accent_color = models.CharField(max_length=7, blank=True)
    show_gstin = models.BooleanField(default=True)
    show_logo = models.BooleanField(default=True)
    show_watermark = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        verbose_name = "Receipt template"
        verbose_name_plural = "Receipt template"

    def __str__(self):
        return "Receipt template"

    @classmethod
    def get_solo(cls) -> "ReceiptTemplate":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class ReceiptTemplateChange(models.Model):
    """Append-only record of each change to the receipt template: who, when, and each field's old and new value."""

    changed_at = models.DateTimeField(auto_now_add=True)
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    changes = models.JSONField(default=dict)

    class Meta:
        ordering = ["-changed_at", "-id"]


class ReceiptSerialCounter(models.Model):
    """Last receipt serial used in a calendar year (IST). Live and test-mode payments count separately, so test
    payments never leave gaps in the real series."""

    year = models.PositiveSmallIntegerField()
    test_mode = models.BooleanField(default=False)
    last = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["year", "test_mode"], name="payments_one_receipt_counter_per_year")]

    def __str__(self):
        return f"{self.year}{' test' if self.test_mode else ''}: {self.last}"
