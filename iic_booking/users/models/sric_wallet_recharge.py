"""SRIC wallet recharge: rows of the Wallet_Recharge.csv file that the SRIC portal (rnd.iitr.ac.in) emails."""

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

DEFAULT_SENDER = "no-reply@sric.iitr.ac.in"
DEFAULT_ATTACHMENT_NAME = "Wallet_Recharge.csv"
SRIC_PORTAL_URL = "https://rnd.iitr.ac.in"


class SricWalletRechargeSettings(models.Model):
    """Single-row configuration of the SRIC wallet recharge mailbox reader (Main Administrator)."""

    scan_enabled = models.BooleanField(
        _("Read SRIC wallet recharge emails"),
        default=False,
        help_text=_("Every 5 minutes (and on Refresh), read new Wallet_Recharge.csv emails from the SRIC portal."),
    )
    auto_credit_enabled = models.BooleanField(
        _("Auto-credit SRIC recharges"),
        default=False,
        help_text=_(
            "When on, rows that match a faculty member and a receiver, from an email whose origin is verified, "
            "are credited at once. When off, they wait for the Main Administrator to credit them."
        ),
    )
    sender_email = models.CharField(_("Sender address"), max_length=255, default=DEFAULT_SENDER)
    attachment_name = models.CharField(_("Attachment name"), max_length=255, default=DEFAULT_ATTACHMENT_NAME)
    auto_credit_max_amount = models.DecimalField(
        _("Auto-credit only up to (₹ per row)"),
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        help_text=_("Rows above this amount wait for the Main Administrator. Empty = no limit."),
    )
    trusted_authserv_ids = models.TextField(
        _("Trusted Authentication-Results servers"),
        blank=True,
        help_text=_(
            "authserv-id of our own mail servers whose Authentication-Results header is trusted (SPF / DKIM / DMARC "
            "pass for the sender domain). One per line. Empty = not used."
        ),
    )
    require_internal_relay = models.BooleanField(
        _("Require delivery through internal mail relays"),
        default=True,
        help_text=_(
            "Without a trusted Authentication-Results pass, the email counts as genuine only if every relay that "
            "handled it has a private (campus) address or one of the trusted relay ranges, and the gateway marker matches."
        ),
    )
    trusted_relay_ranges = models.TextField(
        _("Trusted public relay ranges"),
        blank=True,
        help_text=_("Extra public IP ranges (CIDR, one per line) treated as internal relays."),
    )
    gateway_marker_header = models.CharField(
        _("Gateway marker header"),
        max_length=120,
        blank=True,
        default="mail_from_trusted_domains",
        help_text=_("Header the campus mail gateway adds to mail from trusted domains. Empty = not required."),
    )
    gateway_marker_value = models.CharField(_("Gateway marker value"), max_length=120, blank=True, default="true")
    confirmation_cc_emails = models.TextField(
        _("Copy credit confirmations to"),
        blank=True,
        help_text=_("Optional addresses copied on every credit confirmation sent to the faculty member."),
    )
    review_alert_emails = models.TextField(
        _("Send 'needs review' alerts to"),
        blank=True,
        help_text=_("Empty = every active Main Administrator."),
    )
    last_scan_at = models.DateTimeField(_("Last scan at"), null=True, blank=True)
    last_scan_result = models.JSONField(_("Last scan result"), default=dict, blank=True)
    updated_at = models.DateTimeField(_("Updated at"), auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        verbose_name = _("SRIC wallet recharge settings")
        verbose_name_plural = _("SRIC wallet recharge settings")

    def __str__(self) -> str:
        return "SRIC wallet recharge settings"

    @classmethod
    def get_singleton(cls) -> "SricWalletRechargeSettings":
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class SricReceiverMapping(models.Model):
    """Receiver Project code in the SRIC file -> internal department whose sub-wallet is credited."""

    code = models.CharField(_("Receiver Project code"), max_length=80, unique=True)
    label = models.CharField(_("Receiver type"), max_length=120, help_text=_("As shown in the SRIC portal, e.g. IIC."))
    department = models.ForeignKey(
        "users.Department",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sric_receiver_mappings",
        limit_choices_to={"department_type": "internal"},
        verbose_name=_("Department sub-wallet"),
    )
    is_active = models.BooleanField(_("Active"), default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["code"]
        verbose_name = _("SRIC receiver mapping")
        verbose_name_plural = _("SRIC receiver mappings")

    def __str__(self) -> str:
        return f"{self.code} -> {self.department_id or '-'}"

    def save(self, *args, **kwargs):
        self.code = (self.code or "").strip().upper()
        super().save(*args, **kwargs)


class SricWalletMailStatus(models.TextChoices):
    PROCESSED = "processed", _("Processed")
    DRY_RUN = "dry_run", _("Dry run (nothing stored)")
    NO_ATTACHMENT = "no_attachment", _("No Wallet_Recharge.csv attachment")
    PARSE_ERROR = "parse_error", _("File could not be read")
    BEFORE_CUTOFF = "before_cutoff", _("Sent before the cutoff date")
    DUPLICATE_MESSAGE = "duplicate_message", _("Same email already processed")
    WRONG_SENDER = "wrong_sender", _("Not from the SRIC sender")
    ERROR = "error", _("Error")


class SricWalletMailMessage(models.Model):
    """One email from the SRIC portal already handled by the reader (never processed twice)."""

    folder = models.CharField(_("Folder"), max_length=120)
    uid = models.CharField(_("IMAP UID"), max_length=32)
    message_id = models.CharField(_("Message-ID"), max_length=500, blank=True, db_index=True)
    received_at = models.DateTimeField(_("Email date"), null=True, blank=True)
    from_addr = models.CharField(_("From"), max_length=255, blank=True)
    attachment_name = models.CharField(_("Attachment"), max_length=255, blank=True)
    attachment_sha256 = models.CharField(_("Attachment SHA-256"), max_length=64, blank=True, db_index=True)
    row_count = models.PositiveIntegerField(_("Rows"), default=0)
    authenticated = models.BooleanField(_("Origin verified"), default=False)
    auth_verdict = models.CharField(_("Origin check"), max_length=500, blank=True)
    status = models.CharField(_("Status"), max_length=32, choices=SricWalletMailStatus.choices)
    error = models.TextField(_("Error"), blank=True)
    trigger = models.CharField(_("Trigger"), max_length=40, blank=True)
    processed_at = models.DateTimeField(_("Processed at"), auto_now_add=True)

    class Meta:
        ordering = ["-processed_at"]
        verbose_name = _("SRIC wallet recharge email")
        verbose_name_plural = _("SRIC wallet recharge emails")
        constraints = [
            models.UniqueConstraint(fields=["folder", "uid"], name="unique_sric_wallet_mail_folder_uid"),
        ]

    def __str__(self) -> str:
        return f"{self.folder}#{self.uid} ({self.status})"


class SricWalletRechargeStatus(models.TextChoices):
    CREDITED = "credited", _("Credited")
    AWAITING_CREDIT = "awaiting_credit", _("Ready to credit (auto-credit off)")
    NEEDS_REVIEW = "needs_review", _("Needs review")
    DUPLICATE = "duplicate", _("Duplicate")
    FAILED = "failed", _("Failed")
    REJECTED = "rejected", _("Rejected")


CREDITABLE_STATUSES = (
    SricWalletRechargeStatus.AWAITING_CREDIT,
    SricWalletRechargeStatus.NEEDS_REVIEW,
    SricWalletRechargeStatus.FAILED,
)


class SricWalletRecharge(models.Model):
    """One row of a SRIC Wallet_Recharge.csv file."""

    message = models.ForeignKey(
        SricWalletMailMessage, on_delete=models.PROTECT, null=True, blank=True, related_name="rows"
    )
    row_number = models.PositiveIntegerField(_("Row in file"), default=0)
    project_number = models.CharField(_("Project Number"), max_length=120, blank=True)
    pi_name = models.CharField(_("PI Name"), max_length=255, blank=True)
    employee_id = models.CharField(_("Employee ID"), max_length=60, blank=True)
    ledger_id = models.CharField(_("Ledger ID"), max_length=80, db_index=True)
    receiver_code = models.CharField(_("Receiver Project"), max_length=80, blank=True)
    amount_raw = models.CharField(_("Amount as in file"), max_length=60, blank=True)
    amount = models.DecimalField(_("Amount"), max_digits=12, decimal_places=2, null=True, blank=True)
    financial_year = models.CharField(_("Financial year"), max_length=9, db_index=True)
    status = models.CharField(_("Status"), max_length=20, choices=SricWalletRechargeStatus.choices, db_index=True)
    review_reason = models.CharField(_("Review reason"), max_length=40, blank=True)
    review_message = models.TextField(_("Review details"), blank=True)
    origin_verified = models.BooleanField(_("Email origin verified"), default=False)
    duplicate_of = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True, related_name="duplicates"
    )
    matched_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sric_wallet_recharges",
    )
    receiver_mapping = models.ForeignKey(
        SricReceiverMapping, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    department = models.ForeignKey(
        "users.Department", on_delete=models.SET_NULL, null=True, blank=True, related_name="sric_wallet_recharges"
    )
    sub_wallet = models.ForeignKey(
        "users.SubWallet", on_delete=models.SET_NULL, null=True, blank=True, related_name="sric_recharges"
    )
    wallet_transaction = models.OneToOneField(
        "users.SubWalletTransaction",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="sric_wallet_recharge",
    )
    credit_key = models.CharField(_("Credit key"), max_length=120, null=True, blank=True, unique=True)
    balance_after = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    credited_at = models.DateTimeField(_("Credited at"), null=True, blank=True)
    credited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    confirmation_sent_at = models.DateTimeField(_("Confirmation email sent at"), null=True, blank=True)
    fund_receipt_verified = models.BooleanField(_("Fund receipt verified"), default=False, db_index=True)
    fund_receipt_verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    fund_receipt_verified_at = models.DateTimeField(_("Fund receipt verified at"), null=True, blank=True)
    fund_receipt_verification_remarks = models.TextField(_("Verification remarks"), blank=True)
    rejected_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    rejected_at = models.DateTimeField(_("Rejected at"), null=True, blank=True)
    rejection_reason = models.TextField(_("Rejection reason"), blank=True)
    history = models.JSONField(_("History"), default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = _("SRIC wallet recharge")
        verbose_name_plural = _("SRIC wallet recharges")
        constraints = [
            models.UniqueConstraint(
                fields=["ledger_id", "financial_year"],
                condition=~Q(status="duplicate"),
                name="unique_sric_wallet_recharge_ledger_fy",
            ),
        ]

    def __str__(self) -> str:
        return f"SRIC recharge #{self.pk} ({self.status})"

    @property
    def reference(self) -> str:
        return f"SWR-{self.pk:06d}" if self.pk else "SWR-—"

    @property
    def amount_value(self) -> Decimal:
        return self.amount or Decimal("0.00")
