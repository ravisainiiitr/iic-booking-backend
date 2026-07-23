from decimal import Decimal
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from iic_booking.equipment.booking_payment_service import compute_booking_payment_split
from iic_booking.equipment.calculators import (
    ChargeCalculationEngine,
    finalize_charge_result,
    icpms_total_sample_units,
    quantize_money,
)
from iic_booking.equipment.models import (
    Booking,
    ChargeProfile,
    DynamicInputField,
    DynamicInputFieldType,
    Equipment,
    EquipmentProfileType,
    SlotStatus,
)
from iic_booking.equipment.slot_utils import SlotAvailabilityChecker
from iic_booking.equipment.waitlist_booking import reduce_waitlist_inputs_to_fit_available_slots
from iic_booking.users.models import Department, DepartmentType
from iic_booking.users.models.user_type import UserType


class ChargeProfileWithType:
    """Mirrors api_views wrapper: profile_type lives on equipment."""

    def __init__(self, charge_profile, equipment):
        self.equipment = charge_profile.equipment
        self.user_type = charge_profile.user_type
        self.is_active = charge_profile.is_active
        self.primary_unit_charge = charge_profile.primary_unit_charge
        self.secondary_unit_charge = charge_profile.secondary_unit_charge
        self.breakpoint = charge_profile.breakpoint
        self.time_formula = charge_profile.time_formula
        self.pricing_profile = charge_profile.pricing_profile
        self.profile_type = equipment.profile_type


class ChargeApproximationTests(TestCase):
    def test_quantize_money_rounds_to_nearest_rupee(self):
        self.assertEqual(quantize_money("10.335"), Decimal("10"))
        self.assertEqual(quantize_money(Decimal("99.5")), Decimal("100"))
        self.assertEqual(quantize_money(Decimal("99.4")), Decimal("99"))

    def test_finalize_charge_result_rounds_total_and_breakdown(self):
        total, breakdown = finalize_charge_result(
            Decimal("12.345"),
            [{"description": "line", "amount": 3.336}],
        )
        self.assertEqual(total, Decimal("12"))
        self.assertEqual(breakdown[0]["amount"], 3.0)


class ICPMSChargeTests(TestCase):
    def setUp(self):
        self.equipment = Equipment.objects.create(
            name="ICPMS Test",
            code="ICPMS-TEST",
            profile_type=EquipmentProfileType.SAMPLE_ELEMENT,
            slot_duration_minutes=60,
        )
        self.charge_profile = ChargeProfile.objects.create(
            equipment=self.equipment,
            user_type="internal_faculty",
            primary_unit_charge=Decimal("10.00"),
            secondary_unit_charge=Decimal("2.50"),
            breakpoint=Decimal("5"),
        )
        DynamicInputField.objects.create(
            equipment=self.equipment,
            field_key="D",
            field_label="Standards required",
            field_type=DynamicInputFieldType.ICPMS_STANDARD_COVERAGE,
            source_element_field_key="B",
        )

    def test_icpms_total_sample_units_includes_blank(self):
        self.assertEqual(
            icpms_total_sample_units(Decimal("2"), Decimal("1")),
            Decimal("6"),
        )

    def test_icpms_charge_uses_a_plus_3c_plus_1(self):
        profile = ChargeProfileWithType(self.charge_profile, self.equipment)
        input_values = {"A": 2, "B": 3, "D": 1}
        total, breakdown = ChargeCalculationEngine.calculate_charge(
            profile, input_values, total_time_minutes=60
        )
        # A=2, C=1 => 2 + 3*1 + 1 blank = 6 runs @ ₹10
        self.assertEqual(total, Decimal("60.00"))
        self.assertTrue(any("blank" in line["description"].lower() for line in breakdown))
        self.assertEqual(breakdown[0]["amount"], 60.0)

    def test_non_icpms_sample_element_uses_legacy_a_only_model(self):
        equipment = Equipment.objects.create(
            name="Legacy SE",
            code="LEG-SE",
            profile_type=EquipmentProfileType.SAMPLE_ELEMENT,
            slot_duration_minutes=60,
        )
        charge_profile = ChargeProfile.objects.create(
            equipment=equipment,
            user_type="internal_faculty",
            primary_unit_charge=Decimal("10.00"),
            secondary_unit_charge=Decimal("0.00"),
            breakpoint=Decimal("99"),
        )
        profile = ChargeProfileWithType(charge_profile, equipment)
        total, _ = ChargeCalculationEngine.calculate_charge(
            profile, {"A": 2, "B": 1, "C": 1}, total_time_minutes=60
        )
        # Legacy SAMPLE_ELEMENT (no ICPMS field): A × primary only
        self.assertEqual(total, Decimal("20.00"))


