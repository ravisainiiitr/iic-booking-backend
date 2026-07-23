"""Quota limit tests: Mon–Sun week, Faculty→Individual order, exclusions, urgent bypass."""

from datetime import datetime, timedelta
from decimal import Decimal
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
    QuotaService,
    booking_quota_should_skip,
)
from iic_booking.users.models import User
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus


IST = ZoneInfo("Asia/Kolkata")


def _aware(year, month, day, hour=10, minute=0):
    return timezone.make_aware(datetime(year, month, day, hour, minute), IST)


class QuotaPeriodBoundaryTests(TestCase):
    def test_monthly_period_uses_ist_not_utc(self):
        # 00:30 IST Aug 1 == 19:00 UTC Jul 31
        ref = datetime(2026, 8, 1, 0, 30, tzinfo=IST)
        start, end = QuotaService._get_quota_period(QuotaType.MONTHLY, ref)
        self.assertEqual(start.date().isoformat(), "2026-08-01")
        self.assertEqual(end.date().isoformat(), "2026-08-31")

    def test_weekly_monday_through_sunday(self):
        # Sunday 00:30 IST 2026-08-02 → week Mon 2026-07-27 … Sun 2026-08-02
        ref = datetime(2026, 8, 2, 0, 30, tzinfo=IST)
        start, end = QuotaService._get_quota_period(QuotaType.WEEKLY, ref)
        self.assertEqual(start.date().isoformat(), "2026-07-27")  # Monday
        self.assertEqual(end.date().isoformat(), "2026-08-02")  # Sunday

    def test_weekly_wednesday_same_week(self):
        # Wed 2026-07-29 → same Mon–Sun week
        ref = datetime(2026, 7, 29, 12, 0, tzinfo=IST)
        start, end = QuotaService._get_quota_period(QuotaType.WEEKLY, ref)
        self.assertEqual(start.date().isoformat(), "2026-07-27")
        self.assertEqual(end.date().isoformat(), "2026-08-02")


