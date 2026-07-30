"""Celery/periodic task smoke coverage (WS3)."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.management import call_command
from django.utils import timezone

from iic_booking.remote_analysis import tasks
from iic_booking.remote_analysis.constants import ReservationStatus, WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation, MaintenanceWindow
from iic_booking.remote_analysis.services.tokens import issue_agent_token


@pytest.mark.django_db
def test_expire_and_process_queue_tasks(ra_user, reservation_window):
    start, end = reservation_window
    AnalysisReservation.objects.create(
        user=ra_user,
        status=ReservationStatus.QUEUED,
        requested_start=start,
        requested_end=end,
        priority=100,
    )
    assert isinstance(tasks.expire_reservations(), dict)
    assert isinstance(tasks.process_reservation_queue(limit=5), dict)


@pytest.mark.django_db
def test_refresh_health_and_availability_tasks(eligible_workstation):
    assert isinstance(tasks.refresh_workstation_health(), int)
    payload = tasks.refresh_availability_snapshot()
    assert "expired" in payload
    assert "utilization" in payload


@pytest.mark.django_db
def test_monitor_maintenance_windows_task(eligible_workstation):
    now = timezone.now()
    MaintenanceWindow.objects.create(
        workstation=eligible_workstation,
        reason="Nightly",
        start=now - timedelta(minutes=5),
        end=now + timedelta(hours=1),
        active=True,
    )
    result = tasks.monitor_maintenance_windows()
    assert result["applied"] >= 1
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.MAINTENANCE


@pytest.mark.django_db
def test_session_and_conflict_tasks(ra_settings):
    assert isinstance(tasks.detect_reservation_conflicts(), int)
    assert isinstance(tasks.advance_preparing_sessions(), dict)
    assert isinstance(tasks.expire_desktop_sessions(), dict)
    assert isinstance(tasks.monitor_session_health(), int)


@pytest.mark.django_db
def test_ops_aggregation_and_alert_tasks():
    assert "kpi_id" in tasks.aggregate_hourly_kpis()
    assert "utilization_rows" in tasks.aggregate_daily_utilization()
    assert isinstance(tasks.evaluate_alerts(), dict)
    assert "generated_at" in tasks.refresh_operations_dashboard()


@pytest.mark.django_db
def test_report_and_archive_tasks():
    weekly = tasks.generate_weekly_reports()
    assert "reports" in weekly
    monthly = tasks.generate_monthly_reports()
    assert "reports" in monthly
    assert isinstance(tasks.archive_old_metrics(days=1), dict)
    assert isinstance(tasks.expire_invitations(), dict)
    assert isinstance(tasks.send_reservation_reminders(), dict)
    assert isinstance(tasks.purge_expired_workspaces(), dict)


@pytest.mark.django_db
def test_sync_remote_analysis_settings_command():
    call_command("sync_remote_analysis_settings")


@pytest.mark.django_db
def test_compat_reexport_modules_importable():
    from iic_booking.remote_analysis import (
        allocation,
        availability,
        conflicts,
        queue,
        reservation,
        scheduler,
    )

    assert allocation
    assert availability
    assert conflicts
    assert queue
    assert reservation
    assert scheduler
