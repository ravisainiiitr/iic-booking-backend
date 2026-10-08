"""Wallet ledger (Main Administrator): owners, transactions, manual credit / debit, exports."""

from __future__ import annotations

import csv
import io
from datetime import timedelta
from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.wallet import SubWallet, SubWalletTransaction, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.models.wallet_admin_adjustment import WalletAdminAdjustment

User = get_user_model()

OWNERS = "/api/admin/wallet-ledger/owners/"
OPTIONS = "/api/admin/wallet-ledger/options/"
TXNS = "/api/admin/wallet-ledger/transactions/"
PREVIEW = "/api/admin/wallet-ledger/adjustments/preview/"
ADJUST = "/api/admin/wallet-ledger/adjustments/"


def detail(owner_id):
    return f"/api/admin/wallet-ledger/owners/{owner_id}/"


class LedgerBase(TestCase):
    def setUp(self):
        self.chem = Department.objects.create(name="Chemistry WL", code="CHWL", department_type=DepartmentType.INTERNAL)
        self.phys = Department.objects.create(name="Physics WL", code="PHWL", department_type=DepartmentType.INTERNAL)
        self.admin = User.objects.create_user(
            email="admin.wl@test.iitr.ac.in", password="x12345678", name="Admin WL", user_type=UserType.ADMIN
        )
        self.fac = User.objects.create_user(
            email="alpha.wl@test.iitr.ac.in", password="x12345678", name="Alpha Faculty", user_type=UserType.FACULTY,
            department=self.chem, emp_id="E100",
        )
        self.fac2 = User.objects.create_user(
            email="beta.wl@test.iitr.ac.in", password="x12345678", name="Beta Faculty", user_type=UserType.FACULTY,
            department=self.phys, emp_id="E200",
        )
        self.ext = User.objects.create_user(
            email="gamma.wl@test.example.com", password="x12345678", name="Gamma Industry", user_type=UserType.INSTITUTE,
        )
        self.student = User.objects.create_user(
            email="stud.wl@test.iitr.ac.in", password="x12345678", name="Stud WL", user_type=UserType.STUDENT,
            department=self.chem,
        )
        self.w1 = Wallet.objects.create(user=self.fac)
        self.w2 = Wallet.objects.create(user=self.fac2)
        self.w3 = Wallet.objects.create(user=self.ext)
        self.sw1 = SubWallet.objects.create(wallet=self.w1, department=self.chem, balance=Decimal("0.00"))
        self.sw1b = SubWallet.objects.create(wallet=self.w1, department=self.phys, balance=Decimal("0.00"))
        self.sw2 = SubWallet.objects.create(wallet=self.w2, department=self.phys, balance=Decimal("0.00"))
        self.sw3 = SubWallet.objects.create(wallet=self.w3, department=self.chem, balance=Decimal("0.00"))
        self.sw1.credit(Decimal("1000.00"), "Wallet recharge approved — WRR-1")
        self.sw1.debit(Decimal("300.00"), "Booking #XRD - X-Ray Diffractometer (60 minutes) | Ref: IIC-XRD-0001", related_user=self.student)
        self.sw1.credit(Decimal("100.00"), "Refund for cancelled Booking IIC-XRD-0001- X-Ray Diffractometer | Ref: IIC-XRD-0001")
        self.sw1b.credit(Decimal("50.00"), "IIC wallet recharge – Receipt No. 77")
        self.sw2.credit(Decimal("20.00"), "Admin credit")
        SubWallet.objects.filter(pk=self.sw3.pk).update(balance=Decimal("-40.00"))
        WalletJoinRequest.objects.create(
            student=self.student, faculty=self.fac, wallet=self.w1, status=WalletJoinRequestStatus.APPROVED
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def as_user(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def adjust(self, **overrides):
        payload = {
            "client_request_id": "req-0000001",
            "owner_id": self.fac.pk,
            "sub_wallet_id": self.sw1.pk,
            "direction": "credit",
            "amount": "250.50",
            "reason": "correction",
            "remarks": "Fix double charge",
            "external_reference": "UTR123",
            "notify_owner": False,
        }
        payload.update(overrides)
        return self.client.post(ADJUST, payload, format="json")


class PermissionTests(LedgerBase):
    def test_only_main_admin(self):
        dept_admin = User.objects.create_user(
            email="da.wl@test.iitr.ac.in", password="x12345678", name="DA", user_type=UserType.DEPT_ADMIN, department=self.chem
        )
        finance = User.objects.create_user(
            email="fin.wl@test.iitr.ac.in", password="x12345678", name="Fin", user_type=UserType.FINANCE
        )
        oic = User.objects.create_user(
            email="oic.wl@test.iitr.ac.in", password="x12345678", name="OIC", user_type=UserType.MANAGER, department=self.chem
        )
        for user in (self.fac, dept_admin, finance, oic):
            c = self.as_user(user)
            for url in (OWNERS, OPTIONS, TXNS, detail(self.fac.pk)):
                self.assertEqual(c.get(url).status_code, 403, (user.user_type, url))
            self.assertEqual(c.post(PREVIEW, {}, format="json").status_code, 403)
            self.assertEqual(c.post(ADJUST, {}, format="json").status_code, 403)
        self.assertIn(APIClient().get(OWNERS).status_code, (401, 403))
        self.assertEqual(self.client.get(OWNERS).status_code, 200)
        self.assertEqual(SubWalletTransaction.objects.filter(admin_adjustment__isnull=False).count(), 0)


class OwnerListTests(LedgerBase):
    def names(self, **params):
        res = self.client.get(OWNERS, params)
        self.assertEqual(res.status_code, 200)
        names = dict(User.objects.values_list("pk", "name"))
        return [names[r["owner_id"]] for r in res.data["results"]]

    def test_list_details_and_summary(self):
        res = self.client.get(OWNERS, {"with_options": 1})
        data = res.data
        self.assertEqual(data["count"], 3)
        self.assertEqual(data["summary"]["total_balance"], "830.00")
        self.assertEqual(data["summary"]["negative_owners"], 1)
        alpha = next(r for r in data["results"] if r["owner_id"] == self.fac.pk)
        self.assertEqual(alpha["s_no"], 1)
        self.assertEqual(alpha["total_balance"], "850.00")
        self.assertEqual(alpha["linked_students"], 1)
        self.assertEqual(alpha["department_name"], "Chemistry WL")
        self.assertEqual({s["department_name"] for s in alpha["sub_wallets"]}, {"Chemistry WL", "Physics WL"})
        self.assertIsNotNone(alpha["last_transaction_at"])
        self.assertIn("owner_types", data["options"])
        self.assertTrue(any(c["value"] == "manual_admin" for c in data["options"]["categories"]))

    def test_filters(self):
        self.assertEqual(self.names(search="alp"), ["Alpha Faculty"])
        self.assertEqual(self.names(search="E200"), ["Beta Faculty"])
        self.assertEqual(self.names(department=self.phys.pk), ["Beta Faculty"])
        self.assertEqual(self.names(owner_type=UserType.INSTITUTE), ["Gamma Industry"])
        self.assertEqual(sorted(self.names(sub_wallet_department=self.phys.pk)), ["Alpha Faculty", "Beta Faculty"])
        self.assertEqual(self.names(balance_state="negative"), ["Gamma Industry"])
        self.assertEqual(self.names(balance_state="zero_or_negative"), ["Gamma Industry"])
        self.assertEqual(self.names(balance_min="10", balance_max="100"), ["Beta Faculty"])
        today = timezone.localdate().isoformat()
        self.assertEqual(sorted(self.names(activity_from=today, activity_to=today)), ["Alpha Faculty", "Beta Faculty"])
        future = (timezone.localdate() + timedelta(days=3)).isoformat()
        self.assertEqual(self.names(activity_from=future), [])
        self.assertEqual(self.names(activity="none"), ["Gamma Industry"])

    def test_sorting_and_paging(self):
        self.assertEqual(self.names(ordering="-balance"), ["Alpha Faculty", "Beta Faculty", "Gamma Industry"])
        self.assertEqual(self.names(ordering="balance"), ["Gamma Industry", "Beta Faculty", "Alpha Faculty"])
        res = self.client.get(OWNERS, {"page": 2, "page_size": 2})
        self.assertEqual([r["s_no"] for r in res.data["results"]], [3])

    def test_owner_detail(self):
        data = self.client.get(detail(self.fac.pk)).data
        self.assertEqual(data["email"], self.fac.email)
        self.assertEqual(data["total_credits"], "1150.00")
        self.assertEqual(data["total_debits"], "300.00")
        self.assertEqual([s["name"] for s in data["students"]], ["Stud WL"])
        chem = next(s for s in data["sub_wallets"] if s["id"] == self.sw1.pk)
        self.assertEqual(chem["transaction_count"], 3)
        self.assertNotIn(str(self.chem.pk), [d["value"] for d in data["credit_departments"]])
        self.assertEqual(self.client.get(detail(self.student.pk)).status_code, 404)


class TransactionListTests(LedgerBase):
    def rows(self, **params):
        res = self.client.get(TXNS, params)
        self.assertEqual(res.status_code, 200)
        return res.data

    def test_owner_transactions_with_sources_and_balance(self):
        data = self.rows(owner=self.fac.pk, sub_wallet=self.sw1.pk)
        self.assertEqual(data["count"], 3)
        newest, booking, recharge = data["results"]
        self.assertEqual(newest["category"], "refund")
        self.assertEqual(newest["balance_after"], "800.00")
        self.assertEqual(newest["booking_code"], "IIC-XRD-0001")
        self.assertEqual(booking["category"], "booking_charge")
        self.assertEqual(booking["performer"], "user")
        self.assertEqual(booking["performed_by"], "Stud WL")
        self.assertEqual(booking["balance_after"], "700.00")
        self.assertEqual(recharge["category"], "recharge")
        self.assertEqual(recharge["balance_after"], "1000.00")
        self.assertEqual(data["summary"]["total_credits"], "1100.00")
        self.assertEqual(data["summary"]["net"], "800.00")

    def test_filters(self):
        self.assertEqual(self.rows(type="debit")["count"], 1)
        self.assertEqual(self.rows(category="recharge")["count"], 2)
        self.assertEqual(self.rows(category="manual_admin")["count"], 1)
        self.assertEqual(self.rows(performer="admin")["count"], 1)
        self.assertEqual(self.rows(amount_min="60", amount_max="300")["count"], 2)
        self.assertEqual(self.rows(booking="IIC-XRD-0001")["count"], 2)
        self.assertEqual(self.rows(owner_department=self.phys.pk)["count"], 1)
        self.assertEqual(self.rows(sub_wallet_department=self.phys.pk)["count"], 2)
        self.assertEqual(self.rows(search="Beta")["count"], 1)
        txn = SubWalletTransaction.objects.filter(sub_wallet=self.sw1b).first()
        self.assertEqual(self.rows(search=f"TXN-{txn.pk}")["results"][0]["id"], txn.pk)
        today = timezone.localdate()
        self.assertEqual(self.rows(date_from=today.isoformat(), date_to=today.isoformat())["count"], 5)
        self.assertEqual(self.rows(date_to=(today - timedelta(days=1)).isoformat())["count"], 0)
        page = self.rows(page_size=2, page=3, ordering="amount")
        self.assertEqual([r["s_no"] for r in page["results"]], [5])
        self.assertEqual(page["results"][0]["amount"], "1000.00")


class AdjustmentTests(LedgerBase):
    def test_preview_shows_new_balance(self):
        res = self.client.post(PREVIEW, {"owner_id": self.fac.pk, "sub_wallet_id": self.sw1.pk, "direction": "debit", "amount": "100"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual((res.data["balance_before"], res.data["balance_after"]), ("800.00", "700.00"))
        res = self.client.post(PREVIEW, {"owner_id": self.fac.pk, "sub_wallet_id": self.sw1.pk, "direction": "debit", "amount": "900"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INSUFFICIENT_BALANCE")
        res = self.client.post(PREVIEW, {"owner_id": self.fac.pk, "sub_wallet_id": self.sw2.pk, "direction": "credit", "amount": "1"}, format="json")
        self.assertEqual(res.status_code, 404)

    def test_credit_creates_ledger_entry_and_audit(self):
        with self.assertLogs("iic_booking.audit.staff_actions", level="INFO") as logs:
            res = self.adjust()
        self.assertEqual(res.status_code, 201, res.data)
        self.sw1.refresh_from_db()
        self.assertEqual(self.sw1.balance, Decimal("1050.50"))
        record = WalletAdminAdjustment.objects.get()
        self.assertEqual(record.performed_by, self.admin)
        self.assertEqual((record.balance_before, record.balance_after), (Decimal("800.00"), Decimal("1050.50")))
        self.assertTrue(record.reference.startswith("WAC-"))
        txn = record.sub_wallet_transaction
        self.assertEqual(txn.transaction_type, "credit")
        self.assertIn(record.reference, txn.description)
        self.assertIn("Correction", txn.description)
        self.assertIn("UTR123", txn.description)
        self.assertNotIn("Ref:", txn.description)
        self.assertTrue(any("wallet_admin_credit" in line and record.reference in line for line in logs.output))
        row = self.client.get(TXNS, {"owner": self.fac.pk, "category": "manual_admin"}).data["results"][0]
        self.assertEqual(row["performed_by"], "Admin WL")
        self.assertEqual(row["reference"], record.reference)
        self.assertEqual(row["balance_after"], "1050.50")

    def test_credit_can_open_new_department_sub_wallet(self):
        res = self.adjust(owner_id=self.fac2.pk, sub_wallet_id=None, department_id=self.chem.pk, amount="10")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(SubWallet.objects.get(wallet=self.w2, department=self.chem).balance, Decimal("10.00"))
        res = self.adjust(client_request_id="req-0000002", owner_id=self.ext.pk, sub_wallet_id=None,
                          department_id=self.phys.pk, direction="debit", amount="1")
        self.assertEqual(res.status_code, 404)

    def test_debit_never_below_zero(self):
        res = self.adjust(direction="debit", amount="800.01")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "INSUFFICIENT_BALANCE")
        self.sw1.refresh_from_db()
        self.assertEqual(self.sw1.balance, Decimal("800.00"))
        self.assertFalse(WalletAdminAdjustment.objects.exists())
        res = self.adjust(owner_id=self.ext.pk, sub_wallet_id=self.sw3.pk, direction="debit", amount="1")
        self.assertEqual(res.status_code, 400)
        res = self.adjust(client_request_id="req-0000003", direction="debit", amount="800")
        self.assertEqual(res.status_code, 201)
        self.sw1.refresh_from_db()
        self.assertEqual(self.sw1.balance, Decimal("0.00"))
        self.assertTrue(WalletAdminAdjustment.objects.get().reference.startswith("WAD-"))

    def test_idempotent_request_id(self):
        first = self.adjust()
        second = self.adjust()
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.data["replayed"])
        self.assertEqual(second.data["id"], first.data["id"])
        self.assertEqual(WalletAdminAdjustment.objects.count(), 1)
        self.sw1.refresh_from_db()
        self.assertEqual(self.sw1.balance, Decimal("1050.50"))
        clash = self.adjust(amount="1.00")
        self.assertEqual(clash.status_code, 409)
        self.assertEqual(self.adjust(client_request_id="short").status_code, 400)

    def test_atomic_when_record_fails(self):
        before = SubWalletTransaction.objects.count()
        with mock.patch.object(WalletAdminAdjustment.objects, "create", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.adjust()
        self.sw1.refresh_from_db()
        self.assertEqual(self.sw1.balance, Decimal("800.00"))
        self.assertEqual(SubWalletTransaction.objects.count(), before)

    def test_validation(self):
        cases = [
            ({"amount": "10.123"}, "INVALID_AMOUNT"),
            ({"amount": "0"}, "INVALID_AMOUNT"),
            ({"amount": "-5"}, "INVALID_AMOUNT"),
            ({"amount": "10000000.01"}, "INVALID_AMOUNT"),
            ({"reason": ""}, "REASON_REQUIRED"),
            ({"remarks": " "}, "REMARKS_REQUIRED"),
            ({"direction": "both"}, "INVALID_DIRECTION"),
        ]
        for i, (override, code) in enumerate(cases):
            res = self.adjust(client_request_id=f"req-val-{i:04d}", **override)
            self.assertEqual(res.status_code, 400, override)
            self.assertEqual(res.data["code"], code)
        self.assertFalse(WalletAdminAdjustment.objects.exists())

    def test_email_toggle(self):
        with self.captureOnCommitCallbacks(execute=True):
            self.adjust(notify_owner=False)
        self.assertEqual(len(mail.outbox), 0)
        self.assertIsNone(WalletAdminAdjustment.objects.get().email_sent_at)
        with self.captureOnCommitCallbacks(execute=True):
            res = self.adjust(client_request_id="req-0000009", notify_owner=True, direction="debit", amount="5")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertIn("debited from your Chemistry WL wallet", msg.subject)
        self.assertIn("Fix double charge", msg.body)
        self.assertIsNotNone(WalletAdminAdjustment.objects.get(client_request_id="req-0000009").email_sent_at)


class ExportTests(LedgerBase):
    def export(self, client, report, **params):
        return client.get(f"/api/exports/{report}/", {"export_format": "csv", **params})

    def test_owner_and_transaction_exports(self):
        res = self.export(self.client, "admin-wallet-owners", department=self.chem.pk)
        self.assertEqual(res.status_code, 200)
        text = res.content.decode("utf-8-sig")
        self.assertIn("Alpha Faculty", text)
        self.assertNotIn(self.fac.email, text)
        res = self.export(self.client, "admin-wallet-transactions", owner=self.fac.pk, type="debit")
        self.assertEqual(res.status_code, 200)
        rows = list(csv.reader(io.StringIO(res.content.decode("utf-8-sig"))))
        self.assertTrue(any("IIC-XRD-0001" in cell for row in rows for cell in row))
        self.assertEqual(self.client.get("/api/exports/admin-wallet-owners/", {"export_format": "xlsx"}).status_code, 200)
        self.assertEqual(self.export(self.as_user(self.fac), "admin-wallet-transactions").status_code, 403)
