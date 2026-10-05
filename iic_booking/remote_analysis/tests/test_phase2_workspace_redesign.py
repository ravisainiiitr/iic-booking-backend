"""Workspace redesign: setup popup, My Research raw copy / output bridge, collect plan, verified cleanup."""

from __future__ import annotations

import base64
import hashlib
import io
from uuid import uuid4

import pytest
from botocore.exceptions import ClientError
from django.core.cache import cache
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import BookingResultFile, BookingStatus
from iic_booking.equipment.remote_analysis_integration import analysis_setup, raw_staging, research
from iic_booking.my_research import storage as research_storage
from iic_booking.my_research.models import FileOrigin, FileStatus, ResearchFile, ResearchFolder, ResearchWorkspace
from iic_booking.remote_analysis.constants import (
    CommandType,
    TransferStatus,
    WorkspaceSyncPhase,
    WorkstationStatus,
)
from iic_booking.remote_analysis.guacamole.cleanup import SessionCleanupService
from iic_booking.remote_analysis.models import AnalysisWorkstation, RemoteCommand
from iic_booking.remote_analysis.services.commands import CommandService, parse_result_suffix
from iic_booking.remote_analysis.services.heartbeat import HeartbeatService
from iic_booking.remote_analysis.services.reservation import ReservationService
from iic_booking.remote_analysis.services.tokens import issue_agent_token
from iic_booking.remote_analysis.tests.test_session_id_collect_hotfix import _booking_for, _started_session
from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
from iic_booking.remote_analysis.workspace.transfer import TransferManager
from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace, BookingAnalysisSetup, WorkspaceTransfer
from iic_booking.users.tests.factories import UserFactory


def _sha_hex(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _sha_b64(body: bytes) -> str:
    return base64.b64encode(hashlib.sha256(body).digest()).decode()


class FakeS3:
    def __init__(self):
        self.objects: dict[str, bytes] = {}
        self.calls: list[tuple[str, str]] = []

    def put_object(self, Bucket, Key, Body, ContentType=None, ChecksumSHA256=None, **kwargs):
        body = Body.read()
        if ChecksumSHA256 and ChecksumSHA256 != _sha_b64(body):
            raise ClientError({"Error": {"Code": "BadDigest", "Message": "checksum"}}, "PutObject")
        self.objects[Key] = body
        self.calls.append(("put_object", Key))

    def upload_fileobj(self, Fileobj, Bucket, Key, ExtraArgs=None):
        self.objects[Key] = Fileobj.read()
        self.calls.append(("upload_fileobj", Key))

    def copy_object(self, CopySource, Bucket, Key, ChecksumAlgorithm=None, **kwargs):
        body = self.objects[CopySource["Key"]]
        self.objects[Key] = body
        self.calls.append(("copy_object", Key))
        return {"CopyObjectResult": {"ETag": f'"{hashlib.md5(body).hexdigest()}"', "ChecksumSHA256": _sha_b64(body)}}

    def head_object(self, Bucket, Key, **kwargs):
        body = self.objects[Key]
        return {"ContentLength": len(body), "ETag": f'"{hashlib.md5(body).hexdigest()}"'}

    def get_object(self, Bucket, Key, Range=None):
        body = self.objects[Key]
        if Range:
            body = body[: int(Range.split("-")[1]) + 1]
        return {"Body": io.BytesIO(body)}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)


@pytest.fixture
def fake_s3(monkeypatch):
    fake = FakeS3()
    monkeypatch.setattr(research_storage, "_client", lambda: fake)
    return fake


@pytest.fixture
def s3_listing(monkeypatch):
    listing: dict[str, list] = {}
    monkeypatch.setattr(
        raw_staging, "list_results_s3_objects", lambda vid, prefix_only=False: list(listing.get(vid, []))
    )
    cache.clear()
    return listing


