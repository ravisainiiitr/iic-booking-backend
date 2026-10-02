import uuid
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import (
    Equipment,
    EquipmentManager,
    EquipmentOperator,
    EquipmentTemporaryOIC,
    Semester,
    StudentEquipmentNomination,
    StudentEquipmentNominationStatus,
)
from iic_booking.users.models.department import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _user(user_type, **kwargs):
    return UserFactory(
        user_type=user_type,
        email=f"n{uuid.uuid4().hex[:10]}@iitr.ac.in",
        email_verified=True,
        admin_approved=True,
        **kwargs,
    )


def _client(user):
    c = APIClient()
    c.force_authenticate(user=user)
    return c


@pytest.fixture
def setup():
    tag = uuid.uuid4().hex[:4].upper()
    dept = Department.objects.create(name=f"Nom Dept {tag}", code=f"ND{tag}", department_type="internal")
    equipment = Equipment.objects.create(name=f"Nom EQ {tag}", code=f"NEQ{tag}", status="ACTIVE", internal_department=dept)
    other_equipment = Equipment.objects.create(name=f"Other EQ {tag}", code=f"OEQ{tag}", status="ACTIVE")
    oic = _user(UserType.MANAGER)
    other_oic = _user(UserType.MANAGER)
    temp_oic = _user(UserType.MANAGER)
    EquipmentManager.objects.create(equipment=equipment, manager=oic)
    EquipmentManager.objects.create(equipment=other_equipment, manager=other_oic)
    EquipmentTemporaryOIC.objects.create(
        equipment=equipment, primary_oic=oic, temporary_oic=temp_oic, resume_at=timezone.now() + timedelta(days=5)
    )
    operator = _user(UserType.OPERATOR)
    EquipmentOperator.objects.create(equipment=equipment, operator=operator, role=EquipmentOperator.Role.PRIMARY)
    faculty = _user(UserType.FACULTY, department=dept)
    semester = Semester.objects.create(
        name="2026-27 Odd",
        code=f"SEM{tag}",
        start_date=timezone.localdate(),
        end_date=timezone.localdate() + timedelta(days=120),
    )

    def nomination():
        student = _user(UserType.STUDENT, department=dept, supervisor=faculty)
        return StudentEquipmentNomination.objects.create(
            student=student, supervisor=faculty, equipment=equipment, semester=semester
        )

    return {
        "admin": _user(UserType.ADMIN),
        "oic": oic,
        "other_oic": other_oic,
        "temp_oic": temp_oic,
        "operator": operator,
        "dept_admin": _user(UserType.DEPT_ADMIN, department=dept),
        "nomination": nomination,
    }


def _approve(user, nom):
    return _client(user).post(f"/api/equipment-nominations/{nom.id}/approve/", {}, format="json")


def _reject(user, nom):
    return _client(user).post(f"/api/equipment-nominations/{nom.id}/reject/", {"remarks": "Not this term"}, format="json")


@pytest.mark.parametrize("who", ["oic", "temp_oic", "admin"])
def test_admin_and_equipment_oics_can_approve(setup, who):
    nom = setup["nomination"]()
    resp = _approve(setup[who], nom)
    assert resp.status_code == 200, resp.content
    nom.refresh_from_db()
    assert nom.status == StudentEquipmentNominationStatus.APPROVED
    assert nom.approved_by_id == setup[who].id


@pytest.mark.parametrize("who", ["oic", "temp_oic", "admin"])
def test_admin_and_equipment_oics_can_reject(setup, who):
    nom = setup["nomination"]()
    resp = _reject(setup[who], nom)
    assert resp.status_code == 200, resp.content
    nom.refresh_from_db()
    assert nom.status == StudentEquipmentNominationStatus.REJECTED


@pytest.mark.parametrize("who", ["operator", "dept_admin"])
def test_operators_and_dept_admins_are_refused_with_clear_message(setup, who):
    nom = setup["nomination"]()
    for call in (_approve, _reject):
        resp = call(setup[who], nom)
        assert resp.status_code == 403
        assert "Officer In Charge" in resp.json()["error"]
    nom.refresh_from_db()
    assert nom.status == StudentEquipmentNominationStatus.PENDING


def test_oic_of_other_equipment_is_refused(setup):
    nom = setup["nomination"]()
    for call in (_approve, _reject):
        resp = call(setup["other_oic"], nom)
        assert resp.status_code == 403
        assert "equipment you manage" in resp.json()["error"]
    nom.refresh_from_db()
    assert nom.status == StudentEquipmentNominationStatus.PENDING


def test_admin_list_scoped_consistently(setup):
    nom = setup["nomination"]()
    url = "/api/equipment-nominations/admin/"

    def ids(user):
        resp = _client(user).get(url)
        assert resp.status_code == 200, resp.content
        body = resp.json()
        rows = body.get("nominations", body.get("results", body)) if isinstance(body, dict) else body
        return {row["id"] for row in rows}

    assert nom.id in ids(setup["admin"])
    assert nom.id in ids(setup["oic"])
    assert nom.id in ids(setup["temp_oic"])
    assert nom.id not in ids(setup["other_oic"])
    assert _client(setup["operator"]).get(url).status_code == 403
    assert _client(setup["dept_admin"]).get(url).status_code == 403
