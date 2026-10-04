"""End-of-session result folders on the Analysis PC, any-file-type output, input path on the setup popup."""

from __future__ import annotations

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from iic_booking.equipment.remote_analysis_integration import analysis_setup, pc_folders, research
from iic_booking.my_research.models import FileOrigin, ResearchFile
from iic_booking.remote_analysis.constants import CommandStatus, CommandType
from iic_booking.remote_analysis.models import RemoteCommand
from iic_booking.remote_analysis.services.commands import CommandService
from iic_booking.remote_analysis.tests.test_phase2_workspace_redesign import (  # noqa: F401 (fixtures)
    _agent_client,
    _client,
    _collect_after_end,
    _linked_session,
    _sha_hex,
    _upload_output,
    _url,
    fake_s3,
    research_on,
    s3_listing,
)
from iic_booking.remote_analysis.tests.test_session_id_collect_hotfix import _booking_for, _started_session
from iic_booking.remote_analysis.workspace.transfer import TransferError, TransferManager
from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace
from iic_booking.users.tests.factories import UserFactory

FULL_CAPS = ["collect_plan_v1", "progress_v1", "verified_cleanup_v1", "extra_sources_v1"]


def _caps(workstation, caps):
    workstation.agent_capabilities = caps
    workstation.save(update_fields=["agent_capabilities"])


# --- file types -----------------------------------------------------------------


