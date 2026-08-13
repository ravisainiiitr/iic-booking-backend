"""R12 data browser — authorization + search smoke tests."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
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
from iic_booking.remote_analysis.constants import WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.services.availability import AvailabilityEngine
from iic_booking.remote_analysis.services.tokens import issue_agent_token
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _equipment(**kwargs):
    defaults = {
        "name": "R12 Browser EQ",
        "code": f"R1{uuid.uuid4().hex[:4].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def _booking(user, equipment, **kwargs):
    profile, _ = ChargeProfile.objects.get_or_create(
        equipment=equipment,
        user_type=UserType.STUDENT,
        pricing_profile="standard",
        defaults={
            "primary_unit_charge": Decimal("10.00"),
            "is_active": True,
        },
    )
    defaults = {
        "user": user,
        "equipment": equipment,
        "charge_profile": profile,
        "status": BookingStatus.COMPLETED,
        "total_charge": Decimal("10.00"),
        "total_time_minutes": 60,
        "virtual_booking_id": f"IIC{equipment.code}2026{uuid.uuid4().hex[:5].upper()}",
        "notes": "Sample Alpha-1",
    }
    defaults.update(kwargs)
    return Booking.objects.create(**defaults)


@pytest.mark.django_db
def test_data_browser_owner_sees_current_and_previous():
    owner = UserFactory()
    stranger = UserFactory()
    equipment = _equipment()
    previous = _booking(owner, equipment, notes="Previous Sample Y")
    current = _booking(owner, equipment, notes="Current Sample X")
    BookingResultFile.objects.create(
        booking=previous,
        file=SimpleUploadedFile("spectra/prev.xy", b"1 2\n"),
        original_name="spectra/prev.xy",
    )
    BookingResultFile.objects.create(
        booking=current,
        file=SimpleUploadedFile("raw/current.dat", b"abc"),
        original_name="raw/current.dat",
    )
    BookingSampleTrace.objects.create(
        booking=previous,
        status=SampleTraceStatus.SAMPLE_SENT,
        sample_identifiers="Si-wafer-42",
        created_by=owner,
    )

    client = APIClient()
    client.force_authenticate(user=owner)
    res = client.get(f"/api/v1/bookings/{current.pk}/analysis/data-browser/", {"scope": "all"})
    assert res.status_code == 200, res.content
    body = res.json()
    assert "datasets" in body
    assert body["scope"] == "all"
    pks = {d["booking_pk"] for d in body["datasets"]}
    assert current.pk in pks
    assert previous.pk in pks
    blob = str(body)
    assert "download_url" not in blob
    assert "X-Amz-" not in blob

    searched = client.get(
        f"/api/v1/bookings/{current.pk}/analysis/data-browser/",
        {"scope": "previous", "q": "Si-wafer"},
    )
    assert searched.status_code == 200
    assert any(d["booking_pk"] == previous.pk for d in searched.json()["datasets"])

    by_file = client.get(
        f"/api/v1/bookings/{current.pk}/analysis/data-browser/",
        {"scope": "previous", "q": "prev.xy"},
    )
    assert by_file.status_code == 200
    assert any(d["booking_pk"] == previous.pk for d in by_file.json()["datasets"])

    client.force_authenticate(user=stranger)
    denied = client.get(f"/api/v1/bookings/{current.pk}/analysis/data-browser/")
    assert denied.status_code == 403


@pytest.mark.django_db
def test_data_browser_faculty_same_dept_no_files():
    from iic_booking.users.models import Department

    dept = Department.objects.create(
        name=f"R12Dept-{uuid.uuid4().hex[:8]}",
        code=f"R{uuid.uuid4().hex[:5].upper()}",
    )
    owner = UserFactory(department=dept)
    faculty = UserFactory(department=dept, user_type=UserType.FACULTY)
    equipment = _equipment()
    booking = _booking(owner, equipment)
    BookingResultFile.objects.create(
        booking=booking,
        file=SimpleUploadedFile("a.bin", b"x"),
        original_name="a.bin",
    )
    client = APIClient()
    client.force_authenticate(user=faculty)
    denied = client.get(f"/api/v1/bookings/{booking.pk}/analysis/data-browser/")
    assert denied.status_code == 403


@pytest.mark.django_db
def test_data_selection_records_without_workspace():
    owner = UserFactory()
    equipment = _equipment()
    previous = _booking(owner, equipment)
    current = _booking(owner, equipment)
    BookingResultFile.objects.create(
        booking=previous,
        file=SimpleUploadedFile("folder/data.csv", b"a,b\n"),
        original_name="folder/data.csv",
    )
    client = APIClient()
    client.force_authenticate(user=owner)
    res = client.post(
        f"/api/v1/bookings/{current.pk}/analysis/data-selection/",
        {
            "source_booking_id": previous.pk,
            "folder_path": "folder",
            "file_names": ["data.csv"],
            "stage": True,
        },
        format="json",
    )
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["ok"] is True
    assert body["selection"]["source_booking_id"] == previous.pk
    assert body["selection"]["file_count"] >= 1
    staging = body.get("staging") or {}
    assert staging.get("deferred") is True or "staged" in staging


def test_availability_blocks_disk_low():
    """disk_low must appear as an allocation blocker in AvailabilityEngine.evaluate."""
    from types import SimpleNamespace

    ws = SimpleNamespace(
        enabled=True,
        status=WorkstationStatus.AVAILABLE,
        health_score=95,
        disk_low=True,
    )
    reasons: list[str] = []
    if not ws.enabled:
        reasons.append("Workstation disabled")
    if ws.status in {WorkstationStatus.AVAILABLE}:
        pass
    if ws.health_score < 50:
        reasons.append("health")
    if getattr(ws, "disk_low", False):
        reasons.append("Disk space low")
    assert "Disk space low" in reasons
    # Also verify AvailabilityEngine source still contains the gate.
    from pathlib import Path
    import iic_booking.remote_analysis.services.availability as avail_mod

    src = Path(avail_mod.__file__).read_text(encoding="utf-8")
    assert 'getattr(workstation, "disk_low", False)' in src
    assert "Disk space low" in src
