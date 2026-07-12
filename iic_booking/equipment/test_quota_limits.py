"""Quota limit tests: period boundaries, group/equipment quotas, skip flags."""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase, override_settings
from django.utils import timezone

from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    Equipment,
    EquipmentGroup,
    EquipmentGroupQuota,
    ExternalUserQuota,
    QuotaLimitType,
    QuotaType,
    SlotMaster,
    SlotStatus,
    UserTypeQuota,
)
from iic_booking.equipment.quota_utils import (
    QuotaChecker,
    booking_quota_should_skip,
)
from iic_booking.users.models import User
from iic_booking.users.models.user_type import UserType


IST = ZoneInfo("Asia/Kolkata")


def _aware(year, month, day, hour=10, minute=0):
    return timezone.make_aware(datetime(year, month, day, hour, minute), IST)


class QuotaPeriodBoundaryTests(TestCase):
    def test_monthly_period_uses_ist_not_utc(self):
        # 00:30 IST Aug 1 == 19:00 UTC Jul 31
        ref = datetime(2026, 8, 1, 0, 30, tzinfo=IST)
        start, end = QuotaChecker._get_quota_period(QuotaType.MONTHLY, ref)
        self.assertEqual(start.date().isoformat(), "2026-08-01")
        self.assertEqual(end.date().isoformat(), "2026-08-31")

    def test_weekly_sunday_start_in_ist(self):
        # Sunday 00:30 IST (2026-08-02) is still Saturday UTC
        ref = datetime(2026, 8, 2, 0, 30, tzinfo=IST)
        start, end = QuotaChecker._get_quota_period(QuotaType.WEEKLY, ref)
        self.assertEqual(start.date().isoformat(), "2026-08-02")  # Sunday
        self.assertEqual(end.date().isoformat(), "2026-08-08")  # Saturday


class GroupQuotaLimitTests(TestCase):
    def setUp(self):
        self.group = EquipmentGroup.objects.create(name="Quota Test Group")
        self.equipment = Equipment.objects.create(
            name="Quota EQ",
            code="QEQ",
            equipment_group=self.group,
            slot_duration_minutes=60,
            skip_quota_check=False,
        )
        EquipmentGroupQuota.objects.create(
            equipment_group=self.group,
            quota_type=QuotaType.WEEKLY,
            internal_individual_quota_minutes=120,
            internal_faculty_quota_minutes=240,
            external_individual_quota_minutes=60,
            external_faculty_quota_minutes=120,
            is_enforced=True,
        )
        self.charge_profile = ChargeProfile.objects.create(
            equipment=self.equipment,
            user_type=UserType.STUDENT,
            primary_unit_charge=Decimal("10.00"),
            secondary_unit_charge=Decimal("0"),
            breakpoint=Decimal("99"),
        )
        self.slot_master = SlotMaster.objects.create(
            equipment=self.equipment,
            slot_number=1,
            slot_name="S1",
            open_time=datetime.strptime("09:00", "%H:%M").time(),
            close_time=datetime.strptime("10:00", "%H:%M").time(),
            is_active=True,
        )
        self.student = User.objects.create_user(
            email="quota.student@test.local",
            password="x",
            user_type=UserType.STUDENT,
        )
        self.external = User.objects.create_user(
            email="quota.external@test.local",
            password="x",
            user_type=UserType.EXTERNAL,
        )

    def _book(self, user, *, minutes, day, user_type):
        start = _aware(day.year, day.month, day.day, 9, 0)
        end = start + timedelta(minutes=minutes)
        booking = Booking.objects.create(
            user=user,
            equipment=self.equipment,
            charge_profile=self.charge_profile,
            user_type_snapshot=user_type,
            total_time_minutes=minutes,
            total_charge=Decimal("10.00"),
            status=BookingStatus.BOOKED,
            quota_period_anchor_at=start,
        )
        DailySlot.objects.create(
            slot_master=self.slot_master,
            date=day,
            start_datetime=start,
            end_datetime=end,
            status=SlotStatus.BOOKED,
            booking=booking,
        )
        return booking

    def test_weekly_allows_within_individual_limit(self):
        day = timezone.localdate()
        # Align to a weekday inside current IST week by using "today"
        self._book(self.student, minutes=60, day=day, user_type=UserType.STUDENT)
        ok, err = QuotaChecker.check_user_quota(
            self.student,
            self.equipment,
            QuotaType.WEEKLY,
            additional_time_minutes=60,
            booking_date=_aware(day.year, day.month, day.day, 11),
        )
        self.assertTrue(ok, err)

    def test_weekly_blocks_when_individual_limit_exceeded(self):
        day = timezone.localdate()
        self._book(self.student, minutes=90, day=day, user_type=UserType.STUDENT)
        ok, err = QuotaChecker.check_user_quota(
            self.student,
            self.equipment,
            QuotaType.WEEKLY,
            additional_time_minutes=60,
            booking_date=_aware(day.year, day.month, day.day, 11),
        )
        self.assertFalse(ok)
        self.assertIn("quota exceeded", (err or "").lower())

    def test_external_weekly_limit(self):
        day = timezone.localdate()
        self._book(self.external, minutes=60, day=day, user_type=UserType.EXTERNAL)
        ok, err = QuotaChecker.check_user_quota(
            self.external,
            self.equipment,
            QuotaType.WEEKLY,
            additional_time_minutes=30,
            booking_date=_aware(day.year, day.month, day.day, 11),
        )
        self.assertFalse(ok, "60 used + 30 new should exceed external weekly 60")
        self.assertIn("quota exceeded", (err or "").lower())

    def test_unenforced_group_quota_allows(self):
        EquipmentGroupQuota.objects.filter(equipment_group=self.group).update(is_enforced=False)
        day = timezone.localdate()
        ok, err = QuotaChecker.check_user_quota(
            self.student,
            self.equipment,
            QuotaType.WEEKLY,
            additional_time_minutes=9999,
            booking_date=_aware(day.year, day.month, day.day, 11),
        )
        self.assertTrue(ok, err)


