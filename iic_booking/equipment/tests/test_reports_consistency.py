"""Sanity tests: every Reports & Statistics widget agrees with the same scoped bookings."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment.models import BookingSlotRange, BookingStatus, EquipmentManager, EquipmentOperator
from iic_booking.equipment.tests.conftest import _EgsFactory
from iic_booking.users.models import Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _link(faculty, student, wallet):
    WalletJoinRequest.objects.create(
        student=student, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )


def _book(f, owner, equipment, start, *, status=BookingStatus.BOOKED, total_charge="10.00", slot_count=1,
          user_type_snapshot=UserType.STUDENT, **fields):
    booking = f.booking(owner, equipment, start, total_charge=total_charge, slot_count=slot_count, **fields)
    booking.status = status
    booking.user_type_snapshot = user_type_snapshot
    booking.save(update_fields=["status", "user_type_snapshot"])
    return booking


def _stats(f, user, **params):
    resp = f.client_for(user).get("/api/bookings/stats/", params)
    assert resp.status_code == 200, resp.content
    data = resp.json()
    assert sum(data["status_counts"].values()) == data["total_bookings"]
    assert data["status_sum_matches_total"] is True
    return data


@pytest.fixture
def staff_permission(monkeypatch):
    monkeypatch.setattr("iic_booking.users.rbac.user_has_permission", lambda *a, **k: True)


@pytest.fixture
def faculty_group(egs_factory):
    """Screenshot scenario: faculty + 2 linked students, 3 bookings (1 BOOKED, 2 COMPLETED), ₹1100."""
    f = egs_factory
    faculty = UserFactory(user_type=UserType.FACULTY, department=f.department)
    wallet = Wallet.objects.create(user=faculty)
    mayank, neha = f.student(), f.student()
    _link(faculty, mayank, wallet)
    _link(faculty, neha, wallet)
    squid, ppms = f.equipment(), f.equipment()
    start = f.future(days=2)
    bookings = [
        _book(f, mayank, ppms, start, total_charge="330.00", slot_count=2),
        _book(f, mayank, squid, start + timedelta(days=1), total_charge="220.00", slot_count=3,
              status=BookingStatus.COMPLETED),
        _book(f, neha, squid, start + timedelta(days=2), total_charge="550.00", slot_count=4,
              status=BookingStatus.COMPLETED),
    ]
    return {"faculty": faculty, "wallet": wallet, "students": (mayank, neha), "equipment": (squid, ppms),
            "bookings": bookings, "start": start}


def _add_uncharged(f, group):
    """Refunded / operator-unavailable / waitlisted / cancelled bookings (slots released)."""
    mayank, neha = group["students"]
    squid, ppms = group["equipment"]
    start = group["start"] + timedelta(days=5)
    return [
        _book(f, neha, squid, start, total_charge="200.00", slot_count=0, status=BookingStatus.REFUNDED),
        _book(f, mayank, ppms, start, total_charge="100.00", slot_count=0, status=BookingStatus.ABSENT),
        _book(f, mayank, ppms, start, total_charge="80.00", slot_count=0, status=BookingStatus.WAITLISTED),
        _book(f, mayank, squid, start, total_charge="60.00", slot_count=0, status=BookingStatus.CANCELLED),
    ]


# ------------------------------------------------------------------ faculty (own + linked students)


def test_faculty_screenshot_scenario_all_widgets_agree(egs_factory, faculty_group):
    from iic_booking.users.faculty_wallet_report import build_faculty_wallet_expense_report

    data = _stats(egs_factory, faculty_group["faculty"])
    assert data["scope"] == "wallet_group"
    assert data["total_bookings"] == 3
    assert data["status_counts"] == {"COMPLETED": 2, "BOOKED": 1}
    assert data["charged_bookings"] == 3
    assert Decimal(str(data["total_spent"])) == Decimal("1100.00")
    assert data["total_hours"] == 9.0
    assert data["average_cost"] == 366.67
    assert data["refunded_amount"] == 0.0

    rep = build_faculty_wallet_expense_report(faculty_group["faculty"])
    assert Decimal(rep["period_booking_spend"]["total"]) == Decimal("1100.00")
    assert rep["period_booking_spend"]["booking_count"] == 3
    mayank, neha = faculty_group["students"]
    members = {m["user_id"]: (m["booking_count"], Decimal(m["total_spend"])) for m in rep["by_member"]}
    assert members == {mayank.pk: (2, Decimal("550.00")), neha.pk: (1, Decimal("550.00"))}
    squid, ppms = faculty_group["equipment"]
    equipment = {e["equipment_id"]: (e["booking_count"], Decimal(e["total_spend"])) for e in rep["by_equipment"]}
    assert equipment == {squid.pk: (2, Decimal("770.00")), ppms.pk: (1, Decimal("330.00"))}
    assert sum(c for c, _ in members.values()) == sum(c for c, _ in equipment.values()) == 3
    assert sum(s for _, s in members.values()) == sum(s for _, s in equipment.values()) == Decimal("1100.00")


def test_faculty_uncharged_bookings_counted_but_not_spent(egs_factory, faculty_group):
    from iic_booking.users.faculty_wallet_report import build_faculty_wallet_expense_report

    _add_uncharged(egs_factory, faculty_group)
    data = _stats(egs_factory, faculty_group["faculty"])
    assert data["total_bookings"] == 7
    assert data["status_counts"] == {
        "COMPLETED": 2, "BOOKED": 1, "REFUNDED": 1, "ABSENT": 1, "WAITLISTED": 1, "CANCELLED": 1,
    }
    assert data["charged_bookings"] == 3
    assert Decimal(str(data["total_spent"])) == Decimal("1100.00")
    assert data["total_hours"] == 9.0
    assert data["average_cost"] == 366.67
    assert data["refunded_amount"] == 300.0

    rep = build_faculty_wallet_expense_report(faculty_group["faculty"])
    assert Decimal(rep["period_booking_spend"]["total"]) == Decimal("1100.00")
    assert rep["period_booking_spend"]["booking_count"] == 3
    assert rep["period_booking_spend"]["uncharged_booking_count"] == 4
    assert sum(m["booking_count"] for m in rep["by_member"]) == 3


def test_status_and_date_filters_use_same_scope(egs_factory, faculty_group):
    f = egs_factory
    _add_uncharged(f, faculty_group)
    faculty = faculty_group["faculty"]

    completed = _stats(f, faculty, status="completed")
    assert completed["total_bookings"] == 2
    assert completed["status_counts"] == {"COMPLETED": 2}
    assert Decimal(str(completed["total_spent"])) == Decimal("770.00")

    refunded = _stats(f, faculty, status="REFUNDED")
    assert refunded["total_bookings"] == 1 and refunded["total_spent"] == 0.0 and refunded["refunded_amount"] == 200.0

    today = timezone.localdate()
    assert _stats(f, faculty, date_from=today.isoformat(), date_to=today.isoformat())["total_bookings"] == 7
    assert _stats(f, faculty, date_to=(today - timedelta(days=1)).isoformat())["total_bookings"] == 0

    client = f.client_for(faculty)
    assert client.get("/api/bookings/stats/", {"status": "NOPE"}).status_code == 400
    assert client.get("/api/bookings/stats/", {"date_from": "2026-13-40"}).status_code == 400


# ------------------------------------------------------------------ personal scopes


def test_student_sees_only_own_bookings(egs_factory, faculty_group):
    mayank, neha = faculty_group["students"]
    data = _stats(egs_factory, mayank)
    assert data["scope"] == "personal"
    assert data["total_bookings"] == 2
    assert data["status_counts"] == {"BOOKED": 1, "COMPLETED": 1}
    assert Decimal(str(data["total_spent"])) == Decimal("550.00")
    assert _stats(egs_factory, neha)["total_bookings"] == 1


def test_external_user_sees_only_own_bookings(egs_factory, faculty_group):
    f = egs_factory
    ext_type = sorted(UserType.get_external_user_codes())[0]
    ext = UserFactory(user_type=ext_type)
    squid, _ = faculty_group["equipment"]
    _book(f, ext, squid, f.future(days=9), total_charge="1000.00", user_type_snapshot=ext_type)
    _book(f, ext, squid, f.future(days=10), total_charge="500.00", slot_count=0,
          status=BookingStatus.REFUNDED, user_type_snapshot=ext_type)
    data = _stats(f, ext)
    assert data["total_bookings"] == 2
    assert data["status_counts"] == {"BOOKED": 1, "REFUNDED": 1}
    assert Decimal(str(data["total_spent"])) == Decimal("1000.00")
    assert data["refunded_amount"] == 500.0


def test_test_account_sees_own_bookings_but_is_hidden_from_staff(egs_factory, faculty_group, staff_permission):
    f = egs_factory
    tester = f.student()
    tester.is_test_account = True
    tester.save(update_fields=["is_test_account"])
    squid, _ = faculty_group["equipment"]
    _book(f, tester, squid, f.future(days=12), total_charge="999.00")
    assert _stats(f, tester)["total_bookings"] == 1
    admin = UserFactory(user_type=UserType.ADMIN)
    assert _stats(f, admin)["total_bookings"] == 3


# ------------------------------------------------------------------ staff scopes


def test_oic_operator_dept_admin_and_admin_scopes(egs_factory, faculty_group, staff_permission):
    f = egs_factory
    _add_uncharged(f, faculty_group)
    squid, ppms = faculty_group["equipment"]

    oic = UserFactory(user_type=UserType.MANAGER, department=f.department)
    EquipmentManager.objects.create(equipment=squid, manager=oic)
    data = _stats(f, oic)
    assert data["scope"] == "equipment"
    assert data["total_bookings"] == 4
    assert data["status_counts"] == {"COMPLETED": 2, "REFUNDED": 1, "CANCELLED": 1}
    assert Decimal(str(data["total_spent"])) == Decimal("770.00")

    operator = UserFactory(user_type=UserType.OPERATOR, department=f.department)
    EquipmentOperator.objects.create(equipment=ppms, operator=operator)
    data = _stats(f, operator)
    assert data["total_bookings"] == 3
    assert data["status_counts"] == {"BOOKED": 1, "ABSENT": 1, "WAITLISTED": 1}
    assert Decimal(str(data["total_spent"])) == Decimal("330.00")

    dept_admin = UserFactory(user_type=UserType.DEPT_ADMIN, department=f.department)
    data = _stats(f, dept_admin)
    assert data["scope"] == "department"
    assert data["total_bookings"] == 7
    assert Decimal(str(data["total_spent"])) == Decimal("1100.00")

    other_dept_eq = _EgsFactory().equipment()
    _book(f, f.student(), other_dept_eq, f.future(days=14), total_charge="42.00")
    assert _stats(f, dept_admin)["total_bookings"] == 7

    admin = UserFactory(user_type=UserType.ADMIN)
    data = _stats(f, admin)
    assert data["scope"] == "institute"
    assert data["total_bookings"] == 8
    assert Decimal(str(data["total_spent"])) == Decimal("1142.00")


def test_staff_without_bookings_manage_fall_back_to_personal_scope(egs_factory, faculty_group, monkeypatch):
    monkeypatch.setattr("iic_booking.users.rbac.user_has_permission", lambda *a, **k: False)
    f = egs_factory
    squid, _ = faculty_group["equipment"]
    oic = UserFactory(user_type=UserType.MANAGER, department=f.department)
    EquipmentManager.objects.create(equipment=squid, manager=oic)
    data = _stats(f, oic)
    assert data["scope"] == "personal"
    assert data["total_bookings"] == 0


# ------------------------------------------------------------------ equipment performance report


def test_equipment_report_counts_each_booking_once(egs_factory):
    from iic_booking.equipment.reports import get_equipment_report_data

    f = egs_factory
    eq = f.equipment()
    start = f.future(days=3)
    s1, s2 = f.student(), f.student()
    _book(f, s1, eq, start, total_charge="300.00", slot_count=3, status=BookingStatus.COMPLETED)
    _book(f, s2, eq, start + timedelta(days=1), total_charge="100.00", slot_count=1, status=BookingStatus.COMPLETED)
    _book(f, s2, eq, start + timedelta(days=2), total_charge="150.00", slot_count=2)
    _book(f, s1, eq, start + timedelta(days=3), total_charge="50.00", slot_count=1, status=BookingStatus.ABSENT)
    refunded = _book(f, s1, eq, start, total_charge="70.00", slot_count=0, status=BookingStatus.REFUNDED)
    BookingSlotRange.objects.create(
        booking=refunded, start_datetime=start + timedelta(days=1), end_datetime=start + timedelta(days=1, hours=1)
    )
    far = _book(f, s1, eq, start, total_charge="90.00", slot_count=0, status=BookingStatus.CANCELLED)
    BookingSlotRange.objects.create(
        booking=far, start_datetime=start + timedelta(days=90), end_datetime=start + timedelta(days=90, hours=1)
    )

    d0 = timezone.localtime(start).date()
    rep = get_equipment_report_data((d0 - timedelta(days=1)).isoformat(), (d0 + timedelta(days=5)).isoformat(), [eq.pk])
    row = rep["equipment"][0]
    assert row["total_bookings_in_period"] == 5
    assert row["completed_in_period"] == 2
    assert row["total_booking_hours"] == 6.0
    assert row["distinct_users_served"] == 2
    assert rep["summary"]["revenue_total"] == 400.0
    fin = rep["financial"]
    for key in ("revenue_by_user_type", "revenue_by_department", "revenue_by_user", "revenue_by_equipment"):
        assert sum(float(r["total"]) for r in fin[key]) == 400.0, key
        assert sum(int(r["count"]) for r in fin[key]) == 2, key


# ------------------------------------------------------------------ finance dashboard


def test_finance_revenue_matches_charged_definition(egs_factory, faculty_group):
    from iic_booking.users.finance_reports import build_finance_report

    f = egs_factory
    _add_uncharged(f, faculty_group)
    squid, _ = faculty_group["equipment"]
    _book(f, f.student(), squid, f.future(days=20), total_charge="40.00", status=BookingStatus.PROCESSING)
    finance = UserFactory(user_type=UserType.FINANCE, department=f.department)
    today = timezone.localdate()
    rep = build_finance_report(user=finance, date_from=today, date_to=today)
    s = rep["summary"]
    assert s["booking_count"] == 4
    assert s["total_revenue"] == 1140.0
    assert s["internal_revenue"] + s["external_revenue"] == s["total_revenue"]
    assert s["refunded_amount"] == 300.0
    assert sum(r["bookings"] for r in rep["charts"]["revenue_by_org_category"]) == 4
