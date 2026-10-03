"""Department / equipment filters on the equipment waitlist, repeat samples and urgent requests lists.

Query params only narrow what the user may see: an OIC (incl. temporary OIC) gets their own equipment
whatever department_id / equipment_id they pass; a Department Administrator gets their department.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    ChargeProfile,
    Equipment,
    EquipmentManager,
    EquipmentTemporaryOIC,
    RepeatSampleRequest,
    RepeatSampleRequestStatus,
    UrgentBookingRequest,
    UrgentBookingRequestType,
    WaitlistEntry,
)
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

WAITLIST_ALL = "/api/admin/equipment/waitlist-all/"
REPEATS = "/api/repeat-sample-requests/"
URGENT = "/api/urgent-booking-requests/"


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


def _equipment(department, **kwargs) -> Equipment:
    defaults = {
        "name": f"EQ {uuid.uuid4().hex[:4]}",
        "code": f"SL{uuid.uuid4().hex[:5].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
        "internal_department": department,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def _completed_booking(owner, equipment):
    profile, _ = ChargeProfile.objects.get_or_create(
        equipment=equipment, user_type=UserType.STUDENT, defaults={"primary_unit_charge": Decimal("10.00")}
    )
    return Booking.objects.create(
        user=owner,
        equipment=equipment,
        charge_profile=profile,
        status=BookingStatus.COMPLETED,
        completed_at=timezone.now(),
        total_charge=Decimal("10.00"),
        total_time_minutes=60,
        virtual_booking_id=f"IIC{equipment.code}{uuid.uuid4().hex[:4]}",
        user_type_snapshot=UserType.STUDENT,
    )


@pytest.fixture
def world():
    """
    IIC: ``mine`` (OIC), ``delegated`` (OIC is temporary OIC), ``other_iic`` (someone else's).
    Chemistry: ``chem``. The OIC also manages ``mine_chem`` in Chemistry.
    """
    iic, chem_dept = _department("Iic"), _department("Chem")
    oic = _user(user_type=UserType.MANAGER, department=iic)
    primary = _user(user_type=UserType.MANAGER, department=iic)
    mine = _equipment(iic, name="A mine", waitlist_queue_depth=5)
    delegated = _equipment(iic, name="B delegated")
    other_iic = _equipment(iic, name="C other")
    chem = _equipment(chem_dept, name="D chem")
    mine_chem = _equipment(chem_dept, name="E mine chem")
    EquipmentManager.objects.create(equipment=mine, manager=oic)
    EquipmentManager.objects.create(equipment=mine_chem, manager=oic)
    EquipmentManager.objects.create(equipment=delegated, manager=primary)
    EquipmentTemporaryOIC.objects.create(
        equipment=delegated, primary_oic=primary, temporary_oic=oic, resume_at=timezone.now() + timedelta(days=3)
    )
    student = _user(user_type=UserType.STUDENT, department=iic)
    every = [mine, delegated, other_iic, chem, mine_chem]
    return {
        "iic": iic,
        "chem_dept": chem_dept,
        "oic": oic,
        "student": student,
        "mine": mine,
        "delegated": delegated,
        "other_iic": other_iic,
        "chem": chem,
        "mine_chem": mine_chem,
        "every": every,
        "oic_equipment": {mine.pk, delegated.pk, mine_chem.pk},
    }


# --- Equipment waitlist ----------------------------------------------------------------------


@pytest.fixture
def waitlists(world):
    for eq in world["every"]:
        WaitlistEntry.objects.create(user=world["student"], equipment=eq)
        WaitlistEntry.objects.create(user=_user(user_type=UserType.STUDENT), equipment=eq, status="OPT_OUT")
    return world


def _equipment_ids(rows):
    return {r["equipment_id"] for r in rows}


def test_waitlist_all_is_limited_to_oic_equipment_whatever_the_params(waitlists):
    w = waitlists
    oic = _client(w["oic"])

    res = oic.get(WAITLIST_ALL)
    assert res.status_code == 200, getattr(res, "data", res.content)
    assert _equipment_ids(res.data["entries"]) == w["oic_equipment"]
    assert {o["equipment_id"] for o in res.data["filters"]["equipment_options"]} == w["oic_equipment"]
    assert res.data["filters"]["scope"] == "equipment"
    assert res.data["filters"]["department_locked"] is True
    assert res.data["active_count"] == 3 and res.data["opted_out_count"] == 3
    assert res.data["equipment"] is None
    # Positions are numbered per equipment.
    assert {r["waitlist_code"] for r in res.data["entries"] if r["status"] == "ACTIVE"} == {"WL1"}

    for params in (
        {"equipment_id": w["other_iic"].pk},
        {"equipment_id": w["chem"].pk},
        {"department_id": w["chem_dept"].pk, "equipment_id": w["chem"].pk},
    ):
        narrowed = oic.get(WAITLIST_ALL, params)
        assert narrowed.status_code == 200
        assert narrowed.data["entries"] == [], params
        assert narrowed.data["equipment"] is None

    chem_only = oic.get(WAITLIST_ALL, {"department_id": w["chem_dept"].pk})
    assert _equipment_ids(chem_only.data["entries"]) == {w["mine_chem"].pk}

    single = oic.get(WAITLIST_ALL, {"equipment_id": w["mine"].pk})
    assert _equipment_ids(single.data["entries"]) == {w["mine"].pk}
    assert single.data["equipment"]["waitlist_queue_depth"] == 5
    assert single.data["equipment"]["equipment_id"] == w["mine"].pk


def test_expired_temporary_oic_loses_waitlist_access(waitlists):
    w = waitlists
    EquipmentTemporaryOIC.objects.filter(temporary_oic=w["oic"]).update(resume_at=timezone.now() - timedelta(hours=1))
    res = _client(w["oic"]).get(WAITLIST_ALL)
    assert _equipment_ids(res.data["entries"]) == {w["mine"].pk, w["mine_chem"].pk}


def test_waitlist_all_admin_filters_by_department_and_equipment(waitlists):
    w = waitlists
    admin = _client(_user(user_type=UserType.ADMIN, is_staff=True))

    everything = admin.get(WAITLIST_ALL)
    assert everything.status_code == 200, getattr(everything, "data", everything.content)
    assert {eq.pk for eq in w["every"]} <= _equipment_ids(everything.data["entries"])
    assert everything.data["filters"]["scope"] == "all"
    assert everything.data["filters"]["department_locked"] is False

    iic = admin.get(WAITLIST_ALL, {"department_id": w["iic"].pk})
    assert _equipment_ids(iic.data["entries"]) == {w["mine"].pk, w["delegated"].pk, w["other_iic"].pk}
    assert [o["name"] for o in iic.data["filters"]["equipment_options"]] == ["A mine", "B delegated", "C other"]

    one = admin.get(WAITLIST_ALL, {"department_id": w["iic"].pk, "equipment_id": w["other_iic"].pk})
    assert _equipment_ids(one.data["entries"]) == {w["other_iic"].pk}
    assert one.data["equipment"]["equipment_id"] == w["other_iic"].pk
    assert len(one.data["filters"]["equipment_options"]) == 3


def test_waitlist_all_department_admin_sees_own_department_only(waitlists):
    w = waitlists
    da = _user(user_type=UserType.DEPT_ADMIN, department=w["iic"])
    with patch("config.admin_panel_access_api.user_can_access_admin_module", return_value=False):
        res = _client(da).get(WAITLIST_ALL)
        assert res.status_code == 200, getattr(res, "data", res.content)
        assert _equipment_ids(res.data["entries"]) == {w["mine"].pk, w["delegated"].pk, w["other_iic"].pk}
        assert res.data["filters"]["scope"] == "department"
        assert res.data["filters"]["locked_department_id"] == w["iic"].pk
        assert _client(da).get(WAITLIST_ALL, {"department_id": w["chem_dept"].pk}).data["entries"] == []
        # Read-only: clearing still needs the equipment module.
        assert _client(da).post(f"/api/admin/equipment/{w['mine'].pk}/waitlist-clear/", {}, format="json").status_code == 403
    assert WaitlistEntry.objects.filter(equipment=w["mine"]).count() == 2


def test_waitlist_all_denied_to_students(world):
    assert _client(world["student"]).get(WAITLIST_ALL).status_code == 403


def test_per_equipment_waitlist_keeps_its_shape(waitlists):
    w = waitlists
    res = _client(w["oic"]).get(f"/api/admin/equipment/{w['mine'].pk}/waitlist/")
    assert res.status_code == 200
    assert res.data["waitlist_queue_depth"] == 5
    assert res.data["count"] == 2
    assert (res.data["active_count"], res.data["cannot_fulfill_count"], res.data["opted_out_count"]) == (1, 0, 1)
    assert res.data["entries"][0]["waitlist_code"] == "WL1"
    assert res.data["entries"][0]["equipment_id"] == w["mine"].pk


# --- Repeat samples --------------------------------------------------------------------------


@pytest.fixture
def repeats(world):
    for eq in world["every"]:
        RepeatSampleRequest.objects.create(
            booking=_completed_booking(world["student"], eq), status=RepeatSampleRequestStatus.APPROVED
        )
    return world


def _repeat_equipment_codes(res):
    return {r["equipment_code"] for r in res.data["repeat_sample_requests"]}


def _codes(world, *keys):
    return {world[k].code for k in keys}


def test_repeat_samples_limited_to_oic_equipment_whatever_the_params(repeats):
    w = repeats
    oic = _client(w["oic"])
    res = oic.get(REPEATS)
    assert res.status_code == 200, res.data
    assert _repeat_equipment_codes(res) == _codes(w, "mine", "delegated", "mine_chem")
    assert {o["equipment_id"] for o in res.data["filters"]["equipment_options"]} == w["oic_equipment"]

    assert oic.get(REPEATS, {"equipment_id": w["other_iic"].pk}).data["repeat_sample_requests"] == []
    assert oic.get(REPEATS, {"department_id": w["chem_dept"].pk, "equipment_id": w["chem"].pk}).data[
        "repeat_sample_requests"
    ] == []
    assert _repeat_equipment_codes(oic.get(REPEATS, {"equipment_id": w["delegated"].pk})) == _codes(w, "delegated")


def test_repeat_samples_admin_and_department_admin_filters(repeats):
    w = repeats
    admin = _client(_user(user_type=UserType.ADMIN, is_staff=True))
    assert _repeat_equipment_codes(admin.get(REPEATS, {"department_id": w["chem_dept"].pk})) == _codes(
        w, "chem", "mine_chem"
    )
    assert _repeat_equipment_codes(admin.get(REPEATS, {"equipment_id": w["chem"].pk})) == _codes(w, "chem")

    da = _client(_user(user_type=UserType.DEPT_ADMIN, department=w["chem_dept"]))
    res = da.get(REPEATS)
    assert res.status_code == 200, res.data
    assert _repeat_equipment_codes(res) == _codes(w, "chem", "mine_chem")
    assert da.get(REPEATS, {"department_id": w["iic"].pk}).data["repeat_sample_requests"] == []

    operator = _user(user_type=UserType.OPERATOR, department=w["iic"])
    assert _client(operator).get(REPEATS).status_code == 403


# --- Urgent requests -------------------------------------------------------------------------


@pytest.fixture
def urgents(world):
    requester = _user(user_type=UserType.FACULTY, department=world["iic"])
    for eq in world["every"]:
        UrgentBookingRequest.objects.create(
            user=requester, equipment=eq, request_type=UrgentBookingRequestType.REVIEWER_URGENT
        )
        UrgentBookingRequest.objects.create(user=requester, equipment=eq, request_type=UrgentBookingRequestType.NO_SLOT)
    return world


def _urgent_equipment_ids(res):
    return {r["equipment_id"] for r in res.data["urgent_requests"]}


def test_urgent_requests_limited_to_oic_equipment_whatever_the_params(urgents):
    w = urgents
    oic = _client(w["oic"])
    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        res = oic.get(URGENT)
        outside = oic.get(URGENT, {"equipment_id": w["other_iic"].pk})
        other_dept = oic.get(URGENT, {"department_id": w["chem_dept"].pk, "equipment_id": w["chem"].pk})
        delegated = oic.get(URGENT, {"equipment_id": w["delegated"].pk, "request_type": "REVIEWER_URGENT"})
    assert res.status_code == 200, getattr(res, "data", res.content)
    assert _urgent_equipment_ids(res) == w["oic_equipment"]
    assert res.data["total_count"] == 6
    assert {o["equipment_id"] for o in res.data["filters"]["equipment_options"]} == w["oic_equipment"]
    assert outside.data["urgent_requests"] == [] and outside.data["total_count"] == 0
    assert other_dept.data["total_count"] == 0
    assert _urgent_equipment_ids(delegated) == {w["delegated"].pk}
    assert delegated.data["total_count"] == 1


def test_urgent_requests_admin_lists_all_types_with_department_and_equipment_filters(urgents):
    w = urgents
    admin = _client(_user(user_type=UserType.ADMIN, is_staff=True))
    iic = admin.get(URGENT, {"department_id": w["iic"].pk})
    assert iic.status_code == 200, getattr(iic, "data", iic.content)
    assert _urgent_equipment_ids(iic) == {w["mine"].pk, w["delegated"].pk, w["other_iic"].pk}
    assert {r["request_type"] for r in iic.data["urgent_requests"]} == {"REVIEWER_URGENT", "NO_SLOT"}
    assert iic.data["total_count"] == 6
    assert iic.data["filters"]["scope"] == "all"

    one = admin.get(URGENT, {"department_id": w["iic"].pk, "equipment_id": w["other_iic"].pk})
    assert _urgent_equipment_ids(one) == {w["other_iic"].pk}
    assert one.data["total_count"] == 2


def test_urgent_requests_department_admin_sees_own_department_only(urgents):
    w = urgents
    da = _client(_user(user_type=UserType.DEPT_ADMIN, department=w["iic"]))
    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        res = da.get(URGENT)
        other = da.get(URGENT, {"department_id": w["chem_dept"].pk})
    assert res.status_code == 200, getattr(res, "data", res.content)
    assert _urgent_equipment_ids(res) == {w["mine"].pk, w["delegated"].pk, w["other_iic"].pk}
    assert res.data["filters"]["scope"] == "department"
    assert other.data["total_count"] == 0
