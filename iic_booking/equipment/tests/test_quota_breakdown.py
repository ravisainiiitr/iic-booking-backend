"""Bookings counted toward a weekly / monthly limit: totals equal enforcement, exclusions follow the
accounting rules, IST period edges, who may see what, external users, the structured quota failure
payload, and the Booking Attempt Log breakdown for the requested slot's period."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.utils import timezone

from iic_booking.equipment.models import (
    Booking,
    BookingAttemptLog,
    BookingAttemptOutcome,
    BookingStatus,
    DynamicInputField,
    DynamicInputFieldType,
    EquipmentGroupQuota,
    EquipmentManager,
    ExternalUserQuota,
    QuotaLimitType,
    QuotaType,
)
from iic_booking.equipment.quota_breakdown import resolve_dimension, supervisor_of
from iic_booking.equipment.quota_utils import QuotaService
from iic_booking.users.models.user import User
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

URL = "/api/bookings/quota-breakdown/"
WEEKLY_INDIVIDUAL = 120
WEEKLY_FACULTY = 300


@pytest.fixture(autouse=True)
def _fresh_cache():
    cache.clear()
    yield
    cache.clear()


def _monday(weeks_ahead: int):
    today = timezone.localdate()
    return today + timedelta(days=7 * weeks_ahead - today.weekday())


def _at(day, hour, minute=0):
    return timezone.make_aware(datetime.combine(day, time(hour, minute)), timezone.get_current_timezone())


@pytest.fixture
def world(egs_factory, settings):
    settings.SKIP_BOOKING_QUOTA_CHECK = False
    group = egs_factory.group()
    eq = egs_factory.equipment(group, time_formula="A*30")
    eq2 = egs_factory.equipment(group, time_formula="A*30")
    for quota_type, individual, faculty in (
        (QuotaType.WEEKLY, WEEKLY_INDIVIDUAL, WEEKLY_FACULTY),
        (QuotaType.MONTHLY, 2000, 5000),
    ):
        EquipmentGroupQuota.objects.create(
            equipment_group=group,
            quota_type=quota_type,
            internal_individual_quota_minutes=individual,
            internal_faculty_quota_minutes=faculty,
            external_individual_quota_minutes=individual,
            external_faculty_quota_minutes=faculty,
            is_enforced=True,
        )
    DynamicInputField.objects.create(
        equipment=eq,
        field_key="A",
        field_label="No. of Samples",
        field_type=DynamicInputFieldType.NUMERIC,
        options={"min": 1, "max": 10},
        editing_required=True,
    )
    faculty = UserFactory(user_type=UserType.FACULTY, department=egs_factory.department)
    wallet = Wallet.objects.create(user=faculty)
    student = egs_factory.student()
    peer = egs_factory.student()
    for s in (student, peer):
        WalletJoinRequest.objects.create(student=s, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED)
    outsider = egs_factory.student()
    oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    other_oic = UserFactory(user_type=UserType.MANAGER, department=egs_factory.department, admin_approved=True)
    EquipmentManager.objects.create(equipment=egs_factory.equipment(None), manager=other_oic)
    admin = UserFactory(user_type=UserType.ADMIN)
    return SimpleNamespace(
        f=egs_factory, group=group, eq=eq, eq2=eq2, faculty=faculty, student=student, peer=peer,
        outsider=outsider, oic=oic, other_oic=other_oic, admin=admin,
    )


def _book(w, user, day, hour=10, *, eq=None, minutes=60, slot_count=1, status=BookingStatus.BOOKED,
          user_type_snapshot=UserType.STUDENT, **fields):
    booking = w.f.booking(user, eq or w.eq, _at(day, hour), slot_count=slot_count, **fields)
    Booking.objects.filter(pk=booking.pk).update(
        total_time_minutes=minutes, status=status, user_type_snapshot=user_type_snapshot
    )
    booking.refresh_from_db()
    return booking


def _get(w, viewer, **params):
    return w.f.client_for(viewer).get(URL, params)


def _enforced_used(user, equipment, quota_type, scope, at):
    dim = resolve_dimension(user, equipment, quota_type, scope)
    return QuotaService.evaluate_dimension(dim, booking_date=at).used_minutes


# --- totals equal enforcement -------------------------------------------------------------------


def test_individual_totals_equal_enforcement_usage(world):
    w = world
    week = _monday(2)
    a = _book(w, w.student, week, 9)
    b = _book(w, w.student, week + timedelta(days=2), 14, eq=w.eq2, minutes=30)
    _book(w, w.peer, week + timedelta(days=1), 11)

    res = _get(w, w.student, equipment=w.eq.pk, period="week", scope="individual", date=week.isoformat(), requested=45)

    assert res.status_code == 200, res.data
    assert res.data["used_minutes"] == 90 == _enforced_used(w.student, w.eq, QuotaType.WEEKLY, "individual", _at(week, 12))
    assert [r["booking_id"] for r in res.data["counted"]] == [a.pk, b.pk]
    assert res.data["limit_minutes"] == WEEKLY_INDIVIDUAL
    assert res.data["requested_minutes"] == 45
    assert res.data["remaining_minutes"] == 30
    assert res.data["over_by_minutes"] == 15
    assert res.data["scope_label"] == "Individual Weekly"
    assert res.data["period_label"].startswith(f"Week of Mon {week.day} ")
    assert all(r["can_open"] for r in res.data["counted"])


def test_group_totals_equal_faculty_usage_with_member_subtotals(world):
    w = world
    week = _monday(2)
    _book(w, w.faculty, week, 9, minutes=60)
    _book(w, w.student, week + timedelta(days=1), 9, minutes=60)
    _book(w, w.student, week + timedelta(days=2), 9, eq=w.eq2, minutes=30)
    _book(w, w.peer, week + timedelta(days=3), 9, minutes=60)
    _book(w, w.outsider, week + timedelta(days=3), 12, minutes=60)

    res = _get(w, w.faculty, equipment=w.eq.pk, period="week", scope="group", date=week.isoformat())

    assert res.status_code == 200, res.data
    assert res.data["used_minutes"] == 210 == _enforced_used(w.student, w.eq, QuotaType.WEEKLY, "group", _at(week, 12))
    assert res.data["scope_label"] == "Faculty Weekly"
    assert res.data["group_members_count"] == 3
    subtotals = {m["user_id"]: (m["minutes"], m["bookings"]) for m in res.data["members"]}
    assert subtotals == {w.student.pk: (90, 2), w.faculty.pk: (60, 1), w.peer.pk: (60, 1)}
    assert res.data["full_details"] is True
    assert all(r["can_open"] and r["booking_id"] for r in res.data["counted"])


def test_breakdown_matches_the_enforcement_failure_message(world):
    w = world
    week = _monday(2)
    _book(w, w.student, week, 9, minutes=60)
    _book(w, w.student, week + timedelta(days=1), 9, minutes=30)

    decision = QuotaService.evaluate_booking_quota(
        w.student, w.eq, additional_time_minutes=60, booking_date=_at(week + timedelta(days=3), 10)
    )
    res = _get(w, w.student, equipment=w.eq.pk, period="week", scope="individual", date=week.isoformat())

    assert not decision.allowed
    assert decision.failure.scope_kind == "individual"
    assert decision.failure.used_minutes == res.data["used_minutes"] == 90
    assert decision.failure.limit_minutes == res.data["limit_minutes"]


# --- exclusions -------------------------------------------------------------------------------


def test_exclusions_follow_the_accounting_rules(world):
    w = world
    week = _monday(2)
    counted = _book(w, w.student, week, 9)
    original = _book(w, w.student, week - timedelta(days=7), 9, status=BookingStatus.COMPLETED)
    repeat = _book(w, w.student, week + timedelta(days=1), 9, source_booking=original)
    cancelled = _book(w, w.student, week + timedelta(days=2), 9, status=BookingStatus.CANCELLED)
    hold = _book(w, w.student, week + timedelta(days=3), 9, status=BookingStatus.HOLD)
    refunded = _book(w, w.student, week + timedelta(days=4), 9, status=BookingStatus.REFUNDED)
    no_show = _book(w, w.student, week + timedelta(days=4), 12, status=BookingStatus.BOOKING_NOT_UTILIZED)
    moved_here = _book(w, w.student, week + timedelta(days=5), 9)
    Booking.objects.filter(pk=moved_here.pk).update(quota_period_anchor_at=_at(week - timedelta(days=5), 10))

    res = _get(w, w.student, equipment=w.eq.pk, period="week", scope="individual", date=week.isoformat())

    assert res.status_code == 200, res.data
    assert {r["booking_id"] for r in res.data["counted"]} == {counted.pk, no_show.pk}
    assert res.data["used_minutes"] == 120
    reasons = {r["booking_id"]: r["note"] for r in res.data["not_counted"]}
    assert reasons[repeat.pk].startswith("Repeat sample")
    assert reasons[cancelled.pk].startswith("Cancelled")
    assert reasons[hold.pk].startswith("Urgent request on hold")
    assert reasons[refunded.pk].startswith("Refunded")
    assert reasons[moved_here.pk].startswith("Moved by a disruption or staff reschedule – counted in the week of")
    assert all(not r["counted"] for r in res.data["not_counted"])

    previous = _get(w, w.student, equipment=w.eq.pk, period="week", scope="individual",
                    date=(week - timedelta(days=7)).isoformat())
    moved_row = next(r for r in previous.data["counted"] if r["booking_id"] == moved_here.pk)
    assert moved_row["note"].startswith("Moved by a disruption or staff reschedule – still counted here")


# --- IST period edges ----------------------------------------------------------------------------


def test_week_and_month_edges_are_in_ist(world):
    w = world
    sunday = _monday(2) - timedelta(days=1)
    late_sunday = _book(w, w.student, sunday, 23, minutes=30)
    _book(w, w.student, _monday(2), 0, minutes=45)

    this_week = _get(w, w.student, equipment=w.eq.pk, period="week", scope="individual", date=sunday.isoformat())
    next_week = _get(w, w.student, equipment=w.eq.pk, period="week", scope="individual", date=_monday(2).isoformat())

    assert [r["booking_id"] for r in this_week.data["counted"]] == [late_sunday.pk]
    assert this_week.data["used_minutes"] == 30
    assert next_week.data["used_minutes"] == 45
    assert this_week.data["period_end"].endswith("+05:30")

    month_start = (timezone.localdate().replace(day=1) + timedelta(days=40)).replace(day=1)
    last_day = month_start - timedelta(days=1)
    _book(w, w.peer, last_day, 23, minutes=30)
    _book(w, w.peer, month_start, 0, minutes=60)
    prev_month = _get(w, w.peer, equipment=w.eq.pk, period="month", scope="individual", date=last_day.isoformat())
    new_month = _get(w, w.peer, equipment=w.eq.pk, period="month", scope="individual", date=month_start.isoformat())
    assert prev_month.data["used_minutes"] == 30
    assert new_month.data["used_minutes"] == 60
    assert new_month.data["period_label"] == month_start.strftime("%B %Y")


# --- who sees what -----------------------------------------------------------------------------


def test_student_sees_group_members_by_name_without_links(world):
    w = world
    week = _monday(2)
    own = _book(w, w.student, week, 9)
    prof = _book(w, w.faculty, week + timedelta(days=1), 9)
    _book(w, w.peer, week + timedelta(days=2), 9, status=BookingStatus.CANCELLED)

    res = _get(w, w.student, equipment=w.eq.pk, period="week", scope="group", date=week.isoformat())

    assert res.status_code == 200, res.data
    own_row = next(r for r in res.data["counted"] if r["is_viewer"])
    assert own_row["booking_id"] == own.pk and own_row["can_open"] is True
    prof_row = next(r for r in res.data["counted"] if r["user_id"] == w.faculty.pk)
    assert prof_row["user_name"] and prof_row["user_name"] != "Another user"
    assert prof_row["booking_id"] is None and prof_row["can_open"] is False
    assert prof_row["display_booking_id"] == prof.virtual_booking_id
    assert res.data["used_minutes"] == 120
    assert res.data["full_details"] is False
    assert res.data["not_counted"] == []
    assert "total_charge" not in prof_row and "input_values" not in prof_row


def test_supervisor_in_full_for_staff_and_by_name_for_the_student(world):
    w = world
    week = _monday(2)
    _book(w, w.student, week, 9)
    _book(w, w.peer, week + timedelta(days=1), 9)
    params = dict(equipment=w.eq.pk, period="week", scope="group", date=week.isoformat())

    staff = _get(w, w.oic, user_id=w.student.pk, **params).data
    assert staff["supervisor"]["id"] == w.faculty.pk
    assert staff["supervisor"]["email"] == w.faculty.email
    assert staff["supervisor"]["department_name"] == w.f.department.name
    assert staff["group_owner"]["id"] == w.faculty.pk
    assert {r["supervisor_id"] for r in staff["counted"]} == {w.faculty.pk}

    own = _get(w, w.student, **params).data
    assert own["supervisor"]["name"] == staff["supervisor"]["name"]
    assert own["supervisor"]["email"] is None and own["supervisor"]["id_number"] is None
    peer_row = next(r for r in own["counted"] if r["user_id"] == w.peer.pk)
    assert peer_row["supervisor_name"] == staff["supervisor"]["name"]


def test_supervisor_falls_back_to_the_users_supervisor_and_is_empty_for_faculty(world):
    w = world
    User.objects.filter(pk=w.outsider.pk).update(supervisor=w.faculty)
    res = _get(w, w.admin, equipment=w.eq.pk, period="week", scope="individual", user_id=w.outsider.pk)
    assert res.data["supervisor"]["id"] == w.faculty.pk

    res = _get(w, w.admin, equipment=w.eq.pk, period="week", scope="group", user_id=w.faculty.pk)
    assert res.data["supervisor"]["id"] == w.faculty.pk  # group limits: the group head
    assert supervisor_of(w.faculty) is None
    assert supervisor_of(w.admin) is None


def test_a_single_request_over_the_limit_is_flagged(world):
    w = world
    params = dict(equipment=w.eq.pk, period="week", scope="individual")
    over = _get(w, w.student, requested=WEEKLY_INDIVIDUAL + 30, **params).data
    assert over["request_exceeds_limit"] is True
    assert over["used_minutes"] == 0 and over["over_by_minutes"] == 30
    assert _get(w, w.student, requested=30, **params).data["request_exceeds_limit"] is False


@pytest.mark.parametrize("viewer_name", ["faculty", "oic", "admin"])
def test_group_owner_oic_and_admin_see_full_details_of_another_user(world, viewer_name):
    w = world
    week = _monday(2)
    _book(w, w.peer, week, 9)

    res = _get(w, getattr(w, viewer_name), equipment=w.eq.pk, period="week", scope="group",
               user_id=w.student.pk, date=week.isoformat())

    assert res.status_code == 200, res.data
    assert res.data["full_details"] is True
    assert res.data["counted"][0]["can_open"] is True


def test_department_admin_of_the_equipment_department_may_view(world):
    w = world
    dept_admin = UserFactory(user_type=UserType.DEPT_ADMIN, department=w.f.department)
    res = _get(w, dept_admin, equipment=w.eq.pk, period="week", scope="individual", user_id=w.student.pk)
    assert res.status_code == 200, res.data


@pytest.mark.parametrize("viewer_name", ["outsider", "peer", "other_oic"])
def test_other_users_cannot_view_someone_elses_breakdown(world, viewer_name):
    w = world
    res = _get(w, getattr(w, viewer_name), equipment=w.eq.pk, period="week", scope="individual", user_id=w.student.pk)
    assert res.status_code == 403


def test_student_without_faculty_wallet_has_no_group_limit(world):
    w = world
    res = _get(w, w.outsider, equipment=w.eq.pk, period="week", scope="group")
    assert res.status_code == 404


def test_booking_breakdown_is_private_to_owner_and_excludes_the_booking(world):
    w = world
    week = _monday(2)
    target = _book(w, w.student, week, 9)
    other = _book(w, w.student, week + timedelta(days=1), 9, minutes=30)

    res = _get(w, w.student, booking_id=target.pk, period="week", scope="individual")
    assert res.status_code == 200, res.data
    assert [r["booking_id"] for r in res.data["counted"]] == [other.pk]
    assert res.data["excluded_booking_id"] == target.pk

    assert _get(w, w.outsider, booking_id=target.pk, period="week").status_code == 403
    assert _get(w, w.faculty, booking_id=target.pk, period="week", scope="individual").status_code == 200


# --- external users (equipment-level limit shared by external bookings) -------------------------


def test_external_pool_hides_other_external_users_except_from_staff(world):
    w = world
    eq = w.f.equipment(None)
    ExternalUserQuota.objects.create(
        equipment=eq, quota_type=QuotaType.WEEKLY, limit_type=QuotaLimitType.HOURS, limit_value=Decimal("240")
    )
    ext_a = UserFactory(user_type=UserType.EXTERNAL)
    ext_b = UserFactory(user_type=UserType.EXTERNAL)
    week = _monday(2)
    own = _book(w, ext_a, week, 9, eq=eq, user_type_snapshot=UserType.EXTERNAL)
    _book(w, ext_b, week + timedelta(days=1), 9, eq=eq, minutes=30, user_type_snapshot=UserType.EXTERNAL)

    res = _get(w, ext_a, equipment=eq.pk, period="week", date=week.isoformat())

    assert res.status_code == 200, res.data
    assert res.data["scope"] == "pool" and res.data["scope_label"] == "External Weekly"
    assert res.data["used_minutes"] == 90 == _enforced_used(ext_a, eq, QuotaType.WEEKLY, None, _at(week, 12))
    others = [r for r in res.data["counted"] if not r["is_viewer"]]
    assert others[0]["user_name"] == "Another user" and others[0]["booking_id"] is None
    assert next(r for r in res.data["counted"] if r["is_viewer"])["booking_id"] == own.pk

    staff = _get(w, w.admin, equipment=eq.pk, period="week", user_id=ext_a.pk, date=week.isoformat())
    assert all(r["user_name"] != "Another user" for r in staff.data["counted"])


# --- structured failure payload ------------------------------------------------------------------


def test_reschedule_refusal_carries_the_quota_payload(world, egs_quiet_side_effects):
    w = world
    booking = _book(w, w.student, _monday(2))
    _book(w, w.student, _monday(3), 12, minutes=WEEKLY_INDIVIDUAL, slot_count=2)
    slot = w.f.slot(w.eq, _at(_monday(3) + timedelta(days=1), 15))

    res = w.f.client_for(w.student).post(
        f"/api/bookings/{booking.pk}/user-reschedule/",
        {"start_time": slot.start_datetime.isoformat(), "end_time": slot.end_datetime.isoformat()},
        format="json",
    )

    assert res.status_code == 400
    assert res.data["code"] == "QUOTA_EXCEEDED"
    quota = res.data["quota"]
    assert quota["scope"] == "individual" and quota["period"] == QuotaType.WEEKLY
    assert (quota["used_minutes"], quota["requested_minutes"], quota["limit_minutes"]) == (WEEKLY_INDIVIDUAL, 60, WEEKLY_INDIVIDUAL)
    assert quota["booking_id"] == booking.pk and quota["user_id"] == w.student.pk
    assert quota["date"] == (_monday(3) + timedelta(days=1)).isoformat()
    assert quota["message"] == res.data["error"]

    follow = _get(w, w.student, booking_id=booking.pk, period="week", scope="individual", date=quota["date"])
    assert follow.data["used_minutes"] == quota["used_minutes"]


def test_input_edit_refusal_carries_the_quota_payload(world):
    w = world
    day = _monday(2)
    booking = _book(w, w.student, day, slot_count=2, input_values={"A": 2})
    _book(w, w.student, day + timedelta(days=1), 14)

    res = w.f.client_for(w.student).patch(
        f"/api/bookings/{booking.pk}/input-values/", {"input_values": {"A": 3}}, format="json"
    )

    assert res.status_code == 400
    assert res.data["code"] == "QUOTA_EXCEEDED"
    assert res.data["quota"]["used_minutes"] == 60
    assert res.data["quota"]["requested_minutes"] == 90
    assert res.data["quota"]["booking_id"] == booking.pk


# --- Booking Attempt Log: the requested slot's period, not the attempt's ---------------------------


def _quota_attempt(w, *, attempted_at, info, reason):
    log = BookingAttemptLog.objects.create(
        user=w.student,
        equipment=w.eq,
        outcome=BookingAttemptOutcome.FAILED,
        failure_reason=reason,
        duration_minutes=90,
        additional_info=info,
    )
    BookingAttemptLog.objects.filter(pk=log.pk).update(requested_at=attempted_at)
    return log


def _wednesday_evening_attempt(w, *, with_slots=True):
    next_week = _monday(3)
    wednesday_evening = _at(_monday(2) + timedelta(days=2), 21, 5)
    first = _book(w, w.student, next_week, 9, minutes=60)
    second = _book(w, w.student, next_week + timedelta(days=1), 9, minutes=60)
    _book(w, w.student, _monday(2), 9, minutes=30)  # the attempt's own week: must not be shown
    slot = w.f.slot(w.eq, _at(next_week + timedelta(days=2), 15))
    reason = (
        "Quota check failed: Individual Weekly quota exceeded: current usage 120 min + requested 90 min "
        "> 120 min"
    )
    info = {"slot_ids": [slot.pk]} if with_slots else {"input_values": {"A": 3}}
    return _quota_attempt(w, attempted_at=wednesday_evening, info=info, reason=reason), next_week, {first.pk, second.pk}


def test_attempt_breakdown_uses_the_requested_slots_week(world):
    w = world
    log, next_week, counted_ids = _wednesday_evening_attempt(w)

    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        res = _get(w, w.oic, log_id=log.pk)

    assert res.status_code == 200, res.data
    assert res.data["period_start"].startswith(next_week.isoformat())
    assert {r["booking_id"] for r in res.data["counted"]} == counted_ids
    assert res.data["used_minutes"] == 120 == res.data["attempt"]["logged_used_minutes"]
    assert res.data["requested_minutes"] == 90
    assert res.data["historical"] is True
    assert res.data["attempt"]["period_source"] == "requested_slot"
    assert res.data["attempt"]["limit_changed"] is False


def test_old_attempt_without_slots_picks_the_period_matching_the_logged_usage(world):
    w = world
    log, next_week, counted_ids = _wednesday_evening_attempt(w, with_slots=False)

    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        res = _get(w, w.admin, log_id=log.pk)
        legacy = w.f.client_for(w.admin).get(f"/api/booking-attempt-logs/{log.pk}/quota-breakdown/")

    assert res.data["period_start"].startswith(next_week.isoformat())
    assert res.data["attempt"]["period_source"] == "matched_usage"
    assert res.data["used_minutes"] == 120
    assert legacy.status_code == 200, legacy.data
    assert legacy.data["period_start"].startswith(next_week.isoformat())
    assert legacy.data["total_minutes"] == 120
    assert {e["real_booking_id"] for e in legacy.data["events"]} == counted_ids


def test_attempt_breakdown_reports_a_changed_limit(world):
    w = world
    log, _, _ = _wednesday_evening_attempt(w)
    EquipmentGroupQuota.objects.filter(equipment_group=w.group, quota_type=QuotaType.WEEKLY).update(
        internal_individual_quota_minutes=200
    )

    with patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        res = _get(w, w.admin, log_id=log.pk)

    assert res.data["limit_minutes"] == 200
    assert res.data["attempt"]["logged_limit_minutes"] == 120
    assert res.data["attempt"]["limit_changed"] is True


def test_the_user_can_open_the_calculation_of_their_own_attempt_only(world):
    w = world
    log, next_week, counted_ids = _wednesday_evening_attempt(w)
    _book(w, w.peer, next_week + timedelta(days=3), 9, minutes=30)

    own = _get(w, w.student, log_id=log.pk)
    assert own.status_code == 200, own.data
    assert own.data["viewer_access"] == "self" and own.data["full_details"] is False
    assert own.data["period_start"].startswith(next_week.isoformat())
    assert {r["booking_id"] for r in own.data["counted"]} == counted_ids
    assert own.data["used_minutes"] == 120 and own.data["requested_minutes"] == 90
    assert own.data["attempt"]["logged_limit_minutes"] == 120

    assert _get(w, w.faculty, log_id=log.pk).data["viewer_access"] == "owner"
    for stranger in (w.peer, w.outsider, w.other_oic):
        assert _get(w, stranger, log_id=log.pk).status_code == 403
    # The older date-wise view stays staff-only.
    assert w.f.client_for(w.student).get(f"/api/booking-attempt-logs/{log.pk}/quota-breakdown/").status_code == 403


def test_attempt_booked_for_the_user_by_staff_is_theirs(world):
    w = world
    log, _, _ = _wednesday_evening_attempt(w)
    BookingAttemptLog.objects.filter(pk=log.pk).update(
        user=w.admin, additional_info={**log.additional_info, "booked_for_user_id": w.student.pk}
    )
    assert _get(w, w.student, log_id=log.pk).status_code == 200
    assert _get(w, w.peer, log_id=log.pk).status_code == 403


def test_non_minute_limit_failures_have_no_calculation(world):
    w = world
    log = _quota_attempt(
        w,
        attempted_at=timezone.now(),
        info={},
        reason="Quota check failed: Individual Weekly booking-count quota exceeded: 3 bookings vs limit 2",
    )
    assert _get(w, w.student, log_id=log.pk).status_code == 400


# --- when each booking was requested ------------------------------------------------------------


def test_rows_carry_the_original_request_time(world):
    from iic_booking.equipment.models import BookingEvent, BookingEventType

    w = world
    week = _monday(2)
    first = _book(w, w.student, week, 9)
    asked = timezone.now() - timedelta(days=3, seconds=17)
    Booking.objects.filter(pk=first.pk).update(created_at=asked)
    # A reschedule moves the slots but keeps the booking (and its request time).
    first.daily_slots.update(
        start_datetime=_at(week + timedelta(days=1), 15), end_datetime=_at(week + timedelta(days=1), 16)
    )
    from_waitlist = _book(w, w.student, week + timedelta(days=2), 9, minutes=30)
    joined = timezone.now() - timedelta(days=5)
    BookingEvent.objects.create(
        booking=from_waitlist,
        event_type=BookingEventType.CREATED,
        metadata={"from_waitlist": True, "waitlist_joined_at": joined.isoformat()},
    )
    old_waitlist = _book(w, w.student, week + timedelta(days=3), 9, minutes=30)
    BookingEvent.objects.create(booking=old_waitlist, event_type=BookingEventType.CREATED, metadata={"from_waitlist": True})

    res = _get(w, w.student, equipment=w.eq.pk, period="week", scope="individual", date=week.isoformat())

    rows = {r["booking_id"]: r for r in res.data["counted"]}
    assert datetime.fromisoformat(rows[first.pk]["requested_at"]) == asked
    assert rows[first.pk]["requested_note"] is None
    assert datetime.fromisoformat(rows[from_waitlist.pk]["requested_at"]) == joined
    assert rows[from_waitlist.pk]["requested_note"] == "Joined the waitlist"
    assert rows[old_waitlist.pk]["requested_note"] == "Confirmed from the waitlist"
    assert rows[old_waitlist.pk]["requested_at"] is not None


def test_students_see_group_members_request_times(world):
    w = world
    week = _monday(2)
    _book(w, w.peer, week, 9)
    res = _get(w, w.student, equipment=w.eq.pk, period="week", scope="group", date=week.isoformat())
    peer_row = res.data["counted"][0]
    assert peer_row["can_open"] is False and peer_row["requested_at"]


# --- My booking attempts --------------------------------------------------------------------------

MINE = "/api/booking-attempt-logs/mine/"


def test_my_attempts_lists_only_my_own_and_booked_for_me(world):
    w = world
    log, _, _ = _wednesday_evening_attempt(w)
    for_me = _quota_attempt(w, attempted_at=timezone.now() - timedelta(days=1), info={"booked_for_user_id": w.student.pk},
                            reason="Selected slots are already occupied.")
    BookingAttemptLog.objects.filter(pk=for_me.pk).update(user=w.oic)
    peers = _quota_attempt(w, attempted_at=timezone.now(), info={}, reason="Quota check failed: Individual Weekly quota exceeded")
    BookingAttemptLog.objects.filter(pk=peers.pk).update(user=w.peer)
    ok = BookingAttemptLog.objects.create(user=w.student, equipment=w.eq, outcome=BookingAttemptOutcome.SUCCESS)

    res = w.f.client_for(w.student).get(MINE)

    assert res.status_code == 200, res.data
    by_id = {r["id"]: r for r in res.data["results"]}
    assert set(by_id) == {log.pk, for_me.pk} and res.data["total_count"] == 2
    assert by_id[log.pk]["can_view_calculation"] is True
    assert by_id[log.pk]["failure_title"] == "Weekly booking limit reached"
    assert by_id[log.pk]["requested_slots"][0]["start_datetime"]
    assert by_id[for_me.pk]["can_view_calculation"] is False
    assert by_id[for_me.pk]["booked_by_name"]
    assert "additional_info" not in by_id[log.pk] and "failure_reason" not in by_id[log.pk]
    assert {r["id"] for r in w.f.client_for(w.student).get(MINE, {"outcome": "ALL"}).data["results"]} == {log.pk, for_me.pk, ok.pk}


def test_my_attempts_date_filter_and_pages(world):
    w = world
    today = timezone.localdate()
    logs = [
        _quota_attempt(w, attempted_at=_at(today - timedelta(days=d), 21), info={}, reason="Quota check failed: Individual Weekly quota exceeded")
        for d in (0, 1, 2, 10)
    ]
    client = w.f.client_for(w.student)
    recent = client.get(MINE, {"date_from": (today - timedelta(days=2)).isoformat(), "date_to": today.isoformat()})
    assert recent.data["total_count"] == 3
    first_page = client.get(MINE, {"limit": 2})
    second_page = client.get(MINE, {"limit": 2, "offset": 2})
    assert [r["id"] for r in first_page.data["results"]] == [logs[0].pk, logs[1].pk]
    assert [r["id"] for r in second_page.data["results"]] == [logs[2].pk, logs[3].pk]
    assert client.get(MINE, {"date_from": "yesterday"}).status_code == 400
