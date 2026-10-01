"""OICs view and calculate charges for every catalog equipment; slot and booking management stay on assigned equipment."""

from __future__ import annotations

import uuid
from datetime import time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    Equipment,
    EquipmentManager,
    EquipmentProfileType,
    SlotMaster,
    SlotStatus,
    UrgentBookingRequest,
    UrgentBookingRequestStatus,
    UrgentBookingRequestType,
    WaitlistEntry,
)
from iic_booking.users.models import Department
from iic_booking.users.models.user_group import UserGroup
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _department(*, visible: bool = True) -> Department:
    tag = uuid.uuid4().hex[:5].upper()
    return Department.objects.create(
        name=f"Charges Dept {tag}",
        code=f"CH{tag}",
        equipment_visibility_enabled=visible,
        equipment_booking_enabled=True,
    )


def _equipment(dept, **kwargs) -> Equipment:
    defaults = {
        "name": f"EQ {uuid.uuid4().hex[:4]}",
        "code": f"CH{uuid.uuid4().hex[:5].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
        "status": "ACTIVE",
        "internal_department": dept,
        "profile_type": EquipmentProfileType.SAMPLE,
    }
    defaults.update(kwargs)
    eq = Equipment.objects.create(**defaults)
    for ut in (UserType.STUDENT, UserType.EXTERNAL):
        ChargeProfile.objects.create(equipment=eq, user_type=ut, primary_unit_charge=Decimal("100.00"))
    return eq


def _slot(equipment, start, status=SlotStatus.AVAILABLE) -> DailySlot:
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


def _booking(owner, equipment, status=BookingStatus.BOOKED) -> Booking:
    return Booking.objects.create(
        user=owner,
        equipment=equipment,
        charge_profile=ChargeProfile.objects.get(equipment=equipment, user_type=UserType.STUDENT),
        status=status,
        total_charge=Decimal("100.00"),
        total_time_minutes=60,
        input_values={"A": 1},
        virtual_booking_id=f"IIC{equipment.code}{uuid.uuid4().hex[:4]}",
        user_type_snapshot=UserType.STUDENT,
    )


@pytest.fixture
def world():
    dept = _department()
    oic = UserFactory(admin_approved=True, user_type=UserType.MANAGER)
    mine = _equipment(dept)
    EquipmentManager.objects.create(equipment=mine, manager=oic)
    other = _equipment(dept)
    student = UserFactory(admin_approved=True, user_type=UserType.STUDENT, department=dept)
    return SimpleNamespace(dept=dept, oic=oic, mine=mine, other=other, student=student)


# --- Read-only charges: all catalog equipment ----------------------------------------------------


def test_oic_analysis_charges_lists_unassigned_equipment(world):
    res = _client(world.oic).get("/api/equipments/analysis-charges/")
    assert res.status_code == 200, res.data
    rows = {e["equipment_id"]: e for e in res.data["equipments"]}
    assert {world.mine.pk, world.other.pk} <= set(rows)
    assert UserType.STUDENT in {cp["user_type"] for cp in rows[world.other.pk]["charge_profiles"]}
    assert world.dept.pk in {d["id"] for d in res.data["departments"]}

    only_other = _client(world.oic).get(
        "/api/equipments/analysis-charges/", {"equipment_ids": str(world.other.pk)}
    )
    assert [e["equipment_id"] for e in only_other.data["equipments"]] == [world.other.pk]


def test_oic_analysis_charges_keeps_catalog_visibility_rules(world):
    suffix = uuid.uuid4().hex[:6]
    group = UserGroup.objects.create(name=f"Private {suffix}", code=f"PRV{suffix}")
    private = _equipment(world.dept, visibility_group=group)
    hidden = _equipment(_department(visible=False))

    listed = {
        e["equipment_id"]
        for e in _client(world.oic).get("/api/equipments/analysis-charges/").data["equipments"]
    }
    assert world.other.pk in listed
    assert private.pk not in listed
    assert hidden.pk not in listed


def test_lab_incharge_analysis_charges_still_limited_to_mapped_equipment(world):
    operator = UserFactory(admin_approved=True, user_type=UserType.OPERATOR)
    listed = _client(operator).get("/api/equipments/analysis-charges/").data["equipments"]
    assert world.other.pk not in {e["equipment_id"] for e in listed}


def test_oic_views_and_calculates_charges_for_unassigned_equipment(world):
    detail = _client(world.oic).get(f"/api/equipments/{world.other.pk}/")
    assert detail.status_code == 200, detail.data
    assert detail.data["viewer_catalog_only"] is True
    assert UserType.STUDENT in {str(cp["user_type"]) for cp in detail.data["charge_profiles"]}

    calc = _client(world.oic).get(
        f"/api/equipments/{world.other.pk}/calculate/", {"user_type": UserType.STUDENT, "A": "2"}
    )
    assert calc.status_code == 200, calc.data

    listed = _client(world.oic).get("/api/equipments/", {"catalog_scope": "all"}).data["equipments"]
    assert world.other.pk in {e["equipment_id"] for e in listed}
    managed = _client(world.oic).get("/api/equipments/").data["equipments"]
    assert {e["equipment_id"] for e in managed} == {world.mine.pk}


# --- Slot management: assigned equipment only ----------------------------------------------------


