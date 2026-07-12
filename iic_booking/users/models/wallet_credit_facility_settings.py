"""Admin-editable defaults for faculty wallet recharge credit facility (temporary overdraft)."""

from django.db import models
from django.utils.translation import gettext_lazy as _


class WalletCreditFacilitySettings(models.Model):
    """
    Single-row settings (pk=1). Faculty may opt into a short credit line when raising a recharge
    request while the department sub-wallet balance is below the threshold.
    """

    balance_threshold_inr = models.DecimalField(
        _("Balance threshold (₹)"),
        max_digits=10,
        decimal_places=2,
        default=1000,
        help_text=_(
            "If the selected department sub-wallet balance is below this amount when raising a "
            "recharge request, the faculty may be offered the credit facility popup."
        ),
    )
    credit_window_days = models.PositiveSmallIntegerField(
        _("Credit window (days)"),
        default=7,
        help_text=_(
            "Parse confirmation is expected within this many days from OTP verification. "
            "If not credited via parse in time, bookings for that department are blocked."
        ),
    )
    max_credit_inr = models.DecimalField(
        _("Maximum credit line (₹)"),
        max_digits=10,
        decimal_places=2,
        default=1000,
        help_text=_(
            "Upper cap for the temporary credit line. Actual line is min(this, requested recharge amount)."
        ),
    )

    class Meta:
        db_table = "users_walletcreditfacilitysettings"
        verbose_name = _("Wallet credit facility settings")
        verbose_name_plural = _("Wallet credit facility settings")

    def __str__(self) -> str:
        return "Wallet credit facility settings"

    @classmethod
    def get_singleton(cls) -> "WalletCreditFacilitySettings":
        obj, _ = cls.objects.get_or_create(
            pk=1,
            defaults={
                "balance_threshold_inr": 1000,
                "credit_window_days": 7,
                "max_credit_inr": 1000,
            },
        )
        return obj
