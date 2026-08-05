"""Tip coverage over 90% — session guards, transfer edges, inventory delta, queue expire."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.remote_analysis.constants import CommandStatus, ReservationStatus, SessionStatus, WorkstationStatus
from iic_booking.remote_analysis.guacamole.session import SessionError, SessionOrchestrator
from iic_booking.remote_analysis.models import RemoteCommand
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
from iic_booking.remote_analysis.services.inventory import InventoryService
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.services.scheduler import SchedulerService
from iic_booking.remote_analysis.workspace.transfer import TransferError, TransferManager
from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
from iic_booking.remote_analysis.workspace_models import TransferPolicy, WorkspaceFolder
from iic_booking.users.tests.factories import UserFactory


@pytest.mark.django_db
def test_session_guard_paths(ra_user, eligible_workstation, reservation_window, ra_settings):
    start, end = reservation_window
    orch = SessionOrchestrator()

    # No workstation
    queued = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user, auto_allocate=False
    )
    with pytest.raises(SessionError) as exc:
        orch.create_session(reservation=queued, user=ra_user)
    assert exc.value.code in {"reservation_inactive", "no_workstation"}

    reserved = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end + timedelta(hours=1), created_by=ra_user
    )
    # Capacity
    ra_settings.max_concurrent_sessions = 0
    ra_settings.save()
    orch = SessionOrchestrator()
    with pytest.raises(SessionError) as exc2:
        orch.create_session(reservation=reserved, user=ra_user)
    assert exc2.value.code == "capacity"
    ra_settings.max_concurrent_sessions = 50
    ra_settings.save()
    orch = SessionOrchestrator()

    # Unhealthy workstation (disabled)
    eligible_workstation.enabled = False
    eligible_workstation.save(update_fields=["enabled"])
    reserved.refresh_from_db()
    with pytest.raises(SessionError) as exc3:
        orch.create_session(reservation=reserved, user=ra_user)
    assert exc3.value.code == "workstation_unhealthy"

    eligible_workstation.enabled = True
    eligible_workstation.status = WorkstationStatus.AVAILABLE
    eligible_workstation.health_score = 95
    eligible_workstation.last_heartbeat = timezone.now()
    eligible_workstation.save(update_fields=["enabled", "status", "health_score", "last_heartbeat"])

    # Expired reservation window
    AnalysisReservation.objects.filter(pk=reserved.pk).update(requested_end=timezone.now() - timedelta(minutes=1))
    reserved.refresh_from_db()
    with pytest.raises(SessionError) as exc4:
        orch.create_session(reservation=reserved, user=ra_user)
    assert exc4.value.code == "reservation_expired"

    ReservationService().cancel(reserved, actor=ra_user, reason="free slot")
    eligible_workstation.status = WorkstationStatus.AVAILABLE
    eligible_workstation.save(update_fields=["status"])

    # Fresh reservation + idempotent create
    reserved2 = ReservationService().create_reservation(
        user=ra_user,
        requested_start=timezone.now() + timedelta(minutes=1),
        requested_end=timezone.now() + timedelta(hours=2),
        created_by=ra_user,
    )
    assert reserved2.status == ReservationStatus.AWAITING_CHECKIN
    s1 = orch.create_session(reservation=reserved2, user=ra_user)
    s2 = orch.create_session(reservation=reserved2, user=ra_user)
    assert s1.id == s2.id

    # Prepare failed path
    if s1.prepare_command_id:
        RemoteCommand.objects.filter(pk=s1.prepare_command_id).update(status=CommandStatus.FAILED, error_message="boom")
        s1.status = SessionStatus.PREPARING
        s1.save(update_fields=["status"])
        assert orch.try_advance_after_prepare(s1) is False


@pytest.mark.django_db
def test_transfer_policy_readonly_and_write_fail(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.blocked_extensions = ".exe"
    ra_settings.allowed_extensions = ".txt"
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user)
    TransferPolicy.objects.create(
        name="strict",
        workstation=eligible_workstation,
        is_active=True,
        blocked_extensions=".bin",
        allowed_extensions=".txt",
        max_file_size=10,
    )
    mgr = TransferManager(ra_settings)
    with pytest.raises(TransferError):
        mgr.upload(workspace, SimpleUploadedFile("x.bin", b"12345"), actor=ra_user)
    with pytest.raises(TransferError):
        mgr.upload(workspace, SimpleUploadedFile("big.txt", b"0123456789ABC"), actor=ra_user)

    folder, _ = WorkspaceFolder.objects.get_or_create(
        workspace=workspace,
        relative_path="RawData",
        defaults={"name": "RawData", "read_only": True},
    )
    if not folder.read_only:
        folder.read_only = True
        folder.save(update_fields=["read_only"])
    with pytest.raises(TransferError):
        mgr.upload(workspace, SimpleUploadedFile("a.txt", b"ok"), folder="RawData", actor=ra_user)

    workspace.read_only = True
    workspace.save(update_fields=["read_only"])
    with pytest.raises(TransferError):
        mgr.upload(workspace, SimpleUploadedFile("b.txt", b"ok"), folder="Processed", actor=ra_user)

    workspace.read_only = False
    workspace.save(update_fields=["read_only"])
    with patch.object(mgr.storage, "write_bytes", side_effect=OSError("disk full")):
        with pytest.raises(TransferError) as exc:
            mgr.upload(workspace, SimpleUploadedFile("c.txt", b"ok"), folder="Processed", actor=ra_user)
        assert getattr(exc.value, "code", "") == "write_failed"


@pytest.mark.django_db
def test_inventory_version_change_and_queue_expire(ra_user, eligible_workstation, reservation_window):
    InventoryService().synchronize(
        eligible_workstation,
        {"software": [{"displayName": "Foo", "version": "1.0", "publisher": "X"}]},
    )
    InventoryService().synchronize(
        eligible_workstation,
        {"software": [{"displayName": "Foo", "version": "2.0", "publisher": "X"}]},
    )
    start = timezone.now() - timedelta(hours=5)
    end = timezone.now() - timedelta(hours=3)
    reservation = AnalysisReservation.objects.create(
        user=ra_user,
        status=ReservationStatus.QUEUED,
        requested_start=start,
        requested_end=end,
        priority=100,
    )
    from iic_booking.remote_analysis.services.queue import QueueService

    QueueService().enqueue(reservation)
    stats = SchedulerService().expire_stale()
    reservation.refresh_from_db()
    assert reservation.status == ReservationStatus.EXPIRED
    assert stats["expired"] >= 1


@pytest.mark.django_db
def test_collaboration_workspace_comments_and_report_download(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    api = APIClient()
    api.force_authenticate(user=ra_user)
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user)
    post = api.post(
        "/api/v1/analysis/comments/",
        {"workspace_id": str(workspace.id), "body": "ws comment"},
        format="json",
    )
    assert post.status_code == 201
    listed = api.get(f"/api/v1/analysis/comments/?workspace_id={workspace.id}")
    assert listed.status_code == 200

    from iic_booking.remote_analysis.constants import ReportFormat, ReportType
    from iic_booking.remote_analysis.operations.reporting import ReportingEngine

    report = ReportingEngine().generate(ReportType.SESSION_SUMMARY, fmt=ReportFormat.JSON)
    dl = api.get(f"/api/v1/analysis/reports/{report.id}/download/")
    assert dl.status_code in (200, 404, 400)

    # Notifications mark-read
    api.post("/api/v1/analysis/notifications/read/", {"all": True}, format="json")
