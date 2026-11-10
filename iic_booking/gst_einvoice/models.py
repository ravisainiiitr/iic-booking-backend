"""GST e-invoice sandbox demonstration: customers, customer purchase orders, invoices, approvals and IRP attempts.

Every record here belongs to the sandbox demonstration (``is_demo`` / ``mode``). Nothing is linked to the payment,
wallet or receipt tables in a way that changes them: a booking or payment receipt is only *referenced*.
Issued invoices are immutable apart from the status transitions in ``service``; history is append-only.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from iic_booking.procurement_management.models import AppendOnlyModel

USER = settings.AUTH_USER_MODEL
ZERO = Decimal("0.00")


class EinvoiceSettings(models.Model):
    """Singleton: supplier details and defaults shown on demonstration invoices. Edited by administrators only; the
    mode (sandbox) and provider are environment settings and cannot be changed here."""

    supplier_legal_name = models.CharField(max_length=200, default="Sponsored Research & Industrial Consultancy, Indian Institute of Technology Roorkee")
    supplier_trade_name = models.CharField(max_length=200, default="SRIC, IIT Roorkee")
    supplier_gstin = models.CharField(max_length=15, default="05AAALI0033R1Z5")
    supplier_pan = models.CharField(max_length=10, default="AAALI0033R")
    supplier_address1 = models.CharField(max_length=100, default="Indian Institute of Technology Roorkee")
    supplier_address2 = models.CharField(max_length=100, blank=True, default="")
    supplier_location = models.CharField(max_length=100, default="Roorkee")
    supplier_pincode = models.CharField(max_length=6, default="247667")
    supplier_state_code = models.CharField(max_length=2, default="05")
    supplier_email = models.EmailField(default="arsric@iitr.ac.in")
    bank_details = models.TextField(default=(
        "A/c Holder's Name: Conference, Seminar, Symposium & Wshop\nBank Name: State Bank of India\n"
        "A/c No.: 33136732957\nBranch & IFS Code: IIT Roorkee & SBIN0001069\nSWIFT Code: SBININBB559"))
    terms = models.TextField(default=(
        "1. Charges are for testing / analysis services rendered by the facility named above.\n"
        "2. Please quote the invoice number in all correspondence.\n"
        "3. Subject to Roorkee jurisdiction."))
    approver_emails = models.TextField(
        blank=True, default="",
        help_text=_("Extra accounts (one email per line) allowed to approve demonstration invoices, e.g. a demo SRIC "
                    "account. Accounts In Charge and the Main Administrator can always approve."))
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get(cls) -> "EinvoiceSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class TaxRule(models.Model):
    """Service type → SAC, description and GST rate. Illustrative until SRIC approves it; the portal never decides
    the rate on its own."""

    key = models.SlugField(max_length=40, unique=True)
    label = models.CharField(max_length=120)
    sac_code = models.CharField(max_length=8)
    description = models.CharField(max_length=200)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2)
    illustrative = models.BooleanField(default=True, help_text=_("Shown as 'illustrative, subject to SRIC approval'."))
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["label"]

    def __str__(self):
        return f"{self.label} ({self.sac_code}, {self.gst_rate}%)"


class InvoiceSeries(models.Model):
    """A numbering series per financial year, allocated atomically (``numbering``). Sandbox series only."""

    prefix = models.CharField(max_length=10, default="EBP")
    financial_year = models.CharField(max_length=5, help_text=_("e.g. 26-27"))
    mode = models.CharField(max_length=10, default="sandbox")
    width = models.PositiveSmallIntegerField(default=4)
    last_number = models.PositiveIntegerField(default=0)
    approved_by_sric = models.BooleanField(default=False, help_text=_("Demonstration series until SRIC approves one."))

    class Meta:
        constraints = [models.UniqueConstraint(fields=["prefix", "financial_year", "mode"], name="gst_einv_series_uniq")]


class CustomerType(models.TextChoices):
    B2B = "B2B", _("GST-registered business or institution")
    GOVT = "GOVT", _("Government organisation (GST-registered)")
    UNREGISTERED = "URP", _("Not GST-registered (individual / unregistered)")


class GstinStatus(models.TextChoices):
    NONE = "none", _("No GSTIN")
    FORMAT_VALID = "format_valid", _("Format valid (not checked with GST)")
    SANDBOX_VERIFIED = "sandbox_verified", _("Checked with the GST sandbox")
    SANDBOX_INACTIVE = "sandbox_inactive", _("GST sandbox: not active")
    SIMULATED_VERIFIED = "simulated_verified", _("Simulated check (mock sandbox)")


class GstCustomer(models.Model):
    """The buyer on an invoice. For portal users it is built from their billing profile; demonstration customers
    are marked ``is_demo`` and never treated as verified customer information."""

    user = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    legal_name = models.CharField(max_length=200)
    trade_name = models.CharField(max_length=200, blank=True, default="")
    customer_type = models.CharField(max_length=4, choices=CustomerType.choices, default=CustomerType.B2B)
    gstin = models.CharField(max_length=15, blank=True, default="")
    gstin_status = models.CharField(max_length=20, choices=GstinStatus.choices, default=GstinStatus.NONE)
    gstin_checked_at = models.DateTimeField(null=True, blank=True)
    address1 = models.CharField(max_length=100)
    address2 = models.CharField(max_length=100, blank=True, default="")
    location = models.CharField(max_length=100)
    pincode = models.CharField(max_length=6)
    state_code = models.CharField(max_length=2)
    place_of_supply = models.CharField(max_length=2, help_text=_("GST state code of the place of supply."))
    contact_name = models.CharField(max_length=120, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    phone = models.CharField(max_length=20, blank=True, default="")
    is_demo = models.BooleanField(default=True)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["legal_name"]

    def __str__(self):
        return self.legal_name

    @property
    def registered(self) -> bool:
        return bool(self.gstin) and self.customer_type != CustomerType.UNREGISTERED


class PurchaseOrderStatus(models.TextChoices):
    PENDING = "pending", _("Awaiting verification")
    VERIFIED = "verified", _("Verified")
    REJECTED = "rejected", _("Rejected")


class CustomerPurchaseOrder(models.Model):
    """A purchase order the customer issued to IIT Roorkee (not IIT Roorkee's own supplier procurement)."""

    customer = models.ForeignKey(GstCustomer, on_delete=models.PROTECT, related_name="purchase_orders")
    po_number = models.CharField(max_length=60)
    po_date = models.DateField()
    issuing_organisation = models.CharField(max_length=200)
    po_value = models.DecimalField(max_digits=14, decimal_places=2)
    valid_until = models.DateField(null=True, blank=True)
    document = models.FileField(upload_to="gst_einvoice/po/%Y/%m/", null=True, blank=True)
    document_name = models.CharField(max_length=255, blank=True, default="")
    booking = models.ForeignKey("equipment.Booking", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    status = models.CharField(max_length=10, choices=PurchaseOrderStatus.choices, default=PurchaseOrderStatus.PENDING)
    remarks = models.TextField(blank=True, default="")
    verified_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)
    is_demo = models.BooleanField(default=True)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-po_date", "-pk"]
        constraints = [models.UniqueConstraint(fields=["customer", "po_number"], name="gst_einv_po_uniq")]


class InvoiceStatus(models.TextChoices):
    DRAFT = "draft", _("Draft")
    PENDING_APPROVAL = "pending_approval", _("Pending SRIC approval")
    APPROVED = "approved", _("Approved")
    REJECTED = "rejected", _("Rejected")
    SUBMITTING = "submitting", _("Submitting to sandbox")
    IRN_GENERATED = "irn_generated", _("IRN generated (sandbox)")
    SUBMISSION_FAILED = "submission_failed", _("Submission failed")
    SUBMISSION_UNCERTAIN = "submission_uncertain", _("Submission outcome unknown")
    CANCELLED = "cancelled", _("Cancelled")


class SupplyType(models.TextChoices):
    B2B = "B2B", _("B2B (recipient GST-registered): e-invoice / IRN applies")
    B2C = "B2C", _("B2C (recipient not GST-registered): tax invoice, no IRN")


class EInvoice(models.Model):
    """One demonstration invoice. Draft reference until approval; the invoice number is allocated on approval."""

    mode = models.CharField(max_length=10, default="sandbox", editable=False)
    is_demo = models.BooleanField(default=True, editable=False)
    provider = models.CharField(max_length=20, blank=True, default="")
    simulated = models.BooleanField(default=False, help_text=_("IRN data came from the mock sandbox, not from an IRP."))
    status = models.CharField(max_length=24, choices=InvoiceStatus.choices, default=InvoiceStatus.DRAFT, db_index=True)
    draft_reference = models.CharField(max_length=24, unique=True)
    series = models.ForeignKey(InvoiceSeries, on_delete=models.PROTECT, null=True, blank=True, related_name="invoices")
    invoice_number = models.CharField(max_length=16, blank=True, default="", db_index=True)
    invoice_date = models.DateField(null=True, blank=True)
    customer = models.ForeignKey(GstCustomer, on_delete=models.PROTECT, related_name="invoices")
    purchase_order = models.ForeignKey(CustomerPurchaseOrder, on_delete=models.PROTECT, null=True, blank=True, related_name="invoices")
    booking = models.ForeignKey("equipment.Booking", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    booking_reference = models.CharField(max_length=40, blank=True, default="")
    fbr_number = models.CharField(max_length=40, blank=True, default="")
    equipment_name = models.CharField(max_length=200, blank=True, default="")
    department_name = models.CharField(max_length=200, blank=True, default="")
    project_number = models.CharField(max_length=40, blank=True, default="")
    pi_name = models.CharField(max_length=120, blank=True, default="")
    receipt_number = models.CharField(max_length=40, blank=True, default="", help_text=_("Payment receipt referenced (separate document)."))
    receipt_date = models.DateField(null=True, blank=True)
    supply_type = models.CharField(max_length=3, choices=SupplyType.choices, default=SupplyType.B2B)
    supplier_state_code = models.CharField(max_length=2, default="05")
    place_of_supply = models.CharField(max_length=2)
    inter_state = models.BooleanField(default=False)
    taxable_value = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    cgst = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    sgst = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    igst = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    round_off = models.DecimalField(max_digits=6, decimal_places=2, default=ZERO)
    amount_in_words = models.CharField(max_length=300, blank=True, default="")
    tax_in_words = models.CharField(max_length=300, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    # IRP (sandbox) result
    irn = models.CharField(max_length=80, blank=True, default="")
    ack_number = models.CharField(max_length=40, blank=True, default="")
    ack_date = models.DateTimeField(null=True, blank=True)
    irp_status = models.CharField(max_length=10, blank=True, default="", help_text=_("ACT / CNL as returned."))
    signed_invoice = models.TextField(blank=True, default="")
    signed_qr = models.TextField(blank=True, default="")
    payload = models.JSONField(default=dict, blank=True, help_text=_("The e-invoice JSON last prepared (schema 1.1)."))
    payload_hash = models.CharField(max_length=64, blank=True, default="")
    cancel_reason = models.CharField(max_length=2, blank=True, default="")
    cancel_remarks = models.CharField(max_length=100, blank=True, default="")
    cancelled_at = models.DateTimeField(null=True, blank=True)
    # People
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    submitted_for_approval_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    approved_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)
    approval_remarks = models.TextField(blank=True, default="")
    irp_submitted_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    irp_submitted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["series", "invoice_number"], condition=~models.Q(invoice_number=""),
                                    name="gst_einv_invoice_number_uniq"),
        ]

    @property
    def display_number(self) -> str:
        return self.invoice_number or self.draft_reference


