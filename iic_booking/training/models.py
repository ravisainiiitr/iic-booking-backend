"""
Training & Certification (Phase 1).

One ``TrainingEvent`` per offering (demo, hands-on training, later certification courses and workshops)
with sessions that reserve free slots, seats (``Registration``), attendance and the resulting
``CertificationAward``. Faculty demo requests and the nomination → shortlist → selection pipeline feed
events. Assessments, L1/L2 rights, certificate PDFs, deputation and paid events are later phases; the
models keep room for them (levels with ``rights``, award validity/suspension fields, payment fields on
registrations) without using them yet.
"""

from __future__ import annotations

import secrets
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

USER = settings.AUTH_USER_MODEL


def _verify_token() -> str:
    return secrets.token_urlsafe(24)


DEFAULT_SCORING_WEIGHTS: dict[str, float] = {
    "first_time_equipment": 30,
    "never_trained_anywhere": 10,
    "need_per_point": 6,
    "need_max_points": 3,
    "demand_max": 15,
    "tenure_max": 10,
    "tenure_full_months": 18,
    "tenure_zero_months": 6,
    "tenure_unknown": 5,
    "department_max": 10,
    "group_no_certified": 7,
    "cooldown_penalty": -15,
    "no_show_penalty": -10,
    "tie_window": 1,
}


# ---------------------------------------------------------------------------
# Policy and catalogue
# ---------------------------------------------------------------------------
class PolicyScope(models.TextChoices):
    GLOBAL = "GLOBAL", _("Global")
    DEPARTMENT = "DEPARTMENT", _("Department")
    EQUIPMENT = "EQUIPMENT", _("Equipment")


class TrainingPolicy(models.Model):
    """Versioned selection, validity and demo policy. The newest active row per scope applies."""

    scope = models.CharField(max_length=20, choices=PolicyScope.choices, default=PolicyScope.GLOBAL)
    department = models.ForeignKey(
        "users.Department", on_delete=models.CASCADE, null=True, blank=True, related_name="training_policies"
    )
    equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.CASCADE, null=True, blank=True, related_name="training_policies"
    )
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    per_faculty_cap = models.PositiveSmallIntegerField(default=1)
    per_department_pct = models.PositiveSmallIntegerField(default=40)
    reserved_pct = models.PositiveSmallIntegerField(default=20)
    underrepresented_override_department_ids = models.JSONField(default=list, blank=True)
    scoring_weights = models.JSONField(default=dict, blank=True)
    cooldown_months = models.PositiveSmallIntegerField(default=6)
    min_tenure_months_after_training = models.PositiveSmallIntegerField(default=3)
    suspension_lookback_months = models.PositiveSmallIntegerField(default=12)

    trained_validity_months = models.PositiveSmallIntegerField(default=24)
    dormancy_months = models.PositiveSmallIntegerField(default=6)

    seat_confirm_hours = models.PositiveSmallIntegerField(default=48)
    appeal_working_days = models.PositiveSmallIntegerField(default=3)
    proposal_expiry_working_days = models.PositiveSmallIntegerField(default=3)
    review_sla_working_days = models.PositiveSmallIntegerField(default=3)

    demo_rate_per_hour = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    demo_max_minutes = models.PositiveIntegerField(default=180)
    demo_refund_full_days = models.PositiveSmallIntegerField(default=7)
    demo_refund_half_days = models.PositiveSmallIntegerField(default=2)

    deputy_settings = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True, default="")
    published_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["scope", "-version"]
        indexes = [models.Index(fields=["scope", "is_active"])]
        verbose_name_plural = "Training policies"

    def __str__(self) -> str:
        target = self.equipment or self.department or "global"
        return f"Training policy v{self.version} ({target})"

    def weights(self) -> dict[str, float]:
        return {**DEFAULT_SCORING_WEIGHTS, **(self.scoring_weights or {})}


class CertificationLevel(models.Model):
    code = models.CharField(max_length=30, unique=True)
    name = models.CharField(max_length=100)
    rank = models.PositiveSmallIntegerField(default=10)
    rights = models.JSONField(default=dict, blank=True)
    default_validity_months = models.PositiveSmallIntegerField(null=True, blank=True)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["rank"]

    def __str__(self) -> str:
        return self.name


class EventKind(models.TextChoices):
    DEMO = "DEMO", _("Internal demonstration")
    HANDS_ON = "HANDS_ON", _("Hands-on training")
    CERT_COURSE = "CERT_COURSE", _("Certification course")
    REFRESHER = "REFRESHER", _("Refresher")
    EXTERNAL_WORKSHOP = "EXTERNAL_WORKSHOP", _("External workshop")


class TrainingProgram(models.Model):
    code = models.CharField(max_length=50, unique=True)
    title = models.CharField(max_length=255)
    kind = models.CharField(max_length=30, choices=EventKind.choices, default=EventKind.HANDS_ON)
    primary_equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.SET_NULL, null=True, blank=True, related_name="training_programs"
    )
    equipment_group = models.ForeignKey(
        "equipment.EquipmentGroup", on_delete=models.SET_NULL, null=True, blank=True, related_name="training_programs"
    )
    level_awarded = models.ForeignKey(
        CertificationLevel, on_delete=models.PROTECT, null=True, blank=True, related_name="programs_awarding"
    )
    prerequisite_level = models.ForeignKey(
        CertificationLevel, on_delete=models.PROTECT, null=True, blank=True, related_name="programs_requiring"
    )
    syllabus = models.TextField(blank=True, default="")
    min_practice_sessions = models.PositiveSmallIntegerField(default=0)
    assessment_mode = models.CharField(max_length=20, default="NONE")
    audience_types = models.JSONField(default=list, blank=True)
    default_capacity = models.PositiveSmallIntegerField(default=8)
    default_duration_minutes = models.PositiveIntegerField(default=120)
    department = models.ForeignKey(
        "users.Department", on_delete=models.SET_NULL, null=True, blank=True, related_name="training_programs"
    )
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["title"]

    def __str__(self) -> str:
        return self.title


