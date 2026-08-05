"""Commissioning console API tests (Guacamole-free sync pipeline)."""

from __future__ import annotations

import logging

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APIClient

from iic_booking.remote_analysis.constants import CommandType, WorkstationStatus, WorkspaceSyncPhase
from iic_booking.remote_analysis.models import RemoteCommand
from iic_booking.remote_analysis.operations.commissioning import (
    EVT_COMMAND_QUEUED,
    prepare_workspace,
)
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
from iic_booking.remote_analysis.workspace_models import WorkspaceAudit

# Local settings load debug_toolbar; URLconf only registers `djdt` when DEBUG is
# True at import. Tests that flip DEBUG=True must suppress the toolbar so error
# pages are not rewritten via reverse("djdt:…").
_DISABLE_DEBUG_TOOLBAR = override_settings(
    DEBUG_TOOLBAR_CONFIG={"SHOW_TOOLBAR_CALLBACK": lambda request: False},
)


@pytest.fixture
def api(ra_user):
    client = APIClient()
    client.force_authenticate(user=ra_user)
    return client


@pytest.mark.django_db
def test_commissioning_html_and_json(api, eligible_workstation):
    html = api.get("/api/v1/analysis/operations/commissioning/?view=html")
    assert html.status_code == 200
    assert b"Sync Commissioning Console" in html.content
    assert b"Prepare Workspace" in html.content
    assert b"Collect Output" in html.content
    assert html["Content-Type"].startswith("text/html")

    js = api.get("/api/v1/analysis/operations/commissioning/")
    assert js.status_code == 200
    body = js.json()
    assert "workstations" in body
    assert "workspaces" in body
    assert body["poll_interval_seconds"] == 5
    assert any(w["id"] == str(eligible_workstation.id) for w in body["workstations"])


@pytest.mark.django_db
@_DISABLE_DEBUG_TOOLBAR
def test_commissioning_html_error_page_logs_and_renders_hint(api, monkeypatch, settings, caplog):
    """HTML path logs full traceback; page shows traceback only when DEBUG=True."""
    settings.DEBUG = True

    def boom(**kwargs):
        raise RuntimeError("simulated commissioning failure")

    monkeypatch.setattr(
        "iic_booking.remote_analysis.operations.commissioning.build_commissioning_payload",
        boom,
    )
    with caplog.at_level(logging.ERROR):
        html = api.get("/api/v1/analysis/operations/commissioning/?view=html")
    assert html.status_code == 500
    assert b"Commissioning Error" in html.content or b"Sync Commissioning Console" in html.content
    assert b"simulated commissioning failure" in html.content
    assert b"Traceback" in html.content
    assert "Commissioning console failed" in caplog.text


@pytest.mark.django_db
def test_commissioning_html_hides_traceback_when_debug_false(api, monkeypatch, settings, caplog):
    settings.DEBUG = False

    def boom(**kwargs):
        raise RuntimeError("secret path leak")

    monkeypatch.setattr(
        "iic_booking.remote_analysis.operations.commissioning.build_commissioning_payload",
        boom,
    )
    with caplog.at_level(logging.ERROR):
        html = api.get("/api/v1/analysis/operations/commissioning/?view=html")
    assert html.status_code == 500
    assert b"secret path leak" in html.content  # exception message ok
    assert b"Traceback (most recent call last)" not in html.content
    assert "Commissioning console failed" in caplog.text


@pytest.mark.django_db
@_DISABLE_DEBUG_TOOLBAR
def test_commissioning_json_missing_schema_hint(api, monkeypatch, settings):
    from django.db.utils import ProgrammingError

    settings.DEBUG = True

    def boom(**kwargs):
        raise ProgrammingError(
            "column remote_analysis_analysisworkspace.upload_verified_at does not exist"
        )

    monkeypatch.setattr(
        "iic_booking.remote_analysis.operations.commissioning.build_commissioning_payload",
        boom,
    )
    res = api.get("/api/v1/analysis/operations/commissioning/")
    assert res.status_code == 500
    body = res.json()
    assert "upload_verified_at" in body["detail"]
    assert "migrate remote_analysis" in body["hint"]
    assert body["traceback"]


@pytest.mark.django_db
def test_commissioning_json_omits_traceback_when_debug_false(api, monkeypatch, settings):
    settings.DEBUG = False

    def boom(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "iic_booking.remote_analysis.operations.commissioning.build_commissioning_payload",
        boom,
    )
    res = api.get("/api/v1/analysis/operations/commissioning/")
    assert res.status_code == 500
    body = res.json()
    assert body["detail"] == "boom"
    assert body["traceback"] is None


@pytest.mark.django_db
def test_commissioning_action_requires_workstation(api):
    res = api.post(
        "/api/v1/analysis/operations/commissioning/action/",
        {"action": "create", "booking_id": 1},
        format="json",
    )
    assert res.status_code == 400
    assert "workstation_id" in res.json()["detail"]


@pytest.mark.django_db
def test_commissioning_unauthenticated_json_keeps_drf_auth_error():
    client = APIClient()
    res = client.get("/api/v1/analysis/operations/commissioning/")
    assert res.status_code in {401, 403}
    body = res.json()
    assert "credentials" in body["detail"].lower() or "authentication" in body["detail"].lower()
    assert client.post(
        "/api/v1/analysis/operations/commissioning/action/",
        {"action": "refresh"},
        format="json",
    ).status_code in {401, 403}


