"""Automatic maintenance handling (models). Imported at the end of ``iic_booking.equipment.models``.

An EquipmentMaintenancePeriod runs from the moment equipment is marked Under Maintenance until it is Operational
again. It remembers which slots the mechanism converted (so only those go back to Available), the last day the
08:30 check handled, and, through MaintenanceNotification, which emails were sent so nobody is emailed twice for
the same booking and day.
"""

from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class EquipmentMaintenancePeriod(models.Model):
    equipment = models.ForeignKey(
        "equipment.Equipment", on_delete=models.CASCADE, related_name="maintenance_periods"
    )
    disruption_event = models.ForeignKey(
        "equipment.DisruptionEvent", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    started_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    ended_at = models.DateTimeField(null=True, blank=True, db_index=True)
    ended_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    last_checked_on = models.DateField(
        null=True, blank=True, help_text=_("Last day whose slots and bookings the 08:30 check handled.")
    )
    auto_slot_ids = models.JSONField(
        default=list, blank=True, help_text=_("Slots this mechanism set to Under Maintenance.")
    )
    adopted = models.BooleanField(
        default=False, help_text=_("Created for equipment that was already Under Maintenance before this mechanism.")
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-started_at", "-id"]
        verbose_name = _("Equipment maintenance period")
        verbose_name_plural = _("Equipment maintenance periods")
        indexes = [models.Index(fields=["equipment", "ended_at"], name="equip_maint_period_eq_end")]

    def __str__(self):
        return f"Maintenance period {self.pk} equipment {self.equipment_id}"

    @property
    def is_active(self) -> bool:
        return self.ended_at is None


class MaintenanceNotificationKind(models.TextChoices):
    MARKED = "MARKED", _("Equipment under maintenance: upcoming booking at risk")
    DAY = "DAY", _("Booking on a maintenance day: on hold")
    RESOLVED = "RESOLVED", _("Equipment operational: booking stands")
    STAFF_MARKED = "STAFF_MARKED", _("Staff summary: marked under maintenance")
    STAFF_DAY = "STAFF_DAY", _("Staff summary: daily check")
    STAFF_RESOLVED = "STAFF_RESOLVED", _("Staff summary: operational")
    RESCHEDULED = "RESCHEDULED", _("Booking rescheduled automatically after maintenance")
    STAFF_RESCHEDULED = "STAFF_RESCHEDULED", _("Staff summary: bookings rescheduled automatically")
    STAFF_UNPLACED = "STAFF_UNPLACED", _("Staff summary: bookings still waiting for a slot")


class MaintenanceRescheduleSettings(models.Model):
    """Singleton: how bookings on hold for equipment maintenance are moved once the equipment works again."""

    min_lead_hours = models.PositiveIntegerField(
        default=24,
        verbose_name=_("Minimum notice before the new slot (hours)"),
        help_text=_("The new slot starts at least this long after the reschedule, and never on the same day."),
    )
    horizon_days = models.PositiveIntegerField(
        default=28,
        verbose_name=_("Search window (days)"),
        help_text=_(
            "How far ahead a free slot is searched for, including weeks not yet open to users. Bookings that do "
            "not fit stay on hold and are retried at the next 08:30 check; the OIC is told."
        ),
    )
    deadline_extension_hours = models.PositiveIntegerField(
        default=24,
        verbose_name=_("Sample deadline / Not Utilized extension (hours)"),
        help_text=_(
            "Added to the sample submission deadline (never later than the slot start) and to the Booking Not "
            "Utilized threshold of a booking rescheduled automatically."
        ),
    )
    cancel_grace_hours = models.PositiveIntegerField(
        default=24,
        verbose_name=_("Full-refund cancellation window after the reschedule (hours)"),
        help_text=_(
            "The user may cancel for a full refund until the normal cancellation cutoff of the new slot, or for "
            "this many hours after the reschedule email if that is later (never after the slot start)."
        ),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Maintenance reschedule settings")
        verbose_name_plural = _("Maintenance reschedule settings")

    def __str__(self):
        return (
            f"Notice {self.min_lead_hours} h, window {self.horizon_days} days, "
            f"extension {self.deadline_extension_hours} h"
        )

    @classmethod
    def current(cls) -> "MaintenanceRescheduleSettings":
        row = cls.objects.order_by("pk").first()
        return row if row is not None else cls()


class MaintenanceNotification(models.Model):
    """One email (plus in-app notice) sent by the maintenance mechanism; ``dedupe_key`` is unique per period."""

    period = models.ForeignKey(EquipmentMaintenancePeriod, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=20, choices=MaintenanceNotificationKind.choices, db_index=True)
    booking = models.ForeignKey(
        "equipment.Booking", on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    for_date = models.DateField(null=True, blank=True)
    dedupe_key = models.CharField(max_length=120)
    sent_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["sent_at", "id"]
        constraints = [
            models.UniqueConstraint(fields=["period", "dedupe_key"], name="equip_maint_notif_period_key_uniq"),
        ]

    def __str__(self):
        return f"{self.kind} {self.dedupe_key}"
