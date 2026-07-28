"""Signals for Remote Analysis — seed RBAC permission definitions."""

from __future__ import annotations

import logging

from django.db.models.signals import post_migrate
from django.dispatch import receiver

from iic_booking.remote_analysis.constants import (
    PERMISSION_REMOTE_ANALYSIS_MANAGE,
    PERMISSION_REMOTE_ANALYSIS_VIEW,
)

logger = logging.getLogger(__name__)


@receiver(post_migrate)
def ensure_remote_analysis_permissions(sender, **kwargs):
    if sender.name != "iic_booking.remote_analysis":
        return
    try:
        from iic_booking.users.models.rbac import PermissionDefinition

        PermissionDefinition.objects.get_or_create(
            code=PERMISSION_REMOTE_ANALYSIS_MANAGE,
            defaults={
                "name": "Manage remote analysis workstations",
                "description": "Register, command, enable/disable, and maintain remote analysis workstations.",
            },
        )
        PermissionDefinition.objects.get_or_create(
            code=PERMISSION_REMOTE_ANALYSIS_VIEW,
            defaults={
                "name": "View remote analysis",
                "description": "View remote analysis dashboards, inventory, and health.",
            },
        )
    except Exception:
        logger.exception("Failed to seed remote analysis permission definitions")


@receiver(post_migrate)
def ensure_scheduler_periodic_tasks(sender, **kwargs):
    """Register Celery beat schedules for the reservation engine."""
    if sender.name != "iic_booking.remote_analysis":
        return
    try:
        from django_celery_beat.models import CrontabSchedule, IntervalSchedule, PeriodicTask

        interval_1m, _ = IntervalSchedule.objects.get_or_create(every=1, period=IntervalSchedule.MINUTES)
        interval_5m, _ = IntervalSchedule.objects.get_or_create(every=5, period=IntervalSchedule.MINUTES)
        interval_1h, _ = IntervalSchedule.objects.get_or_create(every=1, period=IntervalSchedule.HOURS)
        interval_1d, _ = IntervalSchedule.objects.get_or_create(every=1, period=IntervalSchedule.DAYS)

        tasks = [
            ("RAA Expire Reservations", "remote_analysis.expire_reservations", interval_1m),
            ("RAA Process Reservation Queue", "remote_analysis.process_reservation_queue", interval_1m),
            ("RAA Refresh Workstation Health", "remote_analysis.refresh_workstation_health", interval_5m),
            ("RAA Monitor Maintenance Windows", "remote_analysis.monitor_maintenance_windows", interval_5m),
            ("RAA Detect Reservation Conflicts", "remote_analysis.detect_reservation_conflicts", interval_5m),
            ("RAA Refresh Availability Snapshot", "remote_analysis.refresh_availability_snapshot", interval_5m),
            ("RAA Advance Preparing Sessions", "remote_analysis.advance_preparing_sessions", interval_1m),
            ("RAA Expire Desktop Sessions", "remote_analysis.expire_desktop_sessions", interval_1m),
            ("RAA Monitor Session Health", "remote_analysis.monitor_session_health", interval_1m),
            ("RAA Purge Expired Workspaces", "remote_analysis.purge_expired_workspaces", interval_5m),
            ("RAA Aggregate Hourly KPIs", "remote_analysis.aggregate_hourly_kpis", interval_1h),
            ("RAA Aggregate Daily Utilization", "remote_analysis.aggregate_daily_utilization", interval_1d),
            ("RAA Evaluate Alerts", "remote_analysis.evaluate_alerts", interval_5m),
            ("RAA Refresh Operations Dashboard", "remote_analysis.refresh_operations_dashboard", interval_5m),
            ("RAA Generate Weekly Reports", "remote_analysis.generate_weekly_reports", interval_1d),
            ("RAA Generate Monthly Reports", "remote_analysis.generate_monthly_reports", interval_1d),
            ("RAA Archive Old Metrics", "remote_analysis.archive_old_metrics", interval_1d),
            ("RAA Expire Invitations", "remote_analysis.expire_invitations", interval_5m),
            ("RAA Send Reservation Reminders", "remote_analysis.send_reservation_reminders", interval_5m),
        ]
        for name, task, schedule in tasks:
            PeriodicTask.objects.update_or_create(
                name=name,
                defaults={
                    "task": task,
                    "interval": schedule,
                    "crontab": None,
                    "enabled": True,
                },
            )
    except Exception:
        logger.exception("Failed to seed remote analysis scheduler periodic tasks")
