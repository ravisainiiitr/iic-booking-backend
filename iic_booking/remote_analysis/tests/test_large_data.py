"""RAA large data (14 Nov 2026): speed test, raw-data pre-load, results outbox, keep running, storage rules."""

from __future__ import annotations

import hashlib
from datetime import timedelta
from unittest import mock

import pytest
from botocore.exceptions import ClientError
from django.utils import timezone

from iic_booking.equipment.remote_analysis_integration import research
from iic_booking.equipment.remote_analysis_integration.experience import AnalysisExperienceBuilder
from iic_booking.my_research.models import FileOrigin, FileStatus, ResearchFile, ResearchFolder
from iic_booking.remote_analysis.constants import (
    CommandType,
    ReservationStatus,
    SessionStatus,
    WorkspaceSyncPhase,
    WorkstationStatus,
)
from iic_booking.remote_analysis.guacamole.cleanup import SessionCleanupService
from iic_booking.remote_analysis.large_data import keep_running, lifecycle, outbox, preload, speedtest
from iic_booking.remote_analysis.models import RemoteCommand
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
from iic_booking.remote_analysis.services.commands import CommandService
from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings, RemoteDesktopSession
from iic_booking.remote_analysis.tests.test_phase2_workspace_redesign import (  # noqa: F401
    FakeS3,
    _agent_client,
    _client,
    _collect_after_end,
    _linked_session,
    _setup,
    _sha_hex,
    _url,
    research_on,
    s3_listing,
)
from iic_booking.remote_analysis.tests.test_queue_fairness_and_holds import _enqueue, _open_session, _reservation, _ws
from iic_booking.remote_analysis.tests.test_session_id_collect_hotfix import _booking_for
from iic_booking.remote_analysis.transfer_models import (
    InputPreload,
    OutputState,
    OutputUpload,
    PreloadState,
    SpeedTestStatus,
    TransferSpeedTest,
)
from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace, BookingAnalysisSetup

ALL_CAPS = ["preload_input_v1", "results_outbox_v1", "keep_running_v1", "speed_test_v1", "place_input_v1"]
MiB = 1024 * 1024


class BigFakeS3(FakeS3):
    """FakeS3 plus presigning, multipart and lifecycle."""

    def __init__(self):
        super().__init__()
        self.uploads: dict[str, dict] = {}
        self.lifecycle: dict[str, list] = {}
        self.signed: list[tuple[str, dict]] = []
        self._n = 0

    def generate_presigned_url(self, op, Params=None, ExpiresIn=None, HttpMethod=None):
        self.signed.append((op, dict(Params or {})))
        extra = f"&part={Params['PartNumber']}" if "PartNumber" in (Params or {}) else ""
        return f"https://s3.test/{Params['Bucket']}/{Params['Key']}?op={op}{extra}"

    def create_multipart_upload(self, Bucket, Key, **kwargs):
        self._n += 1
        upload_id = f"up-{self._n}"
        self.uploads[upload_id] = {"key": Key, "parts": {}}
        return {"UploadId": upload_id}

    def head_object(self, Bucket, Key, **kwargs):
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
        return super().head_object(Bucket, Key, **kwargs)

    def put_part(self, upload_id, n, data: bytes):
        self.uploads[upload_id]["parts"][n] = data

    def list_parts(self, Bucket, Key, UploadId, PartNumberMarker=0):
        if UploadId not in self.uploads:
            raise ClientError({"Error": {"Code": "NoSuchUpload"}}, "ListParts")
        parts = self.uploads[UploadId]["parts"]
        return {
            "Parts": [
                {"PartNumber": n, "ETag": f'"{hashlib.md5(d).hexdigest()}"', "Size": len(d)} for n, d in sorted(parts.items())
            ],
            "IsTruncated": False,
        }

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        if UploadId not in self.uploads:
            raise ClientError({"Error": {"Code": "NoSuchUpload"}}, "CompleteMultipartUpload")
        parts = self.uploads.pop(UploadId)["parts"]
        self.objects[Key] = b"".join(parts[p["PartNumber"]] for p in MultipartUpload["Parts"])

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self.uploads.pop(UploadId, None)

    def get_bucket_lifecycle_configuration(self, Bucket):
        if Bucket not in self.lifecycle:
            raise ClientError({"Error": {"Code": "NoSuchLifecycleConfiguration"}}, "GetBucketLifecycleConfiguration")
        return {"Rules": self.lifecycle[Bucket]}

    def put_bucket_lifecycle_configuration(self, Bucket, LifecycleConfiguration):
        self.lifecycle[Bucket] = LifecycleConfiguration["Rules"]


