"""Legacy SRIC transfer-request API: disabled by default (404 for every method, with or without the key);
when re-enabled, the static key is checked with a constant-time comparison."""

from __future__ import annotations

import hmac
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.payment import SricTransferRequest, SricTransferRequestStatus
from iic_booking.users.models.wallet import WalletRechargeRequest

User = get_user_model()
KEY = "unit-test-sric-key"
BASE = "/api/integrations/sric/transfer-requests/"


class _LegacySricBase(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(
            name="IIC SRIC Legacy", code="ISLG", department_type=DepartmentType.INTERNAL
        )
        self.faculty = User.objects.create_user(
            email="fac.sric.legacy@test.iitr.ac.in",
            password="pass12345",
            name="Legacy Faculty",
            user_type=UserType.FACULTY,
            department=self.dept,
        )
        self.wallet, _ = Wallet.objects.get_or_create(user=self.faculty)
        recharge = WalletRechargeRequest.objects.create(
            user=self.faculty,
            wallet=self.wallet,
            department=self.dept,
            amount=Decimal("1000.00"),
            user_otp_verified=True,
        )
        self.transfer = SricTransferRequest.objects.create(
            wallet_recharge_request=recharge,
            department=self.dept,
            grant_code="GRANT-TEST",
            amount=Decimal("1000.00"),
            faculty_email=self.faculty.email,
        )
        self.client = APIClient()

    def urls(self):
        tid = self.transfer.id
        return {
            "list": BASE,
            "detail": f"{BASE}{tid}/",
            "complete": f"{BASE}{tid}/complete/",
            "reject": f"{BASE}{tid}/reject/",
        }


@override_settings(SRIC_API_KEY=KEY, SRIC_LEGACY_TRANSFER_ENDPOINT_ENABLED=False)
class LegacySricDisabledTests(_LegacySricBase):
    def test_every_method_returns_404_with_and_without_key(self):
        for name, url in self.urls().items():
            for headers in ({}, {"HTTP_X_SRIC_API_KEY": KEY}, {"HTTP_X_SRIC_API_KEY": "wrong"}):
                for method in ("get", "post", "put", "patch", "delete"):
                    resp = getattr(self.client, method)(url, {"reason": "x", "sric_reference": "R1"}, format="json", **headers)
                    self.assertEqual(
                        resp.status_code, 404, f"{method.upper()} {name} headers={list(headers)} -> {resp.status_code}"
                    )

    def test_disabled_post_with_key_changes_nothing(self):
        self.client.post(self.urls()["complete"], {"sric_reference": "R1"}, format="json", HTTP_X_SRIC_API_KEY=KEY)
        self.client.post(self.urls()["reject"], {"reason": "x"}, format="json", HTTP_X_SRIC_API_KEY=KEY)
        self.transfer.refresh_from_db()
        self.assertEqual(self.transfer.status, SricTransferRequestStatus.PENDING)
        self.assertEqual(self.transfer.sric_reference, "")

    def test_default_setting_is_disabled(self):
        from django.conf import settings

        with self.settings():
            del settings.SRIC_LEGACY_TRANSFER_ENDPOINT_ENABLED
            resp = self.client.get(BASE, HTTP_X_SRIC_API_KEY=KEY)
        self.assertEqual(resp.status_code, 404)


@override_settings(SRIC_API_KEY=KEY, SRIC_LEGACY_TRANSFER_ENDPOINT_ENABLED=True)
class LegacySricEnabledTests(_LegacySricBase):
    def test_correct_key_uses_constant_time_compare(self):
        with mock.patch(
            "iic_booking.users.api.sric_api_views.hmac.compare_digest", wraps=hmac.compare_digest
        ) as cmp:
            resp = self.client.get(BASE, HTTP_X_SRIC_API_KEY=KEY)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["count"], 1)
        cmp.assert_called_once_with(KEY.encode("utf-8"), KEY.encode("utf-8"))

    def test_wrong_or_missing_key_is_401_via_constant_time_compare(self):
        with mock.patch(
            "iic_booking.users.api.sric_api_views.hmac.compare_digest", wraps=hmac.compare_digest
        ) as cmp:
            self.assertEqual(self.client.get(BASE, HTTP_X_SRIC_API_KEY="wrong").status_code, 401)
            self.assertEqual(self.client.get(BASE).status_code, 401)
        self.assertEqual(cmp.call_count, 2)

    @override_settings(SRIC_API_KEY="")
    def test_unconfigured_key_rejects_everything(self):
        self.assertEqual(self.client.get(BASE, HTTP_X_SRIC_API_KEY="").status_code, 401)
        self.assertEqual(self.client.get(BASE, HTTP_X_SRIC_API_KEY="anything").status_code, 401)

    def test_wrong_method_is_405_only_when_enabled(self):
        self.assertEqual(self.client.post(BASE, {}, format="json", HTTP_X_SRIC_API_KEY=KEY).status_code, 405)

    def test_complete_with_key_records_transfer_without_wallet_credit(self):
        balance_before = self.wallet.total_balance
        resp = self.client.post(
            self.urls()["complete"], {"sric_reference": "R1"}, format="json", HTTP_X_SRIC_API_KEY=KEY
        )
        self.assertEqual(resp.status_code, 200)
        self.transfer.refresh_from_db()
        self.assertEqual(self.transfer.status, SricTransferRequestStatus.TRANSFERRED)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.total_balance, balance_before)
