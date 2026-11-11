"""GST invoicing of facility charges through the portal (Dean SRIC proposal, 10-11 Nov 2026). SANDBOX ONLY.

* ``EInvoice`` is prepared from the existing ``external_services.SricInvoiceRequest`` (its snapshot of the booking,
  billing details and payments) or, for the demonstration, from a demo customer. It never changes the booking,
  payment, wallet or receipt tables: the payment receipt is only *referenced* by number.
* B2B (buyer has a valid GSTIN): an IRN and signed QR from the NIC e-invoice SANDBOX (or the simulated mock).
* B2C (no GSTIN): a tax-invoice PDF signed with the DSC of an SRIC signatory, on the signatory's own computer
  (``tools/sric_signer``). The portal only accepts a signed PDF made with a registered certificate that matches the
  prepared PDF byte for byte (``dsc``).
* One continuous portal series (EBP/26-27/0001), numbers allocated only when an invoice is final. No cancellation.
* History is append-only (``EInvoiceEvent``, ``IrpAttempt``); invoices are never deleted.
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
    """Singleton: supplier details printed on invoices and SRIC's review rules. Edited by administrators only; the
    mode (sandbox) and provider are environment settings and cannot be changed here."""

    supplier_legal_name = models.CharField(max_length=200, default="Indian Institute of Technology Roorkee")
    supplier_trade_name = models.CharField(max_length=200, default="SRIC, IIT Roorkee")
    supplier_office = models.CharField(max_length=200, default="Sponsored Research & Industrial Consultancy (SRIC)")
    supplier_gstin = models.CharField(max_length=15, default="05AAALI0033R1Z5")
    supplier_pan = models.CharField(max_length=10, default="AAALI0033R")
    supplier_address1 = models.CharField(max_length=100, default="Indian Institute of Technology Roorkee")
    supplier_address2 = models.CharField(max_length=100, blank=True, default="")
    supplier_location = models.CharField(max_length=100, default="Roorkee")
    supplier_pincode = models.CharField(max_length=6, default="247667")
    supplier_state_code = models.CharField(max_length=2, default="05")
    supplier_email = models.EmailField(default="arsric@iitr.ac.in")
    terms = models.TextField(default=(
        "1. Charges are for testing / analysis services rendered by the facility named above.\n"
        "2. Please quote the invoice number in all correspondence.\n"
        "3. Subject to Roorkee jurisdiction."))
    review_above_amount = models.DecimalField(
        max_digits=14, decimal_places=2, default=Decimal("200000.00"),
        help_text=_("Invoices with a total above this wait for SRIC's review before a number is given."))
    auto_issue = models.BooleanField(
        default=True, help_text=_("Issue invoices that pass every rule without a manual review (SRIC may switch off)."))
    approver_emails = models.TextField(
        blank=True, default="",
        help_text=_("Extra accounts (one email per line) allowed to review invoices, e.g. SRIC staff accounts. "
                    "Accounts In Charge and the Main Administrator can always review."))
    updated_at = models.DateTimeField(auto_now=True)

    @classmethod
    def get(cls) -> "EinvoiceSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class TaxRule(models.Model):
    """Service type -> SAC, description and GST rate, as SRIC approves them. The portal never decides the rate."""

    key = models.SlugField(max_length=40, unique=True)
    label = models.CharField(max_length=120)
    sac_code = models.CharField(max_length=8)
    description = models.CharField(max_length=200)
    gst_rate = models.DecimalField(max_digits=5, decimal_places=2)
    illustrative = models.BooleanField(default=True, help_text=_("Shown as 'subject to SRIC approval'."))
    is_default = models.BooleanField(default=False)
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["label"]

    def __str__(self):
        return f"{self.label} ({self.sac_code}, {self.gst_rate}%)"


class InvoiceSeries(models.Model):
    """The portal's own continuous series per financial year, allocated atomically (``numbering``)."""

    prefix = models.CharField(max_length=10, default="EBP")
    financial_year = models.CharField(max_length=5, help_text=_("e.g. 26-27"))
    mode = models.CharField(max_length=10, default="sandbox")
    width = models.PositiveSmallIntegerField(default=4)
    last_number = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["prefix", "financial_year", "mode"], name="gst_einv_series_uniq")]

    def __str__(self):
        return f"{self.prefix}/{self.financial_year} ({self.mode})"


class CustomerType(models.TextChoices):
    REGISTERED = "B2B", _("GST-registered (GSTIN given)")
    UNREGISTERED = "URP", _("Not GST-registered")


class GstinStatus(models.TextChoices):
    NONE = "none", _("No GSTIN")
    FORMAT_VALID = "format_valid", _("Format and check digit valid")
    SANDBOX_VERIFIED = "sandbox_verified", _("Checked with the GST sandbox")
    SANDBOX_INACTIVE = "sandbox_inactive", _("GST sandbox: not active")
    SIMULATED_VERIFIED = "simulated_verified", _("Simulated check (mock sandbox)")
    INVALID = "invalid", _("Invalid GSTIN")