@pytest.fixture
def big_s3(monkeypatch, settings):
    from iic_booking.my_research import storage as research_storage

    fake = BigFakeS3()
    monkeypatch.setattr(research_storage, "_client", lambda: fake)
    settings.AWS_STORAGE_BUCKET_NAME = settings.AWS_STORAGE_BUCKET_NAME or "results-bucket"
    return fake


def _caps(ws, caps=ALL_CAPS, version=""):
    ws.agent_capabilities = list(caps)
    ws.agent_version = version
    ws.save(update_fields=["agent_capabilities", "agent_version"])


def _commands(ws, kind):
    return RemoteCommand.objects.filter(workstation=ws, command_type=kind).order_by("created_at")


# --- Phase 0: speed test ------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_admin_queues_speed_test_and_agent_reports_result(ra_user, eligible_workstation, big_s3):
    _caps(eligible_workstation)
    resp = _client(ra_user).post(f"/api/v1/analysis/workstations/{eligible_workstation.id}/speed-test/")
    assert resp.status_code == 201, resp.data
    cmd = _commands(eligible_workstation, CommandType.SPEED_TEST).get()
    payload = cmd.payload
    assert payload["bytes"] == speedtest.SPEED_TEST_BYTES
    assert "op=put_object" in payload["put_url"] and "op=get_object" in payload["get_url"]
    test = TransferSpeedTest.objects.get(pk=payload["test_id"])
    assert test.object_key == f"speedtest/{eligible_workstation.agent_id}/{test.id}.bin"
    big_s3.objects[test.object_key] = b"x"

    agent = _agent_client(eligible_workstation)
    result = agent.post(
        payload["result_url"],
        {"upload_mbps": 412.5, "download_mbps": 870.1, "bytes": payload["bytes"], "seconds": 7.4},
        format="json",
    )
    assert result.status_code == 200, result.data
    test.refresh_from_db()
    assert test.status == SpeedTestStatus.COMPLETED and test.upload_mbps == 412.5
    assert test.object_key not in big_s3.objects  # deleted after the test
    summary = _client(ra_user).get(f"/api/v1/analysis/workstations/{eligible_workstation.id}/speed-test/").data
    assert summary["latest"]["download_mbps"] == 870.1 and summary["pending"] is False
    detail = _client(ra_user).get(f"/api/v1/analysis/workstations/{eligible_workstation.id}/").data
    assert detail["speed_test"]["upload_mbps"] == 412.5
    assert detail["link_speed"] == {"upload_mbps": 412.5, "download_mbps": 870.1, "measured_at": test.measured_at.isoformat()}


@pytest.mark.django_db
def test_speed_test_refused_for_old_agent_and_result_via_command_completion(ra_user, eligible_workstation, big_s3):
    _caps(eligible_workstation, caps=[], version="1.0.39")
    assert _client(ra_user).post(f"/api/v1/analysis/workstations/{eligible_workstation.id}/speed-test/").status_code == 409
    _caps(eligible_workstation, caps=[], version="1.0.40")
    test, cmd = speedtest.issue_for_workstation(eligible_workstation)
    CommandService().complete(cmd, success=True, result={"upload_mbps": 100, "download_mbps": 200})
    test.refresh_from_db()
    assert test.status == SpeedTestStatus.COMPLETED and test.download_mbps == 200
    bad, cmd2 = speedtest.issue_for_workstation(eligible_workstation)
    CommandService().complete(cmd2, success=False, message="upload timed out")
    bad.refresh_from_db()
    assert bad.status == SpeedTestStatus.FAILED and "timed out" in bad.detail


# --- Phase 1: pre-load ------------------------------------------------------------------------------------------


def _held_reservation(ws, booking=None, status=ReservationStatus.AWAITING_CHECKIN):
    reservation = _reservation(status=ReservationStatus.QUEUED, booking=booking)
    reservation.workstation = ws
    reservation.status = status
    reservation.save()
    return reservation


@pytest.mark.django_db
def test_allocation_queues_preload_and_setup_change_requeues(django_capture_on_commit_callbacks, big_s3, s3_listing):
    ws = _ws([])
    _caps(ws)
    booking = _reservation().booking
    s3_listing[booking.virtual_booking_id] = [
        {"key": f"Results/{booking.virtual_booking_id}/b1/scan.raw", "s3_key": f"Results/{booking.virtual_booking_id}/b1/scan.raw",
         "name": "scan.raw", "size_bytes": 2048, "source": "s3"}
    ]
    with django_capture_on_commit_callbacks(execute=True):
        reservation = _held_reservation(ws, booking=booking)
    cmd = _commands(ws, CommandType.PRELOAD_INPUT).get()
    assert cmd.payload["reservation_id"] == str(reservation.id) and cmd.payload["revision"] == 1
    assert cmd.payload["manifest_url"].endswith(f"/input/{reservation.id}/manifest/")
    pre = InputPreload.objects.get(reservation=reservation)
    assert pre.state == PreloadState.QUEUED and pre.workstation_id == ws.id

    with django_capture_on_commit_callbacks(execute=True):
        BookingAnalysisSetup.objects.create(booking=booking, user=booking.user, input_source="booking", input_booking=booking)
    pre.refresh_from_db()
    assert pre.revision == 2
    assert _commands(ws, CommandType.PRELOAD_INPUT).count() == 2


