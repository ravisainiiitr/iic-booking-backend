"""RAA large data Phase 3: the Equipment PC uploads raw data straight to the results bucket (presigned multipart)."""

from __future__ import annotations

import hashlib
import uuid

import pytest
from rest_framework.test import APIClient

from iic_booking.remote_analysis.tests.test_large_data import MiB, BigFakeS3, big_s3  # noqa: F401
from iic_booking.remote_analysis.tests.test_session_id_collect_hotfix import _booking_for
from iic_booking.remote_analysis.transfer_models import SpeedTestStatus, TransferSpeedTest
from iic_booking.sync.models import (
    AgentCommand,
    AgentCommandType,
    AgentDiagnosticState,
    AgentLifecycleStatus,
    AgentUploadSession,
    DepartmentSyncAgent,
    DirectResultUpload,
    EquipmentResult,
    ResultAttachment,
)
from iic_booking.sync.services.results_s3 import booking_results_folder
from iic_booking.sync.services.tokens import issue_access_token
from iic_booking.users.models import Department
from iic_booking.users.tests.factories import UserFactory


def _sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _agent():
    dept = Department.objects.create(name=f"MP-{uuid.uuid4().hex[:8]}", code=f"M{uuid.uuid4().hex[:6].upper()}")
    agent = DepartmentSyncAgent.objects.create(
        agent_name="Multipart Agent", department=dept, machine_guid=uuid.uuid4(),
        status=AgentLifecycleStatus.ENROLLED, is_active=True,
    )
    token = issue_access_token(agent)
    api = APIClient()
    api.credentials(HTTP_AUTHORIZATION=f"Bearer {token}", HTTP_X_AGENT_UUID=str(agent.agent_uuid))
    return agent, api


@pytest.mark.django_db
def test_dsa_multipart_records_the_file_like_upload_complete(big_s3, settings):
    settings.AWS_STORAGE_BUCKET_NAME = "results-bucket"
    agent, api = _agent()
    booking = _booking_for(UserFactory())
    big = b"T" * (64 * MiB) + b"E" * 7
    small = b"metadata"
    resp = api.post(
        "/api/v1/sync/uploads/multipart/init/",
        {"booking_id": booking.pk, "files": [
            {"rel_path": "run1/stack.tif", "size": len(big), "sha256": _sha(big)},
            {"rel_path": "notes.txt", "size": len(small), "sha256": _sha(small)},
        ]},
        format="json",
    )
    assert resp.status_code == 200, resp.content
    files = {f["rel_path"]: f for f in resp.data["files"]}
    multi, single = files["run1/stack.tif"], files["notes.txt"]
    folder = booking_results_folder(booking)
    assert multi["key"] == f"Results/{booking.virtual_booking_id}/{folder}/stack.tif"
    assert multi["upload_id"] and multi["part_size"] == 64 * MiB and multi["single_put_url"] is None
    assert single["single_put_url"] and single["put_headers"]["x-amz-checksum-sha256"]

    urls = api.post("/api/v1/sync/uploads/multipart/parts/", {"file_id": multi["file_id"], "part_numbers": [1, 2]}, format="json")
    assert set(urls.data["urls"]) == {"1", "2"}
    big_s3.put_part(multi["upload_id"], 1, big[: 64 * MiB])
    state = api.post("/api/v1/sync/uploads/multipart/state/", {"file_ids": [multi["file_id"]]}, format="json").data
    assert state["files"][0]["parts_done"] == [1] and state["files"][0]["done"] is False
    big_s3.put_part(multi["upload_id"], 2, big[64 * MiB:])
    parts = [{"n": p["PartNumber"], "etag": p["ETag"]} for p in big_s3.list_parts("b", "k", multi["upload_id"])["Parts"]]

    done = api.post("/api/v1/sync/uploads/multipart/complete/", {"file_id": multi["file_id"], "parts": parts, "sha256": _sha(big)}, format="json")
    assert done.status_code == 200, done.content
    row = DirectResultUpload.objects.get(pk=multi["file_id"])
    attachment = ResultAttachment.objects.get(pk=done.data["attachment_id"])
    assert attachment.s3_key == multi["key"] and attachment.sha256 == _sha(big) and attachment.size_bytes == len(big)
    assert attachment.result.booking_id == booking.pk and EquipmentResult.objects.filter(booking=booking).count() == 1
    session = AgentUploadSession.objects.get(agent_upload_id=row.agent_upload_id)
    assert session.status == "COMPLETED" and session.server_path == ""
    # Idempotent: a repeated complete (lost response) changes nothing.
    again = api.post("/api/v1/sync/uploads/multipart/complete/", {"file_id": multi["file_id"], "parts": parts}, format="json")
    assert again.data["done"] is True and EquipmentResult.objects.filter(booking=booking).count() == 1

    big_s3.objects[single["key"]] = small
    assert api.post("/api/v1/sync/uploads/multipart/complete/", {"file_id": single["file_id"]}, format="json").status_code == 200
    assert ResultAttachment.objects.filter(result__booking=booking).count() == 2