class EInvoiceLine(models.Model):
    invoice = models.ForeignKey(EInvoice, on_delete=models.CASCADE, related_name="lines")
    sl_no = models.PositiveSmallIntegerField()
    description = models.CharField(max_length=300)
    detail = models.CharField(max_length=300, blank=True, default="")
    sac_code = models.CharField(max_length=8)
    quantity = models.DecimalField(max_digits=12, decimal_places=3, default=Decimal("1"))
    unit = models.CharField(max_length=8, default="OTH")
    unit_price = models.DecimalField(max_digits=14, decimal_places=2)
    taxable_value = models.DecimalField(max_digits=14, decimal_places=2)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2)
    cgst = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    sgst = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    igst = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    tax_rule = models.ForeignKey(TaxRule, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["sl_no"]


class EInvoiceEvent(AppendOnlyModel):
    """Append-only history of an invoice (who did what, when)."""

    invoice = models.ForeignKey(EInvoice, on_delete=models.CASCADE, related_name="events")
    action = models.CharField(max_length=30)
    from_status = models.CharField(max_length=24, blank=True, default="")
    to_status = models.CharField(max_length=24, blank=True, default="")
    actor = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    actor_name = models.CharField(max_length=200, blank=True, default="")
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "pk"]


class IrpOperation(models.TextChoices):
    AUTH = "auth", _("Authenticate")
    GSTIN = "gstin", _("Get GSTIN details")
    GENERATE = "generate", _("Generate IRN")
    GET_BY_DOC = "get_by_doc", _("Get IRN by document")
    GET_IRN = "get_irn", _("Get e-invoice by IRN")
    CANCEL = "cancel", _("Cancel IRN")


