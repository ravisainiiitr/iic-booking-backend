"""Department Administrators act like the OIC on urgent requests, equipment waitlists and repeat samples,
but only for equipment in their own department (others: 403) and only with the Manage bookings grant."""

from __future__ import annotations

import logging
import uuid
from datetime import time, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import (
    Booking,
    BookingEvent,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    Equipment,
    EquipmentProfileType,
    RepeatSampleRequest,
    RepeatSampleRequestStatus,
    SlotMaster,
    SlotStatus,
    UrgentBookingRequest,
    UrgentBookingRequestStatus,
    UrgentBookingRequestType,
    UrgentHoldExpiryConfig,
    WaitlistEntry,
)
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.rbac import DeptAdminPermissionGrant, PermissionDefinition
from iic_booking.users.models.user_type import UserType
from iic_booking.users.rbac import ensure_default_permission_definitions
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

AUDIT_LOGGER = "iic_booking.audit.staff_actions"


def _client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _user(**kwargs):
    return UserFactory(admin_approved=True, **kwargs)


def _department(prefix: str) -> Department:
    tag = uuid.uuid4().hex[:4].upper()
    return Department.objects.create(
        name=f"{prefix} {tag}", code=f"{prefix[:2].upper()}{tag}", department_type=DepartmentType.INTERNAL
    )


def _equipment(department) -> Equipment:
    eq = Equipment.objects.create(
        name=f"EQ {uuid.uuid4().hex[:4]}",
        code=f"DA{uuid.uuid4().hex[:5].upper()}",
        slot_duration_minutes=60,
        user_rating_enabled=False,
        profile_type=EquipmentProfileType.HOUR,
        internal_department=department,
    )
    ChargeProfile.objects.create(equipment=eq, user_type=UserType.STUDENT, primary_unit_charge=Decimal("0"))
    return eq


def _slot(equipment, start, status=SlotStatus.AVAILABLE):
    master = SlotMaster.objects.create(
        equipment=equipment,
        slot_number=SlotMaster.objects.filter(equipment=equipment).count() + 1,
        open_time=time(9),
        close_time=time(10),
        is_active=True,
    )
    return DailySlot.objects.create(
        slot_master=master,
        date=timezone.localtime(start).date(),
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        status=status,
    )


def _completed_booking(owner, equipment):
    return Booking.objects.create(
        user=owner,
        equipment=equipment,
        charge_profile=ChargeProfile.objects.get(equipment=equipment, user_type=UserType.STUDENT),
        status=BookingStatus.COMPLETED,
        completed_at=timezone.now(),
        total_charge=Decimal("10.00"),
        total_time_minutes=60,
        virtual_booking_id=f"IIC{equipment.code}{uuid.uuid4().hex[:4]}",
        user_type_snapshot=UserType.STUDENT,
    )


def _grant_manage_bookings(dept_admin):
    ensure_default_permission_definitions()
    DeptAdminPermissionGrant.objects.create(
        department_id=dept_admin.department_id,
        dept_admin=dept_admin,
        permission=PermissionDefinition.objects.get(code="bookings.manage"),
    )


@pytest.fixture
def world():
    own_dept, other_dept = _department("Chem"), _department("Phys")
    da = _user(user_type=UserType.DEPT_ADMIN, department=own_dept)
    _grant_manage_bookings(da)
    return {
        "da": da,
        "own": _equipment(own_dept),
        "other": _equipment(other_dept),
        "student": _user(user_type=UserType.STUDENT, department=own_dept),
    }


@pytest.fixture
def no_admin_panel():
    with patch("config.admin_panel_access_api.user_can_access_admin_module", return_value=False):
        yield


@pytest.fixture
def booking_ok():
    with patch(
        "iic_booking.users.legacy_ledger.booking_lock.booking_is_locked", return_value=(False, "")
    ), patch(
        "iic_booking.users.legacy_ledger.booking_lock.department_equipment_booking_blocked", return_value=(False, "")
    ), patch(
        "iic_booking.equipment.waitlist_booking.WalletRepository.get_booking_wallet_target",
        return_value=(MagicMock(), None),
    ), patch(
        "iic_booking.users.wallet_credit_facility.subwallet_booking_balance_ok", return_value=(True, "")
    ):
        yield


def _audited(caplog, action):
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == AUDIT_LOGGER and f"action={action} " in r.getMessage() and "actor_role=dept_admin" in r.getMessage()
    ]


# --- Equipment waitlist ----------------------------------------------------------------------


def test_dept_admin_clears_own_department_waitlist_only(world, no_admin_panel, caplog):
    w = world
    for eq in (w["own"], w["other"]):
        WaitlistEntry.objects.create(equipment=eq, user=w["student"])
    da = _client(w["da"])

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        res = da.post(f"/api/admin/equipment/{w['own'].pk}/waitlist-clear/", {}, format="json")
    assert res.status_code == 200, getattr(res, "data", res.content)
    assert not WaitlistEntry.objects.filter(equipment=w["own"]).exists()
    assert _audited(caplog, "waitlist.clear")

    denied = da.post(f"/api/admin/equipment/{w['other'].pk}/waitlist-clear/", {}, format="json")
    assert denied.status_code == 403
    assert WaitlistEntry.objects.filter(equipment=w["other"]).count() == 1


