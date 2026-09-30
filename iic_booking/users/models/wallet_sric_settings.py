"""Singleton settings: SRIC Office recipient emails for faculty wallet recharge notifications."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class WalletSricSettings(models.Model):
    """
    Single-row configuration for SRIC Office email recipients.
    Admins edit this in Django admin; faculty wallet recharge flow uses it when sending to SRIC.
    """

    recipient_emails = models.TextField(
        _("SRIC Office email addresses"),
        blank=True,
        help_text=_(
            "One address per line, or comma/semicolon separated. "
            "Used when a faculty member sends a Project Grant wallet recharge request to the SRIC Office."
        ),
    )
    bill_section_emails = models.TextField(
        _("SRIC Bill Section email addresses"),
        blank=True,
        help_text=_(
            "One address per line, or comma/semicolon separated. "
            "Used for Direct Cash Deposit / Bank Transfer wallet recharge requests. "
            "Configurable by Main Administrator and Department Administrator."
        ),
    )
    project_grant_recharge_enabled = models.BooleanField(
        _("Allow wallet recharge requests via Project Grant"),
        default=False,
        db_default=False,
        help_text=_(
            "When off, faculty cannot raise new Project Grant recharge requests (or send unsent ones to the "
            "SRIC Office). Direct Cash Deposit / Bank Transfer is unaffected."
        ),
    )
    project_grant_cc_emails = models.TextField(
        _("Project Grant recharge CC email addresses"),
        blank=True,
        help_text=_(
            "Additional addresses copied on Project Grant recharge requests. "
            "The requesting user is always copied. CC recipients get the request details "
            "without Approve / Decline links."
        ),
    )
    cash_deposit_cc_emails = models.TextField(
        _("Direct Cash / Bank Transfer recharge CC email addresses"),
        blank=True,
        help_text=_(
            "Additional addresses copied on Direct Cash Deposit / Bank Transfer recharge requests. "
            "The requesting user is always copied. CC recipients get the request details "
            "without Approve / Decline links."
        ),
    )
    grant_code_for_credit = models.CharField(
        _("Default grant code (fallback)"),
        max_length=80,
        default="IIC-000-002",
        help_text=_(
            "Used in the SRIC Office recharge email only when the selected internal department "
            "has no grant code of its own. Prefer setting codes per department below / on this page."
        ),
    )
    ar_sric_emails = models.TextField(
        _("AR SRIC email addresses"),
        blank=True,
        help_text=_(
            "Copied (without Approve / Decline links) on every Project Grant and Direct Cash Deposit "
            "recharge request and on its final decision."
        ),
    )
    dean_sric_emails = models.TextField(
        _("Dean SRIC email addresses"),
        blank=True,
        help_text=_(
            "Copied (without Approve / Decline links) on every Project Grant recharge request "
            "and on its final decision."
        ),
    )
    decline_converts_to_credit = models.BooleanField(
        _("Treat SRIC-declined Project Grant requests as auto-approved credit"),
        default=True,
        help_text=_(
            "When SRIC declines a Project Grant request (before or after approval), the request is "
            "cancelled and the amount is treated as an auto-approved credit facility, recovered from "
            "the faculty member's next approved recharge for the same department."
        ),
    )
    auto_read_cashbook_mailbox = models.BooleanField(
        _("Read the SRIC cash-book mailbox automatically"),
        default=True,
        help_text=_(
            "Every 30 minutes, read new cash-book emails from the configured senders (IMAP_* server "
            "settings) and mark matching recharge requests as fund-received."
        ),
    )
    cashbook_sender_emails = models.TextField(
        _("SRIC cash-book sender addresses"),
        blank=True,
        default="bills@sric.iitr.ac.in",
        help_text=_("Only emails from these senders are read by the automatic cash-book reader."),
    )
    fund_receipt_overdue_days = models.PositiveSmallIntegerField(
        _("Flag approved requests without a cash-book match after (days)"),
        default=15,
        help_text=_(
            "Approved recharge requests with no matching SRIC cash-book entry after this many days are "
            "shown to the Main Administrator and Account In-charge every time they open the dashboard."
        ),
    )

    class Meta:
        db_table = "users_walletsricsettings"
        verbose_name = _("Wallet SRIC office notification settings")
        verbose_name_plural = _("Wallet SRIC office notification settings")

    def __str__(self) -> str:
        return "Wallet SRIC office emails"

    @classmethod
    def get_singleton(cls) -> "WalletSricSettings":
        obj, created = cls.objects.get_or_create(
            pk=1,
            defaults={
                "recipient_emails": "",
                "bill_section_emails": "ravisaini.15@gmail.com",
                "grant_code_for_credit": "IIC-000-002",
            },
        )
        # Seed Bill Section routing email when empty (editable by Main Admin in UI).
        if not created and not (obj.bill_section_emails or "").strip():
            obj.bill_section_emails = "ravisaini.15@gmail.com"
            obj.save(update_fields=["bill_section_emails"])
        return obj


def project_grant_recharge_enabled() -> bool:
    return bool(WalletSricSettings.get_singleton().project_grant_recharge_enabled)


class WalletCashbookMailboxMessage(models.Model):
    """One SRIC cash-book email already handled by the automatic mailbox reader."""

    folder = models.CharField(_("Folder"), max_length=120)
    uid = models.CharField(_("IMAP UID"), max_length=32)
    subject = models.CharField(_("Subject"), max_length=500, blank=True)
    from_addr = models.CharField(_("From"), max_length=255, blank=True)
    attachment_name = models.CharField(_("Attachment"), max_length=255, blank=True)
    rows_parsed = models.PositiveIntegerField(_("Rows parsed"), default=0)
    rows_stored = models.PositiveIntegerField(_("Rows stored"), default=0)
    error = models.TextField(_("Error"), blank=True)
    processed_at = models.DateTimeField(_("Processed at"), auto_now_add=True)

    class Meta:
        verbose_name = _("SRIC cash-book mailbox message")
        verbose_name_plural = _("SRIC cash-book mailbox messages")
        ordering = ["-processed_at"]
        constraints = [
            models.UniqueConstraint(fields=["folder", "uid"], name="unique_cashbook_mailbox_folder_uid"),
        ]

    def __str__(self) -> str:
        return f"{self.folder}#{self.uid} ({self.rows_stored} rows)"
