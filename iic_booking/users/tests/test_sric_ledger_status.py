"""SRIC "Ledger Request Status" emails: read from the HTML table, compared with the wallet credit, and never moving money."""

from __future__ import annotations

import email
from datetime import date, datetime, timedelta
from decimal import Decimal
from email.message import EmailMessage
from email.utils import format_datetime
from unittest import mock

from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.users import sric_ledger_status as ledger
from iic_booking.users import sric_wallet_recharge as svc
from iic_booking.users.models import User, UserType
from iic_booking.users.models.payment import DepartmentPaymentReceipt
from iic_booking.users.models.sric_wallet_recharge import SricLedgerStatusEmail, SricWalletMailMessage, SricWalletRecharge
from iic_booking.users.models.wallet import SubWalletTransaction, Wallet
from iic_booking.users.models.wallet_sric_settings import WalletSricSettings, wallet_mode_flags
from iic_booking.users.tests.test_sric_wallet_recharge import IMAP, INTERNAL_HOPS, PUBLIC_HOP, SENDER, FakeImap, _World, csv_text

LEDGER, PROJECT = "LED10000000000001", "TST-1000-ABC/26-27"


def status_email(*, ledger_id=LEDGER, amount="Dr. 5000", party="IIC-000-002", project=PROJECT, status="Approved", hops=None, message_id="<ledger1@sric.example>") -> bytes:
    msg = EmailMessage()
    for hop in hops if hops is not None else INTERNAL_HOPS:
        msg["Received"] = hop
    msg["From"] = f'"SRIC, IIT Roorkee" <{SENDER}>'
    msg["To"] = '"Test Faculty" <fac.one@test.iitr.ac.in>'
    msg["Cc"] = "portal@test.iitr.ac.in"
    msg["Return-Path"] = f"<{SENDER}>"
    msg["Subject"] = f"Ledger Request Status: {status}"
    msg["Message-ID"] = message_id
    msg["Date"] = format_datetime(timezone.make_aware(datetime(2026, 10, 30, 14, 40)))
    msg["mail_from_trusted_domains"] = "True"
    cells = [("Ledger ID:", ledger_id), ("Amount(Rs):", f"<b><span style='color:red'>{amount}</span></b>"), ("Bill Token No.:", "N/A"), ("Bank Account No:", "N/A"),
             ("Bank IFSC Code:", "N/A"), ("Party Name:", party), ("Project No:", project)]
    rows = "".join(f"<tr><td>{a}</td><td>{b}</td></tr>" for a, b in cells)
    msg.set_content("See the HTML part.")
    msg.add_alternative(f"<html><body><p>Dear Prof. Test</p><table><tr><th>Particulars</th><th>Details</th></tr>{rows}</table>"
                        f"<p><b>Bank credit may appear in your account within 2 or 3 working days.</b></p><p>Warm Regards SRIC Office</p></body></html>", subtype="html")
    return msg.as_bytes()


class ParseTests(_World):
    def test_reads_every_field_from_the_html_table(self):
        parsed = ledger.parse(email.message_from_bytes(status_email()))
        self.assertEqual((parsed.status, parsed.ledger_id, parsed.direction, parsed.amount, parsed.party_code, parsed.project_number, parsed.bill_token),
                         ("Approved", LEDGER, "Dr", Decimal("5000"), "IIC-000-002", PROJECT, "N/A"))

    def test_amount_with_commas_and_paise(self):
        self.assertEqual(ledger.parse(email.message_from_bytes(status_email(amount="Dr. 1,00,000.50"))).amount, Decimal("100000.50"))

    def test_no_ledger_id_is_not_guessed(self):
        self.assertIsNone(ledger.parse(email.message_from_bytes(status_email(ledger_id=""))))


class _Credited(_World):
    def setUp(self):
        super().setUp()
        self.process(csv_text(f"{PROJECT},Prof. Test,123456,{LEDGER},IIC-000-002,5000"), uid="1")
        self.row = SricWalletRecharge.objects.get(ledger_id=LEDGER)
        self.assertEqual(self.row.status, "credited")
        self.txns = SubWalletTransaction.objects.count()

    def status(self, uid="2", **kw):
        return svc.process_message(status_email(**kw), uid=uid, folder="INBOX", config=self.config)