class VirtualBookingIdTests(TestCase):
    def test_virtual_id_prefix_includes_department_code(self):
        dept = Department.objects.create(
            name="Chemistry",
            code="CH",
            department_type=DepartmentType.INTERNAL,
        )
        equipment = Equipment.objects.create(
            name="GEM",
            code="GEM",
            internal_department=dept,
        )
        prefix = Booking._virtual_id_prefix(equipment.equipment_id, equipment.code, department_code="CH")
        self.assertTrue(prefix.startswith("CH"))
        self.assertIn("GEM", prefix)
        self.assertTrue(prefix.endswith(str(timezone.now().year)))


class BookingPaymentSplitTests(SimpleTestCase):
    """Branch coverage for compute_booking_payment_split (last line before money moves)."""

    def test_hold_short_circuit(self):
        applied, due, err = compute_booking_payment_split(
            MagicMock(), Decimal("150.00"), user_type=UserType.FACULTY, create_as_hold=True
        )
        self.assertEqual(applied, Decimal("0.00"))
        self.assertEqual(due, Decimal("0.00"))
        self.assertIsNone(err)

    def test_zero_charge_short_circuit(self):
        applied, due, err = compute_booking_payment_split(
            MagicMock(), Decimal("0.00"), user_type=UserType.FACULTY, create_as_hold=False
        )
        self.assertEqual((applied, due, err), (Decimal("0.00"), Decimal("0.00"), None))

    def test_negative_charge_short_circuit(self):
        applied, due, err = compute_booking_payment_split(
            MagicMock(), Decimal("-10.00"), user_type=UserType.EXTERNAL, create_as_hold=False
        )
        self.assertEqual((applied, due, err), (Decimal("0.00"), Decimal("0.00"), None))

    @patch("iic_booking.equipment.booking_payment_service.wallet_booking_block_message")
    def test_wallet_block_returns_full_amount_due(self, mock_block):
        mock_block.return_value = "wallet on hold"
        applied, due, err = compute_booking_payment_split(
            MagicMock(), Decimal("80.00"), user_type=UserType.FACULTY, create_as_hold=False
        )
        self.assertEqual(applied, Decimal("0.00"))
        self.assertEqual(due, Decimal("80.00"))
        self.assertEqual(err, "wallet on hold")

    @patch("iic_booking.equipment.booking_payment_service.wallet_max_spendable_on_subwallet")
    @patch("iic_booking.equipment.booking_payment_service.wallet_booking_block_message", return_value=None)
    def test_external_partial_wallet(self, _block, mock_spendable):
        mock_spendable.return_value = Decimal("40.00")
        applied, due, err = compute_booking_payment_split(
            MagicMock(), Decimal("100.00"), user_type=UserType.EXTERNAL, create_as_hold=False
        )
        self.assertEqual(applied, Decimal("40.00"))
        self.assertEqual(due, Decimal("60.00"))
        self.assertIsNone(err)

    @patch("iic_booking.equipment.booking_payment_service.wallet_max_spendable_on_subwallet")
    @patch("iic_booking.equipment.booking_payment_service.wallet_booking_block_message", return_value=None)
    def test_external_full_wallet_covers_charge(self, _block, mock_spendable):
        mock_spendable.return_value = Decimal("200.00")
        applied, due, err = compute_booking_payment_split(
            MagicMock(), Decimal("75.50"), user_type=UserType.EXTERNAL, create_as_hold=False
        )
        self.assertEqual(applied, Decimal("75.50"))
        self.assertEqual(due, Decimal("0.00"))
        self.assertIsNone(err)

    @patch("iic_booking.equipment.booking_payment_service.subwallet_booking_balance_ok")
    @patch("iic_booking.equipment.booking_payment_service.wallet_booking_block_message", return_value=None)
    def test_internal_insufficient_balance(self, _block, mock_ok):
        mock_ok.return_value = (False, "Insufficient wallet balance. Required: ₹50.00, Available: ₹10.00")
        applied, due, err = compute_booking_payment_split(
            MagicMock(), Decimal("50.00"), user_type=UserType.FACULTY, create_as_hold=False
        )
        self.assertEqual(applied, Decimal("0.00"))
        self.assertEqual(due, Decimal("50.00"))
        self.assertIn("Insufficient", err)

    @patch("iic_booking.equipment.booking_payment_service.subwallet_booking_balance_ok")
    @patch("iic_booking.equipment.booking_payment_service.wallet_booking_block_message", return_value=None)
    def test_internal_full_wallet(self, _block, mock_ok):
        mock_ok.return_value = (True, None)
        applied, due, err = compute_booking_payment_split(
            MagicMock(), Decimal("50.00"), user_type=UserType.FACULTY, create_as_hold=False
        )
        self.assertEqual(applied, Decimal("50.00"))
        self.assertEqual(due, Decimal("0.00"))
        self.assertIsNone(err)


