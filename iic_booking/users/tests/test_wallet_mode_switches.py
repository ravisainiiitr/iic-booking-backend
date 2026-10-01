"""Main Administrator switches for wallet funding / transfer options and credit caps."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.wallet import SubWallet, WalletRechargeRequest
from iic_booking.users.models.wallet_credit_facility import WalletCreditPolicy
from iic_booking.users.models.wallet_sric_settings import WalletSricSettings
from iic_booking.users.wallet_credit_facility_v2 import (
    WalletCreditError,
    approve_facility,
    create_and_submit_request,
)

User = get_user_model()

SETTINGS = "/api/wallet/student-recharge/settings/"
SEND_OTP = "/api/wallet/recharge-request/send-otp/"
ADMIN_MODES = "/api/admin/wallet-mode-settings/"
GATEWAY_ORDER = "/api/payments/razorpay/create-order/"
LEGACY_GATEWAY_ORDER = "/api/wallet/razorpay/create-order/"
PEER_SEND_OTP = "/api/wallet/peer-transfer/send-otp/"
PEER_CONFIRM = "/api/wallet/peer-transfer/confirm/"


@override_settings(
    EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
    WALLET_CREDIT_FACILITY_V2_ENABLED=True,
)
class WalletModeSwitchTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Modes Dept", code="MOD", department_type=DepartmentType.INTERNAL)
        self.faculty = User.objects.create_user(
            email="fac.modes@test.iitr.ac.in", password="pass12345", name="Fac", user_type=UserType.FACULTY,
            department=self.dept,
        )
        self.admin = User.objects.create_user(
            email="admin.modes@test.iitr.ac.in", password="pass12345", name="Admin", user_type=UserType.ADMIN,
        )
        self.wallet, _ = Wallet.objects.get_or_create(user=self.faculty)
        SubWallet.objects.create(wallet=self.wallet, department=self.dept, balance=Decimal("0"))
        self.api = APIClient()
        self.api.force_authenticate(self.faculty)

    def _set(self, **flags):
        WalletSricSettings.objects.update_or_create(pk=1, defaults=flags)

    def test_defaults_keep_current_behaviour(self):
        data = self.api.get(SETTINGS).data
        self.assertFalse(data["project_grant_recharge_enabled"])
        self.assertTrue(data["direct_cash_recharge_enabled"])
        self.assertFalse(data["online_gateway_recharge_enabled"])
        self.assertTrue(data["peer_transfer_enabled"])
        self.assertEqual(data["disabled_message"], "Awaiting Competent Authority Approval.")

    def test_direct_cash_blocked_when_disabled(self):
        self._set(direct_cash_recharge_enabled=False)
        res = self.api.post(
            SEND_OTP,
            {
                "amount": "1000.00",
                "department_id": self.dept.id,
                "recharge_mode": "direct_cash_deposit",
                "undertaking_accepted": True,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "direct_cash_recharge_disabled")
        self.assertIn("Awaiting Competent Authority Approval", res.data["error"])
        self.assertFalse(WalletRechargeRequest.objects.exists())

    def test_direct_cash_draft_cannot_be_verified_after_switch_off(self):
        res = self.api.post(
            SEND_OTP,
            {
                "amount": "1000.00",
                "department_id": self.dept.id,
                "recharge_mode": "direct_cash_deposit",
                "undertaking_accepted": True,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        rid = res.data["request_id"]
        otp = WalletRechargeRequest.objects.get(pk=rid).user_otp_code
        self._set(direct_cash_recharge_enabled=False)
        res = self.api.post("/api/wallet/recharge-request/", {"request_id": rid, "user_otp": otp}, format="json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "direct_cash_recharge_disabled")

    def test_online_gateway_blocked_when_disabled(self):
        for url, payload in (
            (GATEWAY_ORDER, {"purpose": "WALLET_RECHARGE", "amount": "500", "department_id": self.dept.id}),
            (LEGACY_GATEWAY_ORDER, {"amount": "500", "department_id": self.dept.id}),
        ):
            res = self.api.post(url, payload, format="json")
            self.assertEqual(res.status_code, 403, url)
            self.assertEqual(res.data["code"], "online_gateway_recharge_disabled")

    def test_peer_transfer_blocked_when_disabled(self):
        self._set(peer_transfer_enabled=False)
        res = self.api.post(
            PEER_SEND_OTP, {"department_id": self.dept.id, "recipient_id": self.admin.id, "amount": "10"}, format="json"
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "peer_transfer_disabled")
        res = self.api.post(PEER_CONFIRM, {"transfer_id": 1, "otp": "123456"}, format="json")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "peer_transfer_disabled")

    def test_admin_endpoint_is_main_admin_only(self):
        self.assertEqual(self.api.get(ADMIN_MODES).status_code, 403)
        self.assertEqual(self.api.patch(f"{ADMIN_MODES}1/", {"peer_transfer_enabled": False}, format="json").status_code, 403)

    def test_admin_updates_switches_and_credit_caps(self):
        admin_api = APIClient()
        admin_api.force_authenticate(self.admin)
        res = admin_api.patch(
            f"{ADMIN_MODES}1/",
            {
                "project_grant_recharge_enabled": True,
                "direct_cash_recharge_enabled": False,
                "online_gateway_recharge_enabled": True,
                "peer_transfer_enabled": False,
                "credit_facility_enabled": True,
                "credit_max_amount": "75000.00",
                "credit_max_days": 45,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertTrue(res.data["credit_facility_available_in_environment"])
        s = WalletSricSettings.get_singleton()
        self.assertTrue(s.project_grant_recharge_enabled)
        self.assertFalse(s.direct_cash_recharge_enabled)
        self.assertTrue(s.online_gateway_recharge_enabled)
        self.assertFalse(s.peer_transfer_enabled)
        policy = WalletCreditPolicy.get_solo()
        self.assertTrue(policy.enabled)
        self.assertEqual(policy.max_credit_amount, Decimal("75000.00"))
        self.assertGreaterEqual(policy.max_outstanding_amount, Decimal("75000.00"))
        self.assertEqual(policy.max_credit_duration_days, 45)

        data = self.api.get(SETTINGS).data
        self.assertTrue(data["credit_facility_enabled"])
        self.assertFalse(data["direct_cash_recharge_enabled"])

        res = admin_api.patch(f"{ADMIN_MODES}1/", {"credit_max_days": 0}, format="json")
        self.assertEqual(res.status_code, 400)

    def test_credit_due_date_capped_by_max_days(self):
        policy = WalletCreditPolicy.get_solo()
        policy.enabled = True
        policy.max_credit_amount = Decimal("5000.00")
        policy.min_request_amount = Decimal("100.00")
        policy.max_credit_duration_days = 10
        policy.save()
        Department.objects.filter(pk=self.dept.pk).update(enable_wallet_credit=True)
        facility = create_and_submit_request(
            user=self.faculty, requested_amount=Decimal("1000"), purpose="buffer", department_id=self.dept.id
        )
        with self.assertRaises(WalletCreditError) as ctx:
            approve_facility(
                facility=facility,
                actor=self.admin,
                approved_amount=Decimal("1000"),
                due_date=timezone.localdate() + timedelta(days=11),
            )
        self.assertEqual(ctx.exception.code, "DUE_DATE_TOO_LATE")
        facility = approve_facility(
            facility=facility,
            actor=self.admin,
            approved_amount=Decimal("1000"),
            due_date=timezone.localdate() + timedelta(days=10),
        )
        self.assertEqual(facility.due_date, timezone.localdate() + timedelta(days=10))

        with self.assertRaises(WalletCreditError) as ctx:
            create_and_submit_request(
                user=self.faculty, requested_amount=Decimal("6000"), purpose="too much", department_id=self.dept.id
            )
        self.assertIn(ctx.exception.code, {"AMOUNT_TOO_HIGH", "ACTIVE_CREDIT_EXISTS"})

    def test_credit_request_shows_awaiting_approval_when_disabled(self):
        policy = WalletCreditPolicy.get_solo()
        policy.enabled = False
        policy.save()
        res = self.api.get("/api/wallet/credit-requests/summary/")
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data["feature_enabled"])
        self.assertEqual(res.data["eligibility"]["message"], "Awaiting Competent Authority Approval.")