class StatusEmailTests(_Credited):
    def test_a_matching_approval_settles_the_row_and_moves_no_money(self):
        balance = self.balance()
        summary = self.status()
        self.assertEqual((summary["status"], summary["ledger_result"]), ("ledger_status", "settled"))
        self.row.refresh_from_db()
        self.assertTrue(self.row.fund_receipt_verified)
        self.assertIn("SRIC ledger approval email", self.row.fund_receipt_verification_remarks)
        self.assertEqual(self.balance(), balance)
        self.assertEqual(SubWalletTransaction.objects.count(), self.txns)

    def test_no_could_not_be_read_alert(self):
        fake = FakeImap({"2": status_email()})
        SricWalletMailMessage.objects.all().update(folder="OLD")
        mail.outbox.clear()
        with mock.patch.object(svc, "_imap_config", return_value=IMAP), mock.patch("iic_booking.users.imap_fetch.connect_imap", return_value=fake):
            result = svc.scan_mailbox(trigger="schedule")
        self.assertEqual(result["ledger_problem_ids"], [])
        self.assertFalse([m for m in mail.outbox if "could not be read" in m.subject])
        self.assertEqual(fake.flag_changes, 0)

    def test_each_difference_stops_settlement(self):
        for name, kw in (("amount", {"amount": "Dr. 6000"}), ("receiver", {"party": "TINK-000-01"}), ("project", {"project": "OTHER/26-27"}), ("direction", {"amount": "Cr. 5000"})):
            with self.subTest(name):
                SricLedgerStatusEmail.objects.all().delete()
                SricWalletMailMessage.objects.exclude(uid="1").delete()
                summary = self.status(message_id=f"<{name}@sric.example>", **kw)
                self.assertEqual(summary["ledger_result"], "mismatch")
                self.assertIn(name, SricLedgerStatusEmail.objects.get().detail)
                self.row.refresh_from_db()
                self.assertFalse(self.row.fund_receipt_verified)

    def test_a_status_other_than_approved_settles_nothing_and_alerts(self):
        fake = FakeImap({"2": status_email(status="Rejected")})
        SricWalletMailMessage.objects.all().update(folder="OLD")
        mail.outbox.clear()
        with mock.patch.object(svc, "_imap_config", return_value=IMAP), mock.patch("iic_booking.users.imap_fetch.connect_imap", return_value=fake):
            svc.scan_mailbox(trigger="schedule")
        self.row.refresh_from_db()
        self.assertFalse(self.row.fund_receipt_verified)
        self.assertEqual(self.row.status, "credited")
        self.assertTrue([m for m in mail.outbox if "ledger status" in m.subject])

    def test_unverified_origin_settles_nothing(self):
        summary = self.status(hops=[*INTERNAL_HOPS, PUBLIC_HOP])
        self.assertEqual(summary["ledger_result"], "origin_unverified")
        self.row.refresh_from_db()
        self.assertFalse(self.row.fund_receipt_verified)

    def test_a_reversed_credit_is_not_settled(self):
        SricWalletRecharge.objects.filter(pk=self.row.pk).update(reversed_at=timezone.now())
        self.assertEqual(self.status()["ledger_result"], "not_credited")

    def test_approval_with_no_row_credits_nobody(self):
        summary = self.status(ledger_id="LED99999999999999")
        self.assertEqual(summary["ledger_result"], "no_wallet_credit")
        self.assertEqual(SubWalletTransaction.objects.count(), self.txns)

    def test_ordinary_email_with_no_attachment_still_alerts(self):
        msg = EmailMessage()
        for hop in INTERNAL_HOPS:
            msg["Received"] = hop
        msg["From"], msg["Subject"], msg["Message-ID"] = SENDER, "Something else", "<other@sric.example>"
        msg["Date"] = format_datetime(timezone.make_aware(datetime(2026, 10, 30, 14, 40)))
        msg.set_content("hello")
        summary = svc.process_message(msg.as_bytes(), uid="3", folder="INBOX", config=self.config)
        self.assertEqual(summary["status"], "no_attachment")


class ApprovalBeforeCreditTests(_World):
    def test_an_approval_that_arrives_first_settles_the_row_when_it_is_credited(self):
        self.assertEqual(svc.process_message(status_email(), uid="2", folder="INBOX", config=self.config)["ledger_result"], "no_wallet_credit")
        self.process(csv_text(f"{PROJECT},Prof. Test,123456,{LEDGER},IIC-000-002,5000"), uid="3")
        row = SricWalletRecharge.objects.get(ledger_id=LEDGER)
        self.assertTrue(row.fund_receipt_verified)
        self.assertEqual(SricLedgerStatusEmail.objects.get().result, "settled")


class BackfillTests(_Credited):
    def setUp(self):
        super().setUp()
        self.raw = status_email()
        self.record_row = SricWalletMailMessage.objects.create(folder="INBOX", uid="9", message_id="<ledger1@sric.example>", status="no_attachment",
                                                               received_at=timezone.make_aware(datetime(2026, 10, 30, 14, 40)), from_addr=SENDER)

    def run_it(self, apply):
        fake = FakeImap({"9": self.raw})
        with mock.patch.object(svc, "_imap_config", return_value=IMAP), mock.patch("iic_booking.users.imap_fetch.connect_imap", return_value=fake):
            result = ledger.reconcile_mailbox(since=date(2026, 10, 1), apply=apply)
        self.assertEqual(fake.flag_changes, 0)
        return result

    def test_dry_run_changes_nothing(self):
        result = self.run_it(False)
        self.assertEqual(result["results"], {"settled": 1})
        self.record_row.refresh_from_db()
        self.row.refresh_from_db()
        self.assertEqual(self.record_row.status, "no_attachment")
        self.assertFalse(self.row.fund_receipt_verified)
        self.assertFalse(SricLedgerStatusEmail.objects.exists())

    def test_apply_settles_once_and_can_be_repeated(self):
        balance = self.balance()
        result = self.run_it(True)
        self.assertEqual((result["results"], result["credited_rows_verified"], result["credited_rows_awaiting_sric"]), ({"settled": 1}, 1, []))
        self.assertEqual(self.run_it(True)["emails_checked"], 0)
        self.assertEqual(SricLedgerStatusEmail.objects.count(), 1)
        self.assertEqual(self.balance(), balance)
        self.assertEqual(SubWalletTransaction.objects.count(), self.txns)

    def test_a_different_email_at_the_same_number_is_skipped(self):
        self.raw = status_email(message_id="<another@sric.example>")
        result = self.run_it(True)
        self.assertEqual((result["emails_checked"], result["not_status_emails"]), (0, 1))