class IrpOutcome(models.TextChoices):
    SUCCESS = "success", _("Success")
    REFUSED = "refused", _("Refused by the sandbox")
    UNCERTAIN = "uncertain", _("No answer (timeout / network)")
    NOT_SENT = "not_sent", _("Not sent")
    UNSUPPORTED = "unsupported", _("Operation not available")


class IrpAttempt(AppendOnlyModel):
    """One call (or simulated call) to the IRP sandbox, with secrets and personal data redacted."""

    invoice = models.ForeignKey(EInvoice, on_delete=models.CASCADE, null=True, blank=True, related_name="attempts")
    customer = models.ForeignKey(GstCustomer, on_delete=models.CASCADE, null=True, blank=True, related_name="+")
    operation = models.CharField(max_length=12, choices=IrpOperation.choices)
    provider = models.CharField(max_length=20)
    simulated = models.BooleanField(default=False)
    outcome = models.CharField(max_length=12, choices=IrpOutcome.choices)
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    error_code = models.CharField(max_length=20, blank=True, default="")
    error_message = models.CharField(max_length=500, blank=True, default="")
    request_redacted = models.JSONField(default=dict, blank=True)
    response_redacted = models.JSONField(default=dict, blank=True)
    payload_hash = models.CharField(max_length=64, blank=True, default="")
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    actor = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-pk"]


class SimulatedIrpRecord(models.Model):
    """What the mock sandbox has 'registered' (simulated, never an IRP record). Lets the mock answer Get IRN by
    document after a simulated timeout, exactly as the IRP would, so the no-renumbering rule can be demonstrated."""

    supplier_gstin = models.CharField(max_length=15)
    doc_type = models.CharField(max_length=3, default="INV")
    doc_number = models.CharField(max_length=16)
    doc_date = models.CharField(max_length=10)
    irn = models.CharField(max_length=80, unique=True)
    ack_number = models.CharField(max_length=40)
    ack_date = models.DateTimeField()
    status = models.CharField(max_length=3, default="ACT")
    signed_qr = models.TextField()
    signed_invoice = models.TextField()
    cancelled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["supplier_gstin", "doc_type", "doc_number"], name="gst_einv_sim_doc_uniq")]