def test_dept_admin_without_manage_bookings_cannot_change_waitlists(world, no_admin_panel):
    w = world
    DeptAdminPermissionGrant.objects.filter(dept_admin=w["da"]).delete()
    WaitlistEntry.objects.create(equipment=w["own"], user=w["student"])
    da = _client(w["da"])
    assert da.get(f"/api/admin/equipment/{w['own'].pk}/waitlist/").status_code == 200
    assert da.post(f"/api/admin/equipment/{w['own'].pk}/waitlist-clear/", {}, format="json").status_code == 403
    assert WaitlistEntry.objects.filter(equipment=w["own"]).count() == 1


def test_dept_admin_confirms_waitlist_entry_in_own_department(world, no_admin_panel, booking_ok, caplog):
    w = world
    entry = WaitlistEntry.objects.create(equipment=w["own"], user=w["student"])
    slot = _slot(w["own"], timezone.now() + timedelta(days=2), SlotStatus.NOT_AVAILABLE)
    da = _client(w["da"])

    listing = da.get(f"/api/admin/equipment/{w['own'].pk}/waitlist-slots/", {"date": slot.date.isoformat()})
    assert listing.status_code == 200, getattr(listing, "data", listing.content)

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        res = da.post(
            f"/api/admin/equipment/{w['own'].pk}/waitlist-confirm/",
            {"entry_id": entry.id, "slot_ids": [slot.id]},
            format="json",
        )
    assert res.status_code == 201, res.data
    booking = Booking.objects.get(booking_id=res.data["booking_id"])
    assert booking.created_by_id == w["da"].pk
    assert not WaitlistEntry.objects.filter(pk=entry.pk).exists()
    assert _audited(caplog, "waitlist.confirm")
    events = list(BookingEvent.objects.filter(booking=booking, created_by=w["da"]))
    assert events and all(e.metadata.get("actor_role") == UserType.DEPT_ADMIN for e in events)


def test_dept_admin_cannot_confirm_or_list_slots_for_other_department(world, no_admin_panel, booking_ok):
    w = world
    entry = WaitlistEntry.objects.create(equipment=w["other"], user=w["student"])
    slot = _slot(w["other"], timezone.now() + timedelta(days=2))
    da = _client(w["da"])
    slots = da.get(f"/api/admin/equipment/{w['other'].pk}/waitlist-slots/", {"date": slot.date.isoformat()})
    assert slots.status_code == 403
    res = da.post(
        f"/api/admin/equipment/{w['other'].pk}/waitlist-confirm/",
        {"entry_id": entry.id, "slot_ids": [slot.id]},
        format="json",
    )
    assert res.status_code == 403
    assert WaitlistEntry.objects.filter(pk=entry.pk).exists()


# --- Urgent requests -------------------------------------------------------------------------


def _urgent(equipment, owner):
    return UrgentBookingRequest.objects.create(
        user=owner, equipment=equipment, request_type=UrgentBookingRequestType.NO_SLOT
    )


@pytest.fixture
def quiet_emails():
    with patch("iic_booking.equipment.api_views.CommunicationService.send_email"):
        yield


def test_dept_admin_approves_and_rejects_own_department_urgent_requests(world, quiet_emails, caplog):
    w = world
    approve, reject = _urgent(w["own"], w["student"]), _urgent(w["own"], w["student"])
    da = _client(w["da"])

    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        ok = da.patch(f"/api/urgent-booking-requests/{approve.id}/", {"status": "APPROVED"}, format="json")
        no = da.patch(
            f"/api/urgent-booking-requests/{reject.id}/", {"status": "REJECTED", "admin_notes": "Busy"}, format="json"
        )
    assert ok.status_code == 200, ok.data
    assert no.status_code == 200, no.data
    approve.refresh_from_db()
    reject.refresh_from_db()
    assert approve.status == UrgentBookingRequestStatus.APPROVED and approve.decided_by_id == w["da"].pk
    assert reject.status == UrgentBookingRequestStatus.REJECTED and reject.decided_by_id == w["da"].pk
    assert _audited(caplog, "urgent_request.approved") and _audited(caplog, "urgent_request.rejected")


def test_dept_admin_deletes_own_department_urgent_request(world, caplog):
    w = world
    urg = _urgent(w["own"], w["student"])
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        res = _client(w["da"]).delete(f"/api/urgent-booking-requests/{urg.id}/")
    assert res.status_code == 204
    assert not UrgentBookingRequest.objects.filter(pk=urg.pk).exists()
    assert _audited(caplog, "urgent_request.delete")


def test_dept_admin_cannot_act_on_other_department_urgent_requests(world, quiet_emails):
    w = world
    urg = _urgent(w["other"], w["student"])
    da = _client(w["da"])
    assert da.patch(f"/api/urgent-booking-requests/{urg.id}/", {"status": "APPROVED"}, format="json").status_code == 403
    assert da.patch(f"/api/urgent-booking-requests/{urg.id}/", {"status": "REJECTED"}, format="json").status_code == 403
    assert da.delete(f"/api/urgent-booking-requests/{urg.id}/").status_code == 403
    urg.refresh_from_db()
    assert urg.status == UrgentBookingRequestStatus.PENDING