class SlotAvailabilityPastGuardTests(SimpleTestCase):
    def _slot(self, *, start, status=SlotStatus.AVAILABLE, reserved_for_external=False):
        slot = MagicMock()
        slot.start_datetime = start
        slot.status = status
        slot.reserved_for_external = reserved_for_external
        return slot

    def test_future_available_slot_is_bookable(self):
        slot = self._slot(start=timezone.now() + timedelta(hours=2))
        self.assertTrue(SlotAvailabilityChecker.is_slot_available(slot))

    def test_past_available_slot_is_not_bookable(self):
        slot = self._slot(start=timezone.now() - timedelta(hours=1))
        self.assertFalse(SlotAvailabilityChecker.is_slot_available(slot))

    def test_missing_start_is_not_bookable(self):
        slot = self._slot(start=None)
        self.assertFalse(SlotAvailabilityChecker.is_slot_available(slot))

    def test_external_past_slot_is_not_bookable(self):
        slot = self._slot(
            start=timezone.now() - timedelta(minutes=5),
            reserved_for_external=False,
        )
        self.assertFalse(SlotAvailabilityChecker.is_slot_available_for_external(slot))

    def test_external_future_available_without_reserved_is_bookable(self):
        slot = self._slot(
            start=timezone.now() + timedelta(hours=1),
            reserved_for_external=False,
        )
        self.assertTrue(SlotAvailabilityChecker.is_slot_available_for_external(slot))


