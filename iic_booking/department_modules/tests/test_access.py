from datetime import datetime, timedelta, timezone as dt_timezone

from iic_booking.department_modules.access import Cell

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=dt_timezone.utc)
BEFORE = T0 - timedelta(hours=1)
AFTER = T0 + timedelta(hours=1)


def test_unconfigured_allows_everything():
    assert Cell().allows() and Cell().allows(started_at=AFTER)


def test_off_blocks_new_and_allows_work_started_before():
    off = Cell(enabled=False, disabled_at=T0, configured=True)
    assert not off.allows()
    assert not off.allows(is_test=True)
    assert not off.allows(started_at=AFTER)
    assert off.allows(started_at=BEFORE)


def test_off_without_cutoff_blocks_everything():
    assert not Cell(enabled=False, configured=True).allows(started_at=BEFORE)


def test_test_users_only():
    pilot = Cell(enabled=True, test_users_only=True, test_only_since=T0, configured=True)
    assert pilot.allows(is_test=True)
    assert not pilot.allows()
    assert not pilot.allows(started_at=AFTER)
    assert pilot.allows(started_at=BEFORE)


def test_off_and_test_only_combine():
    cell = Cell(enabled=False, test_users_only=True, disabled_at=T0, test_only_since=T0 - timedelta(days=1),
                configured=True)
    two_days = T0 - timedelta(days=2)
    half_day = T0 - timedelta(hours=12)
    assert cell.allows(started_at=two_days)  # before both restrictions
    assert not cell.allows(started_at=half_day)  # already test-only then
    assert cell.allows(started_at=half_day, is_test=True)
    assert not cell.allows(started_at=AFTER, is_test=True)
