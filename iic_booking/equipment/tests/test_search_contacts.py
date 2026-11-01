"""Search results name the responsible people: Lab Operator, Officer In Charge and Accounts In Charge (Module 2)."""

from __future__ import annotations

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from iic_booking.equipment.models import EquipmentManager, EquipmentOperator
from iic_booking.equipment.tests.test_global_search import _booking, _department, _equipment, _get
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _staff(user_type, name, department=None, **extra):
    return UserFactory(user_type=user_type, name=name, phone_number="9876500011", department=department,
                       email_verified=True, admin_approved=True, **extra)


@pytest.fixture
def lab():
    dept = _department()
    other_dept = _department()
    eq = _equipment("Raman Spectrometer", dept)
    op = _staff(UserType.OPERATOR, "Op Kumar", designation="Technical Officer")
    oic = _staff(UserType.MANAGER, "Prof Oic")
    acc = _staff(UserType.FINANCE, "Acc Sharma", department=dept)
    _staff(UserType.FINANCE, "Other Dept Accounts", department=other_dept)
    gone = _staff(UserType.FINANCE, "Inactive Accounts", department=dept)
    type(gone).objects.filter(pk=gone.pk).update(is_active=False)
    EquipmentOperator.objects.create(equipment=eq, operator=op)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return eq, op, oic, acc


def _names(contacts, key):
    return [p["name"] for p in contacts[key]]


def test_equipment_hit_lists_operator_oic_and_the_departments_accounts_in_charge(lab):
    eq, op, oic, acc = lab
    student = _staff(UserType.STUDENT, "Stu")
    hit = next(e for e in _get(student, "Raman")["equipment"] if e["id"] == eq.equipment_id)
    c = hit["contacts"]
    assert _names(c, "lab_operators") == ["Op Kumar"] and _names(c, "oics") == ["Prof Oic"]
    assert _names(c, "accounts_in_charge") == ["Acc Sharma"]  # not another department's, not inactive ones
    assert c["lab_operators"][0] == {"name": "Op Kumar", "designation": "Technical Officer", "email": op.email,
                                     "phone": "9876500011"}
    assert c["oics"][0]["designation"] == "Officer In Charge"  # role when no designation is recorded


def test_anonymous_visitors_get_names_and_emails_but_no_phone_numbers(lab):
    eq, *_ = lab
    hit = next(e for e in _get(None, "Raman")["equipment"] if e["id"] == eq.equipment_id)
    assert hit["contacts"]["oics"][0]["email"] and hit["contacts"]["oics"][0]["phone"] == ""


def test_booking_hit_lists_the_contacts_of_its_equipment(lab):
    eq, *_ = lab
    student = _staff(UserType.STUDENT, "Stu Two")
    b = _booking(student, eq, "RAMAN-4242")
    hit = next(x for x in _get(student, "RAMAN-4242")["bookings"] if x["id"] == b.booking_id)
    assert _names(hit["contacts"], "oics") == ["Prof Oic"]
    assert _names(hit["contacts"], "accounts_in_charge") == ["Acc Sharma"]


def test_a_mode_without_its_own_staff_uses_its_base_equipments(lab):
    eq, *_ = lab
    mode = _equipment("Raman Spectrometer Low Temperature Mode", eq.internal_department)
    mode.parent_equipment = eq
    mode.save(update_fields=["parent_equipment"])
    from iic_booking.equipment.contacts import contacts_for_equipment

    c = contacts_for_equipment([mode], with_phone=False)[mode.equipment_id]
    assert _names(c, "oics") == ["Prof Oic"] and _names(c, "lab_operators") == ["Op Kumar"]


def test_no_staff_gives_empty_lists_not_errors():
    eq = _equipment("Lonely Viscometer", _department())
    hit = next(e for e in _get(None, "Lonely Viscometer")["equipment"] if e["id"] == eq.equipment_id)
    assert hit["contacts"] == {"lab_operators": [], "oics": [], "accounts_in_charge": []}


def test_contacts_take_a_fixed_number_of_queries_however_many_equipment(lab):
    from iic_booking.equipment.contacts import contacts_for_equipment

    eq, op, oic, _acc = lab
    many = [eq]
    for i in range(5):
        e = _equipment(f"Raman Clone {i}", _department())
        EquipmentOperator.objects.create(equipment=e, operator=op)
        EquipmentManager.objects.create(equipment=e, manager=oic)
        many.append(e)
    with CaptureQueriesContext(connection) as ctx:
        contacts_for_equipment(many, with_phone=True)
    assert len(ctx.captured_queries) == 3
