"""SAT-09 Database Integrity."""

from __future__ import annotations

import pytest
from django.db.models import Count

from iic_booking.remote_analysis.constants import WorkspaceSyncPhase
from iic_booking.remote_analysis.models import AnalysisWorkstation, RemoteCommand
from iic_booking.remote_analysis.operations.commissioning import prepare_workspace
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_09_01_02_phase_and_timestamps(ra_user, eligible_workstation, reservation_window):
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
    assert workspace.created_at is not None

    prepare_workspace(workspace_id=str(workspace.id), actor=ra_user)
    workspace.refresh_from_db()
    assert workspace.sync_phase == WorkspaceSyncPhase.DOWNLOADING_INPUT
    assert workspace.updated_at >= workspace.created_at
    cmd = RemoteCommand.objects.filter(workstation=eligible_workstation).latest("created_at")
    assert cmd.created_at is not None


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_09_03_foreign_keys(ra_user, eligible_workstation, reservation_window):
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

    workspace = AnalysisWorkspace.objects.select_related("workstation", "reservation").get(pk=workspace.pk)
    assert workspace.workstation_id == eligible_workstation.id
    assert workspace.reservation_id == reservation.id
    assert AnalysisWorkstation.objects.filter(pk=workspace.workstation_id).exists()


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_09_no_duplicate_agent_rows():
    dupes = (
        AnalysisWorkstation.objects.values("agent_id")
        .annotate(c=Count("id"))
        .filter(c__gt=1)
    )
    assert list(dupes) == []