class EquipmentLevelQuotaTests(TestCase):
    def setUp(self):
        self.equipment = Equipment.objects.create(
            name="Solo EQ",
            code="SOLO",
            slot_duration_minutes=60,
            skip_quota_check=False,
        )
        self.charge_profile = ChargeProfile.objects.create(
            equipment=self.equipment,
            user_type=UserType.STUDENT,
            primary_unit_charge=Decimal("10.00"),
            secondary_unit_charge=Decimal("0"),
            breakpoint=Decimal("99"),
        )
        self.slot_master = SlotMaster.objects.create(
            equipment=self.equipment,
            slot_number=1,
            slot_name="S1",
            open_time=datetime.strptime("09:00", "%H:%M").time(),
            close_time=datetime.strptime("10:00", "%H:%M").time(),
            is_active=True,
        )
        self.student = User.objects.create_user(
            email="solo.student@test.local",
            password="x",
            user_type=UserType.STUDENT,
        )
        UserTypeQuota.objects.create(
            equipment=self.equipment,
            user_type=UserType.STUDENT,
            quota_type=QuotaType.MONTHLY,
            limit_type=QuotaLimitType.HOURS,
            limit_value=Decimal("60"),  # minutes in this codebase
            is_enforced=True,
        )
        UserTypeQuota.objects.create(
            equipment=self.equipment,
            user_type=UserType.STUDENT,
            quota_type=QuotaType.WEEKLY,
            limit_type=QuotaLimitType.BOOKINGS,
            limit_value=Decimal("1"),
            is_enforced=True,
        )

    def _book(self, *, minutes, day):
        start = _aware(day.year, day.month, day.day, 9, 0)
        end = start + timedelta(minutes=minutes)
        booking = Booking.objects.create(
            user=self.student,
            equipment=self.equipment,
            charge_profile=self.charge_profile,
            user_type_snapshot=UserType.STUDENT,
            total_time_minutes=minutes,
            total_charge=Decimal("10.00"),
            status=BookingStatus.BOOKED,
            quota_period_anchor_at=start,
        )
        DailySlot.objects.create(
            slot_master=self.slot_master,
            date=day,
            start_datetime=start,
            end_datetime=end,
            status=SlotStatus.BOOKED,
            booking=booking,
        )

    def test_monthly_hours_quota_blocks(self):
        day = timezone.localdate()
        self._book(minutes=60, day=day)
        ok, err = QuotaChecker.check_user_quota(
            self.student,
            self.equipment,
            QuotaType.MONTHLY,
            additional_time_minutes=1,
            booking_date=_aware(day.year, day.month, day.day, 12),
        )
        self.assertFalse(ok)
        self.assertIn("quota exceeded", (err or "").lower())

    def test_weekly_bookings_count_quota_blocks(self):
        day = timezone.localdate()
        self._book(minutes=30, day=day)
        ok, err = QuotaChecker.check_user_quota(
            self.student,
            self.equipment,
            QuotaType.WEEKLY,
            additional_bookings=1,
            booking_date=_aware(day.year, day.month, day.day, 12),
        )
        self.assertFalse(ok)
        self.assertIn("bookings", (err or "").lower())


