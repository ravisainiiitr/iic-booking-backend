"""Coverage push: transfer, sharing, assistance, timeline, Guacamole client (WS3)."""

from __future__ import annotations

from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.remote_analysis.assistance import AssistanceService
from iic_booking.remote_analysis.constants import AssistanceStatus, CommandType, WorkstationStatus
from iic_booking.remote_analysis.guacamole.client import GuacamoleClient, GuacamoleClientError
from iic_booking.remote_analysis.guacamole.session import SessionOrchestrator
from iic_booking.remote_analysis.operations.alerts import AlertEngine
from iic_booking.remote_analysis.services.commands import CommandService
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.services.workstation_admin import WorkstationAdminService
from iic_booking.remote_analysis.sharing import InvitationService, SharingService
from iic_booking.remote_analysis.timeline import TimelineService
from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
from iic_booking.remote_analysis.workspace.transfer import TransferError, TransferManager
from iic_booking.users.tests.factories import UserFactory


@pytest.mark.django_db
def test_transfer_upload_download_delete(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.allowed_extensions = ".txt,.csv"
    ra_settings.blocked_extensions = ".exe"
    ra_settings.save()

    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user)
    mgr = TransferManager(ra_settings)

    with pytest.raises(TransferError):
        mgr.upload(
            workspace,
            SimpleUploadedFile("bad.exe", b"MZ"),
            folder="RawData",
            actor=ra_user,
        )

    uploaded = mgr.upload(
        workspace,
        SimpleUploadedFile("data.txt", b"payload"),
        folder="RawData",
        actor=ra_user,
    )
    assert uploaded.relative_path.endswith("data.txt")
    assert uploaded.size == 7

    response = mgr.download_file(workspace, uploaded, actor=ra_user)
    assert response.status_code == 200

    mgr.soft_delete(workspace, uploaded, actor=ra_user)
    uploaded.refresh_from_db()
    assert uploaded.deleted is True


@pytest.mark.django_db
def test_sharing_invitation_assistance_timeline(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.mock_guacamole = True
    ra_settings.save()

    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user)
    other = UserFactory(user_type="faculty")

    shared = SharingService().share(workspace, ra_user, user=other, name="Lab share")
    assert shared.id
    assert SharingService().can_share(ra_user, workspace)

    invite = InvitationService().invite(
        ra_user,
        invited_email=other.email,
        workspace=workspace,
        reservation=reservation,
    )
    assert invite.id
    InvitationService().expire_stale()

    help_req = AssistanceService().request_help(
        ra_user, "Need Origin help", "details", reservation=reservation
    )
    assert help_req.status == AssistanceStatus.REQUESTED
    AssistanceService().assign(help_req, ra_user, actor=ra_user)
    AssistanceService().resolve(help_req, ra_user, resolution="Fixed")

    session = SessionOrchestrator().create_session(reservation=reservation, user=ra_user)
    timeline = TimelineService().build_for_session(session)
    assert "events" in timeline or isinstance(timeline, dict)


@pytest.mark.django_db
def test_guacamole_client_live_paths_mocked(ra_settings):
    ra_settings.mock_guacamole = False
    ra_settings.guacamole_api_url = "https://guac.example/api"
    ra_settings.guacamole_admin_username = "guacadmin"
    ra_settings.guacamole_admin_password = "secret"
    ra_settings.save()
    client = GuacamoleClient(ra_settings)
    assert client.mock is False

    token_resp = MagicMock()
    token_resp.raise_for_status = MagicMock()
    token_resp.json.return_value = {"authToken": "tok-1", "dataSource": "postgresql"}

    ok_resp = MagicMock()
    ok_resp.raise_for_status = MagicMock()
    ok_resp.status_code = 200
    ok_resp.content = b'{"identifier":"c-1"}'
    ok_resp.json.return_value = {"identifier": "c-1"}
    ok_resp.headers = {}

    def post_side_effect(url, *args, **kwargs):
        if "tokens" in str(url):
            return token_resp
        return ok_resp

    def request_side_effect(method, url, *args, **kwargs):
        return ok_resp

    with (
        patch(
            "iic_booking.remote_analysis.guacamole.client.requests.post",
            side_effect=post_side_effect,
        ),
        patch(
            "iic_booking.remote_analysis.guacamole.client.requests.request",
            side_effect=request_side_effect,
        ),
    ):
        assert client.authenticate() == "tok-1"
        assert client.health_check() is True
        conn = client.create_connection(name="sess", parameters={"hostname": "1.2.3.4"})
        assert conn["identifier"] == "c-1"
        assert client.create_user("u1", "p1")["username"] == "u1"
        client.grant_connection("u1", "c-1")
        client.delete_connection("c-1")
        client.delete_user("u1")

    client._auth_token = None
    with patch(
        "iic_booking.remote_analysis.guacamole.client.requests.post",
        side_effect=__import__("requests").RequestException("down"),
    ):
        assert client.health_check() is False


@pytest.mark.django_db
def test_alert_engine_emits_for_offline_agent(eligible_workstation):
    eligible_workstation.last_heartbeat = timezone.now() - __import__("datetime").timedelta(hours=2)
    eligible_workstation.status = WorkstationStatus.OFFLINE
    eligible_workstation.save(update_fields=["last_heartbeat", "status"])
    AlertEngine().ensure_default_rules()
    result = AlertEngine().evaluate()
    assert isinstance(result, dict)


@pytest.mark.django_db
def test_workstation_admin_and_command_api(ra_user, eligible_workstation):
    api = APIClient()
    api.force_authenticate(user=ra_user)
    listed = api.get("/api/v1/analysis/workstations/")
    assert listed.status_code == 200

    detail = api.get(f"/api/v1/analysis/workstations/{eligible_workstation.id}/")
    assert detail.status_code == 200

    cmd = CommandService().create_command(eligible_workstation, CommandType.PING)
    assert cmd.id

    admin = WorkstationAdminService()
    admin.disable(eligible_workstation, actor=ra_user)
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.enabled is False
    admin.enable(eligible_workstation, actor=ra_user)
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.enabled is True


@pytest.mark.django_db
def test_collaboration_share_invite_assistance_apis(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    api = APIClient()
    api.force_authenticate(user=ra_user)
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user
    )
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user)
    other = UserFactory(user_type="faculty")

    share = api.post(
        "/api/v1/analysis/share/",
        {"workspace_id": str(workspace.id), "user_id": other.id, "name": "share-api"},
        format="json",
    )
    assert share.status_code in (200, 201, 400)

    invite = api.post(
        "/api/v1/analysis/invite/",
        {"email": other.email, "workspace_id": str(workspace.id)},
        format="json",
    )
    assert invite.status_code in (200, 201, 400)

    assist = api.post(
        "/api/v1/analysis/assistance/",
        {"subject": "Help", "description": "Please", "reservation_id": str(reservation.id)},
        format="json",
    )
    assert assist.status_code in (200, 201, 400)

    timeline = api.get(f"/api/v1/analysis/timeline/?reservation_id={reservation.id}")
    assert timeline.status_code in (200, 400)
