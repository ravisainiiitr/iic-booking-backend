"""R14.2 dummy-booking API E2E — current/previous/upload/selection (no production DB)."""

from __future__ import annotations

from decimal import Decimal
import uuid

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from iic_booking.equipment.models import (
    Booking,
    BookingResultFile,
    BookingSampleTrace,
    BookingStatus,
    ChargeProfile,
    Equipment,
    SampleTraceStatus,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


@pytest.fixture(autouse=True)
def _local_file_storage(settings, tmp_path):
    media = tmp_path / "media"
    media.mkdir(parents=True, exist_ok=True)
    settings.MEDIA_ROOT = str(media)
    settings.MEDIA_URL = "/media/"
    settings.STORAGES = {
        "default": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(media)},
        },
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage",
        },
    }
    from django.core.files.storage import storages

    try:
        storages._storages.clear()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass


def _equipment(**kwargs):
    defaults = {
        "name": "R14 Dummy FE-SEM",
        "code": f"DM{uuid.uuid4().hex[:4].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
        "enable_remote_analysis": True,
        "auto_complete_booking": False,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def _booking(owner, equipment, *, virtual, notes=""):
    profile, _ = ChargeProfile.objects.get_or_create(
        equipment=equipment,
        user_type=UserType.STUDENT,
        pricing_profile="standard",
        defaults={"primary_unit_charge": Decimal("10.00")},
    )
    return Booking.objects.create(
        user=owner,
        equipment=equipment,
        charge_profile=profile,
        status=BookingStatus.COMPLETED,
        total_charge=Decimal("10.00"),
        total_time_minutes=60,
        virtual_booking_id=virtual,
        notes=notes,
    )


@pytest.mark.django_db
def test_dummy_booking_current_previous_upload_and_selection_before_allocation():
    owner = UserFactory()
    stranger = UserFactory()
    equipment = _equipment()
    other_eq = _equipment(name="Other EQ")

    previous = _booking(
        owner, equipment, virtual="IICPREV202600008", notes="Previous Sample Y"
    )
    current = _booking(
        owner, equipment, virtual="IICAPREO202600005", notes="Current Sample X"
    )
    foreign = _booking(owner, other_eq, virtual="IICOTHEREQ202600007")

    BookingResultFile.objects.create(
        booking=previous,
        file=SimpleUploadedFile("SEM_Raw/prev_scan.tif", b"prev-bytes"),
        original_name="SEM_Raw/prev_scan.tif",
    )
    BookingResultFile.objects.create(
        booking=current,
        file=SimpleUploadedFile("SEM_Raw/image_01.tif", b"cur-bytes"),
        original_name="SEM_Raw/image_01.tif",
    )
    BookingSampleTrace.objects.create(
        booking=previous,
        status=SampleTraceStatus.SAMPLE_SENT,
        sample_identifiers="Si-wafer-42",
        created_by=owner,
    )

    client = APIClient()
    client.force_authenticate(user=owner)

    current_res = client.get(
        f"/api/v1/bookings/{current.pk}/analysis/data-browser/?scope=current"
    )
    assert current_res.status_code == 200, current_res.content
    current_body = current_res.json()
    assert current_body["datasets"]
    row = current_body["datasets"][0]
    assert row["virtual_booking_id"] == "IICAPREO202600005"
    assert row["booking_id"] == "IICAPREO202600005"
    assert row["booking_id"] != str(current.pk)
    assert row["is_current"] is True
    folders = row.get("folders") or []
    assert any(
        f.get("path") == "SEM_Raw"
        or any("image_01.tif" in str(x) for x in (f.get("files") or []))
        for f in folders
    )

    prev_res = client.get(
        f"/api/v1/bookings/{current.pk}/analysis/data-browser/?scope=previous"
    )
    assert prev_res.status_code == 200
    prev_ids = {d["virtual_booking_id"] for d in prev_res.json()["datasets"]}
    assert "IICPREV202600008" in prev_ids
    assert "IICAPREO202600005" not in prev_ids
    assert "IICOTHEREQ202600007" not in prev_ids
    assert foreign.pk

    by_virtual = client.get(
        f"/api/v1/bookings/{current.pk}/analysis/data-browser/?scope=previous&q=IICPREV202600008"
    )
    assert by_virtual.status_code == 200
    assert [d["virtual_booking_id"] for d in by_virtual.json()["datasets"]] == [
        "IICPREV202600008"
    ]

    by_sample = client.get(
        f"/api/v1/bookings/{current.pk}/analysis/data-browser/?scope=previous&q=Si-wafer-42"
    )
    assert by_sample.status_code == 200
    assert any(d["booking_pk"] == previous.pk for d in by_sample.json()["datasets"])

    by_file = client.get(
        f"/api/v1/bookings/{current.pk}/analysis/data-browser/?scope=previous&q=prev_scan.tif"
    )
    assert by_file.status_code == 200
    assert any(d["booking_pk"] == previous.pk for d in by_file.json()["datasets"])

    by_folder = client.get(
        f"/api/v1/bookings/{current.pk}/analysis/data-browser/?scope=previous&q=SEM_Raw"
    )
    assert by_folder.status_code == 200
    assert any(d["booking_pk"] == previous.pk for d in by_folder.json()["datasets"])

    client.force_authenticate(user=stranger)
    denied = client.get(f"/api/v1/bookings/{current.pk}/analysis/data-browser/?scope=current")
    assert denied.status_code == 403
    denied_select = client.post(
        f"/api/v1/bookings/{current.pk}/analysis/data-selection/",
        {"source_booking_id": current.pk, "stage": False},
        format="json",
    )
    assert denied_select.status_code == 403

    client.force_authenticate(user=owner)
    selected = client.post(
        f"/api/v1/bookings/{current.pk}/analysis/data-selection/",
        {
            "source_booking_id": current.pk,
            "folder_path": "SEM_Raw",
            "file_names": ["image_01.tif"],
            "stage": False,
        },
        format="json",
    )
    assert selected.status_code == 200, selected.content
    sel_body = selected.json()
    assert sel_body["ok"] is True
    assert sel_body["selection"]["virtual_booking_id"] == "IICAPREO202600005"
    current.refresh_from_db()
    assert current.analysis_data_selection["source_booking_id"] == current.pk
    assert current.analysis_reservation_id is None

    upload = client.post(
        f"/api/v1/bookings/{current.pk}/analysis/files/upload/",
        {"file": SimpleUploadedFile("analysis_dummy_upload.txt", b"dummy-upload-ok")},
        format="multipart",
    )
    assert upload.status_code == 200, upload.content
    up_body = upload.json()
    assert up_body.get("ok") is True
    assert "analysis_dummy_upload.txt" in str(up_body.get("file") or {})
    current.refresh_from_db()
    stored = current.analysis_data_selection or {}
    assert stored.get("source") == "upload"
    names = stored.get("file_names") or []
    assert any("analysis_dummy_upload.txt" in str(n) for n in names)
    # Existing R12 upload may create a workspace reservation with auto_allocate=False.
    # That is not RAA PC allocation — no workstation must be assigned.
    reservation = current.analysis_reservation
    assert reservation is not None
    assert getattr(reservation, "workstation_id", None) is None
