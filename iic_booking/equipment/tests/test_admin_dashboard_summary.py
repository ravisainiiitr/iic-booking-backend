"""Administrator home overview: who may see it, how it is scoped, and that it agrees with Reports."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import BookingAttemptLog, BookingStatus, WaitlistEntry
from iic_booking.equipment.tests.conftest import _EgsFactory
from iic_booking.support.models import Ticket
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

URL = "/api/admin/dashboard-summary/"


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def _client(user) -> APIClient:
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _summary(user, **params):
    resp = _client(user).get(URL, params)
    assert resp.status_code == 200, resp.content
    return resp.json()


def _book(f, owner, equipment, start, *, status=BookingStatus.BOOKED, total_charge="100.00", slot_count=1):
    booking = f.booking(owner, equipment, start, total_charge=total_charge, slot_count=slot_count)
    if status != BookingStatus.BOOKED:
        booking.status = status
        booking.save(update_fields=["status"])
    return booking


@pytest.fixture
def two_departments():
    """Department A: 2 equipment (1 under maintenance), 2 bookings; department B: 1 equipment, 1 booking."""
    a, b = _EgsFactory(), _EgsFactory()
    eq_a1 = a.equipment()
    eq_a2 = a.equipment(status="REPAIR")
    eq_b = b.equipment()
    student_a, student_b = a.student(), b.student()
    now = timezone.localtime()
    today_slot = now.replace(minute=0, second=0, microsecond=0)
    bookings = {
        "a_today": _book(a, student_a, eq_a1, today_slot, total_charge="150.00"),
        "a_cancelled": _book(a, student_a, eq_a1, a.future(days=2), status=BookingStatus.CANCELLED, total_charge="40.00"),
        "b_future": _book(b, student_b, eq_b, b.future(days=2), total_charge="70.00", slot_count=2),
    }
    WaitlistEntry.objects.create(user=student_a, equipment=eq_a1, status="ACTIVE")
    WaitlistEntry.objects.create(user=student_b, equipment=eq_b, status="ACTIVE")
    WaitlistEntry.objects.create(user=UserFactory(user_type=UserType.STUDENT), equipment=eq_b, status="OPT_OUT")
    BookingAttemptLog.objects.create(user=student_a, equipment=eq_a1, outcome="FAILED", failure_reason="No slot")
    BookingAttemptLog.objects.create(user=student_b, equipment=eq_b, outcome="FAILED", failure_reason="No slot")
    BookingAttemptLog.objects.create(user=student_b, equipment=eq_b, outcome="SUCCESS")
    return {"a": a, "b": b, "eq": (eq_a1, eq_a2, eq_b), "students": (student_a, student_b), "bookings": bookings}


def _admin():
    return UserFactory(user_type=UserType.ADMIN)


def _dept_admin(department):
    return UserFactory(user_type=UserType.DEPT_ADMIN, department=department)


@pytest.mark.parametrize(
    "user_type", [UserType.STUDENT, UserType.FACULTY, UserType.MANAGER, UserType.OPERATOR]
)
def test_other_roles_are_forbidden(user_type):
    resp = _client(UserFactory(user_type=user_type)).get(URL)
    assert resp.status_code == 403


def test_anonymous_is_rejected():
    assert APIClient().get(URL).status_code in (401, 403)


def test_main_admin_sees_the_whole_institute(two_departments):
    data = _summary(_admin())
    assert data["scope"] == "institute"
    assert data["department"] is None

    assert data["bookings"]["created_today"] == 3
    assert data["bookings"]["sessions_today"] == 1
    assert data["bookings"]["sessions_next_7_days"] == 2
    revenue = data["revenue"]
    assert revenue["charged_this_month"] == pytest.approx(220.0)
    assert revenue["charged_bookings_this_month"] == 2

    assert data["equipment"] == {"total": 3, "operational": 2, "under_maintenance": 1, "disposed": 0, "other": 0}
    assert data["waitlist"]["active"] == 2
    attempts = data["booking_attempts"]
    assert (attempts["total"], attempts["failed"]) == (3, 2)
    assert attempts["top_failure_reasons"][0] == {"reason": "No slot", "count": 2}

    assert len(data["bookings_per_day"]) == 30
    assert data["bookings_per_day"][-1]["date"] == timezone.localdate().isoformat()
    assert data["bookings_per_day"][-1]["count"] == 3
    # Utilisation counts slots up to today; department B's booking is still in the future.
    assert [row["equipment_id"] for row in data["top_equipment"]] == [two_departments["eq"][0].pk]
    assert len(data["recent_bookings"]) == 3
    assert data["system"]["server_time"]


def test_department_admin_is_scoped_to_their_department(two_departments):
    dept_a = two_departments["a"].department
    data = _summary(_dept_admin(dept_a))
    assert data["scope"] == "department"
    assert data["department"]["id"] == dept_a.pk

    assert data["bookings"]["created_today"] == 2
    assert data["bookings"]["sessions_today"] == 1
    assert data["bookings"]["sessions_next_7_days"] == 1
    assert data["revenue"]["charged_this_month"] == pytest.approx(150.0)
    assert data["equipment"]["total"] == 2
    assert data["equipment"]["under_maintenance"] == 1
    assert data["waitlist"]["active"] == 1
    assert data["booking_attempts"]["total"] == 1
    assert {row["equipment_id"] for row in data["top_equipment"]} == {two_departments["eq"][0].pk}
    assert {row["booking_id"] for row in data["recent_bookings"]} == {
        two_departments["bookings"]["a_today"].pk,
        two_departments["bookings"]["a_cancelled"].pk,
    }
    assert data["ratings"]["portal_average"] is None


def test_revenue_matches_reports_for_the_same_scope(two_departments):
    dept_admin = _dept_admin(two_departments["a"].department)
    stats = _client(dept_admin).get(
        "/api/bookings/stats/", {"date_from": timezone.localdate().replace(day=1).isoformat()}
    ).json()
    data = _summary(dept_admin)
    assert data["revenue"]["charged_this_month"] == pytest.approx(stats["total_spent"])
    assert data["revenue"]["charged_bookings_this_month"] == stats["charged_bookings"]


def test_department_admin_without_department_sees_nothing(two_departments):
    data = _summary(UserFactory(user_type=UserType.DEPT_ADMIN, department=None))
    assert data["bookings"]["created_today"] == 0
    assert data["equipment"]["total"] == 0
    assert data["waitlist"]["active"] == 0
    assert data["recent_bookings"] == []


def test_test_account_bookings_are_excluded(two_departments):
    f = two_departments["a"]
    tester = f.student()
    tester.is_test_account = True
    tester.save(update_fields=["is_test_account"])
    _book(f, tester, two_departments["eq"][0], f.future(days=1), total_charge="999.00")
    assert _summary(_admin())["bookings"]["created_today"] == 3


def test_open_tickets_need_main_admin_attention(two_departments):
    student = two_departments["students"][0]
    Ticket.objects.create(user=student, subject="Help", description="d")
    Ticket.objects.create(user=student, subject="Done", description="d", status=Ticket.TicketStatus.RESOLVED)
    items = {i["key"]: i for i in _summary(_admin())["attention"]}
    assert items["open_support_tickets"]["count"] == 1
    assert items["open_support_tickets"]["link"] == "/admin-settings/support"

    dept_items = {i["key"] for i in _summary(_dept_admin(two_departments["a"].department))["attention"]}
    assert "open_support_tickets" not in dept_items


def test_summary_is_cached_per_scope_and_refresh_rebuilds(two_departments):
    admin = _admin()
    dept_admin = _dept_admin(two_departments["a"].department)
    assert _summary(admin)["bookings"]["created_today"] == 3
    assert _summary(dept_admin)["bookings"]["created_today"] == 2

    f = two_departments["a"]
    _book(f, two_departments["students"][0], two_departments["eq"][0], f.future(days=4))
    assert _summary(_admin())["bookings"]["created_today"] == 3
    assert _summary(dept_admin)["bookings"]["created_today"] == 2
    assert _summary(admin, refresh="1")["bookings"]["created_today"] == 4
    assert _summary(_admin())["bookings"]["created_today"] == 4


def test_new_registrations_are_counted_in_scope(two_departments):
    dept_a = two_departments["a"].department
    old = UserFactory(user_type=UserType.STUDENT, department=dept_a)
    old.date_joined = timezone.now() - timedelta(days=60)
    old.save(update_fields=["date_joined"])
    dept_admin = _dept_admin(dept_a)
    in_department = UserFactory._meta.model.objects.filter(department=dept_a, is_test_account=False)
    users = _summary(dept_admin)["users"]
    assert users["new_last_30_days"] == in_department.count() - 1
    assert users["new_last_7_days"] == users["new_last_30_days"]
    assert users["active"] == in_department.filter(is_active=True).count()


def test_query_count_does_not_grow_with_bookings(two_departments, django_assert_max_num_queries):
    from iic_booking.equipment.admin_dashboard_summary import build_admin_dashboard_summary

    f = two_departments["a"]
    for days in range(1, 6):
        _book(f, two_departments["students"][0], two_departments["eq"][0], f.future(days=days))
    admin = _admin()
    with django_assert_max_num_queries(20):
        build_admin_dashboard_summary(admin)


def test_unique_cache_key_per_department():
    from iic_booking.equipment.admin_dashboard_summary import _Scope

    a = _Scope(UserFactory(user_type=UserType.DEPT_ADMIN, department=_EgsFactory().department))
    b = _Scope(UserFactory(user_type=UserType.DEPT_ADMIN, department=_EgsFactory().department))
    assert a.cache_key != b.cache_key
    assert _Scope(UserFactory(user_type=UserType.ADMIN)).cache_key.endswith(":institute")
