"""Tests for Dynamic Weekly External Slot Quota."""

from datetime import date, datetime, time, timedelta
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from iic_booking.equipment.external_slot_quota import (
    EXTERNAL_QUOTA_SNAPSHOT_LEAD_MINUTES,
    ExternalQuotaValidationResult,
    ExternalSlotQuotaService,
)
from iic_booking.equipment.slot_utils import SlotAvailabilityChecker
from iic_booking.equipment.models import SlotStatus
from iic_booking.users.models.user_type import UserType


class ExternalSlotQuotaMathTests(SimpleTestCase):
    def test_week_bounds_monday_sunday(self):
        # 2026-07-22 is a Wednesday
        start, end = ExternalSlotQuotaService.week_bounds_for_date(date(2026, 7, 22))
        self.assertEqual(start, date(2026, 7, 20))
        self.assertEqual(end, date(2026, 7, 26))

    def test_max_slots_floor_percent(self):
        self.assertEqual(ExternalSlotQuotaService.compute_max_external_slots(20, 20), 4)
        self.assertEqual(ExternalSlotQuotaService.compute_max_external_slots(20, 0), 0)
        self.assertEqual(ExternalSlotQuotaService.compute_max_external_slots(20, 100), 20)
        self.assertEqual(ExternalSlotQuotaService.compute_max_external_slots(21, 20), 4)  # floor
        self.assertEqual(ExternalSlotQuotaService.compute_max_external_slots(19, 20), 3)

    def test_error_payload_shape(self):
        r = ExternalQuotaValidationResult(
            allowed=False,
            message="quota exceeded",
            week_start=date(2026, 7, 20),
            week_end=date(2026, 7, 26),
            total_bookable_slots=20,
            external_quota_percent=20,
            max_external_slots=4,
            external_slots_consumed=4,
            slots_requested=1,
            remaining_external_slots=0,
        )
        payload = r.as_error_payload()
        self.assertEqual(payload["error"], "quota exceeded")
        q = payload["external_slot_quota"]
        self.assertEqual(q["max_external_slots_allowed"], 4)
        self.assertEqual(q["external_slots_already_consumed"], 4)
        self.assertEqual(q["slots_requested"], 1)
        self.assertEqual(q["remaining_external_slots"], 0)


class ExternalSlotWindowBoundsTests(SimpleTestCase):
    @patch("iic_booking.equipment.api_views.get_slot_window_reference_datetime_for_local_week")
    @patch("iic_booking.equipment.api_views.get_equipment_slot_window_reference_config")
    def test_before_reference_opens_w1_only(self, mock_cfg, mock_ref):
        mock_cfg.return_value = (2, time(21, 0))  # Wednesday
        # Local Monday 2026-07-20 10:00; ref is Wed 21:00 same week
        local = timezone.make_aware(datetime(2026, 7, 20, 10, 0, 0))
        mock_ref.return_value = timezone.make_aware(datetime(2026, 7, 22, 21, 0, 0))
        equipment = MagicMock()
        with patch("django.utils.timezone.now", return_value=local):
            with patch("django.utils.timezone.localtime", return_value=local):
                min_d, max_d, before = ExternalSlotQuotaService.get_external_slot_window_date_bounds(
                    equipment, at=local
                )
        self.assertTrue(before)
        self.assertEqual(min_d, date(2026, 7, 27))  # W1 Monday
        self.assertEqual(max_d, date(2026, 8, 2))  # W1 Sunday

    @patch("iic_booking.equipment.api_views.get_slot_window_reference_datetime_for_local_week")
    @patch("iic_booking.equipment.api_views.get_equipment_slot_window_reference_config")
    def test_after_reference_opens_w1_and_w2(self, mock_cfg, mock_ref):
        mock_cfg.return_value = (2, time(21, 0))
        local = timezone.make_aware(datetime(2026, 7, 22, 21, 30, 0))
        mock_ref.return_value = timezone.make_aware(datetime(2026, 7, 22, 21, 0, 0))
        equipment = MagicMock()
        with patch("django.utils.timezone.localtime", return_value=local):
            min_d, max_d, before = ExternalSlotQuotaService.get_external_slot_window_date_bounds(
                equipment, at=local
            )
        self.assertFalse(before)
        self.assertEqual(min_d, date(2026, 7, 27))
        self.assertEqual(max_d, date(2026, 8, 9))  # W2 Sunday


class ExternalSlotAvailabilityTests(SimpleTestCase):
    def test_external_available_no_longer_requires_reserved(self):
        slot = MagicMock()
        slot.start_datetime = timezone.now() + timedelta(hours=2)
        slot.status = SlotStatus.AVAILABLE
        slot.reserved_for_external = False
        self.assertTrue(SlotAvailabilityChecker.is_slot_available_for_external(slot))

    def test_external_past_slot_still_not_bookable(self):
        slot = MagicMock()
        slot.start_datetime = timezone.now() - timedelta(minutes=5)
        slot.status = SlotStatus.AVAILABLE
        slot.reserved_for_external = False
        self.assertFalse(SlotAvailabilityChecker.is_slot_available_for_external(slot))


