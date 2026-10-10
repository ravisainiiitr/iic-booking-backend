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
TODAY = SATURDAY


@pytest.fixture(autouse=True)
def today(monkeypatch):
    monkeypatch.setattr("iic_booking.equipment.utilization._now", lambda: _at(TODAY, 23, 59))
    return TODAY


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
        "utilization_period_display": "05 Oct 2026 – 10 Oct 2026",
        "utilization_period_note": "Effective period for utilization: 05 Oct 2026 – 10 Oct 2026 "
                                   "(portal go-live 05 Oct 2026)",
        "portal_go_live_date": "2026-10-05",
    }
    assert utilization_period_caption(period.as_dict()) == period.note()


def test_period_after_go_live_is_unchanged(go_live):
    period = utilization_period("2026-10-06", "2026-10-09")
    assert (period.start, period.end, period.clamped, period.capped) == (date(2026, 10, 6), FRIDAY, False, False)
    assert utilization_period_caption(period.as_dict()) == ""


def test_period_end_is_capped_at_today(go_live):
    period = utilization_period("2026-01-01", "2026-10-31")
    assert (period.start, period.end, period.clamped, period.capped) == (go_live, TODAY, True, True)
    assert period.note() == ("Effective period for utilization: 05 Oct 2026 – 10 Oct 2026 "
                             "(portal go-live 05 Oct 2026; till current date)")


def test_period_entirely_before_go_live_is_empty(go_live):
    period = utilization_period(date(2026, 9, 1), date(2026, 9, 30))
    assert period.is_empty
    assert period.as_dict()["utilization_period_from"] is None
    assert utilization_period_caption(period.as_dict()) == (
        "Effective period for utilization: none (period is before portal go-live, 05 Oct 2026)"
    )


def test_future_period_is_empty(go_live):
    period = utilization_period(date(2026, 11, 1), date(2026, 11, 30))
    assert period.is_empty
    assert period.note() == "Effective period for utilization: none (period has not started yet)"


@pytest.mark.django_db
def test_slot_time_counts_only_up_to_now(egs_factory, monkeypatch):
    f = egs_factory
    eq = f.equipment()
    f.booking(f.student(), eq, _at(MONDAY, 10), slot_count=2)  # 10:00-12:00, half elapsed
    f.slot(eq, _at(MONDAY, 14), minutes=120)  # later today: not counted yet
    monkeypatch.setattr("iic_booking.equipment.utilization._now", lambda: _at(MONDAY, 11))
    result = compute_utilization(eq, MONDAY, MONDAY + timedelta(days=30))
    assert (result["booked_hours"], result["available_hours"], result["utilization_factor"]) == (1.0, 1.0, 1.0)
    assert result["utilization_period_to"] == MONDAY.isoformat()


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


@pytest.mark.django_db
def test_report_cards_share_one_denominator(office_equipment):
    from iic_booking.equipment.models import BookingStatus
    from iic_booking.equipment.reports import get_equipment_report_data

    office_equipment.bookings.filter(daily_slots__date=MONDAY, user__is_test_account=False).update(
        status=BookingStatus.COMPLETED
    )
    data = get_equipment_report_data(MONDAY.isoformat(), SATURDAY.isoformat(), [office_equipment.pk])
    summary, row = data["summary"], data["equipment"][0]
    # Available = booked 2 + no booking 2 + 0.5 (17:00-17:30); the test booking, blocked, holiday and weekend
    # slots and the out-of-window hours are in none of the cards.
    for payload in (summary, row):
        assert payload["utilization_available_hours"] == payload["available_hours_working_window"] == 4.5
        assert payload["utilization_vs_working_capacity"] == round(2.0 / 4.5, 4)
    assert summary["total_hours"] == 4.5
    assert summary["completed_hours_in_working_window"] == row["completed_slot_hours_working_window"] == 2.0
    pie = {p["name"]: p["hours"] for p in data["utilization_pie"]}
    assert pie == {"Utilized (Booked)": 2.0, "No booking": 2.5}
    assert sum(pie.values()) == summary["utilization_available_hours"]
    assert row["available_hours_weekend_or_holiday"] == 4.0  # Tuesday holiday 10-12 + Saturday 10-12


