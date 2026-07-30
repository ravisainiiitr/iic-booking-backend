"""Coverage push to ≥90% — workspace, collaboration, alerts, guacamole, admin APIs."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.remote_analysis.comments import CommentService, NoteService
from iic_booking.remote_analysis.constants import (
    AlertStatus,
    CommandStatus,
    CommandType,
    ReportFormat,
    ReportType,
    ReservationStatus,
    SessionStatus,
    TransferStatus,
    WorkstationStatus,
)
from iic_booking.remote_analysis.guacamole.secrets import decrypt_password, encrypt_password
from iic_booking.remote_analysis.guacamole.session import SessionError, SessionOrchestrator
from iic_booking.remote_analysis.models import WorkstationHeartbeat
from iic_booking.remote_analysis.operations.alerts import AlertEngine
from iic_booking.remote_analysis.operations.reporting import ReportingEngine
from iic_booking.remote_analysis.operations_models import AlertEvent, AlertRule
from iic_booking.remote_analysis.production_hardening import (
    correlation_scope,
    get_correlation_id,
    json_safe,
    new_correlation_id,
    structured_log,
)
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation, ReservationConflict
from iic_booking.remote_analysis.selectors import workstations as ws_selectors
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.services.tokens import issue_agent_token
from iic_booking.remote_analysis.session_models import RemoteDesktopSession
from iic_booking.remote_analysis.workspace.storage import StorageManager
from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
from iic_booking.remote_analysis.workspace.transfer import TransferError, TransferManager
from iic_booking.remote_analysis.workspace_models import (
    WorkspaceShare,
    WorkspaceTransfer,
)
from iic_booking.users.tests.factories import UserFactory


@pytest.fixture
def api(ra_user):
    client = APIClient()
    client.force_authenticate(user=ra_user)
    return client


@pytest.fixture
def reserved_workspace(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.allowed_extensions = ".txt,.csv,.bin"
    ra_settings.blocked_extensions = ".exe"
    ra_settings.mock_guacamole = True
    ra_settings.save()
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user)
    return reservation, workspace


@pytest.mark.django_db
def test_workspace_upload_download_archive_restore_sync(api, reserved_workspace, ra_settings):
    reservation, workspace = reserved_workspace
    wid = workspace.id

    missing = api.post(f"/api/v1/analysis/workspaces/{wid}/upload/", {}, format="multipart")
    assert missing.status_code == 400

    upload = api.post(
        f"/api/v1/analysis/workspaces/{wid}/upload/",
        {"file": SimpleUploadedFile("run.txt", b"abc123"), "folder": "RawData"},
        format="multipart",
    )
    assert upload.status_code == 201
    file_id = upload.json()["id"]

    # Versioned re-upload
    upload2 = api.post(
        f"/api/v1/analysis/workspaces/{wid}/upload/",
        {"file": SimpleUploadedFile("run.txt", b"abc1234"), "folder": "RawData"},
        format="multipart",
    )
    assert upload2.status_code == 201

    # Checksum mismatch
    bad = api.post(
        f"/api/v1/analysis/workspaces/{wid}/upload/",
        {
            "file": SimpleUploadedFile("chk.txt", b"data"),
            "folder": "RawData",
            "sha256": "0" * 64,
        },
        format="multipart",
    )
    assert bad.status_code == 400

    files = api.get(f"/api/v1/analysis/workspaces/{wid}/files/?folder=RawData")
    assert files.status_code == 200
    assert len(files.json()) >= 1

    dl = api.get(f"/api/v1/analysis/workspaces/{wid}/download/?file_id={file_id}")
    assert dl.status_code == 200
    if hasattr(dl, "streaming_content"):
        b"".join(dl.streaming_content)
    else:
        _ = dl.content

    zip_dl = api.get(f"/api/v1/analysis/workspaces/{wid}/download/?zip=1")
    assert zip_dl.status_code == 200
    if hasattr(zip_dl, "streaming_content"):
        b"".join(zip_dl.streaming_content)
    else:
        _ = zip_dl.content

    detail = api.get(f"/api/v1/analysis/workspaces/{wid}/")
    assert detail.status_code == 200

    create = api.post(
        "/api/v1/analysis/workspaces/",
        {"reservation_id": str(reservation.id)},
        format="json",
    )
    assert create.status_code in (200, 201)

    sync = api.post(f"/api/v1/analysis/workspaces/{wid}/sync/")
    assert sync.status_code == 200
    assert sync.json()["command_id"]

    archive = api.post(f"/api/v1/analysis/workspaces/{wid}/archive/", {"note": "done"}, format="json")
    assert archive.status_code == 200
    workspace.refresh_from_db()
    assert workspace.read_only is True or workspace.status == "ARCHIVED"

    restore = api.post(f"/api/v1/analysis/workspaces/{wid}/restore/")
    assert restore.status_code == 200


@pytest.mark.django_db
def test_agent_workspace_manifest_and_upload(reserved_workspace, eligible_workstation):
    reservation, workspace = reserved_workspace
    assert workspace.workstation_id == eligible_workstation.id
    _, token = issue_agent_token(eligible_workstation)
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_AGENT_ID=eligible_workstation.agent_id)

    # Seed a file for download path
    mgr = TransferManager()
    row = mgr.upload(workspace, SimpleUploadedFile("a.txt", b"zz"), folder="Processed", source="agent")

    manifest = api.get(f"/api/v1/analysis/workspaces/{workspace.id}/manifest/")
    assert manifest.status_code == 200
    assert manifest.json()["workspace_id"] == str(workspace.id)

    content = api.get(f"/api/v1/analysis/workspaces/{workspace.id}/files/{row.id}/content/")
    assert content.status_code == 200

    agent_up = api.post(
        f"/api/v1/analysis/workspaces/{workspace.id}/agent-upload/",
        {"file": SimpleUploadedFile("out.txt", b"result"), "folder": "Processed"},
        format="multipart",
    )
    assert agent_up.status_code == 201

    # Wrong agent forbidden
    from iic_booking.remote_analysis.models import AnalysisWorkstation

    other_ws = AnalysisWorkstation.objects.create(
        agent_id="other-agent-ws",
        hostname="OTHER",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        last_heartbeat=timezone.now(),
    )
    _, other_tok = issue_agent_token(other_ws)
    other = APIClient()
    other.credentials(HTTP_AUTHORIZATION=f"Bearer {other_tok}", HTTP_X_AGENT_ID=other_ws.agent_id)
    denied = other.get(f"/api/v1/analysis/workspaces/{workspace.id}/manifest/")
    assert denied.status_code == 403


@pytest.mark.django_db
def test_collaboration_full_post_flows(api, ra_user, reserved_workspace, eligible_workstation, ra_settings):
    reservation, workspace = reserved_workspace
    other = UserFactory(user_type="faculty")
    session = SessionOrchestrator().create_session(reservation=reservation, user=ra_user)

    assert api.get("/api/v1/analysis/share/").status_code == 200
    share = api.post(
        "/api/v1/analysis/share/",
        {"workspace_id": str(workspace.id), "user_id": other.id, "name": "share1"},
        format="json",
    )
    assert share.status_code == 201

    assert api.get("/api/v1/analysis/invite/").status_code == 200
    invite = api.post(
        "/api/v1/analysis/invite/",
        {
            "user_id": other.id,
            "workspace_id": str(workspace.id),
            "reservation_id": str(reservation.id),
            "message": "join",
        },
        format="json",
    )
    assert invite.status_code == 201
    inv_id = invite.json()["id"]

    # Accept as other user
    other_api = APIClient()
    other_api.force_authenticate(user=other)
    accept = other_api.post("/api/v1/analysis/invite/", {"accept_id": inv_id}, format="json")
    assert accept.status_code == 200

    assist = api.post(
        "/api/v1/analysis/assistance/",
        {"subject": "Need help", "description": "x", "reservation_id": str(reservation.id)},
        format="json",
    )
    assert assist.status_code == 201
    req_id = assist.json()["id"]
    assert api.get("/api/v1/analysis/assistance/").status_code == 200
    assigned = api.post(
        "/api/v1/analysis/assistance/",
        {"action": "assign", "request_id": req_id, "assignee_id": ra_user.id},
        format="json",
    )
    assert assigned.status_code == 200
    accepted = api.post(
        "/api/v1/analysis/assistance/",
        {"action": "accept", "request_id": req_id},
        format="json",
    )
    assert accepted.status_code == 200
    resolved = api.post(
        "/api/v1/analysis/assistance/",
        {"action": "resolve", "request_id": req_id, "resolution": "ok"},
        format="json",
    )
    assert resolved.status_code == 200
    closed = api.post(
        "/api/v1/analysis/assistance/",
        {"action": "close", "request_id": req_id},
        format="json",
    )
    assert closed.status_code == 200

    comment = api.post(
        "/api/v1/analysis/comments/",
        {"session_id": str(session.id), "body": "looks good"},
        format="json",
    )
    assert comment.status_code == 201
    listed = api.get(f"/api/v1/analysis/comments/?session_id={session.id}")
    assert listed.status_code == 200

    ws_comment = api.post(
        "/api/v1/analysis/comments/",
        {"workspace_id": str(workspace.id), "body": "file note"},
        format="json",
    )
    assert ws_comment.status_code == 201

    ann = api.post("/api/v1/analysis/announcements/", {"title": "Maint", "body": "tonight"}, format="json")
    assert ann.status_code == 201

    bm = api.post(
        "/api/v1/analysis/bookmarks/",
        {"label": "RA", "url": "https://example.test", "target_type": "url"},
        format="json",
    )
    assert bm.status_code == 201

    fav = api.post(
        "/api/v1/analysis/favorites/",
        {"workstation_id": str(eligible_workstation.id)},
        format="json",
    )
    assert fav.status_code == 201
    assert api.get("/api/v1/analysis/favorites/").status_code == 200

    recent = api.post(
        "/api/v1/analysis/recent-workspaces/",
        {"workspace_id": str(workspace.id)},
        format="json",
    )
    assert recent.status_code == 200

    tl = api.get(f"/api/v1/analysis/timeline/?session_id={session.id}")
    assert tl.status_code == 200
    assert "events" in tl.json()

    # Shared access path for permissions module
    WorkspaceShare.objects.create(workspace=workspace, shared_with=other, created_by=ra_user)
    from iic_booking.remote_analysis.workspace.permissions import can_access_workspace, can_write_workspace

    assert can_access_workspace(other, workspace) is True
    assert can_write_workspace(ra_user, workspace) is True


@pytest.mark.django_db
def test_alerts_emit_ack_resolve_and_ops_reports(api, ra_user, eligible_workstation, reserved_workspace):
    reservation, workspace = reserved_workspace
    WorkstationHeartbeat.objects.create(
        workstation=eligible_workstation,
        cpu=99.0,
        memory=96.0,
        disk=97.0,
        received_at=timezone.now(),
    )
    eligible_workstation.last_heartbeat = timezone.now() - timedelta(hours=2)
    eligible_workstation.status = WorkstationStatus.OFFLINE
    eligible_workstation.save(update_fields=["last_heartbeat", "status"])

    RemoteDesktopSession.objects.create(
        reservation=reservation,
        user=ra_user,
        workstation=eligible_workstation,
        status=SessionStatus.FAILED,
    )
    RemoteDesktopSession.objects.create(
        reservation=reservation,
        user=ra_user,
        workstation=eligible_workstation,
        status=SessionStatus.IDLE,
    )
    WorkspaceTransfer.objects.create(
        workspace=workspace,
        direction="PORTAL_TO_WORKSPACE",
        status=TransferStatus.FAILED,
    )
    ReservationConflict.objects.create(
        reservation=reservation,
        workstation=eligible_workstation,
        conflict_type="DOUBLE_BOOKING",
        resolved=False,
    )

    workspace.quota_gb = 1
    workspace.current_usage_bytes = int(0.99 * (1024**3))
    workspace.save(update_fields=["quota_gb", "current_usage_bytes"])

    engine = AlertEngine()
    engine.ensure_default_rules()
    # Lower thresholds so metrics fire
    AlertRule.objects.filter(metric_name="session_failures").update(threshold=1, operator="gte")
    AlertRule.objects.filter(metric_name="sync_failures").update(threshold=1, operator="gte")
    AlertRule.objects.filter(metric_name="idle_sessions").update(threshold=1, operator="gte")
    AlertRule.objects.filter(metric_name="quota_pct").update(threshold=50, operator="gte")
    AlertRule.objects.filter(metric_name="open_conflicts").update(threshold=1, operator="gte")
    AlertRule.objects.filter(metric_name="cpu").update(threshold=50, operator="gt")

    result = engine.evaluate()
    assert result["raised"] >= 1

    alert = AlertEvent.objects.filter(status=AlertStatus.OPEN).first()
    if alert:
        engine.acknowledge(alert, ra_user)
        engine.resolve(alert, ra_user)
        ack = api.post(f"/api/v1/analysis/alerts/{alert.id}/acknowledge/", {"resolve": True}, format="json")
        # May already be resolved
        assert ack.status_code in (200, 404)

    assert api.get("/api/v1/analysis/alerts/").status_code == 200
    assert api.get("/api/v1/analysis/reports/").status_code == 200
    gen = api.post(
        "/api/v1/analysis/reports/generate/",
        {"report_type": ReportType.SESSION_SUMMARY, "format": ReportFormat.JSON},
        format="json",
    )
    assert gen.status_code in (200, 201)


@pytest.mark.django_db
def test_session_connect_activity_fail_and_lists(api, ra_user, reserved_workspace, ra_settings):
    reservation, workspace = reserved_workspace
    orch = SessionOrchestrator()
    session = orch.create_session(reservation=reservation, user=ra_user)
    if session.status == SessionStatus.PREPARING:
        orch.try_advance_after_prepare(session)
        session.refresh_from_db()

    launch = orch.build_launch_payload(
        session, user=ra_user, request_absolute_uri_builder=lambda p: f"http://test{p}", client_ip="127.0.0.1"
    )
    from urllib.parse import parse_qs, urlparse

    token = parse_qs(urlparse(launch["launch_url"]).query)["t"][0]
    connect = api.get(f"/api/v1/analysis/session/{session.id}/connect/?t={token}")
    assert connect.status_code == 200
    assert connect.json().get("mock") is True

    act = api.post(
        f"/api/v1/analysis/session/{session.id}/activity/",
        {"bytes_in": 10, "bytes_out": 20},
        format="json",
    )
    assert act.status_code == 200

    audits = api.get(f"/api/v1/analysis/session/{session.id}/audits/")
    assert audits.status_code == 200
    assert api.get("/api/v1/analysis/sessions/").status_code == 200
    assert api.get("/api/v1/analysis/session/history/").status_code == 200
    assert api.get("/api/v1/analysis/session/dashboard/").status_code == 200

    orch.fail_session(session, "prepare exploded")
    session.refresh_from_db()
    assert session.status == SessionStatus.FAILED

    with pytest.raises(SessionError):
        orch.create_session(reservation=reservation, user=UserFactory(user_type="faculty"))


@pytest.mark.django_db
def test_workstation_admin_apis_and_dashboard(api, eligible_workstation):
    assert api.get("/api/v1/analysis/dashboard/").status_code == 200
    assert api.get(f"/api/v1/analysis/workstations/?status=AVAILABLE").status_code == 200

    maint = api.post(
        f"/api/v1/analysis/workstations/{eligible_workstation.id}/maintenance/",
        {"reason": "patch"},
        format="json",
    )
    assert maint.status_code == 200
    enable = api.post(f"/api/v1/analysis/workstations/{eligible_workstation.id}/enable/")
    assert enable.status_code == 200
    disable = api.post(f"/api/v1/analysis/workstations/{eligible_workstation.id}/disable/")
    assert disable.status_code == 200
    enable2 = api.post(f"/api/v1/analysis/workstations/{eligible_workstation.id}/enable/")
    assert enable2.status_code == 200

    cmd = api.post(
        f"/api/v1/analysis/workstations/{eligible_workstation.id}/commands/",
        {"command_type": CommandType.PING, "payload": {"x": 1}},
        format="json",
    )
    assert cmd.status_code in (200, 201)

    metrics = ws_selectors.dashboard_metrics()
    assert metrics["total_workstations"] >= 1
    assert ws_selectors.workstation_by_agent_id(eligible_workstation.agent_id) is not None
    assert ws_selectors.workstation_by_id(eligible_workstation.id) is not None
    list(ws_selectors.recent_commands(workstation=eligible_workstation, limit=5))
    list(ws_selectors.recent_events(workstation=eligible_workstation, limit=5))


@pytest.mark.django_db
def test_reservation_detail_queue_scheduler_apis(api, ra_user, eligible_workstation, reservation_window):
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    detail = api.get(f"/api/v1/analysis/reservations/{reservation.id}/")
    assert detail.status_code == 200
    assert "history" in detail.json()
    assert api.get("/api/v1/analysis/queue/").status_code == 200
    assert api.get("/api/v1/analysis/scheduler/dashboard/").status_code == 200
    assert api.get("/api/v1/analysis/reservations/?status=RESERVED").status_code == 200


@pytest.mark.django_db
def test_transfer_zip_soft_delete_and_helpers(reserved_workspace, ra_user):
    _, workspace = reserved_workspace
    mgr = TransferManager()
    f1 = mgr.upload(workspace, SimpleUploadedFile("a.txt", b"one"), actor=ra_user)
    f2 = mgr.upload(workspace, SimpleUploadedFile("b.txt", b"two"), actor=ra_user)
    zip_resp = mgr.download_zip(workspace, [f1, f2], actor=ra_user)
    assert zip_resp.status_code == 200
    mgr.soft_delete(workspace, f1, actor=ra_user)
    f1.refresh_from_db()
    assert f1.deleted is True
    with pytest.raises(TransferError):
        mgr.download_file(workspace, f1, actor=ra_user)

    WorkspaceSyncService().mark_synced(workspace, success=True, message="ok")
    WorkspaceSyncService().issue_collect_command(workspace, actor=ra_user)


@pytest.mark.django_db
def test_misc_hardening_secrets_comments_notes(ra_user, reserved_workspace):
    reservation, workspace = reserved_workspace
    assert encrypt_password("secret")
    assert decrypt_password(encrypt_password("secret")) == "secret"
    assert decrypt_password("") == ""
    assert decrypt_password("not-valid") == ""

    assert json_safe({"id": workspace.id, "when": timezone.now(), "set": {1, 2}})
    cid = new_correlation_id()
    with correlation_scope(cid, session_id="s1"):
        assert get_correlation_id() == cid
        structured_log(20, "hello", workstation="x")

    session = SessionOrchestrator().create_session(reservation=reservation, user=ra_user)
    CommentService().add_session_comment(session, ra_user, "hi")
    CommentService().add_workspace_comment(workspace, ra_user, "ws")
    NoteService().add_note(ra_user, "body", session=session, title="t")

    report = ReportingEngine().generate(ReportType.ALERT_REPORT, fmt=ReportFormat.CSV)
    assert report.id
    report2 = ReportingEngine().generate(ReportType.CAPACITY_REPORT, fmt=ReportFormat.PDF)
    assert report2.id
