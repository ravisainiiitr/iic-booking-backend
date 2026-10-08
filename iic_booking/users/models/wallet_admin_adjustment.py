"""Manual credit / debit of a sub-wallet by the Main Administrator (Wallet ledger)."""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class WalletAdminAdjustmentDirection(models.TextChoices):
    CREDIT = "credit", _("Credit")
    DEBIT = "debit", _("Debit")


class WalletAdminAdjustmentReason(models.TextChoices):
    MANUAL_ADJUSTMENT = "manual_adjustment", _("Manual adjustment")
    CORRECTION = "correction", _("Correction")
    REFUND_OUTSIDE_SYSTEM = "refund_outside_system", _("Refund outside system")
    GRANT_TOP_UP = "grant_top_up", _("Grant top-up")
    OTHER = "other", _("Other")


class WalletAdminAdjustment(models.Model):
    """One manual credit or debit. The ledger entry is the linked sub-wallet transaction."""

    reference = models.CharField(max_length=32, blank=True, db_index=True)
    client_request_id = models.CharField(max_length=64, unique=True)
    direction = models.CharField(max_length=8, choices=WalletAdminAdjustmentDirection.choices)
    wallet = models.ForeignKey("users.Wallet", on_delete=models.PROTECT, related_name="admin_adjustments")
    sub_wallet = models.ForeignKey("users.SubWallet", on_delete=models.PROTECT, related_name="admin_adjustments")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    reason = models.CharField(max_length=32, choices=WalletAdminAdjustmentReason.choices)
    remarks = models.TextField()
    external_reference = models.CharField(max_length=120, blank=True)
    balance_before = models.DecimalField(max_digits=12, decimal_places=2)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2)
    sub_wallet_transaction = models.OneToOneField(
        "users.SubWalletTransaction", on_delete=models.PROTECT, related_name="admin_adjustment"
    )
    performed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    notify_owner = models.BooleanField(default=True)
    email_sent_at = models.DateTimeField(null=True, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Wallet admin adjustment")
        verbose_name_plural = _("Wallet admin adjustments")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.reference or self.pk} {self.direction} ₹{self.amount}"
