"""Utilization factor: booked ÷ available slot hours inside the weekly view window on working days."""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import pytest
from django.utils import timezone

from iic_booking.equipment.models import Holiday, SlotStatus
from iic_booking.equipment.utilization import (
    UtilizationTally,
    ViewWindow,
    WorkingCalendar,
    compute_utilization,
    utilization_period,
    utilization_period_caption,
    view_window_for,
    window_hours,
)

MONDAY = date(2026, 10, 5)
FRIDAY = date(2026, 10, 9)
SATURDAY = date(2026, 10, 10)
OFFICE = ViewWindow(time(9, 0), time(17, 30))
WEEKDAYS = WorkingCalendar()


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    return timezone.make_aware(datetime.combine(day, time(hour, minute)))


# --- window clipping ----------------------------------------------------------------------------------------


def test_slot_inside_window_counts_fully():
    assert window_hours(_at(MONDAY, 10), _at(MONDAY, 12), OFFICE, WEEKDAYS) == (2.0, 0.0)


def test_slot_outside_window_counts_nothing():
    assert window_hours(_at(MONDAY, 18), _at(MONDAY, 20), OFFICE, WEEKDAYS) == (0.0, 0.0)
    assert window_hours(_at(MONDAY, 6), _at(MONDAY, 9), OFFICE, WEEKDAYS) == (0.0, 0.0)


def test_partial_overlap_is_clipped_to_the_window():
    assert window_hours(_at(MONDAY, 8), _at(MONDAY, 10), OFFICE, WEEKDAYS) == (1.0, 0.0)
    assert window_hours(_at(MONDAY, 17), _at(MONDAY, 19), OFFICE, WEEKDAYS) == (0.5, 0.0)


def test_no_window_and_midnight_end_mean_the_whole_day():
    assert window_hours(_at(MONDAY, 20), _at(MONDAY, 23), ViewWindow(), WEEKDAYS) == (3.0, 0.0)
    assert window_hours(_at(MONDAY, 20), _at(MONDAY, 23), ViewWindow(time(18, 0), time(0, 0)), WEEKDAYS) == (3.0, 0.0)


# --- weekends, holidays and multi-day slots -------------------------------------------------------------------


def test_weekend_hours_are_not_working_hours():
    assert window_hours(_at(SATURDAY, 10), _at(SATURDAY, 12), OFFICE, WEEKDAYS) == (0.0, 2.0)


def test_holiday_hours_are_not_working_hours():
    calendar = WorkingCalendar(holidays=frozenset({MONDAY}))
    assert window_hours(_at(MONDAY, 10), _at(MONDAY, 12), OFFICE, calendar) == (0.0, 2.0)


@pytest.mark.django_db
def test_configurable_weekend(settings):
    settings.UTILIZATION_WEEKEND_DAYS = (6,)
    assert WorkingCalendar.for_range(SATURDAY, SATURDAY).is_working_day(SATURDAY)


def test_full_day_slot_counts_only_the_window():
    # A 1440-minute slot (00:00-24:00) with a 09:00-17:30 window gives 8.5 h per working day.
    assert window_hours(_at(MONDAY, 0), _at(MONDAY + timedelta(days=1), 0), OFFICE, WEEKDAYS) == (8.5, 0.0)
    assert window_hours(_at(MONDAY, 0), _at(MONDAY + timedelta(days=1), 0), ViewWindow(), WEEKDAYS) == (24.0, 0.0)


def test_slot_across_midnight_is_split_per_day():
    full_day = ViewWindow()
    # Thursday 18:00 -> Friday 02:00: both working days.
    assert window_hours(_at(FRIDAY - timedelta(days=1), 18), _at(FRIDAY, 2), full_day, WEEKDAYS) == (8.0, 0.0)
    # Friday 18:00 -> Saturday 02:00: the Saturday part is weekend.
    assert window_hours(_at(FRIDAY, 18), _at(SATURDAY, 2), full_day, WEEKDAYS) == (6.0, 2.0)
    # Multi-day slot Friday 09:00 -> Monday 17:30 within the office window: Friday and Monday only.
    assert window_hours(_at(FRIDAY, 9), _at(MONDAY + timedelta(days=7), 17, 30), OFFICE, WEEKDAYS) == (17.0, 17.0)


# --- tally ---------------------------------------------------------------------------------------------------


def test_tally_uses_the_same_clipping_for_booked_and_available_hours():
    tally = UtilizationTally()
    tally.add(SlotStatus.BOOKED, _at(MONDAY, 9), _at(MONDAY, 11), OFFICE, WEEKDAYS)
    tally.add(SlotStatus.AVAILABLE, _at(MONDAY, 11), _at(MONDAY, 13), OFFICE, WEEKDAYS)
    tally.add(SlotStatus.BOOKED, _at(MONDAY, 17), _at(MONDAY, 19), OFFICE, WEEKDAYS)
    tally.add(SlotStatus.BOOKED, _at(SATURDAY, 10), _at(SATURDAY, 12), OFFICE, WEEKDAYS)
    assert tally.booked_hours == 2.5
    assert tally.available_hours == 4.5
    assert tally.booked_hours_outside_window == 3.5
    assert tally.factor == round(2.5 / 4.5, 4)
    # Previous formula: every booked slot hour over every slot hour.
    assert tally.all_slot_factor == round(6 / 8, 4)


