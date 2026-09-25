"""Cash-book (SRIC TXT) matching on wallet recharge requests: single credit, no receipt reuse."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase

from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.wallet import (
    SubWallet,
    WalletRechargeImportRecord,
    WalletRechargeParseEntry,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
)
from iic_booking.users.wallet_recharge_import import (
    CashbookIndex,
    CashbookMatchError,
    import_wallet_recharge_rows,
    link_cashbook_entry_to_request,
    match_pending_recharge_requests_to_parse_entries,
)

User = get_user_model()
GRANT = "IIC-000-002"


class CashbookMatchTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(
            name="IIC Cashbook Test", code="ICBT", department_type=DepartmentType.INTERNAL
        )
        self.fac_a = self._faculty("a", "E2001")
        self.fac_b = self._faculty("b", "E2002")

    def _faculty(self, tag: str, emp: str):
        return User.objects.create_user(
            email=f"fac.{tag}.cashbook@test.iitr.ac.in",
            password="pass12345",
            name=f"Faculty {tag.upper()}",
            user_type=UserType.FACULTY,
            department=self.dept,
            emp_id=emp,
        )

    def _request(self, user, amount="5000.00", status=WalletRechargeRequestStatus.PENDING):
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
        )

    def _entry(self, receipt="R-101", emp="E2001", amount="5,000.00", grant=GRANT, dated=date(2026, 9, 20)):
        return WalletRechargeParseEntry.objects.create(
            receipt_no=receipt, dated=dated, emp_no=emp, amount=amount, credited_to_project_no=grant, name="X"
        )

    def _balance(self, user) -> Decimal:
        sw = SubWallet.objects.filter(wallet__user=user, department=self.dept).first()
        return sw.balance if sw else Decimal("0")

    def test_link_pending_approves_and_credits_once(self):
        req = self._request(self.fac_a)
        entry = self._entry()
        updated, outcome = link_cashbook_entry_to_request(req.pk, entry.pk, actor_email="t@test")
        self.assertEqual(outcome, "approved")
        self.assertEqual(updated.status, WalletRechargeRequestStatus.APPROVED)
        self.assertTrue(updated.fund_receipt_verified)
        self.assertEqual(updated.cashbook_receipt_no, "R-101")
        self.assertEqual(self._balance(self.fac_a), Decimal("5000.00"))
        self.assertEqual(WalletRechargeImportRecord.objects.filter(receipt_no="R-101").count(), 1)

        with self.assertRaises(CashbookMatchError):
            link_cashbook_entry_to_request(req.pk, entry.pk)
        self.assertEqual(self._balance(self.fac_a), Decimal("5000.00"))

    def test_entry_cannot_be_used_for_second_request(self):
        req1 = self._request(self.fac_a)
        req2 = self._request(self.fac_a)
        entry = self._entry()
        link_cashbook_entry_to_request(req1.pk, entry.pk)
        with self.assertRaises(CashbookMatchError):
            link_cashbook_entry_to_request(req2.pk, entry.pk)
        req2.refresh_from_db()
        self.assertEqual(req2.status, WalletRechargeRequestStatus.PENDING)
        self.assertEqual(self._balance(self.fac_a), Decimal("5000.00"))

    def test_receipt_reuse_blocked_after_entries_cleared(self):
        req1 = self._request(self.fac_a)
        link_cashbook_entry_to_request(req1.pk, self._entry().pk)
        WalletRechargeParseEntry.objects.all().delete()
        again = self._entry()
        req2 = self._request(self.fac_a)
        with self.assertRaises(CashbookMatchError):
            link_cashbook_entry_to_request(req2.pk, again.pk)
        self.assertEqual(CashbookIndex().candidates_for(req2), [])

    def test_approved_request_is_verified_without_second_credit(self):
        req = self._request(self.fac_a, status=WalletRechargeRequestStatus.APPROVED)
        _, outcome = link_cashbook_entry_to_request(req.pk, self._entry().pk)
        self.assertEqual(outcome, "verified")
        self.assertEqual(self._balance(self.fac_a), Decimal("0"))

    def test_amount_or_grant_mismatch_rejected(self):
        req = self._request(self.fac_a)
        with self.assertRaises(CashbookMatchError):
            link_cashbook_entry_to_request(req.pk, self._entry(receipt="R-1", amount="4,000.00").pk)
        with self.assertRaises(CashbookMatchError):
            link_cashbook_entry_to_request(req.pk, self._entry(receipt="R-2", grant="SRIC-999").pk)
        req.refresh_from_db()
        self.assertEqual(req.status, WalletRechargeRequestStatus.PENDING)

    def test_manual_import_skips_when_request_exists_or_receipt_used(self):
        req = self._request(self.fac_a)
        row = {"receipt_no": "R-300", "amount": Decimal("5000.00"), "emp_no": "E2001", "dated": date(2026, 9, 21)}
        credited, _skipped, errors, _ = import_wallet_recharge_rows([row], default_department_id=self.dept.pk)
        self.assertEqual(credited, 0)
        self.assertTrue(any("WRR-" in e for e in errors))

        link_cashbook_entry_to_request(req.pk, self._entry(receipt="R-300", dated=date(2026, 9, 21)).pk)
        credited, _skipped, _errors, _ = import_wallet_recharge_rows([row], default_department_id=self.dept.pk)
        self.assertEqual(credited, 0)
        self.assertEqual(self._balance(self.fac_a), Decimal("5000.00"))

    def test_auto_match_requires_emp_and_skips_ambiguity(self):
        req_a = self._request(self.fac_a)
        req_b = self._request(self.fac_b)
        self._entry(receipt="R-401", emp="E2002")
        matched, errors = match_pending_recharge_requests_to_parse_entries()
        self.assertEqual((matched, errors), (1, []))
        req_a.refresh_from_db()
        req_b.refresh_from_db()
        self.assertEqual(req_a.status, WalletRechargeRequestStatus.PENDING)
        self.assertEqual(req_b.cashbook_receipt_no, "R-401")

        self._request(self.fac_a)
        self._entry(receipt="R-402", emp="E2001")
        matched, _ = match_pending_recharge_requests_to_parse_entries()
        self.assertEqual(matched, 0)

        self.assertEqual(match_pending_recharge_requests_to_parse_entries()[0], 0)
        self.assertEqual(self._balance(self.fac_b), Decimal("5000.00"))

    def test_admin_api_lists_candidates_and_links_once(self):
        from rest_framework.test import APIClient

        admin = User.objects.create_user(
            email="admin.cashbook@test.iitr.ac.in", password="pass12345", name="Admin", user_type=UserType.ADMIN
        )
        client = APIClient()
        client.force_authenticate(admin)
        req = self._request(self.fac_a)
        entry = self._entry()
        base = "/api/admin/wallet-recharge-requests/"

        res = client.get(base, {"cashbook": "received"})
        self.assertEqual(res.status_code, 200)
        rows = res.json()["results"]
        self.assertEqual([r["id"] for r in rows], [req.id])
        self.assertEqual(rows[0]["cashbook_candidates"][0]["id"], entry.id)

        res = client.post(f"{base}{req.id}/cashbook-link/", {"parse_entry_id": entry.id}, format="json")
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["outcome"], "approved")
        res = client.post(f"{base}{req.id}/cashbook-link/", {"parse_entry_id": entry.id}, format="json")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self._balance(self.fac_a), Decimal("5000.00"))

        matched = client.get(base, {"cashbook": "matched"}).json()["results"]
        self.assertEqual(matched[0]["cashbook_receipt_no"], "R-101")