# ---------------------------------------------------------------------------
# Events, sessions, slot reservations
# ---------------------------------------------------------------------------
class EventStatus(models.TextChoices):
    DRAFT = "DRAFT", _("Draft")
    OPEN = "OPEN", _("Open")
    SHORTLISTING = "SHORTLISTING", _("Shortlisting")
    SELECTION_PUBLISHED = "SELECTION_PUBLISHED", _("Selection published")
    CONFIRMED = "CONFIRMED", _("Confirmed")
    IN_PROGRESS = "IN_PROGRESS", _("In progress")
    ASSESSING = "ASSESSING", _("Assessing")
    COMPLETED = "COMPLETED", _("Completed")
    CLOSED = "CLOSED", _("Closed")
    CANCELLED = "CANCELLED", _("Cancelled")


class SelectionMode(models.TextChoices):
    FCFS = "FCFS", _("First come, first served")
    NOMINATION = "NOMINATION", _("Nomination and shortlist")
    INVITE = "INVITE", _("Invite")


class TrainingEvent(models.Model):
    program = models.ForeignKey(TrainingProgram, on_delete=models.SET_NULL, null=True, blank=True, related_name="events")
    kind = models.CharField(max_length=30, choices=EventKind.choices, default=EventKind.HANDS_ON)
    title = models.CharField(max_length=255)
    slug = models.SlugField(max_length=80, unique=True)
    description = models.TextField(blank=True, default="")
    equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.PROTECT, null=True, blank=True, related_name="training_events"
    )
    department = models.ForeignKey(
        "users.Department", on_delete=models.SET_NULL, null=True, blank=True, related_name="training_events"
    )
    level_awarded = models.ForeignKey(
        CertificationLevel, on_delete=models.PROTECT, null=True, blank=True, related_name="events_awarding"
    )
    audience = models.CharField(max_length=30, default="INTERNAL")
    visibility = models.CharField(max_length=30, default="INSTITUTE")
    status = models.CharField(max_length=30, choices=EventStatus.choices, default=EventStatus.DRAFT)
    capacity = models.PositiveSmallIntegerField(default=8)
    min_participants = models.PositiveSmallIntegerField(default=1)
    selection_mode = models.CharField(max_length=20, choices=SelectionMode.choices, default=SelectionMode.NOMINATION)
    registration_opens_at = models.DateTimeField(null=True, blank=True)
    registration_closes_at = models.DateTimeField(null=True, blank=True)
    venue = models.CharField(max_length=255, blank=True, default="")
    coordinators = models.ManyToManyField(USER, blank=True, related_name="coordinated_training_events")
    fee_required = models.BooleanField(default=False)
    refund_policy = models.JSONField(default=dict, blank=True)
    certificate_template = models.CharField(max_length=100, blank=True, default="")
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    published_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["equipment", "status"]), models.Index(fields=["kind", "status"])]

    def __str__(self) -> str:
        return self.title


class SessionType(models.TextChoices):
    THEORY = "THEORY", _("Theory")
    DEMO = "DEMO", _("Demonstration")
    HANDS_ON = "HANDS_ON", _("Hands-on")
    PRACTICE = "PRACTICE", _("Supervised practice")
    ASSESSMENT = "ASSESSMENT", _("Assessment")


class SessionStatus(models.TextChoices):
    PLANNED = "PLANNED", _("Planned")
    SCHEDULED = "SCHEDULED", _("Scheduled (slots reserved)")
    COMPLETED = "COMPLETED", _("Completed")
    CANCELLED = "CANCELLED", _("Cancelled")


class TrainingSession(models.Model):
    event = models.ForeignKey(TrainingEvent, on_delete=models.CASCADE, related_name="sessions")
    seq = models.PositiveSmallIntegerField(default=1)
    title = models.CharField(max_length=255, blank=True, default="")
    session_type = models.CharField(max_length=20, choices=SessionType.choices, default=SessionType.HANDS_ON)
    equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.PROTECT, null=True, blank=True, related_name="training_sessions"
    )
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    location = models.CharField(max_length=255, blank=True, default="")
    trainers = models.ManyToManyField(USER, blank=True, related_name="training_sessions_as_trainer")
    status = models.CharField(max_length=20, choices=SessionStatus.choices, default=SessionStatus.PLANNED)
    attendance_marked_at = models.DateTimeField(null=True, blank=True)
    attendance_marked_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    notes = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["event", "seq", "start_at"]
        indexes = [models.Index(fields=["start_at"]), models.Index(fields=["equipment", "start_at"])]

    def __str__(self) -> str:
        return f"{self.event.title} · session {self.seq}"