def test_tally_skips_blocked_slots_and_test_bookings():
    tally = UtilizationTally()
    tally.add(SlotStatus.BLOCKED, _at(MONDAY, 9), _at(MONDAY, 10), OFFICE, WEEKDAYS)
    tally.add(SlotStatus.NOT_AVAILABLE, _at(MONDAY, 10), _at(MONDAY, 11), OFFICE, WEEKDAYS)
    tally.add(SlotStatus.BOOKED, _at(MONDAY, 11), _at(MONDAY, 12), OFFICE, WEEKDAYS, test_booking=True)
    tally.add(SlotStatus.UNDER_MAINTENANCE, _at(MONDAY, 12), _at(MONDAY, 13), OFFICE, WEEKDAYS)
    assert tally.available_hours == 1.0 and tally.booked_hours == 0.0
    assert tally.factor == 0.0


def test_no_slots_is_not_applicable():
    assert UtilizationTally().factor is None
    tally = UtilizationTally()
    tally.add(SlotStatus.AVAILABLE, _at(SATURDAY, 10), _at(SATURDAY, 12), OFFICE, WEEKDAYS)
    assert tally.factor is None


def test_window_falls_back_to_parent_then_setting(settings):
    from types import SimpleNamespace

    none = SimpleNamespace(weekly_view_time_from=None, weekly_view_time_to=None)
    parent = SimpleNamespace(weekly_view_time_from=time(10, 0), weekly_view_time_to=None)
    assert view_window_for(none, parent) == ViewWindow(time(10, 0), None)
    assert view_window_for(none) == ViewWindow()
    settings.UTILIZATION_DEFAULT_VIEW_WINDOW = ("09:00", "24:00")
    assert view_window_for(none) == ViewWindow(time(9, 0), time(0, 0))


# --- portal go-live date ---------------------------------------------------------------------------------------

GO_LIVE = date(2026, 10, 5)


@pytest.fixture
def go_live(settings):
    settings.PORTAL_GO_LIVE_DATE = GO_LIVE.isoformat()
    return GO_LIVE


def test_period_start_is_clamped_to_go_live(go_live):
    period = utilization_period(date(2026, 9, 11), date(2026, 10, 10))
    assert (period.start, period.end, period.clamped, period.is_empty) == (go_live, date(2026, 10, 10), True, False)
    assert period.as_dict() == {
        "utilization_period_from": "2026-10-05",
        "utilization_period_to": "2026-10-10",
        "utilization_period_clamped": True,
        "portal_go_live_date": "2026-10-05",
    }
    assert utilization_period_caption(period.as_dict()) == "Since 05-10-2026 (portal go-live)"


def test_period_after_go_live_is_unchanged(go_live):
    period = utilization_period("2026-10-07", "2026-10-31")
    assert (period.start, period.clamped) == (date(2026, 10, 7), False)
    assert utilization_period_caption(period.as_dict()) == ""


def test_period_entirely_before_go_live_is_empty(go_live):
    period = utilization_period(date(2026, 9, 1), date(2026, 9, 30))
    assert period.is_empty
    assert period.as_dict()["utilization_period_from"] is None
    assert utilization_period_caption(period.as_dict()) == "Period is before portal go-live (05-10-2026)"


def test_no_go_live_setting_means_no_clamp(settings):
    settings.PORTAL_GO_LIVE_DATE = ""
    period = utilization_period(date(2026, 9, 1), date(2026, 9, 30))
    assert (period.start, period.clamped, period.is_empty) == (date(2026, 9, 1), False, False)


@pytest.fixture
def straddling_equipment(egs_factory, go_live):
    f = egs_factory
    eq = f.equipment(weekly_view_time_from=time(9, 0), weekly_view_time_to=time(17, 30))
    f.booking(f.student(), eq, _at(go_live - timedelta(days=3), 10), slot_count=2)  # Friday before go-live
    f.slot(eq, _at(go_live, 10), minutes=120)
    f.booking(f.student(), eq, _at(go_live, 12), slot_count=1)
    return eq


@pytest.mark.django_db
def test_slots_before_go_live_are_not_counted(straddling_equipment):
    result = compute_utilization(straddling_equipment, date(2026, 9, 28), date(2026, 10, 9))
    assert (result["booked_hours"], result["available_hours"]) == (1.0, 3.0)
    assert result["utilization_factor"] == round(1 / 3, 4)
    assert result["utilization_period_from"] == "2026-10-05" and result["utilization_period_clamped"] is True


@pytest.mark.django_db
def test_period_before_go_live_is_not_applicable(straddling_equipment):
    result = compute_utilization(straddling_equipment, date(2026, 9, 1), date(2026, 10, 4))
    assert result["utilization_factor"] is None
    assert result["utilization_period_from"] is None