@pytest.fixture
def research_on(settings, monkeypatch, s3_listing):
    settings.MY_RESEARCH_ENABLED = True
    settings.MY_RESEARCH_PILOT_EMAILS = ""
    settings.MY_RESEARCH_S3_BUCKET = "research-bucket"
    settings.MY_RESEARCH_USER_STORAGE_QUOTA = 0
    settings.MY_RESEARCH_WORKSPACE_STORAGE_QUOTA = 0
    settings.AWS_STORAGE_BUCKET_NAME = "results-bucket"
    monkeypatch.setattr("iic_booking.my_research.access.is_internal_iitr_user", lambda user: True)
    return settings


@pytest.fixture
def research_off(settings, s3_listing):
    settings.MY_RESEARCH_ENABLED = False
    return settings


def _client(user):
    api = APIClient()
    api.force_authenticate(user=user)
    return api


def _agent_client(workstation):
    _, token = issue_agent_token(workstation)
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_AGENT_ID=workstation.agent_id)
    return api


def _url(booking, segment):
    return f"/api/v1/bookings/{booking.booking_id}/analysis/{segment}/"


def _setup(user, booking, **body):
    resp = _client(user).post(_url(booking, "setup"), body, format="json")
    assert resp.status_code == 200, resp.data
    return resp.data


def _seed_result_file(booking, name: str, body: bytes):
    return BookingResultFile.objects.create(booking=booking, file=ContentFile(body, name=name), original_name=name)


# --- setup GET/POST ---------------------------------------------------------


@pytest.mark.django_db
def test_setup_get_for_ineligible_user_keeps_booking_details_destination(ra_user, research_off):
    booking = _booking_for(ra_user)
    data = _client(ra_user).get(_url(booking, "setup")).data
    assert data["booking"]["id"] == booking.booking_id
    assert data["my_research"]["eligible"] is False
    assert data["my_research"]["current_link"] is None
    assert data["output"]["destination_label"] == "Booking Details › Analyzed Data"
    assert data["folders_preview"] == {"root": booking.virtual_booking_id, "raw": "Raw Data", "processed": "Processed Data"}

    resp = _client(ra_user).post(_url(booking, "setup"), {"new_workspace_name": "Thesis"}, format="json")
    assert resp.status_code == 400 and resp.data["code"] == "not_eligible"
    ok = _client(ra_user).post(_url(booking, "setup"), {"input_source": "upload"}, format="json")
    assert ok.status_code == 200
    assert ok.data["input"]["selected"]["source"] == "upload"


@pytest.mark.django_db
def test_setup_endpoints_never_404_for_foreign_or_missing_booking(ra_user, research_on):
    booking = _booking_for(ra_user)
    stranger = UserFactory(admin_approved=True, email_verified=True)
    for segment in ("setup", "input-sources", "sync-status"):
        assert _client(stranger).get(_url(booking, segment)).status_code == 403
    assert _client(stranger).post(_url(booking, "sync-now")).status_code == 403
    assert _client(ra_user).get("/api/v1/bookings/999999999/analysis/setup/").status_code == 403
    assert _client(ra_user).get(f"/api/bookings/{booking.booking_id}/analysis/setup/").status_code == 200


@pytest.mark.django_db
def test_setup_post_creates_project_folders_idempotently(ra_user, research_on, fake_s3):
    booking = _booking_for(ra_user)
    first = _setup(ra_user, booking, new_workspace_name="Nanocomposite Coating", input_source="upload")
    link = first["my_research"]["current_link"]
    assert link["workspace_name"] == "Nanocomposite Coating"
    assert link["raw_folder_id"] and link["processed_folder_id"]
    assert link["folder_path"] == f"Nanocomposite Coating / {booking.virtual_booking_id}"
    assert first["input"]["selected"] == {"source": "upload", "booking_id": None, "virtual_id": None, "file_count": 0}
    assert first["output"]["destination_label"] == (
        f"My Research › Nanocomposite Coating / {booking.virtual_booking_id} / Processed Data"
    )

    again = _setup(ra_user, booking, workspace_id=link["workspace_id"], input_source="upload")
    assert again["my_research"]["current_link"] == link
    keep = _setup(ra_user, booking, input_source="upload")
    assert keep["my_research"]["current_link"] == link
    same_name = _setup(ra_user, booking, new_workspace_name="nanocomposite coating", input_source="upload")
    assert same_name["my_research"]["current_link"]["workspace_id"] == link["workspace_id"]
    assert ResearchWorkspace.objects.filter(owner=ra_user).count() == 1
    assert ResearchFolder.objects.filter(workspace_id=link["workspace_id"]).count() == 3
    assert BookingAnalysisSetup.objects.get(booking=booking).state["phase"] == "queued"