class SessionSlotReservation(models.Model):
    """One reserved DailySlot. Only AVAILABLE slots are taken; release restores ``previous_status``."""

    session = models.ForeignKey(TrainingSession, on_delete=models.CASCADE, related_name="slot_reservations")
    daily_slot = models.ForeignKey("equipment.DailySlot", on_delete=models.CASCADE, related_name="training_reservations")
    equipment = models.ForeignKey("equipment.Equipment", on_delete=models.CASCADE, related_name="+")
    is_family_block = models.BooleanField(default=False)
    previous_status = models.CharField(max_length=20)
    previous_label = models.CharField(max_length=255, blank=True, null=True)
    label = models.CharField(max_length=255)
    reserved_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    reserved_at = models.DateTimeField(auto_now_add=True)
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    release_note = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        ordering = ["daily_slot__start_datetime"]
        constraints = [
            models.UniqueConstraint(
                fields=["daily_slot"],
                condition=models.Q(released_at__isnull=True),
                name="training_one_active_reservation_per_slot",
            )
        ]


# ---------------------------------------------------------------------------
# Demo intake
# ---------------------------------------------------------------------------
class DemoPurpose(models.TextChoices):
    COURSE = "COURSE", _("Course / curricular")
    RESEARCH_INDUCTION = "RESEARCH_INDUCTION", _("Research group induction")
    OTHER = "OTHER", _("Other")


class DemoStatus(models.TextChoices):
    SUBMITTED = "SUBMITTED", _("Submitted")
    UNDER_REVIEW = "UNDER_REVIEW", _("Under review")
    PROPOSED_ALTERNATIVE = "PROPOSED_ALTERNATIVE", _("Another time proposed")
    APPROVED = "APPROVED", _("Approved")
    SCHEDULED = "SCHEDULED", _("Scheduled")
    COMPLETED = "COMPLETED", _("Completed")
    REJECTED = "REJECTED", _("Rejected")
    WITHDRAWN = "WITHDRAWN", _("Withdrawn")
    CANCELLED = "CANCELLED", _("Cancelled")
    EXPIRED = "EXPIRED", _("Proposal expired")
    NO_SHOW = "NO_SHOW", _("No show")


class CurtailReason(models.TextChoices):
    INSTRUMENT_TIME = "INSTRUMENT_TIME", _("Instrument time constraints")
    SAMPLE_CONSUMABLE = "SAMPLE_CONSUMABLE", _("Sample / consumable limits")
    SAFETY_CAPACITY = "SAFETY_CAPACITY", _("Safety capacity")
    POLICY_MAX = "POLICY_MAX", _("Policy maximum")
    OTHER = "OTHER", _("Other")


class ChargeMode(models.TextChoices):
    FREE = "FREE", _("No charge")
    WALLET = "WALLET", _("Charge faculty wallet")
    WAIVED = "WAIVED", _("Charge waived by the OIC")


