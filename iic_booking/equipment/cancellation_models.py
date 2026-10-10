"""Cancellation log (models). Imported at the end of ``iic_booking.equipment.models``.

One row per booking that ended cancelled, refunded, lab-disrupted or not utilized: when, by whom (role at the
time), why, how far ahead of the slot, what was refunded and which slots were given back. Written by
``booking_cancellation_log`` whenever a booking event moves a booking into one of those statuses; rows for
older bookings are rebuilt from booking history by ``backfill_booking_cancellations``.
"""

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _


class CancellationActorRole(models.TextChoices):
    USER = "USER", _("Booking user")
    SUPERVISOR = "SUPERVISOR", _("Supervisor / wallet owner")
    OIC = "OIC", _("Officer In Charge")
    LAB_OPERATOR = "LAB_OPERATOR", _("Lab Operator")
    DEPT_ADMIN = "DEPT_ADMIN", _("Department Administrator")
    MAIN_ADMIN = "MAIN_ADMIN", _("Main Administrator")
    OTHER_STAFF = "OTHER_STAFF", _("Other staff")
    SYSTEM = "SYSTEM", _("System (automatic)")
    UNKNOWN = "UNKNOWN", _("Not recorded")


class CancellationReason(models.TextChoices):
    USER_REQUEST = "USER_REQUEST", _("Cancelled by the user")
    STAFF_CANCEL = "STAFF_CANCEL", _("Cancelled by staff")
    STAFF_REFUND = "STAFF_REFUND", _("Refunded by staff")
    CANCELLATION_REQUEST = "CANCELLATION_REQUEST", _("Cancellation request approved")
    EQUIPMENT_DOWN = "EQUIPMENT_DOWN", _("Equipment under maintenance")
    OPERATOR_UNAVAILABLE = "OPERATOR_UNAVAILABLE", _("Operator unavailable")
    ANALYSIS_NOT_POSSIBLE = "ANALYSIS_NOT_POSSIBLE", _("Analysis not possible")
    DISRUPTION_DEADLINE = "DISRUPTION_DEADLINE", _("No choice after a disruption (automatic)")
    LAB_REJECTED_FILES = "LAB_REJECTED_FILES", _("Lab rejected the design files")
    URGENT_HOLD_RELEASED = "URGENT_HOLD_RELEASED", _("Urgent hold released or expired")
    NO_SHOW = "NO_SHOW", _("Booking not utilized (no-show)")
    OTHER = "OTHER", _("Other")


class CancellationDataQuality(models.TextChoices):
    RECORDED = "RECORDED", _("Recorded at the time")
    FROM_HISTORY = "FROM_HISTORY", _("Rebuilt from booking history")
    INFERRED = "INFERRED", _("Inferred from the booking status only")


class BookingCancellation(models.Model):
    booking = models.OneToOneField(
        "equipment.Booking",
        on_delete=models.CASCADE,
        related_name="cancellation_record",
    )
    cancelled_at = models.DateTimeField(db_index=True)
    cancelled_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    actor_role = models.CharField(
        max_length=20, choices=CancellationActorRole.choices, default=CancellationActorRole.UNKNOWN, db_index=True
    )
    reason = models.CharField(
        max_length=32, choices=CancellationReason.choices, default=CancellationReason.OTHER, db_index=True
    )
    note = models.TextField(blank=True, default="", help_text=_("Notes typed when cancelling, if any."))
    previous_status = models.CharField(max_length=30, blank=True, default="")
    new_status = models.CharField(max_length=30, blank=True, default="")
    charge_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=0, help_text=_("Amount paid for the booking when it was cancelled.")
    )
    refund_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True, help_text=_("Empty when the refund is not known.")
    )
    refund_estimated = models.BooleanField(
        default=False, help_text=_("Refund taken as the full charge because the amount was not recorded.")
    )
    slot_start = models.DateTimeField(null=True, blank=True)
    slot_end = models.DateTimeField(null=True, blank=True)
    lead_minutes = models.IntegerField(
        null=True,
        blank=True,
        db_index=True,
        help_text=_("Minutes between the cancellation and the first slot start (negative: after it started)."),
    )
    released_slot_ids = models.JSONField(default=list, blank=True)
    data_quality = models.CharField(
        max_length=16, choices=CancellationDataQuality.choices, default=CancellationDataQuality.RECORDED, db_index=True
    )
    event = models.ForeignKey(
        "equipment.BookingEvent",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-cancelled_at", "-id"]
        verbose_name = _("Booking cancellation")
        verbose_name_plural = _("Booking cancellations")
        indexes = [
            models.Index(fields=["cancelled_at", "reason"], name="equip_bcancel_at_reason"),
        ]

    def __str__(self):
        return f"Cancellation of booking {self.booking_id} ({self.reason})"