def test_oic_cannot_manage_slots_or_config_of_unassigned_equipment(world):
    start = timezone.now() + timedelta(days=3)
    other_slot = _slot(world.other, start)
    entry = WaitlistEntry.objects.create(equipment=world.other, user=world.student)
    client = _client(world.oic)

    with patch("iic_booking.users.rbac.user_has_admin_panel_access", return_value=False), patch(
        "config.admin_panel_access_api.user_can_access_admin_module", return_value=False
    ):
        assert client.post(
            f"/api/admin/equipment/{world.other.pk}/bulk-slot-status/",
            {"slot_ids": [other_slot.id], "status": SlotStatus.UNDER_MAINTENANCE},
            format="json",
        ).status_code == 404
        assert client.post(
            f"/api/admin/equipment/{world.other.pk}/bulk-home-department-only/",
            {"slot_ids": [other_slot.id], "home_department_only": True},
            format="json",
        ).status_code == 404
        assert client.get(
            f"/api/admin/equipment/{world.other.pk}/waitlist-slots/",
            {"date": other_slot.date.isoformat(), "entry_id": entry.id},
        ).status_code == 404
        assert client.post(
            f"/api/admin/equipment/{world.other.pk}/waitlist-confirm/",
            {"entry_id": entry.id, "slot_ids": [other_slot.id]},
            format="json",
        ).status_code == 404
        assert client.patch(
            f"/api/admin/equipment/{world.other.pk}/", {"name": "Renamed"}, format="json"
        ).status_code in (403, 404)

    assert client.patch(f"/api/equipments/{world.other.pk}/", {"status": "REPAIR"}, format="json").status_code == 403
    other_slot.refresh_from_db()
    world.other.refresh_from_db()
    assert other_slot.status == SlotStatus.AVAILABLE
    assert world.other.status == "ACTIVE"
    assert world.other.name != "Renamed"
    assert WaitlistEntry.objects.filter(pk=entry.pk).exists()


# --- Booking management for users: assigned equipment only ---------------------------------------


def test_oic_cannot_book_or_price_for_a_user_on_unassigned_equipment(world):
    client = _client(world.oic)
    start = timezone.now() + timedelta(days=3)
    _slot(world.other, start)

    on_behalf = client.post(
        f"/api/equipments/{world.other.pk}/book/",
        {
            "user_id": world.student.pk,
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(hours=1)).isoformat(),
            "input_values": {"A": "1"},
        },
        format="json",
    )
    assert on_behalf.status_code == 403, on_behalf.data
    assert not Booking.objects.filter(equipment=world.other).exists()

    info = client.get(f"/api/equipments/{world.other.pk}/book-for-user-info/", {"user_id": world.student.pk})
    assert info.status_code == 403

    for_user = client.get(f"/api/equipments/{world.other.pk}/calculate/", {"user_id": world.student.pk, "A": "1"})
    assert for_user.status_code == 403

    own = client.get(f"/api/equipments/{world.mine.pk}/calculate/", {"user_id": world.student.pk, "A": "1"})
    assert own.status_code == 200, own.data


def test_oic_cannot_change_bookings_on_unassigned_equipment(world):
    booking = _booking(world.student, world.other)
    start = timezone.now() + timedelta(days=5)
    new_slot = _slot(world.other, start)
    client = _client(world.oic)

    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        responses = {
            "cancel": client.post(f"/api/bookings/{booking.pk}/cancel/", {"refund": True}, format="json"),
            "refund": client.post(f"/api/bookings/{booking.pk}/refund/", {}, format="json"),
            "reschedule": client.post(
                f"/api/bookings/{booking.pk}/reschedule/",
                {
                    "start_time": new_slot.start_datetime.isoformat(),
                    "end_time": new_slot.end_datetime.isoformat(),
                },
                format="json",
            ),
            "complete": client.post(f"/api/bookings/{booking.pk}/complete/", {}, format="json"),
            "not_utilized": client.post(f"/api/bookings/{booking.pk}/mark-not-utilized/", {}, format="json"),
        }
    for action, res in responses.items():
        assert res.status_code == 403, (action, res.status_code, getattr(res, "data", None))
    booking.refresh_from_db()
    assert booking.status == BookingStatus.BOOKED


def test_oic_cannot_decide_urgent_requests_for_unassigned_equipment(world):
    urgent = UrgentBookingRequest.objects.create(
        user=world.student, equipment=world.other, request_type=UrgentBookingRequestType.NO_SLOT
    )
    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        res = _client(world.oic).patch(
            f"/api/urgent-booking-requests/{urgent.pk}/",
            {"status": UrgentBookingRequestStatus.APPROVED},
            format="json",
        )
    assert res.status_code == 403
    urgent.refresh_from_db()
    assert urgent.status != UrgentBookingRequestStatus.APPROVED


def test_oic_view_booking_list_shows_only_assigned_equipment(world):
    mine_booking = _booking(world.student, world.mine)
    other_booking = _booking(world.student, world.other)
    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        res = _client(world.oic).get("/api/bookings/")
    assert res.status_code == 200, res.data
    ids = {row["real_booking_id"] for row in res.data["bookings"]}
    assert mine_booking.pk in ids
    assert other_booking.pk not in ids
