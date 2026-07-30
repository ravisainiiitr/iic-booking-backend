"""SAT-06 Failure Recovery (automated fragments + lab gates)."""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from iic_booking.remote_analysis.constants import WorkspaceSyncPhase
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_06_09_corrupt_workspace_missing_workstation(ra_user, reservation_window):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user,
        requested_start=start,
        requested_end=end,
        created_by=ra_user,
        auto_allocate=False,
    )
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user, ingest=False)
    workspace.workstation = None
    workspace.save(update_fields=["workstation", "updated_at"])

    from iic_booking.remote_analysis.operations.commissioning import prepare_workspace

    with pytest.raises(ValueError, match="workstation"):
        prepare_workspace(workspace_id=str(workspace.id), actor=ra_user)


@pytest.mark.sat
@pytest.mark.django_db
def test_sat_06_06_partial_upload_not_verified(ra_user, eligible_workstation, reservation_window):
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
    assert workspace.upload_verified_at is None
    assert workspace.sync_phase != WorkspaceSyncPhase.UPLOAD_VERIFIED


@pytest.mark.sat_lab
@pytest.mark.django_db
def test_sat_06_infra_restarts_lab(sat_lab_enabled):
    pytest.skip("Lab: agent/portal/redis/db/network/disk/ACL scenarios per docs/sat/07-Recovery-Procedures.md")
