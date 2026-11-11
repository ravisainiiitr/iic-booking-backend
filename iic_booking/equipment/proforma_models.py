"""Saved proforma invoices: every proforma the portal issues, the payments made against it, and its history.

A saved proforma is never changed after it is issued. Editing issues a revision (a new number) and marks the old one
REPLACED; cancelling marks it CANCELLED. Payments are ordinary wallet recharges (online through Razorpay, or offline
against a receipt) linked to the proforma, so the wallet, receipt, approval and reconciliation trails stay where they
are and the proforma shows how far its money has got.
"""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class ProformaStatus(models.TextChoices):
    # Stored states. Payment progress (submitted, part paid, paid, reconciled) and expiry are worked out from the
    # linked payments when the proforma is read; see proforma_store.state_of.
    ISSUED = "ISSUED", _("Issued")
    CANCELLED = "CANCELLED", _("Cancelled")
    REPLACED = "REPLACED", _("Replaced by a revision")


class ProformaAmountSource(models.TextChoices):
    SERVER = "SERVER", _("Calculated by the portal")
    CLIENT = "CLIENT", _("Sent by the browser (estimate only)")


def proforma_pdf_upload_to(instance, filename):
    return f"proforma_invoices/{instance.issued_at:%Y/%m}/{instance.number}.pdf"


class SavedProformaInvoice(models.Model):
    number = models.CharField(_("Proforma no."), max_length=40, unique=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="saved_proformas",
        help_text=_("Who asked for it; blank for an estimate made without signing in."),
    )
    department = models.ForeignKey(
        "users.Department", on_delete=models.PROTECT, null=True, blank=True, related_name="saved_proformas",
        help_text=_("The department whose equipment it is for; payments recharge this department's sub-wallet."),
    )
    issued_at = models.DateTimeField()
    valid_until = models.DateField()
    # Snapshots as printed, so the record never changes when profiles, tariffs or templates do.
    buyer = models.JSONField(default=dict)
    supplier = models.JSONField(default=dict)
    lines = models.JSONField(default=list)
    # What was asked for (equipment and inputs), so the user can edit it into a revision.
    request_items = models.JSONField(default=list, blank=True)
    taxable_total = models.DecimalField(max_digits=12, decimal_places=2)
    gst_total = models.DecimalField(max_digits=12, decimal_places=2)
    total = models.DecimalField(max_digits=12, decimal_places=2)
    place_of_supply = models.CharField(max_length=2, blank=True)
    inter_state = models.BooleanField(default=False)
    amount_source = models.CharField(max_length=8, choices=ProformaAmountSource.choices, default=ProformaAmountSource.SERVER)
    status = models.CharField(max_length=12, choices=ProformaStatus.choices, default=ProformaStatus.ISSUED, db_index=True)
    revision = models.PositiveSmallIntegerField(default=0)
    replaces = models.OneToOneField(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="replaced_by",
        help_text=_("The proforma this revision replaced."),
    )
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+",
    )
    cancel_reason = models.CharField(max_length=300, blank=True)
    pdf = models.FileField(upload_to=proforma_pdf_upload_to, max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-issued_at", "-id"]
        indexes = [models.Index(fields=["user", "-issued_at"]), models.Index(fields=["department", "-issued_at"])]
        verbose_name = _("Saved proforma invoice")
        verbose_name_plural = _("Saved proforma invoices")

    def __str__(self) -> str:
        return self.number


class ProformaPaymentKind(models.TextChoices):
    ONLINE = "ONLINE", _("Online (Razorpay)")
    OFFLINE = "OFFLINE", _("Offline, against a receipt")


class ProformaPayment(models.Model):
    """One payment made against a proforma: a Razorpay recharge order or an offline-receipt recharge request."""

    proforma = models.ForeignKey(SavedProformaInvoice, on_delete=models.PROTECT, related_name="payments")
    kind = models.CharField(max_length=8, choices=ProformaPaymentKind.choices)
    payment_order = models.OneToOneField(
        "payments.PaymentOrder", on_delete=models.PROTECT, null=True, blank=True, related_name="proforma_payment",
    )
    recharge_request = models.OneToOneField(
        "users.WalletRechargeRequest", on_delete=models.PROTECT, null=True, blank=True, related_name="proforma_payment",
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]
        constraints = [
            models.CheckConstraint(
                condition=(models.Q(payment_order__isnull=False, recharge_request__isnull=True)
                           | models.Q(payment_order__isnull=True, recharge_request__isnull=False)),
                name="proforma_payment_one_source",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.proforma.number} {self.kind} {self.amount}"


class ProformaEvent(models.Model):
    """Append-only history of a proforma: issued, revised, cancelled, payments started or submitted."""

    proforma = models.ForeignKey(SavedProformaInvoice, on_delete=models.PROTECT, related_name="events")
    at = models.DateTimeField(auto_now_add=True)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    action = models.CharField(max_length=32)
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["at", "id"]

    def save(self, *args, **kwargs):
        if self.pk:
            raise RuntimeError("Proforma history is append-only.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise RuntimeError("Proforma history is append-only.")