class WaitlistReduceBoundaryTests(SimpleTestCase):
    """Boundary coverage for reduce_waitlist_inputs_to_fit_available_slots."""

    def test_zero_available_slots_returns_none(self):
        result = reduce_waitlist_inputs_to_fit_available_slots(
            MagicMock(slot_duration_minutes=60, profile_type="HOUR"),
            MagicMock(),
            input_values={"B": 3},
            desired_slots=3,
            max_slots_available=0,
        )
        self.assertIsNone(result)

    @patch("iic_booking.equipment.waitlist_booking._resolve_charge_profile_for_user")
    def test_no_charge_profile_returns_none(self, mock_resolve):
        mock_resolve.return_value = (None, "faculty", False)
        result = reduce_waitlist_inputs_to_fit_available_slots(
            MagicMock(slot_duration_minutes=60, profile_type="HOUR"),
            MagicMock(),
            input_values={"B": 2},
            desired_slots=2,
            max_slots_available=2,
        )
        self.assertIsNone(result)

    @patch("iic_booking.equipment.waitlist_booking.TimeCalculationEngine.calculate_time", return_value=0)
    @patch("iic_booking.equipment.waitlist_booking.build_safe_input_values_for_charge_calculation", side_effect=lambda d: d)
    @patch("iic_booking.equipment.waitlist_booking._resolve_charge_profile_for_user")
    def test_zero_time_from_engine_never_succeeds(self, mock_resolve, _safe, _calc):
        cp = MagicMock()
        cp.equipment = MagicMock()
        cp.user_type = "faculty"
        cp.is_active = True
        cp.primary_unit_charge = Decimal("1")
        cp.secondary_unit_charge = Decimal("0")
        cp.breakpoint = Decimal("0")
        cp.time_formula = ""
        cp.pricing_profile = "STANDARD"
        mock_resolve.return_value = (cp, "faculty", False)

        result = reduce_waitlist_inputs_to_fit_available_slots(
            MagicMock(slot_duration_minutes=60, profile_type="HOUR"),
            MagicMock(),
            input_values={"B": 2},
            desired_slots=2,
            max_slots_available=2,
        )
        self.assertIsNone(result)

    @patch("iic_booking.equipment.waitlist_booking.TimeCalculationEngine.calculate_time", return_value=60)
    @patch("iic_booking.equipment.waitlist_booking.build_safe_input_values_for_charge_calculation", side_effect=lambda d: d)
    @patch("iic_booking.equipment.waitlist_booking._resolve_charge_profile_for_user")
    def test_hour_reduce_never_returns_zero_slots(self, mock_resolve, _safe, _calc):
        cp = MagicMock()
        cp.equipment = MagicMock()
        cp.user_type = "faculty"
        cp.is_active = True
        cp.primary_unit_charge = Decimal("1")
        cp.secondary_unit_charge = Decimal("0")
        cp.breakpoint = Decimal("0")
        cp.time_formula = ""
        cp.pricing_profile = "STANDARD"
        mock_resolve.return_value = (cp, "faculty", False)

        result = reduce_waitlist_inputs_to_fit_available_slots(
            MagicMock(slot_duration_minutes=60, profile_type="HOUR"),
            MagicMock(),
            input_values={"B": 3},
            desired_slots=3,
            max_slots_available=1,
        )
        self.assertIsNotNone(result)
        _inputs, _minutes, slots_to_book = result
        self.assertGreaterEqual(slots_to_book, 1)
        self.assertLessEqual(slots_to_book, 1)


class PartialCancelPlanGuardTests(SimpleTestCase):
    """Equal-split refund must not be used for partial cancels with tiered pricing."""

    def test_ensure_reuses_existing_plan(self):
        from iic_booking.equipment.booking_cancellation import ensure_partial_cancel_plan

        plan = {"refund_amount": "10.00"}
        self.assertIs(
            ensure_partial_cancel_plan(MagicMock(), slot_ids=[1], partial_plan=plan),
            plan,
        )

    def test_ensure_without_plan_or_slots_raises(self):
        from iic_booking.equipment.booking_cancellation import (
            CancellationValidationError,
            ensure_partial_cancel_plan,
        )

        with self.assertRaises(CancellationValidationError):
            ensure_partial_cancel_plan(MagicMock(), slot_ids=[], partial_plan=None)

    @patch("iic_booking.equipment.booking_cancellation.compute_partial_cancel_plan")
    def test_ensure_builds_plan_from_slot_ids(self, mock_compute):
        from iic_booking.equipment.booking_cancellation import ensure_partial_cancel_plan

        built = {"refund_amount": "12.50", "new_charge": "37.50"}
        mock_compute.return_value = built
        booking = MagicMock()
        result = ensure_partial_cancel_plan(booking, slot_ids=[2, 3], partial_plan=None)
        self.assertEqual(result, built)
        mock_compute.assert_called_once_with(booking, slot_ids_to_cancel=[2, 3])

    def test_equal_split_helper_is_linear_only(self):
        """Document: calculate_refund_for_cancelled_slots ignores tiered pricing."""
        from iic_booking.equipment.booking_cancellation import (
            calculate_refund_for_cancelled_slots,
        )

        booking = MagicMock()
        booking.total_charge = Decimal("90.00")
        booking.daily_slots.all.return_value = [MagicMock(), MagicMock(), MagicMock()]
        cancelled = [MagicMock()]
        self.assertEqual(
            calculate_refund_for_cancelled_slots(booking, cancelled),
            Decimal("30.00"),
        )