class OverdueAlertTests(_Credited):
    def setUp(self):
        super().setUp()
        self.old = timezone.now() - timedelta(days=16)
        SricWalletRecharge.objects.filter(pk=self.row.pk).update(credited_at=self.old)
        self.finance = User.objects.create_user(email="accounts@test.iitr.ac.in", password="pass12345", user_type=UserType.FINANCE, email_verified=True, admin_approved=True)
        mail.outbox.clear()

    def test_alerts_accounts_and_main_admin_once_per_period(self):
        self.assertEqual(ledger.send_overdue_settlement_alert()["overdue"], 1)
        to = {m.to[0] for m in mail.outbox}
        self.assertIn(self.finance.email, to)
        self.assertIn(self.admin.email, to)
        self.assertNotIn(self.faculty.email, to)
        self.assertEqual(ledger.send_overdue_settlement_alert()["overdue"], 0)
        self.assertEqual(ledger.send_overdue_settlement_alert(now=timezone.now() + timedelta(days=16))["overdue"], 1)

    def test_the_period_can_be_changed(self):
        config = WalletSricSettings.get_singleton()
        config.fund_receipt_overdue_days = 30
        config.save()
        self.assertEqual(ledger.send_overdue_settlement_alert()["overdue"], 0)

    def test_settled_reversed_and_recent_rows_are_left_alone(self):
        SricWalletRecharge.objects.filter(pk=self.row.pk).update(fund_receipt_verified=True)
        self.assertEqual(ledger.send_overdue_settlement_alert()["overdue"], 0)
        SricWalletRecharge.objects.filter(pk=self.row.pk).update(fund_receipt_verified=False, reversed_at=timezone.now())
        self.assertEqual(ledger.send_overdue_settlement_alert()["overdue"], 0)
        SricWalletRecharge.objects.filter(pk=self.row.pk).update(reversed_at=None, credited_at=timezone.now())
        self.assertEqual(ledger.send_overdue_settlement_alert()["overdue"], 0)
        self.assertFalse(mail.outbox)


class RechargeOptionsTests(_World):
    """Faculty: SRIC portal + online. Government and R&D: credit against a letter + online. Everyone else: online. With the
    switches set for this (Direct Cash off, online on) nobody can recharge by cash or bank reference."""

    TYPES = (UserType.FACULTY, UserType.STUDENT, UserType.STARTUP_INCUBATED_IITR, UserType.EXTERNAL, UserType.RND, UserType.INSTITUTE, UserType.OTHER, UserType.EXTERNAL_STARTUP_MSME)

    def setUp(self):
        super().setUp()
        config = WalletSricSettings.get_singleton()
        config.direct_cash_recharge_enabled = False
        config.online_gateway_recharge_enabled = True
        config.save()

    def user(self, user_type):
        return User.objects.create_user(email=f"{user_type.lower()}.opt@test.iitr.ac.in", password="pass12345", email_verified=True, admin_approved=True, user_type=user_type)

    def test_flags_match_the_table(self):
        for user_type in self.TYPES:
            with self.subTest(user_type):
                flags = wallet_mode_flags(self.user(user_type))
                self.assertTrue(flags["online_gateway_recharge_enabled"])
                self.assertFalse(flags["direct_cash_recharge_enabled"])
                self.assertFalse(flags["project_grant_recharge_enabled"])
                self.assertFalse(flags["credit_facility_enabled"])

    def test_only_government_and_rnd_users_can_ask_for_credit_against_a_letter(self):
        from iic_booking.external_services.govt_credit import is_govt_user

        self.assertEqual({t for t in self.TYPES if is_govt_user(self.user(t))}, {UserType.RND})

    def test_a_bank_reference_cannot_recharge_while_direct_cash_is_off(self):
        Wallet.objects.get_or_create(user=self.faculty)
        client = APIClient()
        client.force_authenticate(self.faculty)
        with mock.patch("iic_booking.users.api.payment_views.resolve_internal_department_for_wallet_recharge", return_value=self.iic):
            answer = client.post("/api/payments/utr/submit/", {"utr_reference": "UTR1", "amount": "100", "purpose": "WALLET_RECHARGE", "department_id": self.iic.pk}, format="json")
        self.assertEqual((answer.status_code, answer.json().get("code")), (403, "direct_cash_recharge_disabled"))
        self.assertFalse(DepartmentPaymentReceipt.objects.exists())