@pytest.mark.django_db
def test_old_agent_gets_no_preload(django_capture_on_commit_callbacks, s3_listing):
    ws = _ws([])
    _caps(ws, caps=[], version="1.0.39")
    booking = _reservation().booking
    BookingAnalysisSetup.objects.create(booking=booking, user=booking.user, input_source="booking", input_booking=booking)
    with django_capture_on_commit_callbacks(execute=True):
        _held_reservation(ws, booking=booking)
    assert not _commands(ws, CommandType.PRELOAD_INPUT).exists()
    assert not InputPreload.objects.exists()


@pytest.mark.django_db
def test_cancel_discards_and_move_discards_on_old_pc(django_capture_on_commit_callbacks, s3_listing):
    ws1, ws2 = _ws([]), _ws([])
    _caps(ws1)
    _caps(ws2)
    booking = _reservation().booking
    BookingAnalysisSetup.objects.create(booking=booking, user=booking.user, input_source="booking", input_booking=booking)
    with django_capture_on_commit_callbacks(execute=True):
        reservation = _held_reservation(ws1, booking=booking)
    with django_capture_on_commit_callbacks(execute=True):
        reservation.workstation = ws2
        reservation.save()
    assert _commands(ws1, CommandType.DISCARD_PRELOAD).get().payload["reason"] == "moved"
    assert _commands(ws2, CommandType.PRELOAD_INPUT).exists()
    pre = InputPreload.objects.get(reservation=reservation)
    assert pre.workstation_id == ws2.id and pre.state == PreloadState.QUEUED and pre.revision == 2

    with django_capture_on_commit_callbacks(execute=True):
        reservation.status = ReservationStatus.CANCELLED
        reservation.save()
    assert _commands(ws2, CommandType.DISCARD_PRELOAD).get().payload["reason"] == "cancelled"
    pre.refresh_from_db()
    assert pre.state == PreloadState.DISCARDED


@pytest.mark.django_db
def test_no_show_release_discards(django_capture_on_commit_callbacks, s3_listing):
    from iic_booking.remote_analysis.services.checkin import CheckinService

    ws = _ws([], status=WorkstationStatus.RESERVED)
    _caps(ws)
    booking = _reservation().booking
    BookingAnalysisSetup.objects.create(booking=booking, user=booking.user, input_source="booking", input_booking=booking)
    with django_capture_on_commit_callbacks(execute=True):
        reservation = _held_reservation(ws, booking=booking)
        AnalysisReservation.objects.filter(pk=reservation.pk).update(checkin_expires_at=timezone.now() - timedelta(minutes=1))
    # Without the queue re-run the reservation stays released (the scheduler could hand it this PC again).
    with django_capture_on_commit_callbacks(execute=True), mock.patch(
        "iic_booking.remote_analysis.services.scheduler.SchedulerService.process_queue"
    ):
        CheckinService().expire_due()
    assert _commands(ws, CommandType.DISCARD_PRELOAD).exists()
    assert InputPreload.objects.get(reservation=reservation).state == PreloadState.DISCARDED


@pytest.mark.django_db
def test_manifest_from_booking_results_and_progress(django_capture_on_commit_callbacks, big_s3, s3_listing):
    ws = _ws([])
    _caps(ws)
    booking = _reservation().booking
    vid = booking.virtual_booking_id
    s3_listing[vid] = [
        {"key": f"Results/{vid}/b1/scan.raw", "s3_key": f"Results/{vid}/b1/scan.raw", "name": "scan.raw", "size_bytes": 5 * MiB, "source": "s3"},
        {"key": f"Results/{vid}/b1/sub/meta.txt", "s3_key": f"Results/{vid}/b1/sub/meta.txt", "name": "sub/meta.txt", "size_bytes": 10, "source": "s3"},
    ]
    with django_capture_on_commit_callbacks(execute=True):
        reservation = _held_reservation(ws, booking=booking)
    agent = _agent_client(ws)
    base = f"/api/v1/analysis/agent/transfers/input/{reservation.id}"
    data = agent.get(f"{base}/manifest/").data
    assert data["reservation_id"] == str(reservation.id) and data["total_bytes"] == 5 * MiB + 10
    by_path = {f["rel_path"]: f for f in data["files"]}
    assert set(by_path) == {"scan.raw", "sub/meta.txt"}
    assert by_path["sub/meta.txt"]["name"] == "meta.txt"
    assert "op=get_object" in by_path["scan.raw"]["url"] and by_path["scan.raw"]["url_expires_at"]
    assert by_path["scan.raw"]["auth"] is None and data["limits"]["part_size"] == 64 * MiB

    resp = agent.post(
        f"{base}/progress/",
        {"state": "downloading", "files_done": 1, "files_total": 2, "bytes_done": 10, "bytes_total": 5 * MiB + 10},
        format="json",
    )
    assert resp.status_code == 200 and resp.data["revision"] == 1
    info = preload.payload_for(reservation)
    assert info["state"] == "downloading" and info["files_done"] == 1 and info["bytes_total"] == 5 * MiB + 10
    assert agent.post(f"{base}/progress/", {"state": "bogus"}, format="json").status_code == 400

    other = _ws([])
    assert _agent_client(other).get(f"{base}/manifest/").status_code == 403


