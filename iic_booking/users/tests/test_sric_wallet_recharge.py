"""SRIC wallet recharge via the emailed Wallet_Recharge.csv (all people, ids and ledgers are invented)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from email.message import EmailMessage
from email.utils import format_datetime
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.users import sric_wallet_recharge as svc
from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.sric_wallet_recharge import (
    SricReceiverMapping,
    SricWalletMailMessage,
    SricWalletRecharge,
    SricWalletRechargeSettings,
)
from iic_booking.users.models.wallet import (
    SubWallet,
    SubWalletTransaction,
    WalletRechargeMode,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
)
from iic_booking.users.sric_wallet_csv import (
    financial_year,
    is_wallet_recharge_attachment,
    normalize_employee_id,
    parse_wallet_recharge_csv,
)
from iic_booking.users.sric_wallet_mail_auth import check_message

User = get_user_model()
SENDER = "no-reply@sric.iitr.ac.in"
HEADER = "Project Number,PI Name,Employee ID,Ledger ID,Receiver Project,Amount"
INTERNAL_HOPS = [
    "from mx1.mail.local (mx1.mail.local [10.20.30.40]) by store.mail.local with LMTP",
    "from relay.mail.local (relay.mail.local [10.20.30.41]) by gw.mail.local with ESMTP",
]
PUBLIC_HOP = "from evil.example.com (evil.example.com [198.51.100.7]) by gw.mail.local with ESMTP"
FOREIGN_HOP = "from mail.example.net (mail.example.net [74.125.10.20]) by gw.mail.local with ESMTP"


def csv_text(*rows: str) -> str:
    return "\n".join([HEADER, *rows]) + "\n"


def make_email(
    content: str | bytes,
    *,
    sender: str = SENDER,
    filename: str = "Wallet_Recharge.csv",
    hops=None,
    marker: int = 1,
    when: datetime | None = None,
    message_id: str = "<m1@sric.example>",
    extra_headers: dict | None = None,
) -> bytes:
    msg = EmailMessage()
    for hop in hops if hops is not None else INTERNAL_HOPS:
        msg["Received"] = hop
    msg["From"] = f"SRIC Portal <{sender}>"
    msg["To"] = "portal@test.iitr.ac.in"
    msg["Return-Path"] = f"<{sender}>"
    msg["Subject"] = "Wallet Recharge"
    msg["Message-ID"] = message_id
    msg["Date"] = format_datetime(when or timezone.make_aware(datetime(2026, 10, 9, 11, 0)))
    for _ in range(marker):
        msg["mail_from_trusted_domains"] = "True"
    for k, v in (extra_headers or {}).items():
        msg[k] = v
    msg.set_content("Please find attached.")
    data = content.encode("utf-8") if isinstance(content, str) else content
    msg.add_attachment(data, maintype="text", subtype="csv", filename=filename)
    return msg.as_bytes()


class CsvParsingTests(TestCase):
    def test_basic_row(self):
        parsed = parse_wallet_recharge_csv(csv_text("ABC-1234/26-27,Prof. Test One,123456,LED12345600012345,IIC-000-002,10000"))
        self.assertEqual(parsed.error, "")
        row = parsed.rows[0]
        self.assertEqual(
            (row.project_number, row.employee_id, row.ledger_id, row.receiver_code, row.amount, row.errors),
            ("ABC-1234/26-27", "123456", "LED12345600012345", "IIC-000-002", Decimal("10000.00"), []),
        )

    def test_bom_quotes_whitespace_and_thousands(self):
        content = ("\ufeff" + csv_text(
            ' "ABC-1/26-27" , "Prof. A, B" , " 0123456 " , led-9 , iic-000-002 , "10,000.50"',
            "XYZ-2/26-27,Dr. C,7,LED-10,TINK-000-01,2500",
        )).encode("utf-8")
        rows = parse_wallet_recharge_csv(content).rows
        self.assertEqual([r.amount for r in rows], [Decimal("10000.50"), Decimal("2500.00")])
        self.assertEqual((rows[0].pi_name, rows[0].employee_id, rows[0].ledger_id, rows[0].receiver_code),
                         ("Prof. A, B", "0123456", "LED-9", "IIC-000-002"))

    def test_unquoted_thousands_separator_in_last_column(self):
        row = parse_wallet_recharge_csv(csv_text("P-1,Prof. D,55,LED-11,IIC-000-002,10,000")).rows[0]
        self.assertEqual(row.amount, Decimal("10000.00"))

    def test_invalid_amounts_and_missing_values_flag_errors(self):
        rows = parse_wallet_recharge_csv(csv_text("P,X,1,LED-1,IIC-000-002,abc", "P,X,1,LED-2,IIC-000-002,0", "P,X,,LED-3,,-5")).rows
        self.assertEqual(rows[0].errors, ["invalid_amount"])
        self.assertEqual(rows[1].errors, ["invalid_amount"])
        self.assertEqual(rows[2].errors, ["missing_employee_id", "missing_receiver", "invalid_amount"])

    def test_missing_columns_and_empty_file(self):
        self.assertIn("Missing column", parse_wallet_recharge_csv("A,B\n1,2\n").error)
        self.assertTrue(parse_wallet_recharge_csv(b"").error)
        self.assertTrue(parse_wallet_recharge_csv(HEADER + "\n").error)

    def test_attachment_names(self):
        for ok in ("Wallet_Recharge.csv", "wallet_recharge.CSV", "Wallet_Recharge (1).csv", "Wallet_Recharge(2).csv"):
            self.assertTrue(is_wallet_recharge_attachment(ok), ok)
        for bad in ("Other.csv", "Wallet_Recharge.txt", "01.10.26.txt", ""):
            self.assertFalse(is_wallet_recharge_attachment(bad), bad)

    def test_financial_year_and_employee_normalisation(self):
        self.assertEqual(financial_year(date(2026, 3, 31)), "2025-26")
        self.assertEqual(financial_year(date(2026, 4, 1)), "2026-27")
        self.assertEqual(financial_year(date(2027, 1, 15)), "2026-27")
        self.assertEqual(normalize_employee_id(" 00123456 "), "123456")
        self.assertEqual(normalize_employee_id("123456.0"), "123456")
        self.assertEqual(normalize_employee_id("e0042"), "E0042")


class MailAuthTests(TestCase):
    def setUp(self):
        self.config = SricWalletRechargeSettings.get_singleton()

    def _check(self, **kw):
        import email as email_lib

        return check_message(email_lib.message_from_bytes(make_email(csv_text(), **kw)), self.config)

    def test_internal_relay_with_gateway_marker_passes(self):
        result = self._check()
        self.assertTrue(result.authenticated, result.verdict)

    def test_spoof_through_public_relay_fails(self):
        self.assertFalse(self._check(hops=[*INTERNAL_HOPS, PUBLIC_HOP]).authenticated)
        self.assertFalse(self._check(hops=[FOREIGN_HOP, *INTERNAL_HOPS]).authenticated)
        self.config.trusted_relay_ranges = "74.125.10.0/24"
        self.assertTrue(self._check(hops=[FOREIGN_HOP, *INTERNAL_HOPS]).authenticated)

    def test_missing_or_repeated_marker_fails(self):
        self.assertFalse(self._check(marker=0).authenticated)
        self.assertFalse(self._check(marker=2).authenticated)

    def test_wrong_sender_fails(self):
        self.assertFalse(self._check(sender="no-reply@sric.example.org").authenticated)

    def test_trusted_authentication_results_pass_only_from_trusted_server(self):
        ar = {"Authentication-Results": "mx.portal.test; spf=pass smtp.mailfrom=no-reply@sric.iitr.ac.in; dkim=none"}
        self.assertFalse(self._check(hops=[PUBLIC_HOP], extra_headers=ar).authenticated)
        self.config.trusted_authserv_ids = "mx.portal.test"
        self.assertTrue(self._check(hops=[PUBLIC_HOP], extra_headers=ar).authenticated)
        forged = {"Authentication-Results": "attacker.test; spf=pass smtp.mailfrom=no-reply@sric.iitr.ac.in"}
        self.assertFalse(self._check(hops=[PUBLIC_HOP], extra_headers=forged).authenticated)


class _World(TestCase):
    def setUp(self):
        cache.clear()
        self.iic = Department.objects.create(name="Test Instrument Centre", code="TIC", department_type=DepartmentType.INTERNAL)
        self.tink = Department.objects.create(name="Test Tinker Lab", code="TTL", department_type=DepartmentType.INTERNAL)
        SricReceiverMapping.objects.create(code="IIC-000-002", label="IIC", department=self.iic)
        SricReceiverMapping.objects.create(code="TINK-000-01", label="Tinkering", department=self.tink)
        self.faculty = User.objects.create_user(
            email="fac.one@test.iitr.ac.in", password="pass12345", email_verified=True, admin_approved=True, name="Test Faculty", user_type=UserType.FACULTY, emp_id="123456"
        )
        self.admin = User.objects.create_user(
            email="main.admin@test.iitr.ac.in", password="pass12345", email_verified=True, admin_approved=True, name="Main", user_type=UserType.ADMIN
        )
        self.config = SricWalletRechargeSettings.get_singleton()
        self.config.scan_enabled = True
        self.config.auto_credit_enabled = True
        self.config.save()

    def process(self, content, *, uid="1", **kw):
        with self.captureOnCommitCallbacks(execute=True):
            return svc.process_message(make_email(content, **kw), uid=uid, folder="INBOX", config=self.config)

    def balance(self, dept=None, user=None):
        sub = SubWallet.objects.filter(wallet__user=user or self.faculty, department=dept or self.iic).first()
        return sub.balance if sub else Decimal("0.00")

    def admin_client(self):
        client = APIClient()
        client.force_authenticate(self.admin)
        return client


class IngestionTests(_World):
    def test_auto_credit_credits_sub_wallet_records_ledger_and_emails_faculty(self):
        summary = self.process(csv_text("ABC-1/26-27,Prof. Test,123456,LED-100,IIC-000-002,10000"))
        rec = SricWalletRecharge.objects.get()
        self.assertEqual((summary["status"], rec.status, rec.financial_year), ("processed", "credited", "2026-27"))
        self.assertEqual(self.balance(), Decimal("10000.00"))
        txn = rec.wallet_transaction
        self.assertIn("LED-100", txn.description)
        self.assertIn("ABC-1/26-27", txn.description)
        self.assertTrue(txn.description.startswith("SRIC wallet recharge"))
        self.assertEqual(rec.credit_key, "LED-100|2026-27")
        msg = SricWalletMailMessage.objects.get()
        self.assertEqual((msg.message_id, msg.row_count, msg.authenticated, len(msg.attachment_sha256)), ("<m1@sric.example>", 1, True, 64))
        sent = [m for m in mail.outbox if "fac.one@test.iitr.ac.in" in m.to]
        self.assertEqual(len(sent), 1)
        self.assertIn("LED-100", sent[0].body)
        self.assertIn("10,000.00", sent[0].body)
        self.assertIsNotNone(SricWalletRecharge.objects.get().confirmation_sent_at)

    def test_tinkering_receiver_and_leading_zero_employee_id(self):
        self.process(csv_text("P-2,Prof. Test,00123456,LED-101,TINK-000-01,2500.50"))
        self.assertEqual(self.balance(self.tink), Decimal("2500.50"))

    def test_same_ledger_same_year_is_duplicate_next_year_is_credited(self):
        row = "P-3,Prof. Test,123456,LED-102,IIC-000-002,1000"
        self.process(csv_text(row), uid="1", message_id="<a@x>")
        self.process(csv_text(row), uid="2", message_id="<b@x>")
        self.assertEqual(list(SricWalletRecharge.objects.order_by("pk").values_list("status", flat=True)), ["credited", "duplicate"])
        self.assertEqual(self.balance(), Decimal("1000.00"))
        dup = SricWalletRecharge.objects.get(status="duplicate")
        self.assertEqual(dup.duplicate_of_id, SricWalletRecharge.objects.get(status="credited").pk)
        self.process(csv_text(row), uid="3", message_id="<c@x>", when=timezone.make_aware(datetime(2027, 4, 2, 10, 0)))
        self.assertEqual(self.balance(), Decimal("2000.00"))
        self.assertEqual(SricWalletRecharge.objects.filter(status="credited").count(), 2)

    def test_same_message_redelivered_is_not_reprocessed(self):
        content = csv_text("P-4,Prof. Test,123456,LED-103,IIC-000-002,500")
        self.process(content, uid="1")
        summary = self.process(content, uid="9")
        self.assertEqual(summary["status"], "duplicate_message")
        self.assertEqual(SricWalletRecharge.objects.count(), 1)

    def test_auto_credit_off_queues_rows_without_credit(self):
        self.config.auto_credit_enabled = False
        self.config.save()
        self.process(csv_text("P-5,Prof. Test,123456,LED-104,IIC-000-002,700"))
        rec = SricWalletRecharge.objects.get()
        self.assertEqual(rec.status, "awaiting_credit")
        self.assertEqual(self.balance(), Decimal("0.00"))
        self.assertFalse(SubWalletTransaction.objects.exists())

    def test_needs_review_paths_never_credit(self):
        User.objects.create_user(email="stu@test.iitr.ac.in", password="x12345678", email_verified=True, admin_approved=True, name="S", user_type=UserType.EXTERNAL, emp_id="888")
        User.objects.create_user(email="f2@test.iitr.ac.in", password="x12345678", email_verified=True, admin_approved=True, name="F2", user_type=UserType.FACULTY, emp_id="0777")
        User.objects.create_user(email="f3@test.iitr.ac.in", password="x12345678", email_verified=True, admin_approved=True, name="F3", user_type=UserType.FACULTY, emp_id="777")
        self.process(csv_text(
            "P,Prof. A,123456,LED-201,UNKNOWN-1,100",
            "P,Prof. B,999999,LED-202,IIC-000-002,100",
            "P,Prof. C,777,LED-203,IIC-000-002,100",
            "P,Mr. D,888,LED-204,IIC-000-002,100",
            "P,Prof. E,123456,LED-205,IIC-000-002,0",
        ))
        reasons = dict(SricWalletRecharge.objects.values_list("ledger_id", "review_reason"))
        self.assertEqual(reasons, {
            "LED-201": "unknown_receiver",
            "LED-202": "unmatched_employee",
            "LED-203": "ambiguous_employee",
            "LED-204": "not_faculty",
            "LED-205": "invalid_amount",
        })
        self.assertEqual(set(SricWalletRecharge.objects.values_list("status", flat=True)), {"needs_review"})
        self.assertFalse(SubWalletTransaction.objects.exists())

    def test_unverified_origin_goes_to_review(self):
        self.process(csv_text("P,Prof. Test,123456,LED-206,IIC-000-002,100"), hops=[*INTERNAL_HOPS, PUBLIC_HOP])
        rec = SricWalletRecharge.objects.get()
        self.assertEqual((rec.status, rec.review_reason, rec.origin_verified), ("needs_review", "origin_unverified", False))
        self.assertEqual(self.balance(), Decimal("0.00"))

    def test_auto_credit_limit(self):
        self.config.auto_credit_max_amount = Decimal("5000")
        self.config.save()
        self.process(csv_text("P,Prof. Test,123456,LED-207,IIC-000-002,6000"))
        self.assertEqual(SricWalletRecharge.objects.get().review_reason, "over_auto_limit")

    def test_wrong_sender_no_attachment_and_before_cutoff(self):
        self.assertEqual(self.process(csv_text(), uid="1", sender="someone@sric.iitr.ac.in")["status"], "wrong_sender")
        self.assertEqual(self.process(csv_text(), uid="2", filename="Bills.csv", message_id="<z@x>")["status"], "no_attachment")
        old = timezone.make_aware(datetime(2026, 9, 1, 10, 0))
        self.assertEqual(self.process(csv_text(), uid="3", when=old, message_id="<y@x>")["status"], "before_cutoff")
        self.assertEqual(SricWalletMailMessage.objects.count(), 3)

    def test_credit_row_is_idempotent(self):
        self.config.auto_credit_enabled = False
        self.config.save()
        self.process(csv_text("P,Prof. Test,123456,LED-208,IIC-000-002,300"))
        rec = SricWalletRecharge.objects.get()
        _, first = svc.credit_row(rec.pk, actor=self.admin)
        _, second = svc.credit_row(rec.pk, actor=self.admin)
        self.assertEqual((first, second), (True, False))
        self.assertEqual(SubWalletTransaction.objects.count(), 1)
        self.assertEqual(self.balance(), Decimal("300.00"))

    def test_review_alert_emailed_to_main_admin(self):
        mail.outbox.clear()
        svc.send_review_alert(
            [self.process(csv_text("P,Prof. B,999999,LED-209,IIC-000-002,100"))["review_row_ids"][0]]
        )
        alert = [m for m in mail.outbox if "main.admin@test.iitr.ac.in" in m.to]
        self.assertEqual(len(alert), 1)
        self.assertIn("LED-209", alert[0].body)


class FakeImap:
    def __init__(self, messages: dict[str, bytes]):
        self.messages = messages
        self.flag_changes = 0

    def select(self, folder, readonly=False):
        assert readonly
        return "OK", [b"1"]

    def uid(self, command, *args):
        if command == "search":
            return "OK", [" ".join(self.messages).encode()]
        if command == "fetch":
            uid = args[0].decode()
            assert "PEEK" in args[1]
            return "OK", [(b"1 (BODY[] {1}", self.messages[uid]), b")"]
        self.flag_changes += 1
        return "NO", []

    def logout(self):
        return "BYE", []


IMAP = {"host": "imap.test", "port": 993, "use_ssl": True, "email_address": "portal@test", "password": "x", "timeout": 5}


class ScanTests(_World):
    def scan(self, messages, **kw):
        fake = FakeImap(messages)
        with mock.patch.object(svc, "_imap_config", return_value=IMAP), mock.patch(
            "iic_booking.users.imap_fetch.connect_imap", return_value=fake
        ), self.captureOnCommitCallbacks(execute=True):
            return svc.scan_mailbox(**kw), fake

    def test_scan_processes_once_and_never_changes_the_mailbox(self):
        msgs = {"5": make_email(csv_text("P,Prof. Test,123456,LED-300,IIC-000-002,100"))}
        first, fake = self.scan(msgs)
        again, _ = self.scan(msgs)
        self.assertEqual((first["messages_read"], first["credited"], again["messages_new"]), (1, 1, 0))
        self.assertEqual(fake.flag_changes, 0)
        self.assertEqual(SricWalletRecharge.objects.count(), 1)
        self.assertIsNotNone(SricWalletRechargeSettings.get_singleton().last_scan_at)

    def test_disabled_scan_and_dry_run_store_nothing(self):
        self.config.scan_enabled = False
        self.config.save()
        msgs = {"5": make_email(csv_text("P,Prof. Test,123456,LED-301,IIC-000-002,100"))}
        self.assertEqual(self.scan(msgs)[0]["status"], "disabled")
        result, _ = self.scan(msgs, dry_run=True)
        self.assertEqual((result["status"], result["rows_new"], result["statuses"]), ("dry_run", 1, {"credited": 1}))
        self.assertFalse(SricWalletMailMessage.objects.exists())
        self.assertFalse(SricWalletRecharge.objects.exists())
        out = StringIO()
        with mock.patch.object(svc, "_imap_config", return_value=IMAP), mock.patch(
            "iic_booking.users.imap_fetch.connect_imap", return_value=FakeImap(msgs)
        ):
            call_command("sric_wallet_recharge", "dry-run-scan", stdout=out)
        self.assertIn("would_be={'credited': 1}", out.getvalue())
        self.assertNotIn("LED-301", out.getvalue())
        self.assertNotIn("123456", out.getvalue())


class ApiTests(_World):
    def setUp(self):
        super().setUp()
        self.config.auto_credit_enabled = False
        self.config.save()

    def _queued(self, row="P-9,Prof. Test,123456,LED-400,IIC-000-002,900", **kw):
        self.process(csv_text(row), **kw)
        return SricWalletRecharge.objects.latest("pk")

    def test_admin_endpoints_require_main_admin(self):
        client = APIClient()
        client.force_authenticate(self.faculty)
        self.assertEqual(client.get("/api/admin/sric-wallet-recharges/").status_code, 403)
        rec = self._queued()
        self.assertEqual(client.post(f"/api/admin/sric-wallet-recharges/{rec.pk}/credit/", {}, format="json").status_code, 403)

    def test_admin_list_filters_and_credit_once(self):
        rec = self._queued()
        client = self.admin_client()
        data = client.get("/api/admin/sric-wallet-recharges/", {"status": "awaiting_credit", "search": "LED-400"}).json()
        self.assertEqual((data["count"], data["results"][0]["can_credit"]), (1, True))
        self.assertEqual(client.get("/api/admin/sric-wallet-recharges/", {"financial_year": "2025-26"}).json()["count"], 0)
        with self.captureOnCommitCallbacks(execute=True):
            ok = client.post(f"/api/admin/sric-wallet-recharges/{rec.pk}/credit/", {}, format="json")
        self.assertEqual(ok.status_code, 200)
        again = client.post(f"/api/admin/sric-wallet-recharges/{rec.pk}/credit/", {}, format="json")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.balance(), Decimal("900.00"))
        verify = client.post(f"/api/admin/sric-wallet-recharges/{rec.pk}/verify/", {"verified": True, "remarks": "Receipt seen"}, format="json")
        self.assertTrue(verify.json()["row"]["fund_receipt_verified"])

    def test_assign_user_and_receiver_then_credit(self):
        rec = self._queued("P,Prof. Unknown,999999,LED-401,NEW-CODE,450")
        client = self.admin_client()
        resp = client.post(
            f"/api/admin/sric-wallet-recharges/{rec.pk}/credit/",
            {"user_id": self.faculty.pk, "receiver_code": "TINK-000-01"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self.balance(self.tink), Decimal("450.00"))

    def test_reject_and_duplicate_not_creditable(self):
        rec = self._queued()
        self.process(csv_text("P-9,Prof. Test,123456,LED-400,IIC-000-002,900"), uid="2", message_id="<dup@x>")
        dup = SricWalletRecharge.objects.get(status="duplicate")
        client = self.admin_client()
        self.assertEqual(client.post(f"/api/admin/sric-wallet-recharges/{dup.pk}/credit/", {}, format="json").status_code, 409)
        self.assertEqual(client.post(f"/api/admin/sric-wallet-recharges/{rec.pk}/reject/", {"reason": ""}, format="json").status_code, 400)
        self.assertEqual(client.post(f"/api/admin/sric-wallet-recharges/{rec.pk}/reject/", {"reason": "Test entry"}, format="json").status_code, 200)
        self.assertEqual(client.post(f"/api/admin/sric-wallet-recharges/{rec.pk}/credit/", {}, format="json").status_code, 409)
        self.assertEqual(self.balance(), Decimal("0.00"))

    def test_settings_and_mappings(self):
        client = self.admin_client()
        resp = client.patch("/api/admin/sric-wallet-recharges/settings/", {"auto_credit_enabled": True}, format="json")
        self.assertTrue(resp.json()["auto_credit_enabled"])
        resp = client.post("/api/admin/sric-wallet-recharges/mappings/", {"code": "new-01", "label": "New", "department_id": self.iic.pk}, format="json")
        self.assertIn("NEW-01", [m["code"] for m in resp.json()["mappings"]])

    def test_refresh_permissions_rate_limit_and_message(self):
        def fake_scan(**kw):
            self.config.auto_credit_enabled = True
            self.config.save()
            self.process(csv_text("P-10,Prof. Test,123456,LED-402,IIC-000-002,1200"))
            return {"status": "ok", "messages_read": 1}

        client = APIClient()
        client.force_authenticate(self.faculty)
        with mock.patch.object(svc, "scan_mailbox", side_effect=fake_scan):
            resp = client.post("/api/wallet/sric-recharges/refresh/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("Credited ₹1,200.00 (ledger LED-402)", resp.json()["message"])
        self.assertNotIn("pi_name", resp.json()["results"][0])
        limited = client.post("/api/wallet/sric-recharges/refresh/")
        self.assertEqual(limited.status_code, 429)
        self.assertIn("retry_after", limited.json())
        mine = client.get("/api/wallet/sric-recharges/").json()
        self.assertEqual((mine["portal_url"], len(mine["results"])), ("https://rnd.iitr.ac.in", 1))

        student = User.objects.create_user(email="s1@test.iitr.ac.in", password="x12345678", email_verified=True, admin_approved=True, name="S1", user_type=UserType.STUDENT)
        client.force_authenticate(student)
        self.assertEqual(client.post("/api/wallet/sric-recharges/refresh/").status_code, 403)

    def test_refresh_reports_no_new_recharges_and_is_debounced(self):
        client = APIClient()
        client.force_authenticate(self.faculty)
        with mock.patch.object(svc, "scan_mailbox", return_value={"status": "ok"}) as scan:
            self.assertEqual(client.post("/api/wallet/sric-recharges/refresh/").json()["message"], "No new recharges.")
            other = APIClient()
            other.force_authenticate(self.admin)
            self.assertTrue(other.post("/api/admin/sric-wallet-recharges/refresh/").json()["debounced"])
        self.assertEqual(scan.call_count, 1)


class ProjectGrantRetiredTests(_World):
    def test_new_project_grant_request_rejected_with_procedure(self):
        from iic_booking.users.models.wallet_sric_settings import WalletSricSettings

        from iic_booking.users.models import Project

        WalletSricSettings.objects.update_or_create(pk=1, defaults={"project_grant_recharge_enabled": True})
        project = Project.objects.create(faculty=self.faculty, name="Test project", project_code="TST/2026/001", agency="DST")
        client = APIClient()
        client.force_authenticate(self.faculty)
        resp = client.post(
            "/api/wallet/recharge-request/send-otp/",
            {"amount": "1000", "department_id": self.iic.pk, "project_id": project.pk, "recharge_mode": "project_grant", "undertaking_accepted": True},
            format="json",
        )
        self.assertEqual(resp.status_code, 403, resp.content)
        body = resp.json()
        self.assertTrue(body["retired"])
        self.assertIn("rnd.iitr.ac.in", body["error"])

    def test_mode_flags_report_retired(self):
        from iic_booking.users.models.wallet_sric_settings import WalletSricSettings, wallet_mode_flags

        WalletSricSettings.objects.update_or_create(pk=1, defaults={"project_grant_recharge_enabled": True})
        flags = wallet_mode_flags(self.faculty)
        self.assertEqual((flags["project_grant_recharge_enabled"], flags["project_grant_retired"]), (False, True))


class RetirePendingProjectGrantTests(_World):
    def _req(self, mode=WalletRechargeMode.PROJECT_GRANT, status=WalletRechargeRequestStatus.PENDING, **extra):
        wallet, _ = Wallet.objects.get_or_create(user=self.faculty)
        return WalletRechargeRequest.objects.create(
            user=self.faculty, wallet=wallet, department=self.iic, amount=Decimal("100"), status=status,
            recharge_mode=mode, user_otp_verified=True, **extra,
        )

    def test_dry_run_then_apply_only_pending_project_grant(self):
        pending = self._req(cashbook_receipt_no="R-77")
        second = self._req()
        approved = self._req(status=WalletRechargeRequestStatus.APPROVED)
        cash = self._req(mode=WalletRechargeMode.DIRECT_CASH_DEPOSIT)
        deleted = self._req(is_deleted=True)
        out = StringIO()
        call_command("retire_project_grant_pending", stdout=out)
        self.assertIn(f"selected_ids={[pending.pk, second.pk]}", out.getvalue())
        self.assertIn(f"with_cashbook_link_ids={[pending.pk]}", out.getvalue())
        self.assertFalse(WalletRechargeRequest.objects.filter(is_deleted=True).exclude(pk=deleted.pk).exists())

        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            call_command("retire_project_grant_pending", "--apply", stdout=StringIO())
        mail.outbox.clear()
        out = StringIO()
        call_command("retire_project_grant_pending", "--apply", "--confirm", "RETIRE", stdout=out)
        self.assertIn("deleted=2", out.getvalue())
        self.assertIn("remaining_pending_project_grant=0", out.getvalue())
        pending.refresh_from_db()
        self.assertTrue(pending.is_deleted)
        self.assertEqual(pending.status, WalletRechargeRequestStatus.CANCELLED)
        self.assertEqual(pending.cashbook_receipt_no, "")
        self.assertEqual(pending.deletion_reason, "Project grant mode retired — use SRIC wallet recharge")
        for other in (approved, cash):
            other.refresh_from_db()
            self.assertFalse(other.is_deleted)
        cash.refresh_from_db()
        self.assertEqual(cash.status, WalletRechargeRequestStatus.PENDING)
        self.assertEqual(len(mail.outbox), 0)
