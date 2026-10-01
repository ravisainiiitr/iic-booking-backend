"""Wallet recharge by uploading a payment receipt is discontinued: new submissions are rejected,
while receipts already on file stay listed and processable by finance."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from rest_framework.test import APIClient

from iic_booking.users.api.payment_views import RECEIPT_UPLOAD_RECHARGE_DISCONTINUED_MESSAGE
from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.payment import (
    DepartmentPaymentReceipt,
    DepartmentPaymentReceiptPurpose,
    DepartmentPaymentReceiptStatus,
)
from iic_booking.users.models.wallet import SubWallet, WalletJoinRequest, WalletJoinRequestStatus

User = get_user_model()


class ReceiptUploadRechargeDiscontinuedTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(
            name="IIC Receipt Test",
            code="IRCT",
            department_type=DepartmentType.INTERNAL,
            enable_student_wallet_recharge=True,
        )
        self.faculty = User.objects.create_user(
            email="prof.receipt@test.iitr.ac.in",
            password="pass12345",
            name="Prof. Receipt",
            user_type=UserType.FACULTY,
            department=self.dept,
        )
        self.faculty_wallet, _ = Wallet.objects.get_or_create(user=self.faculty)
        self.student = User.objects.create_user(
            email="student.receipt@test.iitr.ac.in",
            password="pass12345",
            name="Student Receipt",
            user_type=UserType.STUDENT,
            department=self.dept,
        )
        WalletJoinRequest.objects.create(
            student=self.student,
            faculty=self.faculty,
            wallet=self.faculty_wallet,
            status=WalletJoinRequestStatus.APPROVED,
        )
        self.finance = User.objects.create_user(
            email="finance.receipt@test.iitr.ac.in",
            password="pass12345",
            name="Finance Receipt",
            user_type=UserType.FINANCE,
        )

    def test_new_receipt_upload_recharge_is_rejected(self):
        client = APIClient()
        client.force_authenticate(self.student)
        res = client.post(
            "/api/payments/wallet-recharge-receipt/",
            {
                "amount": "500",
                "department_id": str(self.dept.id),
                "utr_reference": "UTR-NEW-1",
                "receipt_file": SimpleUploadedFile("receipt.pdf", b"%PDF-1.4 test", content_type="application/pdf"),
            },
            format="multipart",
        )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["error"], RECEIPT_UPLOAD_RECHARGE_DISCONTINUED_MESSAGE)
        self.assertEqual(res.data["code"], "receipt_upload_recharge_discontinued")
        self.assertFalse(DepartmentPaymentReceipt.objects.exists())

    def test_existing_receipt_request_is_still_listed_and_processed(self):
        receipt = DepartmentPaymentReceipt.objects.create(
            utr_reference="FILE-OLD-1",
            department=self.dept,
            user=self.student,
            amount=Decimal("750.00"),
            purpose=DepartmentPaymentReceiptPurpose.WALLET_RECHARGE,
            receipt_file=SimpleUploadedFile("old.pdf", b"%PDF-1.4 old", content_type="application/pdf"),
        )
        client = APIClient()
        client.force_authenticate(self.finance)

        listed = client.get("/api/finance/payment-receipts/", {"status": "PENDING"})
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([r["id"] for r in listed.data["receipts"]], [receipt.id])
        self.assertTrue(listed.data["receipts"][0]["has_receipt_file"])

        processed = client.post(
            f"/api/finance/payment-receipts/{receipt.id}/process/", {"remarks": "Verified"}, format="json"
        )
        self.assertEqual(processed.status_code, 200)
        receipt.refresh_from_db()
        self.assertEqual(receipt.status, DepartmentPaymentReceiptStatus.PROCESSED)
        self.assertEqual(receipt.finance_processed_by, self.finance)
        sub = SubWallet.objects.get(wallet=self.faculty_wallet, department=self.dept)
        self.assertEqual(sub.balance, Decimal("750.00"))
