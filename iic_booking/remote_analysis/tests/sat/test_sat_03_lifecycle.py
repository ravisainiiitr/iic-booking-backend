"""SAT-03 Workspace Lifecycle phases."""

from __future__ import annotations

import pytest

from iic_booking.remote_analysis.constants import (
    CommandType,
    WorkspaceSyncPhase,
    WorkstationStatus,
    normalize_sync_phase,
)
from iic_booking.remote_analysis.models import RemoteCommand
from iic_booking.remote_analysis.operations.commissioning import prepare_workspace, collect_workspace, cleanup_workspace
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService


@pytest.mark.sat
def test_sat_03_phase_normalization_queued():
    assert normalize_sync_phase("QUEUED") == WorkspaceSyncPhase.PREPARING
    assert normalize_sync_phase("DOWNLOADING") == WorkspaceSyncPhase.DOWNLOADING_INPUT
    assert normalize_sync_phase("READY") == WorkspaceSyncPhase.INPUT_READY


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_03_prepare_collect_cleanup_phase_chain(ra_user, eligible_workstation, reservation_window):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
        auto_allocate=False,
    )
    reservation.workstation = eligible_workstation
    reservation.save(update_fields=["workstation", "updated_at"])
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user, ingest=False)
    workspace.workstation = eligible_workstation
    workspace.save(update_fields=["workstation", "updated_at"])

    prepare_workspace(workspace_id=str(workspace.id), actor=ra_user)
    workspace.refresh_from_db()
    assert workspace.sync_phase == WorkspaceSyncPhase.DOWNLOADING_INPUT
    assert RemoteCommand.objects.filter(command_type=CommandType.PREPARE_WORKSTATION).exists()
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.PREPARING

    workspace.sync_phase = WorkspaceSyncPhase.INPUT_READY
    workspace.save(update_fields=["sync_phase", "updated_at"])

    collect_workspace(workspace_id=str(workspace.id), actor=ra_user)
    assert RemoteCommand.objects.filter(command_type=CommandType.COLLECT_WORKSPACE).exists()

    cleanup_workspace(workspace_id=str(workspace.id), actor=ra_user)
    workspace.refresh_from_db()
    assert workspace.sync_phase == WorkspaceSyncPhase.CLEANUP
    assert RemoteCommand.objects.filter(command_type=CommandType.CLEAN_WORKSTATION).exists()


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_03_failure_phase_values_exist():
    for phase in (
        WorkspaceSyncPhase.PREPARATION_FAILED,
        WorkspaceSyncPhase.UPLOAD_FAILED,
        WorkspaceSyncPhase.RETRY_PENDING,
        WorkspaceSyncPhase.CANCELLED,
        WorkspaceSyncPhase.COMPLETED,
        WorkspaceSyncPhase.CLEANUP,
    ):
        assert phase.value


@pytest.mark.sat_lab
@pytest.mark.django_db
def test_sat_03_live_phase_observation(sat_lab_enabled):
    pytest.skip("Lab: observe every phase via commissioning console while agent runs.")
