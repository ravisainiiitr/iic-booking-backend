"""Staff add more result files to a completed booking (results sent in batches)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment import api_views
from iic_booking.equipment.models import (
    Booking,
    BookingEvent,
    BookingResultFile,
    BookingResultView,
    BookingStatus,
    ChargeProfile,
    Equipment,
)
from iic_booking.equipment.pending_actions import collect_pending_actions
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def media(tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    with override_settings(AWS_STORAGE_BUCKET_NAME=""):
        yield


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(
        api_views,
        "_send_results_available_push_and_email",
        lambda booking, added_files=None, note="": calls.append((booking.pk, added_files, note)),
    )
    return calls


def _booking(status=BookingStatus.COMPLETED):
    owner = UserFactory(admin_approved=True, user_type=UserType.STUDENT)
    equipment = Equipment.objects.create(
        name="Batch Results EQ", code=f"BR{uuid.uuid4().hex[:4].upper()}", slot_duration_minutes=60, user_rating_enabled=False
    )
    profile = ChargeProfile.objects.create(
        equipment=equipment, user_type=UserType.STUDENT, primary_unit_charge=Decimal("10.00")
    )
    return Booking.objects.create(
        user=owner,
        equipment=equipment,
        charge_profile=profile,
        status=status,
        completed_at=timezone.now() - timedelta(days=1) if status == BookingStatus.COMPLETED else None,
        total_charge=Decimal("10.00"),
        total_time_minutes=60,
        virtual_booking_id=f"IIC{equipment.code}202600001",
    )


def _admin_client():
    client = APIClient()
    client.force_authenticate(user=UserFactory(admin_approved=True, user_type=UserType.ADMIN))
    return client


def _post(client, booking, *names, **extra):
    data = {"results": [SimpleUploadedFile(n, b"x" * 10) for n in names], **extra}
    return client.post(f"/api/bookings/{booking.pk}/add-results/", data, format="multipart")


def test_adds_files_records_event_and_notifies_the_user(sent):
    booking = _booking()
    BookingResultFile.objects.create(booking=booking, file=SimpleUploadedFile("day1.csv", b"1"), original_name="day1.csv")

    res = _post(_admin_client(), booking, "day2_a.csv", "day2_b.csv", note="Remaining samples")

    assert res.status_code == 200, res.data
    assert res.data["uploaded_files"] == ["day2_a.csv", "day2_b.csv"]
    assert res.data["notified"] is True
    assert sorted(booking.result_files.values_list("original_name", flat=True)) == ["day1.csv", "day2_a.csv", "day2_b.csv"]
    event = BookingEvent.objects.filter(booking=booking).latest("event_id")
    assert event.metadata["results_added"] == ["day2_a.csv", "day2_b.csv"]
    assert "Remaining samples" in event.comment
    assert sent == [(booking.pk, ["day2_a.csv", "day2_b.csv"], "Remaining samples")]


def test_notify_off_skips_the_email(sent):
    booking = _booking()
    res = _post(_admin_client(), booking, "extra.csv", notify="false")
    assert res.status_code == 200, res.data
    assert res.data["notified"] is False
    assert sent == []


def test_refused_before_completion_without_files_and_for_users(sent):
    booked = _booking(status=BookingStatus.BOOKED)
    assert _post(_admin_client(), booked, "a.csv").status_code == 400

    completed = _booking()
    res = _admin_client().post(f"/api/bookings/{completed.pk}/add-results/", {}, format="multipart")
    assert res.status_code == 400

    owner_client = APIClient()
    owner_client.force_authenticate(user=completed.user)
    assert _post(owner_client, completed, "a.csv").status_code == 403
    assert not BookingResultFile.objects.filter(booking__in=[booked, completed]).exists()
    assert sent == []


def test_files_added_after_the_last_download_show_as_new_again(sent):
    booking = _booking()
    Booking.objects.filter(pk=booking.pk).update(created_at=timezone.now() - timedelta(days=2))
    BookingResultFile.objects.create(booking=booking, file=SimpleUploadedFile("day1.csv", b"1"), original_name="day1.csv")
    BookingResultFile.objects.filter(booking=booking).update(created_at=timezone.now() - timedelta(hours=20))
    BookingResultView.objects.create(booking=booking, user=booking.user)
    BookingResultView.objects.filter(booking=booking).update(last_viewed_at=timezone.now() - timedelta(hours=10))
    owner_client = APIClient()
    owner_client.force_authenticate(user=booking.user)

    def inbox_new():
        res = owner_client.get("/api/results/inbox/")
        assert res.status_code == 200, res.data
        return [r["is_new"] for r in res.data["results"] if r["booking_id"] == booking.pk]

    def pending_new_results():
        return [i["count"] for i in collect_pending_actions(booking.user) if i["key"] == "new_results"]

    assert inbox_new() == [False]
    assert pending_new_results() in ([], [0])

    assert _post(_admin_client(), booking, "day2.csv").status_code == 200

    assert inbox_new() == [True]
    assert pending_new_results() == [1]
