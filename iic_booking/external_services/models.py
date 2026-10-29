"""External-user services: the central external booking window, special booking requests with payment deadlines,
invoice requests to SRIC, physical invoice delivery and QR sample receipt.

Everything here is additive. Internal-user booking never reads these tables.
"""

from __future__ import annotations

from datetime import time

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from iic_booking.procurement_management.models import AppendOnlyModel

USER = settings.AUTH_USER_MODEL


class Weekday(models.IntegerChoices):
    MONDAY = 0, _("Monday")
    TUESDAY = 1, _("Tuesday")
    WEDNESDAY = 2, _("Wednesday")
    THURSDAY = 3, _("Thursday")
    FRIDAY = 4, _("Friday")
    SATURDAY = 5, _("Saturday")
    SUNDAY = 6, _("Sunday")


class ExternalBookingWindowConfig(models.Model):
    """The portal-wide window in which external users may make ordinary bookings (single row, pk=1).

    The open date range is computed from these fields and the current time (``window.external_window``); no
    scheduled task has to run for it to be right. Changed only by the Main Administrator through
    ``window_admin`` (every change is in ``ExternalBookingWindowChange``). Off by default: while ``enabled`` is
    false, external users keep the per-equipment rule that existed before.
    """

    enabled = models.BooleanField(
        default=False,
        help_text=_("Apply this window to external users' ordinary bookings. Off: the previous per-equipment rule."),
    )
    initial_open_at = models.DateTimeField(
        null=True, blank=True, help_text=_("When external users can first book (stored in UTC, shown in IST).")
    )
    first_week_start = models.DateField(
        null=True, blank=True, help_text=_("Monday of the first week open to external users.")
    )
    initial_weeks = models.PositiveSmallIntegerField(default=4, help_text=_("Weeks open at the initial opening."))
    auto_expand = models.BooleanField(default=True, help_text=_("Open one more week at every weekly expansion time."))
    expansion_weekday = models.PositiveSmallIntegerField(choices=Weekday.choices, default=Weekday.WEDNESDAY)
    expansion_time = models.TimeField(default=time(21, 0), help_text=_("Local time (IST) of the weekly expansion."))
    # Expansions already counted before ``counting_from`` (pause, resume or a schedule change materialise them).
    materialized_expansions = models.PositiveIntegerField(default=0)
    counting_from = models.DateTimeField(
        null=True, blank=True, help_text=_("Weekly expansions are counted after this instant (initially the opening).")
    )
    manual_weeks = models.PositiveIntegerField(default=0, help_text=_("Weeks opened early by the Main Administrator."))
    min_lead_days = models.PositiveSmallIntegerField(
        default=0, help_text=_("Days from today before the first date an external user may book (0 = today).")
    )
    updated_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("External booking window")
        verbose_name_plural = _("External booking window")
        constraints = [models.CheckConstraint(condition=models.Q(pk=1), name="external_window_config_singleton")]

    def __str__(self) -> str:
        return "External booking window"

    @classmethod
    def load(cls) -> ExternalBookingWindowConfig:
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class WindowAction(models.TextChoices):
    CONFIGURE = "configure", _("Configuration changed")
    ENABLE = "enable", _("Enabled")
    DISABLE = "disable", _("Disabled")
    PAUSE = "pause", _("Weekly expansion paused")
    RESUME = "resume", _("Weekly expansion resumed")
    ADVANCE = "advance", _("Opened weeks early")


class ExternalBookingWindowChange(AppendOnlyModel):
    """Append-only record of every change to the window, with the bookable range before and after."""

    actor = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    action = models.CharField(max_length=20, choices=WindowAction.choices)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    range_before = models.JSONField(default=dict, blank=True)
    range_after = models.JSONField(default=dict, blank=True)
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = _("External booking window change")
        verbose_name_plural = _("External booking window changes")

    def __str__(self) -> str:
        return f"{self.get_action_display()} at {self.created_at:%Y-%m-%d %H:%M}"


class ExternalBookingWindowOpening(models.Model):
    """One row per week found open to external users (``recorded_at``: when the scheduled task first saw it open);
    slots and the quota snapshot for the week are prepared once."""

    week_start = models.DateField(unique=True)
    recorded_at = models.DateTimeField(auto_now_add=True)
    slots_prepared_at = models.DateTimeField(null=True, blank=True)
    quota_prepared_at = models.DateTimeField(null=True, blank=True)
    slots_created = models.PositiveIntegerField(default=0)
    snapshots_created = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["week_start"]
        verbose_name = _("External window opening")
        verbose_name_plural = _("External window openings")

    def __str__(self) -> str:
        return f"Week of {self.week_start:%d %b %Y}"


