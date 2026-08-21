"""Administrator-controlled Wallet Credit Facility (request → approve → ledger credit).

This is separate from the retired recharge temporary-credit overdraft and from the
department faculty one-time overdraft. Credit is posted as a real SubWallet CREDIT
transaction; outstanding is tracked on the facility and repaid via ledger entries.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _


class WalletCreditFacilityStatus(models.TextChoices):
    DRAFT = "DRAFT", _("Draft")
    SUBMITTED = "SUBMITTED", _("Submitted")
    UNDER_REVIEW = "UNDER_REVIEW", _("Under review")
    APPROVED = "APPROVED", _("Approved")
    CREDITED = "CREDITED", _("Credited")
    PARTIALLY_SETTLED = "PARTIALLY_SETTLED", _("Partially settled")
    CLEARED = "CLEARED", _("Cleared")
    REJECTED = "REJECTED", _("Rejected")
    CANCELLED = "CANCELLED", _("Cancelled")
    EXPIRED = "EXPIRED", _("Expired")
    WRITTEN_OFF = "WRITTEN_OFF", _("Written off")
    CLARIFICATION = "CLARIFICATION", _("Returned for clarification")


ACTIVE_CREDIT_BLOCKING_STATUSES = frozenset(
    {
        WalletCreditFacilityStatus.SUBMITTED,
        WalletCreditFacilityStatus.UNDER_REVIEW,
        WalletCreditFacilityStatus.APPROVED,
        WalletCreditFacilityStatus.CREDITED,
        WalletCreditFacilityStatus.PARTIALLY_SETTLED,
        WalletCreditFacilityStatus.CLARIFICATION,
    }
)


class WalletCreditLedgerKind(models.TextChoices):
    WALLET_CREDIT = "WALLET_CREDIT", _("Wallet credit posted")
    CREDIT_REPAYMENT = "CREDIT_REPAYMENT", _("Credit repayment")
    CREDIT_ADJUSTMENT = "CREDIT_ADJUSTMENT", _("Credit adjustment")
    CREDIT_REVERSAL = "CREDIT_REVERSAL", _("Credit reversal")


class WalletCreditInvoiceStatus(models.TextChoices):
    DRAFT = "DRAFT", _("Draft")
    ISSUED = "ISSUED", _("Issued")
    PARTIALLY_PAID = "PARTIALLY_PAID", _("Partially paid")
    PAID = "PAID", _("Paid")
    OVERDUE = "OVERDUE", _("Overdue")
    CANCELLED = "CANCELLED", _("Cancelled")


class WalletCreditPolicy(models.Model):
    """Global credit policy singleton. Amounts are not hardcoded to ₹1000."""

    singleton_key = models.CharField(max_length=16, unique=True, default="default")
    enabled = models.BooleanField(
        default=False,
        help_text=_("Master switch for the admin-approved wallet credit facility."),
    )
    max_credit_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("50000.00"))
    max_outstanding_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("50000.00"))
    min_request_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("100.00"))
    max_credit_duration_days = models.PositiveIntegerField(default=30)
    reminder_days_before_due = models.PositiveIntegerField(default=3)
    overdue_reminder_interval_days = models.PositiveIntegerField(default=7)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Wallet credit policy")
        verbose_name_plural = _("Wallet credit policy")

    def __str__(self) -> str:
        return f"WalletCreditPolicy(enabled={self.enabled})"

    @classmethod
    def get_solo(cls) -> "WalletCreditPolicy":
        obj, _ = cls.objects.get_or_create(singleton_key="default")
        return obj


class WalletCreditFacility(models.Model):
    """User-requested temporary credit reviewed by Main Administrator."""

    public_reference = models.CharField(max_length=32, unique=True, db_index=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="wallet_credit_facilities",
    )
    department = models.ForeignKey(
        "users.Department",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="wallet_credit_facilities",
    )
    sub_wallet = models.ForeignKey(
        "users.SubWallet",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="wallet_credit_facilities",
    )
    requested_amount = models.DecimalField(max_digits=12, decimal_places=2)
    approved_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    outstanding_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    purpose = models.TextField()
    remarks = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=32,
        choices=WalletCreditFacilityStatus.choices,
        default=WalletCreditFacilityStatus.DRAFT,
        db_index=True,
    )
    requested_at = models.DateTimeField(auto_now_add=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    approved_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="wallet_credits_approved",
    )
    approval_reason = models.TextField(blank=True, default="")
    rejected_at = models.DateTimeField(null=True, blank=True)
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="wallet_credits_rejected",
    )
    rejection_reason = models.TextField(blank=True, default="")
    due_date = models.DateField(null=True, blank=True)
    credited_at = models.DateTimeField(null=True, blank=True)
    cleared_at = models.DateTimeField(null=True, blank=True)
    credit_posting_key = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_("Idempotency key for credit posting."),
    )
    profile_snapshot = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Audit snapshot of Channel-I/portal profile at submit/approve."),
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Wallet credit facility")
        verbose_name_plural = _("Wallet credit facilities")
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "user"]),
            models.Index(fields=["due_date", "status"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=Q(requested_amount__gt=0),
                name="wallet_credit_requested_amount_gt_0",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.public_reference} ({self.status})"


class WalletCreditLedgerEntry(models.Model):
    """Independent credit-facility ledger row. Never rewrite amounts after create."""

    facility = models.ForeignKey(
        WalletCreditFacility,
        on_delete=models.PROTECT,
        related_name="ledger_entries",
    )
    kind = models.CharField(max_length=32, choices=WalletCreditLedgerKind.choices)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reference = models.CharField(max_length=64, unique=True)
    description = models.TextField(blank=True, default="")
    subwallet_transaction = models.ForeignKey(
        "users.SubWalletTransaction",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="wallet_credit_ledger_entries",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="wallet_credit_ledger_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Wallet credit ledger entry")
        verbose_name_plural = _("Wallet credit ledger entries")
        ordering = ["-created_at"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("Wallet credit ledger entries are immutable")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Wallet credit ledger entries cannot be deleted")


class WalletCreditInvoice(models.Model):
    facility = models.ForeignKey(
        WalletCreditFacility,
        on_delete=models.PROTECT,
        related_name="invoices",
    )
    invoice_number = models.CharField(max_length=40, unique=True)
    status = models.CharField(
        max_length=24,
        choices=WalletCreditInvoiceStatus.choices,
        default=WalletCreditInvoiceStatus.DRAFT,
    )
    issue_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)
    approved_credit = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    amount_settled = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    outstanding_amount = models.DecimalField(max_digits=12, decimal_places=2, default=Decimal("0.00"))
    payment_instructions = models.TextField(blank=True, default="")
    terms = models.TextField(blank=True, default="")
    issued_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="wallet_credit_invoices_issued",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Wallet credit invoice")
        verbose_name_plural = _("Wallet credit invoices")
        ordering = ["-created_at"]


class WalletCreditPayment(models.Model):
    facility = models.ForeignKey(
        WalletCreditFacility,
        on_delete=models.PROTECT,
        related_name="payments",
    )
    invoice = models.ForeignKey(
        WalletCreditInvoice,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="payments",
    )
    ledger_entry = models.ForeignKey(
        WalletCreditLedgerEntry,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="payments",
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_date = models.DateField()
    mode = models.CharField(max_length=64, blank=True, default="")
    utr_or_reference = models.CharField(max_length=128, blank=True, default="")
    remarks = models.TextField(blank=True, default="")
    receipt_number = models.CharField(max_length=40, unique=True)
    recorded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="wallet_credit_payments_recorded",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Wallet credit payment")
        verbose_name_plural = _("Wallet credit payments")
        ordering = ["-created_at"]


class WalletCreditAuditEvent(models.Model):
    facility = models.ForeignKey(
        WalletCreditFacility,
        on_delete=models.CASCADE,
        related_name="audit_events",
    )
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="wallet_credit_audit_events",
    )
    action = models.CharField(max_length=64)
    previous_value = models.TextField(blank=True, default="")
    new_value = models.TextField(blank=True, default="")
    reason = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Wallet credit audit event")
        verbose_name_plural = _("Wallet credit audit events")
        ordering = ["created_at"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise ValueError("Wallet credit audit events are immutable")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Wallet credit audit events cannot be deleted")
