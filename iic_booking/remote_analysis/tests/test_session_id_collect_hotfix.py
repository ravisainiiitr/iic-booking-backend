"""Hotfix: COLLECT / SYNC / verified CLEAN target the session folder PREPARE created."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import pytest

from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile, Equipment
from iic_booking.remote_analysis.constants import (
    CommandType,
    TransferDirection,
    TransferStatus,
    WorkspaceStatus,
    WorkspaceSyncPhase,
    WorkstationStatus,
)
from iic_booking.remote_analysis.guacamole.cleanup import SessionCleanupService
from iic_booking.remote_analysis.guacamole.session import SessionOrchestrator
from iic_booking.remote_analysis.models import RemoteCommand
from iic_booking.remote_analysis.services.commands import CommandService
from iic_booking.remote_analysis.services.heartbeat import HeartbeatService
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.workspace.booking_ingest import BookingResultIngestService
from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType


def _booking_for(user):
    dept = Department.objects.create(name=f"D-{uuid4().hex[:6]}", code=f"D{uuid4().hex[:4].upper()}")
    eq = Equipment.objects.create(
        name="RA Hotfix EQ",
        code=f"RH{uuid4().hex[:4].upper()}",
        slot_duration_minutes=60,
        user_rating_enabled=False,
        internal_department=dept,
        enable_remote_analysis=True,
    )
    profile = ChargeProfile.objects.create(
        equipment=eq, user_type=UserType.STUDENT, primary_unit_charge=Decimal("10.00")
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


def _started_session(ra_user, reservation_window, ra_settings, tmp_path, booking=None):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.mock_guacamole = True
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user, booking=booking
    )
    session = SessionOrchestrator().create_session(reservation=reservation, user=ra_user)
    if session.prepare_command:
        CommandService().complete(session.prepare_command, success=True, message="prepared")
    session.refresh_from_db()
    return reservation, session


def _collect_commands(workspace):
    return RemoteCommand.objects.filter(
        command_type=CommandType.COLLECT_WORKSPACE, payload__workspace_id=str(workspace.id)
    ).order_by("created_at")


def _verified_cleanups(workstation):
    return RemoteCommand.objects.filter(
        workstation=workstation,
        command_type=CommandType.CLEAN_WORKSTATION,
        payload__reason="upload_verified",
    )


@pytest.mark.django_db
def test_collect_and_verified_cleanup_use_prepare_session_id(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    reservation, session = _started_session(ra_user, reservation_window, ra_settings, tmp_path)
    prepare_sid = session.prepare_command.payload["session_id"]
    assert prepare_sid == str(session.id)
    assert prepare_sid != str(reservation.id)

    SessionCleanupService().cleanup(session, reason="user end", actor=ra_user)
    workspace = AnalysisWorkspace.objects.get(reservation=reservation)
    collect = _collect_commands(workspace).last()
    assert collect.payload["session_id"] == prepare_sid
    assert collect.payload["manifest"]["session_id"] == prepare_sid
    session.refresh_from_db()
    assert session.cleanup_command.payload["session_id"] == prepare_sid

    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.CLEANING

    CommandService().complete(collect, success=True, message="Uploaded 3 files")
    workspace.refresh_from_db()
    assert workspace.sync_phase == WorkspaceSyncPhase.COMPLETED
    assert workspace.upload_verified_at is not None
    verified = _verified_cleanups(eligible_workstation).get()
    assert verified.payload["session_id"] == prepare_sid
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.AVAILABLE


@pytest.mark.django_db
def test_legacy_missing_session_folder_is_not_verified(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    reservation, session = _started_session(ra_user, reservation_window, ra_settings, tmp_path)
    SessionCleanupService().cleanup(session, reason="user end", actor=ra_user)
    workspace = AnalysisWorkspace.objects.get(reservation=reservation)
    collect = _collect_commands(workspace).last()

    CommandService().complete(collect, success=True, message="COLLECT_WORKSPACE: No local session folder to collect")
    workspace.refresh_from_db()
    assert workspace.upload_verified_at is None
    assert workspace.sync_phase == WorkspaceSyncPhase.RETRY_PENDING
    assert "SESSION_FOLDER_MISSING" in workspace.sync_message
    assert not _verified_cleanups(eligible_workstation).exists()
    assert WorkspaceSyncService().defer_output_cleanup(workspace) is True

    # Still held: a later CLEAN completion or idle heartbeat must not free the PC mid-retry.
    clean = session.cleanup_command
    CommandService().complete(clean, success=True, message="cleaned")
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.CLEANING
    HeartbeatService().process(eligible_workstation, {"CurrentStatus": "AVAILABLE"})
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.CLEANING


@pytest.mark.django_db
def test_new_agent_missing_folder_failure_code(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    reservation, session = _started_session(ra_user, reservation_window, ra_settings, tmp_path)
    SessionCleanupService().cleanup(session, reason="user end", actor=ra_user)
    workspace = AnalysisWorkspace.objects.get(reservation=reservation)
    CommandService().complete(_collect_commands(workspace).last(), success=False, message="SESSION_FOLDER_MISSING")
    workspace.refresh_from_db()
    assert workspace.upload_verified_at is None
    assert workspace.sync_phase == WorkspaceSyncPhase.RETRY_PENDING
    assert not _verified_cleanups(eligible_workstation).exists()


@pytest.mark.django_db
def test_retry_reuses_session_id_and_gives_up_after_max_retries(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    from iic_booking.remote_analysis.tasks import retry_failed_workspace_collects

    ra_settings.transfer_max_retries = 2
    reservation, session = _started_session(ra_user, reservation_window, ra_settings, tmp_path)
    SessionCleanupService().cleanup(session, reason="user end", actor=ra_user)
    workspace = AnalysisWorkspace.objects.get(reservation=reservation)

    for _attempt in range(3):
        collect = _collect_commands(workspace).last()
        assert collect.payload["session_id"] == str(session.id)
        CommandService().complete(collect, success=False, message="network down")
        workspace.refresh_from_db()
        if workspace.sync_phase == WorkspaceSyncPhase.UPLOAD_FAILED:
            break
        assert retry_failed_workspace_collects(limit=5)["retried"] == 1
        workspace.refresh_from_db()

    assert workspace.sync_phase == WorkspaceSyncPhase.UPLOAD_FAILED
    assert _collect_commands(workspace).count() == 3
    assert retry_failed_workspace_collects(limit=5)["retried"] == 0
    assert not _verified_cleanups(eligible_workstation).exists()
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.AVAILABLE


@pytest.mark.django_db
def test_mid_session_collect_never_cleans_live_folder(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    reservation, session = _started_session(ra_user, reservation_window, ra_settings, tmp_path)
    workspace = AnalysisWorkspace.objects.get(reservation=reservation)
    svc = WorkspaceSyncService()
    cmd = svc.issue_collect_command(workspace, actor=ra_user)
    assert cmd.payload["session_id"] == str(session.id)

    CommandService().complete(cmd, success=True, message="Uploaded 1 file")
    workspace.refresh_from_db()
    assert workspace.sync_phase == WorkspaceSyncPhase.SESSION_ACTIVE
    assert workspace.status == WorkspaceStatus.ACTIVE
    assert not _verified_cleanups(eligible_workstation).exists()


@pytest.mark.django_db
def test_mid_session_sync_command_uses_session_id(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    reservation, session = _started_session(ra_user, reservation_window, ra_settings, tmp_path)
    workspace = AnalysisWorkspace.objects.get(reservation=reservation)
    cmd = WorkspaceSyncService().issue_sync_command(workspace, actor=ra_user)
    assert cmd.payload["session_id"] == str(session.id)
    assert cmd.payload["manifest"]["session_id"] == str(session.id)


@pytest.mark.django_db
def test_ingest_does_not_rewind_phase_after_session_started(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    booking = _booking_for(ra_user)
    reservation, _session = _started_session(ra_user, reservation_window, ra_settings, tmp_path, booking=booking)
    workspace = AnalysisWorkspace.objects.get(reservation=reservation)
    WorkspaceSyncService().set_sync_phase(workspace, WorkspaceSyncPhase.SESSION_ACTIVE, percent=58, message="Session active")

    with patch(
        "iic_booking.equipment.booking_results_service.iter_dsa_zip_members",
        return_value=[("x/late.txt", b"late-bytes")],
    ), patch(
        "iic_booking.equipment.booking_results_service.iter_booking_result_zip_members",
        return_value=[],
    ):
        BookingResultIngestService().ingest(workspace, actor=ra_user)
    workspace.refresh_from_db()
    assert workspace.sync_phase == WorkspaceSyncPhase.SESSION_ACTIVE
    assert workspace.sync_progress_percent == 58


@pytest.mark.django_db
def test_repeat_launch_post_does_not_restage_or_reingest(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    from iic_booking.equipment.remote_analysis_integration.raw_staging import BookingRawStagingService
    from iic_booking.equipment.remote_analysis_integration.service import BookingRemoteAnalysisService

    ra_settings.workspace_root = str(tmp_path)
    ra_settings.mock_guacamole = True
    ra_settings.save()
    booking = _booking_for(ra_user)
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user, booking=booking
    )
    svc = BookingRemoteAnalysisService()
    svc.eligibility = SimpleNamespace(evaluate=lambda _b: SimpleNamespace(eligible=True, reason=""))

    with patch.object(BookingRemoteAnalysisService, "ensure_reservation", return_value=reservation), patch.object(
        BookingRawStagingService, "stage_into_workspace", return_value={"staged": 0}
    ) as stage, patch.object(BookingResultIngestService, "ingest", return_value={}) as ingest:
        first = svc.launch_session(booking, user=ra_user)
        assert stage.call_count == 1
        assert ingest.call_count == 1
        second = svc.launch_session(booking, user=ra_user)
        third = svc.launch_session(booking, user=ra_user)

    assert first["session_id"] == second["session_id"] == third["session_id"]
    assert stage.call_count == 1
    assert ingest.call_count == 1


@pytest.mark.django_db
def test_failed_collect_transfer_count_ignores_older_successes(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    reservation, _session = _started_session(ra_user, reservation_window, ra_settings, tmp_path)
    workspace = AnalysisWorkspace.objects.get(reservation=reservation)
    svc = WorkspaceSyncService()
    for status in (TransferStatus.FAILED, TransferStatus.COMPLETED, TransferStatus.FAILED):
        workspace.transfers.create(direction=TransferDirection.AGENT_PUSH, status=status)
    assert svc.collect_failures_since_success(workspace) == 1