@pytest.mark.django_db
def test_manifest_size_limit_and_my_research_source(
    ra_user, research_on, big_s3, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    booking = _booking_for(ra_user)
    reservation, session, workspace = _linked_session(ra_user, booking, reservation_window, ra_settings, tmp_path)
    link = workspace.research_link
    _, raw, _ = research.ensure_folders(link, booking, ra_user)
    sub = ResearchFolder.objects.create(workspace=link.workspace, parent=raw, name="day1", created_by=ra_user)
    body = b"tem frame"
    rf = research._new_research_file(link.workspace, sub, "frame.mrc", booking=booking, actor=ra_user, origin="upload", size=len(body))
    big_s3.objects[rf.storage_key] = body
    research._finalize_research_file(rf, checksum_b64=research.hex_to_b64(_sha_hex(body)), verified=True, actor=ra_user)

    url = f"/api/v1/analysis/agent/transfers/input/{reservation.id}/manifest/"
    data = _agent_client(eligible_workstation).get(url).data
    assert [(f["rel_path"], f["sha256"], f["source"]) for f in data["files"]] == [("day1/frame.mrc", _sha_hex(body), "my_research")]

    settings_obj = RemoteAnalysisSettings.get_solo()
    settings_obj.transfer_max_booking_bytes = 4
    settings_obj.save()
    transfer = _client(ra_user).get(_url(booking, "setup")).data["transfer"]
    assert transfer == {"size_limit_bytes": 4, "input_bytes": len(body), "download_mbps": None, "upload_mbps": None}
    resp = _agent_client(eligible_workstation).get(url)
    assert resp.status_code == 413 and resp.data["code"] == "size_limit" and resp.data["limit_bytes"] == 4


@pytest.mark.django_db
def test_portal_stored_upload_input_streams_with_agent_auth(
    ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path, big_s3, s3_listing
):
    from django.core.files.uploadedfile import SimpleUploadedFile

    from iic_booking.remote_analysis.tests.test_session_id_collect_hotfix import _started_session
    from iic_booking.remote_analysis.workspace.transfer import TransferManager

    booking = _booking_for(ra_user)
    BookingAnalysisSetup.objects.create(booking=booking, user=ra_user, input_source="upload")
    reservation, _session = _started_session(ra_user, reservation_window, ra_settings, tmp_path, booking=booking)
    workspace = AnalysisWorkspace.objects.get(reservation=reservation)
    TransferManager().upload(workspace, SimpleUploadedFile("in.csv", b"a,b\n1,2\n"), folder="RawData", relative_name="in.csv")
    agent = _agent_client(eligible_workstation)
    data = agent.get(f"/api/v1/analysis/agent/transfers/input/{reservation.id}/manifest/").data
    (entry,) = data["files"]
    assert entry["auth"] == "agent" and entry["url_expires_at"] is None and entry["source"] == "upload"
    content = agent.get(entry["url"].split("testserver", 1)[-1])
    assert content.status_code == 200 and b"".join(content.streaming_content) == b"a,b\n1,2\n"


@pytest.mark.django_db
def test_place_input_moves_from_staging(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    from iic_booking.equipment.remote_analysis_integration import input_placement
    from iic_booking.remote_analysis.tests.test_session_id_collect_hotfix import _started_session

    _caps(eligible_workstation)
    booking = _booking_for(ra_user)
    reservation, _session = _started_session(ra_user, reservation_window, ra_settings, tmp_path, booking=booking)
    InputPreload.objects.update_or_create(
        reservation=reservation, defaults={"workstation": eligible_workstation, "state": PreloadState.READY, "revision": 3}
    )
    booking.refresh_from_db()
    input_placement.request_place(booking, ra_user, {"place": "Desktop"})
    cmd = _commands(eligible_workstation, CommandType.PLACE_INPUT).last()
    assert cmd.payload["from_staging"] is True and cmd.payload["reservation_id"] == str(reservation.id)
    assert cmd.payload["preload_revision"] == 3


# --- Phase 2: results outbox ------------------------------------------------------------------------------------


def _outbox_session(ra_user, booking, reservation_window, ra_settings, tmp_path, eligible_workstation):
    _caps(eligible_workstation)
    return _linked_session(ra_user, booking, reservation_window, ra_settings, tmp_path)


@pytest.mark.django_db
def test_outbox_upload_registers_files_in_processed_data_and_notifies(
    ra_user, research_on, big_s3, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    booking = _booking_for(ra_user)
    _r, session, workspace = _outbox_session(ra_user, booking, reservation_window, ra_settings, tmp_path, eligible_workstation)
    small = b"result,1\n"
    big = b"A" * (64 * MiB) + b"B" * 10
    agent = _agent_client(eligible_workstation)
    base = f"/api/v1/analysis/agent/transfers/output/{session.id}"
    resp = agent.post(
        f"{base}/init/",
        {"files": [
            {"rel_path": "result.csv", "size": len(small), "sha256": _sha_hex(small)},
            {"rel_path": "maps\\big.mrc", "size": len(big), "sha256": _sha_hex(big)},
        ]},
        format="json",
    )
    assert resp.status_code == 200, resp.data
    files = {f["rel_path"]: f for f in resp.data["files"]}
    single, multi = files["result.csv"], files["maps/big.mrc"]
    assert single["single_put_url"] and single["upload_id"] is None
    assert single["put_headers"]["x-amz-checksum-sha256"] == research.hex_to_b64(_sha_hex(small))
    assert multi["upload_id"] and multi["single_put_url"] is None and multi["part_size"] == 64 * MiB
    assert multi["key"].startswith(f"research/workspaces/{workspace.research_link.workspace_id}/files/")

    parts = agent.post(f"{base}/parts/", {"file_id": multi["file_id"], "part_numbers": [1, 2]}, format="json").data
    assert set(parts["urls"]) == {"1", "2"} and "part=2" in parts["urls"]["2"]
    assert agent.post(f"{base}/parts/", {"file_id": multi["file_id"], "part_numbers": [3]}, format="json").status_code == 400

    big_s3.put_part(multi["upload_id"], 1, big[: 64 * MiB])
    state = agent.get(f"{base}/state/").data
    st = {f["file_id"]: f for f in state["files"]}
    assert st[multi["file_id"]]["parts_done"] == [1] and st[single["file_id"]]["done"] is False
    big_s3.put_part(multi["upload_id"], 2, big[64 * MiB:])

    agent.post(f"{base}/progress/", {"state": "uploading", "bytes_done": 64 * MiB, "bytes_total": len(big) + len(small),
                                     "files_done": 0, "files_total": 2}, format="json")
    payload = outbox.payload_for(booking)
    assert payload["state"] == "uploading" and payload["bytes_done"] == 64 * MiB and payload["destination_label"].startswith("My Research ›")

    etags = [{"n": p["PartNumber"], "etag": p["ETag"]} for p in big_s3.list_parts("b", "k", multi["upload_id"])["Parts"]]
    done = agent.post(f"{base}/complete/", {"file_id": multi["file_id"], "parts": etags, "sha256": _sha_hex(big)}, format="json")
    assert done.status_code == 200, done.data
    big_s3.objects[single["key"]] = small
    with mock.patch("iic_booking.communication.in_app.notify_in_app") as bell, mock.patch(
        "iic_booking.whatsapp.hooks.remote_analysis_results_uploaded"
    ) as whatsapp:
        from django.test.utils import CaptureQueriesContext  # noqa: F401

        resp = agent.post(f"{base}/complete/", {"file_id": single["file_id"], "sha256": _sha_hex(small)}, format="json")
        assert resp.status_code == 200
        outbox.notify_complete(OutputUpload.objects.get(session=session).pk)
    box = OutputUpload.objects.get(session=session)
    assert box.state == OutputState.COMPLETE and box.files_done == 2 and box.bytes_done == len(big) + len(small)
    assert bell.called and "My Research" in bell.call_args.kwargs["message"]
    done_payload = outbox.payload_for(booking)
    _, _, processed_folder = research.find_folders(workspace.research_link)
    assert done_payload["state"] == "done" and done_payload["eta_seconds"] is None
    assert done_payload["my_research_href"] == f"/my-research/{workspace.research_link.workspace_id}?folder={processed_folder.pk}"
    assert bell.call_args.kwargs["link"] == done_payload["my_research_href"]
    summary = _client(ra_user).get(f"/api/v1/bookings/{booking.booking_id}/analysis/").data
    assert summary["output_upload"]["state"] == "done"
    whatsapp.assert_called_once()

    _, _, processed = research.find_folders(workspace.research_link)
    rows = ResearchFile.objects.filter(workspace=workspace.research_link.workspace, origin=FileOrigin.ANALYSIS_OUTPUT)
    assert {(r.folder.name, r.display_name) for r in rows} == {("Processed Data", "result.csv"), ("maps", "big.mrc")}
    assert all(r.status == FileStatus.AVAILABLE for r in rows)
    assert rows.get(display_name="result.csv").checksum_verified is True
    assert rows.get(display_name="big.mrc").folder.parent_id == processed.id

    # Re-sending the outbox (agent restarted) reports both files done; nothing uploads twice.
    again = agent.post(f"{base}/init/", {"files": [{"rel_path": "result.csv", "size": len(small), "sha256": _sha_hex(small)}]}, format="json")
    assert again.data["files"][0]["done"] is True and again.data["files"][0]["single_put_url"] is None


@pytest.mark.django_db
def test_outbox_rejects_size_mismatch_and_unlinked_booking(
    ra_user, research_on, big_s3, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    from iic_booking.remote_analysis.tests.test_session_id_collect_hotfix import _started_session

    booking = _booking_for(ra_user)
    _r, session, _w = _outbox_session(ra_user, booking, reservation_window, ra_settings, tmp_path, eligible_workstation)
    agent = _agent_client(eligible_workstation)
    base = f"/api/v1/analysis/agent/transfers/output/{session.id}"
    body = b"12345"
    f = agent.post(f"{base}/init/", {"files": [{"rel_path": "a.txt", "size": 5, "sha256": _sha_hex(body)}]}, format="json").data["files"][0]
    big_s3.objects[f["key"]] = b"123"
    bad = agent.post(f"{base}/complete/", {"file_id": f["file_id"]}, format="json")
    assert bad.status_code == 409 and bad.data["code"] == "size_mismatch"
    assert agent.post(f"{base}/init/", {"files": [{"rel_path": "../x", "size": 1, "sha256": _sha_hex(b"x")}]}, format="json").status_code == 400

    other_booking = _booking_for(ra_user)
    plain_res = _reservation(status=ReservationStatus.COMPLETED, workstation=eligible_workstation, booking=other_booking)
    plain_session = RemoteDesktopSession.objects.create(
        reservation=plain_res, booking=other_booking, user=ra_user, workstation=eligible_workstation,
        status=SessionStatus.COMPLETED,
    )
    resp = agent.post(f"/api/v1/analysis/agent/transfers/output/{plain_session.id}/init/",
                      {"files": [{"rel_path": "a.txt", "size": 1, "sha256": _sha_hex(b"x")}]}, format="json")
    assert resp.status_code == 409 and resp.data["code"] == "no_research_link"
    assert _agent_client(_ws([])).post(f"{base}/init/", {}, format="json").status_code == 403


@pytest.mark.django_db
def test_collect_into_outbox_frees_the_pc_at_once(
    ra_user, research_on, big_s3, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    booking = _booking_for(ra_user)
    _r, session, workspace = _outbox_session(ra_user, booking, reservation_window, ra_settings, tmp_path, eligible_workstation)
    collect = _collect_after_end(session, ra_user, workspace)
    eligible_workstation.refresh_from_db()
    assert eligible_workstation.status == WorkstationStatus.CLEANING
    CommandService().complete(collect, success=True, message="moved to outbox", result={"outbox": True, "files_total": 3, "bytes_total": 99})
    eligible_workstation.refresh_from_db()
    workspace.refresh_from_db()
    box = OutputUpload.objects.get(session=session)
    assert box.state == OutputState.QUEUED and box.files_total == 3
    assert workspace.sync_phase == WorkspaceSyncPhase.COMPLETED
    assert eligible_workstation.status == WorkstationStatus.AVAILABLE
    assert not RemoteCommand.objects.filter(command_type=CommandType.CLEAN_WORKSTATION, payload__reason="upload_verified").exists()


@pytest.mark.django_db
def test_stalled_outbox_on_offline_pc_is_paused_and_staff_alerted(
    ra_user, research_on, big_s3, eligible_workstation, reservation_window, ra_settings, tmp_path
):
    booking = _booking_for(ra_user)
    _r, session, _w = _outbox_session(ra_user, booking, reservation_window, ra_settings, tmp_path, eligible_workstation)
    box = OutputUpload.objects.create(session=session, workstation=eligible_workstation, booking=booking, user=ra_user,
                                      state=OutputState.UPLOADING)
    OutputUpload.objects.filter(pk=box.pk).update(last_progress_at=timezone.now() - timedelta(minutes=31))
    eligible_workstation.last_heartbeat = timezone.now() - timedelta(hours=1)
    eligible_workstation.save(update_fields=["last_heartbeat"])
    with mock.patch("iic_booking.communication.in_app.notify_in_app") as bell:
        assert outbox.sweep_stalled() == 1
        assert outbox.sweep_stalled() == 0
    box.refresh_from_db()
    assert box.state == OutputState.PAUSED and "offline" in box.paused_reason and box.staff_alerted_at
    assert bell.call_count == 1
    exp = AnalysisExperienceBuilder()._large_data(booking, None, None)
    assert exp["output_upload"]["state"] == "paused" and exp["output_upload"]["eta_seconds"] is None


# --- Part 5: keep running ---------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_keep_running_disconnects_browser_and_survives_idle(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    from iic_booking.remote_analysis.tests.test_session_id_collect_hotfix import _started_session

    _caps(eligible_workstation)
    booking = _booking_for(ra_user)
    reservation, session = _started_session(ra_user, reservation_window, ra_settings, tmp_path, booking=booking)
    RemoteDesktopSession.objects.filter(pk=session.pk).update(status=SessionStatus.ACTIVE, last_activity_at=timezone.now() - timedelta(hours=2))
    with mock.patch("iic_booking.remote_analysis.guacamole.connection.ConnectionManager.destroy"):
        resp = _client(ra_user).post(f"/api/v1/bookings/{booking.booking_id}/analysis/keep-running/")
    assert resp.status_code == 200, resp.data
    session.refresh_from_db()
    assert session.status == SessionStatus.READY and session.keep_running_until
    kr = keep_running.status(session)["keep_running"]
    assert kr["active"] is True and kr["allowed"] is False and kr["until"] == session.keep_running_until.isoformat()
    assert session.expires_at == session.keep_running_until
    assert _commands(eligible_workstation, CommandType.KEEP_RUNNING).get().payload["logoff"] is False
    reservation.refresh_from_db()
    assert reservation.reserved_end >= session.keep_running_until
    from iic_booking.remote_analysis.session_models import GuacamoleConnection

    GuacamoleConnection.objects.filter(session=session).update(is_active=False, destroyed_at=timezone.now())
    session.refresh_from_db()
    assert keep_running.needs_reprovision(session) is True
    RemoteDesktopSession.objects.filter(pk=session.pk).update(status=SessionStatus.IDLE)
    SessionCleanupService().cleanup_idle()
    session.refresh_from_db()
    assert session.status == SessionStatus.IDLE  # not ended for idleness
    # Contract path works too and is idempotent.
    again = _client(ra_user).post(f"/api/v1/equipment/bookings/{booking.booking_id}/analysis/keep-running/")
    assert again.status_code == 200


@pytest.mark.django_db
def test_keep_running_refused_when_someone_waits_and_newcomer_ends_it():
    ws = _ws([], status=WorkstationStatus.BUSY)
    _caps(ws)
    current = _reservation(status=ReservationStatus.ACTIVE, workstation=ws)
    session = _open_session(current, ws, ends_in=timedelta(minutes=30))
    waiting = _reservation(created_ago=timedelta(minutes=5))
    entry = _enqueue(waiting, ago=timedelta(minutes=5))
    assert keep_running.status(session)["can_keep_running"] is False
    assert keep_running.status(session)["keep_running"]["reason"].startswith("Someone is waiting")
    with pytest.raises(keep_running.TransferError):
        keep_running.start(session)
    entry.delete()
    AnalysisReservation.objects.filter(pk=waiting.pk).update(status=ReservationStatus.QUEUED)

    with mock.patch("iic_booking.remote_analysis.guacamole.connection.ConnectionManager.destroy"):
        keep_running.start(session)
    session.refresh_from_db()
    assert session.keep_running_until > timezone.now() + timedelta(hours=1)

    newcomer = _reservation(created_ago=timedelta(minutes=1))
    _enqueue(newcomer, ago=timedelta(minutes=1))
    with mock.patch("iic_booking.communication.in_app.notify_in_app"):
        assert keep_running.sweep() == 1
    session.refresh_from_db()
    newcomer.refresh_from_db()
    assert session.keep_running_until <= timezone.now() + timedelta(minutes=2, seconds=5)
    assert newcomer.workstation_id == ws.id and newcomer.status == ReservationStatus.AWAITING_CHECKIN

    RemoteDesktopSession.objects.filter(pk=session.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    SessionCleanupService().cleanup_expired()
    session.refresh_from_db()
    assert session.status == SessionStatus.EXPIRED  # no second grace for a kept session


@pytest.mark.django_db
def test_keep_running_until_is_ten_minutes_before_the_next_booking():
    ws = _ws([], status=WorkstationStatus.BUSY)
    _caps(ws)
    current = _reservation(status=ReservationStatus.ACTIVE, workstation=ws)
    session = _open_session(current, ws, ends_in=timedelta(minutes=30))
    nxt = _reservation(status=ReservationStatus.RESERVED, workstation=ws)
    start = timezone.now() + timedelta(hours=2)
    AnalysisReservation.objects.filter(pk=nxt.pk).update(reserved_start=start)
    with mock.patch("iic_booking.remote_analysis.guacamole.connection.ConnectionManager.destroy"):
        keep_running.start(session)
    session.refresh_from_db()
    assert abs((session.keep_running_until - (start - timedelta(minutes=10))).total_seconds()) < 2
    AnalysisReservation.objects.filter(pk=nxt.pk).update(reserved_start=timezone.now() + timedelta(minutes=15))
    other = _open_session(_reservation(status=ReservationStatus.ACTIVE, workstation=ws), ws, ends_in=timedelta(minutes=30))
    assert "20 minutes" in keep_running.status(other)["keep_running_blocked_reason"]


# --- experience payload + Phase 4 -------------------------------------------------------------------------------


@pytest.mark.django_db
def test_experience_payload_has_large_data_fields(ra_user, eligible_workstation, reservation_window, ra_settings, tmp_path):
    from iic_booking.remote_analysis.tests.test_session_id_collect_hotfix import _started_session

    booking = _booking_for(ra_user)
    reservation, _session = _started_session(ra_user, reservation_window, ra_settings, tmp_path, booking=booking)
    InputPreload.objects.create(reservation=reservation, workstation=eligible_workstation, state=PreloadState.DOWNLOADING,
                                bytes_done=10, bytes_total=110, rate_bps=10.0)
    booking.refresh_from_db()
    data = AnalysisExperienceBuilder().build(booking)
    assert data["session"]["input_preload"]["eta_seconds"] == 10
    assert set(data["session"]) >= {"can_keep_running", "keep_running_blocked_reason", "keep_running_until", "keep_running"}
    assert set(data["session"]["keep_running"]) == {"allowed", "active", "until", "reason"}
    assert data["output_upload"] is None


def test_lifecycle_rules_never_delete_research_data():
    rules = lifecycle.desired_rules()
    lifecycle.check_rules(rules)
    expiring = [r for r in rules if "Expiration" in r]
    assert [r["Filter"]["Prefix"] for r in expiring] == ["speedtest/"]
    abort = next(r for r in rules if "AbortIncompleteMultipartUpload" in r and r["Filter"]["Prefix"] == "")
    assert abort["AbortIncompleteMultipartUpload"]["DaysAfterInitiation"] == 7
    assert all(len(r.get("Transitions", [])) <= 1 for r in rules)  # no archive until the user decides
    with pytest.raises(ValueError):
        lifecycle.check_rules([{"ID": "raa-large-data-bad", "Filter": {"Prefix": "research/"}, "Expiration": {"Days": 30}}])
    archived = lifecycle.desired_rules(archive_after_days=180)
    assert archived[0]["Transitions"][1] == {"Days": 180, "StorageClass": "GLACIER_IR"}


@pytest.mark.django_db
def test_lifecycle_command_dry_run_then_apply_keeps_other_rules(big_s3, settings):
    from io import StringIO

    from django.core.management import call_command

    settings.AWS_STORAGE_BUCKET_NAME = "results-bucket"
    settings.MY_RESEARCH_S3_BUCKET = "results-bucket"
    existing = {"ID": "my-research-abort-incomplete-multipart", "Status": "Enabled", "Filter": {"Prefix": "research/"},
                "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 2}}
    big_s3.lifecycle["results-bucket"] = [existing]
    out = StringIO()
    call_command("apply_s3_lifecycle", stdout=out)
    assert "DRY RUN" in out.getvalue() and "results-bucket" not in out.getvalue()
    assert big_s3.lifecycle["results-bucket"] == [existing]
    out = StringIO()
    call_command("apply_s3_lifecycle", "--apply", stdout=out)
    rules = big_s3.lifecycle["results-bucket"]
    assert rules[0] == existing and len(rules) == 5 and "PASS" in out.getvalue()


@pytest.mark.django_db
def test_whatsapp_results_uploaded_hook(ra_user, monkeypatch):
    from iic_booking.whatsapp import conf, hooks

    booking = _booking_for(ra_user)
    monkeypatch.setattr(conf, "enabled", lambda: True)
    box = mock.Mock(booking=booking, user=ra_user, files_total=40, bytes_total=18 * 1024**3, session_id="s-1")
    with mock.patch("iic_booking.whatsapp.hooks._notify") as notify:
        hooks.remote_analysis_results_uploaded(box)
    event, params, key = notify.call_args.args[1], notify.call_args.args[2], notify.call_args.args[3]
    assert event == "remote_analysis_results_uploaded" and params[3] == "40 files (18.0 GB)" and key == "ra:s-1:results_uploaded"