# --- settings and policy texts ---------------------------------------------------------------------------------------


class ExternalServicesSettings(models.Model):
    """Switches and operational settings for the external-user services (single row, pk=1).

    Every feature starts off. Changed only by the Main Administrator through ``settings_admin`` (audited in
    ``ExternalServicesSettingsChange``). Values that are institutional policy (surcharge, limits) are kept here, not
    in code, and their defaults are the conservative choice recorded in the technical design.
    """

    special_requests_enabled = models.BooleanField(
        default=False, help_text=_("External users may submit special booking requests.")
    )
    special_request_surcharge = models.BooleanField(
        default=False,
        help_text=_("Add the urgent-booking surcharge to special requests (policy decision; off until approved)."),
    )
    max_open_special_requests = models.PositiveSmallIntegerField(
        default=3, help_text=_("Pending or clarification-requested special requests one user may have at a time.")
    )
    short_notice_days = models.PositiveSmallIntegerField(
        default=12, help_text=_("A slot starting sooner than this many days away is short notice.")
    )
    payment_window_hours = models.PositiveSmallIntegerField(
        default=24, help_text=_("Hours after approval to pay (or until the slot starts, if sooner).")
    )
    # Invoices (SRIC issues the GST invoice; the portal requests it and delivers it)
    invoice_requests_enabled = models.BooleanField(
        default=False, help_text=_("External users may choose an invoice timing and invoice requests are queued.")
    )
    early_invoice_allowed = models.BooleanField(
        default=False, help_text=_("Offer the 'GST invoice before analysis' option (needs approved disclosure).")
    )
    auto_request_without_preference = models.BooleanField(
        default=False,
        help_text=_("After analysis, also request an invoice for bookings whose user never chose a timing."),
    )
    sric_dispatch_mode = models.CharField(
        max_length=8,
        choices=[("off", _("Off: keep requests queued")), ("test", _("Test: send to the test address")),
                 ("live", _("Live: send to SRIC"))],
        default="off",
    )
    sric_recipient_email = models.EmailField(blank=True, default="", help_text=_("SRIC invoice desk (live mode)."))
    sric_test_recipient_email = models.EmailField(blank=True, default="", help_text=_("Used in test mode."))
    # Credit for government organisations (approved per request by the Head IIC by email)
    govt_credit_enabled = models.BooleanField(
        default=False, help_text=_("Government and R&D organisations may raise credit requests.")
    )
    head_iic_emails = models.TextField(
        blank=True, default="",
        help_text=_("Designated Head IIC addresses that receive credit requests with Accept / Reject buttons, one per "
                    "line."),
    )
    # Physical invoice copies, labels and sample receipt
    physical_copies_enabled = models.BooleanField(default=False)
    sample_labels_enabled = models.BooleanField(default=False)
    iic_postal_address = models.TextField(
        blank=True, default="",
        help_text=_("Approved IIC address for labels, one line per row (sender for invoices, recipient for samples)."),
    )
    updated_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("External services settings")
        verbose_name_plural = _("External services settings")
        constraints = [models.CheckConstraint(condition=models.Q(pk=1), name="external_services_settings_singleton")]

    def __str__(self) -> str:
        return "External services settings"

    @classmethod
    def load(cls) -> ExternalServicesSettings:
        obj, _created = cls.objects.get_or_create(pk=1)
        return obj


class ExternalServicesSettingsChange(AppendOnlyModel):
    actor = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    reason = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]


class PolicyKey(models.TextChoices):
    SHORT_NOTICE = "short_notice", _("Short-notice booking conditions")
    EARLY_INVOICE = "early_invoice", _("Early GST invoice disclosure")


class PolicyText(AppendOnlyModel):
    """Versioned wording users acknowledge. A new version is a new row; old versions stay for the record.

    ``approved`` stays false until IIC (or SRIC, for invoice wording) approves the text; the UI says so.
    """

    key = models.CharField(max_length=32, choices=PolicyKey.choices)
    version = models.PositiveIntegerField()
    text = models.TextField()
    approved = models.BooleanField(default=False)
    approval_note = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["key", "-version"]
        constraints = [models.UniqueConstraint(fields=["key", "version"], name="external_policy_text_version_unique")]

    def __str__(self) -> str:
        return f"{self.key} v{self.version}"


