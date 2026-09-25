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