@pytest.mark.django_db
def test_setup_post_error_codes(ra_user, research_on):
    booking = _booking_for(ra_user)
    stranger = UserFactory(admin_approved=True, email_verified=True)
    foreign = _booking_for(stranger)
    locked = _booking_for(ra_user)
    locked.status = BookingStatus.BOOKED
    locked.save(update_fields=["status"])
    others_project = ResearchWorkspace.objects.create(owner=stranger, name="Not mine")

    cases = [
        ({"workspace_id": str(uuid4())}, "invalid_workspace"),
        ({"workspace_id": str(others_project.pk)}, "invalid_workspace"),
        ({"workspace_id": "nope"}, "invalid_workspace"),
        ({"input_source": "booking", "input_booking_id": foreign.booking_id}, "input_booking_not_owned"),
        ({"input_source": "booking", "input_booking_id": locked.booking_id}, "results_locked"),
        ({"input_source": "disk"}, "invalid_input"),
    ]
    for body, code in cases:
        resp = _client(ra_user).post(_url(booking, "setup"), body, format="json")
        assert resp.status_code == 400, (body, resp.data)
        assert resp.data["code"] == code and resp.data["detail"]
    assert not BookingAnalysisSetup.objects.filter(booking=booking).exists()


@pytest.mark.django_db
def test_folders_are_recreated_after_rename_or_delete(ra_user, research_on):
    booking = _booking_for(ra_user)
    link_data = _setup(ra_user, booking, new_workspace_name="Project", input_source="upload")["my_research"]["current_link"]
    raw = ResearchFolder.objects.get(pk=link_data["raw_folder_id"])
    raw.name = "Old raw"
    raw.save(update_fields=["name"])
    root = ResearchFolder.objects.get(pk=link_data["folder_id"])

    renamed = _setup(ra_user, booking, input_source="upload")["my_research"]["current_link"]
    assert renamed["folder_id"] == str(root.pk)
    assert renamed["raw_folder_id"] != link_data["raw_folder_id"]
    assert ResearchFolder.objects.get(pk=renamed["raw_folder_id"]).name == "Raw Data"

    root.deleted_at = timezone.now()
    root.save(update_fields=["deleted_at"])
    recreated = _setup(ra_user, booking, workspace_id=link_data["workspace_id"], input_source="upload")
    new_link = recreated["my_research"]["current_link"]
    assert new_link["folder_id"] != str(root.pk)
    assert new_link["raw_folder_id"] and new_link["processed_folder_id"]