# --- special booking requests (external users) ------------------------------------------------------------------------


class SpecialRequestDetail(models.Model):
    """External-only details of an ``UrgentBookingRequest`` of type ``EXTERNAL_SPECIAL``.

    The requested times are tentative: nothing is reserved until an Officer in Charge allocates slots.
    """

    request = models.OneToOneField(
        "equipment.UrgentBookingRequest", on_delete=models.CASCADE, related_name="external_special"
    )
    preferred_start = models.DateTimeField()
    preferred_end = models.DateTimeField()
    alternatives = models.JSONField(default=list, blank=True, help_text=_("Up to 3 {start, end} alternatives."))
    sample_description = models.TextField(blank=True, default="")
    remarks = models.TextField(blank=True, default="")
    short_notice = models.BooleanField(default=False, help_text=_("Preferred start was short notice when submitted."))
    short_notice_policy = models.ForeignKey(
        PolicyText, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    short_notice_text_sha256 = models.CharField(max_length=64, blank=True, default="")
    short_notice_ack_at = models.DateTimeField(null=True, blank=True)
    clarification_message = models.TextField(blank=True, default="")
    clarification_requested_at = models.DateTimeField(null=True, blank=True)
    clarification_response = models.TextField(blank=True, default="")
    clarification_responded_at = models.DateTimeField(null=True, blank=True)
    withdrawn_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["preferred_start"])]


class SpecialRequestAction(models.TextChoices):
    SUBMITTED = "submitted", _("Submitted")
    CLARIFICATION_REQUESTED = "clarification_requested", _("Clarification requested")
    CLARIFIED = "clarified", _("User replied")
    WITHDRAWN = "withdrawn", _("Withdrawn by user")
    REJECTED = "rejected", _("Rejected")
    ALLOCATED = "allocated", _("Approved and slot allocated")
    SHORT_NOTICE_ACKNOWLEDGED = "short_notice_ack", _("Short-notice conditions acknowledged")
    PAID = "paid", _("Payment realised")
    EXPIRED = "expired", _("Expired unpaid")
    LATE_PAYMENT = "late_payment", _("Payment received after expiry")


class SpecialRequestEvent(AppendOnlyModel):
    """Append-only history of a special request (who did what, when, why)."""

    request = models.ForeignKey("equipment.UrgentBookingRequest", on_delete=models.CASCADE, related_name="+")
    action = models.CharField(max_length=32, choices=SpecialRequestAction.choices)
    actor = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    reason = models.TextField(blank=True, default="")
    details = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["created_at", "id"]
        indexes = [models.Index(fields=["request", "created_at"])]


class AllocationState(models.TextChoices):
    AWAITING_PAYMENT = "awaiting_payment", _("Awaiting payment")
    PAID = "paid", _("Paid")
    EXPIRED = "expired", _("Expired unpaid")
    CANCELLED = "cancelled", _("Cancelled")


class SpecialAllocation(models.Model):
    """The booking an Officer in Charge created for an external special request, and its payment deadline.

    ``payment_deadline_at = min(approved_at + payment window, first allocated slot start)``, both server-side.
    """

    request = models.OneToOneField(
        "equipment.UrgentBookingRequest", on_delete=models.PROTECT, related_name="external_allocation"
    )
    booking = models.OneToOneField(
        "equipment.Booking", on_delete=models.PROTECT, related_name="external_special_allocation"
    )
    approved_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    approved_at = models.DateTimeField()
    slot_start = models.DateTimeField()
    payment_deadline_at = models.DateTimeField(db_index=True)
    previous_slot_statuses = models.JSONField(default=dict, blank=True)
    short_notice = models.BooleanField(default=False, help_text=_("The allocated slot is short notice."))
    short_notice_ack_at = models.DateTimeField(null=True, blank=True)
    short_notice_policy = models.ForeignKey(
        PolicyText, on_delete=models.PROTECT, null=True, blank=True, related_name="+"
    )
    state = models.CharField(
        max_length=20, choices=AllocationState.choices, default=AllocationState.AWAITING_PAYMENT, db_index=True
    )
    paid_at = models.DateTimeField(null=True, blank=True)
    expired_at = models.DateTimeField(null=True, blank=True)
    expiry_reason = models.CharField(max_length=255, blank=True, default="")
    late_payment_at = models.DateTimeField(null=True, blank=True)
    late_payment_note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["state", "payment_deadline_at"])]