@pytest.mark.django_db
def test_equipment_report_slot_hours_start_at_go_live(straddling_equipment):
    from iic_booking.equipment.reports import get_equipment_report_data

    summary = get_equipment_report_data("2026-10-01", "2026-10-09", [straddling_equipment.pk])["summary"]
    assert summary["utilization_factor"] == round(1 / 3, 4)
    assert summary["utilized_hours"] == 1.0
    assert summary["utilization_period_from"] == "2026-10-05" and summary["utilization_period_clamped"] is True

    before = get_equipment_report_data("2026-09-01", "2026-09-30", [straddling_equipment.pk])["summary"]
    assert before["utilization_factor"] is None and before["utilization_period_from"] is None


# --- database: compute_utilization and the equipment report -----------------------------------------------


@pytest.fixture
def office_equipment(egs_factory):
    f = egs_factory
    eq = f.equipment(weekly_view_time_from=time(9, 0), weekly_view_time_to=time(17, 30))
    student = f.student()
    tuesday = MONDAY + timedelta(days=1)
    Holiday.objects.create(date=tuesday, reason="Test holiday")
    f.booking(student, eq, _at(MONDAY, 10), slot_count=2)  # 10:00-12:00 booked, in window
    f.slot(eq, _at(MONDAY, 12), minutes=120)  # 12:00-14:00 available
    f.slot(eq, _at(MONDAY, 17), minutes=120)  # 17:00-19:00 available, 0.5 h in window
    f.slot(eq, _at(MONDAY, 19), minutes=120, status=SlotStatus.BLOCKED)
    f.booking(student, eq, _at(tuesday, 10), slot_count=1)  # holiday
    f.slot(eq, _at(tuesday, 11), minutes=60)  # holiday
    f.booking(student, eq, _at(SATURDAY, 10), slot_count=1)  # weekend
    f.slot(eq, _at(SATURDAY, 11), minutes=60)  # weekend
    tester = f.student()
    tester.is_test_account = True
    tester.save(update_fields=["is_test_account"])
    f.booking(tester, eq, _at(MONDAY, 14), slot_count=1)
    return eq


@pytest.mark.django_db
def test_compute_utilization_excludes_weekends_holidays_and_outside_window(office_equipment):
    result = compute_utilization(office_equipment, MONDAY, SATURDAY)
    assert result["booked_hours"] == 2.0
    assert result["available_hours"] == 4.5
    assert result["utilization_factor"] == round(2.0 / 4.5, 4)
    assert result["booked_hours_outside_window"] == 2.0


@pytest.mark.django_db
def test_compute_utilization_without_slots_is_not_applicable(egs_factory):
    eq = egs_factory.equipment()
    result = compute_utilization(eq.pk, MONDAY, SATURDAY)
    assert result["utilization_factor"] is None
    assert result["available_hours"] == 0.0


@pytest.mark.django_db
def test_utilization_report_command_prints_before_and_after(office_equipment, egs_factory):
    from io import StringIO

    from django.core.management import call_command

    idle = egs_factory.equipment()
    out = StringIO()
    call_command(
        "utilization_report", "--from", MONDAY.isoformat(), "--to", SATURDAY.isoformat(),
        "--equipment", f"{office_equipment.pk},{idle.code}", "--all", "--csv", stdout=out,
    )
    lines = out.getvalue().splitlines()
    rows = {r.split(",")[0]: r.split(",") for r in lines[2:]}
    office = rows[str(office_equipment.pk)]
    # Before: 4 booked of 10 counted slot hours (the test-account booking and the blocked slot are left out).
    assert office[4:7] == ["10.0", "4.0", "40.0%"]
    assert office[7:11] == ["4.5", "2.0", "44.4%", "2.0"]
    assert rows[str(idle.pk)][9] == "N/A"


@pytest.mark.django_db
def test_equipment_report_uses_the_shared_utilization(office_equipment, egs_factory):
    from iic_booking.equipment.reports import get_equipment_report_data

    idle = egs_factory.equipment()
    data = get_equipment_report_data(MONDAY.isoformat(), SATURDAY.isoformat(), [office_equipment.pk, idle.pk])
    rows = {r["equipment_id"]: r for r in data["equipment"]}
    expected = compute_utilization(office_equipment, MONDAY, SATURDAY)
    assert rows[office_equipment.pk]["utilization_factor"] == expected["utilization_factor"]
    assert rows[office_equipment.pk]["utilization_available_hours"] == 4.5
    assert rows[idle.pk]["utilization_factor"] is None
    summary = data["summary"]
    assert summary["utilization_factor"] == expected["utilization_factor"]
    assert summary["utilization_booked_hours"] == 2.0
    assert summary["utilization_available_hours"] == 4.5
    # Working-window capacity uses the same clipping: 2 + 2 + 0.5 + 1 (test booking) + 0 (blocked at 19:00).
    assert rows[office_equipment.pk]["available_hours_working_window"] == 5.5
