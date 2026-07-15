"""Admin toggle: allow IITR Students to recharge the faculty shared wallet."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class WalletStudentRechargeSettings(models.Model):
    """
    Single-row settings (pk=1). When enabled, IITR Students (shared faculty wallet)
    may use SBIePay and Offline payment-receipt recharge. Credits go to the faculty wallet.
    Individual students are unaffected (they already own a wallet).
    """

    enable_iitr_student_wallet_recharge = models.BooleanField(
        _("Enable IITR Student wallet recharge"),
        default=False,
        help_text=_(
            "When on, IITR Students may recharge via SBIePay or Offline Request "
            "(payment receipt upload). Funds park in the concerned faculty wallet. "
            "Individual Students keep their own wallet and are not gated by this flag."
        ),
    )

    class Meta:
        db_table = "users_walletstudentrechargesettings"
        verbose_name = _("Wallet student recharge settings")
        verbose_name_plural = _("Wallet student recharge settings")

    def __str__(self) -> str:
        return "Wallet student recharge settings"

    @classmethod
    def get_singleton(cls) -> "WalletStudentRechargeSettings":
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={"enable_iitr_student_wallet_recharge": False},
        )
        return obj
