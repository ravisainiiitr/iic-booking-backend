"""Recharge Wallet visibility for IITR Students follows the department switch for every account,
not a test-account allowlist; the recharge permission matches what the Wallet page shows."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.wallet import SubWallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.student_wallet_recharge import (
    assert_iitr_student_may_recharge,
    student_has_any_recharge_department,
)

User = get_user_model()

SETTINGS_URL = "/api/wallet/student-recharge/settings/"
DEPARTMENTS_URL = "/api/wallet/departments-for-recharge/"
SEND_OTP_URL = "/api/wallet/recharge-request/send-otp/"


class StudentRechargeVisibilityTests(TestCase):
    def setUp(self):
        self.enabled_dept = Department.objects.create(
            name="Recharge Vis On", code="RVON", department_type=DepartmentType.INTERNAL,
            enable_student_wallet_recharge=True,
        )
        self.disabled_dept = Department.objects.create(
            name="Recharge Vis Off", code="RVOF", department_type=DepartmentType.INTERNAL,
        )
        self.faculty = User.objects.create_user(
            email="prof.rechargevis@test.iitr.ac.in", password="pass12345", name="Prof RV",
            user_type=UserType.FACULTY, department=self.enabled_dept,
        )
        self.wallet, _ = Wallet.objects.get_or_create(user=self.faculty)
        for dept in (self.enabled_dept, self.disabled_dept):
            SubWallet.objects.create(wallet=self.wallet, department=dept, balance=Decimal("0.00"))
        self.student = self._linked_student("student.rechargevis@test.iitr.ac.in", is_test_account=False)

    def _linked_student(self, email: str, *, is_test_account: bool):
        student = User.objects.create_user(
            email=email, password="pass12345", name="Student RV",
            user_type=UserType.STUDENT, department=self.enabled_dept, is_test_account=is_test_account,
        )
        WalletJoinRequest.objects.create(
            student=student, faculty=self.faculty, wallet=self.wallet, status=WalletJoinRequestStatus.APPROVED,
        )
        return student

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def test_real_student_sees_recharge_when_a_department_allows_it(self):
        res = self._client(self.student).get(SETTINGS_URL)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["applies_to_current_user"])
        self.assertTrue(res.data["enabled"])
        self.assertTrue(student_has_any_recharge_department(self.student))

    def test_real_and_test_students_are_treated_alike(self):
        test_student = self._linked_student("student.rechargevis.t@test.iitr.ac.in", is_test_account=True)
        for user in (self.student, test_student):
            res = self._client(user).get(SETTINGS_URL)
            self.assertTrue(res.data["enabled"])
            ids = [d["id"] for d in self._client(user).get(DEPARTMENTS_URL).data["departments"]]
            self.assertEqual(ids, [self.enabled_dept.id])

    def test_no_student_sees_recharge_when_no_department_allows_it(self):
        Department.objects.filter(pk=self.enabled_dept.pk).update(enable_student_wallet_recharge=False)
        res = self._client(self.student).get(SETTINGS_URL)
        self.assertFalse(res.data["enabled"])
        self.assertEqual(self._client(self.student).get(DEPARTMENTS_URL).data["departments"], [])

    def test_permission_matches_department_switch(self):
        self.assertIsNone(assert_iitr_student_may_recharge(self.student, department=self.enabled_dept))
        self.assertIsNotNone(assert_iitr_student_may_recharge(self.student, department=self.disabled_dept))
        res = self._client(self.student).post(
            SEND_OTP_URL,
            {
                "amount": "500",
                "department_id": self.disabled_dept.id,
                "recharge_mode": "direct_cash_deposit",
                "undertaking_accepted": True,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertIn("disabled for this department", res.data["error"])

    def test_faculty_departments_are_not_filtered_by_student_switch(self):
        ids = {d["id"] for d in self._client(self.faculty).get(DEPARTMENTS_URL).data["departments"]}
        self.assertEqual(ids, {self.enabled_dept.id, self.disabled_dept.id})
        self.assertIsNone(assert_iitr_student_may_recharge(self.faculty, department=self.disabled_dept))