class GstCustomer(models.Model):
    """The buyer as printed on an invoice: copied from the booking user's billing profile (the snapshot), or a
    demonstration customer (``is_demo``)."""

    user = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    legal_name = models.CharField(max_length=200)
    trade_name = models.CharField(max_length=200, blank=True, default="")
    customer_type = models.CharField(max_length=4, choices=CustomerType.choices, default=CustomerType.UNREGISTERED)
    gstin = models.CharField(max_length=15, blank=True, default="")
    gstin_status = models.CharField(max_length=20, choices=GstinStatus.choices, default=GstinStatus.NONE)
    gstin_checked_at = models.DateTimeField(null=True, blank=True)
    address1 = models.CharField(max_length=100)
    address2 = models.CharField(max_length=100, blank=True, default="")
    location = models.CharField(max_length=100)
    pincode = models.CharField(max_length=6)
    state_code = models.CharField(max_length=2, blank=True, default="")
    contact_name = models.CharField(max_length=120, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    is_demo = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["legal_name"]

    def __str__(self):
        return self.legal_name

    @property
    def registered(self) -> bool:
        return bool(self.gstin) and self.customer_type == CustomerType.REGISTERED


class InvoiceStatus(models.TextChoices):
    PENDING_REVIEW = "pending_review", _("Waiting for SRIC's review")
    REJECTED = "rejected", _("Not issued (SRIC review)")
    SUBMITTING = "submitting", _("Sending to the e-invoice system")
    SUBMISSION_FAILED = "submission_failed", _("E-invoice system refused it")
    SUBMISSION_UNCERTAIN = "submission_uncertain", _("No answer from the e-invoice system")
    IRN_GENERATED = "irn_generated", _("E-invoice issued (IRN)")
    AWAITING_SIGNATURE = "awaiting_signature", _("Waiting for the signatory's DSC")
    SIGNED = "signed", _("Signed with the DSC")


ISSUED_STATUSES = (InvoiceStatus.IRN_GENERATED, InvoiceStatus.SIGNED)


class SupplyType(models.TextChoices):
    B2B = "B2B", _("Buyer with GSTIN: e-invoice (IRN)")
    B2C = "B2C", _("Buyer without GSTIN: tax invoice signed with SRIC's DSC")


class EInvoice(models.Model):
    """One invoice. The number is allocated when it is final (after review, if any); until then it has a draft
    reference. Amounts and buyer details are frozen copies."""

    mode = models.CharField(max_length=10, default="sandbox", editable=False)
    provider = models.CharField(max_length=20, blank=True, default="")
    simulated = models.BooleanField(default=False, help_text=_("IRN data came from the mock sandbox, not from an IRP."))
    status = models.CharField(max_length=24, choices=InvoiceStatus.choices, default=InvoiceStatus.PENDING_REVIEW,
                              db_index=True)
    supply_type = models.CharField(max_length=3, choices=SupplyType.choices)
    draft_reference = models.CharField(max_length=24, unique=True)
    review_reasons = models.JSONField(default=list, blank=True)
    series = models.ForeignKey(InvoiceSeries, on_delete=models.PROTECT, null=True, blank=True, related_name="invoices")
    invoice_number = models.CharField(max_length=16, blank=True, default="", db_index=True)
    invoice_date = models.DateField(null=True, blank=True)
    # Where it came from: the existing SRIC invoice request of a booking (one invoice per request), or the demo.
    sric_request = models.OneToOneField("external_services.SricInvoiceRequest", on_delete=models.PROTECT, null=True,
                                        blank=True, related_name="gst_einvoice")
    booking = models.ForeignKey("equipment.Booking", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    is_demo = models.BooleanField(default=False)
    customer = models.ForeignKey(GstCustomer, on_delete=models.PROTECT, related_name="invoices")
    booking_reference = models.CharField(max_length=40, blank=True, default="")
    fbr_number = models.CharField(max_length=40, blank=True, default="")
    equipment_name = models.CharField(max_length=200, blank=True, default="")
    department_name = models.CharField(max_length=200, blank=True, default="")
    receipt_number = models.CharField(max_length=48, blank=True, default="",
                                      help_text=_("Payment receipt referenced (a separate document)."))
    receipt_date = models.DateField(null=True, blank=True)
    po_number = models.CharField(max_length=16, blank=True, default="")
    po_date = models.DateField(null=True, blank=True)
    supplier_state_code = models.CharField(max_length=2, default="05")
    place_of_supply = models.CharField(max_length=2)
    inter_state = models.BooleanField(default=False)
    taxable_value = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    cgst = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    sgst = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    igst = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    round_off = models.DecimalField(max_digits=6, decimal_places=2, default=ZERO)
    total = models.DecimalField(max_digits=14, decimal_places=2, default=ZERO)
    portal_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True,
                                       help_text=_("What the booking charged (for the consistency check)."))
    notes = models.TextField(blank=True, default="")
    # B2B: e-invoice system (sandbox) result
    irn = models.CharField(max_length=80, blank=True, default="")
    ack_number = models.CharField(max_length=40, blank=True, default="")
    ack_date = models.DateTimeField(null=True, blank=True)
    irp_status = models.CharField(max_length=10, blank=True, default="")
    signed_invoice = models.TextField(blank=True, default="")
    signed_qr = models.TextField(blank=True, default="")
    payload = models.JSONField(default=dict, blank=True, help_text=_("The e-invoice JSON last prepared (schema 1.1)."))
    payload_hash = models.CharField(max_length=64, blank=True, default="")
    irp_submitted_at = models.DateTimeField(null=True, blank=True)
    # Documents: the prepared PDF (B2C: what the signatory signs), the signed PDF, and the copy delivered to the
    # booking through the existing OfficialInvoiceDocument (``delivery``).
    pdf = models.FileField(upload_to="gst_einvoice/prepared/%Y/%m/", null=True, blank=True)
    pdf_sha256 = models.CharField(max_length=64, blank=True, default="")
    signed_pdf = models.FileField(upload_to="gst_einvoice/signed/%Y/%m/", null=True, blank=True)
    signed_pdf_sha256 = models.CharField(max_length=64, blank=True, default="")
    signed_at = models.DateTimeField(null=True, blank=True)
    signed_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    signer_certificate = models.ForeignKey("SignerCertificate", on_delete=models.PROTECT, null=True, blank=True,
                                           related_name="invoices")
    official_document = models.ForeignKey("external_services.OfficialInvoiceDocument", on_delete=models.SET_NULL,
                                          null=True, blank=True, related_name="+")
    delivered_at = models.DateTimeField(null=True, blank=True)
    # People
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    reviewed_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_remarks = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["series", "invoice_number"], condition=~models.Q(invoice_number=""),
                                    name="gst_einv_invoice_number_uniq"),
        ]

    def __str__(self):
        return self.display_number

    @property
    def display_number(self) -> str:
        return self.invoice_number or self.draft_reference

    @property
    def issued(self) -> bool:
        return self.status in ISSUED_STATUSES