@pytest.mark.django_db
def test_input_sources_lists_own_bookings_with_results(ra_user, research_on, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    booking = _booking_for(ra_user)
    with_files = _booking_for(ra_user)
    _seed_result_file(with_files, "scan.raw", b"spectrum")
    _booking_for(ra_user)
    stranger = UserFactory(admin_approved=True, email_verified=True)
    _seed_result_file(_booking_for(stranger), "x.raw", b"x")

    data = _client(ra_user).get(_url(booking, "input-sources"), {"page_size": 100}).data
    ids = [r["booking_id"] for r in data["results"]]
    assert ids == [with_files.booking_id]
    row = data["results"][0]
    assert row["file_count"] == 1 and row["is_current"] is False and row["locked_reason"] is None
    assert data["count"] == 1

    found = _client(ra_user).get(_url(booking, "input-sources"), {"q": with_files.virtual_booking_id}).data
    assert [r["booking_id"] for r in found["results"]] == [with_files.booking_id]


# --- raw copy / staging -----------------------------------------------------


@pytest.mark.django_db
def test_raw_copy_is_server_side_checksum_verified_and_idempotent(ra_user, research_on, fake_s3, s3_listing):
    booking = _booking_for(ra_user)
    body = b"spectrum-bytes" * 100
    fake_s3.objects["Results/VID/a.raw"] = body
    s3_listing[booking.virtual_booking_id] = [
        {"key": "Results/VID/a.raw", "name": "a.raw", "size_bytes": len(body), "source": "s3"}
    ]
    data = _setup(ra_user, booking, new_workspace_name="P", input_source="booking", input_booking_id=booking.booking_id)
    setup = BookingAnalysisSetup.objects.get(booking=booking)

    first = analysis_setup.run_setup(str(setup.pk))
    assert first["copy"]["copied"] == 1 and not first["copy"]["errors"]
    second = analysis_setup.run_setup(str(setup.pk))
    assert second["copy"]["copied"] == 0 and second["copy"]["skipped"] == 1
    assert [c[0] for c in fake_s3.calls] == ["copy_object"]

    rf = ResearchFile.objects.get(folder_id=data["my_research"]["current_link"]["raw_folder_id"])
    assert rf.origin == FileOrigin.BOOKING_RAW and rf.status == FileStatus.AVAILABLE
    assert rf.checksum_sha256 == _sha_b64(body) and rf.checksum_verified is True
    setup.refresh_from_db()
    assert setup.state["phase"] == "done" and setup.state["files_done"] == 1


@pytest.mark.django_db
def test_raw_copy_streams_portal_files_with_checksum(ra_user, research_on, fake_s3, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    booking = _booking_for(ra_user)
    body = b"operator upload"
    _seed_result_file(booking, "notes.txt", body)
    _setup(ra_user, booking, new_workspace_name="P", input_source="upload")
    link = BookingAnalysisSetup.objects.get(booking=booking).research_link

    first = research.copy_booking_raw(booking, booking, link, actor=ra_user)
    second = research.copy_booking_raw(booking, booking, link, actor=ra_user)
    assert (first["copied"], second["copied"], second["skipped"]) == (1, 0, 1)
    assert [c[0] for c in fake_s3.calls] == ["put_object"]
    rf = ResearchFile.objects.get(workspace=link.workspace, origin=FileOrigin.BOOKING_RAW)
    assert rf.checksum_sha256 == _sha_b64(body) and rf.checksum_verified is True


@pytest.mark.django_db
def test_locked_results_block_raw_copy(ra_user, research_on, fake_s3):
    booking = _booking_for(ra_user)
    _setup(ra_user, booking, new_workspace_name="P", input_source="upload")
    link = BookingAnalysisSetup.objects.get(booking=booking).research_link
    booking.status = BookingStatus.PENDING
    booking.save(update_fields=["status"])
    with pytest.raises(research.ResearchCopyError) as exc:
        research.copy_booking_raw(booking, booking, link, actor=ra_user)
    assert exc.value.code == "results_locked"
    assert not fake_s3.calls


@pytest.mark.django_db
def test_research_raw_data_is_staged_into_workspace_and_ingest_is_skipped(
    ra_user, research_on, fake_s3, eligible_workstation, reservation_window, ra_settings, tmp_path, monkeypatch
):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()
    booking = _booking_for(ra_user)
    _setup(ra_user, booking, new_workspace_name="P", input_source="upload")
    link = BookingAnalysisSetup.objects.get(booking=booking).research_link
    _, raw, _ = research.ensure_folders(link, booking, ra_user)
    sub = ResearchFolder.objects.create(workspace=link.workspace, parent=raw, name="day1")
    body = b"uploaded raw"
    rf = research._new_research_file(link.workspace, sub, "s.csv", booking=booking, actor=ra_user, origin="upload", size=len(body))
    fake_s3.objects[rf.storage_key] = body
    research._finalize_research_file(rf, checksum_b64=_sha_b64(body), verified=True, actor=ra_user)

    calls = []
    monkeypatch.setattr(
        "iic_booking.remote_analysis.workspace.booking_ingest.BookingResultIngestService.ingest",
        lambda self, *a, **k: calls.append(1),
    )
    start, end = reservation_window
    reservation = ReservationService().create_reservation(
        user=ra_user, requested_start=start, requested_end=end, created_by=ra_user, booking=booking
    )
    workspace = WorkspaceSyncService().ensure_for_reservation(reservation, actor=ra_user, ingest=True)
    assert workspace.research_link_id == link.pk
    assert calls == []

    first = analysis_setup.stage_input(booking, workspace, actor=ra_user)
    second = analysis_setup.stage_input(booking, workspace, actor=ra_user)
    assert (first["staged"], second["staged"], second["skipped"]) == (1, 0, 1)
    wf = workspace.files.get(relative_path="RawData/day1/s.csv", is_current=True)
    assert wf.sha256 == _sha_hex(body)
    manifest = WorkspaceSyncService().build_manifest(workspace)
    assert any("day1/s.csv" in str(f) for f in manifest.get("files", []))


# --- collect plan, verification, bridge, cleanup ------------------------------


def _linked_session(ra_user, booking, reservation_window, ra_settings, tmp_path):
    _setup(ra_user, booking, new_workspace_name="Thesis", input_source="upload")
    reservation, session = _started_session(ra_user, reservation_window, ra_settings, tmp_path, booking=booking)
    workspace = AnalysisWorkspace.objects.get(reservation=reservation)
    return reservation, session, workspace


def _collect_after_end(session, ra_user, workspace):
    SessionCleanupService().cleanup(session, reason="user end", actor=ra_user)
    return RemoteCommand.objects.filter(
        command_type=CommandType.COLLECT_WORKSPACE, payload__workspace_id=str(workspace.id)
    ).latest("created_at")


def _upload_output(workspace, rel: str, body: bytes):
    TransferManager().upload(
        workspace,
        SimpleUploadedFile(rel.split("/")[-1], body),
        folder="Processed",
        source="agent",
        expected_sha256=_sha_hex(body),
        relative_name=rel,
    )


@pytest.mark.django_db
def test_collect_plan_and_progress_drive_sync_status(
    ra_user, research_on, fake_s3, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    booking = _booking_for(ra_user)
    _, session, workspace = _linked_session(ra_user, booking, reservation_window, ra_settings, tmp_path)
    agent = _agent_client(eligible_workstation)
    base = f"/api/v1/analysis/workspaces/{workspace.id}"

    bad = agent.post(f"{base}/collect-plan/", {"session_id": str(session.id), "files": [{"path": "../x", "size": 1}]}, format="json")
    assert bad.status_code == 400

    queued = _client(ra_user).post(_url(booking, "sync-now"))
    assert queued.status_code == 202 and queued.data == {"queued": True}
    body = b"a" * 300
    plan = agent.post(
        f"{base}/collect-plan/",
        {"session_id": str(session.id), "files": [{"path": "sub\\a.csv", "size": 300, "sha256": _sha_hex(body)}], "total_bytes": 300},
        format="json",
    )
    assert plan.status_code == 201
    transfer_id = plan.data["transfer_id"]
    resp = agent.post(
        f"{base}/progress/",
        {"transfer_id": transfer_id, "bytes_done": 150, "bytes_total": 300, "files_done": 0, "files_total": 1, "current_file": "sub/a.csv"},
        format="json",
    )
    assert resp.status_code == 200 and resp.data["accepted"] is True

    status = _client(ra_user).get(_url(booking, "sync-status")).data
    assert status["phase"] == "collecting" and status["direction"] == "output"
    assert (status["bytes_done"], status["bytes_total"], status["percent"]) == (150, 300, 50)
    assert status["current_file"] == "sub/a.csv" and status["poll_after_ms"] == 2000
    assert status["destination"]["path_label"] == f"Thesis / {booking.virtual_booking_id} / Processed Data"

    other = AnalysisWorkstation.objects.create(
        agent_id="other-agent", hostname="OTHER", status=WorkstationStatus.AVAILABLE, enabled=True, last_heartbeat=timezone.now()
    )
    assert _agent_client(other).post(f"{base}/progress/", {"transfer_id": transfer_id}, format="json").status_code == 403
    assert WorkspaceTransfer.objects.get(pk=transfer_id).details["files"] == [
        {"path": "sub/a.csv", "size": 300, "sha256": _sha_hex(body)}
    ]


@pytest.mark.django_db
def test_sync_now_without_open_session_is_409(ra_user, research_on):
    booking = _booking_for(ra_user)
    resp = _client(ra_user).post(_url(booking, "sync-now"))
    assert resp.status_code == 409
    assert _client(ra_user).get(_url(booking, "sync-status")).data["phase"] == "idle"


@pytest.mark.django_db
def test_plan_mismatch_is_not_verified_and_nothing_is_cleaned(
    ra_user, research_on, fake_s3, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    booking = _booking_for(ra_user)
    _, session, workspace = _linked_session(ra_user, booking, reservation_window, ra_settings, tmp_path)
    collect = _collect_after_end(session, ra_user, workspace)
    agent = _agent_client(eligible_workstation)
    good, missing = b"good", b"missing"
    plan_id = agent.post(
        f"/api/v1/analysis/workspaces/{workspace.id}/collect-plan/",
        {
            "session_id": str(session.id),
            "files": [
                {"path": "good.csv", "size": 4, "sha256": _sha_hex(good)},
                {"path": "missing.csv", "size": 7, "sha256": _sha_hex(missing)},
            ],
        },
        format="json",
    ).data["transfer_id"]
    _upload_output(workspace, "good.csv", good)

    CommandService().complete(collect, success=True, message="Uploaded 1 files")
    workspace.refresh_from_db()
    assert workspace.upload_verified_at is None
    assert workspace.sync_phase == WorkspaceSyncPhase.RETRY_PENDING
    assert "COLLECT_PLAN_MISMATCH" in workspace.sync_message
    assert WorkspaceTransfer.objects.get(pk=plan_id).status == TransferStatus.CANCELLED
    assert not RemoteCommand.objects.filter(
        command_type=CommandType.CLEAN_WORKSTATION, payload__reason="upload_verified"
    ).exists()
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.CLEANING
    assert WorkspaceSyncService().collect_failures_since_success(workspace) == 1


@pytest.mark.django_db
def test_verified_collect_bridges_to_my_research_and_sends_verified_cleanup(
    ra_user, research_on, fake_s3, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    eligible_workstation.agent_capabilities = ["collect_plan_v1", "progress_v1", "verified_cleanup_v1"]
    eligible_workstation.save(update_fields=["agent_capabilities"])
    booking = _booking_for(ra_user)
    _, session, workspace = _linked_session(ra_user, booking, reservation_window, ra_settings, tmp_path)
    link = workspace.research_link
    collect = _collect_after_end(session, ra_user, workspace)
    a, b = b"alpha result", b"beta result"
    files = [
        {"path": "result.csv", "size": len(a), "sha256": _sha_hex(a)},
        {"path": "plots/fig.png", "size": len(b), "sha256": _sha_hex(b)},
    ]
    plan_id = _agent_client(eligible_workstation).post(
        f"/api/v1/analysis/workspaces/{workspace.id}/collect-plan/",
        {"session_id": str(session.id), "files": files},
        format="json",
    ).data["transfer_id"]
    _upload_output(workspace, "result.csv", a)
    _upload_output(workspace, "plots/fig.png", b)

    CommandService().complete(collect, success=True, message=f"Uploaded 2 files; transfer_id={plan_id}")
    workspace.refresh_from_db()
    assert workspace.upload_verified_at is not None
    assert WorkspaceTransfer.objects.get(pk=plan_id).status == TransferStatus.COMPLETED
    clean = RemoteCommand.objects.get(command_type=CommandType.CLEAN_WORKSTATION, payload__reason="upload_verified")
    assert clean.payload["session_id"] == str(session.id)
    assert clean.payload["defer_output_cleanup"] is False
    assert sorted(clean.payload["verified_files"], key=lambda f: f["path"]) == sorted(
        [{"path": f["path"], "sha256": f["sha256"]} for f in files], key=lambda f: f["path"]
    )
    assert workspace.transfer_state["bridge"]["phase"] == "queued"
    assert workspace.transfer_state["pc_cleanup"] == "pending"
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.AVAILABLE

    status = _client(ra_user).get(_url(booking, "sync-status")).data
    assert status["phase"] == "copying_to_workspace"

    result = analysis_setup.run_bridge(str(workspace.id), str(session.id), [f["path"] for f in files], True)
    assert (result["copied"], result["purged"], result["errors"]) == (2, 2, [])
    workspace.refresh_from_db()
    bridge = workspace.transfer_state["bridge"]
    assert bridge["phase"] == "done" and bridge["copied"] == 2 and bridge["purged"] == 2
    _, _, processed = research.find_folders(link)
    top = ResearchFile.objects.get(folder=processed, display_name="result.csv")
    assert top.origin == FileOrigin.ANALYSIS_OUTPUT and top.checksum_sha256 == _sha_b64(a) and top.checksum_verified
    nested = ResearchFile.objects.get(folder__parent=processed, folder__name="plots", display_name="fig.png")
    assert nested.checksum_sha256 == _sha_b64(b)
    assert not workspace.files.filter(relative_path__startswith="Processed/", deleted=False).exists()

    rerun = analysis_setup.run_bridge(str(workspace.id), str(session.id), None, True)
    assert rerun["copied"] == 0
    assert ResearchFile.objects.filter(origin=FileOrigin.ANALYSIS_OUTPUT).count() == 2

    CommandService().complete(
        clean, success=True, message='Cleaned | result={"deleted":1,"kept":["plots/fig.png"]}'
    )
    workspace.refresh_from_db()
    assert workspace.transfer_state["pc_cleanup"] == "kept"
    assert workspace.transfer_state["kept_files"] == ["plots/fig.png"]
    status = _client(ra_user).get(_url(booking, "sync-status")).data
    assert status["phase"] == "done" and status["verified"] is True
    assert status["pc_cleanup"] == "kept" and status["kept_files"] == ["plots/fig.png"]


@pytest.mark.django_db
def test_old_agent_cleanup_keeps_output_on_the_pc(
    ra_user, research_off, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    booking = _booking_for(ra_user)
    reservation, session = _started_session(ra_user, reservation_window, ra_settings, tmp_path, booking=booking)
    workspace = AnalysisWorkspace.objects.get(reservation=reservation)
    collect = _collect_after_end(session, ra_user, workspace)
    _upload_output(workspace, "out.csv", b"x")
    CommandService().complete(collect, success=True, message="Uploaded 1 files")

    clean = RemoteCommand.objects.get(command_type=CommandType.CLEAN_WORKSTATION, payload__reason="upload_verified")
    assert clean.payload["defer_output_cleanup"] is True
    assert "verified_files" not in clean.payload
    assert "Output" not in clean.payload["delete_folders"] and "Logs" not in clean.payload["delete_folders"]
    workspace.refresh_from_db()
    assert workspace.transfer_state["pc_cleanup"] == "not_supported"
    assert "bridge" not in workspace.transfer_state
    assert workspace.files.filter(relative_path="Processed/out.csv", deleted=False).exists()
    status = _client(ra_user).get(_url(booking, "sync-status")).data
    assert status["phase"] == "done"
    assert status["destination"]["path_label"] == "Booking Details › Analyzed Data"


@pytest.mark.django_db
def test_bridge_renames_on_name_clash(ra_user, research_on, fake_s3, eligible_workstation, reservation_window, ra_settings, tmp_path):
    booking = _booking_for(ra_user)
    _, session, workspace = _linked_session(ra_user, booking, reservation_window, ra_settings, tmp_path)
    link = workspace.research_link
    _, _, processed = research.ensure_folders(link, booking, ra_user)
    existing = b"user edited"
    rf = research._new_research_file(link.workspace, processed, "result.csv", booking=booking, actor=ra_user, origin="upload", size=len(existing))
    fake_s3.objects[rf.storage_key] = existing
    research._finalize_research_file(rf, checksum_b64=_sha_b64(existing), verified=True, actor=ra_user)
    _upload_output(workspace, "result.csv", b"fresh output")

    result = research.bridge_processed(workspace, link)
    assert result["copied"] == 1 and not result["errors"]
    names = sorted(ResearchFile.objects.filter(folder=processed).values_list("display_name", flat=True))
    assert names == ["result (2).csv", "result.csv"]
    assert research.bridge_processed(workspace, link)["copied"] == 0


@pytest.mark.django_db
def test_failed_bridge_is_retried_for_72_hours(
    ra_user, research_on, eligible_workstation, reservation_window, ra_settings, tmp_path, monkeypatch
):
    from datetime import timedelta

    from iic_booking.remote_analysis import tasks

    booking = _booking_for(ra_user)
    _, _, workspace = _linked_session(ra_user, booking, reservation_window, ra_settings, tmp_path)
    queued = []
    monkeypatch.setattr(tasks.bridge_workspace_output, "delay", lambda *a: queued.append(a))
    recent = (timezone.now() - timedelta(hours=1)).isoformat()
    analysis_setup.merge_state(
        AnalysisWorkspace, workspace.pk, "transfer_state", bridge={"phase": "failed", "session_id": "s1", "first_failed_at": recent}
    )
    assert tasks.retry_failed_workspace_collects()["bridge_retried"] == 1
    assert queued == [(str(workspace.id), "s1", None, True)]

    stale = (timezone.now() - timedelta(hours=80)).isoformat()
    analysis_setup.merge_state(
        AnalysisWorkspace, workspace.pk, "transfer_state", bridge={"phase": "failed", "session_id": "s1", "first_failed_at": stale}
    )
    assert tasks.retry_failed_workspace_collects()["bridge_retried"] == 0


# --- agent protocol -----------------------------------------------------------


@pytest.mark.django_db
def test_heartbeat_stores_capabilities(eligible_workstation):
    HeartbeatService().process(eligible_workstation, {"capabilities": ["verified_cleanup_v1", "collect_plan_v1", ""]})
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.agent_capabilities == ["collect_plan_v1", "verified_cleanup_v1"]
    HeartbeatService().process(eligible_workstation, {"heartbeat": {"cpu": 1}})
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.agent_capabilities == []


def test_parse_result_suffix():
    assert parse_result_suffix('Cleaned 3 | result={"deleted":3,"kept":[]}') == {"deleted": 3, "kept": []}
    assert parse_result_suffix("Cleaned | result=not-json") is None
    assert parse_result_suffix("plain") is None


@pytest.mark.django_db
def test_command_complete_accepts_result_and_code(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    reservation, session = _started_session(ra_user, reservation_window, ra_settings, tmp_path)
    workspace = AnalysisWorkspace.objects.get(reservation=reservation)
    collect = _collect_after_end(session, ra_user, workspace)
    resp = _agent_client(eligible_workstation).post(
        f"/api/v1/analysis/commands/{collect.id}/complete/",
        {"success": False, "message": "folder gone", "code": "SESSION_FOLDER_MISSING", "result": {"x": 1}},
        format="json",
    )
    assert resp.status_code == 200
    workspace.refresh_from_db()
    assert workspace.upload_verified_at is None
    assert workspace.sync_message.startswith("SESSION_FOLDER_MISSING")
