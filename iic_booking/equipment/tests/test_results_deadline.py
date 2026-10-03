"""Per-equipment results deadline: working days, overdue list, permissions, user visibility and the safeguard jobs."""

from datetime import datetime, timedelta
from importlib import import_module
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.apps import apps as django_apps
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone

from iic_booking.equipment.booking_lab_outreach import reminder_presets
from iic_booking.equipment.models import (
    Booking,
    BookingSampleTrace,
    BookingStatus,
    EquipmentManager,
    EquipmentOperator,
    EquipmentTemporaryOIC,
    Holiday,
    ResultsDeadlinePolicy,
    SampleTraceStatus,
)
from iic_booking.equipment.results_deadline import (
    UNIT_HOURS,
    UNIT_WORKING_DAYS,
    WorkingCalendar,
    automation_state,
    booking_results_deadline,
    compute_results_deadline,
    dry_run,
    is_results_overdue,
    overdue_booking_ids,
    set_automation,
    working_days_from_timer_hours,
)
from iic_booking.equipment.serializers import (
    BookingSerializer,
    EquipmentAdminWriteSerializer,
    EquipmentListSerializer,
)
from iic_booking.equipment.tasks import (
    auto_mark_operator_absent_disruption_after_booking_end,
    auto_mark_operator_unavailable_after_booking_end,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

ABSENT = "iic_booking.equipment.maintenance_policy.apply_operator_absent_disruption_for_booking"
UNAVAILABLE = "iic_booking.equipment.operator_unavailable.apply_operator_unavailable_booking"


def _local(*args):
    return timezone.make_aware(datetime(*args))


def _staff(user_type, **kw):
    return UserFactory(user_type=user_type, admin_approved=True, **kw)


def _trace(booking, status, at):
    row = BookingSampleTrace.objects.create(booking=booking, status=status)
    BookingSampleTrace.objects.filter(pk=row.pk).update(created_at=at)
    return row


def _reload(booking):
    return Booking.objects.select_related("equipment", "user").get(pk=booking.pk)


@pytest.fixture
def world(egs_factory):
    eq_a = egs_factory.equipment(results_deadline_value=2, results_deadline_unit=UNIT_WORKING_DAYS)
    eq_b = egs_factory.equipment(results_deadline_value=2, results_deadline_unit=UNIT_WORKING_DAYS)
    oic_a = _staff(UserType.MANAGER)
    oic_b = _staff(UserType.MANAGER)
    operator_a = _staff(UserType.OPERATOR)
    EquipmentManager.objects.create(equipment=eq_a, manager=oic_a)
    EquipmentManager.objects.create(equipment=eq_b, manager=oic_b)
    EquipmentOperator.objects.create(equipment=eq_a, operator=operator_a, role=EquipmentOperator.Role.PRIMARY)
    student = egs_factory.student()
    return SimpleNamespace(
        f=egs_factory, eq_a=eq_a, eq_b=eq_b, oic_a=oic_a, oic_b=oic_b, operator_a=operator_a, student=student,
        admin=_staff(UserType.ADMIN), now=timezone.now(),
    )


# --- working days -------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_working_days_skip_weekend_and_holidays():
    friday_slot_end = _local(2026, 10, 2, 15, 0)
    due = compute_results_deadline(friday_slot_end, 2, UNIT_WORKING_DAYS, WorkingCalendar())
    assert timezone.localtime(due) == _local(2026, 10, 6, 23, 59, 59)  # Mon + Tue

    Holiday.objects.create(date=datetime(2026, 10, 5).date(), reason="Test holiday")
    due = compute_results_deadline(friday_slot_end, 2, UNIT_WORKING_DAYS, WorkingCalendar())
    assert timezone.localtime(due) == _local(2026, 10, 7, 23, 59, 59)  # Monday holiday skipped

    Holiday.objects.filter(date=datetime(2026, 10, 5).date()).update(is_active=False)
    due = compute_results_deadline(friday_slot_end, 2, UNIT_WORKING_DAYS, WorkingCalendar())
    assert timezone.localtime(due) == _local(2026, 10, 6, 23, 59, 59)


def test_hours_unit_and_zero():
    end = _local(2026, 10, 2, 15, 0)
    assert compute_results_deadline(end, 36, UNIT_HOURS) == end + timedelta(hours=36)
    assert compute_results_deadline(end, 0, UNIT_WORKING_DAYS) is None


def test_initial_value_from_old_timers_is_never_earlier():
    assert working_days_from_timer_hours(48, 24) == 2
    assert working_days_from_timer_hours(168, 168) == 5
    assert working_days_from_timer_hours(240, 240) == 8
    assert working_days_from_timer_hours(24, 24) == 1
    assert working_days_from_timer_hours(0, 24) == 1
    assert working_days_from_timer_hours(0, 0) == 0


@pytest.mark.django_db
def test_data_migration_sets_only_the_new_fields(egs_factory):
    weekly = egs_factory.equipment(
        operator_unavailable_after_booking_end_hours=168, operator_absent_disruption_after_booking_end_hours=168
    )
    off = egs_factory.equipment(
        operator_unavailable_after_booking_end_hours=0, operator_absent_disruption_after_booking_end_hours=0
    )
    migration = import_module("iic_booking.equipment.migrations.0222_equipment_results_deadline")
    migration.initialise_results_deadline(django_apps, None)
    weekly.refresh_from_db()
    off.refresh_from_db()
    assert (weekly.results_deadline_value, weekly.results_deadline_unit) == (5, UNIT_WORKING_DAYS)
    assert weekly.operator_absent_disruption_after_booking_end_hours == 168
    assert off.results_deadline_value == 0
    assert weekly.show_results_deadline_to_users is False


# --- overdue ------------------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_overdue_detection(world):
    f, now = world.f, world.now
    overdue = f.booking(world.student, world.eq_a, now - timedelta(days=20))
    _trace(overdue, SampleTraceStatus.SAMPLE_ACCEPTED, now - timedelta(days=20))
    recent = f.booking(world.student, world.eq_a, now - timedelta(hours=2))
    waiting_user = f.booking(world.student, world.eq_a, now - timedelta(days=20))
    _trace(waiting_user, SampleTraceStatus.HELD_AT_OFFICE, now - timedelta(days=19))
    done = f.booking(world.student, world.eq_a, now - timedelta(days=20))
    Booking.objects.filter(pk=done.pk).update(status=BookingStatus.COMPLETED)
    processing = f.booking(world.student, world.eq_a, now - timedelta(days=20))
    Booking.objects.filter(pk=processing.pk).update(status=BookingStatus.PROCESSING)
    extended = f.booking(world.student, world.eq_a, now - timedelta(days=20))
    Booking.objects.filter(pk=extended.pk).update(operator_absent_hold_until=now + timedelta(days=1))
    no_deadline_eq = f.equipment(results_deadline_value=0)
    no_deadline = f.booking(world.student, no_deadline_eq, now - timedelta(days=20))

    ids = set(overdue_booking_ids(Booking.objects.all(), now))
    assert ids == {overdue.pk, processing.pk}
    assert recent.pk not in ids and waiting_user.pk not in ids and extended.pk not in ids
    assert no_deadline.pk not in ids

    b = _reload(extended)
    deadline = booking_results_deadline(b)
    assert deadline.extended and not is_results_overdue(b, deadline, now)


@pytest.mark.django_db
def test_overdue_endpoint_list_filter_and_staff_today_are_scoped(world):
    f, now = world.f, world.now
    mine = f.booking(world.student, world.eq_a, now - timedelta(days=20))
    other = f.booking(world.student, world.eq_b, now - timedelta(days=20))
    f.booking(world.student, world.eq_a, now - timedelta(hours=1))

    res = f.client_for(world.oic_a).get("/api/bookings/results-overdue/")
    assert res.status_code == 200 and [r["booking_id"] for r in res.data["bookings"]] == [mine.pk]
    assert res.data["bookings"][0]["link"] == f"/booking-management?expand={mine.pk}"
    assert [r["booking_id"] for r in f.client_for(world.operator_a).get("/api/bookings/results-overdue/").data["bookings"]] == [mine.pk]
    assert [r["booking_id"] for r in f.client_for(world.oic_b).get("/api/bookings/results-overdue/").data["bookings"]] == [other.pk]
    assert f.client_for(world.student).get("/api/bookings/results-overdue/").data["count"] == 0
    assert {r["booking_id"] for r in f.client_for(world.admin).get("/api/bookings/results-overdue/").data["bookings"]} >= {
        mine.pk,
        other.pk,
    }

    listing = f.client_for(world.admin).get("/api/bookings/", {"results_overdue": "1"})
    assert listing.status_code == 200
    rows = listing.data["bookings"]
    assert {r["real_booking_id"] for r in rows} == {mine.pk, other.pk}
    assert all(r["results_deadline"]["overdue"] for r in rows)

    student_list = f.client_for(world.student).get("/api/bookings/", {"results_overdue": "1"})
    assert list(student_list.data["bookings"]) == []

    today = f.client_for(world.operator_a).get("/api/staff-app/today/", {"refresh": "1"})
    assert today.status_code == 200
    assert today.data["counts"]["results_overdue"] == 1
    assert today.data["results_overdue_booking_ids"] == [mine.pk]


# --- who may change it --------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_oic_settings_permissions_and_validation(world):
    f = world.f
    url = f"/api/oic/equipment-settings/{world.eq_a.pk}/"
    body = {"results_deadline_value": 4, "results_deadline_unit": "WORKING_DAYS", "show_results_deadline_to_users": True}

    listing = f.client_for(world.oic_a).get("/api/oic/equipment-settings/")
    settings_row = listing.data["equipments"][0]["settings"]
    assert settings_row["results_deadline_value"] == 2 and settings_row["show_results_deadline_to_users"] is False

    res = f.client_for(world.oic_a).patch(url, body, format="json")
    assert res.status_code == 200, res.data
    world.eq_a.refresh_from_db()
    assert (world.eq_a.results_deadline_value, world.eq_a.show_results_deadline_to_users) == (4, True)

    assert f.client_for(world.oic_b).patch(url, body, format="json").status_code == 403
    assert f.client_for(world.operator_a).patch(url, body, format="json").status_code == 403
    assert f.client_for(world.student).patch(url, body, format="json").status_code == 403

    temp = _staff(UserType.MANAGER)
    EquipmentTemporaryOIC.objects.create(
        equipment=world.eq_a, temporary_oic=temp, primary_oic=world.oic_a, resume_at=timezone.now() + timedelta(days=3)
    )
    assert f.client_for(temp).patch(url, {"results_deadline_value": 3}, format="json").status_code == 200
    assert f.client_for(world.admin).patch(url, {"results_deadline_value": 36, "results_deadline_unit": "HOURS"}, format="json").status_code == 200
    world.eq_a.refresh_from_db()
    assert (world.eq_a.results_deadline_value, world.eq_a.results_deadline_unit) == (36, UNIT_HOURS)

    bad = f.client_for(world.oic_a).patch(url, {"results_deadline_value": 61, "results_deadline_unit": "WORKING_DAYS"}, format="json")
    assert bad.status_code == 400 and "results_deadline_value" in bad.data["errors"]
    bad = f.client_for(world.oic_a).patch(url, {"results_deadline_unit": "WEEKS"}, format="json")
    assert bad.status_code == 400 and "results_deadline_unit" in bad.data["errors"]


@pytest.mark.django_db
def test_admin_equipment_form_only_admin_or_oic_change_the_deadline(world):
    eq = world.eq_a
    dept_admin = _staff(UserType.DEPT_ADMIN)

    def _valid(user, data):
        s = EquipmentAdminWriteSerializer(eq, data=data, partial=True, context={"request": SimpleNamespace(user=user)})
        return s.is_valid(), s.errors

    ok, errors = _valid(dept_admin, {"results_deadline_value": 5})
    assert not ok and "results_deadline_value" in errors
    ok, errors = _valid(world.oic_b, {"show_results_deadline_to_users": True})
    assert not ok
    ok, _errors = _valid(dept_admin, {"results_deadline_value": eq.results_deadline_value})
    assert ok  # unchanged values from a full-form save are fine
    ok, errors = _valid(world.admin, {"results_deadline_value": 5})
    assert ok, errors
    ok, errors = _valid(world.oic_a, {"results_deadline_value": 3, "show_results_deadline_to_users": True})
    assert ok, errors
    ok, errors = _valid(world.admin, {"results_deadline_value": 900, "results_deadline_unit": "HOURS"})
    assert not ok


# --- what users see -----------------------------------------------------------------------------------------------


@pytest.mark.django_db
def test_users_see_the_deadline_only_when_the_oic_turns_it_on(world):
    booking = world.f.booking(world.student, world.eq_a, world.now - timedelta(hours=3))
    booking = _reload(booking)

    def payload(user):
        s = BookingSerializer(booking, context={"request": SimpleNamespace(user=user)})
        return s.get_results_deadline(booking)

    assert world.eq_a.show_results_deadline_to_users is False
    assert payload(world.student) is None
    assert EquipmentListSerializer(world.eq_a).data["results_deadline_public"] is None
    staff = payload(world.operator_a)
    assert staff["value"] == 2 and staff["visible_to_user"] is False and staff["overdue"] is False

    type(world.eq_a).objects.filter(pk=world.eq_a.pk).update(show_results_deadline_to_users=True)
    booking = _reload(booking)
    user_view = payload(world.student)
    assert user_view["visible_to_user"] is True and user_view["due_display"] and user_view["overdue"] is False
    world.eq_a.refresh_from_db()
    assert EquipmentListSerializer(world.eq_a).data["results_deadline_public"]["label"] == "within 2 working days after the slot"


# --- safeguard jobs -----------------------------------------------------------------------------------------------


def _stuck_booking(world, ended_days_ago, *, trace_age=None, eq=None):
    eq = eq or world.eq_a
    booking = world.f.booking(world.student, eq, world.now - timedelta(days=ended_days_ago, hours=1))
    _trace(booking, SampleTraceStatus.SAMPLE_ACCEPTED, world.now - (trace_age or timedelta(days=ended_days_ago)))
    return booking


@pytest.fixture
def weekly_eq(world):
    """Old timers at 7 days, results deadline 1 working day (so the two rules disagree)."""
    return world.f.equipment(
        operator_unavailable_after_booking_end_hours=168,
        operator_absent_disruption_after_booking_end_hours=168,
        results_deadline_value=1,
    )


@pytest.mark.django_db
def test_automation_is_off_by_default_and_old_timers_still_work(world, weekly_eq):
    assert automation_state().enabled is False
    legacy_due = _stuck_booking(world, 10, eq=weekly_eq)
    not_yet = _stuck_booking(world, 5, eq=weekly_eq)
    with patch(ABSENT) as absent:
        assert auto_mark_operator_absent_disruption_after_booking_end() == 1
    assert [c.args[0].pk for c in absent.call_args_list] == [legacy_due.pk]
    assert "(stuck sample status)" in _reload(legacy_due).notes
    assert not _reload(not_yet).notes


@pytest.mark.django_db
def test_switching_on_never_acts_on_bookings_that_ended_before(world, weekly_eq):
    past = _stuck_booking(world, 5, eq=weekly_eq)  # results deadline passed, old 7-day timer not yet
    set_automation(True)
    with patch(ABSENT) as absent:
        assert auto_mark_operator_absent_disruption_after_booking_end() == 0
    absent.assert_not_called()
    assert _reload(past).status == BookingStatus.BOOKED


@pytest.mark.django_db
def test_results_deadline_mode_acts_at_the_deadline_without_restart(world, weekly_eq):
    set_automation(True)
    ResultsDeadlinePolicy.objects.update(automation_since=world.now - timedelta(days=30))
    due = _stuck_booking(world, 5, eq=weekly_eq, trace_age=timedelta(hours=1))  # recent status update: no restart
    before = world.f.booking(world.student, weekly_eq, world.now - timedelta(hours=2))
    _trace(before, SampleTraceStatus.SAMPLE_ACCEPTED, world.now - timedelta(hours=1))
    with patch(ABSENT) as absent:
        assert auto_mark_operator_absent_disruption_after_booking_end() == 1
    assert [c.args[0].pk for c in absent.call_args_list] == [due.pk]
    assert "(results deadline passed)" in _reload(due).notes


@pytest.mark.django_db
def test_results_deadline_mode_keeps_the_full_refund_case(world, weekly_eq):
    set_automation(True)
    ResultsDeadlinePolicy.objects.update(automation_since=world.now - timedelta(days=30))
    booking = world.f.booking(world.student, weekly_eq, world.now - timedelta(days=5, hours=1))
    _trace(booking, SampleTraceStatus.FORWARDED_TO_LAB, world.now - timedelta(days=5))
    _trace(booking, SampleTraceStatus.SAMPLE_SENT, world.now - timedelta(days=4))
    with patch(UNAVAILABLE) as unavailable:
        assert auto_mark_operator_unavailable_after_booking_end() == 1
    assert unavailable.call_args.args[0].pk == booking.pk
    assert unavailable.call_args.kwargs["notes"] == "Automatically marked: results deadline passed (scheduled job)."


@pytest.mark.django_db
def test_extension_moves_the_safeguard(world, weekly_eq):
    set_automation(True)
    ResultsDeadlinePolicy.objects.update(automation_since=world.now - timedelta(days=30))
    booking = _stuck_booking(world, 5, eq=weekly_eq)
    Booking.objects.filter(pk=booking.pk).update(operator_absent_hold_until=world.now + timedelta(days=2))
    with patch(ABSENT) as absent:
        assert auto_mark_operator_absent_disruption_after_booking_end() == 0
    absent.assert_not_called()


@pytest.mark.django_db
def test_switching_off_then_on_starts_a_new_window(world):
    first = set_automation(True)
    since = first.automation_since
    set_automation(False)
    assert automation_state().enabled is False
    again = set_automation(True)
    assert again.automation_since >= since


@pytest.mark.django_db
def test_dry_run_and_enable_command(world, weekly_eq):
    data = dry_run()
    assert data["automation_enabled"] is False and sum(data["would_act_if_enabled_now"].values()) == 0

    _stuck_booking(world, 10, eq=weekly_eq)  # the old timer would act on it
    data = dry_run()
    assert data["would_act_if_enabled_now"] == {"operator_absent": 1}
    with pytest.raises(CommandError):
        call_command("results_deadline_automation", "enable")
    assert automation_state().enabled is False

    Booking.objects.update(status=BookingStatus.COMPLETED)
    call_command("results_deadline_automation", "enable")
    assert automation_state().enabled is True
    call_command("results_deadline_automation", "disable")
    assert automation_state().enabled is False


# --- reminder preset and digest -----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_results_delayed_preset(world):
    started = world.f.booking(world.student, world.eq_a, world.now - timedelta(days=3))
    upcoming = world.f.booking(world.student, world.eq_a, world.now + timedelta(days=3))
    done = world.f.booking(world.student, world.eq_a, world.now - timedelta(days=3))
    Booking.objects.filter(pk=done.pk).update(status=BookingStatus.COMPLETED)

    codes = lambda b: [p["code"] for p in reminder_presets(_reload(b))]  # noqa: E731
    assert "results_delayed" in codes(started)
    assert "results_delayed" not in codes(upcoming)
    assert "results_delayed" not in codes(done)
    text = next(p["text"] for p in reminder_presets(_reload(started)) if p["code"] == "results_delayed")
    assert "taking longer than expected" in text


@pytest.mark.django_db
def test_completion_digest_and_card_show_results_due(world):
    from iic_booking.equipment.completion_reminders import (
        _digest_context,
        bookings_awaiting_completion_for_user,
        serialize_awaiting_booking,
    )

    world.f.booking(world.student, world.eq_a, world.now - timedelta(days=20))
    rows = list(bookings_awaiting_completion_for_user(world.oic_a))
    card = serialize_awaiting_booking(rows[0], world.now)
    assert card["results_overdue"] is True and card["results_due_display"]
    ctx = _digest_context(world.oic_a, rows, world.now)
    assert "Results due" in ctx["bookings_html"] and "results overdue" in ctx["bookings_text"]
