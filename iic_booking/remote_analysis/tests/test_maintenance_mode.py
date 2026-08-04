"""Maintenance mode + fleet exclusion tests."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.remote_analysis import tasks
from iic_booking.remote_analysis.constants import MaintenanceKind, WorkstationStatus
from iic_booking.remote_analysis.scheduler_models import MaintenanceWindow
from iic_booking.remote_analysis.services.availability import AvailabilityEngine
from iic_booking.remote_analysis.services.maintenance import MaintenanceService


@pytest.mark.django_db
def test_monitor_applies_and_restores_maintenance(eligible_workstation):
    now = timezone.now()
    window = MaintenanceWindow.objects.create(
        workstation=eligible_workstation,
        kind=MaintenanceKind.CALIBRATION,
        reason="Annual calibration",
        start=now - timedelta(minutes=5),
        end=now + timedelta(hours=1),
        active=True,
    )
    result = tasks.monitor_maintenance_windows()
    assert result["applied"] >= 1
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.CALIBRATION

    window.end = now - timedelta(minutes=1)
    window.save(update_fields=["end"])
    result2 = tasks.monitor_maintenance_windows()
    assert result2["restored"] >= 1
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status in {
        WorkstationStatus.AVAILABLE,
        WorkstationStatus.ONLINE,
    }
    window.refresh_from_db()
    assert window.active is False


@pytest.mark.django_db
def test_calibration_status_blocks_allocation(eligible_workstation, reservation_window):
    start, end = reservation_window
    eligible_workstation.status = WorkstationStatus.CALIBRATION
    eligible_workstation.save(update_fields=["status", "updated_at"])
    result = AvailabilityEngine().evaluate(eligible_workstation, start, end)
    assert result.available is False
    assert any("not allocatable" in r.lower() or "status" in r.lower() for r in result.reasons)


@pytest.mark.django_db
def test_schedule_maintenance_with_metadata(eligible_workstation, ra_user):
    end = timezone.now() + timedelta(hours=2)
    window = MaintenanceService().schedule(
        workstation=eligible_workstation,
        kind=MaintenanceKind.SOFTWARE_UPDATE,
        start=timezone.now(),
        end=end,
        reason="CasaXPS patch",
        description="Apply vendor patch set",
        assigned_engineer="CIF Engineer",
        amc_reference="AMC-2026-014",
        ticket_number="INC-7781",
        maintenance_notes="Coordinate with XPS lab",
        actor=ra_user,
    )
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.SOFTWARE_UPDATE
    assert window.ticket_number == "INC-7781"
    assert window.amc_reference == "AMC-2026-014"


@pytest.mark.django_db
def test_fleet_dashboard_counts(eligible_workstation):
    eligible_workstation.status = WorkstationStatus.HARDWARE_FAULT
    eligible_workstation.save(update_fields=["status", "updated_at"])
    payload = MaintenanceService().fleet_dashboard()
    assert payload["total_analysis_pcs"] >= 1
    assert payload["faulty"] >= 1
    assert "active_windows" in payload


@pytest.mark.django_db
def test_next_compatible_availability_when_all_in_maintenance(eligible_workstation):
    end = timezone.now() + timedelta(hours=3)
    MaintenanceService().schedule(
        workstation=eligible_workstation,
        kind=MaintenanceKind.MAINTENANCE,
        start=timezone.now(),
        end=end,
        reason="Scheduled maintenance",
    )
    hint = MaintenanceService().next_compatible_availability(
        matching_workstation_ids=[eligible_workstation.id]
    )
    assert hint["all_under_maintenance"] is True
    assert hint["reason"]
    assert hint.get("estimated_availability_display")
