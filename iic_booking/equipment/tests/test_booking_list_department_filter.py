"""View Booking department filter: equipment_department_id, and the departments the Main Administrator can choose."""

import pytest

from iic_booking.facility_groups.tests.conftest import client_for, make_booking, make_department, make_equipment, make_user
from iic_booking.users.models.user_type import UserType

pytestmark = pytest.mark.django_db

LIST = "/api/bookings/"
DEPARTMENTS = "/api/equipments/catalog-departments/"


def _ids(res):
    return {b["virtual_booking_id"] for b in res.data["bookings"]}


def _visible_department(name):
    department = make_department(name)
    department.equipment_visibility_enabled = True
    department.save()
    return department


@pytest.fixture
def world():
    physics = _visible_department("Physics")
    chemistry = _visible_department("Chemistry")
    student = make_user(user_type=UserType.STUDENT)
    return {
        "physics": physics,
        "chemistry": chemistry,
        "in_physics": make_booking(student, make_equipment(physics)),
        "in_chemistry": make_booking(student, make_equipment(chemistry)),
        # The department of the person booking does not count, only the equipment's.
        "chemist_in_physics": make_booking(
            make_user(user_type=UserType.STUDENT, department=chemistry), make_equipment(physics)
        ),
    }


def test_admin_filters_bookings_by_the_equipment_department(world):
    res = client_for(make_user(user_type=UserType.ADMIN)).get(
        LIST, {"equipment_department_id": world["physics"].pk, "list_view": "true"}
    )
    assert res.status_code == 200
    assert _ids(res) == {world["in_physics"].virtual_booking_id, world["chemist_in_physics"].virtual_booking_id}


def test_filter_stays_inside_the_role_scope(world):
    dept_admin = make_user(user_type=UserType.DEPT_ADMIN, department=world["physics"])
    res = client_for(dept_admin).get(LIST, {"equipment_department_id": world["chemistry"].pk, "list_view": "true"})
    assert res.status_code == 200
    assert _ids(res) == set()


def test_invalid_department_is_refused(world):
    res = client_for(make_user(user_type=UserType.ADMIN)).get(LIST, {"equipment_department_id": "abc"})
    assert res.status_code == 400


def test_available_departments_leave_out_what_users_cannot_find(world):
    hidden = make_department("Hidden catalog")
    hidden.equipment_visibility_enabled = False
    hidden.save()
    make_equipment(hidden)
    make_equipment(_visible_department("Test only"), visible_to_test_accounts_only=True)
    make_equipment(_visible_department("Disposed"), status="DISPOSED")
    _visible_department("No equipment")

    admin = client_for(make_user(user_type=UserType.ADMIN))

    available = {d["name"] for d in admin.get(DEPARTMENTS, {"available": "1"}).data["departments"]}
    assert {"Physics", "Chemistry"} <= available
    assert available.isdisjoint({"Hidden catalog", "Test only", "Disposed", "No equipment"})

    # Without the flag the administrator's own wider view is unchanged.
    everything = {d["name"] for d in admin.get(DEPARTMENTS).data["departments"]}
    assert {"Hidden catalog", "Test only"} <= everything
