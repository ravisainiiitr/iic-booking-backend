"""Milestone 6 — Operations Center models (analytics, alerts, reports, capacity)."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from iic_booking.remote_analysis.constants import (
    AggregationPeriod,
    AlertCategory,
    AlertSeverity,
    AlertStatus,
    ReportFormat,
    ReportStatus,
    ReportType,
)


class WorkstationUtilization(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workstation = models.ForeignKey(
        "remote_analysis.AnalysisWorkstation",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="utilization_rows",
    )
    period = models.CharField(max_length=16, choices=AggregationPeriod.choices, db_index=True)
    period_start = models.DateTimeField(db_index=True)
    period_end = models.DateTimeField()
    uptime_hours = models.FloatField(default=0)
    session_hours = models.FloatField(default=0)
    idle_hours = models.FloatField(default=0)
    reservation_hours = models.FloatField(default=0)
    maintenance_hours = models.FloatField(default=0)
    availability_percent = models.FloatField(default=100)
    utilization_percent = models.FloatField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_start"]
        indexes = [models.Index(fields=["period", "period_start"])]
        unique_together = [("workstation", "period", "period_start")]


class SessionAnalytics(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    period = models.CharField(max_length=16, choices=AggregationPeriod.choices, db_index=True)
    period_start = models.DateTimeField(db_index=True)
    period_end = models.DateTimeField()
    total_sessions = models.PositiveIntegerField(default=0)
    average_duration_seconds = models.FloatField(default=0)
    longest_session_seconds = models.FloatField(default=0)
    shortest_session_seconds = models.FloatField(default=0)
    idle_percentage = models.FloatField(default=0)
    average_preparation_ms = models.FloatField(default=0)
    average_cleanup_ms = models.FloatField(default=0)
    average_sync_ms = models.FloatField(default=0)
    average_launch_ms = models.FloatField(default=0)
    average_disconnect_seconds = models.FloatField(default=0)
    reconnect_count = models.PositiveIntegerField(default=0)
    cancellation_rate = models.FloatField(default=0)
    no_show_rate = models.FloatField(default=0)
    success_rate = models.FloatField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_start"]
        unique_together = [("period", "period_start")]


class PerformanceMetric(models.Model):
    id = models.BigAutoField(primary_key=True)
    workstation = models.ForeignKey(
        "remote_analysis.AnalysisWorkstation",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="performance_metrics",
    )
    metric_name = models.CharField(max_length=128, db_index=True)
    value = models.FloatField()
    unit = models.CharField(max_length=32, blank=True, default="")
    period = models.CharField(max_length=16, choices=AggregationPeriod.choices, default=AggregationPeriod.HOURLY)
    period_start = models.DateTimeField(db_index=True)
    tags = models.JSONField(default=dict, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_start"]
        indexes = [models.Index(fields=["metric_name", "period_start"])]


class CapacitySnapshot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    period = models.CharField(max_length=16, choices=AggregationPeriod.choices, default=AggregationPeriod.HOURLY)
    period_start = models.DateTimeField(db_index=True)
    peak_concurrent_sessions = models.PositiveIntegerField(default=0)
    peak_reservation_demand = models.PositiveIntegerField(default=0)
    average_occupancy_percent = models.FloatField(default=0)
    unused_capacity_percent = models.FloatField(default=0)
    overbooked_periods = models.PositiveIntegerField(default=0)
    department_demand = models.JSONField(default=dict, blank=True)
    day_of_week_demand = models.JSONField(default=dict, blank=True)
    hour_of_day_demand = models.JSONField(default=dict, blank=True)
    predicted_capacity_need = models.FloatField(
        default=0,
        help_text=_("Simple trend extrapolation — not ML."),
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_start"]
        unique_together = [("period", "period_start")]


class AlertRule(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    category = models.CharField(max_length=32, choices=AlertCategory.choices)
    severity = models.CharField(max_length=16, choices=AlertSeverity.choices, default=AlertSeverity.WARNING)
    metric_name = models.CharField(max_length=128, blank=True, default="")
    operator = models.CharField(max_length=8, default="gt")  # gt, gte, lt, lte, eq
    threshold = models.FloatField(default=0)
    window_minutes = models.PositiveIntegerField(default=5)
    is_active = models.BooleanField(default=True)
    description = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class AlertEvent(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    rule = models.ForeignKey(
        AlertRule,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="events",
    )
    severity = models.CharField(max_length=16, choices=AlertSeverity.choices, default=AlertSeverity.WARNING)
    category = models.CharField(max_length=32, choices=AlertCategory.choices, default=AlertCategory.AGENT)
    status = models.CharField(max_length=16, choices=AlertStatus.choices, default=AlertStatus.OPEN, db_index=True)
    title = models.CharField(max_length=255)
    message = models.TextField(blank=True, default="")
    workstation = models.ForeignKey(
        "remote_analysis.AnalysisWorkstation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="alert_events",
    )
    acknowledged = models.BooleanField(default=False)
    acknowledged_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ra_alerts_acked",
    )
    acknowledged_at = models.DateTimeField(null=True, blank=True)
    resolved = models.BooleanField(default=False)
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ra_alerts_resolved",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ra_alerts_assigned",
    )
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class DashboardSnapshot(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    dashboard_key = models.CharField(max_length=64, db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    generated_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-generated_at"]
        indexes = [models.Index(fields=["dashboard_key", "generated_at"])]


class UsageTrend(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    metric_name = models.CharField(max_length=128, db_index=True)
    period = models.CharField(max_length=16, choices=AggregationPeriod.choices, db_index=True)
    period_start = models.DateTimeField(db_index=True)
    value = models.FloatField(default=0)
    unit = models.CharField(max_length=32, blank=True, default="")
    tags = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_start"]
        unique_together = [("metric_name", "period", "period_start")]


class AnalysisReport(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    report_type = models.CharField(max_length=64, choices=ReportType.choices)
    format = models.CharField(max_length=16, choices=ReportFormat.choices, default=ReportFormat.JSON)
    status = models.CharField(max_length=16, choices=ReportStatus.choices, default=ReportStatus.PENDING)
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)
    parameters = models.JSONField(default=dict, blank=True)
    payload = models.JSONField(default=dict, blank=True)
    # Relative path under MEDIA — never expose absolute paths
    storage_relpath = models.CharField(max_length=1024, blank=True, default="")
    generated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    error_message = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class OperationalKPI(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    period = models.CharField(max_length=16, choices=AggregationPeriod.choices, default=AggregationPeriod.HOURLY)
    period_start = models.DateTimeField(db_index=True)
    total_workstations = models.PositiveIntegerField(default=0)
    online_workstations = models.PositiveIntegerField(default=0)
    busy_workstations = models.PositiveIntegerField(default=0)
    available_workstations = models.PositiveIntegerField(default=0)
    average_utilization = models.FloatField(default=0)
    average_session_duration = models.FloatField(default=0)
    session_success_rate = models.FloatField(default=0)
    reservation_success_rate = models.FloatField(default=0)
    workspace_transfer_success = models.FloatField(default=0)
    average_preparation_ms = models.FloatField(default=0)
    average_cleanup_ms = models.FloatField(default=0)
    average_sync_ms = models.FloatField(default=0)
    average_launch_ms = models.FloatField(default=0)
    availability_percent = models.FloatField(default=100)
    current_queue_length = models.PositiveIntegerField(default=0)
    open_alerts = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_start"]
        unique_together = [("period", "period_start")]


class PeakUsageWindow(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    concurrent_sessions = models.PositiveIntegerField(default=0)
    concurrent_reservations = models.PositiveIntegerField(default=0)
    label = models.CharField(max_length=128, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-concurrent_sessions", "-period_start"]


class WorkstationAvailability(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workstation = models.ForeignKey(
        "remote_analysis.AnalysisWorkstation",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="availability_rows",
    )
    period = models.CharField(max_length=16, choices=AggregationPeriod.choices, db_index=True)
    period_start = models.DateTimeField(db_index=True)
    operational_availability = models.FloatField(default=100)
    maintenance_availability = models.FloatField(default=100)
    unexpected_downtime_hours = models.FloatField(default=0)
    mtbf_hours = models.FloatField(default=0)
    mttr_hours = models.FloatField(default=0)
    heartbeat_reliability = models.FloatField(default=100)
    reservation_success_rate = models.FloatField(default=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-period_start"]
        unique_together = [("workstation", "period", "period_start")]
