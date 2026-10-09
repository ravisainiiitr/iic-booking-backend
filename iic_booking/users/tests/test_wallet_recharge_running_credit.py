"""Running credit: SRIC approval waits for the cash-book fund receipt; SRIC decline gives no second credit;
unmatched requests are flagged on the admin / account in-charge dashboard."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.wallet import (
    SubWallet,
    WalletRechargeCancellationSource,
    WalletRechargeMode,
    WalletRechargeParseEntry,
    WalletRechargeRejectionReason,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
)
from iic_booking.users.models.wallet_sric_settings import WalletSricSettings
from iic_booking.users.wallet_recharge_import import link_cashbook_entry_to_request
from iic_booking.users.wallet_recharge_workflow import (
    approve_request,
    has_running_credit,
    notify_stakeholders_of_decision,
    reject_request,
    serialize_request_public,
    verify_fund_receipt,
)

User = get_user_model()
GRANT = "IIC-000-003"


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class RunningCreditTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(
            name="IIC Running Credit", code="IRCT", department_type=DepartmentType.INTERNAL
        )
        self.faculty = User.objects.create_user(
            email="fac.running@test.iitr.ac.in",
            password="pass12345",
            name="Running Faculty",
            user_type=UserType.FACULTY,
            department=self.dept,
            emp_id="100901",
        )
        self.wallet, _ = Wallet.objects.get_or_create(user=self.faculty)
        s = WalletSricSettings.get_singleton()
        s.recipient_emails = "sric.desk@test.iitr.ac.in"
        s.save()

    def _request(self, amount="1000.00", mode=WalletRechargeMode.PROJECT_GRANT):
        return WalletRechargeRequest.objects.create(
            user=self.faculty,
            wallet=self.wallet,
            department=self.dept,
            amount=Decimal(amount),
            user_otp_verified=True,
            recharge_mode=mode,
            employee_number="100901",
            department_grant_code=GRANT,
            project_grant_code="PRJ-77" if mode == WalletRechargeMode.PROJECT_GRANT else "",
        )

    def _overdraft(self, amount):
        SubWallet.objects.update_or_create(
            wallet=self.wallet, department=self.dept, defaults={"balance": -Decimal(amount)}
        )

    def _balance(self) -> Decimal:
        sw = SubWallet.objects.filter(wallet=self.wallet, department=self.dept).first()
        return sw.balance if sw else Decimal("0")

    def _entry(self, amount, receipt="R-501"):
        return WalletRechargeParseEntry.objects.create(
            receipt_no=receipt,
            dated=date(2026, 10, 6),
            emp_no="100901",
            amount=amount,
            credited_to_project_no=GRANT,
            name="Running Faculty",
        )

    def test_no_running_credit_credits_immediately(self):
        self.assertFalse(has_running_credit(self.wallet, self.dept.id))
        approved = approve_request(self._request(), actor_email="sric@test")
        self.assertFalse(approved.wallet_credit_pending)
        self.assertIsNotNone(approved.wallet_credited_at)
        self.assertEqual(self._balance(), Decimal("1000.00"))

    def test_approval_with_overdraft_waits_for_cashbook_then_adjusts(self):
        self._overdraft("3000.00")
        self.assertTrue(has_running_credit(self.wallet, self.dept.id))
        approved = approve_request(self._request("5000.00"), actor_email="sric@test")
        self.assertEqual(approved.status, WalletRechargeRequestStatus.APPROVED)
        self.assertTrue(approved.wallet_credit_pending)
        self.assertIsNone(approved.wallet_credited_at)
        self.assertEqual(self._balance(), Decimal("-3000.00"))
        self.assertTrue(approved.audit_logs.filter(action="approved_credit_deferred").exists())
        self.assertTrue(serialize_request_public(approved)["wallet_credit_pending"])

        notify_stakeholders_of_decision(approved)
        faculty_mail = next(m for m in mail.outbox if "fac.running@test.iitr.ac.in" in m.to)
        self.assertIn("credit on receipt of funds", faculty_mail.subject)
        mail.outbox.clear()

        with self.captureOnCommitCallbacks(execute=True):
            updated, outcome = link_cashbook_entry_to_request(
                approved.pk, self._entry("5,000.00").pk, actor_email="sric-cashbook-auto"
            )
        self.assertEqual(outcome, "verified")
        self.assertFalse(updated.wallet_credit_pending)
        self.assertIsNotNone(updated.wallet_credited_at)
        self.assertEqual(updated.credit_settled_amount, Decimal("3000.00"))
        self.assertEqual(self._balance(), Decimal("2000.00"))
        self.assertTrue(updated.audit_logs.filter(action="wallet_credited_on_fund_receipt").exists())
        self.assertTrue(any("Funds received" in m.subject for m in mail.outbox))

    def test_manual_fund_receipt_verification_also_credits(self):
        self._overdraft("500.00")
        approved = approve_request(self._request("1000.00"), actor_email="sric@test")
        verified = verify_fund_receipt(approved, actor=None, remarks="checked")
        self.assertFalse(verified.wallet_credit_pending)
        self.assertEqual(verified.credit_settled_amount, Decimal("500.00"))
        self.assertEqual(self._balance(), Decimal("500.00"))
        with self.assertRaises(ValueError):
            verify_fund_receipt(verified, actor=None)
        self.assertEqual(self._balance(), Decimal("500.00"))

    def test_pending_cashbook_match_with_running_credit_credits_once(self):
        self._overdraft("400.00")
        req = self._request("1000.00")
        updated, outcome = link_cashbook_entry_to_request(req.pk, self._entry("1,000.00").pk)
        self.assertEqual(outcome, "approved")
        self.assertFalse(updated.wallet_credit_pending)
        self.assertEqual(self._balance(), Decimal("600.00"))

    def test_decline_while_credit_running_cancels_without_new_credit(self):
        reject_request(self._request("1000.00"), reason_code=WalletRechargeRejectionReason.WRONG_PROJECT_GRANT)
        self.assertEqual(self._balance(), Decimal("1000.00"))
        second = reject_request(
            self._request("2000.00"), reason_code=WalletRechargeRejectionReason.INSUFFICIENT_BALANCE
        )
        self.assertEqual(second.status, WalletRechargeRequestStatus.CANCELLED)
        self.assertEqual(second.cancellation_source, WalletRechargeCancellationSource.SRIC_DECLINED)
        self.assertEqual(second.decline_credit_amount, Decimal("0.00"))
        self.assertEqual(second.decline_credit_outstanding, Decimal("0.00"))
        self.assertEqual(self._balance(), Decimal("1000.00"))
        self.assertTrue(second.audit_logs.filter(action="declined_credit_already_running").exists())

        mail.outbox.clear()
        notify_stakeholders_of_decision(second)
        faculty_mail = next(m for m in mail.outbox if "fac.running@test.iitr.ac.in" in m.to)
        self.assertIn("credit already running", faculty_mail.subject)
        self.assertIn("no new credit is given", faculty_mail.body)

    def test_decline_of_deferred_approval_never_touches_wallet(self):
        self._overdraft("500.00")
        s = WalletSricSettings.get_singleton()
        s.decline_converts_to_credit = False
        s.save()
        approved = approve_request(self._request("1000.00"), actor_email="sric@test")
        self.assertTrue(serialize_request_public(approved)["can_decline"])
        declined = reject_request(approved, reason_code=WalletRechargeRejectionReason.WRONG_PROJECT_GRANT)
        self.assertEqual(declined.status, WalletRechargeRequestStatus.CANCELLED)
        self.assertFalse(declined.wallet_credit_pending)
        self.assertEqual(declined.decline_credit_amount, Decimal("0.00"))
        self.assertEqual(self._balance(), Decimal("-500.00"))

    def test_cash_deposit_is_credited_immediately_even_with_running_credit(self):
        self._overdraft("300.00")
        approved = approve_request(
            self._request("1000.00", mode=WalletRechargeMode.DIRECT_CASH_DEPOSIT), actor_email="cashier@test"
        )
        self.assertFalse(approved.wallet_credit_pending)
        self.assertEqual(approved.credit_settled_amount, Decimal("300.00"))
        self.assertEqual(self._balance(), Decimal("700.00"))

    def test_token_approve_message_explains_deferral(self):
        self._overdraft("100.00")
        req = self._request()
        from iic_booking.users.wallet_recharge_workflow import populate_request_snapshots

        populate_request_snapshots(req)
        resp = APIClient().post(f"/api/wallet/recharge-action/{req.action_token}/approve/", {}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["wallet_credit_pending"])
        self.assertIn("only after the SRIC cash-book confirms", resp.json()["message"])


class OverdueFundReceiptTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(
            name="IIC Overdue", code="IOVD", department_type=DepartmentType.INTERNAL
        )
        self.other_dept = Department.objects.create(
            name="Physics Overdue", code="POVD", department_type=DepartmentType.INTERNAL
        )
        self.faculty = User.objects.create_user(
            email="fac.overdue@test.iitr.ac.in",
            password="pass12345",
            name="Overdue Faculty",
            user_type=UserType.FACULTY,
            department=self.dept,
            emp_id="100902",
        )
        self.wallet, _ = Wallet.objects.get_or_create(user=self.faculty)
        now = timezone.now()
        self.old_approved = self._make(WalletRechargeRequestStatus.APPROVED, responded=now - timedelta(days=20))
        self.recent_approved = self._make(WalletRechargeRequestStatus.APPROVED, responded=now - timedelta(days=5))
        self.old_pending = self._make(WalletRechargeRequestStatus.PENDING, created=now - timedelta(days=16))
        self.old_matched = self._make(
            WalletRechargeRequestStatus.APPROVED, responded=now - timedelta(days=30), receipt="R-1"
        )
        self.other_dept_old = self._make(
            WalletRechargeRequestStatus.APPROVED, responded=now - timedelta(days=20), dept=self.other_dept
        )

    def _make(self, status, *, responded=None, created=None, receipt="", dept=None):
        req = WalletRechargeRequest.objects.create(
            user=self.faculty,
            wallet=self.wallet,
            department=dept or self.dept,
            amount=Decimal("1000.00"),
            user_otp_verified=True,
            status=status,
            responded_at=responded,
            cashbook_receipt_no=receipt,
            fund_receipt_verified=bool(receipt),
        )
        if created:
            WalletRechargeRequest.objects.filter(pk=req.pk).update(created_at=created)
        return req

    def _get(self, user):
        client = APIClient()
        client.force_authenticate(user)
        return client.get("/api/admin/wallet-recharge-requests/overdue-fund-receipts/")

    def test_main_admin_sees_all_overdue(self):
        admin = User.objects.create_user(
            email="admin.overdue@test.iitr.ac.in", password="pass12345", name="Admin", user_type=UserType.ADMIN
        )
        resp = self._get(admin)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(data["days"], 15)
        ids = {r["id"] for r in data["results"]}
        self.assertEqual(ids, {self.old_approved.id, self.old_pending.id, self.other_dept_old.id})
        self.assertEqual(data["count"], 3)

        client = APIClient()
        client.force_authenticate(admin)
        listed = client.get("/api/admin/wallet-recharge-requests/", {"overdue": "1"}).json()
        rows = listed.get("results", listed) if isinstance(listed, dict) else listed
        self.assertEqual({r["id"] for r in rows}, ids)

    def test_account_incharge_sees_own_department_only(self):
        incharge = User.objects.create_user(
            email="acc.overdue@test.iitr.ac.in",
            password="pass12345",
            name="Accounts",
            user_type=UserType.FINANCE,
            department=self.dept,
        )
        data = self._get(incharge).json()
        self.assertEqual({r["id"] for r in data["results"]}, {self.old_approved.id, self.old_pending.id})

    def test_threshold_is_configurable(self):
        s = WalletSricSettings.get_singleton()
        s.fund_receipt_overdue_days = 3
        s.save()
        admin = User.objects.create_user(
            email="admin2.overdue@test.iitr.ac.in", password="pass12345", name="Admin", user_type=UserType.ADMIN
        )
        data = self._get(admin).json()
        self.assertEqual(data["days"], 3)
        self.assertIn(self.recent_approved.id, {r["id"] for r in data["results"]})

    def test_department_admin_gets_no_alert(self):
        dept_admin = User.objects.create_user(
            email="dadmin.overdue@test.iitr.ac.in",
            password="pass12345",
            name="Dept Admin",
            user_type=UserType.DEPT_ADMIN,
            department=self.dept,
        )
        resp = self._get(dept_admin)
        self.assertTrue(resp.status_code == 403 or resp.json()["count"] == 0)
