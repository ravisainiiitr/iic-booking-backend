"""Lab Operator / Officer in Charge proficiency: pending attribution, handled work, score, ranking and drill-down."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.admin_insights.proficiency import proficiency_score
from iic_booking.equipment.models import (
    BookingEvent,
    BookingEventType,
    BookingSampleTrace,
    BookingStatus,
    EquipmentManager,
    EquipmentOperator,
    SampleTraceStatus,
    UrgentBookingRequest,
)
from iic_booking.equipment.tests.conftest import _EgsFactory
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

URL = "/api/admin/insights/staff-proficiency/"


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def _get(user, **params):
    client = APIClient()
    client.force_authenticate(user=user)
    resp = client.get(URL, params)
    assert resp.status_code == 200, resp.content
    return resp.json()


def _staff(user_type, name, **fields):
    return UserFactory(user_type=user_type, name=name, admin_approved=True, **fields)


def _received(booking):
    BookingSampleTrace.objects.create(booking=booking, status=SampleTraceStatus.SAMPLE_ACCEPTED)
    return booking


@pytest.fixture
def lab():
    a, b = _EgsFactory(), _EgsFactory()
    quick_eq, busy_eq, other_eq = a.equipment(), a.equipment(), b.equipment()
    quick = _staff(UserType.OPERATOR, "Quick Operator")
    busy = _staff(UserType.OPERATOR, "Busy Operator")
    tester = _staff(UserType.OPERATOR, "Test Lab Operator", is_test_account=True)
    oic = _staff(UserType.MANAGER, "Busy OIC")
    EquipmentOperator.objects.create(equipment=quick_eq, operator=quick)
    EquipmentOperator.objects.create(equipment=quick_eq, operator=tester, role="SECONDARY")
    EquipmentOperator.objects.create(equipment=busy_eq, operator=busy)
    EquipmentManager.objects.create(equipment=busy_eq, manager=oic)

    student = a.student()
    ended = timezone.localtime(timezone.now() - timedelta(days=1)).replace(minute=0, second=0, microsecond=0)
    pending = [_received(a.booking(student, busy_eq, ended - timedelta(hours=3 * i))) for i in range(2)]
    done = _received(a.booking(student, quick_eq, ended - timedelta(days=1)))
    done.status = BookingStatus.COMPLETED
    done.save(update_fields=["status"])
    BookingSampleTrace.objects.filter(booking=done).update(created_at=ended - timedelta(days=1))
    BookingEvent.objects.create(
        booking=done, event_type=BookingEventType.COMPLETED, new_status=BookingStatus.COMPLETED, created_by=quick
    )
    test_user = a.student()
    test_user.is_test_account = True
    test_user.save(update_fields=["is_test_account"])
    _received(a.booking(test_user, quick_eq, ended))
    UrgentBookingRequest.objects.create(user=student, equipment=busy_eq)
    return {"a": a, "b": b, "quick": quick, "busy": busy, "tester": tester, "oic": oic, "pending": pending,
            "done": done}


def test_score_formula():
    assert proficiency_score(3, 1, 0) == 75
    assert proficiency_score(3, 1, 1) == 60  # overdue items count twice
    assert proficiency_score(0, 0, 0) is None


def test_ranking_pending_and_handled(lab):
    admin = _staff(UserType.ADMIN, "Main Admin")
    data = _get(admin)
    operators = {r["name"]: r for r in data["operators"]}
    assert set(operators) == {"Quick Operator", "Busy Operator"}  # test accounts are never ranked
    quick, busy = operators["Quick Operator"], operators["Busy Operator"]
    assert (quick["pending"], quick["handled"], quick["score"], quick["rank"]) == (0, 1, 100, 1)
    assert quick["avg_response_hours"] == pytest.approx(47, abs=1.5)  # completed now; slot ended ~47 h ago
    assert (busy["pending"], busy["handled"], busy["score"], busy["rank"]) == (2, 0, 0, 2)
    assert [r["name"] for r in data["operators"]] == ["Quick Operator", "Busy Operator"]

    oic = data["oics"][0]
    assert oic["name"] == "Busy OIC" and oic["pending"] == 3
    assert {k["kind"]: k["count"] for k in oic["pending_by_kind"]} == {"completion": 2, "urgent": 1}
    assert "formula" in data and data["days"] == 30

    by_pending = _get(admin, sort="pending")
    assert [r["name"] for r in by_pending["operators"]] == ["Quick Operator", "Busy Operator"]


def test_drill_down_links_bookings(lab):
    admin = _staff(UserType.ADMIN, "Main Admin")
    data = _get(admin, person=str(lab["busy"].pk), role="operator")
    rows = data["person"]["pending"]
    assert {r["booking_pk"] for r in rows} == {b.pk for b in lab["pending"]}
    assert all(r["link"] == f"/booking-management?expand={r['booking_pk']}" for r in rows)
    assert all(r["kind"] == "completion" for r in rows)


def test_department_scope(lab):
    dept_admin = _staff(UserType.DEPT_ADMIN, "Dept Admin", department=lab["b"].department)
    assert _get(dept_admin)["operators"] == []
    admin = _staff(UserType.ADMIN, "Main Admin")
    assert _get(admin, dept=str(lab["b"].department.pk))["operators"] == []
    assert len(_get(admin, dept=str(lab["a"].department.pk))["operators"]) == 2


def test_only_dashboard_viewers(lab):
    client = APIClient()
    client.force_authenticate(user=lab["busy"])
    assert client.get(URL).status_code == 403
