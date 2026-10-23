"""Baseline for the manual I-STEM FBR workflow before the I-STEM API integration.

The booking user submits the FBR number, the equipment's Officer In-charge (or an admin) marks it executed or
invalid, and the owner's result downloads stay locked until it is executed. These tests pin the current rules so
the integration cannot change them unnoticed.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment import api_views
from iic_booking.equipment.models import (
    Booking,
    BookingEvent,
    BookingEventType,
    BookingResultFile,
    BookingStatus,
    ChargeProfile,
    Equipment,
    EquipmentManager,
    IstemFbrStatus,
)
from iic_booking.equipment.results_download import make_download_token
from iic_booking.equipment.results_sharing_views import _results_lock
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _user(user_type=UserType.EXTERNAL):
    return UserFactory(user_type=user_type, admin_approved=True)


def _equipment():
    return Equipment.objects.create(
        name="FBR Flow EQ",
        code=f"FB{uuid.uuid4().hex[:5].upper()}",
        slot_duration_minutes=60,
        user_rating_enabled=False,
    )


def _booking(user, *, require_istem_fbr=True, equipment=None, **kwargs):
    equipment = equipment or _equipment()
    profile = ChargeProfile.objects.create(
        equipment=equipment,
        user_type=UserType.EXTERNAL,
        primary_unit_charge=Decimal("100.00"),
        require_istem_fbr=require_istem_fbr,
    )
    defaults = {
        "user": user,
        "equipment": equipment,
        "charge_profile": profile,
        "user_type_snapshot": UserType.EXTERNAL,
        "status": BookingStatus.BOOKED,
        "total_charge": Decimal("100.00"),
        "total_time_minutes": 60,
        "virtual_booking_id": f"IIC{equipment.code}{uuid.uuid4().hex[:6].upper()}",
        "istem_fbr_status": IstemFbrStatus.PENDING_FBR if require_istem_fbr else None,
    }
    defaults.update(kwargs)
    return Booking.objects.create(**defaults)


def _oic_of(equipment):
    oic = _user(UserType.MANAGER)
    EquipmentManager.objects.create(equipment=equipment, manager=oic)
    return oic


def _client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _submit(user, booking, number="FBR-001"):
    return _client(user).patch(
        f"/api/bookings/{booking.pk}/istem-fbr/", {"istem_fbr_number": number}, format="json"
    )


def _review(user, booking, **data):
    return _client(user).post(f"/api/bookings/{booking.pk}/istem-fbr/review/", data, format="json")


@pytest.fixture
def notify_oic(monkeypatch):
    """Records the OIC email / in-app notification instead of sending it."""
    recorder = Mock()
    monkeypatch.setattr(api_views, "_notify_oic_istem_fbr_submitted", recorder)
    return recorder


@pytest.fixture
def media(tmp_path, settings):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    with override_settings(AWS_STORAGE_BUCKET_NAME=""):
        yield tmp_path


def _with_results(booking):
    for name, content in (("data.csv", b"a,b\n1,2\n"), ("notes.txt", b"hello")):
        BookingResultFile.objects.create(
            booking=booking, file=SimpleUploadedFile(name, content), original_name=name
        )
    return booking


# --- User submission -------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_owner_submits_fbr_for_oic_verification(notify_oic):
    owner = _user()
    booking = _booking(owner)

    res = _submit(owner, booking, "  FBR-001  ")

    assert res.status_code == 200
    booking.refresh_from_db()
    assert booking.istem_fbr_number == "FBR-001"
    assert booking.istem_fbr_status == IstemFbrStatus.PENDING_OIC
    assert booking.istem_fbr_invalid_reason is None
    assert booking.istem_fbr_executed_at is None
    notify_oic.assert_called_once()
    assert notify_oic.call_args.args[0].pk == booking.pk
    assert BookingEvent.objects.filter(
        booking=booking, event_type=BookingEventType.COMMENT, comment__contains="FBR-001"
    ).exists()


@pytest.mark.django_db
def test_corrected_fbr_after_invalid_goes_back_to_oic(notify_oic):
    owner = _user()
    booking = _booking(
        owner,
        istem_fbr_status=IstemFbrStatus.INVALID,
        istem_fbr_number="WRONG",
        istem_fbr_invalid_reason="Number not found on I-STEM",
    )

    res = _submit(owner, booking, "FBR-002")

    assert res.status_code == 200
    booking.refresh_from_db()
    assert booking.istem_fbr_number == "FBR-002"
    assert booking.istem_fbr_status == IstemFbrStatus.PENDING_OIC
    assert booking.istem_fbr_invalid_reason is None


@pytest.mark.django_db
def test_completed_booking_accepts_fbr(notify_oic):
    owner = _user()
    booking = _booking(owner, status=BookingStatus.COMPLETED)

    assert _submit(owner, booking).status_code == 200
    booking.refresh_from_db()
    assert booking.istem_fbr_status == IstemFbrStatus.PENDING_OIC


@pytest.mark.django_db
def test_fbr_number_is_truncated_to_128_characters(notify_oic):
    owner = _user()
    booking = _booking(owner)

    assert _submit(owner, booking, "F" * 200).status_code == 200
    booking.refresh_from_db()
    assert booking.istem_fbr_number == "F" * 128


@pytest.mark.django_db
def test_only_the_booking_owner_can_submit(notify_oic):
    booking = _booking(_user())

    res = _submit(_user(), booking)

    assert res.status_code == 403
    booking.refresh_from_db()
    assert booking.istem_fbr_status == IstemFbrStatus.PENDING_FBR
    assert booking.istem_fbr_number == ""
    notify_oic.assert_not_called()


@pytest.mark.django_db
def test_blank_fbr_number_is_rejected(notify_oic):
    owner = _user()
    booking = _booking(owner)

    res = _submit(owner, booking, "   ")

    assert res.status_code == 400
    booking.refresh_from_db()
    assert booking.istem_fbr_status == IstemFbrStatus.PENDING_FBR
    notify_oic.assert_not_called()


@pytest.mark.django_db
def test_booking_outside_the_fbr_workflow_is_rejected(notify_oic):
    owner = _user()
    booking = _booking(owner, require_istem_fbr=False)

    res = _submit(owner, booking)

    assert res.status_code == 400
    booking.refresh_from_db()
    assert booking.istem_fbr_status is None
    assert booking.istem_fbr_number == ""


@pytest.mark.django_db
def test_profile_flag_alone_does_not_start_the_workflow_on_submit(notify_oic):
    # The FBR status is initialised when the booking is created / paid (or lazily by the bookings list);
    # submission itself does not initialise it.
    owner = _user()
    booking = _booking(owner, istem_fbr_status=None)

    res = _submit(owner, booking)

    assert res.status_code == 400
    booking.refresh_from_db()
    assert booking.istem_fbr_status is None


@pytest.mark.django_db
def test_unsettled_payment_blocks_submission(notify_oic):
    owner = _user()
    booking = _booking(owner, amount_due=Decimal("50.00"), payment_settled_at=None)

    res = _submit(owner, booking)

    assert res.status_code == 400
    assert "payment" in res.json()["error"].lower()
    booking.refresh_from_db()
    assert booking.istem_fbr_status == IstemFbrStatus.PENDING_FBR


@pytest.mark.django_db
def test_settled_payment_allows_submission(notify_oic):
    owner = _user()
    booking = _booking(owner, amount_due=Decimal("50.00"), payment_settled_at=timezone.now())

    assert _submit(owner, booking).status_code == 200


@pytest.mark.django_db
@pytest.mark.parametrize(
    "booking_status",
    [BookingStatus.PENDING_PAYMENT, BookingStatus.HOLD, BookingStatus.CANCELLED, BookingStatus.REFUNDED],
)
def test_submission_needs_a_booked_or_completed_booking(notify_oic, booking_status):
    owner = _user()
    booking = _booking(owner, status=booking_status)

    res = _submit(owner, booking)

    assert res.status_code == 400
    booking.refresh_from_db()
    assert booking.istem_fbr_status == IstemFbrStatus.PENDING_FBR


@pytest.mark.django_db
@pytest.mark.parametrize("fbr_status", [IstemFbrStatus.PENDING_OIC, IstemFbrStatus.EXECUTED])
def test_fbr_cannot_be_changed_while_pending_or_after_execution(notify_oic, fbr_status):
    owner = _user()
    booking = _booking(owner, istem_fbr_status=fbr_status, istem_fbr_number="FBR-OLD")

    res = _submit(owner, booking, "FBR-NEW")

    assert res.status_code == 400
    booking.refresh_from_db()
    assert booking.istem_fbr_number == "FBR-OLD"
    assert booking.istem_fbr_status == fbr_status
    notify_oic.assert_not_called()


# --- OIC / admin review ----------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_oic_of_the_equipment_marks_fbr_executed():
    booking = _booking(_user(), istem_fbr_status=IstemFbrStatus.PENDING_OIC, istem_fbr_number="FBR-001")
    oic = _oic_of(booking.equipment)

    res = _review(oic, booking, action="execute")

    assert res.status_code == 200
    booking.refresh_from_db()
    assert booking.istem_fbr_status == IstemFbrStatus.EXECUTED
    assert booking.istem_fbr_executed_at is not None
    assert booking.istem_fbr_verified_by_id == oic.pk
    assert booking.istem_fbr_invalid_reason is None


@pytest.mark.django_db
def test_admin_marks_fbr_executed():
    booking = _booking(_user(), istem_fbr_status=IstemFbrStatus.PENDING_OIC, istem_fbr_number="FBR-001")
    admin_user = _user(UserType.ADMIN)

    res = _review(admin_user, booking, action="execute")

    assert res.status_code == 200
    booking.refresh_from_db()
    assert booking.istem_fbr_status == IstemFbrStatus.EXECUTED
    assert booking.istem_fbr_verified_by_id == admin_user.pk


@pytest.mark.django_db
def test_oic_marks_fbr_invalid_with_a_reason():
    booking = _booking(_user(), istem_fbr_status=IstemFbrStatus.PENDING_OIC, istem_fbr_number="FBR-001")
    oic = _oic_of(booking.equipment)

    res = _review(oic, booking, action="invalidate", reason="Not found on I-STEM")

    assert res.status_code == 200
    booking.refresh_from_db()
    assert booking.istem_fbr_status == IstemFbrStatus.INVALID
    assert booking.istem_fbr_invalid_reason == "Not found on I-STEM"
    assert booking.istem_fbr_executed_at is None
    assert booking.istem_fbr_verified_by_id == oic.pk


@pytest.mark.django_db
def test_invalidating_needs_a_reason():
    booking = _booking(_user(), istem_fbr_status=IstemFbrStatus.PENDING_OIC, istem_fbr_number="FBR-001")
    oic = _oic_of(booking.equipment)

    res = _review(oic, booking, action="invalidate", reason="  ")

    assert res.status_code == 400
    booking.refresh_from_db()
    assert booking.istem_fbr_status == IstemFbrStatus.PENDING_OIC


@pytest.mark.django_db
def test_unknown_review_action_is_rejected():
    booking = _booking(_user(), istem_fbr_status=IstemFbrStatus.PENDING_OIC, istem_fbr_number="FBR-001")
    oic = _oic_of(booking.equipment)

    res = _review(oic, booking, action="approve")

    assert res.status_code == 400
    booking.refresh_from_db()
    assert booking.istem_fbr_status == IstemFbrStatus.PENDING_OIC


@pytest.mark.django_db
@pytest.mark.parametrize("reviewer", ["owner", "other_oic", "operator", "finance"])
def test_only_the_equipments_oic_or_an_admin_can_review(reviewer):
    owner = _user()
    booking = _booking(owner, istem_fbr_status=IstemFbrStatus.PENDING_OIC, istem_fbr_number="FBR-001")
    users = {
        "owner": owner,
        "other_oic": _oic_of(_equipment()),
        "operator": _user(UserType.OPERATOR),
        "finance": _user(UserType.FINANCE),
    }

    res = _review(users[reviewer], booking, action="execute")

    assert res.status_code == 403
    booking.refresh_from_db()
    assert booking.istem_fbr_status == IstemFbrStatus.PENDING_OIC
    assert booking.istem_fbr_executed_at is None


@pytest.mark.django_db
@pytest.mark.parametrize("fbr_status", [IstemFbrStatus.PENDING_FBR, IstemFbrStatus.INVALID, IstemFbrStatus.EXECUTED])
def test_review_needs_an_fbr_pending_verification(fbr_status):
    booking = _booking(_user(), istem_fbr_status=fbr_status)

    res = _review(_user(UserType.ADMIN), booking, action="execute")

    assert res.status_code == 400
    booking.refresh_from_db()
    assert booking.istem_fbr_status == fbr_status


@pytest.mark.django_db
def test_review_does_not_apply_outside_the_fbr_workflow():
    booking = _booking(_user(), require_istem_fbr=False)

    res = _review(_user(UserType.ADMIN), booking, action="execute")

    assert res.status_code == 400
    booking.refresh_from_db()
    assert booking.istem_fbr_status is None


# --- Result-download gate --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fbr_status", "blocked"),
    [
        (None, False),
        (IstemFbrStatus.PENDING_FBR, True),
        (IstemFbrStatus.PENDING_OIC, True),
        (IstemFbrStatus.INVALID, True),
        (IstemFbrStatus.EXECUTED, False),
    ],
)
def test_results_gate_follows_the_fbr_status_only(fbr_status, blocked):
    booking = SimpleNamespace(istem_fbr_status=fbr_status)
    assert api_views._booking_requires_istem_fbr_results_block(booking) is blocked


@pytest.mark.django_db
@pytest.mark.parametrize("fbr_status", [IstemFbrStatus.PENDING_FBR, IstemFbrStatus.PENDING_OIC, IstemFbrStatus.INVALID])
def test_owner_cannot_download_results_before_fbr_execution(media, fbr_status):
    owner = _user()
    booking = _with_results(_booking(owner, status=BookingStatus.COMPLETED, istem_fbr_status=fbr_status))
    client = _client(owner)

    link = client.post(f"/api/bookings/{booking.pk}/results/download-link/")
    listing = client.get(f"/api/bookings/{booking.pk}/results/")
    token = make_download_token(owner, booking.pk)
    streamed = APIClient().get(f"/api/bookings/{booking.pk}/results/download/?t={token}")

    for res in (link, listing, streamed):
        assert res.status_code == 403
        assert res.json()["code"] == "istem_fbr_not_executed"


@pytest.mark.django_db
def test_owner_downloads_results_once_the_oic_executes_the_fbr(media):
    owner = _user()
    booking = _with_results(
        _booking(owner, status=BookingStatus.COMPLETED, istem_fbr_status=IstemFbrStatus.PENDING_OIC, istem_fbr_number="FBR-1")
    )
    client = _client(owner)
    assert client.post(f"/api/bookings/{booking.pk}/results/download-link/").status_code == 403

    assert _review(_oic_of(booking.equipment), booking, action="execute").status_code == 200

    assert client.post(f"/api/bookings/{booking.pk}/results/download-link/").status_code == 200


@pytest.mark.django_db
def test_booking_outside_the_fbr_workflow_downloads_normally(media):
    owner = _user()
    booking = _with_results(_booking(owner, require_istem_fbr=False, status=BookingStatus.COMPLETED))

    assert _client(owner).post(f"/api/bookings/{booking.pk}/results/download-link/").status_code == 200


@pytest.mark.django_db
def test_gate_keys_on_the_booking_status_not_the_profile_flag(media):
    # Existing behaviour: a profile that requires FBR but a booking whose FBR status was never initialised is not
    # gated here (the bookings list initialises it lazily). Recorded so the integration does not change it silently.
    owner = _user()
    booking = _with_results(_booking(owner, status=BookingStatus.COMPLETED, istem_fbr_status=None))

    assert _client(owner).post(f"/api/bookings/{booking.pk}/results/download-link/").status_code == 200


@pytest.mark.django_db
def test_completion_gate_applies_before_the_fbr_gate(media):
    owner = _user()
    booking = _with_results(_booking(owner, status=BookingStatus.BOOKED, istem_fbr_status=IstemFbrStatus.PENDING_OIC))

    res = _client(owner).post(f"/api/bookings/{booking.pk}/results/download-link/")

    assert res.status_code == 400
    assert res.json().get("code") != "istem_fbr_not_executed"


@pytest.mark.django_db
def test_booking_staff_are_not_held_by_the_fbr_gate(media):
    # Existing behaviour: admins / booking managers who are not the owner bypass the completion, rating and FBR gates.
    booking = _with_results(
        _booking(_user(), status=BookingStatus.COMPLETED, istem_fbr_status=IstemFbrStatus.PENDING_OIC)
    )

    assert _client(_user(UserType.ADMIN)).post(f"/api/bookings/{booking.pk}/results/download-link/").status_code == 200


@pytest.mark.django_db
def test_shared_results_lock_reports_the_fbr_reason():
    booking = _booking(_user(), status=BookingStatus.COMPLETED, istem_fbr_status=IstemFbrStatus.PENDING_OIC)

    assert _results_lock(booking)[0] == "istem_fbr_not_executed"

    booking.istem_fbr_status = IstemFbrStatus.EXECUTED
    booking.istem_fbr_executed_at = timezone.now() - timedelta(minutes=1)
    assert _results_lock(booking) == (None, None)
