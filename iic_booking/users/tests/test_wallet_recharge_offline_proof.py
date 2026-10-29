"""Offline wallet recharge (bank transfer / cash deposit): the payment proof and UTR are attached to the pending
request, so the Accounts In Charge can check them against the SRIC testing grant before approving. Internal and
external users alike."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.payment import DepartmentPaymentReceipt
from iic_booking.users.models.wallet import WalletRechargeMode, WalletRechargeRequest, WalletRechargeRequestStatus

User = get_user_model()


class OfflineRechargeProofTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="IIC Proof Test", code="IPRT", department_type=DepartmentType.INTERNAL)

    def _user(self, user_type, email):
        user = User.objects.create_user(email=email, password="pass12345", name="Proof User", user_type=user_type,
                                        department=self.dept if user_type == UserType.FACULTY else None)
        Wallet.objects.get_or_create(user=user)
        return user

    def _request(self, user, mode, state=WalletRechargeRequestStatus.PENDING):
        return WalletRechargeRequest.objects.create(user=user, wallet=Wallet.objects.get(user=user),
                                                    department=self.dept, amount=Decimal("2500.00"),
                                                    recharge_mode=mode, status=state)

    def _attach(self, user, req, utr="UTR2026102900001"):
        client = APIClient()
        client.force_authenticate(user)
        return client.post(
            f"/api/wallet/recharge-requests/{req.pk}/attach-receipt/",
            {"utr_reference": utr,
             "receipt_file": SimpleUploadedFile("proof.pdf", b"%PDF-1.4 proof", content_type="application/pdf")},
            format="multipart",
        )

    def test_external_user_attaches_proof_and_utr_to_a_pending_bank_transfer(self):
        user = self._user(UserType.EXTERNAL, "lab@example.org")
        req = self._request(user, WalletRechargeMode.DIRECT_CASH_DEPOSIT)
        res = self._attach(user, req)
        self.assertEqual(res.status_code, 200, res.content)
        req.refresh_from_db()
        self.assertEqual(req.status, WalletRechargeRequestStatus.PENDING)  # still waits for the Accounts In Charge
        self.assertEqual(req.utr_reference, "UTR2026102900001")
        receipt = DepartmentPaymentReceipt.objects.get(wallet_recharge_request=req)
        self.assertTrue(receipt.receipt_file)

    def test_internal_faculty_can_do_the_same(self):
        user = self._user(UserType.FACULTY, "prof.proof@test.iitr.ac.in")
        req = self._request(user, WalletRechargeMode.DIRECT_CASH_DEPOSIT)
        self.assertEqual(self._attach(user, req).status_code, 200)

    def test_a_pending_project_grant_request_takes_no_proof(self):
        user = self._user(UserType.FACULTY, "prof.grant@test.iitr.ac.in")
        req = self._request(user, WalletRechargeMode.PROJECT_GRANT)
        self.assertEqual(self._attach(user, req).status_code, 400)

    def test_another_users_request_is_not_found(self):
        owner = self._user(UserType.EXTERNAL, "owner@example.org")
        other = self._user(UserType.EXTERNAL, "other@example.org")
        req = self._request(owner, WalletRechargeMode.DIRECT_CASH_DEPOSIT)
        self.assertEqual(self._attach(other, req).status_code, 404)
