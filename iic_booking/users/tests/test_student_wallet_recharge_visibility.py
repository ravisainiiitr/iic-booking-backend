"""Recharge Wallet for IITR Students: every student on a supervisor's wallet sees and may use recharge
for the same departments faculty get for that wallet, whatever the department / global student
switches say. Methods follow Wallet Payment Modes; Project Grant stays faculty-only."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.wallet import SubWallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.models.wallet_payment_modes import DepartmentModeState, WalletModeDepartmentSetting
from iic_booking.users.models.wallet_sric_settings import WalletSricSettings
from iic_booking.users.models.wallet_student_recharge_settings import WalletStudentRechargeSettings
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
        sric = WalletSricSettings.get_singleton()
        sric.project_grant_recharge_enabled = True
        sric.direct_cash_recharge_enabled = True
        sric.online_gateway_recharge_enabled = False
        sric.peer_transfer_enabled = True
        sric.save()
        WalletStudentRechargeSettings.objects.update_or_create(
            pk=1, defaults={"enable_iitr_student_wallet_recharge": False},
        )
        self.dept_a = Department.objects.create(
            name="Recharge Vis A", code="RVA", department_type=DepartmentType.INTERNAL,
        )
        self.tinkering = Department.objects.create(
            name="Recharge Vis Tinkering", code="RVTL", department_type=DepartmentType.INTERNAL,
        )
        WalletModeDepartmentSetting.objects.create(
            department=self.tinkering,
            direct_cash=DepartmentModeState.DISABLED,
            peer_transfer=DepartmentModeState.DISABLED,
            project_grant=DepartmentModeState.DISABLED,
        )
        self.faculty = User.objects.create_user(
            email="prof.rechargevis@test.iitr.ac.in", password="pass12345", name="Prof RV",
            user_type=UserType.FACULTY, department=self.dept_a,
        )
        self.wallet, _ = Wallet.objects.get_or_create(user=self.faculty)
        for dept in (self.dept_a, self.tinkering):
            SubWallet.objects.create(wallet=self.wallet, department=dept, balance=Decimal("0.00"))
        self.student = self._student("student.rechargevis@test.iitr.ac.in", linked=True)

    def _student(self, email: str, *, linked: bool, is_test_account: bool = False):
        student = User.objects.create_user(
            email=email, password="pass12345", name="Student RV",
            user_type=UserType.STUDENT, department=self.dept_a, is_test_account=is_test_account,
        )
        if linked:
            WalletJoinRequest.objects.create(
                student=student, faculty=self.faculty, wallet=self.wallet,
                status=WalletJoinRequestStatus.APPROVED,
            )
        return student

    def _client(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client

    def _dept_ids(self, user):
        return {d["id"] for d in self._client(user).get(DEPARTMENTS_URL).data["departments"]}

    def _send_otp(self, user, department, mode="direct_cash_deposit"):
        return self._client(user).post(
            SEND_OTP_URL,
            {
                "amount": "500",
                "department_id": department.id,
                "recharge_mode": mode,
                "undertaking_accepted": True,
            },
            format="json",
        )

    def test_linked_student_sees_recharge_without_any_student_switch(self):
        self.assertFalse(Department.objects.filter(enable_student_wallet_recharge=True).exists())
        res = self._client(self.student).get(SETTINGS_URL)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["applies_to_current_user"])
        self.assertTrue(res.data["enabled"])
        self.assertTrue(student_has_any_recharge_department(self.student))

    def test_student_gets_the_same_departments_as_the_supervisor(self):
        self.assertEqual(self._dept_ids(self.student), self._dept_ids(self.faculty))
        self.assertEqual(self._dept_ids(self.student), {self.dept_a.id, self.tinkering.id})

    def test_real_and_test_students_are_treated_alike(self):
        test_student = self._student("student.rechargevis.t@test.iitr.ac.in", linked=True, is_test_account=True)
        for user in (self.student, test_student):
            self.assertTrue(self._client(user).get(SETTINGS_URL).data["enabled"])
            self.assertEqual(self._dept_ids(user), {self.dept_a.id, self.tinkering.id})

    def test_unlinked_student_gets_no_recharge(self):
        loner = self._student("student.rechargevis.u@test.iitr.ac.in", linked=False)
        self.assertFalse(self._client(loner).get(SETTINGS_URL).data["enabled"])
        self.assertEqual(self._dept_ids(loner), set())
        self.assertIsNotNone(assert_iitr_student_may_recharge(loner, department=self.dept_a))
        res = self._send_otp(loner, self.dept_a)
        self.assertEqual(res.status_code, 403)

    def test_student_direct_cash_request_is_accepted_for_an_open_department(self):
        self.assertIsNone(assert_iitr_student_may_recharge(self.student, department=self.dept_a))
        res = self._send_otp(self.student, self.dept_a)
        self.assertEqual(res.status_code, 200, res.data)
        self.assertIn("request_id", res.data)

    def test_department_outside_the_wallet_list_is_refused(self):
        other = Department.objects.create(name="Recharge Vis Ext", code="RVX", department_type=DepartmentType.EXTERNAL)
        self.assertIsNotNone(assert_iitr_student_may_recharge(self.student, department=other))
        self.assertEqual(self._send_otp(self.student, other).status_code, 403)

    @override_settings(WALLET_PROJECT_GRANT_RETIRED=False)
    def test_tinkering_style_disabled_methods_apply_to_students(self):
        flags = self._client(self.student).get(SETTINGS_URL).data
        self.assertEqual(
            flags["department_modes"][str(self.tinkering.id)],
            {
                "project_grant_recharge_enabled": False,
                "direct_cash_recharge_enabled": False,
                "peer_transfer_enabled": False,
            },
        )
        self.assertNotIn(str(self.dept_a.id), flags["department_modes"])
        res = self._send_otp(self.student, self.tinkering)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data.get("code"), "direct_cash_recharge_disabled")

    @override_settings(WALLET_PROJECT_GRANT_RETIRED=False)
    def test_project_grant_stays_faculty_only(self):
        res = self._send_otp(self.student, self.dept_a, mode="project_grant")
        self.assertEqual(res.status_code, 403)
        self.assertIn("Project-grant", res.data["error"])

    def test_legacy_switches_do_not_change_anything(self):
        Department.objects.filter(pk=self.dept_a.pk).update(enable_student_wallet_recharge=True)
        WalletStudentRechargeSettings.objects.filter(pk=1).update(enable_iitr_student_wallet_recharge=True)
        self.assertEqual(self._dept_ids(self.student), {self.dept_a.id, self.tinkering.id})
        loner = self._student("student.rechargevis.u2@test.iitr.ac.in", linked=False)
        self.assertEqual(self._dept_ids(loner), set())

    def test_faculty_is_unaffected(self):
        self.assertEqual(self._dept_ids(self.faculty), {self.dept_a.id, self.tinkering.id})
        self.assertIsNone(assert_iitr_student_may_recharge(self.faculty, department=self.tinkering))
