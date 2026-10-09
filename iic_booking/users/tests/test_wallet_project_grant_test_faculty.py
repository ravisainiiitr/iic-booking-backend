"""Test faculty accounts may use Project Grant recharge even while the admin switch is off."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from iic_booking.users.models import Department, DepartmentType, Project, UserType, Wallet
from iic_booking.users.models.test_account_email_settings import TestAccountEmailSettings
from iic_booking.users.models.wallet import SubWallet, WalletRechargeMode, WalletRechargeRequest
from iic_booking.users.models.wallet_sric_settings import WalletSricSettings

User = get_user_model()

SEND_OTP = "/api/wallet/recharge-request/send-otp/"
VERIFY = "/api/wallet/recharge-request/"
SETTINGS = "/api/wallet/student-recharge/settings/"

SRIC_OFFICE = "sric.office@test.iitr.ac.in"
DEAN_SRIC = "dean.sric@test.iitr.ac.in"
QA_INBOX = "qa.inbox@example.com"


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend", WALLET_PROJECT_GRANT_RETIRED=False)
class ProjectGrantTestFacultyExemptionTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Exempt Dept", code="EXD", department_type=DepartmentType.INTERNAL)
        WalletSricSettings.objects.update_or_create(
            pk=1,
            defaults={
                "project_grant_recharge_enabled": False,
                "recipient_emails": SRIC_OFFICE,
                "dean_sric_emails": DEAN_SRIC,
            },
        )
        TestAccountEmailSettings.objects.update_or_create(pk=1, defaults={"recipient_emails": QA_INBOX})
        self.test_faculty = self._faculty("test.faculty@iic-booking.test", is_test_account=True)
        self.real_faculty = self._faculty("real.faculty@test.iitr.ac.in", is_test_account=False)

    def _faculty(self, email: str, *, is_test_account: bool):
        user = User.objects.create_user(
            email=email, password="pass12345", name=email.split("@")[0], user_type=UserType.FACULTY,
            department=self.dept, is_test_account=is_test_account,
        )
        wallet, _ = Wallet.objects.get_or_create(user=user)
        SubWallet.objects.create(wallet=wallet, department=self.dept, balance=Decimal("0"))
        user.rc_project = Project.objects.create(
            faculty=user, name="Materials", project_code=f"IITR/EX/{user.pk}", agency="DST"
        )
        user.rc_wallet = wallet
        return user

    def _client(self, user) -> APIClient:
        api = APIClient()
        api.force_authenticate(user)
        return api

    def _send(self, user):
        return self._client(user).post(
            SEND_OTP,
            {
                "amount": "1000.00",
                "department_id": self.dept.id,
                "project_id": user.rc_project.id,
                "recharge_mode": "project_grant",
                "undertaking_accepted": True,
            },
            format="json",
        )

    def test_settings_show_project_grant_to_test_faculty_only(self):
        self.assertTrue(self._client(self.test_faculty).get(SETTINGS).data["project_grant_recharge_enabled"])
        self.assertFalse(self._client(self.real_faculty).get(SETTINGS).data["project_grant_recharge_enabled"])

    def test_test_faculty_can_raise_project_grant_request_while_switch_off(self):
        res = self._send(self.test_faculty)
        self.assertEqual(res.status_code, 200, res.data)
        rid = res.data["request_id"]
        otp = WalletRechargeRequest.objects.get(pk=rid).user_otp_code
        mail.outbox.clear()
        res = self._client(self.test_faculty).post(VERIFY, {"request_id": rid, "user_otp": otp}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        req = WalletRechargeRequest.objects.get(pk=rid)
        self.assertEqual(req.recharge_mode, WalletRechargeMode.PROJECT_GRANT)
        self.assertTrue(req.user_otp_verified)

        delivered = {addr for m in mail.outbox for addr in (m.to + m.cc + m.bcc)}
        self.assertIn(QA_INBOX, delivered)
        self.assertNotIn(SRIC_OFFICE, delivered)
        self.assertNotIn(DEAN_SRIC, delivered)

    def test_real_faculty_still_blocked_while_switch_off(self):
        res = self._send(self.real_faculty)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "project_grant_recharge_disabled")
        self.assertFalse(WalletRechargeRequest.objects.filter(user=self.real_faculty).exists())

    def test_test_faculty_can_send_unsent_request_to_sric_while_switch_off(self):
        req = WalletRechargeRequest.objects.create(
            user=self.test_faculty, wallet=self.test_faculty.rc_wallet, department=self.dept,
            amount=Decimal("500"), project=self.test_faculty.rc_project,
            recharge_mode=WalletRechargeMode.PROJECT_GRANT, undertaking_accepted=True,
            user_otp_verified=True, sric_notification_sent=False,
        )
        res = self._client(self.test_faculty).post(
            f"/api/wallet/recharge-requests/{req.id}/send-sric/", {}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.data)
        req.refresh_from_db()
        self.assertTrue(req.sric_notification_sent)

    def test_real_faculty_request_still_goes_to_sric_office(self):
        WalletSricSettings.objects.filter(pk=1).update(project_grant_recharge_enabled=True)
        rid = self._send(self.real_faculty).data["request_id"]
        otp = WalletRechargeRequest.objects.get(pk=rid).user_otp_code
        mail.outbox.clear()
        res = self._client(self.real_faculty).post(VERIFY, {"request_id": rid, "user_otp": otp}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        delivered = {addr for m in mail.outbox for addr in (m.to + m.cc + m.bcc)}
        self.assertIn(SRIC_OFFICE, delivered)
        self.assertNotIn(QA_INBOX, delivered)

    def test_test_student_is_not_exempt(self):
        student = User.objects.create_user(
            email="test.student@iic-booking.test", password="pass12345", name="Stu",
            user_type=UserType.STUDENT, department=self.dept, is_test_account=True,
        )
        self.assertFalse(self._client(student).get(SETTINGS).data["project_grant_recharge_enabled"])
