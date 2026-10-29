"""The central external booking window: date rules with an explicit clock (no database)."""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from iic_booking.external_services.models import ExternalBookingWindowConfig
from iic_booking.external_services.window import (
    ExternalWindowClosed,
    check_dates,
    compute,
    count_instants,
    date_opens_at,
    first_instant_after,
)

IST = ZoneInfo("Asia/Kolkata")


def ist(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=IST)


def policy(**overrides) -> ExternalBookingWindowConfig:
    """The schedule given by IIC: opening Wed 28 Oct 2026 21:00 IST, weeks from Mon 9 Nov, 4 weeks, Wed 21:00."""
    values = dict(
        enabled=True,
        initial_open_at=ist(2026, 10, 28, 21),
        first_week_start=date(2026, 11, 9),
        initial_weeks=4,
        auto_expand=True,
        expansion_weekday=2,
        expansion_time=time(21, 0),
        materialized_expansions=0,
        counting_from=None,
        manual_weeks=0,
        min_lead_days=0,
    )
    values.update(overrides)
    return ExternalBookingWindowConfig(**values)


@pytest.mark.parametrize(
    "at, first, last, is_open",
    [
        (ist(2026, 10, 28, 20, 59, 59), None, None, False),  # one second before the opening
        (ist(2026, 10, 28, 21, 0, 0), date(2026, 11, 9), date(2026, 12, 6), True),  # exactly at the opening
        (ist(2026, 11, 4, 20, 59, 59), date(2026, 11, 9), date(2026, 12, 6), True),
        (ist(2026, 11, 4, 21, 0, 0), date(2026, 11, 9), date(2026, 12, 13), True),  # expansion 1: 7-13 Dec
        (ist(2026, 11, 11, 21, 0, 0), date(2026, 11, 11), date(2026, 12, 20), True),  # past days drop off
        (ist(2026, 11, 18, 21, 0, 0), date(2026, 11, 18), date(2026, 12, 27), True),
        (ist(2026, 11, 25, 21, 0, 0), date(2026, 11, 25), date(2027, 1, 3), True),  # year end
        (ist(2026, 12, 2, 21, 0, 0), date(2026, 12, 2), date(2027, 1, 10), True),
    ],
)
def test_boundary_table(at, first, last, is_open):
    w = compute(policy(), at)
    assert (w.is_open, w.first_date, w.last_date) == (is_open, first, last)


def test_the_week_before_the_first_week_is_never_open():
    for at in (ist(2026, 10, 28, 21), ist(2026, 11, 2, 9), ist(2026, 11, 8, 23, 59)):
        w = compute(policy(), at)
        for d in (date(2026, 11, 2), date(2026, 11, 5), date(2026, 11, 8)):
            assert not w.contains(d)
    assert compute(policy(), ist(2026, 10, 30, 12)).contains(date(2026, 11, 9))


def test_disabled_policy_reports_disabled_and_checks_nothing():
    w = compute(policy(enabled=False), ist(2026, 11, 20, 12))
    assert not w.enabled and not w.is_open
    assert check_dates([date(2030, 1, 1)], window=w) is w  # no effect while disabled


def test_utc_clock_is_converted_to_ist():
    # 15:30 UTC is 21:00 IST on the opening day.
    at = datetime(2026, 10, 28, 15, 30, tzinfo=ZoneInfo("UTC"))
    assert compute(policy(), at).last_date == date(2026, 12, 6)
    assert not compute(policy(), at - timedelta(seconds=1)).is_open


def test_next_expansion_and_next_week():
    w = compute(policy(), ist(2026, 11, 4, 21, 0, 0))
    assert w.next_expansion_at == ist(2026, 11, 11, 21)
    assert w.next_week_start == date(2026, 12, 14)
    before = compute(policy(), ist(2026, 11, 4, 20, 0))
    assert before.next_expansion_at == ist(2026, 11, 4, 21)


