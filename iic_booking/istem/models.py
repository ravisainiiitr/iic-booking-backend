"""I-STEM integration records (https://www.istem.gov.in/).

Nothing here stores credentials or access tokens. The integration only writes these tables; the existing
``Booking.istem_fbr_*`` fields, the manual FBR flow and the result-download gate are owned by the equipment app.
"""

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from .choices import (
    AttemptOutcome,
    CodeType,
    ErrorCategory,
    EvidenceType,
    JobState,
    LIVE_JOB_STATES,
    LinkOtpState,
    Operation,
    RemoteStatus,
    SyncState,
    UserLinkState,
)


class IstemUserLink(models.Model):
    """A local user's I-STEM ``public_user_id``.

    ``public_user_id`` is deliberately not unique: Create User returns the existing I-STEM user when the email *or*
    the mobile number is already registered, and local mobile numbers are not unique, so two local users can resolve
    to one I-STEM user. That case is flagged for review instead of being blocked by the database; only two *linked*
    accounts on one I-STEM user are refused by it.

    ``candidate_*`` hold the existing I-STEM user awaiting the user's email OTP (state ``AWAITING_OTP``); they are
    cleared once the account is linked.
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="istem_link")
    public_user_id = models.CharField(max_length=64, null=True, blank=True, db_index=True)
    state = models.CharField(
        max_length=20, choices=UserLinkState.choices, default=UserLinkState.PENDING, db_index=True
    )
    identity_fingerprint = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text=_("SHA-256 of the normalised email and mobile sent to I-STEM; detects later changes."),
    )
    review_reason = models.TextField(blank=True, default="")
    candidate_public_user_id = models.CharField(
        max_length=64, blank=True, default="", help_text=_("Existing I-STEM user the email OTP would link to.")
    )
    candidate_email = models.EmailField(
        blank=True, default="", help_text=_("Email registered on I-STEM for the candidate user; the OTP goes here.")
    )
    candidate_profile = models.JSONField(
        default=dict, blank=True, help_text=_("Profile I-STEM's user lookup returned for the candidate (for prefill).")
    )
    istem_profile = models.JSONField(
        default=dict, blank=True, help_text=_("The linked I-STEM user's profile from the lookup, kept for prefill.")
    )
    otp_paused_until = models.DateTimeField(
        null=True, blank=True, help_text=_("OTP linking is paused after repeated wrong codes.")
    )
    linked_at = models.DateTimeField(null=True, blank=True)
    last_verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("I-STEM user link")
        verbose_name_plural = _("I-STEM user links")
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["public_user_id"],
                condition=Q(state=UserLinkState.LINKED) & Q(public_user_id__isnull=False),
                name="istem_user_uniq_linked_id",
            ),
        ]

    def __str__(self):
        return f"{self.user} → {self.public_user_id or '—'} ({self.state})"


class IstemLinkOtp(models.Model):
    """One OTP sent to an existing I-STEM user's email, and what became of it (audit of every send and attempt).

    Only an HMAC of the code is stored; the email itself is kept masked.
    """

    user_link = models.ForeignKey(IstemUserLink, on_delete=models.PROTECT, related_name="otps")
    public_user_id = models.CharField(max_length=64)
    email_masked = models.CharField(max_length=255)
    code_hash = models.CharField(max_length=64)
    state = models.CharField(max_length=20, choices=LinkOtpState.choices, default=LinkOtpState.PENDING)
    attempts = models.PositiveSmallIntegerField(default=0)
    sent_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField()
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("I-STEM link OTP")
        verbose_name_plural = _("I-STEM link OTPs")
        ordering = ["-sent_at"]
        indexes = [models.Index(fields=["user_link", "sent_at"], name="istem_otp_link_idx")]

    def __str__(self):
        return f"OTP {self.pk} → {self.email_masked} ({self.state})"


class IstemEquipmentMapping(models.Model):
    """The I-STEM ``equipment_id`` for a local instrument, entered by an admin and checked against I-STEM's Equipment
    List (``remote_*``, written only by ``service.verify_equipment_mappings``).

    ``istem_equipment_id`` is not unique until I-STEM confirms whether several local instruments may share one ID.
    """

    equipment = models.OneToOneField("equipment.Equipment", on_delete=models.PROTECT, related_name="istem_mapping")
    istem_equipment_id = models.CharField(max_length=64, db_index=True)
    sample_count_input_key = models.CharField(
        max_length=8,
        blank=True,
        default="",
        help_text=_("Booking input field (A-G) holding the number of samples sent as booking_no_sample."),
    )
    is_active = models.BooleanField(default=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    remote_checked_at = models.DateTimeField(
        null=True, blank=True, help_text=_("Last check against I-STEM's Equipment List (Active equipment only).")
    )
    remote_found = models.BooleanField(
        null=True, blank=True, help_text=_("Whether the last check found this equipment_id; empty = never checked.")
    )
    remote_equipment_name = models.CharField(max_length=255, blank=True, default="")
    remote_institute_id = models.CharField(
        max_length=32, blank=True, default="", help_text=_("institute_id of the token used for the last check.")
    )
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("I-STEM equipment mapping")
        verbose_name_plural = _("I-STEM equipment mappings")
        ordering = ["equipment_id"]

    def __str__(self):
        return f"{self.equipment} → {self.istem_equipment_id}"


class IstemCodeMapping(models.Model):
    """A local key (user type, charge profile type, …) mapped to a value from an I-STEM code list.

    ``equipment`` set = override for that instrument; empty = global. ``local_key`` ``*`` is the global default.
    """

    code_type = models.CharField(max_length=20, choices=CodeType.choices)
    local_key = models.CharField(max_length=64)
    equipment = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.CASCADE,
        null=True,
        blank=True,
        related_name="istem_code_mappings",
    )
    istem_value = models.CharField(max_length=64)
    is_active = models.BooleanField(default=True)
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("I-STEM code mapping")
        verbose_name_plural = _("I-STEM code mappings")
        ordering = ["code_type", "local_key"]
        constraints = [
            models.UniqueConstraint(
                fields=["code_type", "local_key", "equipment"],
                condition=Q(equipment__isnull=False),
                name="istem_code_uniq_equipment",
            ),
            models.UniqueConstraint(
                fields=["code_type", "local_key"],
                condition=Q(equipment__isnull=True),
                name="istem_code_uniq_global",
            ),
        ]

    def __str__(self):
        scope = f" [{self.equipment}]" if self.equipment_id else ""
        return f"{self.code_type}: {self.local_key}{scope} → {self.istem_value}"


class IstemBookingLink(models.Model):
    """One local booking's I-STEM booking: remote IDs, sync state and the payload that was sent.

    Each remote identifier belongs to at most one local booking; missing identifiers are NULL.
    """

    booking = models.OneToOneField("equipment.Booking", on_delete=models.PROTECT, related_name="istem_link")
    user_link = models.ForeignKey(
        IstemUserLink, on_delete=models.PROTECT, null=True, blank=True, related_name="booking_links"
    )
    istem_equipment_id = models.CharField(
        max_length=64, blank=True, default="", help_text=_("equipment_id sent with Create Booking (snapshot).")
    )
    request_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text=_("new_service_id returned by Create Booking; sent as request_id afterwards."),
    )
    fbr_no = models.CharField(max_length=64, null=True, blank=True)
    confirmation_id = models.CharField(
        max_length=64, null=True, blank=True, help_text=_("Returned by Confirm Booking; required by FBR Execute.")
    )
    payment_id = models.CharField(
        max_length=64, null=True, blank=True, help_text=_("Returned by FBR Execute (also when already executed).")
    )
    istem_slot_shift_hours = models.PositiveSmallIntegerField(
        default=0,
        help_text=_("Hours the I-STEM slot was moved later because the covering hourly slot was occupied on I-STEM."),
    )
    executed_at = models.DateTimeField(
        null=True, blank=True, help_text=_("When I-STEM reported the FBR as executed (FBR Execute or Get FBR Details).")
    )
    sync_state = models.CharField(max_length=32, choices=SyncState.choices, default=SyncState.NOT_ELIGIBLE)
    remote_status = models.CharField(max_length=20, choices=RemoteStatus.choices, blank=True, default="")
    remote_status_raw = models.CharField(max_length=64, blank=True, default="")
    remote_status_checked_at = models.DateTimeField(null=True, blank=True)
    eligibility_reason = models.CharField(max_length=64, blank=True, default="")
    blocked_fields = models.JSONField(default=list, blank=True)
    create_payload = models.JSONField(
        default=dict,
        blank=True,
        help_text=_("Create Booking payload as sent (contains billing details and GSTIN; restricted view)."),
    )
    create_payload_hash = models.CharField(max_length=64, blank=True, default="")
    local_drift = models.BooleanField(
        default=False, help_text=_("Local booking changed after it was created on I-STEM.")
    )
    drift_reason = models.TextField(blank=True, default="")
    needs_review = models.BooleanField(default=False)
    review_reason = models.TextField(blank=True, default="")
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("I-STEM booking link")
        verbose_name_plural = _("I-STEM booking links")
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["request_id"],
                condition=Q(request_id__isnull=False) & ~Q(request_id=""),
                name="istem_link_uniq_request_id",
            ),
            models.UniqueConstraint(
                fields=["fbr_no"],
                condition=Q(fbr_no__isnull=False) & ~Q(fbr_no=""),
                name="istem_link_uniq_fbr_no",
            ),
            models.UniqueConstraint(
                fields=["confirmation_id"],
                condition=Q(confirmation_id__isnull=False) & ~Q(confirmation_id=""),
                name="istem_link_uniq_confirmation",
            ),
        ]
        indexes = [
            models.Index(fields=["sync_state", "updated_at"], name="istem_link_state_idx"),
            models.Index(fields=["remote_status"], name="istem_link_remote_idx"),
            models.Index(fields=["updated_at"], condition=Q(needs_review=True), name="istem_link_review_idx"),
        ]
        permissions = [
            ("view_sensitive_payload", "Can view unmasked I-STEM payloads"),
            ("reconcile", "Can run I-STEM reconciliation"),
            ("link_manually", "Can link I-STEM identifiers manually"),
            ("resolve_uncertain", "Can request resolution of an uncertain I-STEM operation"),
            ("approve_resubmission", "Can approve resubmitting an uncertain I-STEM operation"),
            ("request_confirm", "Can request I-STEM booking confirmation"),
            ("request_execute", "Can request I-STEM FBR execute"),
        ]

    def __str__(self):
        return f"Booking {self.booking_id} → {self.request_id or '—'} ({self.sync_state})"


class IstemOutboxJob(models.Model):
    """Durable unit of I-STEM work, written in the booking's transaction and run by a worker after commit.

    At most one live job (pending, claimed or waiting to retry) may exist per ``dedupe_key``.
    """

    operation = models.CharField(max_length=20, choices=Operation.choices)
    booking_link = models.ForeignKey(
        IstemBookingLink, on_delete=models.CASCADE, null=True, blank=True, related_name="jobs"
    )
    user_link = models.ForeignKey(IstemUserLink, on_delete=models.CASCADE, null=True, blank=True, related_name="jobs")
    trigger = models.CharField(max_length=64, blank=True, default="")
    state = models.CharField(max_length=20, choices=JobState.choices, default=JobState.PENDING)
    dedupe_key = models.CharField(max_length=128)
    attempt_count = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    claimed_at = models.DateTimeField(null=True, blank=True)
    claimed_by = models.CharField(max_length=128, blank=True, default="")
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    last_error_category = models.CharField(max_length=32, choices=ErrorCategory.choices, blank=True, default="")
    last_error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("I-STEM outbox job")
        verbose_name_plural = _("I-STEM outbox jobs")
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["dedupe_key"],
                condition=Q(state__in=[s.value for s in LIVE_JOB_STATES]),
                name="istem_job_uniq_live_key",
            ),
            models.CheckConstraint(
                condition=Q(booking_link__isnull=False) | Q(user_link__isnull=False),
                name="istem_job_has_target",
            ),
        ]
        indexes = [
            models.Index(fields=["state", "next_attempt_at"], name="istem_job_due_idx"),
        ]

    def __str__(self):
        return f"{self.operation} [{self.state}] {self.dedupe_key}"


class IstemApiAttempt(models.Model):
    """One HTTP call to I-STEM, written as IN_FLIGHT before the request is sent.

    Bodies are stored redacted; reconciliation reads (Get Bookings / Get FBR Details) are recorded here too.
    """

    job = models.ForeignKey(
        IstemOutboxJob, on_delete=models.SET_NULL, null=True, blank=True, related_name="attempts"
    )
    booking_link = models.ForeignKey(
        IstemBookingLink, on_delete=models.SET_NULL, null=True, blank=True, related_name="attempts"
    )
    user_link = models.ForeignKey(
        IstemUserLink, on_delete=models.SET_NULL, null=True, blank=True, related_name="attempts"
    )
    operation = models.CharField(max_length=20, choices=Operation.choices)
    endpoint_name = models.CharField(max_length=64, blank=True, default="")
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_ms = models.PositiveIntegerField(null=True, blank=True)
    outcome = models.CharField(max_length=20, choices=AttemptOutcome.choices, default=AttemptOutcome.IN_FLIGHT)
    error_category = models.CharField(max_length=32, choices=ErrorCategory.choices, blank=True, default="")
    http_status = models.PositiveSmallIntegerField(null=True, blank=True)
    request_fingerprint = models.CharField(max_length=64, blank=True, default="")
    request_redacted = models.JSONField(default=dict, blank=True)
    response_redacted = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = _("I-STEM API attempt")
        verbose_name_plural = _("I-STEM API attempts")
        ordering = ["-started_at"]
        indexes = [
            models.Index(fields=["outcome", "started_at"], name="istem_attempt_outcome_idx"),
            models.Index(fields=["booking_link", "started_at"], name="istem_attempt_link_idx"),
        ]

    def __str__(self):
        return f"{self.operation} {self.outcome} @ {self.started_at:%Y-%m-%d %H:%M:%S}"


class IstemSyncEvent(models.Model):
    """Audit record of a link state change, automatic or manual (actor, reason and evidence)."""

    booking_link = models.ForeignKey(
        IstemBookingLink, on_delete=models.PROTECT, null=True, blank=True, related_name="events"
    )
    user_link = models.ForeignKey(IstemUserLink, on_delete=models.PROTECT, null=True, blank=True, related_name="events")
    from_state = models.CharField(max_length=32, blank=True, default="")
    to_state = models.CharField(max_length=32, blank=True, default="")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
        help_text=_("Empty for automatic changes."),
    )
    reason = models.TextField(blank=True, default="")
    evidence_type = models.CharField(max_length=20, choices=EvidenceType.choices, blank=True, default="")
    evidence_reference = models.CharField(max_length=255, blank=True, default="")
    attempt = models.ForeignKey(
        IstemApiAttempt, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("I-STEM sync event")
        verbose_name_plural = _("I-STEM sync events")
        ordering = ["-created_at"]
        constraints = [
            models.CheckConstraint(
                condition=Q(booking_link__isnull=False) | Q(user_link__isnull=False),
                name="istem_event_has_target",
            ),
        ]
        indexes = [
            models.Index(fields=["booking_link", "created_at"], name="istem_event_link_idx"),
        ]

    def __str__(self):
        return f"{self.from_state or '—'} → {self.to_state or '—'}"
