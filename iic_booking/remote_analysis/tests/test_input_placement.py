"""Input data saved where the user chooses on the Analysis PC (PLACE_INPUT), instead of a fixed agent folder."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.equipment.remote_analysis_integration import input_placement
from iic_booking.remote_analysis.constants import CommandType
from iic_booking.remote_analysis.models import RemoteCommand
from iic_booking.remote_analysis.tests.test_phase2_workspace_redesign import _agent_client, _client, _url  # noqa: F401
from iic_booking.remote_analysis.tests.test_session_id_collect_hotfix import _booking_for, _started_session
from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace
from iic_booking.users.tests.factories import UserFactory

CAPS = ["collect_plan_v1", "progress_v1", "verified_cleanup_v1", "extra_sources_v1", "place_input_v1"]


def _caps(workstation, caps):
    workstation.agent_capabilities = caps
    workstation.save(update_fields=["agent_capabilities"])


def _setup(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path, caps=CAPS):
    booking = _booking_for(ra_user)
    _started_session(ra_user, reservation_window, ra_settings, tmp_path, booking=booking)
    _caps(eligible_workstation, caps)
    return booking


def _complete(workstation, cmd, *, success=True, result=None, message="ok"):
    agent = _agent_client(workstation)
    agent.get("/api/v1/analysis/commands/")
    resp = agent.post(
        f"/api/v1/analysis/commands/{cmd.id}/complete/",
        {"success": success, "message": message, "result": result or {}},
        format="json",
    )
    assert resp.status_code == 200


@pytest.mark.django_db
def test_get_reports_support_default_folder_and_no_placement_yet(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    booking = _setup(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path)
    data = _client(ra_user).get(_url(booking, "input-placement")).data
    assert data["supported"] is True
    assert data["default_place"] == "Desktop"
    assert data["default_folder_name"] == input_placement.clean_folder_name(booking.virtual_booking_id)
    assert data["placement"] is None


@pytest.mark.django_db
def test_place_on_desktop_round_trip_through_the_agent(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    booking = _setup(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path)
    api = _client(ra_user)
    url = _url(booking, "input-placement")

    started = api.post(url, {"place": "Desktop", "folder_name": "  Run 1: XRD  "}, format="json")
    assert started.status_code == 202
    assert started.data["folder_name"] == "Run 1_ XRD"
    cmd = RemoteCommand.objects.get(pk=started.data["request_id"])
    assert cmd.command_type == CommandType.PLACE_INPUT
    assert cmd.payload["place"] == "Desktop" and cmd.payload["open"] is True and cmd.payload["session_id"]
    assert api.get(url).data["placement"]["status"] == "pending"

    _complete(
        eligible_workstation,
        cmd,
        result={"path": r"C:\Users\analyst\Desktop\Run 1_ XRD", "display_path": "Desktop › Run 1_ XRD", "files": 3, "bytes": 2048, "opened": True},
    )
    placement = api.get(url).data["placement"]
    assert placement["status"] == "done"
    assert placement["display_path"] == "Desktop › Run 1_ XRD"
    assert (placement["files"], placement["bytes"], placement["opened"]) == (3, 2048, True)


@pytest.mark.django_db
def test_failure_is_reported_in_plain_words(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    booking = _setup(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path)
    api = _client(ra_user)
    started = api.post(_url(booking, "input-placement"), {"location": "D:\\Data"}, format="json")
    cmd = RemoteCommand.objects.get(pk=started.data["request_id"])
    assert cmd.payload["location"] == "D:\\Data" and "place" not in cmd.payload
    _complete(eligible_workstation, cmd, success=False, message="INPUT_NOT_READY: Your input data has not reached the Analysis PC yet.")
    placement = api.get(_url(booking, "input-placement")).data["placement"]
    assert placement["status"] == "failed"
    assert "not reached the Analysis PC" in placement["detail"]


@pytest.mark.django_db
def test_an_unanswered_request_turns_into_a_retryable_failure(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    booking = _setup(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path)
    api = _client(ra_user)
    started = api.post(_url(booking, "input-placement"), {}, format="json")
    RemoteCommand.objects.filter(pk=started.data["request_id"]).update(created_at=timezone.now() - timedelta(minutes=11))
    placement = api.get(_url(booking, "input-placement")).data["placement"]
    assert placement["status"] == "failed" and "Try again" in placement["detail"]


@pytest.mark.django_db
def test_a_placement_from_another_session_does_not_count(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    booking = _setup(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path)
    api = _client(ra_user)
    api.post(_url(booking, "input-placement"), {}, format="json")
    workspace = AnalysisWorkspace.objects.filter(booking=booking).order_by("-created_at").first() or AnalysisWorkspace.objects.order_by(
        "-created_at"
    ).first()
    state = dict(workspace.transfer_state or {})
    state["input_placement"] = {**state["input_placement"], "session_id": "00000000-0000-0000-0000-000000000000"}
    AnalysisWorkspace.objects.filter(pk=workspace.pk).update(transfer_state=state)
    assert api.get(_url(booking, "input-placement")).data["placement"] is None


@pytest.mark.django_db
def test_validation_old_agents_and_strangers(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    booking = _setup(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path, caps=["extra_sources_v1"])
    api = _client(ra_user)
    url = _url(booking, "input-placement")

    assert api.get(url).data == {**api.get(url).data, "supported": False, "placement": None}
    old = api.post(url, {}, format="json")
    assert old.status_code == 409 and old.data["code"] == "placement_unsupported"

    _caps(eligible_workstation, CAPS)
    assert api.post(url, {"place": "C:\\Windows"}, format="json").data["code"] == "invalid_place"
    assert api.post(url, {"location": "relative\\dir"}, format="json").data["code"] == "invalid_path"
    assert api.post(url, {"folder_name": " .. "}, format="json").data["code"] == "invalid_name"

    stranger = UserFactory(admin_approved=True, email_verified=True)
    assert _client(stranger).get(url).status_code == 403
    assert _client(stranger).post(url, {}, format="json").status_code == 403


def test_clean_folder_name():
    assert input_placement.clean_folder_name("IIC/XRD:2026*41") == "IIC_XRD_2026_41"
    assert input_placement.clean_folder_name("name. ") == "name"
    assert input_placement.clean_folder_name("x" * 200) == "x" * 80
    assert input_placement.clean_folder_name("..") is None
    assert input_placement.clean_folder_name(5) is None


@pytest.mark.django_db
def test_a_finished_copy_is_reported_even_after_the_desktop_dropped(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    from iic_booking.remote_analysis.guacamole.authorization import OPEN_SESSION_STATUSES
    from iic_booking.remote_analysis.session_models import RemoteDesktopSession

    booking = _setup(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path)
    api = _client(ra_user)
    started = api.post(_url(booking, "input-placement"), {}, format="json")
    cmd = RemoteCommand.objects.get(pk=started.data["request_id"])
    _complete(eligible_workstation, cmd, result={"path": "C:\\x", "display_path": "Desktop › X", "files": 1})
    RemoteDesktopSession.objects.filter(booking_id=booking.pk, status__in=OPEN_SESSION_STATUSES).update(status="TERMINATED")
    placement = api.get(_url(booking, "input-placement")).data["placement"]
    assert placement["status"] == "done" and placement["display_path"] == "Desktop › X"


@pytest.mark.django_db
def test_a_pending_placement_is_reconciled_with_its_command(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    from iic_booking.remote_analysis.constants import CommandStatus

    booking = _setup(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path)
    api = _client(ra_user)
    url = _url(booking, "input-placement")
    started = api.post(url, {"place": "Documents", "folder_name": "Run"}, format="json")
    # The agent finished but its result was never recorded on the workspace.
    RemoteCommand.objects.filter(pk=started.data["request_id"]).update(status=CommandStatus.COMPLETED)
    placement = api.get(url).data["placement"]
    assert placement["status"] == "done" and placement["display_path"] == "Documents › Run"

    again = api.post(url, {}, format="json")
    RemoteCommand.objects.filter(pk=again.data["request_id"]).update(status=CommandStatus.FAILED, error_message="COPY_FAILED: Disk full")
    failed = api.get(url).data["placement"]
    assert failed["status"] == "failed" and failed["detail"] == "Disk full"
