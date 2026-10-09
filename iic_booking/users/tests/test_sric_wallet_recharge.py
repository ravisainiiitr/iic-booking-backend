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
        self.config.quiet_window_enabled = False
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


def utc(*args):
    from datetime import timezone as dt_tz

    return datetime(*args, tzinfo=dt_tz.utc)


# Wednesday 7 Oct 2026: 20:55 IST = 15:25 UTC, 21:15 IST = 15:45 UTC.
IN_WINDOW = utc(2026, 10, 7, 15, 30)
AFTER_WINDOW = utc(2026, 10, 7, 15, 45)


class QuietWindowTests(_World):
    def setUp(self):
        super().setUp()
        self.config.quiet_window_enabled = True
        self.config.save()

    def at(self, when):
        return mock.patch("django.utils.timezone.now", return_value=when)

    def test_defaults_are_wednesday_2055_to_2115(self):
        fresh = SricWalletRechargeSettings()
        self.assertEqual((fresh.quiet_window_enabled, fresh.quiet_window_weekday, f"{fresh.quiet_window_start:%H:%M}", f"{fresh.quiet_window_end:%H:%M}"),
                         (True, 2, "20:55", "21:15"))
        self.assertEqual(svc.quiet_window_label(fresh), {"window": "Wednesday 8:55–9:15 PM", "resume": "9:15 PM"})

    def test_boundaries_weekdays_and_disabled_flag(self):
        cases = [
            (utc(2026, 10, 7, 15, 24, 59), False),
            (utc(2026, 10, 7, 15, 25, 0), True),
            (utc(2026, 10, 7, 15, 44, 59), True),
            (utc(2026, 10, 7, 15, 45, 0), False),
            (utc(2026, 10, 8, 15, 30), False),
            (utc(2026, 10, 6, 15, 30), False),
            (utc(2026, 10, 14, 15, 30), True),
        ]
        for when, expected in cases:
            self.assertEqual(svc.in_quiet_window(self.config, when), expected, when)
        self.config.quiet_window_enabled = False
        self.assertFalse(svc.in_quiet_window(self.config, IN_WINDOW))

    def test_window_is_ist_even_with_a_utc_server_clock(self):
        from django.test import override_settings

        with override_settings(TIME_ZONE="UTC"):
            self.assertTrue(svc.in_quiet_window(self.config, utc(2026, 10, 7, 15, 25)))
            self.assertFalse(svc.in_quiet_window(self.config, utc(2026, 10, 7, 20, 55)))
            with self.at(IN_WINDOW):
                self.assertTrue(svc.in_quiet_window(self.config))

    def test_scheduled_scan_skips_without_imap_then_next_scan_reads_mail_from_the_window(self):
        msgs = {"5": make_email(csv_text("P,Prof. Test,123456,LED-700,IIC-000-002,300"), when=timezone.make_aware(datetime(2026, 10, 7, 21, 0)))}
        with self.at(IN_WINDOW), mock.patch.object(svc, "_imap_config", return_value=IMAP), mock.patch(
            "iic_booking.users.imap_fetch.connect_imap", side_effect=AssertionError("no IMAP in the window")
        ) as connect:
            result = svc.scan_mailbox(trigger="schedule")
        self.assertEqual(result["status"], "skipped_peak_window")
        connect.assert_not_called()
        self.assertEqual(SricWalletRechargeSettings.get_singleton().last_scan_result["status"], "skipped_peak_window")
        with self.at(AFTER_WINDOW), mock.patch.object(svc, "_imap_config", return_value=IMAP), mock.patch(
            "iic_booking.users.imap_fetch.connect_imap", return_value=FakeImap(msgs)
        ), self.captureOnCommitCallbacks(execute=True):
            result = svc.scan_mailbox(trigger="schedule")
        self.assertEqual((result["status"], result["credited"]), ("ok", 1))
        self.assertEqual(self.balance(), Decimal("300.00"))

    def test_faculty_refresh_is_paused_but_main_admin_refresh_reads(self):
        faculty = APIClient()
        faculty.force_authenticate(self.faculty)
        with self.at(IN_WINDOW), mock.patch.object(svc, "scan_mailbox", return_value={"status": "ok"}) as scan:
            resp = faculty.post("/api/wallet/sric-recharges/refresh/")
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.json()["status"], "skipped_peak_window")
            self.assertEqual(
                resp.json()["message"],
                "Recharge checks are paused during peak booking time (Wednesday 8:55–9:15 PM). "
                "Your recharge will be credited automatically after 9:15 PM.",
            )
            self.assertEqual(scan.call_count, 0)
            admin = self.admin_client().post("/api/admin/sric-wallet-recharges/refresh/").json()
            self.assertEqual(admin["status"], "ok")
            self.assertEqual(scan.call_args.kwargs["trigger"], "admin-refresh")
        self.assertEqual(faculty.post("/api/wallet/sric-recharges/refresh/").status_code, 200)

    def test_admin_refresh_scan_is_not_skipped_in_the_window(self):
        with self.at(IN_WINDOW), mock.patch.object(svc, "_imap_config", return_value=IMAP), mock.patch(
            "iic_booking.users.imap_fetch.connect_imap", return_value=FakeImap({})
        ) as connect:
            self.assertEqual(svc.scan_mailbox(trigger="admin-refresh")["status"], "ok")
        connect.assert_called_once()

    def test_settings_edit_and_validation(self):
        client = self.admin_client()
        url = "/api/admin/sric-wallet-recharges/settings/"
        data = client.get(url).json()
        self.assertEqual((data["quiet_window_enabled"], data["quiet_window_weekday"], data["quiet_window_start"], data["quiet_window_end"]),
                         (True, 2, "20:55", "21:15"))
        self.assertEqual(data["quiet_window_label"], "Wednesday 8:55–9:15 PM")
        resp = client.patch(url, {"quiet_window_weekday": 4, "quiet_window_start": "09:30", "quiet_window_end": "10:00"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["quiet_window_label"], "Friday 9:30–10:00 AM")
        for bad in ({"quiet_window_weekday": 7}, {"quiet_window_start": "25:00"}, {"quiet_window_start": "10:00", "quiet_window_end": "09:00"}):
            self.assertEqual(client.patch(url, bad, format="json").status_code, 400, bad)
        config = SricWalletRechargeSettings.get_singleton()
        self.assertEqual((config.quiet_window_weekday, f"{config.quiet_window_start:%H:%M}"), (4, "09:30"))

    def test_status_command_shows_the_window(self):
        out = StringIO()
        call_command("sric_wallet_recharge", "status", stdout=out)
        self.assertIn("quiet_window enabled=True weekday=Wednesday start=20:55 end=21:15 tz=Asia/Kolkata", out.getvalue())


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


TEST_SENDER = "tester.example@gmail.test"


class FolderImap(FakeImap):
    """Read-only IMAP double with folders (INBOX + a Junk folder)."""

    def __init__(self, folders: dict[str, dict[str, bytes]]):
        super().__init__({})
        self.folders = folders
        self.current = ""

    def list(self):
        return "OK", [f'(\\HasNoChildren) "/" "{name}"'.encode() for name in self.folders]

    def select(self, folder, readonly=False):
        assert readonly
        self.current = folder.strip('"')
        self.messages = self.folders.get(self.current, {})
        return ("OK", [b"1"]) if self.current in self.folders else ("NO", [])

    def uid(self, command, *args):
        if command == "search":
            sender = args[1].split('"')[1]
            return "OK", [" ".join(u for u, raw in self.messages.items() if sender.encode() in raw).encode()]
        return super().uid(command, *args)


class TestSenderRunTests(_World):
    ROW = "P-9,Prof. Test,123456,LED-900,IIC-000-002,1500"

    def setUp(self):
        super().setUp()
        self.config.auto_credit_enabled = False
        self.config.save()
        self.folders = {
            "INBOX": {"3": make_email(csv_text("P,Prof. X,123456,LED-899,IIC-000-002,10"), sender="other@gmail.test")},
            "Junk E-mail": {"7": make_email(csv_text(self.ROW), sender=TEST_SENDER, hops=[FOREIGN_HOP], marker=0,
                                            message_id="<t1@gmail.test>")},
        }
        self.tester = User.objects.create_user(
            email="qa.faculty@qa.example.test", password="pass12345", email_verified=True, admin_approved=True,
            name="QA Faculty", user_type=UserType.FACULTY, emp_id="900001", is_test_account=True,
        )
        wallet = Wallet(user=self.tester)
        wallet.save()
        SubWallet.objects.create(wallet=wallet, department=self.iic, balance=Decimal("250.00"))

    def run_cmd(self, *args):
        out = StringIO()
        with mock.patch.object(svc, "_imap_config", return_value=IMAP), mock.patch(
            "iic_booking.users.imap_fetch.connect_imap", return_value=FolderImap(self.folders)
        ), self.captureOnCommitCallbacks(execute=True):
            call_command("sric_wallet_recharge", *args, stdout=out)
        return out.getvalue()

    def e2e(self, *extra):
        return self.run_cmd("test-sender-e2e", "--test-sender", TEST_SENDER, "--folder", "Junk E-mail", "--uid", "7",
                            "--test-user-id", str(self.tester.pk), *extra)

    def test_dry_run_finds_the_test_email_in_junk_and_stores_nothing(self):
        out = self.run_cmd("test-sender-dry-run", "--test-sender", TEST_SENDER)
        self.assertIn("folder='INBOX' selectable=True from_test_sender=0", out)
        self.assertIn("folder='Junk E-mail' selectable=True from_test_sender=1", out)
        self.assertIn("uid=7", out)
        self.assertIn("in_scanner_folder=False", out)
        self.assertIn("origin_verified_real=False", out)
        self.assertIn("employee_matched=yes", out)
        self.assertIn("receiver=IIC-000-002 -> IIC", out)
        self.assertIn("ledger=LED-900", out)
        self.assertIn("duplicate_real=False", out)
        self.assertIn("would_be=awaiting_credit", out)
        self.assertIn("would_credit=no", out)
        self.assertIn("test_sender=t***@gmail.test", out)
        for secret in ("123456", "Prof. Test", TEST_SENDER):
            self.assertNotIn(secret, out)
        self.assertFalse(SricWalletMailMessage.objects.exists())
        self.assertFalse(SricWalletRecharge.objects.exists())

    def test_find_test_faculty_lists_ids_and_flags_only(self):
        out = self.run_cmd("find-test-faculty")
        self.assertIn("test_faculty_accounts=1 eligible=1", out)
        self.assertIn(f"user_id={self.tester.pk} eligible=True detail=ok has_employee_id=True iic_sub_wallet=True iic_balance=250.00", out)
        for secret in ("900001", "qa.faculty", "QA Faculty"):
            self.assertNotIn(secret, out)

    def test_e2e_credits_the_test_faculty_only_then_duplicate_then_reversal(self):
        mail.outbox.clear()
        out = self.e2e()
        rec = SricWalletRecharge.objects.get()
        msg = SricWalletMailMessage.objects.get()
        self.assertEqual((rec.is_test, rec.ledger_id, rec.status, rec.matched_user_id), (True, "TEST-LED-900", "credited", self.tester.pk))
        self.assertEqual((rec.employee_id, rec.origin_verified), ("900001", False))
        self.assertNotIn("Prof. Test", rec.pi_name)
        self.assertEqual((msg.is_test, msg.trigger, msg.authenticated), (True, "test-sender", False))
        self.assertIn("origin check bypassed", msg.auth_verdict)
        self.assertEqual(self.balance(user=self.tester), Decimal("1750.00"))
        self.assertEqual(self.balance(), Decimal("0.00"))
        self.assertIn("credit=credited final_status=credited", out)
        self.assertIn("balance_before=250.00 balance_after=1750.00", out)
        self.assertIsNotNone(rec.confirmation_sent_at)
        self.assertIn("in_admin_tab=True admin_row_is_test=True on_faculty_page=True", out)
        self.assertEqual([m.subject[:7] for m in mail.outbox], ["[TEST] "])
        self.assertNotIn("fac.one@test.iitr.ac.in", mail.outbox[0].to)
        for secret in ("123456", "900001", "Prof. Test", TEST_SENDER, "qa.faculty"):
            self.assertNotIn(secret, out)
        config = SricWalletRechargeSettings.get_singleton()
        self.assertEqual((config.sender_email, config.auto_credit_enabled), (SENDER, False))

        again = self.e2e()
        self.assertIn("rerun=True", again)
        self.assertIn("stored_status=duplicate reason='duplicate_ledger'", again)
        self.assertIn("credit=refused:NOT_CREDITABLE", again)
        self.assertEqual(SricWalletRecharge.objects.filter(status="duplicate", is_test=True).count(), 1)
        self.assertEqual(self.balance(user=self.tester), Decimal("1750.00"))
        self.assertEqual(len(mail.outbox), 1)

        out = self.run_cmd("test-sender-reverse", "--row-id", str(rec.pk))
        self.assertIn("created=True", out)
        self.assertIn("amount=1500.00 balance_before_credit=250.00 balance_before_reversal=1750.00 balance_after_reversal=250.00", out)
        self.assertEqual(self.balance(user=self.tester), Decimal("250.00"))
        from iic_booking.users.models.wallet_admin_adjustment import WalletAdminAdjustment

        adj = WalletAdminAdjustment.objects.get()
        self.assertEqual((adj.remarks, adj.direction, adj.performed_by_id), ("SRIC CSV test reversal", "debit", self.admin.pk))
        rec.refresh_from_db()
        self.assertFalse(rec.fund_receipt_verified)
        self.assertIn("reversed", rec.fund_receipt_verification_remarks)
        self.assertEqual((rec.reversal_ref, rec.reversed_by_id, rec.reversed_at is not None), (adj.reference, self.admin.pk, True))
        self.assertIn("created=False", self.run_cmd("test-sender-reverse", "--row-id", str(rec.pk)))
        self.assertEqual(self.balance(user=self.tester), Decimal("250.00"))

        client = self.admin_client()
        data = client.get("/api/admin/sric-wallet-recharges/").json()
        row = next(r for r in data["results"] if r["id"] == rec.pk)
        self.assertEqual((row["is_test"], row["reversed"], row["status_display"], row["reversal_ref"], row["can_verify"]),
                         (True, True, "Reversed", adj.reference, False))
        self.assertEqual((data["status_counts"], data["credited_total"], data["test_count"]), ({}, "0.00", 2))
        self.assertEqual(client.get("/api/admin/sric-wallet-recharges/", {"test": "hide"}).json()["count"], 0)
        self.assertEqual(client.get("/api/admin/sric-wallet-recharges/", {"test": "only"}).json()["count"], 2)
        verify = client.post(f"/api/admin/sric-wallet-recharges/{rec.pk}/verify/", {"verified": True}, format="json")
        self.assertEqual(verify.status_code, 400)
        res = client.get("/api/exports/sric-wallet-recharges/", {"export_format": "csv"})
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("TEST-LED-900", b"".join(res.streaming_content if res.streaming else [res.content]).decode("utf-8-sig"))
        tester = APIClient()
        tester.force_authenticate(self.tester)
        mine = tester.get("/api/wallet/sric-recharges/").json()["results"]
        self.assertEqual([(r["is_test"], r["reversed"], r["status_display"]) for r in mine], [(True, True, "Reversed")])

        self.process(csv_text(self.ROW), uid="20", message_id="<real@sric>")
        real = SricWalletRecharge.objects.get(ledger_id="LED-900")
        self.assertEqual((real.status, real.is_test, real.matched_user_id), ("awaiting_credit", False, self.faculty.pk))

    def test_e2e_guards_stop_before_anything_is_stored(self):
        from django.core.management.base import CommandError

        with self.assertRaises(CommandError):
            self.run_cmd("test-sender-e2e", "--test-sender", TEST_SENDER, "--test-user-id", str(self.tester.pk))
        with self.assertRaises(CommandError):
            self.run_cmd("test-sender-e2e", "--test-sender", TEST_SENDER, "--folder", "Junk E-mail", "--uid", "7",
                         "--test-user-id", str(self.faculty.pk))
        with self.assertRaises(CommandError):
            self.run_cmd("test-sender-e2e", "--test-sender", TEST_SENDER, "--folder", "INBOX", "--uid", "7",
                         "--test-user-id", str(self.tester.pk))
        SubWallet.objects.filter(wallet__user=self.tester).delete()
        with self.assertRaises(CommandError):
            self.e2e()
        self.tester.emp_id = ""
        self.tester.save()
        with self.assertRaises(CommandError):
            self.e2e()
        self.assertFalse(SricWalletMailMessage.objects.exists())
        self.assertFalse(SricWalletRecharge.objects.exists())

    def test_test_employee_id_is_guarded_dry_run_then_apply_then_clear(self):
        from django.core.management.base import CommandError

        self.tester.emp_id = None
        self.tester.save()
        uid = str(self.tester.pk)
        out = self.run_cmd("test-employee-id-dry-run", "--test-user-id", uid, "--employee-id", "test-0001")
        self.assertIn("previous_empty=True previous_was_null=True collisions=0 unchanged=False applied=False now_set=False", out)
        self.tester.refresh_from_db()
        self.assertIsNone(self.tester.emp_id)
        out = self.run_cmd("test-employee-id-apply", "--test-user-id", uid, "--employee-id", "TEST-0001")
        self.assertIn("applied=True now_set=True now_matches_only_this_account=True", out)
        self.tester.refresh_from_db()
        self.assertEqual(self.tester.emp_id, "TEST-0001")
        self.assertIn("unchanged=True applied=False", self.run_cmd("test-employee-id-apply", "--test-user-id", uid, "--employee-id", "TEST-0001"))
        self.assertNotIn("TEST-0001", out)
        for args in (
            (str(self.faculty.pk), "TEST-0002"),
            (uid, "123456"),
            (uid, "TEST-0002"),
        ):
            with self.assertRaises(CommandError):
                self.run_cmd("test-employee-id-apply", "--test-user-id", args[0], "--employee-id", args[1])
        self.faculty.emp_id = " test-0003 "
        self.faculty.save()
        self.run_cmd("test-employee-id-apply", "--test-user-id", uid, "--employee-id", "")
        with self.assertRaises(CommandError):
            self.run_cmd("test-employee-id-dry-run", "--test-user-id", uid, "--employee-id", "TEST-0003")
        self.tester.refresh_from_db()
        self.assertIsNone(self.tester.emp_id)

    def test_reversal_link_marks_an_already_reversed_test_row(self):
        from django.core.management.base import CommandError

        self.e2e()
        rec = SricWalletRecharge.objects.get(is_test=True)
        with self.assertRaises(CommandError):
            self.run_cmd("test-reversal-link-dry-run", "--row-id", str(rec.pk))
        self.run_cmd("test-sender-reverse", "--row-id", str(rec.pk))
        SricWalletRecharge.objects.filter(pk=rec.pk).update(reversed_at=None, reversed_by=None, reversal_ref="")
        out = self.run_cmd("test-reversal-link-dry-run", "--row-id", str(rec.pk))
        self.assertIn("already_reversed=False applied=False direction_debit=True amount_matches=True", out)
        self.assertIn("now_reversed=False", out)
        self.assertIsNone(SricWalletRecharge.objects.get(pk=rec.pk).reversed_at)
        out = self.run_cmd("test-reversal-link-apply", "--row-id", str(rec.pk))
        self.assertIn("applied=True", out)
        self.assertIn("now_reversed=True reversal_ref=WAD-", out)
        self.assertIn("already_reversed=True applied=False", self.run_cmd("test-reversal-link-apply", "--row-id", str(rec.pk)))
        self.assertEqual(self.balance(user=self.tester), Decimal("250.00"))

    def test_auto_credit_switch_changes_only_that_setting(self):
        out = self.run_cmd("auto-credit-dry-run")
        self.assertIn("auto_credit before=False after=False applied=False", out)
        out = self.run_cmd("auto-credit-enable")
        self.assertIn("auto_credit before=False after=True applied=True scan_enabled=True sender_is_default=True", out)
        self.assertIn("applied=False", self.run_cmd("auto-credit-enable"))
        config = SricWalletRechargeSettings.get_singleton()
        self.assertEqual((config.auto_credit_enabled, config.sender_email, config.require_internal_relay), (True, SENDER, True))
        self.process(csv_text(self.ROW), sender=TEST_SENDER, hops=[FOREIGN_HOP], marker=0)
        self.process(csv_text("P-8,Prof. Test,123456,LED-901,IIC-000-002,100"), uid="21", hops=[FOREIGN_HOP], marker=0, message_id="<x@y>")
        self.assertEqual(list(SricWalletRecharge.objects.values_list("status", "review_reason")), [("needs_review", "origin_unverified")])
        self.assertEqual(self.balance(), Decimal("0.00"))
        self.assertIn("before=True after=False applied=True", self.run_cmd("auto-credit-disable"))

    def test_reverse_refuses_real_rows(self):
        from django.core.management.base import CommandError

        self.config.auto_credit_enabled = True
        self.config.save()
        self.process(csv_text(self.ROW), uid="20", message_id="<real@sric>")
        real = SricWalletRecharge.objects.get()
        self.assertEqual(real.status, "credited")
        with self.assertRaises(CommandError):
            self.run_cmd("test-sender-reverse", "--row-id", str(real.pk))
        self.assertEqual(self.balance(), Decimal("1500.00"))

    def test_configured_sender_and_bad_address_are_refused(self):
        from django.core.management.base import CommandError

        for bad in (SENDER, "not-an-address"):
            with self.assertRaises(CommandError):
                self.run_cmd("test-sender-dry-run", "--test-sender", bad)

    def test_normal_scan_ignores_the_test_sender(self):
        self.assertEqual(self.process(csv_text(self.ROW), sender=TEST_SENDER)["status"], "wrong_sender")
        self.assertFalse(SricWalletRecharge.objects.exists())


