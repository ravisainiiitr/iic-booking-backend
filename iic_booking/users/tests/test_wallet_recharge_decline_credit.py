"""SRIC decline -> auto-approved credit, recovery on the next approval, AR/Dean SRIC copies,
transaction-number cash-book matching and the scheduled cash-book mailbox reader."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
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
from iic_booking.users.models.wallet_sric_settings import WalletCashbookMailboxMessage, WalletSricSettings
from iic_booking.users.wallet_recharge_import import match_pending_recharge_requests_to_parse_entries
from iic_booking.users.wallet_recharge_workflow import (
    RechargeAlreadyProcessed,
    approve_request,
    get_recharge_cc_emails,
    notify_stakeholders_of_decision,
    reject_request,
    serialize_request_public,
    verify_fund_receipt,
)

User = get_user_model()
GRANT = "IIC-000-002"


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class DeclineToCreditTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(
            name="IIC Decline Test", code="IDCT", department_type=DepartmentType.INTERNAL
        )
        self.faculty = User.objects.create_user(
            email="fac.decline@test.iitr.ac.in",
            password="pass12345",
            name="Decline Faculty",
            user_type=UserType.FACULTY,
            department=self.dept,
            emp_id="100584",
        )
        self.wallet, _ = Wallet.objects.get_or_create(user=self.faculty)
        s = WalletSricSettings.get_singleton()
        s.recipient_emails = "sric.desk@test.iitr.ac.in"
        s.ar_sric_emails = "ar.sric@test.iitr.ac.in"
        s.dean_sric_emails = "dean.sric@test.iitr.ac.in"
        s.save()

    def _request(self, amount="1000.00", mode=WalletRechargeMode.PROJECT_GRANT):
        return WalletRechargeRequest.objects.create(
            user=self.faculty,
            wallet=self.wallet,
            department=self.dept,
            amount=Decimal(amount),
            user_otp_verified=True,
            recharge_mode=mode,
            employee_number="100584",
            department_grant_code=GRANT,
            project_grant_code="PRJ-42" if mode == WalletRechargeMode.PROJECT_GRANT else "",
        )

    def _balance(self) -> Decimal:
        sw = SubWallet.objects.filter(wallet=self.wallet, department=self.dept).first()
        return sw.balance if sw else Decimal("0")

    def test_pending_decline_cancels_and_credits_wallet_as_credit(self):
        req = self._request()
        declined = reject_request(
            req, reason_code=WalletRechargeRejectionReason.WRONG_PROJECT_GRANT, actor_email="sric@test"
        )
        self.assertEqual(declined.status, WalletRechargeRequestStatus.CANCELLED)
        self.assertEqual(declined.cancellation_source, WalletRechargeCancellationSource.SRIC_DECLINED)
        self.assertEqual(declined.decline_credit_amount, Decimal("1000.00"))
        self.assertEqual(declined.decline_credit_outstanding, Decimal("1000.00"))
        self.assertEqual(self._balance(), Decimal("1000.00"))
        self.assertTrue(declined.audit_logs.filter(action="declined_to_credit").exists())

        notify_stakeholders_of_decision(declined)
        faculty_mail = next(m for m in mail.outbox if "fac.decline@test.iitr.ac.in" in m.to)
        self.assertIn("Declined by SRIC", faculty_mail.subject)
        self.assertIn("Wrong Project Code", faculty_mail.body)
        self.assertIn("auto-approved credit", faculty_mail.body)
        cc_note = next(m for m in mail.outbox if "dean.sric@test.iitr.ac.in" in m.to)
        self.assertIn("ar.sric@test.iitr.ac.in", cc_note.to)

    def test_decline_after_approval_keeps_balance_and_records_credit(self):
        req = approve_request(self._request(), actor_email="sric@test")
        self.assertEqual(self._balance(), Decimal("1000.00"))
        self.assertTrue(serialize_request_public(req)["can_decline"])
        declined = reject_request(
            req, reason_code=WalletRechargeRejectionReason.INSUFFICIENT_BALANCE, actor_email="sric@test"
        )
        self.assertEqual(declined.status, WalletRechargeRequestStatus.CANCELLED)
        self.assertEqual(declined.decline_credit_outstanding, Decimal("1000.00"))
        self.assertEqual(self._balance(), Decimal("1000.00"))

    def test_decline_blocked_once_funds_confirmed(self):
        req = approve_request(self._request(), actor_email="sric@test")
        WalletRechargeRequest.objects.filter(pk=req.pk).update(fund_receipt_verified=True)
        req.refresh_from_db()
        self.assertFalse(serialize_request_public(req)["can_decline"])
        with self.assertRaises(RechargeAlreadyProcessed):
            reject_request(req, reason_code=WalletRechargeRejectionReason.INSUFFICIENT_BALANCE)
        self.assertEqual(self._balance(), Decimal("1000.00"))

    def _approve_and_receive_funds(self, amount):
        approved = approve_request(self._request(amount), actor_email="sric@test")
        self.assertTrue(approved.wallet_credit_pending)
        with self.captureOnCommitCallbacks(execute=True):
            return verify_fund_receipt(approved, actor=None, remarks="SRIC cash-book")

    def test_next_approval_recovers_credit_fully_then_adds_rest(self):
        reject_request(self._request("1000.00"), reason_code=WalletRechargeRejectionReason.WRONG_PROJECT_GRANT)
        self.assertEqual(self._balance(), Decimal("1000.00"))
        credited = self._approve_and_receive_funds("1500.00")
        self.assertEqual(credited.credit_settled_amount, Decimal("1000.00"))
        self.assertEqual(self._balance(), Decimal("1500.00"))
        credit = WalletRechargeRequest.objects.get(cancellation_source=WalletRechargeCancellationSource.SRIC_DECLINED)
        self.assertEqual(credit.decline_credit_outstanding, Decimal("0.00"))
        self.assertIsNotNone(credit.decline_credit_settled_at)
        self.assertTrue(any("adjusted against credit" in m.subject for m in mail.outbox))

    def test_partial_recovery_leaves_outstanding(self):
        reject_request(self._request("1000.00"), reason_code=WalletRechargeRejectionReason.WRONG_PROJECT_GRANT)
        credited = self._approve_and_receive_funds("600.00")
        self.assertEqual(credited.credit_settled_amount, Decimal("600.00"))
        self.assertEqual(self._balance(), Decimal("1000.00"))
        credit = WalletRechargeRequest.objects.get(cancellation_source=WalletRechargeCancellationSource.SRIC_DECLINED)
        self.assertEqual(credit.decline_credit_outstanding, Decimal("400.00"))
        self.assertIsNone(credit.decline_credit_settled_at)

    def test_recovery_after_credit_was_spent(self):
        reject_request(self._request("1000.00"), reason_code=WalletRechargeRejectionReason.WRONG_PROJECT_GRANT)
        SubWallet.objects.get(wallet=self.wallet, department=self.dept).debit(Decimal("1000.00"), "booking")
        self._approve_and_receive_funds("1000.00")
        self.assertEqual(self._balance(), Decimal("0.00"))

    def test_cash_deposit_decline_is_plain_rejection(self):
        req = self._request(mode=WalletRechargeMode.DIRECT_CASH_DEPOSIT)
        self.assertEqual(
            [c["value"] for c in serialize_request_public(req)["rejection_reason_choices"]],
            ["mismatch_user_info", "other"],
        )
        declined = reject_request(req, reason_code=WalletRechargeRejectionReason.MISMATCH_USER_INFO)
        self.assertEqual(declined.status, WalletRechargeRequestStatus.REJECTED)
        self.assertEqual(declined.decline_credit_outstanding, Decimal("0.00"))
        self.assertEqual(self._balance(), Decimal("0"))

    def test_switch_off_restores_plain_rejection(self):
        s = WalletSricSettings.get_singleton()
        s.decline_converts_to_credit = False
        s.save()
        declined = reject_request(self._request(), reason_code=WalletRechargeRejectionReason.WRONG_PROJECT_GRANT)
        self.assertEqual(declined.status, WalletRechargeRequestStatus.REJECTED)
        self.assertEqual(self._balance(), Decimal("0"))

    def test_other_requires_text(self):
        with self.assertRaises(ValueError):
            reject_request(self._request(), reason_code=WalletRechargeRejectionReason.OTHER)
        declined = reject_request(
            self._request(), reason_code=WalletRechargeRejectionReason.OTHER, reason_text="Project closed"
        )
        self.assertEqual(declined.response_message, "Project closed")

    def test_project_grant_reason_choices(self):
        choices = serialize_request_public(self._request())["rejection_reason_choices"]
        self.assertEqual(
            [c["label"] for c in choices],
            ["Wrong Project Code", "Insufficient Funds in the Project", "Project Already Closed", "Other"],
        )

    def test_ar_and_dean_copies(self):
        self.assertEqual(
            get_recharge_cc_emails(WalletRechargeMode.PROJECT_GRANT),
            ["ar.sric@test.iitr.ac.in", "dean.sric@test.iitr.ac.in"],
        )
        self.assertEqual(get_recharge_cc_emails(WalletRechargeMode.DIRECT_CASH_DEPOSIT), ["ar.sric@test.iitr.ac.in"])

    def test_token_decline_endpoint_after_approval(self):
        req = approve_request(self._request(), actor_email="sric@test")
        client = APIClient()
        detail = client.get(f"/api/wallet/recharge-action/{req.action_token}/")
        self.assertTrue(detail.json()["can_decline"])
        resp = client.post(
            f"/api/wallet/recharge-action/{req.action_token}/reject/",
            {"reason_code": "insufficient_balance"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], WalletRechargeRequestStatus.CANCELLED)
        again = client.post(
            f"/api/wallet/recharge-action/{req.action_token}/reject/",
            {"reason_code": "insufficient_balance"},
            format="json",
        )
        self.assertTrue(again.json().get("already_processed"))
        self.assertEqual(self._balance(), Decimal("1000.00"))

    def test_admin_search_by_transaction_number(self):
        req = self._request()
        other = self._request()
        admin = User.objects.create_user(
            email="admin.decline@test.iitr.ac.in", password="pass12345", name="Admin", user_type=UserType.ADMIN
        )
        client = APIClient()
        client.force_authenticate(admin)
        resp = client.get("/api/admin/wallet-recharge-requests/", {"search": req.transaction_number})
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        rows = data.get("results", data) if isinstance(data, dict) else data
        ids = [r["id"] for r in rows]
        self.assertEqual(ids, [req.id])
        self.assertNotIn(other.id, ids)


class TransactionReferenceMatchTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(
            name="IIC Txn Ref Test", code="ITRT", department_type=DepartmentType.INTERNAL
        )
        self.faculty = User.objects.create_user(
            email="fac.txnref@test.iitr.ac.in",
            password="pass12345",
            name="Txn Faculty",
            user_type=UserType.FACULTY,
            department=self.dept,
            emp_id="100777",
        )
        self.wallet, _ = Wallet.objects.get_or_create(user=self.faculty)

    def _request(self):
        return WalletRechargeRequest.objects.create(
            user=self.faculty,
            wallet=self.wallet,
            department=self.dept,
            amount=Decimal("2500.00"),
            user_otp_verified=True,
            employee_number="100777",
            department_grant_code=GRANT,
        )

    def test_payment_details_reference_picks_the_exact_request(self):
        first = self._request()
        second = self._request()
        WalletRechargeParseEntry.objects.create(
            receipt_no="R-900",
            dated=date(2026, 9, 25),
            emp_no="100777",
            amount="2,500.00",
            credited_to_project_no=GRANT,
            payment=f"Transfer against {second.transaction_number}",
            name="X",
        )
        matched, errors = match_pending_recharge_requests_to_parse_entries()
        self.assertEqual((matched, errors), (1, []))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.status, WalletRechargeRequestStatus.PENDING)
        self.assertEqual(second.status, WalletRechargeRequestStatus.APPROVED)
        self.assertTrue(second.fund_receipt_verified)
        self.assertEqual(second.cashbook_receipt_no, "R-900")

    def test_reference_with_wrong_amount_is_not_applied_elsewhere(self):
        req = self._request()
        WalletRechargeParseEntry.objects.create(
            receipt_no="R-901",
            dated=date(2026, 9, 25),
            emp_no="100777",
            amount="2,500.00",
            credited_to_project_no=GRANT,
            payment="Ref IIC-TXN-999999",
            name="X",
        )
        matched, errors = match_pending_recharge_requests_to_parse_entries()
        self.assertEqual(matched, 0)
        self.assertTrue(errors)
        req.refresh_from_db()
        self.assertEqual(req.status, WalletRechargeRequestStatus.PENDING)


CASHBOOK_TXT = (
    "|Sl No|Dated|Receipt No|Credited to Project No.|Amount(Rs)|Payment Details|Received From|\n"
    "|1 |Sep 25, 2026|R-950|IIC-000-002|3,000.00|NEFT IIC-TXN-{pk:06d}|PROF-TEST EMP NO-100888 DEPT-OF PHYSICS|\n"
)


@override_settings(IMAP_USER="reader@test.iitr.ac.in", IMAP_PASSWORD="x", IMAP_HOST="imap.test", IMAP_MAILBOX="INBOX")
class CashbookMailboxReaderTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(
            name="IIC Mailbox Test", code="IMBT", department_type=DepartmentType.INTERNAL
        )
        self.faculty = User.objects.create_user(
            email="fac.mailbox@test.iitr.ac.in",
            password="pass12345",
            name="Mailbox Faculty",
            user_type=UserType.FACULTY,
            department=self.dept,
            emp_id="100888",
        )
        wallet, _ = Wallet.objects.get_or_create(user=self.faculty)
        self.req = WalletRechargeRequest.objects.create(
            user=self.faculty,
            wallet=wallet,
            department=self.dept,
            amount=Decimal("3000.00"),
            user_otp_verified=True,
            employee_number="100888",
            department_grant_code=GRANT,
        )

    def _run(self, emails, content=""):
        from iic_booking.users import wallet_cashbook_mailbox as reader

        with mock.patch.object(reader, "list_emails", return_value=(emails, None)), mock.patch.object(
            reader, "fetch_email_attachment", return_value=(content, "IIC Wallet.txt", None)
        ) as fetch:
            result = reader.read_cashbook_mailbox()
        return result, fetch

    def test_first_run_is_baseline_then_new_mail_is_applied(self):
        old = [{"uid": "10", "subject": "Old cash-book", "from_addr": "bills@sric.iitr.ac.in"}]
        result, fetch = self._run(old)
        self.assertEqual(result["status"], "baseline")
        fetch.assert_not_called()
        self.assertTrue(WalletCashbookMailboxMessage.objects.filter(uid="10").exists())

        new = old + [{"uid": "11", "subject": "Cash-book 25 Sep", "from_addr": "bills@sric.iitr.ac.in"}]
        result, fetch = self._run(new, CASHBOOK_TXT.format(pk=self.req.pk))
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual((result["status"], result["messages_read"], result["requests_matched"]), ("ok", 1, 1))
        self.req.refresh_from_db()
        self.assertEqual(self.req.status, WalletRechargeRequestStatus.APPROVED)
        self.assertTrue(self.req.fund_receipt_verified)

        result, fetch = self._run(new, CASHBOOK_TXT.format(pk=self.req.pk))
        fetch.assert_not_called()
        self.assertEqual(result["messages_read"], 0)

    def test_disabled_and_not_configured(self):
        s = WalletSricSettings.get_singleton()
        s.auto_read_cashbook_mailbox = False
        s.save()
        self.assertEqual(self._run([])[0]["status"], "disabled")
        s.auto_read_cashbook_mailbox = True
        s.save()
        with override_settings(IMAP_PASSWORD=""):
            self.assertEqual(self._run([])[0]["status"], "not_configured")
