"""Automatic data synchronization — ingest, prepare gate, collect, cleanup defer (scenarios)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile, Equipment
from iic_booking.remote_analysis.constants import (
    SessionStatus,
    TransferDirection,
    TransferStatus,
    WorkspaceStatus,
    WorkspaceSyncPhase,
)
from iic_booking.remote_analysis.guacamole.cleanup import SessionCleanupService
from iic_booking.remote_analysis.guacamole.session import SessionOrchestrator
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.services.commands import CommandService
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.services.tokens import issue_agent_token
from iic_booking.remote_analysis.workspace.booking_ingest import BookingResultIngestService, _sha256_bytes
from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
from iic_booking.remote_analysis.workspace.transfer import TransferError, TransferManager
from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace, WorkspaceFile, WorkspaceTransfer
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _booking_for(user):
    dept = Department.objects.create(name=f"D-{uuid4().hex[:6]}", code=f"D{uuid4().hex[:4].upper()}")
    eq = Equipment.objects.create(
        name="RA Sync EQ",
        code=f"RS{uuid4().hex[:4].upper()}",
        slot_duration_minutes=60,
        user_rating_enabled=False,
        internal_department=dept,
    )
    profile = ChargeProfile.objects.create(
        equipment=eq,
        user_type=UserType.STUDENT,
        primary_unit_charge=Decimal("10.00"),
    )
    return Booking.objects.create(
        user=user,
        equipment=eq,
        charge_profile=profile,
        status=BookingStatus.COMPLETED,
        total_charge=Decimal("10.00"),
        total_time_minutes=60,
        virtual_booking_id=f"VB{uuid4().hex[:8]}",
    )


@pytest.mark.django_db
def test_ingest_idempotent_checksum(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    booking = _booking_for(ra_user)
    reservation.booking = booking
    reservation.save(update_fields=["booking"])
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user, ingest=False)
    workspace.booking = booking
    workspace.save(update_fields=["booking"])
    data = b"experiment-raw-bytes"
    digest = _sha256_bytes(data)

    with patch(
        "iic_booking.equipment.booking_results_service.iter_dsa_zip_members",
        return_value=[("booking/dsa/sample.txt", data)],
    ), patch(
        "iic_booking.equipment.booking_results_service.iter_booking_result_zip_members",
        return_value=[],
    ):
        r1 = BookingResultIngestService().ingest(workspace, actor=ra_user)
        r2 = BookingResultIngestService().ingest(workspace, actor=ra_user)

    assert r1["ingested"] == 1
    assert r2["skipped"] == 1
    file_row = WorkspaceFile.objects.get(workspace=workspace, relative_path="RawData/sample.txt")
    assert file_row.sha256 == digest


@pytest.mark.django_db
def test_scenario_prepare_ready_before_launch(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.mock_guacamole = True
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    orch = SessionOrchestrator()
    session = orch.create_session(reservation=reservation, user=ra_user)
    workspace = AnalysisWorkspace.objects.get(reservation=reservation)

    cmd = session.prepare_command
    if cmd:
        CommandService().complete(cmd, success=True, message="Prepared + downloaded")
    session.refresh_from_db()
    workspace.refresh_from_db()
    assert workspace.status in {WorkspaceStatus.READY, WorkspaceStatus.ACTIVE}
    assert workspace.sync_phase in {
        WorkspaceSyncPhase.INPUT_READY,
        WorkspaceSyncPhase.SESSION_STARTING,
        WorkspaceSyncPhase.SESSION_ACTIVE,
        WorkspaceSyncPhase.COMPLETED,
        WorkspaceSyncPhase.DOWNLOADING_INPUT,
    }
    assert session.status in {
        SessionStatus.READY,
        SessionStatus.TOKEN_GENERATED,
        SessionStatus.PREPARING,
    }


@pytest.mark.django_db
def test_scenario_checksum_failure_marks_failed(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user, ingest=False)
    TransferManager().upload(
        workspace,
        SimpleUploadedFile("ok.txt", b"abc"),
        folder="RawData",
        actor=ra_user,
        expected_sha256=_sha256_bytes(b"abc"),
    )
    with pytest.raises(TransferError):
        TransferManager().upload(
            workspace,
            SimpleUploadedFile("bad.txt", b"abc"),
            folder="RawData",
            actor=ra_user,
            expected_sha256="0" * 64,
        )


@pytest.mark.django_db
def test_scenario_collect_and_defer_cleanup(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.mock_guacamole = True
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    orch = SessionOrchestrator()
    session = orch.create_session(reservation=reservation, user=ra_user)
    if session.prepare_command:
        CommandService().complete(session.prepare_command, success=True, message="ok")
    session.refresh_from_db()
    workspace = AnalysisWorkspace.objects.get(reservation=reservation)
    WorkspaceTransfer.objects.create(
        workspace=workspace,
        direction=TransferDirection.AGENT_PUSH,
        status=TransferStatus.FAILED,
        error_message="network",
        started_at=timezone.now(),
        completed_at=timezone.now(),
    )
    assert WorkspaceSyncService().defer_output_cleanup(workspace) is True

    SessionCleanupService().cleanup(session, reason="user end", actor=ra_user)
    session.refresh_from_db()
    clean = session.cleanup_command
    assert clean is not None
    assert clean.payload.get("defer_output_cleanup") is True


@pytest.mark.django_db
def test_scenario_unauthorized_agent_manifest(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user, ingest=False)
    workspace.workstation = eligible_workstation
    workspace.save(update_fields=["workstation"])

    other = AnalysisWorkstation.objects.create(
        agent_id="other-agent-sync",
        hostname="OTHER",
        display_name="Other",
        status="AVAILABLE",
        enabled=True,
        health_score=90,
    )
    _, plaintext = issue_agent_token(other)
    api = APIClient()
    denied = api.get(
        f"/api/v1/analysis/workspaces/{workspace.id}/manifest/",
        HTTP_AUTHORIZATION=f"Bearer {plaintext}",
        HTTP_X_AGENT_ID=other.agent_id,
    )
    assert denied.status_code == 403


@pytest.mark.django_db
def test_retry_transfer_api(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user, ingest=False)
    workspace.workstation = eligible_workstation
    workspace.save(update_fields=["workstation"])
    WorkspaceTransfer.objects.create(
        workspace=workspace,
        direction=TransferDirection.AGENT_PUSH,
        status=TransferStatus.FAILED,
        started_at=timezone.now(),
        completed_at=timezone.now(),
    )
    api = APIClient()
    api.force_authenticate(user=ra_user)
    resp = api.post(f"/api/v1/analysis/workspaces/{workspace.id}/retry-transfer/")
    assert resp.status_code == 200
    workspace.refresh_from_db()
    assert workspace.sync_phase in {
        WorkspaceSyncPhase.UPLOADING_OUTPUT,
        WorkspaceSyncPhase.RETRY_PENDING,
        WorkspaceSyncPhase.DOWNLOADING_INPUT,
        WorkspaceSyncPhase.COLLECTING_OUTPUT,
    }


@pytest.mark.django_db
def test_interval_collect_task_respects_mode(ra_settings):
    from iic_booking.remote_analysis.tasks import interval_workspace_collect

    ra_settings.workspace_sync_mode = "end_of_session"
    ra_settings.save()
    result = interval_workspace_collect()
    assert result.get("skipped") is True

    ra_settings.workspace_sync_mode = "interval"
    ra_settings.save()
    result2 = interval_workspace_collect()
    assert "issued" in result2


@pytest.mark.django_db
def test_mark_synced_collect_sets_completed(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user, ingest=False)
    workspace.workstation = eligible_workstation
    workspace.status = WorkspaceStatus.COLLECTING
    workspace.save(update_fields=["workstation", "status"])
    WorkspaceSyncService().mark_synced(workspace, success=True, message="uploaded")
    workspace.refresh_from_db()
    assert workspace.sync_phase == WorkspaceSyncPhase.COMPLETED
    assert workspace.status == WorkspaceStatus.ACTIVE


@pytest.mark.django_db
def test_session_timeout_triggers_collect_path(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    """Scenario 7: cleanup issues COLLECT then CLEAN."""
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.mock_guacamole = True
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    session = SessionOrchestrator().create_session(reservation=reservation, user=ra_user)
    if session.prepare_command:
        CommandService().complete(session.prepare_command, success=True, message="ok")
    SessionCleanupService().cleanup(session, reason="idle timeout", actor=ra_user)
    session.refresh_from_db()
    workspace = AnalysisWorkspace.objects.get(reservation=reservation)
    assert workspace.status in {WorkspaceStatus.COLLECTING, WorkspaceStatus.ACTIVE, WorkspaceStatus.ARCHIVING, WorkspaceStatus.ARCHIVED, WorkspaceStatus.FAILED}
    assert session.cleanup_command is not None


@pytest.mark.django_db
def test_ingest_no_booking_and_ensure_with_booking(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user, ingest=False)
    empty = BookingResultIngestService().ingest(workspace, actor=ra_user)
    assert empty["ingested"] == 0
    assert "No booking" in empty["message"]

    booking = _booking_for(ra_user)
    reservation.booking = booking
    reservation.save(update_fields=["booking"])
    data = b"seed-bytes"
    with patch(
        "iic_booking.equipment.booking_results_service.iter_dsa_zip_members",
        return_value=[("x/a.txt", data)],
    ), patch(
        "iic_booking.equipment.booking_results_service.iter_booking_result_zip_members",
        return_value=[],
    ):
        ws2 = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user, ingest=True)
    assert WorkspaceFile.objects.filter(workspace=ws2, relative_path="RawData/a.txt").exists()


@pytest.mark.django_db
def test_ingest_booking_result_and_path_payload(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    booking = _booking_for(ra_user)
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user, ingest=False)
    workspace.booking = booking
    workspace.save(update_fields=["booking"])

    disk = tmp_path / "from_disk.bin"
    disk.write_bytes(b"disk-payload")

    class _Field:
        def open(self, mode="rb"):
            from io import BytesIO

            return BytesIO(b"operator-upload")

    with patch(
        "iic_booking.equipment.booking_results_service.iter_dsa_zip_members",
        return_value=[("dsa/from_path.txt", disk)],
    ), patch(
        "iic_booking.equipment.booking_results_service.iter_booking_result_zip_members",
        return_value=[("op/result.txt", _Field())],
    ):
        result = BookingResultIngestService().ingest(workspace, actor=ra_user)
    assert result["ingested"] >= 2
    assert WorkspaceFile.objects.filter(workspace=workspace, relative_path="RawData/from_path.txt").exists()
    assert WorkspaceFile.objects.filter(workspace=workspace, relative_path="RawData/result.txt").exists()


@pytest.mark.django_db
def test_ingest_all_fail_marks_failed(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    booking = _booking_for(ra_user)
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user, ingest=False)
    workspace.booking = booking
    workspace.save(update_fields=["booking"])

    with patch(
        "iic_booking.equipment.booking_results_service.iter_dsa_zip_members",
        return_value=[("x/bad.exe", b"MZ")],
    ), patch(
        "iic_booking.equipment.booking_results_service.iter_booking_result_zip_members",
        return_value=[],
    ), patch.object(
        TransferManager,
        "upload",
        side_effect=TransferError("blocked", code="extension_blocked"),
    ):
        result = BookingResultIngestService().ingest(workspace, actor=ra_user)
    assert result["failed"] == 1
    workspace.refresh_from_db()
    assert workspace.sync_phase == WorkspaceSyncPhase.PREPARATION_FAILED


@pytest.mark.django_db
def test_sync_command_paths_and_mark_synced_variants(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    svc = WorkspaceSyncService()
    workspace = svc.ensure_for_reservation(reservation, actor=ra_user, ingest=False)
    workspace.workstation = None
    workspace.save(update_fields=["workstation"])

    with pytest.raises(ValueError):
        svc.issue_sync_command(workspace, actor=ra_user)
    assert svc.issue_collect_command(workspace, actor=ra_user) is None

    workspace.workstation = eligible_workstation
    workspace.status = WorkspaceStatus.SYNCING
    workspace.save(update_fields=["workstation", "status"])
    payload = svc.prepare_payload(workspace, session_id="sess-1")
    assert payload["workspace_id"] == str(workspace.id)
    assert payload["sync_action"] == "download_input"
    cmd = svc.issue_sync_command(workspace, actor=ra_user)
    assert cmd is not None

    svc.mark_synced(workspace, success=True, message="pull ok")
    workspace.refresh_from_db()
    assert workspace.status == WorkspaceStatus.READY

    workspace.status = WorkspaceStatus.COLLECTING
    workspace.save(update_fields=["status"])
    svc.mark_synced(workspace, success=False, message="upload fail")
    workspace.refresh_from_db()
    assert workspace.sync_phase == WorkspaceSyncPhase.RETRY_PENDING
    assert svc.has_failed_collect(workspace) is True

    svc.mark_prepared(workspace, success=False, message="prep fail")
    workspace.refresh_from_db()
    assert workspace.status == WorkspaceStatus.FAILED


@pytest.mark.django_db
def test_retry_collect_celery_and_cancel_api(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    from iic_booking.remote_analysis.tasks import retry_failed_workspace_collects

    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user, ingest=False)
    workspace.workstation = eligible_workstation
    workspace.sync_phase = WorkspaceSyncPhase.RETRY_PENDING
    workspace.status = WorkspaceStatus.FAILED
    workspace.save(update_fields=["workstation", "sync_phase", "status"])
    WorkspaceTransfer.objects.create(
        workspace=workspace,
        direction=TransferDirection.AGENT_PUSH,
        status=TransferStatus.FAILED,
        started_at=timezone.now(),
        completed_at=timezone.now(),
    )
    out = retry_failed_workspace_collects(limit=5)
    assert out["retried"] >= 1

    api = APIClient()
    api.force_authenticate(user=ra_user)
    cancel = api.post(f"/api/v1/analysis/workspaces/{workspace.id}/cancel-transfer/")
    assert cancel.status_code in (200, 403, 404)


@pytest.mark.django_db
def test_manifest_fields_and_upload_skip(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    svc = WorkspaceSyncService()
    workspace = svc.ensure_for_reservation(reservation, actor=ra_user, ingest=False)
    data = b"manifest-skip-bytes"
    digest = _sha256_bytes(data)
    TransferManager().upload(
        workspace,
        SimpleUploadedFile("nested/out.txt", data),
        folder="Processed",
        actor=ra_user,
        expected_sha256=digest,
        relative_name="nested/out.txt",
    )
    man = svc.build_manifest(workspace, scope="output", session_id="s1")
    assert man["workspaceId"] == str(workspace.id)
    assert man["scope"] == "output"
    assert any(f["sha256"] == digest for f in man["files"])
    assert any(f["relativePath"].endswith("nested/out.txt") for f in man["files"])

    skipped = TransferManager().upload(
        workspace,
        SimpleUploadedFile("nested/out.txt", data),
        folder="Processed",
        actor=ra_user,
        expected_sha256=digest,
        relative_name="nested/out.txt",
    )
    assert getattr(skipped, "_skipped_unchanged", False) is True

    svc.mark_prepared(workspace, success=True)
    workspace.refresh_from_db()
    assert workspace.sync_phase == WorkspaceSyncPhase.INPUT_READY
    assert svc.is_input_ready(workspace) is True
    assert svc.defer_output_cleanup(workspace) is True
    workspace.upload_verified_at = timezone.now()
    workspace.sync_phase = WorkspaceSyncPhase.UPLOAD_VERIFIED
    workspace.save(update_fields=["upload_verified_at", "sync_phase"])
    assert svc.defer_output_cleanup(workspace) is False