# --- notifications ---------------------------------------------------------------------------------------------------


class NotificationChannel(models.TextChoices):
    EMAIL = "email", _("Email")
    IN_APP = "in_app", _("In-portal")


class NotificationStatus(models.TextChoices):
    PENDING = "pending", _("Pending")
    SENDING = "sending", _("Sending")
    SENT = "sent", _("Sent")
    FAILED = "failed", _("Failed")
    UNCERTAIN = "uncertain", _("Outcome unknown (not resent)")


class ExternalNotification(models.Model):
    """One message to one recipient on one channel, created in the same transaction as the event and sent after
    commit (never from ``save()``). ``key`` makes a repeated event a no-op."""

    key = models.CharField(max_length=160)
    recipient = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="+")
    channel = models.CharField(max_length=10, choices=NotificationChannel.choices)
    title = models.CharField(max_length=200)
    message = models.TextField()
    link = models.CharField(max_length=300, blank=True, default="")
    status = models.CharField(
        max_length=12, choices=NotificationStatus.choices, default=NotificationStatus.PENDING, db_index=True
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=255, blank=True, default="")
    claimed_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["key", "recipient", "channel"], name="external_notification_once")
        ]
        indexes = [models.Index(fields=["status", "created_at"])]


# --- invoices -------------------------------------------------------------------------------------------------------


class InvoiceOption(models.TextChoices):
    EARLY = "early", _("Request the GST invoice before analysis is completed")
    AFTER_ANALYSIS = "after_analysis", _("Request the GST invoice after analysis")


class InvoicePreference(AppendOnlyModel):
    """The user's choice of invoice timing for a booking. Append-only: a change is a new row (latest wins)."""

    booking = models.ForeignKey("equipment.Booking", on_delete=models.CASCADE, related_name="external_invoice_prefs")
    option = models.CharField(max_length=20, choices=InvoiceOption.choices)
    chosen_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    disclosure_policy = models.ForeignKey(PolicyText, on_delete=models.PROTECT, null=True, blank=True, related_name="+")
    disclosure_text_sha256 = models.CharField(max_length=64, blank=True, default="")
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["booking", "created_at"])]


class InvoiceTrigger(models.TextChoices):
    EARLY = "early", _("Early request by the user")
    COMPLETION = "completion", _("Analysis completed")


class InvoiceRequestStatus(models.TextChoices):
    QUEUED = "queued", _("Queued for the next SRIC batch")
    BATCHED = "batched", _("Included in a batch")
    SENDING = "sending", _("Sending")
    SENT = "sent", _("Request emailed to SRIC")
    FAILED = "failed", _("Sending failed (will retry)")
    GAVE_UP = "gave_up", _("Sending failed (action required)")
    UNCERTAIN = "uncertain", _("Sending outcome unknown (action required)")
    INVOICE_RECEIVED = "invoice_received", _("Invoice received and uploaded")
    # Not sent in the daily batch until released: credit not yet realised, or waiting to join a consolidated invoice.
    HELD_CREDIT = "held_credit", _("Held until the government credit is settled")
    HELD_CONSOLIDATION = "held_consolidation", _("Held for a consolidated invoice")


class SricInvoiceBatch(models.Model):
    """One batch per working day (09:30 IST). Requests queued before the batch was opened go into it."""

    batch_date = models.DateField(unique=True)
    opened_at = models.DateTimeField(auto_now_add=True)
    mode = models.CharField(max_length=8, default="off")
    requests_count = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["-batch_date"]


