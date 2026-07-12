from django.db import models
from django.utils.translation import gettext_lazy as _

from .user import User


class ExternalBillingProfile(models.Model):
    """
    Billing + shipping details for external users (for invoices/labels).

    Kept in a separate model so we don't bloat the core User table.
    """

    user = models.OneToOneField(
        User,
        on_delete=models.CASCADE,
        related_name="external_billing_profile",
        verbose_name=_("User"),
    )

    # Billing identity
    billing_name = models.CharField(
        _("Billing name / company"),
        max_length=255,
        blank=True,
        help_text=_("Legal entity / institute / company name to appear on invoice."),
    )
    gstin = models.CharField(
        _("GSTIN"),
        max_length=30,
        blank=True,
        help_text=_("GSTIN (if applicable)."),
    )

    # Billing address
    billing_address_line1 = models.CharField(_("Billing address line 1"), max_length=255, blank=True)
    billing_address_line2 = models.CharField(_("Billing address line 2"), max_length=255, blank=True)
    billing_city = models.CharField(_("Billing city"), max_length=120, blank=True)
    billing_state = models.CharField(_("Billing state"), max_length=120, blank=True)
    billing_pincode = models.CharField(_("Billing pincode"), max_length=20, blank=True)
    billing_country = models.CharField(_("Billing country"), max_length=120, blank=True, default="India")

    # Shipping address (optional; can be same as billing)
    shipping_same_as_billing = models.BooleanField(_("Shipping same as billing"), default=True)
    shipping_name = models.CharField(_("Shipping name / contact"), max_length=255, blank=True)
    shipping_phone = models.CharField(_("Shipping phone"), max_length=30, blank=True)
    shipping_address_line1 = models.CharField(_("Shipping address line 1"), max_length=255, blank=True)
    shipping_address_line2 = models.CharField(_("Shipping address line 2"), max_length=255, blank=True)
    shipping_city = models.CharField(_("Shipping city"), max_length=120, blank=True)
    shipping_state = models.CharField(_("Shipping state"), max_length=120, blank=True)
    shipping_pincode = models.CharField(_("Shipping pincode"), max_length=20, blank=True)
    shipping_country = models.CharField(_("Shipping country"), max_length=120, blank=True, default="India")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("External billing profile")
        verbose_name_plural = _("External billing profiles")

    def __str__(self) -> str:
        return f"ExternalBillingProfile(user={self.user_id})"