def test_count_instants_is_half_open_and_calendar_based():
    wed = 2
    assert count_instants(ist(2026, 10, 28, 21), ist(2026, 10, 28, 21), wed, time(21)) == 0
    assert count_instants(ist(2026, 10, 28, 21), ist(2026, 11, 4, 21), wed, time(21)) == 1
    assert count_instants(ist(2026, 10, 28, 21), ist(2026, 11, 4, 20, 59), wed, time(21)) == 0
    assert count_instants(ist(2026, 10, 28, 21), ist(2027, 1, 6, 21), wed, time(21)) == 10
    assert first_instant_after(ist(2026, 12, 30, 21), wed, time(21)) == ist(2027, 1, 6, 21)


def test_a_missed_scheduled_run_does_not_change_the_window():
    # The range depends only on the clock: whatever ran before, three weeks after opening it is the same.
    w = compute(policy(), ist(2026, 11, 19, 3))
    assert w.last_date == date(2026, 12, 27)
    assert w.expansions == 3


def test_paused_expansions_are_not_caught_up():
    # Paused on 10 Nov after one expansion; resumed 20 Nov: 11 and 18 Nov are skipped.
    paused = policy(auto_expand=False, materialized_expansions=1, counting_from=ist(2026, 11, 10, 10))
    assert compute(paused, ist(2026, 11, 19, 12)).last_date == date(2026, 12, 13)
    resumed = policy(materialized_expansions=1, counting_from=ist(2026, 11, 20, 10))
    assert compute(resumed, ist(2026, 11, 25, 20, 59)).last_date == date(2026, 12, 13)
    assert compute(resumed, ist(2026, 11, 25, 21)).last_date == date(2026, 12, 20)


def test_manual_weeks_add_to_the_schedule():
    w = compute(policy(manual_weeks=1), ist(2026, 11, 5, 9))
    assert w.last_date == date(2026, 12, 20)
    assert compute(policy(manual_weeks=1), ist(2026, 11, 11, 21)).last_date == date(2026, 12, 27)


def test_min_lead_days_moves_the_first_date_only():
    w = compute(policy(min_lead_days=7), ist(2026, 11, 20, 10))
    assert w.first_date == date(2026, 11, 27)
    assert w.last_date == date(2026, 12, 27)


def test_backdated_opening_counts_missed_expansions():
    w = compute(policy(), ist(2026, 12, 1, 9))
    assert w.expansions == 4 and w.last_date == date(2027, 1, 3)


def test_check_dates_raises_with_payload():
    w = compute(policy(), ist(2026, 10, 30, 12))
    with pytest.raises(ExternalWindowClosed) as exc:
        check_dates([date(2026, 11, 5)], window=w)
    payload = exc.value.payload()
    assert payload["code"] == "EXTERNAL_WINDOW_CLOSED"
    assert payload["dates_outside_window"] == ["2026-11-05"]
    assert payload["external_window"]["last_date"] == "2026-12-06"
    assert "special booking request" in payload["error"]
    check_dates([date(2026, 11, 9), date(2026, 12, 6)], window=w)  # both ends are inclusive


def test_before_opening_every_date_is_refused():
    w = compute(policy(), ist(2026, 10, 27, 12))
    with pytest.raises(ExternalWindowClosed) as exc:
        check_dates([date(2026, 11, 10)], window=w)
    assert "not open yet" in exc.value.message


def test_date_opens_at():
    w = compute(policy(), ist(2026, 11, 1, 12))
    assert date_opens_at(w, date(2026, 11, 20)) is None  # already open
    assert date_opens_at(w, date(2026, 12, 7)) == ist(2026, 11, 4, 21)
    assert date_opens_at(w, date(2026, 12, 20)) == ist(2026, 11, 11, 21)
    before = compute(policy(), ist(2026, 10, 27, 12))
    assert date_opens_at(before, date(2026, 11, 12)) == ist(2026, 10, 28, 21)
    assert date_opens_at(before, date(2026, 11, 3)) is None  # the excluded week never opens


def test_enabled_but_unconfigured_fails_closed():
    w = compute(policy(initial_open_at=None), ist(2026, 11, 20, 12))
    assert w.enabled and not w.is_open
    with pytest.raises(ExternalWindowClosed):
        check_dates([date(2026, 11, 25)], window=w)
