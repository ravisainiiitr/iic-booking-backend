"""Regression: DSA-imported results appear on Booking Details Results APIs."""

from __future__ import annotations

import io
import uuid
import zipfile
from decimal import Decimal
from pathlib import Path

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APIClient

from iic_booking.equipment.models import (
    Booking,
    BookingResultFile,
    BookingStatus,
    ChargeProfile,
    Equipment,
)
from iic_booking.sync.models import (
    AgentLifecycleStatus,
    DepartmentSyncAgent,
    EquipmentResult,
    ResultAttachment,
)
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _department():
    return Department.objects.create(
        name=f"ResDept-{uuid.uuid4().hex[:8]}",
        code=f"R{uuid.uuid4().hex[:5].upper()}",
    )


def _agent(department=None):
    return DepartmentSyncAgent.objects.create(
        agent_name="Results Test Agent",
        department=department or _department(),
        machine_guid=uuid.uuid4(),
        status=AgentLifecycleStatus.ENROLLED,
        is_active=True,
    )


def _equipment(**kwargs):
    defaults = {
        "name": "PXRD Results EQ",
        "code": f"PX{uuid.uuid4().hex[:4].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def _booking(user, equipment, **kwargs):
    profile = ChargeProfile.objects.create(
        equipment=equipment,
        user_type=UserType.STUDENT,
        primary_unit_charge=Decimal("10.00"),
    )
    defaults = {
        "user": user,
        "equipment": equipment,
        "charge_profile": profile,
        "status": BookingStatus.COMPLETED,
        "total_charge": Decimal("10.00"),
        "total_time_minutes": 60,
        "virtual_booking_id": f"IIC{equipment.code}202600002",
    }
    defaults.update(kwargs)
    return Booking.objects.create(**defaults)


def _write_dsa_file(tmp_path: Path, agent: DepartmentSyncAgent, filename: str, content: bytes) -> str:
    relative = Path(str(agent.agent_uuid)) / str(uuid.uuid4()) / filename
    absolute = tmp_path / relative
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_bytes(content)
    return str(relative).replace("\\", "/")


def _seed_dsa_attachment(*, booking, agent, equipment, storage_root: Path, filename: str, content: bytes):
    relative = _write_dsa_file(storage_root, agent, filename, content)
    result = EquipmentResult.objects.create(
        sync_agent=agent,
        booking=booking,
        equipment=equipment,
        agent_upload_id=uuid.uuid4(),
        source_file_name=filename,
        processed_by="Department Sync Agent",
    )
    attachment = ResultAttachment.objects.create(
        result=result,
        file_name=filename,
        size_bytes=len(content),
        storage_path=relative,
        attachment_kind="primary",
    )
    return result, attachment


@pytest.mark.django_db
@override_settings(AWS_STORAGE_BUCKET_NAME="")
def test_dsa_single_result_listed_and_downloadable(tmp_path, settings):
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    settings.MEDIA_ROOT = str(tmp_path / "media")

    user = UserFactory()
    other = UserFactory()
    equipment = _equipment()
    agent = _agent()
    booking = _booking(user, equipment)
    _seed_dsa_attachment(
        booking=booking,
        agent=agent,
        equipment=equipment,
        storage_root=tmp_path,
        filename="analysis.csv",
        content=b"a,b\n1,2\n",
    )

    client = APIClient()
    client.force_authenticate(user=user)

    listed = client.get(f"/api/v1/bookings/{booking.pk}/results/")
    assert listed.status_code == 200
    body = listed.json()
    assert body["exists"] is True
    assert len(body["files"]) == 1
    assert body["files"][0]["name"] == "analysis.csv"
    assert body["files"][0]["source"] == "dsa"
    assert body["files"][0]["uploaded_by"] == "Department Sync Agent"
    assert "results/attachments/" in body["files"][0]["download_url"]

    attachment_id = body["files"][0]["attachment_id"]
    downloaded = client.get(f"/api/v1/bookings/{booking.pk}/results/attachments/{attachment_id}/")
    assert downloaded.status_code == 200
    assert downloaded.content == b"a,b\n1,2\n"

    # Unauthorized user cannot download
    client.force_authenticate(user=other)
    denied = client.get(f"/api/v1/bookings/{booking.pk}/results/attachments/{attachment_id}/")
    assert denied.status_code == 403


@pytest.mark.django_db
@override_settings(AWS_STORAGE_BUCKET_NAME="")
def test_dsa_multiple_results_listed(tmp_path, settings):
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    settings.MEDIA_ROOT = str(tmp_path / "media")

    user = UserFactory()
    equipment = _equipment()
    agent = _agent()
    booking = _booking(user, equipment)

    for name, payload in (("run1.csv", b"one"), ("run2.pdf", b"%PDF-1.4")):
        _seed_dsa_attachment(
            booking=booking,
            agent=agent,
            equipment=equipment,
            storage_root=tmp_path,
            filename=name,
            content=payload,
        )

    client = APIClient()
    client.force_authenticate(user=user)
    listed = client.get(f"/api/v1/bookings/{booking.pk}/results/")
    assert listed.status_code == 200
    names = sorted(f["name"] for f in listed.json()["files"])
    assert names == ["run1.csv", "run2.pdf"]


@pytest.mark.django_db
@override_settings(AWS_STORAGE_BUCKET_NAME="")
def test_completed_booking_keeps_dsa_results_visible(tmp_path, settings):
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    settings.MEDIA_ROOT = str(tmp_path / "media")

    user = UserFactory()
    equipment = _equipment()
    agent = _agent()
    booking = _booking(user, equipment, status=BookingStatus.COMPLETED)
    _seed_dsa_attachment(
        booking=booking,
        agent=agent,
        equipment=equipment,
        storage_root=tmp_path,
        filename="final.txt",
        content=b"done",
    )

    client = APIClient()
    client.force_authenticate(user=user)
    listed = client.get(f"/api/v1/bookings/{booking.pk}/results/")
    assert listed.status_code == 200
    assert listed.json()["exists"] is True

    zipped = client.get(f"/api/v1/bookings/{booking.pk}/results/download/")
    assert zipped.status_code == 200
    assert zipped["Content-Type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(zipped.content)) as zf:
        members = zf.namelist()
        assert any(m.endswith("final.txt") for m in members)


@pytest.mark.django_db
@override_settings(AWS_STORAGE_BUCKET_NAME="")
def test_has_results_true_for_dsa_attachment_without_booking_result_file(tmp_path, settings):
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    settings.MEDIA_ROOT = str(tmp_path / "media")

    user = UserFactory()
    equipment = _equipment()
    agent = _agent()
    booking = _booking(user, equipment)
    _seed_dsa_attachment(
        booking=booking,
        agent=agent,
        equipment=equipment,
        storage_root=tmp_path,
        filename="only-dsa.csv",
        content=b"x",
    )
    assert not BookingResultFile.objects.filter(booking=booking).exists()

    from iic_booking.equipment.booking_results_service import booking_has_results_annotation

    annotated = Booking.objects.filter(pk=booking.pk).annotate(has_results=booking_has_results_annotation()).get()
    assert annotated.has_results is True


@pytest.mark.django_db
@override_settings(AWS_STORAGE_BUCKET_NAME="")
def test_operator_booking_result_file_also_listed(tmp_path, settings):
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    settings.MEDIA_ROOT = str(tmp_path / "media")

    user = UserFactory()
    equipment = _equipment()
    booking = _booking(user, equipment)
    BookingResultFile.objects.create(
        booking=booking,
        file=SimpleUploadedFile("operator.pdf", b"%PDF-operator"),
        original_name="operator.pdf",
    )

    client = APIClient()
    client.force_authenticate(user=user)
    listed = client.get(f"/api/v1/bookings/{booking.pk}/results/")
    assert listed.status_code == 200
    body = listed.json()
    assert body["exists"] is True
    assert any(f["name"] == "operator.pdf" and f["source"] == "booking_result" for f in body["files"])


@pytest.mark.django_db
@override_settings(AWS_STORAGE_BUCKET_NAME="test-bucket", AWS_S3_REGION_NAME="ap-south-1")
def test_import_publishes_to_s3_and_deletes_local_temp(tmp_path, settings, monkeypatch):
    """After DSA import, portal temp sync_uploads copy is published to S3 and removed."""
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    settings.MEDIA_ROOT = str(tmp_path / "media")

    user = UserFactory()
    equipment = _equipment()
    agent = _agent()
    booking = _booking(user, equipment)
    result, attachment = _seed_dsa_attachment(
        booking=booking,
        agent=agent,
        equipment=equipment,
        storage_root=tmp_path,
        filename="pxrd.csv",
        content=b"col1,col2\n",
    )
    local = tmp_path / attachment.storage_path
    assert local.is_file()

    uploaded: dict = {}

    class FakeS3:
        def upload_file(self, Filename, Bucket, Key, ExtraArgs=None):
            uploaded["key"] = Key
            uploaded["bucket"] = Bucket
            uploaded["bytes"] = Path(Filename).read_bytes()

        def head_object(self, Bucket, Key):
            assert Key == uploaded["key"]
            return {}

    fake = FakeS3()
    import iic_booking.sync.services.results_s3 as results_s3

    monkeypatch.setattr(results_s3, "_s3_client", lambda: (fake, "test-bucket"))

    from iic_booking.sync.services.result_processing import _publish_attachment_to_s3_and_cleanup

    _publish_attachment_to_s3_and_cleanup(attachment.id, booking.pk)

    attachment.refresh_from_db()
    assert attachment.s3_key == f"Results/{booking.virtual_booking_id}/pxrd.csv"
    assert uploaded["bytes"] == b"col1,col2\n"
    assert not local.exists()
    assert attachment.storage_path == ""
    assert result.id  # keep import row