def test_urgent_expiry_is_global_and_not_changed_by_dept_admin(world):
    w = world
    UrgentHoldExpiryConfig.objects.create(hold_expiry_hours=24, urgent_booking_validity_days=2)
    da = _client(w["da"])
    assert da.get("/api/urgent-booking-requests/hold-expiry-config/").data["urgent_booking_validity_days"] == 2
    res = da.patch("/api/urgent-booking-requests/hold-expiry-config/", {"urgent_booking_validity_days": 5}, format="json")
    assert res.status_code == 403
    assert res.data["code"] == "URGENT_EXPIRY_GLOBAL"
    assert UrgentHoldExpiryConfig.objects.first().urgent_booking_validity_days == 2

    admin = _client(_user(user_type=UserType.ADMIN, is_staff=True))
    assert admin.patch(
        "/api/urgent-booking-requests/hold-expiry-config/", {"urgent_booking_validity_days": 5}, format="json"
    ).status_code == 200
    assert UrgentHoldExpiryConfig.objects.first().urgent_booking_validity_days == 5


# --- Repeat samples --------------------------------------------------------------------------


def _pending_repeat(world, equipment):
    return RepeatSampleRequest.objects.create(
        booking=_completed_booking(world["student"], equipment), status=RepeatSampleRequestStatus.PENDING
    )


def test_dept_admin_approves_and_rejects_own_department_repeats(world, caplog):
    w = world
    approve, reject = _pending_repeat(w, w["own"]), _pending_repeat(w, w["own"])
    da = _client(w["da"])
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        ok = da.post(f"/api/repeat-sample-requests/{approve.id}/approve/", {"admin_notes": "Fine"}, format="json")
        no = da.post(f"/api/repeat-sample-requests/{reject.id}/reject/", {"admin_notes": "No"}, format="json")
    assert ok.status_code == 200, ok.data
    assert no.status_code == 200, no.data
    approve.refresh_from_db()
    reject.refresh_from_db()
    assert approve.status == RepeatSampleRequestStatus.APPROVED and approve.responded_by_id == w["da"].pk
    assert reject.status == RepeatSampleRequestStatus.REJECTED and reject.responded_by_id == w["da"].pk
    assert _audited(caplog, "repeat_sample.approve") and _audited(caplog, "repeat_sample.reject")
    offered = BookingEvent.objects.filter(booking=approve.booking, created_by=w["da"]).first()
    assert offered is not None and offered.metadata["actor_role_label"] == "Department Administrator"


def test_dept_admin_cannot_decide_other_department_repeats(world):
    w = world
    req = _pending_repeat(w, w["other"])
    da = _client(w["da"])
    assert da.post(f"/api/repeat-sample-requests/{req.id}/approve/", {}, format="json").status_code == 403
    assert da.post(f"/api/repeat-sample-requests/{req.id}/reject/", {}, format="json").status_code == 403
    req.refresh_from_db()
    assert req.status == RepeatSampleRequestStatus.PENDING


def test_dept_admin_enables_and_books_repeats_in_own_department_only(world, booking_ok, caplog):
    w = world
    own_booking = _completed_booking(w["student"], w["own"])
    other_booking = _completed_booking(w["student"], w["other"])
    da = _client(w["da"])

    assert da.post(f"/api/bookings/{own_booking.pk}/enable-repeat-sample/", {}, format="json").status_code == 200
    assert da.post(f"/api/bookings/{other_booking.pk}/enable-repeat-sample/", {}, format="json").status_code == 403

    slot = _slot(w["own"], timezone.now() + timedelta(days=3))
    with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
        booked = da.post(
            f"/api/bookings/{own_booking.pk}/create-repeat-booking/", {"slot_ids": [slot.id]}, format="json"
        )
    assert booked.status_code == 201, booked.data
    new_booking = Booking.objects.get(source_booking=own_booking)
    assert new_booking.created_by_id == w["da"].pk
    assert _audited(caplog, "repeat_sample.book")
    record = RepeatSampleRequest.objects.get(booking=own_booking)
    assert record.responded_by_id == w["da"].pk
    assert "Department Administrator" in record.admin_notes

    other_slot = _slot(w["other"], timezone.now() + timedelta(days=3))
    denied = da.post(
        f"/api/bookings/{other_booking.pk}/create-repeat-booking/", {"slot_ids": [other_slot.id]}, format="json"
    )
    assert denied.status_code == 403
    assert not Booking.objects.filter(source_booking=other_booking).exists()


def test_repeat_actions_need_manage_bookings_grant(world):
    w = world
    DeptAdminPermissionGrant.objects.filter(dept_admin=w["da"]).delete()
    req = _pending_repeat(w, w["own"])
    assert _client(w["da"]).post(f"/api/repeat-sample-requests/{req.id}/approve/", {}, format="json").status_code == 403
    req.refresh_from_db()
    assert req.status == RepeatSampleRequestStatus.PENDING
