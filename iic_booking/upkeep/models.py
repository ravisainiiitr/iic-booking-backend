"""Equipment upkeep: preventive maintenance / calibration schedule, uptime target and early-warning alerts.

Downtime itself is not stored here: it comes from the disruption events staff already record (and from
Procurement & Assets maintenance records when that module is used), so nothing has to be entered twice.
"""

from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q
from django.utils.translation import gettext_lazy as _

USER = settings.AUTH_USER_MODEL


class PlanKind(models.TextChoices):
    PREVENTIVE = "PREVENTIVE", _("Preventive maintenance")
    CALIBRATION = "CALIBRATION", _("Calibration")
    SERVICE_VISIT = "SERVICE_VISIT", _("Service engineer visit (AMC / CMC)")
    SAFETY_CHECK = "SAFETY_CHECK", _("Safety check")
    CONSUMABLE = "CONSUMABLE", _("Consumable / part replacement")
    OTHER = "OTHER", _("Other")


class UpkeepSetting(models.Model):
    equipment = models.OneToOneField("equipment.Equipment", on_delete=models.CASCADE, related_name="upkeep_setting")
    uptime_target_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal("95.00"),
        help_text=_("Committed uptime over the last 90 days; below it an alert is raised."),
    )
    breakdown_alert_count = models.PositiveSmallIntegerField(
        default=3, help_text=_("Breakdowns within the window that raise an early-warning alert (0 = off)."),
    )
    breakdown_window_days = models.PositiveSmallIntegerField(default=90)
    updated_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"Upkeep setting {self.equipment_id}"


class MaintenancePlan(models.Model):
    equipment = models.ForeignKey("equipment.Equipment", on_delete=models.CASCADE, related_name="maintenance_plans")
    kind = models.CharField(max_length=16, choices=PlanKind.choices, default=PlanKind.PREVENTIVE)
    title = models.CharField(max_length=200)
    instructions = models.TextField(blank=True, default="")
    interval_days = models.PositiveIntegerField(help_text=_("Repeat every this many days."))
    lead_days = models.PositiveSmallIntegerField(default=14, help_text=_("Remind this many days before it is due."))
    last_done_on = models.DateField(null=True, blank=True)
    next_due_on = models.DateField()
    responsible = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    is_active = models.BooleanField(default=True)
    closed_reason = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["next_due_on", "pk"]
        indexes = [models.Index(fields=["equipment", "is_active", "next_due_on"])]

    def __str__(self) -> str:
        return f"{self.title} ({self.equipment_id})"


class PlanCompletion(models.Model):
    """One time the planned work was done. Never edited: a wrong entry is followed by a corrected one."""

    plan = models.ForeignKey(MaintenancePlan, on_delete=models.CASCADE, related_name="completions")
    done_on = models.DateField()
    done_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    notes = models.TextField(blank=True, default="")
    report_reference = models.CharField(max_length=120, blank=True, default="")
    next_due_on = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-done_on", "-pk"]


class AlertKind(models.TextChoices):
    PLAN_DUE = "PLAN_DUE", _("Maintenance due soon")
    PLAN_OVERDUE = "PLAN_OVERDUE", _("Maintenance overdue")
    UPTIME_BELOW_TARGET = "UPTIME_BELOW_TARGET", _("Uptime below target")
    REPEATED_BREAKDOWNS = "REPEATED_BREAKDOWNS", _("Repeated breakdowns")
    UPS_HEALTH = "UPS_HEALTH", _("UPS needs attention")


class AlertSeverity(models.TextChoices):
    INFO = "INFO", _("Info")
    WARNING = "WARNING", _("Warning")
    CRITICAL = "CRITICAL", _("Critical")


class ReliabilityAlert(models.Model):
    equipment = models.ForeignKey("equipment.Equipment", on_delete=models.CASCADE, related_name="reliability_alerts")
    kind = models.CharField(max_length=24, choices=AlertKind.choices)
    key = models.CharField(max_length=80, help_text=_("Identifies the condition so it is raised only once while open."))
    severity = models.CharField(max_length=8, choices=AlertSeverity.choices, default=AlertSeverity.WARNING)
    message = models.TextField()
    opened_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)
    resolved_at = models.DateTimeField(null=True, blank=True)
    acknowledged_by = models.ForeignKey(USER, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    acknowledge_note = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-opened_at"]
        constraints = [
            models.UniqueConstraint(fields=["equipment", "key"], condition=Q(resolved_at__isnull=True),
                                    name="upkeep_one_open_alert_per_key"),
        ]
        indexes = [models.Index(fields=["equipment", "resolved_at"])]