@override_settings(SKIP_BOOKING_QUOTA_CHECK=False)
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
        EquipmentGroupQuota.objects.create(
            equipment_group=self.group,
            quota_type=QuotaType.MONTHLY,
            internal_individual_quota_minutes=300,
            internal_faculty_quota_minutes=480,
            external_individual_quota_minutes=120,
            external_faculty_quota_minutes=240,
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
        self.faculty = User.objects.create_user(
            email="quota.faculty@test.local",
            password="x",
            user_type=UserType.FACULTY,
        )
        self.faculty_wallet = Wallet.objects.create(user=self.faculty)
        WalletJoinRequest.objects.create(
            student=self.student,
            faculty=self.faculty,
            wallet=self.faculty_wallet,
            status=WalletJoinRequestStatus.APPROVED,
        )

    def _book(
        self,
        user,
        *,
        minutes,
        day,
        user_type,
        status=BookingStatus.BOOKED,
        source_booking=None,
        slot_hour=9,
        create_slot=True,
    ):
        start = _aware(day.year, day.month, day.day, slot_hour, 0)
        end = start + timedelta(minutes=minutes)
        booking = Booking.objects.create(
            user=user,
            equipment=self.equipment,
            charge_profile=self.charge_profile,
            user_type_snapshot=user_type,
            total_time_minutes=minutes,
            total_charge=Decimal("10.00"),
            status=status,
            quota_period_anchor_at=start,
            source_booking=source_booking,
        )
        if create_slot:
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
        self._book(self.student, minutes=60, day=day, user_type=UserType.STUDENT)
        ok, err = QuotaService.validate_booking_quota(
            self.student,
            self.equipment,
            additional_time_minutes=60,
            booking_date=_aware(day.year, day.month, day.day, 11),
        )
        self.assertTrue(ok, err)

    def test_weekly_blocks_when_individual_limit_exceeded(self):
        day = timezone.localdate()
        self._book(self.student, minutes=90, day=day, user_type=UserType.STUDENT)
        ok, err = QuotaService.validate_booking_quota(
            self.student,
            self.equipment,
            additional_time_minutes=60,
            booking_date=_aware(day.year, day.month, day.day, 11),
        )
        self.assertFalse(ok)
        self.assertIn("individual weekly", (err or "").lower())
        self.assertIn("quota exceeded", (err or "").lower())

    def test_external_weekly_limit(self):
        day = timezone.localdate()
        self._book(self.external, minutes=60, day=day, user_type=UserType.EXTERNAL)
        ok, err = QuotaService.validate_booking_quota(
            self.external,
            self.equipment,
            additional_time_minutes=30,
            booking_date=_aware(day.year, day.month, day.day, 11),
        )
        self.assertFalse(ok, "60 used + 30 new should exceed external weekly 60")
        self.assertIn("quota exceeded", (err or "").lower())

    def test_unenforced_group_quota_allows(self):
        EquipmentGroupQuota.objects.filter(equipment_group=self.group).update(is_enforced=False)
        day = timezone.localdate()
        ok, err = QuotaService.validate_booking_quota(
            self.student,
            self.equipment,
            additional_time_minutes=9999,
            booking_date=_aware(day.year, day.month, day.day, 11),
        )
        self.assertTrue(ok, err)

    def test_faculty_stops_after_faculty_checks(self):
        """Faculty users are not subject to individual quotas."""
        day = timezone.localdate()
        # Use most of faculty weekly (240); individual weekly is only 120
        self._book(self.faculty, minutes=200, day=day, user_type=UserType.FACULTY)
        ok, err = QuotaService.validate_booking_quota(
            self.faculty,
            self.equipment,
            additional_time_minutes=30,
            booking_date=_aware(day.year, day.month, day.day, 11),
        )
        self.assertTrue(ok, err)

    def test_faculty_monthly_checked_before_weekly(self):
        day = timezone.localdate()
        # Exhaust faculty monthly (480) via prior bookings — weekly alone would still allow
        self._book(self.faculty, minutes=450, day=day, user_type=UserType.FACULTY)
        ok, err = QuotaService.validate_booking_quota(
            self.faculty,
            self.equipment,
            additional_time_minutes=60,
            booking_date=_aware(day.year, day.month, day.day, 11),
        )
        self.assertFalse(ok)
        self.assertIn("faculty monthly", (err or "").lower())

    def test_student_counts_toward_faculty_wallet_quota(self):
        day = timezone.localdate()
        self._book(self.faculty, minutes=200, day=day, user_type=UserType.FACULTY)
        ok, err = QuotaService.validate_booking_quota(
            self.student,
            self.equipment,
            additional_time_minutes=50,
            booking_date=_aware(day.year, day.month, day.day, 11),
        )
        # Faculty weekly 240: 200 + 50 = 250 → fail faculty weekly before individual
        self.assertFalse(ok)
        self.assertIn("faculty weekly", (err or "").lower())

    def test_cancelled_bookings_excluded(self):
        day = timezone.localdate()
        self._book(
            self.student,
            minutes=120,
            day=day,
            user_type=UserType.STUDENT,
            status=BookingStatus.CANCELLED,
        )
        ok, err = QuotaService.validate_booking_quota(
            self.student,
            self.equipment,
            additional_time_minutes=120,
            booking_date=_aware(day.year, day.month, day.day, 11),
        )
        self.assertTrue(ok, err)

    def test_refunded_and_disruption_excluded(self):
        day = timezone.localdate()
        self._book(
            self.student,
            minutes=90,
            day=day,
            user_type=UserType.STUDENT,
            status=BookingStatus.REFUNDED,
            create_slot=False,
        )
        self._book(
            self.student,
            minutes=90,
            day=day,
            user_type=UserType.STUDENT,
            status=BookingStatus.DISRUPTION_PENDING,
            create_slot=False,
        )
        ok, err = QuotaService.validate_booking_quota(
            self.student,
            self.equipment,
            additional_time_minutes=120,
            booking_date=_aware(day.year, day.month, day.day, 11),
        )
        self.assertTrue(ok, err)

    def test_repeat_sample_excluded(self):
        day = timezone.localdate()
        original = self._book(self.student, minutes=60, day=day, user_type=UserType.STUDENT)
        self._book(
            self.student,
            minutes=60,
            day=day,
            user_type=UserType.STUDENT,
            source_booking=original,
            create_slot=False,
        )
        # Only original 60 counts; +60 stays within individual weekly 120
        ok, err = QuotaService.validate_booking_quota(
            self.student,
            self.equipment,
            additional_time_minutes=60,
            booking_date=_aware(day.year, day.month, day.day, 11),
        )
        self.assertTrue(ok, err)

    def test_urgent_bypass(self):
        day = timezone.localdate()
        self._book(self.student, minutes=120, day=day, user_type=UserType.STUDENT)
        ok, err = QuotaService.validate_booking_quota(
            self.student,
            self.equipment,
            additional_time_minutes=60,
            booking_date=_aware(day.year, day.month, day.day, 11),
            bypass_quota=True,
        )
        self.assertTrue(ok, err)
        self.assertIsNone(err)

    def test_check_user_quota_alias_still_works(self):
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


@override_settings(SKIP_BOOKING_QUOTA_CHECK=False)
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
        ok, err = QuotaService.validate_booking_quota(
            self.student,
            self.equipment,
            additional_time_minutes=1,
            booking_date=_aware(day.year, day.month, day.day, 12),
        )
        self.assertFalse(ok)
        self.assertIn("quota exceeded", (err or "").lower())

    def test_weekly_bookings_count_quota_blocks(self):
        day = timezone.localdate()
        self._book(minutes=30, day=day)
        # Only weekly booking-count is configured to block at 1+1; monthly hours allow 30+0
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