@pytest.mark.django_db
def test_agent_output_accepts_any_extension_but_portal_uploads_stay_filtered(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    reservation, _ = _started_session(ra_user, reservation_window, ra_settings, tmp_path)
    workspace = AnalysisWorkspace.objects.get(reservation=reservation)
    wf = TransferManager().upload(
        workspace, SimpleUploadedFile("tool.exe", b"MZ\x90\x00"), folder="Processed", source="agent", relative_name="bin/tool.exe"
    )
    assert wf.relative_path == "Processed/bin/tool.exe"
    with pytest.raises(TransferError) as exc:
        TransferManager().upload(workspace, SimpleUploadedFile("tool.exe", b"MZ\x90\x00"), folder="RawData")
    assert exc.value.code == "blocked_extension"


@pytest.mark.django_db
def test_bridge_copies_executable_analysis_output(
    ra_user, research_on, fake_s3, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    booking = _booking_for(ra_user)
    _, _, workspace = _linked_session(ra_user, booking, reservation_window, ra_settings, tmp_path)
    _upload_output(workspace, "bin/fit.exe", b"MZ\x90\x00 analysis helper")
    result = research.bridge_processed(workspace, workspace.research_link)
    assert (result["copied"], result["errors"]) == (1, [])
    rf = ResearchFile.objects.get(display_name="fit.exe")
    assert rf.origin == FileOrigin.ANALYSIS_OUTPUT and rf.detected_type == "executable"


# --- input path -----------------------------------------------------------------


@pytest.mark.django_db
def test_setup_payload_shows_pc_input_path(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    eligible_workstation.input_path = r"C:\ProgramData\RemoteAnalysisAgent\Sessions\Input"
    eligible_workstation.save(update_fields=["input_path"])
    booking = _booking_for(ra_user)
    _started_session(ra_user, reservation_window, ra_settings, tmp_path, booking=booking)
    data = _client(ra_user).get(_url(booking, "setup")).data
    assert data["input"]["pc_input_path"] == r"C:\ProgramData\RemoteAnalysisAgent\Sessions\Input"


# --- browse -----------------------------------------------------------------------


@pytest.mark.django_db
def test_browse_round_trip_through_the_agent(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    booking = _booking_for(ra_user)
    _started_session(ra_user, reservation_window, ra_settings, tmp_path, booking=booking)
    api = _client(ra_user)
    url = _url(booking, "pc-folders/browse")

    unsupported = api.post(url, {"path": None}, format="json")
    assert unsupported.status_code == 409 and unsupported.data["code"] == "picker_unsupported"

    _caps(eligible_workstation, FULL_CAPS)
    first = api.post(url, {"path": "d:/Results/"}, format="json")
    assert first.status_code == 202
    again = api.post(url, {"path": "D:\\Results"}, format="json")
    assert again.data["request_id"] == first.data["request_id"]
    cmd = RemoteCommand.objects.get(pk=first.data["request_id"])
    assert cmd.command_type == CommandType.BROWSE_PC_FOLDERS and cmd.payload["path"] == "D:\\Results"

    result_url = _url(booking, f"pc-folders/browse/{cmd.id}")
    assert api.get(result_url).data == {"status": "pending"}

    listing = {"path": "D:\\Results", "folders": [{"name": "Run1", "path": "D:\\Results\\Run1", "can_select": True}]}
    agent = _agent_client(eligible_workstation)
    agent.get("/api/v1/analysis/commands/")
    resp = agent.post(f"/api/v1/analysis/commands/{cmd.id}/complete/", {"success": True, "message": "ok", "result": listing}, format="json")
    assert resp.status_code == 200
    assert api.get(result_url).data == {"status": "done", "result": listing}

    stranger = UserFactory(admin_approved=True, email_verified=True)
    assert _client(stranger).get(result_url).status_code == 403
    assert api.get(_url(booking, "pc-folders/browse/not-a-uuid")).status_code == 404
    assert api.post(url, {"path": "relative\\dir"}, format="json").data["code"] == "invalid_path"


@pytest.mark.django_db
def test_browse_failure_is_reported(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    _caps(eligible_workstation, FULL_CAPS)
    booking = _booking_for(ra_user)
    _started_session(ra_user, reservation_window, ra_settings, tmp_path, booking=booking)
    request_id = _client(ra_user).post(_url(booking, "pc-folders/browse"), {}, format="json").data["request_id"]
    cmd = RemoteCommand.objects.get(pk=request_id)
    assert cmd.payload["path"] == ""
    CommandService().complete(cmd, success=False, message="Access to the folder was denied", code="FOLDER_NOT_ALLOWED")
    data = _client(ra_user).get(_url(booking, f"pc-folders/browse/{request_id}")).data
    assert data == {"status": "failed", "detail": "Access to the folder was denied"}


# --- selection, collect, cleanup ---------------------------------------------------


@pytest.mark.django_db
def test_folder_selection_validation(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    _caps(eligible_workstation, FULL_CAPS)
    booking = _booking_for(ra_user)
    _started_session(ra_user, reservation_window, ra_settings, tmp_path, booking=booking)
    api = _client(ra_user)
    url = _url(booking, "pc-folders")
    for folders, code in (
        (["D:\\"], "folder_not_allowed"),
        (["C:\\Windows\\Temp"], "folder_not_allowed"),
        (["C:\\Program Files\\Tool\\out"], "folder_not_allowed"),
        (["D:\\Results", "D:\\Results\\Run1"], "nested_folders"),
        ([f"D:\\R{i}" for i in range(11)], "too_many_folders"),
        ("D:\\Results", "invalid_path"),
    ):
        resp = api.put(url, {"folders": folders}, format="json")
        assert resp.status_code == 400 and resp.data["code"] == code, (folders, resp.data)
    ok = api.put(url, {"folders": ["d:\\Results\\Run1\\", "D:\\results\\run1", "C:\\Users\\lab\\Desktop\\Fits"]}, format="json")
    assert ok.status_code == 200
    assert ok.data["folders"] == ["D:\\Results\\Run1", "C:\\Users\\lab\\Desktop\\Fits"]


@pytest.mark.django_db
def test_chosen_folders_are_collected_verified_and_cleaned(
    ra_user, research_on, fake_s3, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    _caps(eligible_workstation, FULL_CAPS)
    booking = _booking_for(ra_user)
    _, session, workspace = _linked_session(ra_user, booking, reservation_window, ra_settings, tmp_path)
    chosen = ["D:\\Results\\Run1", "D:\\Results\\Locked"]
    assert _client(ra_user).put(_url(booking, "pc-folders"), {"folders": chosen}, format="json").status_code == 200

    collect = _collect_after_end(session, ra_user, workspace)
    assert collect.payload["extra_sources"] == [{"path": p, "kind": "folder"} for p in chosen]

    out, extra = b"output result", b"run1 spectrum"
    files = [
        {"path": "result.csv", "size": len(out), "sha256": _sha_hex(out)},
        {"path": "Run1/spectra/s1.dat", "size": len(extra), "sha256": _sha_hex(extra)},
    ]
    skipped = {"path": "D:\\Results\\Locked", "alias": "", "files": 0, "bytes": 0, "error": "The folder no longer exists."}
    sources = [{"path": "D:\\Results\\Run1", "alias": "Run1", "files": 1, "bytes": len(extra)}, skipped]
    agent = _agent_client(eligible_workstation)
    base = f"/api/v1/analysis/workspaces/{workspace.id}"
    bad = agent.post(f"{base}/collect-plan/", {"session_id": str(session.id), "files": files, "extra_sources": [{"path": "D:\\x", "alias": "../x"}]}, format="json")
    assert bad.status_code == 400
    plan = agent.post(f"{base}/collect-plan/", {"session_id": str(session.id), "files": files, "extra_sources": sources}, format="json")
    assert plan.status_code == 201
    agent.post(f"{base}/progress/", {"transfer_id": plan.data["transfer_id"], "bytes_done": 3, "files_total": 2}, format="json")

    status = _client(ra_user).get(_url(booking, "sync-status")).data
    assert status["extra_folders"] == [{**s, "kind": "folder"} for s in sources]

    _upload_output(workspace, "result.csv", out)
    _upload_output(workspace, "Run1/spectra/s1.dat", extra)
    CommandService().complete(collect, success=True, message="Uploaded 2 files")

    clean = RemoteCommand.objects.get(command_type=CommandType.CLEAN_WORKSTATION, payload__reason="upload_verified")
    assert clean.payload["extra_sources"] == [{"path": "D:\\Results\\Run1", "alias": "Run1", "kind": "folder"}]
    assert {f["path"] for f in clean.payload["verified_files"]} == {"result.csv", "Run1/spectra/s1.dat"}

    analysis_setup.run_bridge(str(workspace.id), str(session.id), [f["path"] for f in files], True)
    _, _, processed = research.find_folders(workspace.research_link)
    assert ResearchFile.objects.filter(folder__parent__parent=processed, folder__name="spectra", display_name="s1.dat").exists()

    CommandService().complete(
        clean, success=True, message='Cleaned | result={"deleted":2,"kept":[],"removed_folders":["D:\\\\Results\\\\Run1"]}'
    )
    status = _client(ra_user).get(_url(booking, "sync-status")).data
    assert status["phase"] == "done" and status["pc_cleanup"] == "done" and status["pc_deleted"] == 2
    assert status["pc_removed_folders"] == ["D:\\Results\\Run1"]
    assert status["extra_folders"][1]["error"] == "The folder no longer exists."


FILE_CAPS = [*FULL_CAPS, "extra_files_v1"]


@pytest.mark.django_db
def test_single_files_need_the_files_capability_and_allow_up_to_50_items(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    _caps(eligible_workstation, FULL_CAPS)
    booking = _booking_for(ra_user)
    _started_session(ra_user, reservation_window, ra_settings, tmp_path, booking=booking)
    api = _client(ra_user)
    url = _url(booking, "pc-folders")
    item = {"path": "D:\\Results\\fit.csv", "kind": "file"}
    assert api.put(url, {"folders": [item]}, format="json").data["code"] == "picker_unsupported"

    _caps(eligible_workstation, FILE_CAPS)
    for folders, code in (
        ([{"path": "D:\\Results", "kind": "folder"}, item], "nested_folders"),
        ([{"path": "D:\\x.csv", "kind": "link"}], "invalid_path"),
        ([{"path": f"D:\\f{i}.csv", "kind": "file"} for i in range(51)], "too_many_folders"),
    ):
        resp = api.put(url, {"folders": folders}, format="json")
        assert resp.status_code == 400 and resp.data["code"] == code, (folders, resp.data)
    many = [{"path": f"D:\\Data\\f{i}.csv", "kind": "file"} for i in range(49)]
    ok = api.put(url, {"folders": ["D:\\Results\\Run1", *many]}, format="json")
    assert ok.status_code == 200, ok.data
    assert ok.data["items"][0] == {"path": "D:\\Results\\Run1", "kind": "folder"}
    assert ok.data["items"][1] == {"path": "D:\\Data\\f0.csv", "kind": "file"}
    assert len(ok.data["folders"]) == 50


@pytest.mark.django_db
def test_chosen_files_are_collected_and_cleaned_by_alias(
    ra_user, research_on, fake_s3, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    _caps(eligible_workstation, FILE_CAPS)
    booking = _booking_for(ra_user)
    _, session, workspace = _linked_session(ra_user, booking, reservation_window, ra_settings, tmp_path)
    chosen = [{"path": "C:\\Users\\lab\\Desktop\\result.csv", "kind": "file"}, {"path": "D:\\Runs\\R1", "kind": "folder"}]
    assert _client(ra_user).put(_url(booking, "pc-folders"), {"folders": chosen}, format="json").status_code == 200
    assert _client(ra_user).get(_url(booking, "sync-status")).data["extra_folders"] == chosen

    collect = _collect_after_end(session, ra_user, workspace)
    assert collect.payload["extra_sources"] == chosen

    data = b"1,2,3"
    files = [{"path": "result (2).csv", "size": len(data), "sha256": _sha_hex(data)}]
    sources = [
        {"path": chosen[0]["path"], "kind": "file", "alias": "result (2).csv", "files": 1, "bytes": len(data)},
        {"path": "D:\\Runs\\R1", "kind": "folder", "alias": "", "files": 0, "bytes": 0, "error": "The folder no longer exists."},
    ]
    agent = _agent_client(eligible_workstation)
    plan = agent.post(
        f"/api/v1/analysis/workspaces/{workspace.id}/collect-plan/",
        {"session_id": str(session.id), "files": files, "extra_sources": sources},
        format="json",
    )
    assert plan.status_code == 201, plan.data
    _upload_output(workspace, "result (2).csv", data)
    CommandService().complete(collect, success=True, message="Uploaded 1 file")

    clean = RemoteCommand.objects.get(command_type=CommandType.CLEAN_WORKSTATION, payload__reason="upload_verified")
    assert clean.payload["extra_sources"] == [{"path": chosen[0]["path"], "alias": "result (2).csv", "kind": "file"}]
    assert [f["path"] for f in clean.payload["verified_files"]] == ["result (2).csv"]


@pytest.mark.django_db
def test_selection_from_another_session_is_not_collected(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    _caps(eligible_workstation, FULL_CAPS)
    booking = _booking_for(ra_user)
    reservation, session = _started_session(ra_user, reservation_window, ra_settings, tmp_path, booking=booking)
    workspace = AnalysisWorkspace.objects.get(reservation=reservation)
    analysis_setup.merge_state(
        AnalysisWorkspace, workspace.pk, "transfer_state", extra_sources={"session_id": "old-session", "paths": ["D:\\Old"]}
    )
    collect = _collect_after_end(session, ra_user, workspace)
    assert "extra_sources" not in collect.payload


@pytest.mark.django_db
def test_end_accepts_extra_folders_and_tolerates_old_agents(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    booking = _booking_for(ra_user)
    reservation, _ = _started_session(ra_user, reservation_window, ra_settings, tmp_path, booking=booking)
    api = _client(ra_user)
    refused = api.post(_url(booking, "end"), {"extra_folders": ["D:\\Results"]}, format="json")
    assert refused.status_code == 409 and refused.data["code"] == "picker_unsupported"

    _caps(eligible_workstation, FULL_CAPS)
    ended = api.post(_url(booking, "end"), {"extra_folders": ["D:\\Results"]}, format="json")
    assert ended.status_code == 200, ended.data
    workspace = AnalysisWorkspace.objects.get(reservation=reservation)
    collect = RemoteCommand.objects.filter(
        command_type=CommandType.COLLECT_WORKSPACE, payload__workspace_id=str(workspace.id)
    ).latest("created_at")
    assert collect.payload["extra_sources"] == [{"path": "D:\\Results", "kind": "folder"}]


@pytest.mark.django_db
def test_end_with_no_folders_works_on_old_agents(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    booking = _booking_for(ra_user)
    _started_session(ra_user, reservation_window, ra_settings, tmp_path, booking=booking)
    assert _client(ra_user).post(_url(booking, "end"), {"extra_folders": []}, format="json").status_code == 200


def test_normalize_path():
    assert pc_folders.normalize_path("d:/data/run 1/") == "D:\\data\\run 1"
    assert pc_folders.normalize_path("d:\\") == "D:\\"
    assert pc_folders.normalize_path("E:/") == "E:\\"
    with pytest.raises(pc_folders.PcFolderError, match="whole drive \\(D:\\\\\\)"):
        pc_folders._check_selectable("D:\\")
    with pytest.raises(pc_folders.PcFolderError, match="system folder"):
        pc_folders._check_selectable("C:\\Program Files (x86)\\App")
    for bad in ("data", "\\\\server\\share", "D:\\a\\..\\b", "D:\\a\x00b", 5):
        with pytest.raises(pc_folders.PcFolderError):
            pc_folders.normalize_path(bad)


@pytest.mark.django_db
def test_expired_browse_reports_failure(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    from django.utils import timezone

    _caps(eligible_workstation, FULL_CAPS)
    booking = _booking_for(ra_user)
    _started_session(ra_user, reservation_window, ra_settings, tmp_path, booking=booking)
    request_id = _client(ra_user).post(_url(booking, "pc-folders/browse"), {}, format="json").data["request_id"]
    RemoteCommand.objects.filter(pk=request_id).update(created_at=timezone.now() - pc_folders.BROWSE_TIMEOUT * 2)
    assert RemoteCommand.objects.get(pk=request_id).status == CommandStatus.PENDING
    data = _client(ra_user).get(_url(booking, f"pc-folders/browse/{request_id}")).data
    assert data["status"] == "failed"
