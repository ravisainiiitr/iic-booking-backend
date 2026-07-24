"""Unit tests for working-day aware sample submission deadlines."""

from datetime import date, datetime, time, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings
from django.utils import timezone

from iic_booking.equipment.sample_submission_deadline_reminders import (
    adjust_datetime_to_previous_working_day,
    compute_sample_submission_deadline,
)


class AdjustDatetimeToPreviousWorkingDayTests(SimpleTestCase):
    @override_settings(TIME_ZONE="Asia/Kolkata", USE_TZ=True)
    def test_weekday_unchanged(self):
        # Wednesday 10:00 IST
        dt = timezone.make_aware(datetime(2026, 7, 22, 10, 0, 0))
        with patch(
            "iic_booking.equipment.models.Holiday.is_holiday",
            return_value=(False, None),
        ):
            out = adjust_datetime_to_previous_working_day(dt)
        self.assertEqual(timezone.localtime(out).date(), date(2026, 7, 22))
        self.assertEqual(timezone.localtime(out).time(), time(10, 0))

    @override_settings(TIME_ZONE="Asia/Kolkata", USE_TZ=True)
    def test_sunday_moves_to_friday(self):
        # Sunday 10:00 → Friday 10:00 (Sat+Sun non-working)
        dt = timezone.make_aware(datetime(2026, 7, 26, 10, 0, 0))  # Sunday

        def _is_holiday(d):
            if d.weekday() >= 5:
                return True, "Weekend"
            return False, None

        with patch("iic_booking.equipment.models.Holiday.is_holiday", side_effect=_is_holiday):
            out = adjust_datetime_to_previous_working_day(dt)
        local = timezone.localtime(out)
        self.assertEqual(local.date(), date(2026, 7, 24))  # Friday
        self.assertEqual(local.time(), time(10, 0))

    @override_settings(TIME_ZONE="Asia/Kolkata", USE_TZ=True)
    def test_holiday_chain_walks_back(self):
        # Tuesday holiday, Monday holiday → Friday
        dt = timezone.make_aware(datetime(2026, 7, 21, 10, 0, 0))  # Tuesday

        def _is_holiday(d):
            if d in {date(2026, 7, 21), date(2026, 7, 20)} or d.weekday() >= 5:
                return True, "Holiday"
            return False, None

        with patch("iic_booking.equipment.models.Holiday.is_holiday", side_effect=_is_holiday):
            out = adjust_datetime_to_previous_working_day(dt)
        local = timezone.localtime(out)
        self.assertEqual(local.date(), date(2026, 7, 17))  # Friday
        self.assertEqual(local.time(), time(10, 0))


class ComputeSampleSubmissionDeadlineTests(SimpleTestCase):
    @override_settings(TIME_ZONE="Asia/Kolkata", USE_TZ=True)
    def test_24h_before_monday_lands_on_friday(self):
        monday_start = timezone.make_aware(datetime(2026, 7, 27, 10, 0, 0))  # Monday
        booking = SimpleNamespace(
            atmosphere_sensitive_sample=False,
            equipment=SimpleNamespace(sample_submission_lead_hours=24),
        )

        def _is_holiday(d):
            if d.weekday() >= 5:
                return True, "Weekend"
            return False, None

        with patch(
            "iic_booking.equipment.serializers._booking_slot_bounds",
            return_value=(monday_start, monday_start + timedelta(hours=1)),
        ), patch(
            "iic_booking.equipment.models.Holiday.is_holiday",
            side_effect=_is_holiday,
        ):
            deadline = compute_sample_submission_deadline(booking)

        self.assertIsNotNone(deadline)
        local = timezone.localtime(deadline)
        # Sunday 10:00 raw → Friday 10:00
        self.assertEqual(local.date(), date(2026, 7, 24))
        self.assertEqual(local.time(), time(10, 0))
