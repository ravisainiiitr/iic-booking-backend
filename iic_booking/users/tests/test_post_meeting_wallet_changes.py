"""31 Oct 2026 committee decisions on wallets: no wallet-to-wallet transfer, no new Individual Students and no
top-ups of existing Individual Student wallets. Nothing here may move money or change a balance."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from iic_booking.users.api.auth_serializers import RegisterSerializer
from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.wallet import SubWallet
from iic_booking.users.models.wallet_sric_settings import WalletSricSettings, peer_transfer_enabled, wallet_mode_flags
from iic_booking.users.student_wallet_recharge import (
    INDIVIDUAL_STUDENT_TOPUPS_CLOSED_MESSAGE,
    assert_iitr_student_may_recharge,
    student_has_any_recharge_department,
)

User = get_user_model()


class PostMeetingWalletTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="IIC PM", code="IICPM", department_type=DepartmentType.INTERNAL)
        self.faculty = User.objects.create_user(
            email="faculty.pm@test.iitr.ac.in", password="pass12345", name="Faculty PM",
            user_type=UserType.FACULTY, department=self.dept, is_active=True,
        )
        self.individual = User.objects.create_user(
            email="fellow.pm@test.iitr.ac.in", password="pass12345", name="Fellow PM",
            user_type=UserType.INDIVIDUAL_STUDENT, department=self.dept,
        )
        self.admin = User.objects.create_user(
            email="admin.pm@test.iitr.ac.in", password="pass12345", name="Admin PM", user_type=UserType.ADMIN,
            is_staff=True,
        )
        self.client = APIClient()

    # --- item 10: wallet-to-wallet transfer ----------------------------------------------------------------------

    def test_peer_transfer_is_off_even_when_the_stored_switch_is_on(self):
        s = WalletSricSettings.get_singleton()
        s.peer_transfer_enabled = True
        s.save()
        self.assertFalse(peer_transfer_enabled(department=self.dept.pk))
        self.assertFalse(wallet_mode_flags(self.faculty)["peer_transfer_enabled"])

    def test_peer_transfer_action_endpoints_are_gone_and_history_remains(self):
        self.client.force_authenticate(user=self.faculty)
        for url in ("/api/wallet/peer-transfer/send-otp/", "/api/wallet/peer-transfer/confirm/",
                    "/api/wallet/peer-transfer/eligible-recipients/", "/api/wallet/peer-transfer/source-departments/"):
            self.assertEqual(self.client.post(url, {}, format="json").status_code, 404, url)
        history = self.client.get("/api/wallet/peer-transfer/history/")
        self.assertEqual(history.status_code, 200)
        self.assertEqual(history.data["count"], 0)

    # --- item 2: Individual Students ---------------------------------------------------------------------------

    def test_individual_student_cannot_top_up_but_keeps_the_balance(self):
        wallet = Wallet.objects.create(user=self.individual)
        sub = SubWallet.objects.create(wallet=wallet, department=self.dept, balance=Decimal("750.00"))
        self.assertEqual(assert_iitr_student_may_recharge(self.individual, department=self.dept),
                         INDIVIDUAL_STUDENT_TOPUPS_CLOSED_MESSAGE)
        self.assertFalse(student_has_any_recharge_department(self.individual))
        self.client.force_authenticate(user=self.individual)
        self.assertEqual(self.client.get("/api/wallet/departments-for-recharge/").data["departments"], [])
        sub.refresh_from_db()
        self.assertEqual(sub.balance, Decimal("750.00"))  # the balance is untouched and still usable

    def test_faculty_recharge_is_unaffected(self):
        self.assertIsNone(assert_iitr_student_may_recharge(self.faculty, department=self.dept))

    def test_registration_as_individual_student_becomes_student_and_needs_a_supervisor(self):
        from datetime import date

        from rest_framework.exceptions import ValidationError

        attrs = {
            "password": "Str0ng!pass#2026", "password_confirm": "Str0ng!pass#2026",
            "user_type": UserType.INDIVIDUAL_STUDENT, "user_type_alias": "IITR PMRF Fellows",
            "program_end_date": date(2028, 6, 30),
        }
        with self.assertRaises(ValidationError) as ctx:
            RegisterSerializer().validate(dict(attrs))
        self.assertIn("supervisor", ctx.exception.detail)
        validated = RegisterSerializer().validate({**attrs, "supervisor": self.faculty})
        self.assertEqual(validated["user_type"], UserType.STUDENT)
        self.assertEqual(validated["user_type_alias"], "IITR PMRF Fellows")
        self.assertEqual(validated["supervisor"], self.faculty)

    def test_admin_cannot_make_a_user_an_individual_student(self):
        student = User.objects.create_user(
            email="student.pm@test.iitr.ac.in", password="pass12345", name="Student PM",
            user_type=UserType.STUDENT, department=self.dept,
        )
        self.client.force_authenticate(user=self.admin)
        res = self.client.patch(f"/api/users/{student.pk}/", {"user_type": "individual_student"}, format="json")
        self.assertIn(res.status_code, (400, 404, 405))
        student.refresh_from_db()
        self.assertEqual(student.user_type, UserType.STUDENT)
