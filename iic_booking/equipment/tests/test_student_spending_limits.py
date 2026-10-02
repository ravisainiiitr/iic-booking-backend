"""Supervisor-set weekly / monthly spending limits for students booking on the faculty wallet."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone as dt_timezone
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    DynamicInputField,
    DynamicInputFieldType,
    EquipmentManager,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.repositories.wallet_repository import SubWalletRepository
from iic_booking.users.student_spending_limits import (
    IST,
    SPENDING_LIMIT_ERROR_CODE,
    limit_summary,
    month_bounds,
    spending_limit_error,
    student_spend,
    week_bounds,
)
from iic_booking.users.tests.factories import UserFactory

# Wednesday 7 Oct 2026, noon IST: week is Mon 5 Oct - Sun 11 Oct, month is October.
NOW = datetime(2026, 10, 7, 12, 0, tzinfo=IST)


def _supervised(egs_factory, *, balance="10000.00", **limits):
    student = egs_factory.student()
    faculty = UserFactory(user_type=UserType.FACULTY, department=egs_factory.department)
    wallet = Wallet.objects.create(user=faculty)
    link = WalletJoinRequest.objects.create(
        student=student,
        faculty=faculty,
        wallet=wallet,
        status=WalletJoinRequestStatus.APPROVED,
        responded_at=datetime(2026, 1, 1, tzinfo=IST),
        **limits,
    )
    sub = SubWalletRepository.get_or_create(wallet, egs_factory.department)
    sub.credit(Decimal(balance), description="Recharge")
    return student, faculty, link, sub


@pytest.fixture
def no_portal_lock(monkeypatch):
    from iic_booking.users.legacy_ledger import booking_lock

    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))


def _booking_at(egs_factory, student, equipment, created_at, charge, *, status=BookingStatus.BOOKED, **fields):
    booking = egs_factory.booking(student, equipment, egs_factory.future(), total_charge=charge, **fields)
    Booking.objects.filter(pk=booking.pk).update(created_at=created_at, status=status)
    return booking


# --- periods ---------------------------------------------------------------------------


def test_week_and_month_bounds_are_ist_monday_and_calendar_month():
    w_start, w_end = week_bounds(NOW)
    assert w_start == datetime(2026, 10, 5, tzinfo=IST)
    assert w_end == datetime(2026, 10, 12, tzinfo=IST)
    m_start, m_end = month_bounds(NOW)
    assert m_start == datetime(2026, 10, 1, tzinfo=IST)
    assert m_end == datetime(2026, 11, 1, tzinfo=IST)
    # Sunday 18:45 UTC is already Monday 00:15 IST.
    sunday_utc = datetime(2026, 10, 11, 18, 45, tzinfo=dt_timezone.utc)
    assert week_bounds(sunday_utc)[0] == datetime(2026, 10, 12, tzinfo=IST)
    assert month_bounds(datetime(2026, 12, 31, 20, 0, tzinfo=dt_timezone.utc))[0] == datetime(2027, 1, 1, tzinfo=IST)


# --- spend calculation -------------------------------------------------------------------


@pytest.mark.django_db
def test_spend_counts_charged_bookings_by_ist_creation_time(egs_factory):
    student, _faculty, link, _sub = _supervised(egs_factory, spending_limit_enabled=True, weekly_limit_inr=100)
    eq = egs_factory.equipment()
    # Monday 00:30 IST (Sunday 19:00 UTC) belongs to this week.
    _booking_at(egs_factory, student, eq, datetime(2026, 10, 4, 19, 0, tzinfo=dt_timezone.utc), "30.00")
    # Sunday 23:30 IST: last week, same month.
    _booking_at(egs_factory, student, eq, datetime(2026, 10, 4, 23, 30, tzinfo=IST), "40.00")
    # Previous month.
    _booking_at(egs_factory, student, eq, datetime(2026, 9, 30, 23, 30, tzinfo=IST), "50.00")
    # Not charged / fully refunded.
    for st in (BookingStatus.CANCELLED, BookingStatus.REFUNDED, BookingStatus.ABSENT, BookingStatus.WAITLISTED):
        _booking_at(egs_factory, student, eq, NOW - timedelta(hours=1), "500.00", status=st)
    # Booking with an unpaid input-edit extra counts at its paid charge only.
    _booking_at(
        egs_factory, student, eq, NOW - timedelta(hours=2), "25.00", charge_recalculation_pending_amount=Decimal("15.00")
    )

    w_start, w_end = week_bounds(NOW)
    m_start, m_end = month_bounds(NOW)
    assert student_spend(link, w_start, w_end) == Decimal("40.00")
    assert student_spend(link, m_start, m_end) == Decimal("80.00")


@pytest.mark.django_db
def test_bookings_before_joining_this_supervisor_do_not_count(egs_factory):
    student, _faculty, link, _sub = _supervised(egs_factory)
    eq = egs_factory.equipment()
    _booking_at(egs_factory, student, eq, datetime(2026, 10, 5, 9, 0, tzinfo=IST), "60.00")
    link.responded_at = datetime(2026, 10, 6, 9, 0, tzinfo=IST)
    link.save()
    _booking_at(egs_factory, student, eq, datetime(2026, 10, 6, 10, 0, tzinfo=IST), "20.00")
    assert student_spend(link, *week_bounds(NOW)) == Decimal("20.00")


# --- enforcement (service) -----------------------------------------------------------------


@pytest.mark.django_db
def test_weekly_limit_boundary_and_message(egs_factory):
    student, _faculty, _link, sub = _supervised(egs_factory, spending_limit_enabled=True, weekly_limit_inr=100)
    eq = egs_factory.equipment()
    _booking_at(egs_factory, student, eq, NOW - timedelta(days=1), "80.00")

    assert spending_limit_error(student, sub, Decimal("20.00"), at=NOW) is None
    err = spending_limit_error(student, sub, Decimal("20.01"), at=NOW)
    assert err == (
        "This booking exceeds the weekly spending limit (₹100.00) set by your supervisor. "
        "Remaining this week: ₹20.00."
    )


@pytest.mark.django_db
def test_monthly_limit_blocks_when_weekly_is_fine(egs_factory):
    student, _faculty, _link, sub = _supervised(
        egs_factory, spending_limit_enabled=True, weekly_limit_inr=500, monthly_limit_inr=1000
    )
    eq = egs_factory.equipment()
    _booking_at(egs_factory, student, eq, datetime(2026, 10, 2, 10, 0, tzinfo=IST), "900.00")

    assert spending_limit_error(student, sub, Decimal("100.00"), at=NOW) is None
    err = spending_limit_error(student, sub, Decimal("150.00"), at=NOW)
    assert "monthly spending limit (₹1,000.00)" in err
    assert "Remaining this month: ₹100.00." in err


@pytest.mark.django_db
def test_disabled_limit_and_zero_charges_never_block(egs_factory):
    student, _faculty, link, sub = _supervised(egs_factory, weekly_limit_inr=10, monthly_limit_inr=10)
    eq = egs_factory.equipment()
    _booking_at(egs_factory, student, eq, NOW - timedelta(days=1), "80.00")
    assert spending_limit_error(student, sub, Decimal("500.00"), at=NOW) is None

    link.spending_limit_enabled = True
    link.save()
    assert spending_limit_error(student, sub, Decimal("0.00"), at=NOW) is None
    assert spending_limit_error(student, sub, Decimal("1.00"), at=NOW) is not None


@pytest.mark.django_db
def test_limit_only_applies_to_the_supervisor_wallet(egs_factory):
    student, faculty, _link, _sub = _supervised(egs_factory, spending_limit_enabled=True, weekly_limit_inr=0)
    other_faculty = UserFactory(user_type=UserType.FACULTY, department=egs_factory.department)
    other_sub = SubWalletRepository.get_or_create(Wallet.objects.create(user=other_faculty), egs_factory.department)
    own_sub = SubWalletRepository.get_or_create(faculty.wallet, egs_factory.department)
    assert spending_limit_error(student, other_sub, Decimal("10.00"), at=NOW) is None
    assert spending_limit_error(faculty, own_sub, Decimal("10.00"), at=NOW) is None


@pytest.mark.django_db
def test_extra_charge_on_booking_from_closed_week_skips_weekly_check(egs_factory):
    student, _faculty, _link, sub = _supervised(
        egs_factory, spending_limit_enabled=True, weekly_limit_inr=50, monthly_limit_inr=1000
    )
    eq = egs_factory.equipment()
    last_week = datetime(2026, 10, 2, 10, 0, tzinfo=IST)
    _booking_at(egs_factory, student, eq, last_week, "50.00")
    assert spending_limit_error(student, sub, Decimal("30.00"), attribution_at=last_week, at=NOW) is None
    assert spending_limit_error(student, sub, Decimal("30.00"), at=NOW) is None
    assert spending_limit_error(student, sub, Decimal("50.01"), at=NOW) is not None


# --- API -------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_faculty_sets_and_reads_limits(egs_factory):
    student, faculty, link, _sub = _supervised(egs_factory)
    client = egs_factory.client_for(faculty)

    listed = client.get("/api/wallet/student-spending-limits/")
    assert listed.status_code == 200
    row = listed.data["limits"][0]
    assert row["join_request_id"] == link.pk
    assert row["spending_limit_enabled"] is False
    assert row["weekly_limit_inr"] is None and row["monthly_limit_inr"] is None

    url = f"/api/wallet/join-requests/{link.pk}/spending-limit/"
    resp = client.put(url, {"spending_limit_enabled": True, "weekly_limit_inr": "1500"}, format="json")
    assert resp.status_code == 200, resp.data
    assert resp.data["limit"]["weekly_limit_inr"] == "1500.00"
    assert resp.data["limit"]["monthly_limit_inr"] is None
    link.refresh_from_db()
    assert link.spending_limit_enabled is True
    assert link.weekly_limit_inr == Decimal("1500.00")

    resp = client.put(url, {"spending_limit_enabled": True, "weekly_limit_inr": "", "monthly_limit_inr": 5000})
    assert resp.status_code == 200, resp.data
    link.refresh_from_db()
    assert link.weekly_limit_inr is None and link.monthly_limit_inr == Decimal("5000.00")

    resp = client.patch(url, {"spending_limit_enabled": False}, format="json")
    assert resp.status_code == 200
    link.refresh_from_db()
    assert link.spending_limit_enabled is False
    assert link.monthly_limit_inr == Decimal("5000.00")

    joined = client.get("/api/wallet/join-requests/")
    assert joined.data["requests"][0]["spending_limit_enabled"] is False


@pytest.mark.django_db
@pytest.mark.parametrize(
    "payload, message",
    [
        ({"spending_limit_enabled": True}, "Enter a weekly limit, a monthly limit, or both."),
        ({"spending_limit_enabled": True, "weekly_limit_inr": "", "monthly_limit_inr": None}, "Enter a weekly"),
        ({"spending_limit_enabled": True, "weekly_limit_inr": "-1"}, "Weekly limit cannot be negative."),
        ({"spending_limit_enabled": True, "monthly_limit_inr": "abc"}, "Monthly limit must be a number."),
        ({"spending_limit_enabled": True, "monthly_limit_inr": "NaN"}, "Monthly limit must be a number."),
    ],
)
def test_invalid_limits_are_rejected(egs_factory, payload, message):
    _student, faculty, link, _sub = _supervised(egs_factory)
    resp = egs_factory.client_for(faculty).put(
        f"/api/wallet/join-requests/{link.pk}/spending-limit/", payload, format="json"
    )
    assert resp.status_code == 400
    assert resp.data["error"].startswith(message)
    link.refresh_from_db()
    assert link.spending_limit_enabled is False


@pytest.mark.django_db
def test_zero_is_a_valid_limit(egs_factory):
    _student, faculty, link, _sub = _supervised(egs_factory)
    resp = egs_factory.client_for(faculty).put(
        f"/api/wallet/join-requests/{link.pk}/spending-limit/",
        {"spending_limit_enabled": True, "weekly_limit_inr": "0"},
        format="json",
    )
    assert resp.status_code == 200, resp.data
    link.refresh_from_db()
    assert link.weekly_limit_inr == Decimal("0.00")


@pytest.mark.django_db
def test_only_the_students_supervisor_can_manage_limits(egs_factory):
    student, _faculty, link, _sub = _supervised(egs_factory)
    url = f"/api/wallet/join-requests/{link.pk}/spending-limit/"
    payload = {"spending_limit_enabled": True, "weekly_limit_inr": "10"}

    other_faculty = UserFactory(user_type=UserType.FACULTY, department=egs_factory.department)
    assert egs_factory.client_for(other_faculty).put(url, payload, format="json").status_code == 404
    assert egs_factory.client_for(other_faculty).get(url).status_code == 404
    assert egs_factory.client_for(student).put(url, payload, format="json").status_code == 403
    admin = UserFactory(user_type=UserType.ADMIN, department=egs_factory.department)
    assert egs_factory.client_for(admin).put(url, payload, format="json").status_code == 403
    assert egs_factory.client_for(student).get("/api/wallet/student-spending-limits/").status_code == 403

    link.refresh_from_db()
    assert link.spending_limit_enabled is False


@pytest.mark.django_db
def test_removed_student_cannot_be_limited(egs_factory):
    _student, faculty, link, _sub = _supervised(egs_factory)
    link.status = WalletJoinRequestStatus.CANCELLED
    link.save()
    resp = egs_factory.client_for(faculty).put(
        f"/api/wallet/join-requests/{link.pk}/spending-limit/",
        {"spending_limit_enabled": True, "weekly_limit_inr": "10"},
        format="json",
    )
    assert resp.status_code == 404


@pytest.mark.django_db
def test_student_sees_own_limit_and_remaining(egs_factory):
    student, faculty, link, _sub = _supervised(egs_factory)
    client = egs_factory.client_for(student)
    assert client.get("/api/wallet/my-spending-limit/").data == {"spending_limit_enabled": False}

    link.spending_limit_enabled = True
    link.weekly_limit_inr = Decimal("100.00")
    link.save()
    eq = egs_factory.equipment()
    _booking_at(egs_factory, student, eq, timezone.now(), "30.00")
    data = client.get("/api/wallet/my-spending-limit/").data
    assert data["spending_limit_enabled"] is True
    assert data["week_spent_inr"] == "30.00"
    assert data["weekly_remaining_inr"] == "70.00"
    assert data["monthly_remaining_inr"] is None
    assert faculty.name and not faculty.name.startswith(("Prof", "Dr"))
    assert data["supervisor_name"] == f"Prof. {faculty.name}"


# --- enforcement on charge paths -------------------------------------------------------------


def _edit_setup(egs_factory, **limits):
    # HOUR profile, ₹10/hour, A hours: A=2 costs ₹20, A=5 costs ₹50.
    eq = egs_factory.equipment(time_formula="A*60")
    DynamicInputField.objects.create(
        equipment=eq,
        field_key="A",
        field_label="No. of Samples",
        field_type=DynamicInputFieldType.NUMERIC,
        options={"min": 1, "max": 10},
        editing_required=False,
    )
    student, _faculty, _link, sub = _supervised(egs_factory, **limits)
    booking = egs_factory.booking(student, eq, egs_factory.future(), input_values={"A": 2}, total_charge="20.00")
    booking.total_time_minutes = 120
    booking.save(update_fields=["total_time_minutes"])
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return student, oic, booking, sub


@pytest.mark.django_db
def test_input_edit_extra_is_blocked_over_the_limit(egs_factory):
    student, _oic, booking, sub = _edit_setup(egs_factory, spending_limit_enabled=True, weekly_limit_inr=40)
    client = egs_factory.client_for(student)
    assert client.patch(
        f"/api/bookings/{booking.pk}/input-values/", {"input_values": {"A": 5}}, format="json"
    ).status_code == 200

    resp = client.post(f"/api/bookings/{booking.pk}/process-charge-recalculation-pay-now/", {}, format="json")
    assert resp.status_code == 400
    assert resp.data["code"] == SPENDING_LIMIT_ERROR_CODE
    assert resp.data["error"] == (
        "This additional charge exceeds the weekly spending limit (₹40.00) set by your supervisor. "
        "Remaining this week: ₹20.00."
    )
    sub.refresh_from_db()
    assert sub.balance == Decimal("10000.00")


@pytest.mark.django_db
def test_input_edit_extra_within_the_limit_is_paid(egs_factory):
    student, _oic, booking, sub = _edit_setup(egs_factory, spending_limit_enabled=True, weekly_limit_inr=50)
    client = egs_factory.client_for(student)
    assert client.patch(
        f"/api/bookings/{booking.pk}/input-values/", {"input_values": {"A": 5}}, format="json"
    ).status_code == 200
    resp = client.post(f"/api/bookings/{booking.pk}/process-charge-recalculation-pay-now/", {}, format="json")
    assert resp.status_code == 200, resp.data
    sub.refresh_from_db()
    assert sub.balance == Decimal("9970.00")


@pytest.mark.django_db
def test_oic_paying_the_extra_is_not_blocked(egs_factory):
    student, oic, booking, sub = _edit_setup(egs_factory, spending_limit_enabled=True, weekly_limit_inr=20)
    assert egs_factory.client_for(student).patch(
        f"/api/bookings/{booking.pk}/input-values/", {"input_values": {"A": 5}}, format="json"
    ).status_code == 200
    resp = egs_factory.client_for(oic).post(
        f"/api/bookings/{booking.pk}/process-charge-recalculation-pay-now/", {}, format="json"
    )
    assert resp.status_code == 200, resp.data
    sub.refresh_from_db()
    assert sub.balance == Decimal("9970.00")


@pytest.mark.django_db
def test_waitlist_auto_booking_respects_limit_but_staff_confirmation_does_not(egs_factory, monkeypatch, no_portal_lock):
    from iic_booking.equipment import waitlist_booking

    monkeypatch.setattr(waitlist_booking, "create_booking_event", lambda **kwargs: None)
    student, _faculty, _link, sub = _supervised(egs_factory, spending_limit_enabled=True, weekly_limit_inr=5)
    eq = egs_factory.equipment()
    slot = egs_factory.slot(eq, egs_factory.future(days=2))
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)

    booking, err = waitlist_booking.create_booking_for_waitlist_user(eq, student, [slot.pk])
    assert booking is None
    assert err.startswith("This booking exceeds the weekly spending limit (₹5.00)")

    booking, err = waitlist_booking.create_booking_for_waitlist_user(
        eq, student, [slot.pk], created_by=oic, staff_override=True
    )
    assert err is None, err
    assert booking.total_charge == Decimal("10.00")


@pytest.mark.django_db
def test_book_endpoint_rejects_over_limit_and_admin_on_behalf_bypasses(egs_factory, monkeypatch, no_portal_lock):
    from iic_booking.equipment import api_views

    student, _faculty, link, sub = _supervised(egs_factory, spending_limit_enabled=True, weekly_limit_inr=5)
    eq = egs_factory.equipment()
    slot = egs_factory.slot(eq, egs_factory.future(days=2))
    body = {
        "slot_ids": [slot.pk],
        "start_time": slot.start_datetime.isoformat(),
        "end_time": slot.end_datetime.isoformat(),
        "input_values": {},
    }

    resp = egs_factory.client_for(student).post(f"/api/equipments/{eq.pk}/book/", body, format="json")
    assert resp.status_code == 400, resp.data
    assert resp.data.get("code") == SPENDING_LIMIT_ERROR_CODE, resp.data
    assert "weekly spending limit (₹5.00)" in resp.data["error"]
    assert not Booking.objects.filter(user=student).exists()

    link.weekly_limit_inr = Decimal("10.00")
    link.save()
    resp = egs_factory.client_for(student).post(f"/api/equipments/{eq.pk}/book/", body, format="json")
    assert resp.status_code in (200, 201), resp.data
    assert limit_summary(link)["weekly_remaining_inr"] == "0.00"

    second = egs_factory.slot(eq, egs_factory.future(days=2, hour=13))
    admin = UserFactory(user_type=UserType.ADMIN, department=egs_factory.department, admin_approved=True)
    monkeypatch.setattr(api_views, "_actor_may_book_on_behalf", lambda actor, equipment: None)
    resp = egs_factory.client_for(admin).post(
        f"/api/equipments/{eq.pk}/book/",
        {**body, "slot_ids": [second.pk], "start_time": second.start_datetime.isoformat(),
         "end_time": second.end_datetime.isoformat(), "user_id": student.pk},
        format="json",
    )
    assert resp.status_code in (200, 201), resp.data
    assert Booking.objects.filter(user=student).count() == 2