class LocalDateConventionTests(SimpleTestCase):
    """IST calendar boundaries must use Django localdate, not OS/UTC date.today()."""

    SENSITIVE_GLOBS = (
        "iic_booking/equipment/*.py",
        "iic_booking/users/faculty_wallet_report.py",
        "iic_booking/users/api/auth_views.py",
        "iic_booking/users/api/project_views.py",
        "iic_booking/users/models/project.py",
        "config/admin_api.py",
    )

    def test_no_naive_date_today_in_sensitive_modules(self):
        from pathlib import Path
        import re

        root = Path(__file__).resolve().parents[2]  # iic-booking-backend/
        repo = root
        pattern = re.compile(r"\bdate\.today\s*\(")
        offenders = []
        for glob in self.SENSITIVE_GLOBS:
            for path in repo.glob(glob):
                if path.name == "tests.py":
                    continue
                text = path.read_text(encoding="utf-8")
                for i, line in enumerate(text.splitlines(), 1):
                    if pattern.search(line) and "date.today()" in line:
                        offenders.append(f"{path.relative_to(repo)}:{i}: {line.strip()}")
        self.assertEqual(offenders, [], "Replace date.today() with timezone.localdate():\n" + "\n".join(offenders))

    def test_no_utc_now_date_in_sensitive_modules(self):
        from pathlib import Path
        import re

        root = Path(__file__).resolve().parents[2]
        repo = root
        # Catches timezone.now().date() and tz.now().date()
        pattern = re.compile(r"\b(?:timezone|tz)\.now\s*\(\s*\)\s*\.\s*date\s*\(")
        offenders = []
        for glob in self.SENSITIVE_GLOBS:
            for path in repo.glob(glob):
                if path.name == "tests.py":
                    continue
                text = path.read_text(encoding="utf-8")
                for i, line in enumerate(text.splitlines(), 1):
                    if pattern.search(line):
                        offenders.append(f"{path.relative_to(repo)}:{i}: {line.strip()}")
        self.assertEqual(
            offenders,
            [],
            "Replace timezone.now().date() / tz.now().date() with localdate():\n" + "\n".join(offenders),
        )

    def test_localdate_is_ist_near_utc_midnight_boundary(self):
        from datetime import datetime
        from unittest.mock import patch
        from zoneinfo import ZoneInfo
        from django.utils import timezone as dj_tz

        # 00:30 IST on 1 Aug == 19:00 UTC on 31 Jul
        ist_moment = datetime(2026, 8, 1, 0, 30, tzinfo=ZoneInfo("Asia/Kolkata"))
        with patch.object(dj_tz, "now", return_value=ist_moment.astimezone(ZoneInfo("UTC"))):
            self.assertEqual(dj_tz.localdate().isoformat(), "2026-08-01")
            # Contrasting anti-pattern still visible as UTC date:
            self.assertEqual(dj_tz.now().date().isoformat(), "2026-07-31")
