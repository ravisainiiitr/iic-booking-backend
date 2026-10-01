"""Per-user weekly / monthly slot limits saved on Equipment (internal_/external_ weekly_/monthly_quota)."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from iic_booking.equipment.equipment_slot_quota import (
    SLOT_LIMIT_ERROR_CODE,
    slot_limit_error,
    slots_used,
    usage_summary,
)
from iic_booking.equipment.models import Booking, BookingStatus, QuotaType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.repositories.wallet_repository import SubWalletRepository
from iic_booking.users.student_spending_limits import IST
from iic_booking.users.tests.factories import UserFactory

# Wednesday 3 Mar 2027 10:00 IST: week Mon 1 - Sun 7 Mar, month March.
WED = datetime(2027, 3, 3, 10, 0, tzinfo=IST)
NEXT_WEEK = WED + timedelta(days=7)


@pytest.fixture
def enforce(settings):
    settings.ENFORCE_EQUIPMENT_SLOT_QUOTA = True
    settings.SKIP_BOOKING_QUOTA_CHECK = False
    return settings


@pytest.fixture
def no_portal_lock(monkeypatch):
    from iic_booking.users.legacy_ledger import booking_lock

    monkeypatch.setattr(booking_lock, "booking_is_locked", lambda user: (False, ""))
    monkeypatch.setattr(booking_lock, "department_equipment_booking_blocked", lambda equipment, user: (False, ""))


def _limited(egs_factory, weekly=3, monthly=0, **kwargs):
    return egs_factory.equipment(
        name="FE-SEM APREO",
        internal_weekly_quota=weekly,
        internal_monthly_quota=monthly,
        external_weekly_quota=weekly,
        external_monthly_quota=monthly,
        **kwargs,
    )


def _student_with_wallet(egs_factory):
    student = egs_factory.student()
    faculty = UserFactory(user_type=UserType.FACULTY, department=egs_factory.department)
    wallet = Wallet.objects.create(user=faculty)
    WalletJoinRequest.objects.create(
        student=student,
        faculty=faculty,
        wallet=wallet,
        status=WalletJoinRequestStatus.APPROVED,
        responded_at=datetime(2026, 1, 1, tzinfo=IST),
    )
    SubWalletRepository.get_or_create(wallet, egs_factory.department).credit(Decimal("10000.00"), description="Recharge")
    return student


def _book_body(slots):
    return {
        "slot_ids": [s.pk for s in slots],
        "start_time": slots[0].start_datetime.isoformat(),
        "end_time": slots[-1].end_datetime.isoformat(),
        "input_values": {},
    }


# --- switches, exemptions, unlimited ------------------------------------------------------------


@pytest.mark.django_db
def test_flag_off_never_blocks(egs_factory, settings):
    settings.ENFORCE_EQUIPMENT_SLOT_QUOTA = False
    eq = _limited(egs_factory, weekly=1)
    user = egs_factory.student()
    egs_factory.booking(user, eq, WED, slot_count=3)
    assert slot_limit_error(user, eq, slots_requested=2, reference=WED) is None


@pytest.mark.django_db
@pytest.mark.parametrize("value", [0, -1])
def test_zero_or_negative_means_unlimited(egs_factory, enforce, value):
    eq = _limited(egs_factory, weekly=value, monthly=value)
    user = egs_factory.student()
    egs_factory.booking(user, eq, WED, slot_count=5)
    assert slot_limit_error(user, eq, slots_requested=20, reference=WED) is None
    assert usage_summary(eq, user, WED) == []


@pytest.mark.django_db
def test_skip_switches_disable_the_check(egs_factory, enforce):
    user = egs_factory.student()
    skipped = _limited(egs_factory, weekly=1, skip_quota_check=True)
    egs_factory.booking(user, skipped, WED, slot_count=1)
    assert slot_limit_error(user, skipped, slots_requested=1, reference=WED) is None

    eq = _limited(egs_factory, weekly=1)
    egs_factory.booking(user, eq, WED, slot_count=1)
    enforce.SKIP_BOOKING_QUOTA_CHECK = True
    assert slot_limit_error(user, eq, slots_requested=1, reference=WED) is None


@pytest.mark.django_db
@pytest.mark.parametrize("role", [UserType.ADMIN, UserType.MANAGER, UserType.OPERATOR, UserType.DEPT_ADMIN])
def test_staff_actor_and_urgent_hold_are_exempt(egs_factory, enforce, role):
    eq = _limited(egs_factory, weekly=1)
    user = egs_factory.student()
    egs_factory.booking(user, eq, WED, slot_count=1)
    staff = UserFactory(user_type=role, department=egs_factory.department)
    assert slot_limit_error(user, eq, slots_requested=1, reference=WED, actor=staff) is None
    assert slot_limit_error(user, eq, slots_requested=1, reference=WED, bypass=True) is None
    assert slot_limit_error(user, eq, slots_requested=1, reference=WED) is not None


@pytest.mark.django_db
def test_external_users_use_the_external_columns(egs_factory, enforce):
    eq = egs_factory.equipment(internal_weekly_quota=0, internal_monthly_quota=0, external_weekly_quota=1,
                               external_monthly_quota=0)
    external = UserFactory(user_type=UserType.EXTERNAL)
    internal = egs_factory.student()
    egs_factory.booking(external, eq, WED, slot_count=1)
    egs_factory.booking(internal, eq, WED + timedelta(hours=3), slot_count=1)
    assert "Weekly slot limit" in slot_limit_error(external, eq, slots_requested=1, reference=WED)
    assert slot_limit_error(internal, eq, slots_requested=5, reference=WED) is None


# --- counting -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_within_the_weekly_limit_is_allowed(egs_factory, enforce):
    eq = _limited(egs_factory, weekly=3)
    user = egs_factory.student()
    egs_factory.booking(user, eq, WED, slot_count=2)
    assert slot_limit_error(user, eq, slots_requested=1, reference=WED) is None


@pytest.mark.django_db
def test_exceeding_the_weekly_limit_is_blocked_with_a_clear_message(egs_factory, enforce):
    eq = _limited(egs_factory, weekly=3)
    user = egs_factory.student()
    egs_factory.booking(user, eq, WED, slot_count=2)
    err = slot_limit_error(user, eq, slots_requested=2, reference=WED + timedelta(days=2))
    assert err == (
        "Weekly slot limit for FE-SEM APREO reached: you have booked 2 of 3 slots in the week of "
        "1 Mar 2027; this booking needs 2."
    )
    # Next week is a fresh period.
    assert slot_limit_error(user, eq, slots_requested=3, reference=NEXT_WEEK) is None


@pytest.mark.django_db
def test_exceeding_the_monthly_limit_is_blocked_when_each_week_is_fine(egs_factory, enforce):
    eq = _limited(egs_factory, weekly=3, monthly=4)
    user = egs_factory.student()
    egs_factory.booking(user, eq, WED, slot_count=2)
    egs_factory.booking(user, eq, NEXT_WEEK, slot_count=2)
    err = slot_limit_error(user, eq, slots_requested=1, reference=NEXT_WEEK + timedelta(days=7))
    assert err == (
        "Monthly slot limit for FE-SEM APREO reached: you have booked 4 of 4 slots in March 2027; "
        "this booking needs 1."
    )
    assert slot_limit_error(user, eq, slots_requested=3, reference=datetime(2027, 4, 7, 10, tzinfo=IST)) is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status",
    [
        BookingStatus.CANCELLED,
        BookingStatus.REFUNDED,
        BookingStatus.WAITLISTED,
        BookingStatus.HOLD,
        BookingStatus.ABSENT,
        BookingStatus.DISRUPTION_PENDING,
    ],
)
def test_inactive_bookings_do_not_count(egs_factory, enforce, status):
    eq = _limited(egs_factory, weekly=2)
    user = egs_factory.student()
    booking = egs_factory.booking(user, eq, WED, slot_count=2)
    Booking.objects.filter(pk=booking.pk).update(status=status)
    assert slots_used(user, eq, QuotaType.WEEKLY, WED) == 0
    assert slot_limit_error(user, eq, slots_requested=2, reference=WED) is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "status",
    [BookingStatus.BOOKED, BookingStatus.PROCESSING, BookingStatus.COMPLETED, BookingStatus.PENDING_PAYMENT],
)
def test_active_bookings_count(egs_factory, enforce, status):
    eq = _limited(egs_factory, weekly=2)
    user = egs_factory.student()
    booking = egs_factory.booking(user, eq, WED, slot_count=2)
    Booking.objects.filter(pk=booking.pk).update(status=status)
    assert slots_used(user, eq, QuotaType.WEEKLY, WED) == 2


@pytest.mark.django_db
def test_every_sample_sets_slot_counts(egs_factory, enforce):
    eq = _limited(egs_factory, weekly=4)
    user = egs_factory.student()
    egs_factory.booking(
        user, eq, WED, slot_count=3,
        input_values={"A": 1, "_sample_sets": [{"A": 1}, {"A": 2}]},
    )
    assert slots_used(user, eq, QuotaType.WEEKLY, WED) == 3
    assert "booked 3 of 4" in slot_limit_error(user, eq, slots_requested=2, reference=WED)


@pytest.mark.django_db
def test_repeat_samples_other_users_and_other_equipment_do_not_count(egs_factory, enforce):
    eq = _limited(egs_factory, weekly=2)
    other_eq = _limited(egs_factory, weekly=2)
    user = egs_factory.student()
    source = egs_factory.booking(user, eq, WED - timedelta(days=1), slot_count=1)
    Booking.objects.filter(pk=source.pk).update(status=BookingStatus.COMPLETED)
    egs_factory.booking(user, eq, WED, slot_count=2, source_booking=source)
    egs_factory.booking(egs_factory.student(), eq, WED + timedelta(hours=3), slot_count=2)
    egs_factory.booking(user, other_eq, WED, slot_count=2)
    assert slots_used(user, eq, QuotaType.WEEKLY, WED) == 1


# --- book endpoint ------------------------------------------------------------------------------


@pytest.mark.django_db
def test_book_endpoint_blocks_over_limit_and_allows_within(egs_factory, enforce, no_portal_lock):
    eq = _limited(egs_factory, weekly=3)
    student = _student_with_wallet(egs_factory)
    day = egs_factory.future(days=2, hour=8)
    egs_factory.booking(student, eq, day, slot_count=2)
    pair = [egs_factory.slot(eq, day + timedelta(hours=4)), egs_factory.slot(eq, day + timedelta(hours=5))]

    resp = egs_factory.client_for(student).post(f"/api/equipments/{eq.pk}/book/", _book_body(pair), format="json")
    assert resp.status_code == 400, resp.data
    assert resp.data["code"] == SLOT_LIMIT_ERROR_CODE
    assert "you have booked 2 of 3 slots" in resp.data["error"]
    assert resp.data["error"].endswith("this booking needs 2.")
    assert Booking.objects.filter(user=student).count() == 1

    resp = egs_factory.client_for(student).post(f"/api/equipments/{eq.pk}/book/", _book_body(pair[:1]), format="json")
    assert resp.status_code in (200, 201), resp.data
    assert Booking.objects.filter(user=student).count() == 2


@pytest.mark.django_db
def test_locked_recheck_inside_the_booking_transaction_also_blocks(egs_factory, enforce, no_portal_lock, monkeypatch):
    from iic_booking.equipment import api_views

    monkeypatch.setattr(api_views, "_equipment_slot_limit_response", lambda *a, **k: None)
    eq = _limited(egs_factory, weekly=1)
    student = _student_with_wallet(egs_factory)
    day = egs_factory.future(days=2, hour=8)
    egs_factory.booking(student, eq, day, slot_count=1)
    slot = egs_factory.slot(eq, day + timedelta(hours=4))
    resp = egs_factory.client_for(student).post(f"/api/equipments/{eq.pk}/book/", _book_body([slot]), format="json")
    assert resp.status_code == 400, resp.data
    assert resp.data["error"].startswith("Weekly slot limit for FE-SEM APREO reached")
    slot.refresh_from_db()
    assert slot.booking_id is None


@pytest.mark.django_db
def test_admin_booking_on_behalf_is_exempt(egs_factory, enforce, no_portal_lock, monkeypatch):
    from iic_booking.equipment import api_views

    eq = _limited(egs_factory, weekly=1)
    student = _student_with_wallet(egs_factory)
    day = egs_factory.future(days=2, hour=8)
    egs_factory.booking(student, eq, day, slot_count=1)
    slot = egs_factory.slot(eq, day + timedelta(hours=4))
    admin = UserFactory(user_type=UserType.ADMIN, department=egs_factory.department, admin_approved=True)
    monkeypatch.setattr(api_views, "_actor_may_book_on_behalf", lambda actor, equipment: None)

    resp = egs_factory.client_for(admin).post(
        f"/api/equipments/{eq.pk}/book/", {**_book_body([slot]), "user_id": student.pk}, format="json"
    )
    assert resp.status_code in (200, 201), resp.data
    assert Booking.objects.filter(user=student).count() == 2


@pytest.mark.django_db
def test_flag_off_book_endpoint_unchanged(egs_factory, settings, no_portal_lock):
    settings.ENFORCE_EQUIPMENT_SLOT_QUOTA = False
    eq = _limited(egs_factory, weekly=1)
    student = _student_with_wallet(egs_factory)
    day = egs_factory.future(days=2, hour=8)
    egs_factory.booking(student, eq, day, slot_count=1)
    slot = egs_factory.slot(eq, day + timedelta(hours=4))
    resp = egs_factory.client_for(student).post(f"/api/equipments/{eq.pk}/book/", _book_body([slot]), format="json")
    assert resp.status_code in (200, 201), resp.data


# --- waitlist -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_waitlist_auto_booking_respects_limit_but_staff_confirmation_does_not(
    egs_factory, enforce, no_portal_lock, monkeypatch
):
    from iic_booking.equipment import waitlist_booking

    monkeypatch.setattr(waitlist_booking, "create_booking_event", lambda **kwargs: None)
    eq = _limited(egs_factory, weekly=1)
    student = _student_with_wallet(egs_factory)
    day = egs_factory.future(days=2, hour=8)
    egs_factory.booking(student, eq, day, slot_count=1)
    slot = egs_factory.slot(eq, day + timedelta(hours=4))
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)

    booking, err = waitlist_booking.create_booking_for_waitlist_user(eq, student, [slot.pk])
    assert booking is None
    assert err.startswith("Weekly slot limit for FE-SEM APREO reached: you have booked 1 of 1 slots")

    booking, err = waitlist_booking.create_booking_for_waitlist_user(
        eq, student, [slot.pk], created_by=oic, staff_override=True
    )
    assert err is None, err


# --- reschedule ---------------------------------------------------------------------------------


@pytest.mark.django_db
def test_reschedule_into_a_full_week_is_blocked(egs_factory, enforce, egs_quiet_side_effects):
    eq = _limited(egs_factory, weekly=2)
    owner = egs_factory.student()
    moving = egs_factory.booking(owner, eq, egs_factory.future(days=5, hour=8))
    egs_factory.booking(owner, eq, egs_factory.future(days=12, hour=8), slot_count=2)
    target = egs_factory.slot(eq, egs_factory.future(days=12, hour=14))

    res = egs_factory.client_for(owner).post(
        f"/api/bookings/{moving.pk}/user-reschedule/",
        {"start_time": target.start_datetime.isoformat(), "end_time": target.end_datetime.isoformat()},
        format="json",
    )
    assert res.status_code == 400, res.data
    assert res.data["code"] == SLOT_LIMIT_ERROR_CODE
    assert "you have booked 2 of 2 slots" in res.data["error"]
    assert res.data["error"].endswith("this reschedule needs 1.")
    target.refresh_from_db()
    assert target.booking_id is None
    assert moving.daily_slots.count() == 1

    same_week = egs_factory.slot(eq, egs_factory.future(days=5, hour=14))
    ok = egs_factory.client_for(owner).post(
        f"/api/bookings/{moving.pk}/user-reschedule/",
        {"start_time": same_week.start_datetime.isoformat(), "end_time": same_week.end_datetime.isoformat()},
        format="json",
    )
    assert ok.status_code == 200, ok.data


@pytest.mark.django_db
def test_reschedule_that_increases_slots_is_blocked(egs_factory, enforce, egs_quiet_side_effects):
    eq = _limited(egs_factory, weekly=3)
    owner = egs_factory.student()
    day = egs_factory.future(days=5, hour=8)
    egs_factory.booking(owner, eq, day, slot_count=1)
    moving = egs_factory.booking(owner, eq, day + timedelta(hours=2), slot_count=2)
    longer = [egs_factory.slot(eq, day + timedelta(hours=h)) for h in (6, 7, 8)]

    res = egs_factory.client_for(owner).post(
        f"/api/bookings/{moving.pk}/user-reschedule/",
        {"start_time": longer[0].start_datetime.isoformat(), "end_time": longer[-1].end_datetime.isoformat()},
        format="json",
    )
    assert res.status_code == 400, res.data
    assert "you have booked 1 of 3 slots" in res.data["error"]
    assert res.data["error"].endswith("this reschedule needs 3.")
    assert moving.daily_slots.count() == 2

    ok = egs_factory.client_for(owner).post(
        f"/api/bookings/{moving.pk}/user-reschedule/",
        {"start_time": longer[0].start_datetime.isoformat(), "end_time": longer[1].end_datetime.isoformat()},
        format="json",
    )
    assert ok.status_code == 200, ok.data


@pytest.mark.django_db
def test_booking_already_over_a_new_limit_can_still_move_within_its_period(egs_factory, enforce):
    eq = _limited(egs_factory, weekly=2)
    user = egs_factory.student()
    egs_factory.booking(user, eq, WED, slot_count=2)
    moving = egs_factory.booking(user, eq, WED + timedelta(days=1), slot_count=1)
    assert slot_limit_error(
        user, eq, slots_requested=1, reference=WED + timedelta(days=2), exclude_booking_id=moving.pk,
        action="reschedule",
    ) is None
    assert slot_limit_error(
        user, eq, slots_requested=2, reference=WED + timedelta(days=2), exclude_booking_id=moving.pk,
        action="reschedule",
    ) is not None


@pytest.mark.django_db
def test_staff_reschedule_for_the_user_is_exempt(egs_factory, enforce, egs_quiet_side_effects, monkeypatch):
    from iic_booking.equipment import api_views

    monkeypatch.setattr(api_views, "_oic_booking_scope_denied", lambda user, booking: None)
    eq = _limited(egs_factory, weekly=1)
    owner = egs_factory.student()
    moving = egs_factory.booking(owner, eq, egs_factory.future(days=5, hour=8))
    egs_factory.booking(owner, eq, egs_factory.future(days=12, hour=8))
    target = egs_factory.slot(eq, egs_factory.future(days=12, hour=14))
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)

    res = egs_factory.client_for(oic).post(
        f"/api/bookings/{moving.pk}/user-reschedule/",
        {"start_time": target.start_datetime.isoformat(), "end_time": target.end_datetime.isoformat()},
        format="json",
    )
    assert res.status_code == 200, res.data


# --- display endpoint and assistant preflight ---------------------------------------------------


@pytest.mark.django_db
def test_slot_limits_endpoint_reports_remaining_only_when_enforced(egs_factory, settings):
    eq = _limited(egs_factory, weekly=10, monthly=0)
    user = egs_factory.student()
    egs_factory.booking(user, eq, WED, slot_count=3)
    client = egs_factory.client_for(user)

    settings.ENFORCE_EQUIPMENT_SLOT_QUOTA = False
    off = client.get(f"/api/equipments/{eq.pk}/slot-limits/?date=2027-03-04")
    assert off.status_code == 200
    assert off.data["limits"] == []

    settings.ENFORCE_EQUIPMENT_SLOT_QUOTA = True
    on = client.get(f"/api/equipments/{eq.pk}/slot-limits/?date=2027-03-04")
    assert on.status_code == 200
    assert [(r["period"], r["limit"], r["used"], r["remaining"]) for r in on.data["limits"]] == [("weekly", 10, 3, 7)]
    assert timezone.localtime(datetime.fromisoformat(on.data["limits"][0]["period_start"])).date().isoformat() == "2027-03-01"

    bad = client.get(f"/api/equipments/{eq.pk}/slot-limits/?date=soon")
    assert bad.status_code == 400


@pytest.mark.django_db
def test_assistant_preflight_reports_the_slot_limit(egs_factory, enforce):
    from iic_booking.research_copilot.services.assistant import preflight

    eq = _limited(egs_factory, weekly=1)
    user = egs_factory.student()
    egs_factory.booking(user, eq, WED, slot_count=1)
    slot = egs_factory.slot(eq, WED + timedelta(hours=3))
    assert preflight._slot_limit_error(user, eq, [slot]).startswith("Weekly slot limit for FE-SEM APREO reached")
