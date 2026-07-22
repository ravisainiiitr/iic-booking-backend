"""Tests for department faculty credit facility (controlled negative balance)."""

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from iic_booking.users.department_faculty_credit_facility import (
    avail_faculty_department_credit,
    department_faculty_credit_floor,
    is_eligible_for_new_facility,
    update_settings,
)
from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.department_faculty_credit_facility import (
    FacultyDepartmentCreditFacility,
    FacultyDepartmentCreditFacilityStatus,
)
from iic_booking.users.models.wallet import SubWallet
from iic_booking.users.wallet_credit_facility import (
    subwallet_booking_balance_ok,
    subwallet_minimum_balance_after_debit,
)

User = get_user_model()


class DepartmentFacultyCreditFacilityTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(
            name="Test Dept CF",
            code="TDCF",
            department_type=DepartmentType.INTERNAL,
        )
        self.faculty = User.objects.create_user(
            email="faculty.cf@test.iitr.ac.in",
            password="pass12345",
            name="Faculty CF",
            user_type=UserType.FACULTY,
            department=self.dept,
            joining_date=date(2026, 7, 1),
        )
        self.wallet = Wallet.objects.create(user=self.faculty)
        self.sub = SubWallet.objects.create(
            wallet=self.wallet,
            department=self.dept,
            balance=Decimal("0.00"),
        )

    def _enable(self, limit: str = "10000"):
        update_settings(
            department_id=self.dept.id,
            enabled=True,
            joining_date_cutoff=date(2026, 7, 1),
            max_credit_limit=Decimal(limit),
        )

    def test_ineligible_when_disabled(self):
        update_settings(
            department_id=self.dept.id,
            enabled=False,
            joining_date_cutoff=date(2026, 7, 1),
            max_credit_limit=Decimal("10000"),
        )
        self.assertFalse(is_eligible_for_new_facility(self.faculty, self.dept, sub=self.sub))
        self.assertEqual(department_faculty_credit_floor(self.sub), Decimal("0.00"))

    def test_eligible_but_no_floor_until_availed(self):
        self._enable()
        self.assertTrue(is_eligible_for_new_facility(self.faculty, self.dept, sub=self.sub))
        self.assertEqual(department_faculty_credit_floor(self.sub), Decimal("0.00"))
        ok, _ = subwallet_booking_balance_ok(self.sub, Decimal("100"), create_as_hold=False)
        self.assertFalse(ok)

    def test_avail_then_booking_and_close_on_credit(self):
        self._enable()
        facility = avail_faculty_department_credit(
            user=self.faculty, department_id=self.dept.id, amount=Decimal("10000")
        )
        self.assertEqual(facility.status, FacultyDepartmentCreditFacilityStatus.ACTIVE)
        self.assertEqual(department_faculty_credit_floor(self.sub), Decimal("-10000.00"))
        ok, _ = subwallet_booking_balance_ok(self.sub, Decimal("8000"), create_as_hold=False)
        self.assertTrue(ok)

        floor = subwallet_minimum_balance_after_debit(self.sub)
        self.sub.debit(Decimal("2500"), description="booking", minimum_balance_after=floor)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.balance, Decimal("-2500.00"))

        self.sub.credit(Decimal("3000"), description="recharge")
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.balance, Decimal("500.00"))
        facility.refresh_from_db()
        self.assertEqual(facility.status, FacultyDepartmentCreditFacilityStatus.CLOSED)

        self.assertFalse(is_eligible_for_new_facility(self.faculty, self.dept, sub=self.sub))
        with self.assertRaises(ValueError):
            avail_faculty_department_credit(
                user=self.faculty, department_id=self.dept.id, amount=Decimal("1000")
            )

    def test_avail_amount_capped_by_department_max(self):
        self._enable("5000")
        with self.assertRaises(ValueError):
            avail_faculty_department_credit(
                user=self.faculty, department_id=self.dept.id, amount=Decimal("6000")
            )

    def test_positive_balance_blocks_eligibility(self):
        self._enable()
        self.sub.credit(Decimal("100"), description="seed")
        self.assertFalse(is_eligible_for_new_facility(self.faculty, self.dept, sub=self.sub))
