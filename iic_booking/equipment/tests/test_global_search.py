"""Header search: equipment for everyone, bookings and people limited to the viewer's role."""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile, Equipment, EquipmentManager
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _department():
    tag = uuid.uuid4().hex[:4].upper()
    return Department.objects.create(
        name=f"Search {tag}",
        code=f"SR{tag}",
        department_type=DepartmentType.INTERNAL,
        equipment_visibility_enabled=True,
    )


def _equipment(name, department):
    return Equipment.objects.create(
        name=name,
        code=f"GS{uuid.uuid4().hex[:4].upper()}",
        slot_duration_minutes=60,
        user_rating_enabled=False,
        internal_department=department,
    )


def _booking(user, equipment, vid, status=BookingStatus.BOOKED):
    profile, _ = ChargeProfile.objects.get_or_create(
        equipment=equipment, user_type=UserType.STUDENT, defaults={"primary_unit_charge": Decimal("10.00")}
    )
    return Booking.objects.create(
        user=user,
        equipment=equipment,
        charge_profile=profile,
        status=status,
        total_charge=Decimal("10.00"),
        total_time_minutes=60,
        virtual_booking_id=vid,
    )


def _get(user, q):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user=user)
    resp = client.get("/api/search/", {"q": q})
    assert resp.status_code == 200
    return resp.json()


@pytest.fixture
def world():
    dept = _department()
    xrd = _equipment("Xenon Ray Diffractometer", dept)
    sem = _equipment("Scanning Electron Microscope", dept)
    alice = UserFactory(admin_approved=True, user_type=UserType.STUDENT, name="Alice Kumar")
    bob = UserFactory(admin_approved=True, user_type=UserType.STUDENT, name="Bob Singh")
    _booking(alice, xrd, "IICXRD202600011")
    _booking(bob, sem, "IICSEM202600012", status=BookingStatus.COMPLETED)
    oic = UserFactory(admin_approved=True, user_type=UserType.MANAGER, name="Dr Oic", department=dept)
    EquipmentManager.objects.create(equipment=xrd, manager=oic)
    return {"xrd": xrd, "sem": sem, "alice": alice, "bob": bob, "oic": oic}


def test_short_query_returns_nothing(world):
    assert _get(world["alice"], "x")["equipment"] == []


def test_anonymous_gets_equipment_only(world):
    body = _get(None, "xenon")
    assert [e["name"] for e in body["equipment"]] == ["Xenon Ray Diffractometer"]
    assert body["bookings"] == [] and body["people"] == [] and body["people_enabled"] is False


def test_user_sees_only_own_bookings(world):
    body = _get(world["alice"], "IIC")
    assert [b["virtual_booking_id"] for b in body["bookings"]] == ["IICXRD202600011"]
    assert body["bookings"][0]["own"] is True and body["bookings"][0]["user_name"] == ""
    assert body["people_enabled"] is False
    assert _get(world["alice"], "Bob")["bookings"] == []


def test_status_word_matches_bookings(world):
    assert [b["virtual_booking_id"] for b in _get(world["bob"], "completed")["bookings"]] == ["IICSEM202600012"]


def test_oic_sees_bookings_on_their_equipment_and_finds_any_user(world):
    body = _get(world["oic"], "IIC")
    assert [b["virtual_booking_id"] for b in body["bookings"]] == ["IICXRD202600011"]
    assert body["bookings"][0]["user_name"] == "Alice Kumar"
    assert [p["name"] for p in _get(world["oic"], "Alice")["people"]] == ["Alice Kumar"]
    # Like the Main Admin, the OIC finds any user; bookings stay limited to their equipment.
    bob = _get(world["oic"], "Bob")
    assert [p["name"] for p in bob["people"]] == ["Bob Singh"]
    assert bob["bookings"] == []


def test_supervisor_name_finds_their_students_bookings(world):
    from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus

    prof = UserFactory(admin_approved=True, user_type=UserType.FACULTY, name="Prof Meera Iyer")
    WalletJoinRequest.objects.create(
        student=world["alice"], faculty=prof, wallet=Wallet.objects.create(user=prof),
        status=WalletJoinRequestStatus.APPROVED,
    )
    _booking(prof, world["xrd"], "IICXRD202600099")
    body = _get(world["oic"], "Meera")
    assert sorted(b["virtual_booking_id"] for b in body["bookings"]) == ["IICXRD202600011", "IICXRD202600099"]


def test_admin_finds_everyone(world):
    admin = UserFactory(admin_approved=True, user_type=UserType.ADMIN)
    body = _get(admin, "Singh")
    assert body["people_enabled"] is True
    assert [p["name"] for p in body["people"]] == ["Bob Singh"]
    assert [b["virtual_booking_id"] for b in body["bookings"]] == ["IICSEM202600012"]


# --- lab staff for users -------------------------------------------------------------------------------------------------

def test_users_find_the_officer_in_charge_and_lab_in_charge_by_name(world):
    from iic_booking.equipment.models import EquipmentOperator

    dept = _department()
    fesem = _equipment("Field Emission SEM", dept)
    oic = UserFactory(admin_approved=True, user_type=UserType.MANAGER, name="Meera Sharma")
    op = UserFactory(admin_approved=True, user_type=UserType.OPERATOR, name="Meenal Rawat")
    EquipmentManager.objects.create(equipment=fesem, manager=oic)
    EquipmentOperator.objects.create(equipment=fesem, operator=op)
    student = UserFactory(admin_approved=True, user_type=UserType.STUDENT, name="Some Student")

    staff = _get(student, "Mee")["lab_staff"]
    by_role = {row["role"]: row for row in staff}
    assert set(by_role) == {"Officer in Charge", "Lab In-charge"}
    assert by_role["Officer in Charge"]["email"] == oic.email
    assert [e["id"] for e in by_role["Lab In-charge"]["equipment"]] == [fesem.pk]
    assert _get(student, "Nobody Here")["lab_staff"] == []
    assert _get(None, "Mee")["lab_staff"] == []  # signed-out visitors do not get staff contacts


def test_equipment_hits_list_their_officer_in_charge_and_lab_in_charge(world):
    from iic_booking.equipment.models import EquipmentOperator

    dept = _department()
    fesem = _equipment("Field Emission SEM", dept)
    oic = UserFactory(admin_approved=True, user_type=UserType.MANAGER, name="Meera Sharma")
    op = UserFactory(admin_approved=True, user_type=UserType.OPERATOR, name="Meenal Rawat")
    EquipmentManager.objects.create(equipment=fesem, manager=oic)
    EquipmentOperator.objects.create(equipment=fesem, operator=op)

    for viewer in (UserFactory(admin_approved=True, user_type=UserType.STUDENT), oic):
        hit = next(e for e in _get(viewer, "Field Emission")["equipment"] if e["id"] == fesem.pk)
        assert {(s["role"], s["email"]) for s in hit["lab_staff"]} == {
            ("Officer in Charge", oic.email), ("Lab In-charge", op.email),
        }
    public = next(e for e in _get(None, "Field Emission")["equipment"] if e["id"] == fesem.pk)
    assert "lab_staff" not in public  # signed-out visitors do not get staff contacts
