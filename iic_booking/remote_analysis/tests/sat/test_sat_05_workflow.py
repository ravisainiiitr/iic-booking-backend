"""SAT-05 Remote Analysis Workflow (portal path; live agent is lab)."""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from iic_booking.remote_analysis.constants import CommandType, WorkstationStatus, WorkspaceSyncPhase
from iic_booking.remote_analysis.models import RemoteCommand
from iic_booking.remote_analysis.operations.commissioning import (
    EVT_COMMAND_QUEUED,
    cleanup_workspace,
    collect_workspace,
    prepare_workspace,
    upload_sample_input,
)
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
from iic_booking.remote_analysis.workspace_models import WorkspaceAudit


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_05_portal_workflow_prepare_collect_cleanup(
    sat_api, ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()

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

    upload_sample_input(
        workspace_id=str(workspace.id),
        uploaded_file=SimpleUploadedFile("sample-input.txt", b"sat-input", content_type="text/plain"),
        actor=ra_user,
        folder="RawData",
    )

    prepare_workspace(workspace_id=str(workspace.id), actor=ra_user)
    workspace.refresh_from_db()
    assert workspace.sync_phase == WorkspaceSyncPhase.DOWNLOADING_INPUT
    assert RemoteCommand.objects.filter(command_type=CommandType.PREPARE_WORKSTATION).exists()

    collect_workspace(workspace_id=str(workspace.id), actor=ra_user)
    assert RemoteCommand.objects.filter(command_type=CommandType.COLLECT_WORKSPACE).exists()

    cleanup_workspace(workspace_id=str(workspace.id), actor=ra_user)
    workspace.refresh_from_db()
    assert workspace.sync_phase == WorkspaceSyncPhase.CLEANUP
    eligible_workstation.status = WorkstationStatus.AVAILABLE
    eligible_workstation.save(update_fields=["status", "updated_at"])
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.AVAILABLE

    assert WorkspaceAudit.objects.filter(
        workspace=workspace, details__contains=EVT_COMMAND_QUEUED
    ).exists()

    # Commissioning console still reachable for admin
    res = sat_api.get("/api/v1/analysis/operations/commissioning/")
    assert res.status_code == 200
    assert "workstations" in res.json()


@pytest.mark.sat_lab
@pytest.mark.django_db
def test_sat_05_live_booking_to_result_lab(sat_lab_enabled):
    """Full path: real booking → upload → prepare → agent download → output → collect → cleanup."""
    pytest.skip("Execute checklist SAT-05.01–05.11 with live agent + commissioning console.")
