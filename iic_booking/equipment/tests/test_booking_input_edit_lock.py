"""After booking, inputs whose field has 'Editing required' unchecked can only be changed by the equipment's OIC
(incl. an active temporary OIC), a Department Administrator of the equipment's department or the Main
Administrator. 'Editing required' fields keep the usual rules."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment.input_edit_lock import LOCKED_INPUT_MESSAGE
from iic_booking.equipment.models import (
    DynamicInputField,
    DynamicInputFieldType,
    EquipmentManager,
    EquipmentOperator,
    EquipmentTemporaryOIC,
)
from iic_booking.equipment.serializers import BookingSerializer
from iic_booking.users.models import Department
from iic_booking.users.models.rbac import DeptAdminPermissionGrant, PermissionDefinition
from iic_booking.users.models.user_type import UserType
from iic_booking.users.rbac import ensure_default_permission_definitions
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _user(user_type, department):
    return UserFactory(user_type=user_type, department=department, admin_approved=True)


def _dept_admin(department):
    user = _user(UserType.DEPT_ADMIN, department)
    ensure_default_permission_definitions()
    DeptAdminPermissionGrant.objects.create(
        department_id=department.pk,
        dept_admin=user,
        permission=PermissionDefinition.objects.get(code="bookings.manage"),
    )
    return user


def _setup(egs_factory, *, a_editable, recalc_flag=False, slot_count=1):
    # HOUR profile, ₹10/hour, A hours: A=2 costs ₹20, A=3 costs ₹30. D is free text.
    eq = egs_factory.equipment(time_formula="A*60", enable_charge_recalculation=recalc_flag)
    DynamicInputField.objects.create(
        equipment=eq,
        field_key="A",
        field_label="No. of Samples",
        field_type=DynamicInputFieldType.NUMERIC,
        options={"min": 1, "max": 10},
        editing_required=a_editable,
    )
    DynamicInputField.objects.create(
        equipment=eq,
        field_key="D",
        field_label="Sample description",
        field_type=DynamicInputFieldType.TEXT,
        editing_required=True,
    )
    owner = egs_factory.student()
    booking = egs_factory.booking(
        owner, eq, egs_factory.future(), input_values={"A": 2, "D": "Powder"}, total_charge="20.00", slot_count=slot_count
    )
    booking.total_time_minutes = max(120, 60 * slot_count)
    booking.save(update_fields=["total_time_minutes"])
    oic = _user(UserType.MANAGER, egs_factory.department)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return eq, owner, oic, booking


def _other_department(tag):
    return Department.objects.create(
        name=f"Lock-Other-{tag}",
        code=f"LO{tag}",
        equipment_booking_enabled=True,
        equipment_visibility_enabled=True,
    )


def _role(name, egs_factory, eq, owner, oic):
    department = egs_factory.department
    if name == "owner":
        return owner
    if name == "supervisor":
        supervisor = _user(UserType.FACULTY, department)
        owner.supervisor = supervisor
        owner.save(update_fields=["supervisor"])
        return supervisor
    if name == "lab_operator":
        operator = _user(UserType.OPERATOR, department)
        EquipmentOperator.objects.create(equipment=eq, operator=operator, role=EquipmentOperator.Role.PRIMARY)
        return operator
    if name == "finance":
        return _user(UserType.FINANCE, department)
    if name == "other_oic":
        other = _user(UserType.MANAGER, department)
        EquipmentManager.objects.create(equipment=egs_factory.equipment(), manager=other)
        return other
    if name == "other_dept_admin":
        return _dept_admin(_other_department("X1"))
    if name == "dept_admin":
        return _dept_admin(department)
    if name == "oic":
        return oic
    if name == "temp_oic":
        temp = _user(UserType.MANAGER, department)
        EquipmentTemporaryOIC.objects.create(
            equipment=eq, primary_oic=oic, temporary_oic=temp, resume_at=timezone.now() + timedelta(days=3)
        )
        return temp
    if name == "expired_temp_oic":
        temp = _user(UserType.MANAGER, department)
        row = EquipmentTemporaryOIC.objects.create(
            equipment=eq, primary_oic=oic, temporary_oic=temp, resume_at=timezone.now() + timedelta(days=3)
        )
        EquipmentTemporaryOIC.objects.filter(pk=row.pk).update(resume_at=timezone.now() - timedelta(hours=1))
        return temp
    if name == "main_admin":
        return _user(UserType.ADMIN, department)
    raise AssertionError(name)


def _patch(egs_factory, user, booking, values):
    return egs_factory.client_for(user).patch(
        f"/api/bookings/{booking.pk}/input-values/", {"input_values": values}, format="json"
    )


PRIVILEGED = ["oic", "temp_oic", "dept_admin", "main_admin"]
REFUSED = ["owner", "supervisor", "lab_operator", "finance", "other_oic", "other_dept_admin", "expired_temp_oic"]


@pytest.mark.parametrize("role", PRIVILEGED)
def test_privileged_roles_may_change_a_field_without_editing_required(egs_factory, role):
    eq, owner, oic, booking = _setup(egs_factory, a_editable=False)
    user = _role(role, egs_factory, eq, owner, oic)

    resp = _patch(egs_factory, user, booking, {"A": 3, "D": "Powder"})

    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.input_values["A"] == 3


@pytest.mark.parametrize("role", REFUSED)
def test_other_roles_are_refused_a_field_without_editing_required(egs_factory, role):
    eq, owner, oic, booking = _setup(egs_factory, a_editable=False)
    user = _role(role, egs_factory, eq, owner, oic)

    resp = _patch(egs_factory, user, booking, {"A": 3, "D": "Powder"})

    assert resp.status_code == 403, resp.data
    booking.refresh_from_db()
    assert booking.input_values["A"] == 2


@pytest.mark.parametrize("role", ["owner", "lab_operator", "other_oic", "other_dept_admin", "expired_temp_oic"])
def test_refusal_names_the_locked_field(egs_factory, role):
    eq, owner, oic, booking = _setup(egs_factory, a_editable=False)
    user = _role(role, egs_factory, eq, owner, oic)

    resp = _patch(egs_factory, user, booking, {"A": 3})

    assert resp.status_code == 403
    assert resp.data["code"] == "input_locked"
    assert resp.data["locked_fields"] == ["A"]
    assert resp.data["error"] == f"{LOCKED_INPUT_MESSAGE}: No. of Samples."


@pytest.mark.parametrize("role", ["owner", "lab_operator", "other_oic", "other_dept_admin", "expired_temp_oic"])
def test_editing_required_fields_stay_editable_and_unchanged_locked_values_are_accepted(egs_factory, role):
    eq, owner, oic, booking = _setup(egs_factory, a_editable=False)
    user = _role(role, egs_factory, eq, owner, oic)

    # The full stored object is sent back, as the edit dialog does: only D changes.
    resp = _patch(egs_factory, user, booking, {"A": "2", "D": "Thin film", "comments": "Handle with care"})

    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.input_values["D"] == "Thin film"
    assert booking.input_values["A"] == 2


@pytest.mark.parametrize("role", ["owner", "lab_operator", "oic", "dept_admin", "main_admin"])
def test_editing_required_field_keeps_current_behaviour(egs_factory, role):
    eq, owner, oic, booking = _setup(egs_factory, a_editable=True)
    user = _role(role, egs_factory, eq, owner, oic)

    resp = _patch(egs_factory, user, booking, {"A": 3})

    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.input_values["A"] == 3


def test_charge_recalculation_no_longer_lets_the_user_change_locked_fields(egs_factory):
    eq, owner, oic, booking = _setup(egs_factory, a_editable=False, recalc_flag=True)

    resp = _patch(egs_factory, owner, booking, {"A": 3})
    assert resp.status_code == 403
    booking.refresh_from_db()
    assert booking.input_values["A"] == 2
    assert booking.total_charge == Decimal("20.00")

    resp = _patch(egs_factory, oic, booking, {"A": 3})
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.input_values["A"] == 3
    assert booking.total_charge == Decimal("30.00")
    assert Decimal(resp.data["charge_recalculation_summary"]["new_charge"]) == Decimal("30")


def test_charge_recalculation_still_applies_to_editing_required_fields(egs_factory):
    eq, owner, oic, booking = _setup(egs_factory, a_editable=True, recalc_flag=True)

    resp = _patch(egs_factory, owner, booking, {"A": 3})

    assert resp.status_code == 200, resp.data
    assert Decimal(resp.data["charge_recalculation_summary"]["new_charge"]) == Decimal("30")


def test_sample_set_values_follow_the_same_rule(egs_factory):
    eq, owner, oic, booking = _setup(egs_factory, a_editable=False, slot_count=4)
    booking.input_values = {"A": 2, "D": "Powder", "_sample_sets": [{"A": 1, "D": "Film"}]}
    booking.save(update_fields=["input_values"])

    resp = _patch(egs_factory, owner, booking, {"A": 2, "_sample_sets": [{"A": 2, "D": "Film"}]})
    assert resp.status_code == 403, resp.data
    assert resp.data["locked_fields"] == ["A"]
    booking.refresh_from_db()
    assert booking.input_values["_sample_sets"][0]["A"] == 1

    resp = _patch(egs_factory, owner, booking, {"A": 2, "_sample_sets": [{"A": 1, "D": "Flakes"}]})
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.input_values["_sample_sets"][0]["D"] == "Flakes"

    dept_admin = _role("dept_admin", egs_factory, eq, owner, oic)
    resp = _patch(egs_factory, dept_admin, booking, {"_sample_sets": [{"A": 2, "D": "Flakes"}]})
    assert resp.status_code == 200, resp.data
    booking.refresh_from_db()
    assert booking.input_values["_sample_sets"][0]["A"] == 2


def test_clearing_a_locked_value_in_a_sample_set_is_refused(egs_factory):
    eq, owner, oic, booking = _setup(egs_factory, a_editable=False, slot_count=4)
    booking.input_values = {"A": 2, "D": "Powder", "_sample_sets": [{"A": 1, "D": "Film"}]}
    booking.save(update_fields=["input_values"])

    resp = _patch(egs_factory, owner, booking, {"_sample_sets": [{"D": "Film"}]})

    assert resp.status_code in (400, 403), resp.data
    booking.refresh_from_db()
    assert booking.input_values["_sample_sets"][0]["A"] == 1


def test_serializer_tells_the_viewer_whether_locked_fields_are_editable(egs_factory, rf):
    eq, owner, oic, booking = _setup(egs_factory, a_editable=False)

    def viewer_flag(user):
        request = rf.get("/")
        request.user = user
        return BookingSerializer(booking, context={"request": request}).data["viewer_can_edit_locked_inputs"]

    assert viewer_flag(owner) is False
    assert viewer_flag(oic) is True
    assert viewer_flag(_role("temp_oic", egs_factory, eq, owner, oic)) is True
    assert viewer_flag(_role("expired_temp_oic", egs_factory, eq, owner, oic)) is False
    assert viewer_flag(_role("dept_admin", egs_factory, eq, owner, oic)) is True
    assert viewer_flag(_role("other_dept_admin", egs_factory, eq, owner, oic)) is False
    assert viewer_flag(_role("lab_operator", egs_factory, eq, owner, oic)) is False
    assert viewer_flag(_role("main_admin", egs_factory, eq, owner, oic)) is True
    assert BookingSerializer(booking).data["viewer_can_edit_locked_inputs"] is None