class ExternalQuotaValidateUnitTests(SimpleTestCase):
    def test_internal_user_always_allowed(self):
        user = MagicMock()
        user.user_type = UserType.FACULTY
        equipment = MagicMock(external_slot_quota_percent=0)
        result = ExternalSlotQuotaService.validate_external_booking(
            user,
            equipment,
            slot_dates=[date(2026, 7, 27)],
            slots_requested=5,
        )
        self.assertTrue(result.allowed)

    def test_bypass_urgent_hold(self):
        user = MagicMock()
        user.user_type = UserType.EXTERNAL
        equipment = MagicMock(external_slot_quota_percent=0)
        result = ExternalSlotQuotaService.validate_external_booking(
            user,
            equipment,
            slot_dates=[date(2026, 7, 27)],
            slots_requested=5,
            bypass=True,
        )
        self.assertTrue(result.allowed)

    @patch.object(ExternalSlotQuotaService, "count_external_usage", return_value=0)
    @patch.object(ExternalSlotQuotaService, "count_bookable_slots", return_value=20)
    def test_zero_percent_rejects_external(self, _bookable, _usage):
        from iic_booking.equipment.models import ExternalWeeklySlotQuotaSnapshot

        user = MagicMock()
        user.user_type = UserType.EXTERNAL
        equipment = MagicMock(external_slot_quota_percent=0, pk=1, equipment_id=1)

        snap = MagicMock()
        snap.max_external_slots = 0
        snap.total_bookable_slots = 20
        snap.external_quota_percent = 0
        snap.pk = 1

        qs = MagicMock()
        qs.filter.return_value = qs
        qs.select_for_update.return_value = qs
        qs.first.return_value = None
        qs.get.return_value = snap

        with patch.object(ExternalWeeklySlotQuotaSnapshot.objects, "select_for_update", return_value=qs):
            with patch.object(ExternalWeeklySlotQuotaSnapshot.objects, "filter", return_value=qs):
                with patch.object(ExternalWeeklySlotQuotaSnapshot.objects, "create", return_value=snap):
                    with patch("django.db.transaction.atomic"):
                        result = ExternalSlotQuotaService.validate_external_booking(
                            user,
                            equipment,
                            slot_dates=[date(2026, 7, 27)],
                            slots_requested=1,
                        )
        self.assertFalse(result.allowed)
        self.assertIn("quota exceeded", (result.message or "").lower())

    @patch.object(ExternalSlotQuotaService, "count_external_usage", return_value=4)
    def test_rejects_when_usage_plus_request_exceeds_max(self, _usage):
        from iic_booking.equipment.models import ExternalWeeklySlotQuotaSnapshot

        user = MagicMock()
        user.user_type = UserType.EXTERNAL
        equipment = MagicMock(external_slot_quota_percent=20, pk=1, equipment_id=1)

        snap = MagicMock()
        snap.max_external_slots = 4
        snap.total_bookable_slots = 20
        snap.external_quota_percent = 20
        snap.pk = 1

        qs = MagicMock()
        qs.filter.return_value = qs
        qs.select_for_update.return_value = qs
        qs.first.return_value = snap

        with patch.object(ExternalWeeklySlotQuotaSnapshot.objects, "select_for_update", return_value=qs):
            with patch.object(ExternalWeeklySlotQuotaSnapshot.objects, "filter", return_value=qs):
                with patch("django.db.transaction.atomic"):
                    result = ExternalSlotQuotaService.validate_external_booking(
                        user,
                        equipment,
                        slot_dates=[date(2026, 7, 27)],
                        slots_requested=1,
                    )
        self.assertFalse(result.allowed)
        self.assertEqual(result.external_slots_consumed, 4)
        self.assertEqual(result.remaining_external_slots, 0)

    @patch.object(ExternalSlotQuotaService, "count_external_usage", return_value=2)
    def test_allows_when_under_max(self, _usage):
        from iic_booking.equipment.models import ExternalWeeklySlotQuotaSnapshot

        user = MagicMock()
        user.user_type = UserType.EXTERNAL
        equipment = MagicMock(external_slot_quota_percent=20, pk=1, equipment_id=1)

        snap = MagicMock()
        snap.max_external_slots = 4
        snap.total_bookable_slots = 20
        snap.external_quota_percent = 20
        snap.pk = 1

        qs = MagicMock()
        qs.filter.return_value = qs
        qs.select_for_update.return_value = qs
        qs.first.return_value = snap

        with patch.object(ExternalWeeklySlotQuotaSnapshot.objects, "select_for_update", return_value=qs):
            with patch.object(ExternalWeeklySlotQuotaSnapshot.objects, "filter", return_value=qs):
                with patch("django.db.transaction.atomic"):
                    result = ExternalSlotQuotaService.validate_external_booking(
                        user,
                        equipment,
                        slot_dates=[date(2026, 7, 27)],
                        slots_requested=2,
                    )
        self.assertTrue(result.allowed)
        self.assertEqual(result.remaining_external_slots, 0)

    def test_cross_week_rejected(self):
        user = MagicMock()
        user.user_type = UserType.EXTERNAL
        equipment = MagicMock(external_slot_quota_percent=50)
        result = ExternalSlotQuotaService.validate_external_booking(
            user,
            equipment,
            slot_dates=[date(2026, 7, 27), date(2026, 8, 3)],  # two Mondays
            slots_requested=2,
        )
        self.assertFalse(result.allowed)
        self.assertIn("single booking week", result.message or "")


class GenerateDueSnapshotsWindowTests(SimpleTestCase):
    def test_lead_minutes_constant(self):
        self.assertEqual(EXTERNAL_QUOTA_SNAPSHOT_LEAD_MINUTES, 15)