class DemoRequest(models.Model):
    requester = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="demo_requests")
    equipment = models.ForeignKey("equipment.Equipment", on_delete=models.PROTECT, related_name="demo_requests")
    purpose = models.CharField(max_length=30, choices=DemoPurpose.choices, default=DemoPurpose.COURSE)
    course_code = models.CharField(max_length=50, blank=True, default="")
    course_name = models.CharField(max_length=255, blank=True, default="")
    participants_requested = models.PositiveSmallIntegerField(default=1)
    participant_users = models.ManyToManyField(USER, blank=True, related_name="demo_requests_as_participant")
    participant_list_text = models.TextField(blank=True, default="")
    preferred_windows = models.JSONField(default=list, blank=True)
    requested_duration_minutes = models.PositiveIntegerField(default=60)
    notes = models.TextField(blank=True, default="")
    charge_acknowledged = models.BooleanField(default=False)

    status = models.CharField(max_length=30, choices=DemoStatus.choices, default=DemoStatus.SUBMITTED)
    approved_duration_minutes = models.PositiveIntegerField(null=True, blank=True)
    approved_participants = models.PositiveSmallIntegerField(null=True, blank=True)
    approved_start_at = models.DateTimeField(null=True, blank=True)
    approved_end_at = models.DateTimeField(null=True, blank=True)
    curtailed = models.BooleanField(default=False)
    curtail_reason_code = models.CharField(max_length=30, choices=CurtailReason.choices, blank=True, default="")
    oic_remarks = models.TextField(blank=True, default="")
    decided_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)

    proposed_start_at = models.DateTimeField(null=True, blank=True)
    proposed_end_at = models.DateTimeField(null=True, blank=True)
    proposal_expires_at = models.DateTimeField(null=True, blank=True)
    counter_used = models.BooleanField(default=False)
    faculty_response = models.TextField(blank=True, default="")

    charge_mode = models.CharField(max_length=10, choices=ChargeMode.choices, default=ChargeMode.FREE)
    rate_per_hour = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    charge_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    sub_wallet = models.ForeignKey("users.SubWallet", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    wallet_txn = models.ForeignKey(
        "users.SubWalletTransaction", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    refund_txn = models.ForeignKey(
        "users.SubWalletTransaction", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    refund_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    event = models.ForeignKey(TrainingEvent, on_delete=models.SET_NULL, null=True, blank=True, related_name="demo_requests")
    submitted_at = models.DateTimeField(auto_now_add=True)
    sla_escalated_at = models.DateTimeField(null=True, blank=True)
    cancelled_by_side = models.CharField(max_length=10, blank=True, default="")
    cancel_reason = models.TextField(blank=True, default="")
    attended_count = models.PositiveSmallIntegerField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-submitted_at"]
        indexes = [models.Index(fields=["equipment", "status"]), models.Index(fields=["requester", "status"])]

    def __str__(self) -> str:
        return f"{self.reference} {self.equipment_id}"

    @property
    def reference(self) -> str:
        return f"D-{self.pk:04d}" if self.pk else "D-new"

    @property
    def is_curricular(self) -> bool:
        return self.purpose == DemoPurpose.COURSE


class DemoRequestRevision(models.Model):
    request = models.ForeignKey(DemoRequest, on_delete=models.CASCADE, related_name="revisions")
    actor = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    action = models.CharField(max_length=40)
    from_status = models.CharField(max_length=30, blank=True, default="")
    to_status = models.CharField(max_length=30, blank=True, default="")
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    reason_code = models.CharField(max_length=30, blank=True, default="")
    reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at", "id"]


# ---------------------------------------------------------------------------
# Nomination, shortlisting, appeals
# ---------------------------------------------------------------------------
class CallStatus(models.TextChoices):
    OPEN = "OPEN", _("Open")
    CLOSED = "CLOSED", _("Closed")
    PUBLISHED = "PUBLISHED", _("Selection published")
    CANCELLED = "CANCELLED", _("Cancelled")


class NominationCall(models.Model):
    event = models.ForeignKey(TrainingEvent, on_delete=models.CASCADE, related_name="nomination_calls")
    equipment = models.ForeignKey("equipment.Equipment", on_delete=models.PROTECT, related_name="training_calls")
    title = models.CharField(max_length=255, blank=True, default="")
    seats = models.PositiveSmallIntegerField()
    deadline = models.DateTimeField()
    eligibility = models.JSONField(default=dict, blank=True)
    caps_snapshot = models.JSONField(default=dict, blank=True)
    policy = models.ForeignKey(TrainingPolicy, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    policy_version = models.PositiveIntegerField(default=1)
    status = models.CharField(max_length=20, choices=CallStatus.choices, default=CallStatus.OPEN)
    notes = models.TextField(blank=True, default="")
    opened_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    opened_at = models.DateTimeField(auto_now_add=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    legacy_ta_call = models.ForeignKey(
        "equipment.EquipmentOperatingTACall", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )

    class Meta:
        ordering = ["-opened_at"]
        indexes = [models.Index(fields=["status", "deadline"])]

    def __str__(self) -> str:
        return self.title or f"Call {self.pk}"

    @property
    def reference(self) -> str:
        return f"C-{self.pk:03d}" if self.pk else "C-new"


class NeedCategory(models.TextChoices):
    THESIS_CRITICAL = "THESIS_CRITICAL", _("Thesis-critical")
    FUNDED_PROJECT = "FUNDED_PROJECT", _("Funded-project deliverable")
    EXPLORATORY = "EXPLORATORY", _("Exploratory")


NEED_CATEGORY_POINTS = {
    NeedCategory.THESIS_CRITICAL: 3,
    NeedCategory.FUNDED_PROJECT: 2,
    NeedCategory.EXPLORATORY: 1,
}


class NominationStatus(models.TextChoices):
    SUBMITTED = "SUBMITTED", _("Submitted")
    ELIGIBLE = "ELIGIBLE", _("Eligible")
    INELIGIBLE = "INELIGIBLE", _("Ineligible")
    WITHDRAWN = "WITHDRAWN", _("Withdrawn")
    SELECTED = "SELECTED", _("Selected")
    WAITLISTED = "WAITLISTED", _("Waitlisted")
    NOT_SELECTED = "NOT_SELECTED", _("Not selected")
    CONFIRMED = "CONFIRMED", _("Seat confirmed")
    DECLINED = "DECLINED", _("Seat declined")
    EXPIRED = "EXPIRED", _("Confirmation expired")


class TrainingNomination(models.Model):
    call = models.ForeignKey(NominationCall, on_delete=models.CASCADE, related_name="nominations")
    student = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="training_nominations")
    nominator = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="training_nominations_made")
    need_category = models.CharField(max_length=30, choices=NeedCategory.choices, default=NeedCategory.EXPLORATORY)
    justification = models.TextField(blank=True, default="")
    expected_hours_month = models.PositiveSmallIntegerField(null=True, blank=True)
    need_adjustment = models.SmallIntegerField(default=0)
    need_adjust_reason = models.TextField(blank=True, default="")
    student_confirmed_at = models.DateTimeField(null=True, blank=True)
    sop_acknowledged_at = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=NominationStatus.choices, default=NominationStatus.SUBMITTED)
    eligibility_flags = models.JSONField(default=dict, blank=True)
    ineligible_reason = models.TextField(blank=True, default="")
    selected_at = models.DateTimeField(null=True, blank=True)
    confirm_deadline = models.DateTimeField(null=True, blank=True)
    confirmed_at = models.DateTimeField(null=True, blank=True)
    selected_on_appeal = models.BooleanField(default=False)
    promoted_from_waitlist = models.BooleanField(default=False)
    legacy_nomination = models.ForeignKey(
        "equipment.StudentEquipmentNomination", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [models.UniqueConstraint(fields=["call", "student"], name="training_one_nomination_per_call")]


class RunStatus(models.TextChoices):
    DRAFT = "DRAFT", _("Preview")
    PUBLISHED = "PUBLISHED", _("Published")
    SUPERSEDED = "SUPERSEDED", _("Superseded")


class ShortlistRun(models.Model):
    call = models.ForeignKey(NominationCall, on_delete=models.CASCADE, related_name="shortlist_runs")
    policy_snapshot = models.JSONField(default=dict)
    inputs_snapshot = models.JSONField(default=dict)
    seed = models.CharField(max_length=64)
    seed_timestamp = models.CharField(max_length=40)
    seed_public_input = models.CharField(max_length=64, blank=True, default="")
    mode = models.CharField(max_length=30, default="SCORE_SEEDED_TIEBREAK")
    status = models.CharField(max_length=20, choices=RunStatus.choices, default=RunStatus.DRAFT)
    run_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    run_at = models.DateTimeField(auto_now_add=True)
    published_at = models.DateTimeField(null=True, blank=True)
    published_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    appeal_deadline = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-run_at", "-id"]


class EntryOutcome(models.TextChoices):
    SELECTED = "SELECTED", _("Selected")
    WAITLISTED = "WAITLISTED", _("Waitlisted")
    NOT_SELECTED = "NOT_SELECTED", _("Not selected")
    INELIGIBLE = "INELIGIBLE", _("Ineligible")


class ShortlistEntry(models.Model):
    run = models.ForeignKey(ShortlistRun, on_delete=models.CASCADE, related_name="entries")
    nomination = models.ForeignKey(TrainingNomination, on_delete=models.CASCADE, related_name="shortlist_entries")
    score_total = models.DecimalField(max_digits=7, decimal_places=2, default=Decimal("0"))
    score_breakdown = models.JSONField(default=dict)
    rank = models.PositiveIntegerField(null=True, blank=True)
    tie_group = models.PositiveIntegerField(null=True, blank=True)
    lottery_key = models.CharField(max_length=64, blank=True, default="")
    outcome = models.CharField(max_length=20, choices=EntryOutcome.choices)
    seat_type = models.CharField(max_length=20, blank=True, default="")
    waitlist_position = models.PositiveIntegerField(null=True, blank=True)
    constraint_note = models.CharField(max_length=255, blank=True, default="")
    overridden = models.BooleanField(default=False)
    override_outcome = models.CharField(max_length=20, blank=True, default="")
    override_reason = models.TextField(blank=True, default="")
    override_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    override_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["run", "rank", "id"]


class AppealStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    UPHELD = "UPHELD", _("Upheld (decision stands)")
    OVERTURNED = "OVERTURNED", _("Overturned (seat granted)")


class SelectionAppeal(models.Model):
    entry = models.ForeignKey(ShortlistEntry, on_delete=models.CASCADE, related_name="appeals")
    submitted_by = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="+")
    reason = models.TextField()
    status = models.CharField(max_length=20, choices=AppealStatus.choices, default=AppealStatus.PENDING)
    decided_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    decided_at = models.DateTimeField(null=True, blank=True)
    decision_note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


# ---------------------------------------------------------------------------
# Seats and attendance
# ---------------------------------------------------------------------------
class RegistrationSource(models.TextChoices):
    NOMINATION = "NOMINATION", _("Nomination")
    DEMO = "DEMO", _("Demo participant")
    DIRECT = "DIRECT", _("Direct")
    INVITE = "INVITE", _("Invite")


class RegistrationStatus(models.TextChoices):
    PENDING_PAYMENT = "PENDING_PAYMENT", _("Pending payment")
    CONFIRMED = "CONFIRMED", _("Confirmed")
    ATTENDED = "ATTENDED", _("Attended")
    PARTIAL = "PARTIAL", _("Partially attended")
    NO_SHOW = "NO_SHOW", _("No show")
    COMPLETED = "COMPLETED", _("Completed")
    CANCELLED = "CANCELLED", _("Cancelled")
    EXPIRED = "EXPIRED", _("Expired")
    TRANSFERRED = "TRANSFERRED", _("Transferred")


class Registration(models.Model):
    event = models.ForeignKey(TrainingEvent, on_delete=models.CASCADE, related_name="registrations")
    user = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="training_registrations")
    source = models.CharField(max_length=20, choices=RegistrationSource.choices, default=RegistrationSource.NOMINATION)
    nomination = models.ForeignKey(
        TrainingNomination, on_delete=models.SET_NULL, null=True, blank=True, related_name="registrations"
    )
    participant_snapshot = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=20, choices=RegistrationStatus.choices, default=RegistrationStatus.CONFIRMED)
    payment_status = models.CharField(max_length=20, default="NOT_REQUIRED")
    payment_method = models.CharField(max_length=20, blank=True, default="")
    amount_total = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    wallet_txn = models.ForeignKey(
        "users.SubWalletTransaction", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    seat_hold_until = models.DateTimeField(null=True, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    refund_amount = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["event", "user__name"]
        constraints = [models.UniqueConstraint(fields=["event", "user"], name="training_one_registration_per_event")]


class AttendanceStatus(models.TextChoices):
    PRESENT = "PRESENT", _("Present")
    LATE = "LATE", _("Late")
    ABSENT = "ABSENT", _("Absent")
    EXCUSED = "EXCUSED", _("Excused")


class Attendance(models.Model):
    registration = models.ForeignKey(Registration, on_delete=models.CASCADE, related_name="attendance")
    session = models.ForeignKey(TrainingSession, on_delete=models.CASCADE, related_name="attendance")
    status = models.CharField(max_length=10, choices=AttendanceStatus.choices)
    minutes = models.PositiveIntegerField(null=True, blank=True)
    method = models.CharField(max_length=10, default="MANUAL")
    remarks = models.CharField(max_length=255, blank=True, default="")
    marked_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    marked_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["registration", "session"], name="training_one_attendance_per_session")
        ]


