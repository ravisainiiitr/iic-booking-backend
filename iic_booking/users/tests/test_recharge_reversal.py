"""Reversal of Project Grant recharges approved only by the requester / wallet owner (reverse_unconfirmed_recharge)."""

from __future__ import annotations

from decimal import Decimal
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from iic_booking.users import recharge_reversal as rr
from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.wallet import (
    SubWallet,
    WalletRechargeCancellationSource,
    WalletRechargeMode,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
)
from iic_booking.users.models.wallet_admin_adjustment import WalletAdminAdjustment
from iic_booking.users.models.wallet_sric_settings import WalletSricSettings
from iic_booking.users.wallet_recharge_workflow import approve_request

User = get_user_model()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class RechargeReversalTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="IIC Reversal Test", code="IRVT", department_type=DepartmentType.INTERNAL)
        self.admin = User.objects.create_superuser(
            email="main.admin@test.iitr.ac.in", password="pass12345", name="Main Admin", user_type=UserType.ADMIN,
        )
        self.faculty = User.objects.create_user(
            email="fac.reverse@test.iitr.ac.in", password="pass12345", name="Reverse Faculty",
            user_type=UserType.FACULTY, department=self.dept, emp_id="100777", admin_approved=True,
        )
        self.student = User.objects.create_user(
            email="stu.reverse@test.iitr.ac.in", password="pass12345", name="Reverse Student",
            user_type=UserType.STUDENT, department=self.dept, admin_approved=True,
        )
        self.bystander = User.objects.create_user(
            email="other.fac@test.iitr.ac.in", password="pass12345", name="Other Faculty",
            user_type=UserType.FACULTY, department=self.dept, admin_approved=True,
        )
        self.wallet, _ = Wallet.objects.get_or_create(user=self.faculty)
        s = WalletSricSettings.get_singleton()
        s.recipient_emails = "sric.desk@test.iitr.ac.in"
        s.save()

    def _request(self, amount="1000.00", user=None):
        return WalletRechargeRequest.objects.create(
            user=user or self.faculty,
            wallet=self.wallet,
            department=self.dept,
            amount=Decimal(amount),
            user_otp_verified=True,
            recharge_mode=WalletRechargeMode.PROJECT_GRANT,
            employee_number="100777",
            department_grant_code="IIC-000-002",
            project_grant_code="PRJ-7",
        )

    def _self_approved(self, amount="1000.00", user=None):
        actor = user or self.faculty
        req = approve_request(self._request(amount, user=actor), actor=actor)
        mail.outbox.clear()
        return req

    def _sub(self):
        return SubWallet.objects.get(wallet=self.wallet, department=self.dept)

    def _run(self, *txns, apply=False, confirm=None, extra=()):
        out = StringIO()
        args = []
        for t in txns:
            args += ["--txn", t]
        if apply:
            args += ["--apply", "--confirm", confirm if confirm is not None else f"REVERSE {','.join(txns)}"]
        call_command("reverse_unconfirmed_recharge", *args, *extra, stdout=out)
        return out.getvalue()

    def test_dry_run_reports_plan_and_changes_nothing(self):
        req = self._self_approved()
        out = self._run(req.transaction_number, extra=["--preview-notice"])
        self.assertIn("approval_source=user", out)
        self.assertIn("eligible=True", out)
        self.assertIn("plan: debit 1000.00", out)
        self.assertIn("rnd.iitr.ac.in", out)
        self.assertNotIn("fac.reverse@", out)
        self.assertNotIn("Reverse Faculty", out)
        self.assertNotIn("PRJ-7", out)
        req.refresh_from_db()
        self.assertEqual(req.status, WalletRechargeRequestStatus.APPROVED)
        self.assertEqual(self._sub().balance, Decimal("1000.00"))
        self.assertFalse(WalletAdminAdjustment.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_apply_needs_exact_confirm(self):
        req = self._self_approved()
        with self.assertRaises(CommandError):
            self._run(req.transaction_number, apply=True, confirm="REVERSE")
        self.assertEqual(self._sub().balance, Decimal("1000.00"))

    def test_apply_debits_cancels_audits_and_notifies_owner_only(self):
        sub = SubWallet.objects.create(wallet=self.wallet, department=self.dept, balance=Decimal("0.00"))
        sub.credit(Decimal("500.00"), "Opening")
        req = self._self_approved("1000.00")
        self.assertEqual(self._sub().balance, Decimal("1500.00"))

        with mock.patch("iic_booking.communication.in_app.notify_in_app") as in_app:
            out = self._run(req.transaction_number, apply=True)
        self.assertIn(f"REVERSED {req.transaction_number}", out)
        self.assertEqual(self._sub().balance, Decimal("500.00"))
        in_app.assert_called_once()
        self.assertEqual([u.pk for u in in_app.call_args.args[0]], [self.faculty.pk])
        self.assertEqual(in_app.call_args.kwargs["event"], "wallet.recharge_reversed")
        adj = WalletAdminAdjustment.objects.get(client_request_id=f"recharge-reversal-{req.pk}")
        self.assertEqual((adj.direction, adj.amount, adj.reason), ("debit", Decimal("1000.00"), "correction"))
        self.assertEqual((adj.balance_before, adj.balance_after), (Decimal("1500.00"), Decimal("500.00")))
        self.assertTrue(adj.reference.startswith("WAD-"))
        self.assertEqual(adj.external_reference, req.transaction_number)
        self.assertIn("rnd.iitr.ac.in", adj.remarks)
        self.assertIsNotNone(adj.email_sent_at)

        req.refresh_from_db()
        self.assertEqual(req.status, WalletRechargeRequestStatus.CANCELLED)
        self.assertEqual(req.cancellation_source, WalletRechargeCancellationSource.ADMIN)
        self.assertEqual(req.processed_by_id, self.admin.pk)
        self.assertEqual(req.response_message, rr.REVERSAL_REASON)
        entry = req.audit_logs.get(action=rr.AUDIT_ACTION)
        self.assertEqual(entry.metadata["adjustment_reference"], adj.reference)
        self.assertEqual(entry.metadata["previous"]["processed_by_id"], self.faculty.pk)
        self.assertTrue(req.audit_logs.filter(action=rr.NOTICE_ACTION).exists())

        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["fac.reverse@test.iitr.ac.in"])
        self.assertEqual(msg.cc, [])
        self.assertIn(req.transaction_number, msg.subject)
        self.assertIn("reversed", msg.subject)
        self.assertIn("Ledger > New Wallet Recharge", msg.body)
        self.assertIn("Direct Cash Deposit / Bank Transfer", msg.body)
        self.assertIn(adj.reference, msg.body)
        self.assertIn("₹500.00", msg.body)
        self.assertIn("https://rnd.iitr.ac.in", msg.alternatives[0][0])
        self.assertNotIn("other.fac@", " ".join(m.to[0] for m in mail.outbox))

    def test_apply_is_idempotent(self):
        req = self._self_approved()
        self._run(req.transaction_number, apply=True)
        out = self._run(req.transaction_number, apply=True)
        self.assertIn(f"ALREADY_REVERSED {req.transaction_number}", out)
        self.assertIn("already_sent=True", out)
        self.assertEqual(WalletAdminAdjustment.objects.count(), 1)
        self.assertEqual(self._sub().balance, Decimal("0.00"))
        self.assertEqual(len(mail.outbox), 1)

    def test_student_requester_and_owner_both_notified(self):
        req = self._self_approved("300.00", user=self.student)
        self.assertEqual(rr.assess(req)["approval_source"], "user")
        self._run(req.transaction_number, apply=True)
        self.assertEqual(sorted(m.to[0] for m in mail.outbox), ["fac.reverse@test.iitr.ac.in", "stu.reverse@test.iitr.ac.in"])

    def test_sric_link_approval_is_not_reversed(self):
        req = approve_request(self._request(), actor_email="sric-email-approval")
        out = self._run(req.transaction_number)
        self.assertIn("approval_source=sric_email_link", out)
        self.assertIn("BLOCKED", out)
        with self.assertRaises(CommandError):
            self._run(req.transaction_number, apply=True)
        self.assertEqual(self._sub().balance, Decimal("1000.00"))

    def test_sric_approver_email_is_not_reversed(self):
        req = approve_request(self._request(), actor_email="sric.desk@test.iitr.ac.in")
        self.assertEqual(rr.assess(req)["approval_source"], "sric_approver")
        self.assertFalse(rr.assess(req)["eligible"])

    def test_cashbook_or_fund_receipt_blocks(self):
        a = self._self_approved()
        WalletRechargeRequest.objects.filter(pk=a.pk).update(cashbook_receipt_no="R-1")
        a.refresh_from_db()
        self.assertIn("SRIC cash-book receipt is linked", rr.assess(a)["blockers"])
        WalletRechargeRequest.objects.filter(pk=a.pk).update(cashbook_receipt_no="", fund_receipt_verified=True)
        a.refresh_from_db()
        self.assertIn("fund receipt is verified", rr.assess(a)["blockers"])

    def test_spent_funds_block_all_named_requests(self):
        ok = self._self_approved("400.00")
        spent = self._self_approved("600.00")
        self._sub().debit(Decimal("100.00"), "Booking IICTEST-XRD-01202600001")
        with self.assertRaises(CommandError):
            self._run(ok.transaction_number, spent.transaction_number, apply=True)
        self.assertEqual(self._sub().balance, Decimal("900.00"))
        self.assertFalse(WalletAdminAdjustment.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_balance_cannot_go_negative(self):
        req = self._self_approved("1000.00")
        SubWallet.objects.filter(pk=self._sub().pk).update(balance=Decimal("200.00"))
        req.refresh_from_db()
        facts = rr.assess(req)
        self.assertFalse(facts["eligible"])
        self.assertTrue(any("balance" in b for b in facts["blockers"]))
