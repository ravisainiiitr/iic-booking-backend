"""Bookings by a test faculty, or by students paying from a test faculty's wallet, are not counted in revenue."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment.models import BookingStatus
from iic_booking.users.models import Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _book(f, owner, equipment, start, *, status=BookingStatus.COMPLETED, total_charge="10.00"):
    booking = f.booking(owner, equipment, start, total_charge=total_charge, slot_count=1)
    booking.status = status
    booking.user_type_snapshot = owner.user_type
    booking.save(update_fields=["status", "user_type_snapshot"])
    return booking


@pytest.fixture
def scenario(egs_factory):
    """A real student (₹100) and a test faculty whose linked (unflagged) student books ₹500 and who books ₹300."""
    f = egs_factory
    eq = f.equipment()
    start = f.future(days=2)
    real_student = f.student()
    test_faculty = UserFactory(user_type=UserType.FACULTY, department=f.department, is_test_account=True)
    wallet = Wallet.objects.create(user=test_faculty)
    linked_student = f.student()
    WalletJoinRequest.objects.create(
        student=linked_student, faculty=test_faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )
    _book(f, real_student, eq, start, total_charge="100.00")
    _book(f, linked_student, eq, start + timedelta(days=1), total_charge="500.00")
    _book(f, test_faculty, eq, start + timedelta(days=2), total_charge="300.00")
    return {"f": f, "eq": eq, "start": start, "test_faculty": test_faculty, "linked_student": linked_student}


def _stats(f, user):
    resp = f.client_for(user).get("/api/bookings/stats/")
    assert resp.status_code == 200, resp.content
    return resp.json()


def test_admin_and_dept_admin_report_revenue_leaves_out_test_wallet_bookings(scenario, monkeypatch):
    monkeypatch.setattr("iic_booking.users.rbac.user_has_permission", lambda *a, **k: True)
    f = scenario["f"]
    admin = UserFactory(user_type=UserType.ADMIN)
    data = _stats(f, admin)
    assert data["total_bookings"] == 1
    assert Decimal(str(data["total_spent"])) == Decimal("100.00")
    dept_admin = UserFactory(user_type=UserType.DEPT_ADMIN, department=f.department)
    assert Decimal(str(_stats(f, dept_admin)["total_spent"])) == Decimal("100.00")


def test_test_faculty_and_linked_student_still_see_their_own_bookings(scenario):
    f = scenario["f"]
    faculty_view = _stats(f, scenario["test_faculty"])
    assert faculty_view["total_bookings"] == 2
    assert Decimal(str(faculty_view["total_spent"])) == Decimal("800.00")
    assert _stats(f, scenario["linked_student"])["total_bookings"] == 1


def test_equipment_report_revenue_leaves_out_test_wallet_bookings(scenario):
    from iic_booking.equipment.reports import get_equipment_report_data

    d0 = timezone.localtime(scenario["start"]).date()
    rep = get_equipment_report_data(
        (d0 - timedelta(days=1)).isoformat(), (d0 + timedelta(days=5)).isoformat(), [scenario["eq"].pk]
    )
    assert rep["summary"]["revenue_total"] == 100.0
    for key in ("revenue_by_user_type", "revenue_by_department", "revenue_by_user", "revenue_by_equipment"):
        assert sum(float(r["total"]) for r in rep["financial"][key]) == 100.0, key


def test_finance_report_revenue_leaves_out_test_wallet_bookings(scenario):
    from iic_booking.users.finance_reports import build_finance_report

    f = scenario["f"]
    finance = UserFactory(user_type=UserType.FINANCE, department=f.department)
    today = timezone.localdate()
    rep = build_finance_report(user=finance, date_from=today, date_to=today)
    assert rep["summary"]["booking_count"] == 1
    assert rep["summary"]["total_revenue"] == 100.0


def test_admin_dashboard_revenue_leaves_out_test_wallet_bookings(scenario):
    from iic_booking.equipment.admin_dashboard_summary import build_admin_dashboard_summary

    payload = build_admin_dashboard_summary(UserFactory(user_type=UserType.ADMIN))
    assert payload["revenue"]["charged_this_month"] == 100.0