# ---------------------------------------------------------------------------
# Certification and badges
# ---------------------------------------------------------------------------
class AwardStatus(models.TextChoices):
    PROVISIONAL = "PROVISIONAL", _("Provisional")
    ACTIVE = "ACTIVE", _("Active")
    DORMANT = "DORMANT", _("Dormant")
    EXPIRED = "EXPIRED", _("Expired")
    SUSPENDED = "SUSPENDED", _("Suspended")
    REVOKED = "REVOKED", _("Revoked")
    SUPERSEDED = "SUPERSEDED", _("Superseded by a higher level")


class CertificationAward(models.Model):
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="certification_awards")
    equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.PROTECT, null=True, blank=True, related_name="certification_awards"
    )
    equipment_group = models.ForeignKey(
        "equipment.EquipmentGroup", on_delete=models.SET_NULL, null=True, blank=True, related_name="certification_awards"
    )
    level = models.ForeignKey(CertificationLevel, on_delete=models.PROTECT, related_name="awards")
    status = models.CharField(max_length=20, choices=AwardStatus.choices, default=AwardStatus.ACTIVE)
    awarded_at = models.DateTimeField()
    valid_until = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    source_event = models.ForeignKey(TrainingEvent, on_delete=models.SET_NULL, null=True, blank=True, related_name="awards")
    source_registration = models.ForeignKey(
        Registration, on_delete=models.SET_NULL, null=True, blank=True, related_name="awards"
    )
    awarded_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    certificate_no = models.CharField(max_length=60, unique=True, null=True, blank=True)
    verify_token = models.CharField(max_length=64, unique=True, default=_verify_token)
    suspended_at = models.DateTimeField(null=True, blank=True)
    suspended_until = models.DateTimeField(null=True, blank=True)
    suspend_reason = models.TextField(blank=True, default="")
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    revoke_reason = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-awarded_at"]
        indexes = [models.Index(fields=["user", "status"]), models.Index(fields=["equipment", "status"])]


