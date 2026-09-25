"""Results inbox (viewed ordering), internal research-data sharing, public equipment availability."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment import results_sharing_views
from iic_booking.equipment.models import (
    Booking,
    BookingDataShare,
    BookingResultFile,
    BookingResultView,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    DynamicInputField,
    Equipment,
    SlotMaster,
)
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _client(user=None) -> APIClient:
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    return client


def _user(**kwargs):
    kwargs.setdefault("email_verified", True)
    kwargs.setdefault("admin_approved", True)
    return UserFactory(**kwargs)


def _department(department_type="internal"):
    tag = uuid.uuid4().hex[:6].upper()
    return Department.objects.create(
        name=f"RS-Dept-{tag}",
        code=f"RS{tag[:4]}",
        department_type=department_type,
        equipment_booking_enabled=True,
        equipment_visibility_enabled=True,
    )


def _equipment(department=None, **kwargs):
    defaults = {
        "name": f"RS EQ {uuid.uuid4().hex[:4]}",
        "code": f"RS{uuid.uuid4().hex[:5].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
        "status": "ACTIVE",
        "internal_department": department,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def _booking(user, equipment, status=BookingStatus.COMPLETED, **kwargs):
    profile, _ = ChargeProfile.objects.get_or_create(
        equipment=equipment, user_type=UserType.STUDENT, defaults={"primary_unit_charge": Decimal("10.00")}
    )
    defaults = {
        "user": user,
        "equipment": equipment,
        "charge_profile": profile,
        "status": status,
        "total_charge": Decimal("10.00"),
        "total_time_minutes": 60,
        "virtual_booking_id": f"IIC{equipment.code}{uuid.uuid4().hex[:4]}",
    }
    defaults.update(kwargs)
    return Booking.objects.create(**defaults)


def _result_file(booking, name="result.txt", created_at=None):
    brf = BookingResultFile.objects.create(
        booking=booking, file=SimpleUploadedFile(name, b"data"), original_name=name
    )
    if created_at is not None:
        BookingResultFile.objects.filter(pk=brf.pk).update(created_at=created_at)
    return brf


@pytest.fixture
def media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.AWS_STORAGE_BUCKET_NAME = ""
    return tmp_path


@pytest.fixture
def internal_dept(db):
    return _department("internal")


@pytest.fixture
def student(internal_dept):
    return _user(user_type=UserType.STUDENT, department=internal_dept, name="Asha Student")


@pytest.fixture
def faculty(internal_dept):
    return _user(user_type=UserType.FACULTY, department=internal_dept, name="Ravi Faculty")


# ---------------------------------------------------------------- results inbox


@pytest.mark.django_db
def test_inbox_lists_new_first_then_viewed_most_recent_first(media_tmp, student, internal_dept):
    eq = _equipment(internal_dept)
    now = timezone.now()
    old = _booking(student, eq)
    mid = _booking(student, eq)
    new = _booking(student, eq)
    _result_file(old, created_at=now - timedelta(days=3))
    _result_file(mid, created_at=now - timedelta(days=2))
    _result_file(new, created_at=now - timedelta(days=1))
    _booking(student, eq)  # no results: not listed
    BookingResultView.objects.create(booking=new, user=student)

    resp = _client(student).get("/api/results/inbox/")

    assert resp.status_code == 200
    ids = [r["booking_id"] for r in resp.data["results"]]
    assert ids == [mid.booking_id, old.booking_id, new.booking_id]
    assert resp.data["new_count"] == 2
    assert resp.data["results"][-1]["is_new"] is False


@pytest.mark.django_db
def test_file_download_marks_viewed_and_moves_booking_to_end(media_tmp, student, internal_dept):
    eq = _equipment(internal_dept)
    now = timezone.now()
    first = _booking(student, eq)
    second = _booking(student, eq)
    brf = _result_file(first, created_at=now - timedelta(hours=1))
    _result_file(second, created_at=now - timedelta(hours=2))
    client = _client(student)

    assert [r["booking_id"] for r in client.get("/api/results/inbox/").data["results"]] == [
        first.booking_id,
        second.booking_id,
    ]
    download = client.get(f"/api/bookings/{first.booking_id}/results/files/{brf.pk}/")
    assert download.status_code == 200

    assert BookingResultView.objects.filter(booking=first, user=student).exists()
    ids = [r["booking_id"] for r in client.get("/api/results/inbox/").data["results"]]
    assert ids == [second.booking_id, first.booking_id]


@pytest.mark.django_db
def test_mark_viewed_endpoint_owner_only(media_tmp, student, faculty, internal_dept):
    eq = _equipment(internal_dept)
    booking = _booking(student, eq)
    _result_file(booking)

    assert _client(faculty).post(f"/api/bookings/{booking.booking_id}/results/mark-viewed/").status_code == 403
    resp = _client(student).post(f"/api/bookings/{booking.booking_id}/results/mark-viewed/")
    assert resp.status_code == 200
    assert resp.data["viewed_at"] is not None


@pytest.mark.django_db
def test_mark_viewed_respects_rating_gate(media_tmp, student, internal_dept):
    eq = _equipment(internal_dept, user_rating_enabled=True)
    booking = _booking(student, eq)
    _result_file(booking)

    resp = _client(student).post(f"/api/bookings/{booking.booking_id}/results/mark-viewed/")

    assert resp.status_code == 403
    assert resp.data["code"] == "rating_required"
    assert not BookingResultView.objects.exists()


# ---------------------------------------------------------------- user search / details


@pytest.mark.django_db
def test_search_returns_only_internal_students_and_faculty(student, faculty, internal_dept):
    external_dept = _department("external")
    _user(user_type=UserType.EXTERNAL, name="Asha External")
    _user(user_type=UserType.STARTUP_INCUBATED_IITR, name="Asha Startup")
    _user(user_type=UserType.FACULTY, department=external_dept, name="Asha Visiting")
    _user(user_type=UserType.STUDENT, department=internal_dept, name="Asha Inactive", force_inactive=True)
    peer = _user(user_type=UserType.INDIVIDUAL_STUDENT, name="Asha Peer")

    resp = _client(faculty).get("/api/data-sharing/users/search/", {"q": "asha"})

    assert resp.status_code == 200
    assert {r["id"] for r in resp.data["results"]} == {student.pk, peer.pk}
    assert _client(student).get("/api/data-sharing/users/search/", {"q": "as"}).data["results"] == []
    own = _client(student).get("/api/data-sharing/users/search/", {"q": "asha"}).data["results"]
    assert student.pk not in {r["id"] for r in own}


@pytest.mark.django_db
def test_search_and_details_rejected_for_external_requester(student):
    external = _user(user_type=UserType.EXTERNAL)
    client = _client(external)
    assert client.get("/api/data-sharing/users/search/", {"q": "asha"}).status_code == 403
    assert client.get(f"/api/data-sharing/users/{student.pk}/").status_code == 403


@pytest.mark.django_db
def test_user_details_for_confirmation(student, faculty):
    external = _user(user_type=UserType.EXTERNAL)
    client = _client(student)

    resp = client.get(f"/api/data-sharing/users/{faculty.pk}/")
    assert resp.status_code == 200
    for key in ("name", "email", "department", "user_type_label", "id_number", "designation"):
        assert key in resp.data
    assert "phone" not in resp.data
    assert client.get(f"/api/data-sharing/users/{external.pk}/").status_code == 404
    assert client.get(f"/api/data-sharing/users/{student.pk}/").status_code == 404


# ---------------------------------------------------------------- sharing


@pytest.mark.django_db
def test_share_requires_confirmation_owner_and_internal_target(
    media_tmp, student, faculty, internal_dept, monkeypatch, django_capture_on_commit_callbacks
):
    notified = []
    monkeypatch.setattr(results_sharing_views, "notify_share_recipient", lambda share: notified.append(share.pk))
    eq = _equipment(internal_dept)
    booking = _booking(student, eq)
    _result_file(booking)
    url = f"/api/bookings/{booking.booking_id}/shares/"
    owner = _client(student)

    assert owner.post(url, {"user_id": faculty.pk}, format="json").data["code"] == "confirmation_required"
    assert _client(faculty).post(url, {"user_id": student.pk, "confirm": True}, format="json").status_code == 403
    external = _user(user_type=UserType.EXTERNAL)
    assert owner.post(url, {"user_id": external.pk, "confirm": True}, format="json").status_code == 400

    with django_capture_on_commit_callbacks(execute=True):
        created = owner.post(url, {"user_id": faculty.pk, "confirm": True}, format="json")
    assert created.status_code == 201
    assert created.data["shared_with"]["id"] == faculty.pk
    assert notified == [created.data["id"]]
    assert owner.post(url, {"user_id": faculty.pk, "confirm": True}, format="json").status_code == 409

    listing = owner.get(url)
    assert listing.data["can_share"] is True
    assert [s["shared_with"]["id"] for s in listing.data["shares"]] == [faculty.pk]


@pytest.mark.django_db
def test_share_blocked_until_completed_with_results(media_tmp, student, faculty, internal_dept):
    eq = _equipment(internal_dept)
    booked = _booking(student, eq, status=BookingStatus.BOOKED)
    no_results = _booking(student, eq)
    owner = _client(student)

    for booking in (booked, no_results):
        url = f"/api/bookings/{booking.booking_id}/shares/"
        assert owner.get(url).data["can_share"] is False
        assert owner.post(url, {"user_id": faculty.pk, "confirm": True}, format="json").status_code == 400
    assert not BookingDataShare.objects.exists()


@pytest.mark.django_db
def test_recipient_can_download_until_revoked(media_tmp, student, faculty, internal_dept):
    eq = _equipment(internal_dept)
    booking = _booking(student, eq)
    brf = _result_file(booking)
    outsider = _user(user_type=UserType.STUDENT, department=internal_dept)
    share = BookingDataShare.objects.create(booking=booking, shared_by=student, shared_with=faculty)
    recipient = _client(faculty)
    file_url = f"/api/bookings/{booking.booking_id}/results/files/{brf.pk}/"

    assert recipient.get(f"/api/bookings/{booking.booking_id}/results/").status_code == 200
    assert recipient.get(file_url).status_code == 200
    assert BookingResultView.objects.filter(booking=booking, user=faculty).exists()
    assert not BookingResultView.objects.filter(booking=booking, user=student).exists()
    assert _client(outsider).get(file_url).status_code == 403

    revoke = _client(student).post(f"/api/bookings/{booking.booking_id}/shares/{share.pk}/revoke/")
    assert revoke.status_code == 200
    assert recipient.get(file_url).status_code == 403
    assert _client(faculty).post(f"/api/bookings/{booking.booking_id}/shares/{share.pk}/revoke/").status_code == 403


@pytest.mark.django_db
def test_recipient_still_subject_to_booking_gates(media_tmp, student, faculty, internal_dept):
    eq = _equipment(internal_dept, user_rating_enabled=True)
    booking = _booking(student, eq)
    brf = _result_file(booking)
    BookingDataShare.objects.create(booking=booking, shared_by=student, shared_with=faculty)

    resp = _client(faculty).get(f"/api/bookings/{booking.booking_id}/results/files/{brf.pk}/")

    assert resp.status_code == 403


@pytest.mark.django_db
def test_shared_with_me_lists_booking_details(media_tmp, student, faculty, internal_dept):
    eq = _equipment(internal_dept)
    DynamicInputField.objects.create(equipment=eq, field_key="A", field_label="Number of samples")
    booking = _booking(student, eq, input_values={"A": 3})
    _result_file(booking)
    BookingDataShare.objects.create(booking=booking, shared_by=student, shared_with=faculty)
    revoked = _booking(student, eq)
    BookingDataShare.objects.create(
        booking=revoked, shared_by=student, shared_with=faculty, revoked_at=timezone.now()
    )

    resp = _client(faculty).get("/api/data-sharing/shared-with-me/")

    assert resp.status_code == 200
    assert [r["booking_id"] for r in resp.data["results"]] == [booking.booking_id]
    row = resp.data["results"][0]
    assert row["shared_by"]["id"] == student.pk
    assert row["equipment_name"] == eq.name
    assert row["inputs"] == [{"key": "A", "label": "Number of samples", "value": "3"}]
    assert row["results_accessible"] is True
    external = _user(user_type=UserType.EXTERNAL)
    assert _client(external).get("/api/data-sharing/shared-with-me/").data["eligible"] is False


# ---------------------------------------------------------------- public availability


def _slot(equipment, start, number, **kwargs):
    master, _ = SlotMaster.objects.get_or_create(
        equipment=equipment,
        slot_number=number,
        defaults={
            "open_time": timezone.localtime(start).time().replace(microsecond=0),
            "close_time": (timezone.localtime(start) + timedelta(hours=1)).time().replace(microsecond=0),
            "is_active": True,
        },
    )
    return DailySlot.objects.create(
        slot_master=master,
        date=timezone.localtime(start).date(),
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        **kwargs,
    )


@pytest.mark.django_db
def test_public_availability_anonymous_read_only_and_private_fields_hidden(internal_dept, student):
    cache.clear()
    eq = _equipment(internal_dept, name="Alpha Diffractometer")
    other = _equipment(internal_dept, name="Beta Microscope")
    base = timezone.localtime(timezone.now() + timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
    _slot(eq, base, 1, status="AVAILABLE")
    _slot(eq, base + timedelta(hours=2), 2, status="AVAILABLE")
    _slot(eq, base + timedelta(days=1), 1, status="AVAILABLE")
    _slot(eq, base + timedelta(hours=4), 3, status="AVAILABLE", home_department_only=True)
    booked = _booking(student, eq, status=BookingStatus.BOOKED)
    _slot(eq, base + timedelta(hours=6), 4, status="BOOKED", booking=booked)
    _slot(eq, timezone.now() - timedelta(days=1), 5, status="AVAILABLE")
    slot_count = DailySlot.objects.count()

    resp = _client().get("/api/public/equipment-availability/", {"days": 14})

    assert resp.status_code == 200
    assert DailySlot.objects.count() == slot_count
    rows = {r["equipment_id"]: r for r in resp.data["equipment"]}
    assert resp.data["equipment"][0]["equipment_id"] == eq.equipment_id
    alpha = rows[eq.equipment_id]
    assert alpha["total_available_slots"] == 3
    assert [d["available_slots"] for d in alpha["dates"]] == [2, 1]
    assert rows[other.equipment_id]["next_available_at"] is None
    assert "booking_user_name" not in str(resp.data)
    assert student.email not in str(resp.data)


@pytest.mark.django_db
def test_public_availability_filters(internal_dept):
    cache.clear()
    other_dept = _department("internal")
    eq = _equipment(internal_dept, name="Gamma Spectrometer")
    _equipment(other_dept, name="Delta Furnace")

    by_dept = _client().get("/api/public/equipment-availability/", {"department": internal_dept.pk})
    assert [r["equipment_id"] for r in by_dept.data["equipment"]] == [eq.equipment_id]
    by_search = _client().get("/api/public/equipment-availability/", {"search": "furnace"})
    assert [r["name"] for r in by_search.data["equipment"]] == ["Delta Furnace"]


@pytest.mark.django_db
def test_ensure_upcoming_slots_generates_for_equipment_with_masters(internal_dept):
    from datetime import time

    from iic_booking.equipment.tasks import ensure_upcoming_slots

    with_master = _equipment(internal_dept)
    without_master = _equipment(internal_dept)
    SlotMaster.objects.create(equipment=with_master, slot_number=1, open_time=time(9), close_time=time(10), is_active=True)

    created = ensure_upcoming_slots()

    assert created > 0
    assert DailySlot.objects.filter(slot_master__equipment=with_master).count() == created
    assert not SlotMaster.objects.filter(equipment=without_master).exists()
    assert ensure_upcoming_slots() == 0
