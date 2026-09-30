"""Admin switch that stops faculty wallet recharge requests via Project Grant."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from iic_booking.users.models import Department, DepartmentType, Project, UserType, Wallet
from iic_booking.users.models.wallet import SubWallet, WalletRechargeMode, WalletRechargeRequest
from iic_booking.users.models.wallet_sric_settings import WalletSricSettings

User = get_user_model()

SEND_OTP = "/api/wallet/recharge-request/send-otp/"
VERIFY = "/api/wallet/recharge-request/"
SETTINGS = "/api/wallet/student-recharge/settings/"


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ProjectGrantRechargeSwitchTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Switch Dept", code="SWD", department_type=DepartmentType.INTERNAL)
        self.faculty = User.objects.create_user(
            email="fac.switch@test.iitr.ac.in", password="pass12345", name="Fac", user_type=UserType.FACULTY,
            department=self.dept,
        )
        self.wallet, _ = Wallet.objects.get_or_create(user=self.faculty)
        SubWallet.objects.create(wallet=self.wallet, department=self.dept, balance=Decimal("0"))
        self.project = Project.objects.create(
            faculty=self.faculty, name="Materials", project_code="IITR/SW/2026/001", agency="DST"
        )
        self.api = APIClient()
        self.api.force_authenticate(self.faculty)

    def _set_enabled(self, enabled: bool):
        WalletSricSettings.objects.update_or_create(pk=1, defaults={"project_grant_recharge_enabled": enabled})

    def _send(self, mode="project_grant"):
        return self.api.post(
            SEND_OTP,
            {
                "amount": "1000.00",
                "department_id": self.dept.id,
                "project_id": self.project.id,
                "recharge_mode": mode,
                "undertaking_accepted": True,
            },
            format="json",
        )

    def test_disabled_by_default(self):
        res = self.api.get(SETTINGS)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["project_grant_recharge_enabled"])

    def test_send_otp_blocked_when_disabled(self):
        self._set_enabled(False)
        res = self._send()
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "project_grant_recharge_disabled")
        self.assertFalse(WalletRechargeRequest.objects.exists())

    def test_direct_cash_unaffected_when_disabled(self):
        self._set_enabled(False)
        res = self._send(mode="direct_cash_deposit")
        self.assertEqual(res.status_code, 200, res.data)
        rid = res.data["request_id"]
        otp = WalletRechargeRequest.objects.get(pk=rid).user_otp_code
        res = self.api.post(VERIFY, {"request_id": rid, "user_otp": otp}, format="json")
        self.assertEqual(res.status_code, 201, res.data)

    def test_draft_created_before_switch_off_cannot_be_submitted(self):
        self._set_enabled(True)
        rid = self._send().data["request_id"]
        otp = WalletRechargeRequest.objects.get(pk=rid).user_otp_code
        self._set_enabled(False)
        res = self.api.post(VERIFY, {"request_id": rid, "user_otp": otp}, format="json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "project_grant_recharge_disabled")
        self.assertFalse(WalletRechargeRequest.objects.filter(pk=rid).exists())

    def test_unsent_request_cannot_be_sent_to_sric_when_disabled(self):
        self._set_enabled(False)
        req = WalletRechargeRequest.objects.create(
            user=self.faculty, wallet=self.wallet, department=self.dept, amount=Decimal("500"),
            project=self.project, recharge_mode=WalletRechargeMode.PROJECT_GRANT, undertaking_accepted=True,
            user_otp_verified=True, sric_notification_sent=False,
        )
        res = self.api.post(f"/api/wallet/recharge-requests/{req.id}/send-sric/", {}, format="json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "project_grant_recharge_disabled")
        req.refresh_from_db()
        self.assertFalse(req.sric_notification_sent)

    def test_enabled_allows_project_grant(self):
        self._set_enabled(True)
        self.assertTrue(self.api.get(SETTINGS).data["project_grant_recharge_enabled"])
        res = self._send()
        self.assertEqual(res.status_code, 200, res.data)
        rid = res.data["request_id"]
        otp = WalletRechargeRequest.objects.get(pk=rid).user_otp_code
        res = self.api.post(VERIFY, {"request_id": rid, "user_otp": otp}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
