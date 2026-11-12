"""External users: wallet recharge against an offline payment (NEFT/RTGS/IMPS/UPI/DD/cheque) with a receipt.

The request itself is a ``WalletRechargeRequest`` (mode ``offline_payment``) so approval, locking, the wallet credit
and the audit trail reuse the existing recharge workflow. This row holds what the payer declared, the receipt, the
single wallet transaction the approval created, and the reconciliation against the funds actually received.
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q, UniqueConstraint
from django.utils.translation import gettext_lazy as _


class OfflinePaymentMode(models.TextChoices):
    NEFT = "NEFT", _("NEFT")
    RTGS = "RTGS", _("RTGS")
    IMPS = "IMPS", _("IMPS")
    UPI = "UPI", _("UPI")
    DD = "DD", _("Demand draft")
    CHEQUE = "CHEQUE", _("Cheque")
    OTHER = "OTHER", _("Other")


class OfflineReconciliationStatus(models.TextChoices):
    NOT_STARTED = "NOT_STARTED", _("Not started")  # still pending approval, or rejected
    AWAITING = "AWAITING", _("Awaiting reconciliation")
    RECONCILED = "RECONCILED", _("Reconciled")
    MISMATCH = "MISMATCH", _("Mismatch flagged")


class OfflineRechargePayment(models.Model):
    recharge_request = models.OneToOneField(
        "users.WalletRechargeRequest",
        on_delete=models.CASCADE,
        related_name="offline_payment",
    )
    client_request_id = models.CharField(
        _("Client request id"),
        max_length=64,
        blank=True,
        help_text=_("Idempotency key sent by the browser or app, so a repeated submit never creates a second request."),
    )
    claimed_amount = models.DecimalField(_("Amount paid (declared)"), max_digits=10, decimal_places=2)
    payment_date = models.DateField(_("Date of payment"))
    payment_mode = models.CharField(_("Mode"), max_length=16, choices=OfflinePaymentMode.choices)
    payment_mode_other = models.CharField(_("Mode (other)"), max_length=60, blank=True)
    transaction_reference = models.CharField(_("UTR / transaction reference"), max_length=64)
    payer_bank = models.CharField(_("Payer bank"), max_length=120)
    payer_account_name = models.CharField(_("Payer account name"), max_length=160)
    paid_into = models.CharField(
        _("Paid into (institute account or grant)"), max_length=160, blank=True
    )
    remarks = models.TextField(_("Remarks"), blank=True)
    receipt = models.OneToOneField(
        "users.DepartmentPaymentReceipt",
        on_delete=models.PROTECT,
        related_name="offline_recharge_payment",
        help_text=_("Uploaded transaction receipt (image or PDF)."),
    )
    wallet_transaction = models.OneToOneField(
        "users.SubWalletTransaction",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="offline_recharge_payment",
        help_text=_("The one wallet credit made on approval."),
    )
    reconciliation_status = models.CharField(
        max_length=16,
        choices=OfflineReconciliationStatus.choices,
        default=OfflineReconciliationStatus.NOT_STARTED,
        db_index=True,
    )
    reconciled_reference = models.CharField(_("Bank / grant credit reference"), max_length=100, blank=True)
    reconciled_credit_date = models.DateField(_("Date funds were credited"), null=True, blank=True)
    reconciliation_note = models.TextField(blank=True)
    reconciled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="offline_recharge_reconciliations",
    )
    reconciled_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Offline payment recharge")
        verbose_name_plural = _("Offline payment recharges")
        ordering = ["-created_at"]
        constraints = [
            UniqueConstraint(
                fields=["client_request_id"],
                condition=~Q(client_request_id=""),
                name="unique_offline_recharge_client_request_id",
            ),
        ]

    def __str__(self) -> str:
        return f"Offline recharge {self.transaction_reference} ₹{self.claimed_amount}"
