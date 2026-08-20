"""Department faculty automatic credit facility — retired in favour of Wallet Credit Facility V2."""

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
from iic_booking.users.models.wallet import SubWallet
from iic_booking.users.wallet_credit_facility import subwallet_minimum_balance_after_debit

User = get_user_model()


class DepartmentFacultyCreditFacilityRetiredTests(TestCase):
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
        update_settings(
            department_id=self.dept.id,
            enabled=True,
            joining_date_cutoff=date(2026, 7, 1),
            max_credit_limit=Decimal("10000"),
        )

    def test_floor_always_zero_after_retirement(self):
        self.assertEqual(department_faculty_credit_floor(self.sub), Decimal("0.00"))
        self.assertEqual(subwallet_minimum_balance_after_debit(self.sub), Decimal("0.00"))

    def test_avail_raises_retired(self):
        with self.assertRaises(ValueError) as ctx:
            avail_faculty_department_credit(
                user=self.faculty, department_id=self.dept.id, amount=Decimal("10000")
            )
        self.assertIn("retired", str(ctx.exception).lower())

    def test_eligibility_helper_still_computes_but_floor_unused(self):
        # Settings/eligibility helpers remain for admin UI; overdraft floor is disabled.
        self.assertTrue(is_eligible_for_new_facility(self.faculty, self.dept, sub=self.sub))
        self.assertEqual(department_faculty_credit_floor(self.sub), Decimal("0.00"))
