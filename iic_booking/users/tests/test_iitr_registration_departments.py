"""IIT Roorkee departments and centres offered at registration to IITR Post-docs, Research Associates and IITR Startup."""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.users.models import Department, DepartmentType, User
from iic_booking.users.models.department import ExternalDepartmentSubcategory, InternalDepartmentSubcategory
from iic_booking.users.models.user_type import UserType
from iic_booking.users.repositories import DepartmentRepository
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

LIST_URL = "/api/departments/"
REGISTER_URL = "/api/auth/register/"
IITR_QUERY = {"type": "internal", "internal_subcategory": "iit_roorkee_dept_centres"}


@pytest.fixture
def depts():
    internal = DepartmentType.INTERNAL
    return {
        "chemistry": Department.objects.create(name="Chemistry Department", code="CY", department_type=internal),
        "nano": Department.objects.create(name="Centre for Nanotechnology", code="NT", department_type=internal),
        "iic": Department.objects.create(
            name="Institute Instrumentation Centre",
            code="IIC",
            department_type=internal,
            internal_subcategory=InternalDepartmentSubcategory.IIT_ROORKEE_DEPT_CENTRES,
        ),
        "no_code": Department.objects.create(name="Architecture and Planning Department", department_type=internal),
        "admin": Department.objects.create(name="ADMIN", code="ADMIN", department_type=internal),
        "startup": Department.objects.create(
            name="Acme Robotics Pvt Ltd",
            code="ACMEROBO",
            department_type=internal,
            internal_subcategory=InternalDepartmentSubcategory.STARTUPS,
        ),
        "external": Department.objects.create(
            name="Delhi University",
            code="DU",
            department_type=DepartmentType.EXTERNAL,
            external_subcategory=ExternalDepartmentSubcategory.EDUCATIONAL_INSTITUTE,
            state="delhi",
        ),
    }


IITR_KEYS = ["no_code", "nano", "chemistry", "iic"]


def test_public_iitr_list_includes_untagged_internal_departments_sorted(depts):
    res = APIClient().get(LIST_URL, IITR_QUERY)
    assert res.status_code == 200
    names = [d["name"] for d in res.data["departments"]]
    assert names == [depts[k].name for k in IITR_KEYS]
    assert names == sorted(names)
    assert res.data["count"] == len(IITR_KEYS)
    assert {d["internal_subcategory"] for d in res.data["departments"]} == {None, "iit_roorkee_dept_centres"}


def test_public_iitr_list_is_not_paginated(depts):
    for i in range(60):
        Department.objects.create(name=f"Centre {i:02d}", code=f"C{i:02d}", department_type=DepartmentType.INTERNAL)
    res = APIClient().get(LIST_URL, IITR_QUERY)
    assert res.status_code == 200
    assert res.data["count"] == len(res.data["departments"]) == 60 + len(IITR_KEYS)


def test_startups_subcategory_still_filters_exactly(depts):
    res = APIClient().get(LIST_URL, {"type": "internal", "internal_subcategory": "startups"})
    assert [d["name"] for d in res.data["departments"]] == [depts["startup"].name]


def test_repository_matches_endpoint(depts):
    assert list(DepartmentRepository.get_iitr_departments_and_centres()) == [depts[k] for k in IITR_KEYS]


@pytest.fixture
def faculty(depts):
    return UserFactory(
        email="prof.gupta@iitr.ac.in",
        name="Anil Gupta",
        user_type=UserType.FACULTY,
        admin_approved=True,
        department=depts["chemistry"],
    )


IITR_TYPES = [
    pytest.param({"user_type": UserType.STUDENT, "user_type_alias": "IITR Post Doctoral Fellows"}, id="postdoc"),
    pytest.param({"user_type": UserType.STUDENT, "user_type_alias": "IITR Research Associates in Projects"}, id="ra"),
    pytest.param({"user_type": UserType.STARTUP_INCUBATED_IITR}, id="iitr-startup"),
]


def _payload(faculty, department, user_type):
    today = timezone.localdate()
    return {
        "email": f"new.{uuid.uuid4().hex[:8]}@gmail.com",
        "password": "Str0ng-pass!",
        "password_confirm": "Str0ng-pass!",
        "name": "Neha Rao",
        "gender": "female",
        "phone_number": "9876543210",
        "program_end_date": (today + timedelta(days=200)).isoformat(),
        "supervisor": faculty.pk,
        "department": department.pk,
        **user_type,
    }


@pytest.mark.parametrize("user_type", IITR_TYPES)
def test_iitr_types_accept_every_listed_department(depts, faculty, user_type):
    listed = APIClient().get(LIST_URL, IITR_QUERY).data["departments"]
    assert listed
    for row in listed:
        dept = Department.objects.get(pk=row["id"])
        payload = _payload(faculty, dept, user_type)
        res = APIClient().post(REGISTER_URL, payload)
        assert res.status_code == 201, (dept.name, res.data)
        assert User.objects.get(email=payload["email"]).department == dept


@pytest.mark.parametrize("user_type", IITR_TYPES)
@pytest.mark.parametrize("key", ["external", "admin", "startup"])
def test_iitr_types_reject_departments_outside_the_list(depts, faculty, user_type, key):
    payload = _payload(faculty, depts[key], user_type)
    res = APIClient().post(REGISTER_URL, payload)
    assert res.status_code == 400
    assert res.data["fieldErrors"]["department"]
    assert not User.objects.filter(email=payload["email"]).exists()