class EInvoiceLine(models.Model):
    invoice = models.ForeignKey(EInvoice, on_delete=models.PROTECT, related_name="lines")
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

    invoice = models.ForeignKey(EInvoice, on_delete=models.PROTECT, related_name="events")
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


class IrpOutcome(models.TextChoices):
    SUCCESS = "success", _("Success")
    REFUSED = "refused", _("Refused by the sandbox")
    UNCERTAIN = "uncertain", _("No answer (timeout / network)")
    NOT_SENT = "not_sent", _("Not sent")
    UNSUPPORTED = "unsupported", _("Operation not available")


class IrpAttempt(AppendOnlyModel):
    """One call (or simulated call) to the IRP sandbox, with secrets and contact details redacted."""

    invoice = models.ForeignKey(EInvoice, on_delete=models.PROTECT, null=True, blank=True, related_name="attempts")
    customer = models.ForeignKey(GstCustomer, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
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
    document after a simulated timeout, exactly as the IRP would, so the no-renumbering rule can be shown."""

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

    class Meta:
        constraints = [models.UniqueConstraint(fields=["supplier_gstin", "doc_type", "doc_number"], name="gst_einv_sim_doc_uniq")]


# --- DSC signing (buyers without a GSTIN) ----------------------------------------------------------------------------


class SignerCertificate(models.Model):
    """The public certificate of an SRIC signatory's DSC, registered by an administrator. Only the certificate (public
    data) is stored: the private key stays in the signatory's token. A signed invoice is accepted only when its
    signature verifies with an active, in-date registered certificate of the person who uploads it."""

    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    label = models.CharField(max_length=120, blank=True, default="")
    certificate_pem = models.TextField()
    sha256_fingerprint = models.CharField(max_length=64, unique=True)
    subject = models.CharField(max_length=400)
    issuer = models.CharField(max_length=400)
    serial_number = models.CharField(max_length=80)
    not_before = models.DateTimeField()
    not_after = models.DateTimeField()
    is_test = models.BooleanField(default=False, help_text=_("A test DSC for the sandbox demonstration."))
    is_active = models.BooleanField(default=True)
    registered_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    registered_at = models.DateTimeField(auto_now_add=True)
    deactivated_at = models.DateTimeField(null=True, blank=True)
    deactivated_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    deactivation_reason = models.CharField(max_length=300, blank=True, default="")

    class Meta:
        ordering = ["-registered_at"]


class SignerDevice(models.Model):
    """A signing utility installed on a signatory's computer. It authenticates with a random token shown once; only
    the token's SHA-256 is stored. A device can list invoices waiting for signature, download them, and upload the
    signed PDFs; nothing else."""

    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    name = models.CharField(max_length=120)
    token_sha256 = models.CharField(max_length=64, unique=True)
    token_hint = models.CharField(max_length=12)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
