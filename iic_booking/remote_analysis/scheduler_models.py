"""Milestone 3 — reservation / scheduler domain models."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from iic_booking.remote_analysis.constants import (
    DEFAULT_SCORING_WEIGHTS,
    AllocationRuleType,
    ConflictType,
    QueueEntryStatus,
    ReservationStatus,
)


class SoftwareRequirement(models.Model):
    """Software / resource profile required for a reservation or rule."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    software = models.CharField(max_length=512, blank=True, default="")
    minimum_version = models.CharField(max_length=128, blank=True, default="")
    required = models.BooleanField(default=True)
    optional = models.BooleanField(default=False)
    license_required = models.BooleanField(default=False)
    gpu_required = models.BooleanField(default=False)
    minimum_ram_gb = models.FloatField(default=0)
    minimum_storage_gb = models.FloatField(default=0)
    minimum_cpu_cores = models.PositiveIntegerField(default=0)
    operating_system = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = _("Software requirement")
        verbose_name_plural = _("Software requirements")

    def __str__(self) -> str:
        return self.name


class AllocationRule(models.Model):
    """Configurable allocation priority / scoring rules."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    rule_type = models.CharField(max_length=64, choices=AllocationRuleType.choices)
    priority_boost = models.IntegerField(default=0)
    weight_overrides = models.JSONField(default=dict, blank=True)
    department = models.ForeignKey(
        "users.Department",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="ra_allocation_rules",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="ra_allocation_rules",
    )
    is_active = models.BooleanField(default=True)
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-priority_boost", "name"]

    def __str__(self) -> str:
        return f"{self.name} ({self.rule_type})"

    def effective_weights(self) -> dict:
        weights = dict(DEFAULT_SCORING_WEIGHTS)
        if isinstance(self.weight_overrides, dict):
            weights.update({k: float(v) for k, v in self.weight_overrides.items()})
        return weights


class MaintenanceWindow(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workstation = models.ForeignKey(
        "remote_analysis.AnalysisWorkstation",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="maintenance_windows",
        help_text=_("Null = applies to all workstations"),
    )
    start = models.DateTimeField()
    end = models.DateTimeField()
    reason = models.CharField(max_length=512, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ra_maintenance_windows",
    )
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-start"]

    def __str__(self) -> str:
        target = self.workstation_id or "ALL"
        return f"Maintenance {target} {self.start}–{self.end}"


class AnalysisReservation(models.Model):
    """Portal-orchestrated workstation reservation (no Guacamole / RDP session)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    booking = models.ForeignKey(
        "equipment.Booking",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="analysis_reservations",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="analysis_reservations",
    )
    department = models.ForeignKey(
        "users.Department",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="analysis_reservations",
    )
    workstation = models.ForeignKey(
        "remote_analysis.AnalysisWorkstation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reservations",
    )
    status = models.CharField(
        max_length=32,
        choices=ReservationStatus.choices,
        default=ReservationStatus.REQUESTED,
        db_index=True,
    )
    requested_start = models.DateTimeField()
    requested_end = models.DateTimeField()
    reserved_start = models.DateTimeField(null=True, blank=True)
    reserved_end = models.DateTimeField(null=True, blank=True)
    allocated_at = models.DateTimeField(null=True, blank=True)
    released_at = models.DateTimeField(null=True, blank=True)
    priority = models.IntegerField(default=100, db_index=True)
    software_profile = models.ForeignKey(
        SoftwareRequirement,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="reservations",
    )
    requested_capabilities = models.JSONField(default=dict, blank=True)
    requested_resources = models.JSONField(default=dict, blank=True)
    allocation_score = models.FloatField(null=True, blank=True)
    allocation_notes = models.TextField(blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ra_reservations_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "requested_start"]),
            models.Index(fields=["workstation", "reserved_start", "reserved_end"]),
        ]
        verbose_name = _("Analysis reservation")
        verbose_name_plural = _("Analysis reservations")

    def __str__(self) -> str:
        return f"Reservation {self.id} ({self.status})"


class ReservationHistory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reservation = models.ForeignKey(
        AnalysisReservation,
        on_delete=models.CASCADE,
        related_name="history",
    )
    from_status = models.CharField(max_length=32, blank=True, default="")
    to_status = models.CharField(max_length=32, choices=ReservationStatus.choices)
    reason = models.CharField(max_length=512, blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class ReservationQueue(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reservation = models.OneToOneField(
        AnalysisReservation,
        on_delete=models.CASCADE,
        related_name="queue_entry",
    )
    status = models.CharField(
        max_length=32,
        choices=QueueEntryStatus.choices,
        default=QueueEntryStatus.WAITING,
        db_index=True,
    )
    priority = models.IntegerField(default=100, db_index=True)
    enqueued_at = models.DateTimeField(auto_now_add=True)
    dequeued_at = models.DateTimeField(null=True, blank=True)
    position_hint = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["priority", "enqueued_at"]  # lower priority number = higher priority; FIFO within
        verbose_name = _("Reservation queue entry")
        verbose_name_plural = _("Reservation queue")


class ReservationEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reservation = models.ForeignKey(
        AnalysisReservation,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="events",
    )
    event_type = models.CharField(max_length=64, db_index=True)
    details = models.TextField(blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class ReservationAudit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reservation = models.ForeignKey(
        AnalysisReservation,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audits",
    )
    action = models.CharField(max_length=128)
    details = models.TextField(blank=True, default="")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    success = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class ReservationConflict(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reservation = models.ForeignKey(
        AnalysisReservation,
        on_delete=models.CASCADE,
        related_name="conflicts",
    )
    conflicting_reservation = models.ForeignKey(
        AnalysisReservation,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="conflicted_by",
    )
    workstation = models.ForeignKey(
        "remote_analysis.AnalysisWorkstation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    conflict_type = models.CharField(max_length=64, choices=ConflictType.choices)
    resolution = models.CharField(max_length=512, blank=True, default="")
    resolved = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class ReservationPreference(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ra_reservation_preferences",
    )
    preferred_workstation = models.ForeignKey(
        "remote_analysis.AnalysisWorkstation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    preferred_building = models.CharField(max_length=255, blank=True, default="")
    preferred_capabilities = models.JSONField(default=dict, blank=True)
    notes = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Reservation preference")
        verbose_name_plural = _("Reservation preferences")


class SchedulerTelemetry(models.Model):
    """Aggregated scheduler telemetry samples."""

    id = models.BigAutoField(primary_key=True)
    metric_name = models.CharField(max_length=128, db_index=True)
    value = models.FloatField()
    unit = models.CharField(max_length=32, blank=True, default="")
    tags = models.JSONField(default=dict, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-recorded_at"]
        indexes = [models.Index(fields=["metric_name", "recorded_at"])]