class SricInvoiceRequest(models.Model):
    """The request to SRIC for a booking's GST invoice: one per booking (the unique booking makes a repeated
    completion event, a double click or an early request plus completion the same request)."""

    booking = models.OneToOneField("equipment.Booking", on_delete=models.PROTECT, related_name="sric_invoice_request")
    trigger = models.CharField(max_length=12, choices=InvoiceTrigger.choices)
    early_requested = models.BooleanField(default=False)
    preference = models.ForeignKey(InvoicePreference, on_delete=models.PROTECT, null=True, blank=True,
                                   related_name="+")
    requested_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    snapshot = models.JSONField(default=dict, help_text=_("Invoice details from portal records when queued."))
    status = models.CharField(max_length=20, choices=InvoiceRequestStatus.choices,
                              default=InvoiceRequestStatus.QUEUED, db_index=True)
    batch = models.ForeignKey(SricInvoiceBatch, on_delete=models.PROTECT, null=True, blank=True,
                              related_name="requests")
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=255, blank=True, default="")
    claimed_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_to = models.CharField(max_length=254, blank=True, default="")
    sent_in_mode = models.CharField(max_length=8, blank=True, default="")
    consolidation_group = models.CharField(
        max_length=64, blank=True, default="", db_index=True,
        help_text=_("Requests sharing a group go to SRIC as one consolidated invoice request."),
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]


class SricDispatchAttempt(AppendOnlyModel):
    request = models.ForeignKey(SricInvoiceRequest, on_delete=models.CASCADE, related_name="attempt_log")
    batch = models.ForeignKey(SricInvoiceBatch, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    outcome = models.CharField(max_length=12)  # sent / failed / uncertain / resolved
    detail = models.CharField(max_length=255, blank=True, default="")
    actor = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class OfficialInvoiceDocument(models.Model):
    """The GST invoice SRIC issued, uploaded by the Accounts In Charge. Never deleted: a correction uploads a new
    document that replaces the current one (``replaces``, ``replacement_reason``)."""

    request = models.ForeignKey(SricInvoiceRequest, on_delete=models.PROTECT, related_name="documents")
    file = models.FileField(upload_to="sric_invoices/%Y/%m/")
    original_name = models.CharField(max_length=255)
    size = models.PositiveIntegerField()
    sha256 = models.CharField(max_length=64)
    invoice_number = models.CharField(max_length=64, blank=True, default="")
    is_current = models.BooleanField(default=True)
    replaces = models.OneToOneField("self", on_delete=models.PROTECT, null=True, blank=True,
                                    related_name="replaced_by")
    replacement_reason = models.TextField(blank=True, default="")
    uploaded_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name="+")
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at"]
        constraints = [
            models.UniqueConstraint(fields=["request"], condition=models.Q(is_current=True),
                                    name="external_one_current_invoice_document")
        ]


# --- physical invoice copies -----------------------------------------------------------------------------------------


class SavedPostalAddress(models.Model):
    """A user's own address book for physical deliveries. Archived (not deleted) once used for a dispatch."""

    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="external_postal_addresses")
    label = models.CharField(max_length=60, blank=True, default="")
    recipient_name = models.CharField(max_length=120)
    organisation = models.CharField(max_length=160, blank=True, default="")
    line1 = models.CharField(max_length=200)
    line2 = models.CharField(max_length=200, blank=True, default="")
    city = models.CharField(max_length=100)
    district = models.CharField(max_length=100, blank=True, default="")
    state = models.CharField(max_length=100)
    postal_code = models.CharField(max_length=12)
    country = models.CharField(max_length=60, default="India")
    phone = models.CharField(max_length=20, blank=True, default="")
    is_default = models.BooleanField(default=False)
    archived_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-is_default", "label", "id"]
        constraints = [
            models.UniqueConstraint(fields=["user"], condition=models.Q(is_default=True, archived_at__isnull=True),
                                    name="external_one_default_address")
        ]


class CopyRequestStatus(models.TextChoices):
    WAITING_FOR_INVOICE = "waiting_invoice", _("Waiting for the invoice from SRIC")
    REQUESTED = "requested", _("Requested")
    PREPARED = "prepared", _("Envelope prepared")
    DISPATCHED = "dispatched", _("Dispatched by Speed Post")
    CANCELLED = "cancelled", _("Cancelled")


class PhysicalCopyRequest(models.Model):
    """A request for a printed copy of the official invoice by Speed Post. The address is copied at request time
    (``address_snapshot``) so later edits to the address book do not change a dispatch."""

    invoice_request = models.ForeignKey(SricInvoiceRequest, on_delete=models.PROTECT, related_name="copy_requests")
    requested_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    address_snapshot = models.JSONField()
    status = models.CharField(max_length=20, choices=CopyRequestStatus.choices, db_index=True)
    prepared_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    prepared_at = models.DateTimeField(null=True, blank=True)
    dispatched_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    dispatched_at = models.DateTimeField(null=True, blank=True)
    dispatch_date = models.DateField(null=True, blank=True)
    speed_post_reference = models.CharField(max_length=40, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["invoice_request"], condition=~models.Q(status="cancelled"),
                name="external_one_open_copy_request",
            )
        ]