@pytest.mark.django_db
def test_commissioning_anonymous_html_redirects_to_portal_login(settings):
    settings.FRONTEND_URL = "https://portal.example"
    client = APIClient()

    by_query = client.get("/api/v1/analysis/operations/commissioning/?view=html")
    assert by_query.status_code == 302
    loc = by_query["Location"]
    assert loc.startswith("https://portal.example/login?")
    assert "next=" in loc
    assert "token=" not in loc

    by_accept = client.get(
        "/api/v1/analysis/operations/commissioning/",
        HTTP_ACCEPT="text/html",
    )
    assert by_accept.status_code == 302
    assert by_accept["Location"].startswith("https://portal.example/login?")


@pytest.mark.django_db
def test_commissioning_session_admin_opens_html_without_token(ra_user, eligible_workstation):
    """Authenticated admin with a Django session can open the console directly."""
    client = APIClient()
    client.force_login(ra_user)
    res = client.get("/api/v1/analysis/operations/commissioning/?view=html")
    assert res.status_code == 200
    assert b"Sync Commissioning Console" in res.content
    assert str(eligible_workstation.hostname).encode() in res.content or b"Prepare Workspace" in res.content


@pytest.mark.django_db
def test_commissioning_portal_token_query_handoff_to_session(ra_user, eligible_workstation):
    """Portal UI can open ?view=html&token=… without pasting an Authorization header."""
    from rest_framework.authtoken.models import Token

    token, _ = Token.objects.get_or_create(user=ra_user)
    client = APIClient()

    first = client.get(
        f"/api/v1/analysis/operations/commissioning/?view=html&token={token.key}",
    )
    assert first.status_code == 302
    location = first["Location"]
    assert "token=" not in location.lower()
    assert "view=html" in location

    # Session established — subsequent HTML load works without token or Authorization.
    second = client.get("/api/v1/analysis/operations/commissioning/?view=html")
    assert second.status_code == 200
    assert b"Sync Commissioning Console" in second.content

    # JSON poll via session also works (credentials: same-origin).
    poll = client.get(
        "/api/v1/analysis/operations/commissioning/",
        HTTP_ACCEPT="application/json",
    )
    assert poll.status_code == 200
    assert "workstations" in poll.json()


@pytest.mark.django_db
def test_commissioning_json_ignores_query_token(ra_user):
    """Query-string tokens must not authenticate JSON/API requests."""
    from rest_framework.authtoken.models import Token

    token, _ = Token.objects.get_or_create(user=ra_user)
    client = APIClient()
    res = client.get(f"/api/v1/analysis/operations/commissioning/?token={token.key}")
    assert res.status_code in {401, 403}


@pytest.mark.django_db
def test_commissioning_header_token_still_works_for_json(ra_user, eligible_workstation):
    from rest_framework.authtoken.models import Token

    token, _ = Token.objects.get_or_create(user=ra_user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    res = client.get("/api/v1/analysis/operations/commissioning/")
    assert res.status_code == 200
    assert "workstations" in res.json()


@pytest.mark.django_db
def test_commissioning_prepare_collect_cleanup_actions(api, ra_user, eligible_workstation, reservation_window):
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

    prep = api.post(
        "/api/v1/analysis/operations/commissioning/action/",
        {"action": "prepare", "workspace_id": str(workspace.id)},
        format="json",
    )
    assert prep.status_code == 200, prep.content
    assert prep.json()["command_type"] == CommandType.PREPARE_WORKSTATION
    assert RemoteCommand.objects.filter(command_type=CommandType.PREPARE_WORKSTATION).exists()

    workspace.refresh_from_db()
    assert workspace.sync_phase == WorkspaceSyncPhase.DOWNLOADING_INPUT

    collect = api.post(
        "/api/v1/analysis/operations/commissioning/action/",
        {"action": "collect", "workspace_id": str(workspace.id)},
        format="json",
    )
    assert collect.status_code == 200, collect.content
    assert collect.json()["command_type"] == CommandType.COLLECT_WORKSPACE

    cleanup = api.post(
        "/api/v1/analysis/operations/commissioning/action/",
        {"action": "cleanup", "workspace_id": str(workspace.id)},
        format="json",
    )
    assert cleanup.status_code == 200, cleanup.content
    assert cleanup.json()["command_type"] == CommandType.CLEAN_WORKSTATION

    audits = WorkspaceAudit.objects.filter(workspace=workspace, details__contains="Commissioning:")
    assert audits.filter(details__contains=EVT_COMMAND_QUEUED).exists()


@pytest.mark.django_db
def test_commissioning_upload_sample(api, ra_user, eligible_workstation, reservation_window):
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

    upload = api.post(
        "/api/v1/analysis/operations/commissioning/action/",
        {
            "action": "upload",
            "workspace_id": str(workspace.id),
            "folder": "RawData",
            "file": SimpleUploadedFile("sample.txt", b"hello-commissioning", content_type="text/plain"),
        },
        format="multipart",
    )
    assert upload.status_code == 201, upload.content
    assert upload.json()["file"]["relative_path"].startswith("RawData/")
    assert upload.json()["file"]["sha256"]


@pytest.mark.django_db
def test_prepare_workspace_helper_sets_workstation_preparing(ra_user, eligible_workstation, reservation_window):
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

    cmd = prepare_workspace(workspace_id=str(workspace.id), actor=ra_user)
    assert cmd.command_type == CommandType.PREPARE_WORKSTATION
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.PREPARING
