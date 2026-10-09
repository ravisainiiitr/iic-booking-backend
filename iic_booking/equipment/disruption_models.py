"""Disruption log (models). Imported at the end of ``iic_booking.equipment.models``.

A DisruptionEvent is one continuous period in which an equipment (or a run of its slots) could not be used:
Under Maintenance, Operator Absent, Scheduled Maintenance or Other Reasons. SlotStatusChangeLog keeps every
staff slot status change, including the ones that are not disruptions (Not Available, Reserved for External).
"""

import os
import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class DisruptionType(models.TextChoices):
    UNDER_MAINTENANCE = "UNDER_MAINTENANCE", _("Under Maintenance")
    OPERATOR_ABSENT = "OPERATOR_ABSENT", _("Operator Absent")
    SCHEDULED_MAINTENANCE = "SCHEDULED_MAINTENANCE", _("Scheduled Maintenance")
    OTHER = "OTHER", _("Other Reasons")


class DisruptionScope(models.TextChoices):
    EQUIPMENT = "EQUIPMENT", _("Whole equipment")
    SLOTS = "SLOTS", _("Selected slots")


class DisruptionSource(models.TextChoices):
    CHANGE_SLOT_STATUS = "CHANGE_SLOT_STATUS", _("Change slot status")
    DASHBOARD_CALENDAR = "DASHBOARD_CALENDAR", _("Dashboard calendar")
    BOOKING_DETAILS = "BOOKING_DETAILS", _("Booking details")
    EQUIPMENT_STATUS = "EQUIPMENT_STATUS", _("Equipment status")
    ADMIN_SLOT_API = "ADMIN_SLOT_API", _("Admin slot edit")
    DJANGO_ADMIN = "DJANGO_ADMIN", _("Django admin")
    BACKFILL = "BACKFILL", _("Recorded from earlier data")
    OTHER = "OTHER", _("Other")


class DisruptionEvent(models.Model):
    equipment = models.ForeignKey(
        "equipment.Equipment",
        on_delete=models.CASCADE,
        related_name="disruption_events",
    )
    disruption_type = models.CharField(max_length=32, choices=DisruptionType.choices, db_index=True)
    scope = models.CharField(max_length=16, choices=DisruptionScope.choices, default=DisruptionScope.SLOTS)
    source = models.CharField(max_length=32, choices=DisruptionSource.choices, default=DisruptionSource.OTHER)

    start_at = models.DateTimeField(db_index=True, help_text=_("Start of the disrupted period."))
    end_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("End of the disrupted period: last affected slot end, or when the equipment became operational."),
    )

    started_at = models.DateTimeField(default=timezone.now, help_text=_("When the disruption was recorded."))
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="disruption_events_started",
    )

    reason_category = models.CharField(max_length=32, blank=True, default="")
    reason = models.TextField(blank=True, default="")
    reason_updated_at = models.DateTimeField(null=True, blank=True)
    reason_updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    slots_affected = models.PositiveIntegerField(default=0)
    bookings_affected = models.PositiveIntegerField(
        default=0, help_text=_("Bookings cancelled, refunded or put on hold for a decision by this disruption.")
    )

    ended_at = models.DateTimeField(null=True, blank=True, db_index=True, help_text=_("When staff resumed it."))
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="disruption_events_ended",
    )
    end_source = models.CharField(max_length=32, choices=DisruptionSource.choices, blank=True, default="")

    action_taken = models.TextField(blank=True, default="")
    action_updated_at = models.DateTimeField(null=True, blank=True)
    action_updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )

    started_by_role = models.CharField(
        max_length=16, blank=True, default="", help_text=_("Role of the person who started it, at that time.")
    )
    ended_by_role = models.CharField(
        max_length=16, blank=True, default="", help_text=_("Role of the person who resumed it, at that time.")
    )
    expected_recovery_at = models.DateTimeField(
        null=True, blank=True, help_text=_("When the equipment or slots are expected back; empty = not announced.")
    )
    procurement_request_ids = models.JSONField(
        default=list, blank=True, help_text=_("Procurement & Assets requests raised from this disruption.")
    )

    backfilled = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Soft delete: hidden from history, reports and slot annotations; slots and bookings are not touched.
    is_deleted = models.BooleanField(default=False, db_index=True)
    deleted_at = models.DateTimeField(null=True, blank=True)
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    delete_reason = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-start_at", "-id"]
        verbose_name = _("Disruption event")
        verbose_name_plural = _("Disruption events")
        indexes = [
            models.Index(fields=["equipment", "disruption_type", "ended_at"], name="equip_disr_eq_type_end"),
        ]

    def __str__(self):
        return f"Disruption {self.pk} {self.disruption_type} equipment {self.equipment_id}"