class CopyRequestEvent(AppendOnlyModel):
    copy_request = models.ForeignKey(PhysicalCopyRequest, on_delete=models.CASCADE, related_name="events")
    action = models.CharField(max_length=20)
    actor = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    detail = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


# --- sample shipment labels and receipt ------------------------------------------------------------------------------


class SampleShipmentLabel(models.Model):
    """A sample shipment label for a booking. The QR code carries a random token; only its SHA-256 is stored, so the
    database cannot be used to rebuild a code, and a label is revoked by issuing a new one."""

    booking = models.ForeignKey("equipment.Booking", on_delete=models.CASCADE, related_name="external_sample_labels")
    token_sha256 = models.CharField(max_length=64, unique=True)
    reference = models.CharField(max_length=12, unique=True, help_text=_("Short random code printed for people."))
    sample_count = models.PositiveSmallIntegerField(default=1)
    description = models.CharField(max_length=200, blank=True, default="")
    sender_snapshot = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class SampleReceiptScan(AppendOnlyModel):
    """Every staff look-up and confirmation through a label (scanning alone never marks a sample received)."""

    label = models.ForeignKey(SampleShipmentLabel, on_delete=models.CASCADE, related_name="scans")
    staff = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, related_name="+")
    action = models.CharField(max_length=12)  # looked_up / confirmed / duplicate
    notes = models.CharField(max_length=500, blank=True, default="")
    trace_status = models.CharField(max_length=20, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


# --- booking checkout (payment choice before the booking exists) ----------------------------------------------------

class CheckoutChoice(models.TextChoices):
    FULL_GATEWAY = "full_gateway", _("Full amount via online payment")
    WALLET_PLUS_GATEWAY = "wallet_plus_gateway", _("Wallet balance + remaining amount online")
    FULL_WALLET = "full_wallet", _("Full amount via wallet")


class CheckoutState(models.TextChoices):
    QUOTED = "quoted", _("Awaiting payment choice")
    AWAITING_PAYMENT = "awaiting_payment", _("Awaiting online payment")
    CONFIRMED = "confirmed", _("Booking confirmed")
    PAID_NOT_CONFIRMED = "paid_not_confirmed", _("Paid, booking could not be confirmed")
    CANCELLED = "cancelled", _("Cancelled")
    EXPIRED = "expired", _("Expired")


class ExternalBookingCheckout(models.Model):
    """An external user's self-booking between pricing and payment. While awaiting an online payment it holds its
    slots for the user (``slot_holds``) until ``expires_at`` or the payment; the booking is created only
    when the payment is confirmed (wallet debit, or Razorpay settlement), through the normal booking pipeline, which
    re-checks the slots, price and wallet at that moment.

    ``wallet_amount`` and ``gateway_amount`` are set by the server from the chosen option; ``gateway_amount`` excludes
    the gateway convenience fee, which the payment order adds.
    """

    public_id = models.UUIDField(unique=True, editable=False)
    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="external_booking_checkouts")
    equipment = models.ForeignKey("equipment.Equipment", on_delete=models.PROTECT, related_name="+")
    request_body = models.JSONField(default=dict, help_text=_("Booking request as validated (no client amounts)."))
    slot_ids = models.JSONField(default=list)
    summary = models.JSONField(default=dict, blank=True, help_text=_("Display snapshot: slots, samples, charges."))
    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    subwallet = models.ForeignKey(
        "users.SubWallet", on_delete=models.PROTECT, null=True, blank=True, related_name="+",
        help_text=_("Sub-wallet the booking would be charged to."),
    )
    choice = models.CharField(max_length=24, choices=CheckoutChoice.choices, blank=True, default="")
    wallet_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    gateway_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    payment_order = models.OneToOneField(
        "payments.PaymentOrder", on_delete=models.PROTECT, null=True, blank=True, related_name="external_checkout"
    )
    booking = models.OneToOneField(
        "equipment.Booking", on_delete=models.PROTECT, null=True, blank=True, related_name="external_checkout"
    )
    state = models.CharField(max_length=24, choices=CheckoutState.choices, default=CheckoutState.QUOTED, db_index=True)
    expires_at = models.DateTimeField(null=True, blank=True, db_index=True)
    outcome_note = models.TextField(blank=True, default="")
    confirmed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "state"])]

    def __str__(self) -> str:
        return f"Checkout {self.public_id} ({self.state})"


