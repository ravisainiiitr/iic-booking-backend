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
            "Used when a faculty member sends a wallet recharge request to the SRIC Office."
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
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={"recipient_emails": "", "grant_code_for_credit": "IIC-000-002"},
        )
        return obj
