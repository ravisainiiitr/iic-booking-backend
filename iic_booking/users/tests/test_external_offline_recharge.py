"""External users' wallet recharge against an offline payment: submission, scoped approval, single credit,
rejection and reconciliation."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.external_services.models import ExternalNotification
from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.offline_recharge import OfflineRechargePayment, OfflineReconciliationStatus
from iic_booking.users.models.payment import DepartmentPaymentReceipt, DepartmentPaymentReceiptStatus
from iic_booking.users.models.wallet import (
    SubWallet,
    SubWalletTransaction,
    WalletRechargeRequest,
    WalletRechargeRequestAuditLog,
    WalletRechargeRequestStatus,
)

User = get_user_model()
BASE = "/api/wallet/offline-recharge/"
PDF = b"%PDF-1.4 receipt"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"


def _user(email, user_type, department=None, **extra):
    return User.objects.create_user(
        email=email,
        password="pass12345",
        name=email.split("@")[0],
        user_type=user_type,
        department=department,
        admin_approved=True,  # is_active follows admin_approved (User.save)
        email_verified=True,
        **extra,
    )


@override_settings(EXTERNAL_OFFLINE_RECHARGE_ALL_EXTERNAL_USERS=True)
class OfflineRechargeTestBase(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="IIC Offline", code="IICOFF", department_type=DepartmentType.INTERNAL)
        self.other = Department.objects.create(name="Physics Offline", code="PHYOFF", department_type=DepartmentType.INTERNAL)
        self.org = Department.objects.create(name="Acme Labs", code="ACMEOFF", department_type=DepartmentType.EXTERNAL)
        self.external = _user("ext.offline@example.com", UserType.EXTERNAL, self.org)
        self.wallet, _ = Wallet.objects.get_or_create(user=self.external)
        SubWallet.objects.create(wallet=self.wallet, department=self.dept, balance=Decimal("0.00"))
        SubWallet.objects.create(wallet=self.wallet, department=self.other, balance=Decimal("0.00"))
        self.admin = _user("main.admin.off@iitr.ac.in", UserType.ADMIN)
        self.dept_admin = _user("dadmin.off@iitr.ac.in", UserType.DEPT_ADMIN, self.dept)
        self.dept_admin_other = _user("dadmin.phy.off@iitr.ac.in", UserType.DEPT_ADMIN, self.other)
        self.accounts = _user("accounts.off@iitr.ac.in", UserType.FINANCE, self.dept)
        self.accounts_other = _user("accounts.phy.off@iitr.ac.in", UserType.FINANCE, self.other)

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def payload(self, **over):
        data = {
            "amount": "5000",
            "department_id": str(self.dept.id),
            "payment_date": (timezone.localdate() - timedelta(days=1)).isoformat(),
            "payment_mode": "NEFT",
            "transaction_reference": "SBIN 1234 5678",
            "payer_bank": "State Bank of India",
            "payer_account_name": "Acme Labs Pvt Ltd",
            "paid_into": "IIC testing grant",
            "remarks": "Advance for XRD",
            "receipt_file": SimpleUploadedFile("receipt.pdf", PDF, content_type="application/pdf"),
        }
        data.update(over)
        return data

    def submit(self, **over):
        res = self.client_for(self.external).post(BASE, self.payload(**over), format="multipart")
        return res

    def submitted(self, **over) -> WalletRechargeRequest:
        res = self.submit(**over)
        self.assertEqual(res.status_code, 201, res.data)
        return WalletRechargeRequest.objects.get(pk=res.data["request"]["id"])

    def balance(self, dept=None) -> Decimal:
        return SubWallet.objects.get(wallet=self.wallet, department=dept or self.dept).balance

    def act(self, user, req, action, **data):
        return self.client_for(user).post(f"{BASE}{req.pk}/{action}/", data, format="json")


class SubmissionTests(OfflineRechargeTestBase):
    def test_submit_creates_pending_request_with_details_receipt_audit_and_notifications(self):
        req = self.submitted(client_request_id="c-1")
        self.assertEqual(req.status, WalletRechargeRequestStatus.PENDING)
        self.assertEqual(req.recharge_mode, "offline_payment")
        detail = req.offline_payment
        self.assertEqual(detail.transaction_reference, "SBIN12345678")
        self.assertEqual(detail.claimed_amount, Decimal("5000.00"))
        self.assertEqual(detail.payer_bank, "State Bank of India")
        self.assertEqual(detail.paid_into, "IIC testing grant")
        self.assertEqual(detail.receipt.status, DepartmentPaymentReceiptStatus.PENDING)
        self.assertTrue(detail.receipt.receipt_file.name.endswith(".pdf"))
        self.assertEqual(self.balance(), Decimal("0.00"))
        self.assertEqual(list(req.audit_logs.values_list("action", flat=True)), ["offline_payment_submitted"])
        notified = set(ExternalNotification.objects.values_list("recipient__email", flat=True))
        self.assertIn(self.external.email, notified)
        self.assertTrue({self.admin.email, self.dept_admin.email, self.accounts.email} <= notified)
        self.assertNotIn(self.dept_admin_other.email, notified)
        self.assertNotIn(self.accounts_other.email, notified)

    def test_camera_photo_is_accepted(self):
        req = self.submitted(receipt_file=SimpleUploadedFile("photo.jpg", JPEG, content_type="image/jpeg"))
        self.assertTrue(req.offline_payment.receipt.receipt_file.name.endswith(".jpg"))

    def test_repeat_submit_with_same_client_id_returns_the_same_request(self):
        first = self.submitted(client_request_id="same-key")
        again = self.submit(client_request_id="same-key")
        self.assertEqual(again.status_code, 200)
        self.assertFalse(again.data["created"])
        self.assertEqual(again.data["request"]["id"], first.pk)
        self.assertEqual(OfflineRechargePayment.objects.count(), 1)

    def test_validation(self):
        cases = [
            ({"receipt_file": ""}, "receipt_file"),
            ({"receipt_file": SimpleUploadedFile("x.pdf", b"not a pdf", content_type="application/pdf")}, "receipt_file"),
            ({"receipt_file": SimpleUploadedFile("x.exe", PDF)}, "receipt_file"),
            ({"payment_date": (timezone.localdate() + timedelta(days=1)).isoformat()}, "payment_date"),
            ({"payment_mode": "BITCOIN"}, "payment_mode"),
            ({"payment_mode": "OTHER"}, "payment_mode_other"),
            ({"transaction_reference": ""}, "transaction_reference"),
            ({"payer_bank": ""}, "payer_bank"),
            ({"payer_account_name": ""}, "payer_account_name"),
            ({"amount": "0"}, "amount"),
        ]
        for over, field in cases:
            with self.subTest(field=field, over=list(over)):
                res = self.submit(**over)
                self.assertEqual(res.status_code, 400, res.data)
                self.assertEqual(res.data.get("field"), field)
        self.assertFalse(WalletRechargeRequest.objects.exists())

    def test_duplicate_reference_is_refused(self):
        self.submitted()
        res = self.submit(department_id=str(self.other.id), transaction_reference="sbin12345678")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "duplicate_reference")

    def test_internal_users_cannot_submit(self):
        faculty = _user("prof.off@iitr.ac.in", UserType.FACULTY, self.dept)
        res = self.client_for(faculty).post(BASE, self.payload(), format="multipart")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "not_external")

    @override_settings(EXTERNAL_OFFLINE_RECHARGE_ALL_EXTERNAL_USERS=False)
    def test_pilot_is_limited_to_test_accounts(self):
        self.assertEqual(self.submit().status_code, 403)
        User.objects.filter(pk=self.external.pk).update(is_test_account=True)
        self.external.refresh_from_db()
        self.assertEqual(self.submit().status_code, 201)

    def approver_notice_recipients(self, req):
        return set(
            ExternalNotification.objects.filter(key=f"offline-recharge:{req.pk}:to-approvers").values_list(
                "recipient_id", flat=True
            )
        )

    def test_test_account_request_is_announced_only_to_test_approvers(self):
        User.objects.filter(pk__in=[self.external.pk, self.accounts.pk]).update(is_test_account=True)
        self.external.refresh_from_db()
        req = self.submitted()
        self.assertEqual(self.approver_notice_recipients(req), {self.accounts.pk})

    def test_real_request_is_not_announced_to_test_approvers(self):
        User.objects.filter(pk=self.accounts.pk).update(is_test_account=True)
        req = self.submitted()
        self.assertEqual(self.approver_notice_recipients(req), {self.admin.pk, self.dept_admin.pk})

    def test_user_list_shows_state(self):
        self.submitted()
        res = self.client_for(self.external).get(BASE)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["available"])
        self.assertEqual(res.data["requests"][0]["state"], "pending")
        self.assertTrue(res.data["requests"][0]["can_cancel"])
        legacy = self.client_for(self.external).get("/api/wallet/recharge-requests/")
        self.assertEqual(legacy.status_code, 200)
        self.assertEqual(legacy.data["count"], 0)


class ApproverScopeTests(OfflineRechargeTestBase):
    def test_out_of_scope_approvers_and_the_user_cannot_decide(self):
        req = self.submitted()
        for user in (self.dept_admin_other, self.accounts_other, self.external):
            with self.subTest(user=user.email):
                self.assertEqual(self.act(user, req, "approve", note="ok").status_code, 403)
                self.assertEqual(self.act(user, req, "reject", note="no").status_code, 403)
        req.refresh_from_db()
        self.assertEqual(req.status, WalletRechargeRequestStatus.PENDING)
        self.assertEqual(self.balance(), Decimal("0.00"))

    def test_each_in_scope_role_can_approve(self):
        for approver in (self.admin, self.dept_admin, self.accounts):
            with self.subTest(role=approver.user_type):
                req = self.submitted(transaction_reference=f"UTR-{approver.pk}")
                res = self.act(approver, req, "approve", note="Matches bank statement")
                self.assertEqual(res.status_code, 200, res.data)
                self.assertEqual(res.data["request"]["state"], "approved")
        self.assertEqual(self.balance(), Decimal("15000.00"))

    def test_main_admin_acts_across_departments(self):
        req = self.submitted(department_id=str(self.other.id))
        self.assertEqual(self.act(self.dept_admin, req, "approve", note="x").status_code, 403)
        self.assertEqual(self.act(self.admin, req, "approve", note="ok").status_code, 200)
        self.assertEqual(self.balance(self.other), Decimal("5000.00"))

    def test_queue_is_scoped(self):
        mine = self.submitted()
        theirs = self.submitted(department_id=str(self.other.id), transaction_reference="UTR-PHY")
        ids = lambda user: {r["id"] for r in self.client_for(user).get(f"{BASE}queue/").data["requests"]}  # noqa: E731
        self.assertEqual(ids(self.admin), {mine.pk, theirs.pk})
        self.assertEqual(ids(self.dept_admin), {mine.pk})
        self.assertEqual(ids(self.accounts_other), {theirs.pk})
        self.assertEqual(self.client_for(self.external).get(f"{BASE}queue/").status_code, 403)
        self.assertEqual(self.client_for(self.dept_admin).get(f"{BASE}{theirs.pk}/").status_code, 404)
        detail = self.client_for(self.dept_admin).get(f"{BASE}{mine.pk}/")
        self.assertEqual(detail.status_code, 200)
        self.assertTrue(detail.data["request"]["can_decide"])
        self.assertEqual(detail.data["request"]["audit"][0]["action"], "offline_payment_submitted")

    def test_note_is_required(self):
        req = self.submitted()
        self.assertEqual(self.act(self.accounts, req, "approve", note="  ").status_code, 400)
        self.assertEqual(self.act(self.accounts, req, "reject").status_code, 400)


class SingleCreditTests(OfflineRechargeTestBase):
    def test_second_approval_never_credits_again(self):
        req = self.submitted()
        self.assertEqual(self.act(self.accounts, req, "approve", note="ok").status_code, 200)
        again = self.act(self.admin, req, "approve", note="again")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.data["code"], "already_processed")
        self.assertEqual(self.balance(), Decimal("5000.00"))
        detail = OfflineRechargePayment.objects.get(recharge_request=req)
        txns = SubWalletTransaction.objects.filter(sub_wallet__wallet=self.wallet, transaction_type="credit")
        self.assertEqual(txns.count(), 1)
        self.assertEqual(detail.wallet_transaction_id, txns.get().pk)
        self.assertEqual(detail.reconciliation_status, OfflineReconciliationStatus.AWAITING)
        self.assertEqual(detail.receipt.status, DepartmentPaymentReceiptStatus.PROCESSED)

    def test_lower_approved_amount_is_credited_and_logged(self):
        req = self.submitted()
        self.assertEqual(self.act(self.accounts, req, "approve", note="x", approved_amount="6000").status_code, 400)
        res = self.act(self.accounts, req, "approve", note="Bank shows 4,500", approved_amount="4500")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.balance(), Decimal("4500.00"))
        self.assertEqual(res.data["request"]["approved_amount"], "4500.00")
        self.assertEqual(res.data["request"]["claimed_amount"], "5000.00")
        self.assertIn("offline_payment_amount_changed", req.audit_logs.values_list("action", flat=True))

    def test_older_recharge_endpoints_cannot_decide_or_credit(self):
        req = self.submitted()
        anon = APIClient()
        self.assertIn(anon.post(f"/api/wallet/recharge-requests/{req.pk}/approve/", {}, format="json").status_code, (401, 403))
        admin = self.client_for(self.admin)
        self.assertEqual(admin.post(f"/api/wallet/recharge-requests/{req.pk}/approve/", {}, format="json").status_code, 409)
        self.assertEqual(
            admin.post(f"/api/wallet/recharge-requests/{req.pk}/reject/", {"reason_text": "x"}, format="json").status_code,
            409,
        )
        token = WalletRechargeRequest.objects.get(pk=req.pk).action_token
        self.assertEqual(anon.post(f"/api/wallet/recharge-action/{token}/approve/", {}, format="json").status_code, 409)
        receipt = req.offline_payment.receipt
        processed = self.client_for(self.accounts).post(f"/api/finance/payment-receipts/{receipt.pk}/process/", {}, format="json")
        self.assertEqual(processed.status_code, 409)
        req.refresh_from_db()
        receipt.refresh_from_db()
        self.assertEqual(req.status, WalletRechargeRequestStatus.PENDING)
        self.assertEqual(receipt.status, DepartmentPaymentReceiptStatus.PENDING)
        self.assertEqual(self.balance(), Decimal("0.00"))

    def test_audit_rows_are_append_only(self):
        req = self.submitted()
        log = WalletRechargeRequestAuditLog.objects.filter(request=req).first()
        log.message = "changed"
        with self.assertRaises(ValueError):
            log.save()


class RejectTests(OfflineRechargeTestBase):
    def test_reject_records_reason_without_credit_and_frees_the_reference(self):
        req = self.submitted()
        res = self.act(self.dept_admin, req, "reject", note="Receipt is unreadable")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["request"]["state"], "rejected")
        req.refresh_from_db()
        self.assertEqual(req.status, WalletRechargeRequestStatus.REJECTED)
        self.assertEqual(req.rejection_reason_text, "Receipt is unreadable")
        self.assertEqual(self.balance(), Decimal("0.00"))
        self.assertFalse(SubWalletTransaction.objects.filter(sub_wallet__wallet=self.wallet).exists())
        receipt = DepartmentPaymentReceipt.objects.get(pk=req.offline_payment.receipt_id)
        self.assertEqual(receipt.status, DepartmentPaymentReceiptStatus.REJECTED)
        self.assertEqual(self.act(self.admin, req, "approve", note="late").status_code, 409)
        self.assertIn("rejected", req.audit_logs.values_list("action", flat=True))
        self.assertTrue(ExternalNotification.objects.filter(key=f"offline-recharge:{req.pk}:rejected").exists())
        # A clearer receipt for the same payment can be sent again.
        self.submitted()

    def test_user_can_cancel_a_pending_request(self):
        req = self.submitted()
        res = self.client_for(self.external).post(f"{BASE}{req.pk}/cancel/")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["request"]["state"], "cancelled")
        self.assertEqual(self.act(self.accounts, req, "approve", note="x").status_code, 409)
        other = _user("ext2.off@example.com", UserType.EXTERNAL, self.org)
        req2 = self.submitted(transaction_reference="UTR-2")
        self.assertEqual(self.client_for(other).post(f"{BASE}{req2.pk}/cancel/").status_code, 404)


class ReconciliationTests(OfflineRechargeTestBase):
    def approved(self):
        req = self.submitted()
        self.assertEqual(self.act(self.accounts, req, "approve", note="ok").status_code, 200)
        return req

    def test_pending_request_cannot_be_reconciled(self):
        req = self.submitted()
        res = self.act(self.accounts, req, "reconcile", outcome="reconciled", reference="R1", credit_date="2026-11-01")
        self.assertEqual(res.status_code, 409)

    def test_only_accounts_in_scope_or_main_admin_reconcile(self):
        req = self.approved()
        for user in (self.dept_admin, self.accounts_other, self.external):
            with self.subTest(user=user.email):
                res = self.act(user, req, "reconcile", outcome="reconciled", reference="R1", credit_date="2026-11-01")
                self.assertEqual(res.status_code, 403)
        res = self.act(self.admin, req, "reconcile", outcome="reconciled", reference="R1", credit_date="2026-11-01")
        self.assertEqual(res.status_code, 200, res.data)

    def test_mismatch_then_reconciled(self):
        req = self.approved()
        self.assertEqual(self.act(self.accounts, req, "reconcile", outcome="mismatch").status_code, 400)
        res = self.act(self.accounts, req, "reconcile", outcome="mismatch", note="Not in grant statement yet")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["request"]["state"], "mismatch")
        self.assertEqual(self.act(self.accounts, req, "reconcile", outcome="reconciled", reference="R9").status_code, 400)
        res = self.act(
            self.accounts, req, "reconcile", outcome="reconciled", reference="GRANT-CR-77", credit_date="2026-11-05"
        )
        self.assertEqual(res.status_code, 200, res.data)
        data = res.data["request"]
        self.assertEqual(data["state"], "reconciled")
        self.assertEqual(data["reconciled_reference"], "GRANT-CR-77")
        self.assertEqual(data["reconciled_credit_date"], "2026-11-05")
        req.refresh_from_db()
        self.assertTrue(req.fund_receipt_verified)
        self.assertEqual(self.balance(), Decimal("5000.00"))  # reconciliation never credits again
        again = self.act(self.accounts, req, "reconcile", outcome="mismatch", note="x")
        self.assertEqual(again.status_code, 409)
        actions = list(req.audit_logs.order_by("created_at", "pk").values_list("action", flat=True))
        self.assertEqual(
            actions,
            [
                "offline_payment_submitted",
                "approved",
                "offline_payment_mismatch",
                "fund_receipt_verified",
                "offline_payment_reconciled",
            ],
        )
        states = {
            s: self.client_for(self.admin).get(f"{BASE}queue/", {"state": s}).data["counts"][s]
            for s in ("pending", "approved", "mismatch", "reconciled", "rejected")
        }
        self.assertEqual(states, {"pending": 0, "approved": 0, "mismatch": 0, "reconciled": 1, "rejected": 0})