class BadgeDefinition(models.Model):
    code = models.CharField(max_length=40, unique=True)
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True, default="")
    icon = models.CharField(max_length=40, blank=True, default="award")
    color = models.CharField(max_length=20, blank=True, default="#0f766e")
    rule_type = models.CharField(max_length=20, default="LEVEL")
    level = models.ForeignKey(CertificationLevel, on_delete=models.SET_NULL, null=True, blank=True, related_name="badges")
    threshold = models.PositiveIntegerField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class UserBadge(models.Model):
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="training_badges")
    badge = models.ForeignKey(BadgeDefinition, on_delete=models.PROTECT, related_name="user_badges")
    equipment = models.ForeignKey("equipment.Equipment", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    award = models.ForeignKey(CertificationAward, on_delete=models.SET_NULL, null=True, blank=True, related_name="badges")
    source = models.CharField(max_length=40, blank=True, default="")
    awarded_at = models.DateTimeField()
    is_public = models.BooleanField(default=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-awarded_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["user", "badge", "equipment"],
                condition=models.Q(revoked_at__isnull=True),
                name="training_one_active_badge_per_equipment",
            )
        ]


# ---------------------------------------------------------------------------
# Module switch, audience and per-equipment enablement (Main Admin)
# ---------------------------------------------------------------------------
class TrainingAudience(models.TextChoices):
    TEST_ACCOUNTS = "TEST_ACCOUNTS", _("Test accounts only")
    EVERYONE = "EVERYONE", _("Everyone eligible")


class TrainingModuleSettings(models.Model):
    """Single row (pk=1) edited by the Main Admin in Admin Settings → Training Policy.

    The module is on when this switch or the server env ``TRAINING_MODULE_ENABLED`` is on. The audience
    applies either way; faculty and students outside it see no Training menus and get 403 from the API.
    """

    SINGLETON_PK = 1

    module_enabled = models.BooleanField(
        default=False,
        help_text=_("Turns Training & Certification on without editing the server env (env TRAINING_MODULE_ENABLED also turns it on)."),
    )
    audience = models.CharField(
        max_length=20,
        choices=TrainingAudience.choices,
        default=TrainingAudience.TEST_ACCOUNTS,
        help_text=_("Test accounts only: only flagged test faculty/students see Training. OICs, operators and admins of enabled equipment always can."),
    )
    course_demos_free = models.BooleanField(
        default=False,
        help_text=_(
            "Course/curricular demonstrations are free. Off: every demonstration is charged at the equipment's "
            "internal IITR rate and deducted from the faculty member's wallet."
        ),
    )
    updated_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Training module settings"
        verbose_name_plural = "Training module settings"

    def __str__(self) -> str:
        return f"Training module ({'on' if self.module_enabled else 'off'}, {self.get_audience_display()})"

    def save(self, *args, **kwargs):
        self.pk = self.SINGLETON_PK
        super().save(*args, **kwargs)

    @classmethod
    def current(cls) -> "TrainingModuleSettings":
        """Read-only view of the row; an unsaved default when it does not exist yet."""
        return cls.objects.filter(pk=cls.SINGLETON_PK).first() or cls(pk=cls.SINGLETON_PK)


class TrainingEquipmentSetting(models.Model):
    """Per-equipment Training enablement set by the Main Admin (UI, API or Django admin)."""

    equipment = models.OneToOneField(
        "equipment.Equipment", on_delete=models.CASCADE, related_name="training_setting"
    )
    enabled = models.BooleanField(default=False)
    updated_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [models.Index(fields=["enabled"])]

    def __str__(self) -> str:
        return f"Training {'on' if self.enabled else 'off'}: {self.equipment_id}"


