"""Per-department wallet payment options, email recipients and direct wallet recharge.

The global switches on ``WalletSricSettings`` / ``WalletCreditPolicy`` stay the master control.
Every table here is new, so code reading them must tolerate the tables not existing yet
(see ``iic_booking.users.wallet_payment_modes``).
"""

from __future__ import annotations

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _


class WalletModeOption(models.TextChoices):
    PROJECT_GRANT = "project_grant", _("Recharge via Project Grant")
    DIRECT_CASH = "direct_cash", _("Direct Cash Deposit / Bank Transfer")
    ONLINE_GATEWAY = "online_gateway", _("Online payment gateway")
    PEER_TRANSFER = "peer_transfer", _("Transfer within the same department")
    CREDIT = "credit", _("Credit Limit")
    DIRECT_RECHARGE = "direct_recharge", _("Direct wallet recharge")


class DepartmentModeState(models.TextChoices):
    INHERIT = "inherit", _("Follows master")
    DISABLED = "disabled", _("Disabled")


class WalletPaymentModeConfig(models.Model):
    """Singleton for switches that have no older home (direct wallet recharge)."""

    direct_recharge_enabled = models.BooleanField(
        _("Allow direct wallet recharge"),
        default=False,
        help_text=_(
            "When on, the Main Administrator and currently valid designated persons can add funds "
            "directly to a selected wallet."
        ),
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        verbose_name = _("Wallet payment mode configuration")
        verbose_name_plural = _("Wallet payment mode configuration")

    def __str__(self) -> str:
        return "Wallet payment mode configuration"


class WalletModeDepartmentSetting(models.Model):
    """Department-level state for each option. Missing row = every option follows the master."""

    department = models.OneToOneField(
        "users.Department", on_delete=models.CASCADE, related_name="wallet_mode_setting"
    )
    project_grant = models.CharField(
        max_length=16, choices=DepartmentModeState.choices, default=DepartmentModeState.INHERIT
    )
    direct_cash = models.CharField(
        max_length=16, choices=DepartmentModeState.choices, default=DepartmentModeState.INHERIT
    )
    online_gateway = models.CharField(
        max_length=16, choices=DepartmentModeState.choices, default=DepartmentModeState.INHERIT
    )
    peer_transfer = models.CharField(
        max_length=16, choices=DepartmentModeState.choices, default=DepartmentModeState.INHERIT
    )
    direct_recharge = models.CharField(
        max_length=16, choices=DepartmentModeState.choices, default=DepartmentModeState.INHERIT
    )
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        verbose_name = _("Wallet payment mode (department)")
        verbose_name_plural = _("Wallet payment modes (department)")

    def __str__(self) -> str:
        return f"Wallet modes for {self.department_id}"


class WalletModeEmailRecipients(models.Model):
    """To / CC recipients for one option; ``department`` NULL is the default for all departments.

    Entries are email addresses or ``role:<key>`` tokens resolved when the email is sent.
    """

    option = models.CharField(max_length=32, choices=WalletModeOption.choices)
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="wallet_mode_email_recipients",
    )
    to_recipients = models.JSONField(default=list, blank=True)
    cc_recipients = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        verbose_name = _("Wallet payment mode email recipients")
        verbose_name_plural = _("Wallet payment mode email recipients")
        constraints = [
            models.UniqueConstraint(
                fields=["option", "department"],
                condition=Q(department__isnull=False),
                name="uniq_wallet_mode_recipients_option_dept",
            ),
            models.UniqueConstraint(
                fields=["option"],
                condition=Q(department__isnull=True),
                name="uniq_wallet_mode_recipients_option_default",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.option} recipients ({self.department_id or 'default'})"


class WalletDirectRechargeGrant(models.Model):
    """Temporary permission for a designated person to perform direct wallet recharges."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="wallet_direct_recharge_grants"
    )
    valid_from = models.DateTimeField()
    valid_until = models.DateTimeField()
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="wallet_direct_recharge_grants",
        help_text=_("Leave empty to allow every department."),
    )
    max_amount_per_transaction = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    reason = models.TextField()
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    revoke_reason = models.TextField(blank=True)

    class Meta:
        verbose_name = _("Direct wallet recharge grant")
        verbose_name_plural = _("Direct wallet recharge grants")
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "valid_until"], name="wallet_drg_user_until_idx")]

    def __str__(self) -> str:
        return f"Direct recharge grant #{self.pk} for {self.user_id}"


class WalletDirectRechargeMode(models.TextChoices):
    CASH = "cash", _("Cash")
    BANK_TRANSFER = "bank_transfer", _("Bank transfer")
    CHEQUE = "cheque", _("Cheque")
    DEMAND_DRAFT = "dd", _("Demand draft")
    INTERNAL_ADJUSTMENT = "internal_adjustment", _("Internal adjustment")
    OTHER = "other", _("Other")


class WalletDirectRecharge(models.Model):
    """One direct wallet recharge. The ledger entry is the linked sub-wallet transaction."""

    reference = models.CharField(max_length=32, blank=True, db_index=True)
    client_request_id = models.CharField(max_length=64, unique=True)
    wallet = models.ForeignKey("users.Wallet", on_delete=models.PROTECT, related_name="direct_recharges")
    sub_wallet = models.ForeignKey("users.SubWallet", on_delete=models.PROTECT, related_name="direct_recharges")
    department = models.ForeignKey("users.Department", on_delete=models.PROTECT, related_name="+")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    mode = models.CharField(max_length=32, choices=WalletDirectRechargeMode.choices)
    reference_number = models.CharField(max_length=120, blank=True)
    transaction_date = models.DateField()
    remarks = models.TextField()
    attachment = models.FileField(upload_to="wallet_direct_recharge/%Y/%m/%d/", blank=True, null=True, max_length=255)
    balance_before = models.DecimalField(max_digits=12, decimal_places=2)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2)
    sub_wallet_transaction = models.OneToOneField(
        "users.SubWalletTransaction", on_delete=models.PROTECT, related_name="direct_recharge"
    )
    performed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+")
    performed_as = models.CharField(max_length=24)
    grant = models.ForeignKey(
        WalletDirectRechargeGrant, on_delete=models.PROTECT, null=True, blank=True, related_name="recharges"
    )
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    email_to = models.JSONField(default=list, blank=True)
    email_cc = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Direct wallet recharge")
        verbose_name_plural = _("Direct wallet recharges")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.reference or self.pk} ₹{self.amount}"


class WalletPaymentModeAuditEvent(models.Model):
    """Who changed which wallet payment mode setting, and every direct recharge / grant action."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    action = models.CharField(max_length=64)
    target = models.CharField(max_length=120, blank=True)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("Wallet payment mode audit event")
        verbose_name_plural = _("Wallet payment mode audit events")
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.action} {self.target}"