# --- invoice mode and government credit --------------------------------------------------------------------------------

class InvoiceMode(models.TextChoices):
    PER_BOOKING = "per_booking", _("One GST invoice per booking")
    CONSOLIDATED = "consolidated", _("One consolidated GST invoice for several bookings")


class InvoiceModePreference(AppendOnlyModel):
    """A user's choice between per-booking and consolidated GST invoices. Append-only: the latest row wins; no row
    means per booking."""

    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="external_invoice_modes")
    mode = models.CharField(max_length=16, choices=InvoiceMode.choices)
    chosen_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["user", "created_at"])]


class GovtCreditStatus(models.TextChoices):
    PENDING = "pending", _("Waiting for the Head IIC")
    ACCEPTED = "accepted", _("Accepted; wallet credited")
    REJECTED = "rejected", _("Rejected by the Head IIC")
    SETTLED = "settled", _("Settled; payment realised")
    CANCELLED = "cancelled", _("Cancelled by the user")


class GovtCreditRequest(models.Model):
    """Credit for a government or R&D organisation that pays only after the invoice: raised against the approval
    letter its department issued for the portal's consolidated pro forma invoice, accepted or rejected by the Head IIC
    by email, credited to the user's wallet on acceptance, and settled when SRIC confirms the payment is realised.
    No GST invoice is sent for the user's bookings while a credit is accepted and not settled."""

    public_id = models.UUIDField(unique=True, editable=False)
    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="govt_credit_requests")
    subwallet = models.ForeignKey("users.SubWallet", on_delete=models.PROTECT, related_name="+",
                                  help_text=_("Department sub-wallet credited on acceptance."))
    organisation = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    proforma_reference = models.CharField(max_length=64, help_text=_("Consolidated pro forma invoice number."))
    purpose = models.TextField(blank=True, default="", help_text=_("Planned work covered by the credit."))
    approval_letter = models.FileField(upload_to="govt_credit_letters/%Y/%m/")
    credit_until = models.DateField(help_text=_("End of the credit period."))
    settlement_date = models.DateField(help_text=_("Date by which the organisation settles the credit."))
    invoice_mode = models.CharField(max_length=16, choices=InvoiceMode.choices, default=InvoiceMode.CONSOLIDATED)
    status = models.CharField(max_length=12, choices=GovtCreditStatus.choices, default=GovtCreditStatus.PENDING,
                              db_index=True)
    decided_by_email = models.EmailField(blank=True, default="")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default="")
    wallet_transaction = models.ForeignKey("users.SubWalletTransaction", on_delete=models.PROTECT, null=True,
                                           blank=True, related_name="+")
    settled_at = models.DateTimeField(null=True, blank=True)
    settled_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    settlement_reference = models.CharField(max_length=128, blank=True, default="",
                                            help_text=_("SRIC receipt / UTR confirming the payment is realised."))
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["user", "status"])]

    def __str__(self) -> str:
        return f"Govt credit {self.proforma_reference} Rs. {self.amount} ({self.status})"


class GovtCreditEvent(AppendOnlyModel):
    """Audit trail of a credit request: raised, emailed, accepted, rejected, settled."""

    request = models.ForeignKey(GovtCreditRequest, on_delete=models.PROTECT, related_name="events")
    action = models.CharField(max_length=24)
    actor = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    actor_email = models.EmailField(blank=True, default="")
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


class ConsolidatedProforma(models.Model):
    """A consolidated pro forma invoice for planned work across bookings (not a GST invoice). A government or R&D
    user's department issues its approval letter against it, and the credit request names it."""

    reference = models.CharField(max_length=64, unique=True)
    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="consolidated_proformas")
    items = models.JSONField(default=list, help_text=_("[{description, amount}] of the planned work."))
    total = models.DecimalField(max_digits=12, decimal_places=2)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.reference} Rs. {self.total}"