@pytest.fixture
def xps_like(egs_factory, go_live):
    """09:00-21:00 window; unbooked slots before go-live; fully booked working days after it."""
    f = egs_factory
    eq = f.equipment(weekly_view_time_from=time(9, 0), weekly_view_time_to=time(21, 0))
    for day in range(1, 8):  # 28 Sep - 4 Oct: available, never booked (portal not open yet)
        f.slot(eq, _at(go_live - timedelta(days=day), 9), minutes=12 * 60)
    tuesday = go_live + timedelta(days=1)
    monday_booking = f.booking(f.student(), eq, _at(go_live, 9), slot_count=12)  # 09:00-21:00
    monday_booking.status = "COMPLETED"
    monday_booking.save(update_fields=["status"])
    f.slot(eq, _at(go_live, 21), minutes=180, status=SlotStatus.NOT_AVAILABLE)  # outside the window
    f.booking(f.student(), eq, _at(tuesday, 9), slot_count=3)  # 09:00-12:00
    f.slot(eq, _at(tuesday, 12), minutes=9 * 60, status=SlotStatus.NOT_AVAILABLE)
    f.slot(eq, _at(SATURDAY, 9), minutes=12 * 60, status=SlotStatus.NOT_AVAILABLE)
    return eq


@pytest.mark.django_db
def test_report_from_january_counts_only_go_live_to_today(xps_like):
    from iic_booking.equipment.reports import get_equipment_report_data

    data = get_equipment_report_data("2026-01-01", "2026-10-10", [xps_like.pk])
    summary = data["summary"]
    assert summary["utilization_factor"] == 1.0
    assert summary["utilization_booked_hours"] == summary["utilization_available_hours"] == 15.0
    assert summary["available_hours_working_window"] == summary["total_hours"] == 15.0
    assert summary["completed_hours_in_working_window"] == 12.0
    assert summary["utilization_vs_working_capacity"] == 0.8
    assert summary["utilization_period_from"] == "2026-10-05" and summary["utilization_period_to"] == "2026-10-10"
    assert data["date_from"] == "2026-01-01"  # revenue and booking counts keep the requested range
    assert data["report_header"]["utilization_period_note"] == (
        "Effective period for utilization: 05 Oct 2026 – 10 Oct 2026 (portal go-live 05 Oct 2026)"
    )


@pytest.mark.django_db
def test_every_reports_role_gets_the_same_utilization(xps_like, egs_factory):
    from iic_booking.equipment.models import EquipmentManager, EquipmentOperator
    from iic_booking.users.models.user_type import UserType
    from iic_booking.users.tests.factories import UserFactory

    f = egs_factory
    users = {ut: UserFactory(user_type=ut, admin_approved=True, department=f.department)
             for ut in (UserType.ADMIN, UserType.FINANCE, UserType.MANAGER, UserType.OPERATOR)}
    EquipmentManager.objects.create(equipment=xps_like, manager=users[UserType.MANAGER])
    EquipmentOperator.objects.create(equipment=xps_like, operator=users[UserType.OPERATOR])
    keys = ("utilization_factor", "utilization_booked_hours", "utilization_available_hours",
            "available_hours_working_window", "completed_hours_in_working_window", "utilization_vs_working_capacity",
            "utilization_period_from", "utilization_period_to")
    params = {"date_from": "2026-01-01", "date_to": "2026-10-10", "equipment_id": xps_like.pk}
    seen = {}
    for ut, user in users.items():
        res = f.client_for(user).get("/api/admin/equipment-reports/", params)
        assert res.status_code == 200, (ut, res.content[:300])
        seen[ut] = tuple(res.data["summary"][k] for k in keys)
    assert set(seen.values()) == {(1.0, 15.0, 15.0, 15.0, 12.0, 0.8, "2026-10-05", "2026-10-10")}, seen


# --- test data and the Equipment overview ------------------------------------------------------------------------


def _admin_user():
    from iic_booking.users.models.user_type import UserType
    from iic_booking.users.tests.factories import UserFactory

    return UserFactory(user_type=UserType.ADMIN, admin_approved=True)


@pytest.fixture
def test_equipment(egs_factory, go_live):
    """A test-only instrument and one in a category marked as test data, each with an idle slot after go-live."""
    from iic_booking.equipment.models import EquipmentCategory
    from iic_booking.equipment.testdata import forget_marks, mark
    from iic_booking.equipment.testdata_models import TestDataKind

    f = egs_factory
    flagged = f.equipment(visible_to_test_accounts_only=True)
    category = EquipmentCategory.objects.create(name="Sample Category")
    mark(TestDataKind.CATEGORY, category)
    categorised = f.equipment(category=category)
    for eq in (flagged, categorised):
        f.slot(eq, _at(go_live, 10), minutes=120)
    yield flagged, categorised
    forget_marks()