# ---------------------------------------------------------------------------
# Competency assessment (practical checklist + theory score, ISO/IEC 17025-style authorisation record)
# ---------------------------------------------------------------------------
DEFAULT_CHECKLIST_ITEMS: list[dict] = [
    {"key": "safety", "label": "Safety induction: hazards, PPE, interlocks and emergency stop", "critical": True},
    {"key": "sop_startup", "label": "Start-up as per SOP (checks, warm-up, vacuum/gas/cooling as applicable)", "critical": True},
    {"key": "sample_prep", "label": "Sample preparation, mounting and loading", "critical": False},
    {"key": "calibration", "label": "Calibration / alignment / standard check", "critical": False},
    {"key": "acquisition", "label": "Method set-up and data acquisition", "critical": False},
    {"key": "data_handling", "label": "Data saving, naming, transfer and logbook entry", "critical": False},
    {"key": "troubleshooting", "label": "Recognises common faults and when to stop and call staff", "critical": False},
    {"key": "shutdown", "label": "Shutdown / standby as per SOP and clean-up", "critical": True},
    {"key": "emergency", "label": "Emergency response (spill, power failure, alarm) and reporting", "critical": True},
]


class CompetencyChecklist(models.Model):
    """Practical sign-off items for an equipment. A row without equipment is the institute default."""

    equipment = models.OneToOneField(
        "equipment.Equipment", on_delete=models.CASCADE, null=True, blank=True, related_name="competency_checklist"
    )
    items = models.JSONField(default=list, blank=True)
    theory_pass_pct = models.PositiveSmallIntegerField(default=70)
    practical_pass_pct = models.PositiveSmallIntegerField(default=80)
    updated_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)


class AssessmentResult(models.TextChoices):
    PASS = "PASS", _("Competent (pass)")
    RETAKE = "RETAKE", _("Not yet competent (retake)")
    FAIL = "FAIL", _("Not competent (fail)")


class Assessment(models.Model):
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="training_assessments")
    equipment = models.ForeignKey("equipment.Equipment", on_delete=models.PROTECT, related_name="training_assessments")
    event = models.ForeignKey(TrainingEvent, on_delete=models.SET_NULL, null=True, blank=True, related_name="assessments")
    registration = models.ForeignKey(Registration, on_delete=models.SET_NULL, null=True, blank=True, related_name="assessments")
    target_level = models.ForeignKey(CertificationLevel, on_delete=models.PROTECT, related_name="assessments")
    assessor = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="training_assessments_made")
    theory_score_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    practical_items = models.JSONField(default=list, blank=True)
    practical_score_pct = models.DecimalField(max_digits=5, decimal_places=2, null=True, blank=True)
    result = models.CharField(max_length=10, choices=AssessmentResult.choices)
    scope_note = models.TextField(blank=True, default="")
    remarks = models.TextField(blank=True, default="")
    award = models.ForeignKey(CertificationAward, on_delete=models.SET_NULL, null=True, blank=True, related_name="assessments")
    validity_months = models.PositiveSmallIntegerField(null=True, blank=True)
    prerequisite_waiver_reason = models.TextField(blank=True, default="")
    signed_off_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    signed_off_at = models.DateTimeField(null=True, blank=True)
    assessed_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-assessed_at", "-id"]
        indexes = [models.Index(fields=["user", "equipment"])]


# ---------------------------------------------------------------------------
# Operator duty: policy, roster, allocations, shifts
# ---------------------------------------------------------------------------
DEFAULT_DUTY_WEIGHTS: dict[str, float] = {
    "load": 40,
    "rotation": 15,
    "rotation_full_days": 30,
    "faculty_share": 15,
    "department_share": 10,
    "repeat": 5,
}


class OperatorPolicy(models.Model):
    """Versioned duty and fairness policy (global, department or equipment); the newest active row per scope applies."""

    scope = models.CharField(max_length=20, choices=PolicyScope.choices, default=PolicyScope.GLOBAL)
    department = models.ForeignKey(
        "users.Department", on_delete=models.CASCADE, null=True, blank=True, related_name="operator_policies"
    )
    equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.CASCADE, null=True, blank=True, related_name="operator_policies"
    )
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)

    selection_cooldown_days = models.PositiveSmallIntegerField(default=180)
    selection_cooldown_blocks = models.BooleanField(default=False)
    group_repeat_penalty = models.PositiveSmallIntegerField(default=5)

    duty_confirmation_required = models.BooleanField(default=True)
    duty_confirm_hours = models.PositiveSmallIntegerField(default=24)
    duty_reminder_hours = models.PositiveSmallIntegerField(default=6)
    duty_max_hours_week = models.PositiveSmallIntegerField(default=12)
    duty_max_hours_term = models.PositiveSmallIntegerField(default=150)
    duty_cooling_days = models.PositiveSmallIntegerField(default=2)
    duty_fairness_weights = models.JSONField(default=dict, blank=True)
    duty_hourly_rate = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))

    expiry_reminder_days = models.PositiveSmallIntegerField(default=30)
    notes = models.TextField(blank=True, default="")
    published_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["scope", "-version"]
        indexes = [models.Index(fields=["scope", "is_active"])]
        verbose_name_plural = "Operator policies"

    def __str__(self) -> str:
        target = self.equipment or self.department or "global"
        return f"Operator policy v{self.version} ({target})"

    def duty_weights(self) -> dict[str, float]:
        return {**DEFAULT_DUTY_WEIGHTS, **(self.duty_fairness_weights or {})}


class RosterSource(models.TextChoices):
    AWARD = "AWARD", _("Certified on this equipment")
    LEGACY_TA = "LEGACY_TA", _("Approved TA nomination")
    MANUAL = "MANUAL", _("Added by the OIC")


class RosterStatus(models.TextChoices):
    ACTIVE = "ACTIVE", _("Active")
    PAUSED = "PAUSED", _("Paused")
    REMOVED = "REMOVED", _("Removed")