class ExternalEquipmentQuotaTests(TestCase):
    def setUp(self):
        self.equipment = Equipment.objects.create(
            name="Ext EQ",
            code="EXTQ",
            slot_duration_minutes=60,
        )
        ExternalUserQuota.objects.create(
            equipment=self.equipment,
            quota_type=QuotaType.WEEKLY,
            limit_type=QuotaLimitType.CHARGE,
            limit_value=Decimal("100.00"),
            is_enforced=True,
        )
        self.charge_profile = ChargeProfile.objects.create(
            equipment=self.equipment,
            user_type=UserType.EXTERNAL,
            primary_unit_charge=Decimal("50.00"),
            secondary_unit_charge=Decimal("0"),
            breakpoint=Decimal("99"),
        )
        self.slot_master = SlotMaster.objects.create(
            equipment=self.equipment,
            slot_number=1,
            slot_name="S1",
            open_time=datetime.strptime("09:00", "%H:%M").time(),
            close_time=datetime.strptime("10:00", "%H:%M").time(),
            is_active=True,
        )
        self.user = User.objects.create_user(
            email="ext.charge@test.local",
            password="x",
            user_type=UserType.EXTERNAL,
        )

    def test_charge_quota_blocks(self):
        day = timezone.localdate()
        start = _aware(day.year, day.month, day.day, 9, 0)
        booking = Booking.objects.create(
            user=self.user,
            equipment=self.equipment,
            charge_profile=self.charge_profile,
            user_type_snapshot=UserType.EXTERNAL,
            total_time_minutes=60,
            total_charge=Decimal("80.00"),
            status=BookingStatus.BOOKED,
            quota_period_anchor_at=start,
        )
        DailySlot.objects.create(
            slot_master=self.slot_master,
            date=day,
            start_datetime=start,
            end_datetime=start + timedelta(hours=1),
            status=SlotStatus.BOOKED,
            booking=booking,
        )
        ok, err = QuotaChecker.check_user_quota(
            self.user,
            self.equipment,
            QuotaType.WEEKLY,
            additional_charge=Decimal("30.00"),
            booking_date=start,
        )
        self.assertFalse(ok)
        self.assertIn("quota exceeded", (err or "").lower())


class QuotaSkipFlagTests(TestCase):
    def setUp(self):
        self.equipment = Equipment.objects.create(
            name="Skip EQ",
            code="SKIP",
            skip_quota_check=True,
        )

    @override_settings(SKIP_BOOKING_QUOTA_CHECK=False)
    def test_per_equipment_skip(self):
        self.assertTrue(booking_quota_should_skip(self.equipment))

    @override_settings(SKIP_BOOKING_QUOTA_CHECK=True)
    def test_global_skip(self):
        eq = Equipment.objects.create(name="NoSkip", code="NOSKIP", skip_quota_check=False)
        self.assertTrue(booking_quota_should_skip(eq))

    @override_settings(SKIP_BOOKING_QUOTA_CHECK=False)
    def test_enforced_when_neither_skip(self):
        eq = Equipment.objects.create(name="Enforce", code="ENF", skip_quota_check=False)
        self.assertFalse(booking_quota_should_skip(eq))