@pytest.mark.django_db
def test_test_equipment_and_test_oics_are_left_out_of_reports(xps_like, test_equipment, egs_factory):
    from iic_booking.equipment.models import EquipmentManager
    from iic_booking.equipment.reports import get_equipment_report_data
    from iic_booking.equipment.utilization import compute_utilization_by_equipment
    from iic_booking.users.models.user_type import UserType
    from iic_booking.users.tests.factories import UserFactory

    flagged, categorised = test_equipment
    tallies = compute_utilization_by_equipment([xps_like.pk, flagged.pk, categorised.pk], "2026-10-01", "2026-10-10")
    assert set(tallies) == {xps_like.pk}

    real_oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    test_oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True, is_test_account=True)
    for oic in (real_oic, test_oic):
        EquipmentManager.objects.create(equipment=xps_like, manager=oic)

    data = get_equipment_report_data("2026-10-01", "2026-10-10")
    rows = {r["equipment_id"]: r for r in data["equipment"]}
    assert flagged.pk not in rows and categorised.pk not in rows
    assert [o["id"] for o in rows[xps_like.pk]["officers_in_charge"]] == [real_oic.pk]
    assert data["summary"]["utilization_available_hours"] == 15.0
    assert get_equipment_report_data("2026-10-01", "2026-10-10", [flagged.pk])["equipment"] == []


@pytest.mark.django_db
def test_equipment_overview_shows_the_report_utilization(xps_like, test_equipment, egs_factory):
    from iic_booking.equipment.reports import get_equipment_report_data

    flagged, _categorised = test_equipment
    res = egs_factory.client_for(_admin_user()).get("/api/admin/insights/equipment/", {"page_size": 100})
    assert res.status_code == 200, res.content[:300]
    body = res.json()
    summary, rows = body["summary"], {r["equipment_id"]: r for r in body["results"]}
    report = get_equipment_report_data("2026-09-11", "2026-10-10", [xps_like.pk])["summary"]
    # Last 30 days from go-live (05 Oct) till now: the same 15 of 15 hours as Reports; test equipment not counted.
    assert summary["utilisation"] == report["utilization_factor"] == 1.0
    assert summary["utilisation_booked_hours"] == summary["utilisation_available_hours"] == 15.0
    assert (summary["utilisation_period_from"], summary["utilisation_period_to"]) == ("2026-10-05", "2026-10-10")
    assert summary["utilisation_period_note"] == (
        "Effective period for utilization: 05 Oct 2026 – 10 Oct 2026 (portal go-live 05 Oct 2026)"
    )
    assert (rows[xps_like.pk]["utilisation"], rows[xps_like.pk]["slot_hours_30d"]) == (1.0, 15.0)
    assert rows[flagged.pk]["utilisation"] is None and rows[flagged.pk]["utilisation_test_excluded"] is True


@pytest.mark.django_db
def test_equipment_overview_counts_modes_on_the_parent_row(egs_factory, go_live):
    f = egs_factory
    parent = f.equipment(enable_multi_mode=True)
    mode = f.equipment(parent_equipment=parent)
    f.booking(f.student(), mode, _at(go_live, 10), slot_count=1)
    f.slot(parent, _at(go_live, 11), minutes=60)
    res = f.client_for(_admin_user()).get("/api/admin/insights/equipment/", {"page_size": 500})
    rows = {r["equipment_id"]: r for r in res.json()["results"]}
    assert rows[parent.pk]["utilisation"] == 0.5
    assert rows[mode.pk]["utilisation"] is None and rows[mode.pk]["utilisation_counted_under"] == parent.pk


@pytest.mark.django_db
def test_equipment_overview_export_profile_type_is_optional(xps_like, egs_factory):
    client = egs_factory.client_for(_admin_user())
    without = client.get("/api/exports/admin-equipment-overview/", {"export_format": "csv"})
    assert without.status_code == 200
    text = without.content.decode("utf-8-sig")
    assert "Profile" not in text and "By profile type" not in text
    with_profile = client.get(
        "/api/exports/admin-equipment-overview/", {"export_format": "csv", "include_profile_type": "1"}
    ).content.decode("utf-8-sig")
    assert "Profile" in with_profile


@pytest.mark.django_db
def test_admin_equipment_list_can_hide_test_equipment(xps_like, test_equipment, egs_factory):
    flagged, categorised = test_equipment
    client = egs_factory.client_for(_admin_user())

    def ids(**params):
        res = client.get("/api/admin/equipment/", {"page_size": 500, **params})
        body = res.json()
        return {e["equipment_id"] for e in (body["results"] if isinstance(body, dict) else body)}

    assert {flagged.pk, categorised.pk} <= ids()
    hidden = ids(test="hide")
    assert xps_like.pk in hidden and not ({flagged.pk, categorised.pk} & hidden)


@pytest.mark.django_db
def test_booking_statistics_leave_out_test_equipment(xps_like, test_equipment, egs_factory):
    from iic_booking.equipment.booking_report_metrics import report_bookings_scope

    flagged, categorised = test_equipment
    f = egs_factory
    f.booking(f.student(), categorised, _at(SATURDAY, 10), slot_count=1)
    qs, scope = report_bookings_scope(_admin_user())
    equipment = set(qs.values_list("equipment_id", flat=True))
    assert xps_like.pk in equipment and categorised.pk not in equipment