class DisruptionEventSlot(models.Model):
    """A slot covered by a disruption. ``released_at`` is set when staff put the slot back (or changed it to a
    status that is not this disruption); the slot then counts only up to that moment."""

    event = models.ForeignKey(DisruptionEvent, on_delete=models.CASCADE, related_name="slot_links")
    daily_slot = models.ForeignKey(
        "equipment.DailySlot",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="disruption_links",
    )
    start_datetime = models.DateTimeField()
    end_datetime = models.DateTimeField()
    released_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["start_datetime", "id"]
        constraints = [
            models.UniqueConstraint(fields=["event", "daily_slot"], name="equip_disr_slot_event_slot_uniq"),
        ]
        indexes = [models.Index(fields=["daily_slot", "released_at"], name="equip_disr_slot_rel")]


def service_report_upload_to(instance, filename):
    ext = os.path.splitext(filename or "")[1].lower()
    now = timezone.now()
    return f"disruption_reports/{instance.event.equipment_id}/{now:%Y/%m}/{uuid.uuid4().hex}{ext}"


class DisruptionServiceReport(models.Model):
    """Optional service report attached when a disruption is resolved. Private storage; authenticated download only."""

    event = models.ForeignKey(DisruptionEvent, on_delete=models.CASCADE, related_name="service_reports")
    file = models.FileField(upload_to=service_report_upload_to, max_length=255)
    original_name = models.CharField(max_length=255)
    content_type = models.CharField(max_length=100)
    size_bytes = models.PositiveBigIntegerField(default=0)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-uploaded_at", "-id"]


class DisruptionEventEdit(models.Model):
    """Timeline of a disruption: created, extended, reason / action edits, report uploads, resumed."""

    event = models.ForeignKey(DisruptionEvent, on_delete=models.CASCADE, related_name="edits")
    kind = models.CharField(max_length=32)
    field = models.CharField(max_length=32, blank=True, default="")
    old_value = models.TextField(blank=True, default="")
    new_value = models.TextField(blank=True, default="")
    note = models.CharField(max_length=255, blank=True, default="")
    edited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    edited_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["edited_at", "id"]


class SlotStatusChangeLog(models.Model):
    """One staff slot status change (any status), e.g. Not Available or Reserved for External with its FBR reference."""

    equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.CASCADE, related_name="slot_status_change_logs"
    )
    new_status = models.CharField(max_length=32, db_index=True)
    previous_statuses = models.JSONField(default=dict, blank=True, help_text=_("Status -> number of slots before."))
    slot_ids = models.JSONField(default=list, blank=True)
    slot_count = models.PositiveIntegerField(default=0)
    first_start = models.DateTimeField(null=True, blank=True)
    last_end = models.DateTimeField(null=True, blank=True)
    label = models.CharField(max_length=255, blank=True, default="")
    external_reference = models.CharField(max_length=100, blank=True, default="")
    source = models.CharField(max_length=32, choices=DisruptionSource.choices, default=DisruptionSource.OTHER)
    bookings_affected = models.PositiveIntegerField(default=0)
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    changed_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-changed_at", "-id"]