class OperatorRosterEntry(models.Model):
    equipment = models.ForeignKey("equipment.Equipment", on_delete=models.CASCADE, related_name="operator_roster")
    user = models.ForeignKey(USER, on_delete=models.CASCADE, related_name="operator_roster_entries")
    source = models.CharField(max_length=20, choices=RosterSource.choices, default=RosterSource.AWARD)
    award = models.ForeignKey(CertificationAward, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    legacy_nomination = models.ForeignKey(
        "equipment.StudentEquipmentNomination", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    status = models.CharField(max_length=10, choices=RosterStatus.choices, default=RosterStatus.ACTIVE)
    status_reason = models.TextField(blank=True, default="")
    faculty = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    department = models.ForeignKey("users.Department", on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    max_hours_week = models.PositiveSmallIntegerField(null=True, blank=True)
    note = models.TextField(blank=True, default="")
    added_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["equipment", "user__name"]
        constraints = [models.UniqueConstraint(fields=["equipment", "user"], name="training_one_roster_entry_per_equipment")]


class DutyStatus(models.TextChoices):
    PENDING = "PENDING", _("Awaiting operator confirmation")
    CONFIRMED = "CONFIRMED", _("Confirmed")
    DECLINED = "DECLINED", _("Declined")
    EXPIRED = "EXPIRED", _("Released (not confirmed in time)")
    CANCELLED = "CANCELLED", _("Cancelled")
    COMPLETED = "COMPLETED", _("Completed")


class DutyAllocation(models.Model):
    equipment = models.ForeignKey("equipment.Equipment", on_delete=models.PROTECT, related_name="duty_allocations")
    operator = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="duty_allocations")
    roster_entry = models.ForeignKey(OperatorRosterEntry, on_delete=models.SET_NULL, null=True, blank=True, related_name="allocations")
    status = models.CharField(max_length=12, choices=DutyStatus.choices, default=DutyStatus.PENDING)
    requires_confirmation = models.BooleanField(default=True)
    confirm_by = models.DateTimeField(null=True, blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)
    response_channel = models.CharField(max_length=10, blank=True, default="")
    decline_reason = models.TextField(blank=True, default="")
    token_nonce = models.CharField(max_length=32, default=_verify_token)
    reminder_sent_at = models.DateTimeField(null=True, blank=True)
    escalated_at = models.DateTimeField(null=True, blank=True)
    title = models.CharField(max_length=255, blank=True, default="")
    note = models.TextField(blank=True, default="")
    suggested_rank = models.PositiveSmallIntegerField(null=True, blank=True)
    override_reason = models.TextField(blank=True, default="")
    fairness_snapshot = models.JSONField(default=dict, blank=True)
    academic_year = models.CharField(max_length=9, blank=True, default="")
    hourly_rate = models.DecimalField(max_digits=10, decimal_places=2, default=Decimal("0.00"))
    planned_minutes = models.PositiveIntegerField(default=0)
    allocated_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    cancel_reason = models.TextField(blank=True, default="")
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["equipment", "status"]),
            models.Index(fields=["operator", "status"]),
            models.Index(fields=["status", "confirm_by"]),
        ]

    @property
    def reference(self) -> str:
        return f"DA-{self.pk:04d}" if self.pk else "DA-new"


class ShiftStatus(models.TextChoices):
    SCHEDULED = "SCHEDULED", _("Scheduled")
    CHECKED_IN = "CHECKED_IN", _("On duty")
    COMPLETED = "COMPLETED", _("Completed")
    MISSED = "MISSED", _("Missed")
    RELEASED = "RELEASED", _("Released")
    CANCELLED = "CANCELLED", _("Cancelled")


class HoursSource(models.TextChoices):
    CHECKIN = "CHECKIN", _("Check-in / check-out")
    BOOKING = "BOOKING", _("Completed bookings in the shift")
    OIC = "OIC", _("Entered by the OIC")


class DutyShift(models.Model):
    allocation = models.ForeignKey(DutyAllocation, on_delete=models.CASCADE, related_name="shifts")
    equipment = models.ForeignKey("equipment.Equipment", on_delete=models.PROTECT, related_name="+")
    operator = models.ForeignKey(USER, on_delete=models.PROTECT, related_name="duty_shifts")
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    daily_slot_ids = models.JSONField(default=list, blank=True)
    status = models.CharField(max_length=12, choices=ShiftStatus.choices, default=ShiftStatus.SCHEDULED)
    check_in_at = models.DateTimeField(null=True, blank=True)
    check_out_at = models.DateTimeField(null=True, blank=True)
    checked_in_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    operated_minutes = models.PositiveIntegerField(null=True, blank=True)
    hours_source = models.CharField(max_length=10, choices=HoursSource.choices, blank=True, default="")
    verified_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    verified_at = models.DateTimeField(null=True, blank=True)
    remarks = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["start_at", "id"]
        indexes = [
            models.Index(fields=["operator", "start_at"]),
            models.Index(fields=["equipment", "start_at"]),
            models.Index(fields=["status", "end_at"]),
        ]

    @property
    def planned_minutes(self) -> int:
        return max(0, int((self.end_at - self.start_at).total_seconds() // 60))


class TrainingAuditLog(models.Model):
    actor = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    action = models.CharField(max_length=60)
    object_type = models.CharField(max_length=40)
    object_id = models.CharField(max_length=40)
    before = models.JSONField(default=dict, blank=True)
    after = models.JSONField(default=dict, blank=True)
    note = models.TextField(blank=True, default="")
    ip = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["object_type", "object_id"])]