@pytest.mark.django_db
def test_dsa_multipart_rejections(big_s3, settings):
    settings.AWS_STORAGE_BUCKET_NAME = "results-bucket"
    agent, api = _agent()
    booking = _booking_for(UserFactory())
    assert api.post("/api/v1/sync/uploads/multipart/init/", {"booking_id": 999999, "files": []}, format="json").status_code == 404
    bad = api.post("/api/v1/sync/uploads/multipart/init/", {"booking_id": booking.pk, "files": [{"rel_path": "a", "size": 1}]}, format="json")
    assert bad.status_code == 400
    f = api.post(
        "/api/v1/sync/uploads/multipart/init/",
        {"booking_id": booking.pk, "files": [{"rel_path": "a.bin", "size": 4, "sha256": _sha(b"abcd")}]},
        format="json",
    ).data["files"][0]
    assert api.post("/api/v1/sync/uploads/multipart/complete/", {"file_id": f["file_id"]}, format="json").data["code"] == "not_uploaded"
    big_s3.objects[f["key"]] = b"ab"
    assert api.post("/api/v1/sync/uploads/multipart/complete/", {"file_id": f["file_id"]}, format="json").data["code"] == "size_mismatch"
    _other, other_api = _agent()
    assert other_api.post("/api/v1/sync/uploads/multipart/complete/", {"file_id": f["file_id"]}, format="json").status_code == 404


@pytest.mark.django_db
def test_fleet_speed_test_action_and_result(big_s3, settings):
    settings.AWS_STORAGE_BUCKET_NAME = "results-bucket"
    agent, api = _agent()
    AgentDiagnosticState.objects.create(sync_agent=agent, capabilities={"remote_commands": ["SPEED_TEST"]})
    admin = UserFactory(user_type="admin", is_staff=True, is_superuser=True, admin_approved=True, email_verified=True)
    client = APIClient()
    client.force_authenticate(admin)
    resp = client.post(f"/api/v1/sync/admin/fleet/{agent.id}/actions/", {"action": "speed_test"}, format="json")
    assert resp.status_code == 201, resp.content
    cmd = AgentCommand.objects.get(sync_agent=agent, command_type=AgentCommandType.SPEED_TEST)
    assert cmd.payload["put_url"] and cmd.payload["result_url"].startswith("/api/v1/sync/speed-test/")
    result = api.post(cmd.payload["result_url"], {"upload_mbps": 95.5, "download_mbps": 300}, format="json")
    assert result.status_code == 200, result.content
    test = TransferSpeedTest.objects.get(pk=cmd.payload["test_id"])
    assert test.status == SpeedTestStatus.COMPLETED and test.sync_agent_id == agent.id
    row = next(r for r in client.get("/api/v1/sync/admin/fleet/").json()["results"] if r["id"] == str(agent.id))
    assert row["speed_test"]["upload_mbps"] == 95.5
