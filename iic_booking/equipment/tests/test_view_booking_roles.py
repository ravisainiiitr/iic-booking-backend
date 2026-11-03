"""View Booking (My Bookings) lists bookings for every role: the list call the page makes must answer 200 with rows."""

from __future__ import annotations

import pytest

from iic_booking.research_copilot.tests.test_copilot_intelligence import _client, _World
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

PAGE = {"list_view": "true", "limit": 50, "offset": 0, "ordering": "default"}


def _user(user_type, **kw):
    return UserFactory(user_type=user_type, is_active=True, admin_approved=True, **kw)


@pytest.fixture
def lab(db):
    from iic_booking.equipment.models import EquipmentManager, EquipmentOperator

    w = _World()
    w.eq = w.equipment("Field Emission SEM", "FESEM-VB")
    w.other_eq = w.equipment("XRD", "XRD-VB")
    w.oic = _user(UserType.MANAGER, department=w.department)
    EquipmentManager.objects.create(equipment=w.eq, manager=w.oic)
    w.operator = _user(UserType.OPERATOR, department=w.department)
    EquipmentOperator.objects.create(equipment=w.eq, operator=w.operator)
    w.dept_admin = _user(UserType.DEPT_ADMIN, department=w.department)
    w.admin = _user(UserType.ADMIN)
    w.faculty = _user(UserType.FACULTY, department=w.department)
    w.mine, _ = w.booking(w.student, w.eq, w.future(days=3))
    w.theirs, _ = w.booking(w.other, w.other_eq, w.future(days=4))
    return w


@pytest.mark.parametrize("role", ["student", "oic", "operator", "dept_admin", "admin"])
@pytest.mark.parametrize("extra", [{}, {"list_status": "BOOKED"}])
def test_view_booking_list_answers_for_every_role(lab, role, extra):
    user = getattr(lab, role)
    res = _client(user).get("/api/bookings/", {**PAGE, **extra})
    assert res.status_code == 200, (role, res.status_code, str(res.content)[:500])
    ids = {b["real_booking_id"] for b in res.json()["bookings"]}
    if role in ("student", "oic", "operator", "dept_admin", "admin"):
        assert lab.mine.pk in ids, (role, ids)
    if role in ("oic", "operator", "student"):
        assert lab.theirs.pk not in ids, role
