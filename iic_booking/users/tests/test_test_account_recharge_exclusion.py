"""Test-account wallet recharges: never matched to the SRIC cash-book, never overdue, not counted in revenue."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.wallet import (
    SubWallet,
    SubWalletTransaction,
    WalletRechargeParseEntry,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
)
from iic_booking.users.wallet_recharge_admin_actions import reminder_blocked_reason
from iic_booking.users.wallet_recharge_import import (
    CashbookIndex,
    CashbookMatchError,
    link_cashbook_entry_to_request,
    match_pending_recharge_requests_to_parse_entries,
)
from iic_booking.users.wallet_recharge_workflow import overdue_fund_receipt_requests

User = get_user_model()
GRANT = "IIC-000-002"


class TestAccountRechargeTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(
            name="IIC Test Exclusion", code="ITEX", department_type=DepartmentType.INTERNAL
        )
        self.real = self._faculty("real", "E3001")
        self.tester = self._faculty("tester", "TEST-0001", is_test_account=True)
        self.admin = User.objects.create_user(
            email="admin.texcl@test.iitr.ac.in", password="pass12345", name="Admin", user_type=UserType.ADMIN
        )

    def _faculty(self, tag, emp, **extra):
        return User.objects.create_user(
            email=f"fac.{tag}.texcl@test.iitr.ac.in",
            password="pass12345",
            name=f"Faculty {tag}",
            user_type=UserType.FACULTY,
            department=self.dept,
            emp_id=emp,
            **extra,
        )

    def _request(self, user, *, status=WalletRechargeRequestStatus.PENDING, amount="5000.00", responded=None):
        wallet, _ = Wallet.objects.get_or_create(user=user)
        return WalletRechargeRequest.objects.create(
            user=user,
            wallet=wallet,
            department=self.dept,
            amount=Decimal(amount),
            status=status,
            user_otp_verified=True,
            employee_number=user.emp_id,
            department_grant_code=GRANT,
            responded_at=responded,
        )

    def _entry(self, receipt, emp, amount="5,000.00"):
        return WalletRechargeParseEntry.objects.create(
            receipt_no=receipt, dated=date(2026, 10, 2), emp_no=emp, amount=amount, credited_to_project_no=GRANT, name="X"
        )

    def _client(self, user=None):
        client = APIClient()
        client.force_authenticate(user or self.admin)
        return client

    # --------------------------------------------------------------- SRIC follow-up list

    def test_test_requests_are_never_overdue(self):
        old = timezone.now() - timedelta(days=40)
        real = self._request(self.real, status=WalletRechargeRequestStatus.APPROVED, responded=old)
        self._request(self.tester, status=WalletRechargeRequestStatus.APPROVED, responded=old)
        _, rows = overdue_fund_receipt_requests(WalletRechargeRequest.objects.all())
        self.assertEqual([r.pk for r in rows], [real.pk])
        _, everything = overdue_fund_receipt_requests(WalletRechargeRequest.objects.all(), include_test=True)
        self.assertEqual(len(everything), 2)

        data = self._client().get("/api/admin/wallet-recharge-requests/overdue-fund-receipts/").json()
        self.assertEqual({r["id"] for r in data["results"]}, {real.pk})
        listed = self._client().get("/api/admin/wallet-recharge-requests/", {"overdue": "1"}).json()
        self.assertEqual({r["id"] for r in listed["results"]}, {real.pk})

    # --------------------------------------------------------------- SRIC cash-book matching

    def test_cashbook_never_offers_or_links_entries_to_test_requests(self):
        req = self._request(self.tester)
        entry = self._entry("R-T1", "TEST-0001")
        self.assertEqual(CashbookIndex().candidates_for(req), [])
        with self.assertRaises(CashbookMatchError):
            link_cashbook_entry_to_request(req.pk, entry.pk)
        req.refresh_from_db()
        self.assertEqual(req.status, WalletRechargeRequestStatus.PENDING)
        self.assertEqual(req.cashbook_receipt_no, "")

    def test_auto_match_skips_test_requests_and_still_matches_real_ones(self):
        test_req = self._request(self.tester, amount="7000.00")
        real_req = self._request(self.real)
        # A row with no Emp No. would otherwise pair with the test request on amount + grant alone.
        WalletRechargeParseEntry.objects.create(
            receipt_no="R-NOEMP", dated=date(2026, 10, 2), emp_no="", amount="7,000.00", credited_to_project_no=GRANT
        )
        self._entry("R-REAL", "E3001")
        matched, _errors = match_pending_recharge_requests_to_parse_entries()
        self.assertEqual(matched, 1)
        test_req.refresh_from_db()
        real_req.refresh_from_db()
        self.assertEqual(test_req.cashbook_receipt_no, "")
        self.assertEqual(test_req.status, WalletRechargeRequestStatus.PENDING)
        self.assertEqual(real_req.cashbook_receipt_no, "R-REAL")

    def test_transaction_reference_to_a_test_request_is_refused(self):
        test_req = self._request(self.tester)
        WalletRechargeParseEntry.objects.create(
            receipt_no="R-REF",
            dated=date(2026, 10, 2),
            emp_no="",
            amount="5,000.00",
            credited_to_project_no=GRANT,
            payment=f"Wallet recharge IIC-TXN-{test_req.pk:06d}",
        )
        matched, errors = match_pending_recharge_requests_to_parse_entries()
        self.assertEqual(matched, 0)
        self.assertTrue(any("test account" in e for e in errors))

    def test_received_and_awaiting_filters_leave_out_test_requests(self):
        test_req = self._request(self.tester)
        real_req = self._request(self.real)
        awaiting = self._client().get("/api/admin/wallet-recharge-requests/", {"cashbook": "awaiting"}).json()
        self.assertEqual({r["id"] for r in awaiting["results"]}, {real_req.pk})
        self.assertNotIn(test_req.pk, {r["id"] for r in awaiting["results"]})

    def test_funds_not_received_reminder_is_blocked_for_approved_test_requests(self):
        approved = self._request(self.tester, status=WalletRechargeRequestStatus.APPROVED, responded=timezone.now())
        self.assertIn("test account", reminder_blocked_reason(approved))
        self.assertEqual(reminder_blocked_reason(self._request(self.tester)), "")
        real = self._request(self.real, status=WalletRechargeRequestStatus.APPROVED, responded=timezone.now())
        self.assertEqual(reminder_blocked_reason(real), "")

    # --------------------------------------------------------------- list: badge and filter

    def test_list_marks_and_filters_test_requests(self):
        test_req = self._request(self.tester)
        real_req = self._request(self.real)
        rows = self._client().get("/api/admin/wallet-recharge-requests/").json()["results"]
        flags = {r["id"]: r["is_test_account"] for r in rows}
        self.assertEqual(flags, {test_req.pk: True, real_req.pk: False})
        hidden = self._client().get("/api/admin/wallet-recharge-requests/", {"test": "hide"}).json()["results"]
        self.assertEqual({r["id"] for r in hidden}, {real_req.pk})
        only = self._client().get("/api/admin/wallet-recharge-requests/", {"test": "only"}).json()["results"]
        self.assertEqual({r["id"] for r in only}, {test_req.pk})

    # --------------------------------------------------------------- finance report

    def test_finance_report_wallet_recharges_leave_out_test_accounts(self):
        from iic_booking.users.finance_reports import build_finance_report

        now = timezone.now()
        self._request(self.real, status=WalletRechargeRequestStatus.APPROVED, responded=now)
        self._request(self.tester, status=WalletRechargeRequestStatus.APPROVED, responded=now, amount="9000.00")
        finance = User.objects.create_user(
            email="acc.texcl@test.iitr.ac.in",
            password="pass12345",
            name="Accounts",
            user_type=UserType.FINANCE,
            department=self.dept,
        )
        today = timezone.localdate()
        rep = build_finance_report(user=finance, date_from=today, date_to=today)
        self.assertEqual(rep["summary"]["wallet_recharges"], 5000.0)
        self.assertEqual(rep["tables"]["payment_analytics"]["wallet_recharge_approved"], 5000.0)

    # --------------------------------------------------------------- SRIC recharges tab

    def test_sric_rows_credited_to_a_test_account_are_test_rows(self):
        from iic_booking.users.models.sric_wallet_recharge import SricWalletRecharge, SricWalletRechargeStatus

        def row(ledger, amount, user=None, **extra):
            return SricWalletRecharge.objects.create(
                ledger_id=ledger,
                amount=Decimal(amount),
                financial_year="2026-27",
                status=SricWalletRechargeStatus.CREDITED,
                matched_user=user,
                **extra,
            )

        real = row("LED-REAL", "1000.00", self.real)
        unmatched = row("LED-NONE", "50.00")
        to_tester = row("LED-TEST", "7000.00", self.tester)
        test_run = row("LED-RUN", "300.00", is_test=True)
        data = self._client().get("/api/admin/sric-wallet-recharges/").json()
        flags = {r["id"]: r["is_test"] for r in data["results"]}
        self.assertEqual(flags, {real.pk: False, unmatched.pk: False, to_tester.pk: True, test_run.pk: True})
        self.assertEqual(data["credited_total"], "1050.00")
        self.assertEqual(data["test_count"], 2)
        hidden = self._client().get("/api/admin/sric-wallet-recharges/", {"test": "hide"}).json()
        self.assertEqual({r["id"] for r in hidden["results"]}, {real.pk, unmatched.pk})

    # --------------------------------------------------------------- wallet ledger

    def test_wallet_ledger_marks_and_filters_test_accounts(self):
        from iic_booking.users import admin_wallet_ledger as ledger

        for user in (self.real, self.tester):
            wallet, _ = Wallet.objects.get_or_create(user=user)
            sw, _ = SubWallet.objects.get_or_create(wallet=wallet, department=self.dept)
            SubWalletTransaction.objects.create(sub_wallet=sw, transaction_type="credit", amount=Decimal("100.00"))
        ours = {self.real.pk, self.tester.pk}
        owners = [o for o in ledger.list_owners({})["results"] if o["owner_id"] in ours]
        self.assertEqual({o["owner_id"]: o["is_test_account"] for o in owners}, {self.real.pk: False, self.tester.pk: True})
        hidden = {o["owner_id"] for o in ledger.list_owners({"test": "hide"})["results"]}
        self.assertIn(self.real.pk, hidden)
        self.assertNotIn(self.tester.pk, hidden)
        txns = ledger.list_transactions({"test": "only"})
        self.assertEqual([t["owner_id"] for t in txns["results"]], [self.tester.pk])
        self.assertTrue(txns["results"][0]["is_test_account"])
        self.assertEqual(ledger.list_transactions({"test": "hide"})["summary"]["total_credits"], "100.00")

    # --------------------------------------------------------------- Main Admin sets the flag

    def test_main_admin_can_mark_and_unmark_test_accounts(self):
        url = f"/api/admin/users/{self.real.pk}/set-test-account/"
        resp = self._client().post(url, {"is_test_account": True}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertTrue(resp.json()["changed"])
        self.assertTrue(resp.json()["user"]["is_test_account"])
        self.real.refresh_from_db()
        self.assertTrue(self.real.is_test_account)
        resp = self._client().post(url, {"is_test_account": False}, format="json")
        self.real.refresh_from_db()
        self.assertFalse(self.real.is_test_account)

        listed = self._client().get("/api/admin/users/", {"is_test_account": "true"}).json()
        rows = listed.get("results", listed) if isinstance(listed, dict) else listed
        self.assertEqual({r["id"] for r in rows}, {self.tester.pk})

    def test_only_main_admin_can_mark_and_main_admin_cannot_be_marked(self):
        dept_admin = User.objects.create_user(
            email="dadmin.texcl@test.iitr.ac.in",
            password="pass12345",
            name="Dept Admin",
            user_type=UserType.DEPT_ADMIN,
            department=self.dept,
        )
        resp = self._client(dept_admin).post(
            f"/api/admin/users/{self.real.pk}/set-test-account/", {"is_test_account": True}, format="json"
        )
        self.assertIn(resp.status_code, (403, 404))
        self.real.refresh_from_db()
        self.assertFalse(self.real.is_test_account)

        resp = self._client().post(
            f"/api/admin/users/{self.admin.pk}/set-test-account/", {"is_test_account": True}, format="json"
        )
        self.assertEqual(resp.status_code, 400)

    # --------------------------------------------------------------- management command

    def test_flag_command_is_a_dry_run_unless_confirmed(self):
        out = StringIO()
        call_command("flag_test_accounts", "--user-ids", str(self.real.pk), stdout=out)
        self.assertIn("would_set=True", out.getvalue())
        self.real.refresh_from_db()
        self.assertFalse(self.real.is_test_account)

        with self.assertRaises(CommandError):
            call_command("flag_test_accounts", "--user-ids", str(self.real.pk), "--confirm", "yes", stdout=StringIO())

        out = StringIO()
        call_command(
            "flag_test_accounts", "--user-ids", str(self.real.pk), "--confirm", "FLAG_TEST_ACCOUNTS", stdout=out
        )
        self.assertIn("changed=1", out.getvalue())
        self.real.refresh_from_db()
        self.assertTrue(self.real.is_test_account)

        out = StringIO()
        call_command("flag_test_accounts", "--user-ids", str(self.admin.pk), "--confirm", "FLAG_TEST_ACCOUNTS", stdout=out)
        self.assertIn("refused=main_administrator", out.getvalue())

    def test_flag_command_report_lists_ids_not_emails(self):
        old = timezone.now() - timedelta(days=40)
        self._request(self.tester, status=WalletRechargeRequestStatus.APPROVED, responded=old)
        lookalike = self._faculty("lookalike", "E3009")
        lookalike.name = "Test Faculty Two"
        lookalike.save(update_fields=["name"])
        out = StringIO()
        call_command("flag_test_accounts", stdout=out)
        text = out.getvalue()
        self.assertIn("flagged_test_accounts=1", text)
        self.assertIn(f"user_id={self.tester.pk}", text)
        self.assertIn(f"user_id={lookalike.pk}", text)
        self.assertIn("name_has_test", text)
        self.assertIn("hidden_as_test=1", text)
        self.assertNotIn("@", text)
