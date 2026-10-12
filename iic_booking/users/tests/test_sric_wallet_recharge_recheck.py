"""SRIC Wallet_Recharge.csv: internal-only mail without the gateway marker, and rechecking rows waiting for review.

All people, ids, ledgers and hosts are invented.
"""

from __future__ import annotations

import email as email_lib
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core import mail
from django.core.management import call_command

from iic_booking.users import sric_wallet_recharge as svc
from iic_booking.users.models import UserType
from iic_booking.users.models.sric_wallet_recharge import SricWalletMailMessage, SricWalletRecharge
from iic_booking.users.models.wallet import SubWalletTransaction
from iic_booking.users.sric_wallet_mail_auth import AuthResult, check_message
from iic_booking.users.tests.test_sric_wallet_recharge import (
    IMAP,
    INTERNAL_HOPS,
    PUBLIC_HOP,
    FakeImap,
    User,
    _World,
    csv_text,
    make_email,
)

SRIC_PORTAL_HOPS = [
    "from store1.mail.local (store1.mail.local) by store1.mail.local with LMTP",
    "from mta1 (mta1 [10.1.2.3]) by store1.mail.local with ESMTP",
    "from gw1.mail.local (gw1.mail.local [10.1.2.4]) by mta1 with ESMTP",
    "from esa1.iitr.ac.in (esa1.iitr.ac.in [10.1.2.5]) by gw1.mail.local with ESMTP",
    "from rndportal (rndportal [10.9.8.7]) by esa1.iitr.ac.in with ESMTP/TLS/TLS_AES_256_GCM_SHA384",
]
IRONPORT = {"X-IronPort-AV": "E=Sophos;i=6.00", "X-ThreatScanner-Verdict": "Negative"}


def sric_portal_email(content: str, **kw) -> bytes:
    """Shaped like the SRIC portal's mail: internal relays only, gateway scan headers, no trusted-domain marker."""
    return make_email(content, hops=SRIC_PORTAL_HOPS, marker=0, extra_headers=IRONPORT, **kw)


class InternalOnlyMailTests(_World):
    def test_sric_portal_mail_without_marker_is_verified_and_credited(self):
        auth = check_message(email_lib.message_from_bytes(sric_portal_email(csv_text())), self.config)
        self.assertTrue(auth.authenticated, auth.verdict)
        with self.captureOnCommitCallbacks(execute=True):
            svc.process_message(
                sric_portal_email(csv_text("PRJ-1/26-27,Prof. Test,123456,LED-900,IIC-000-002,5000")),
                uid="1", folder="INBOX", config=self.config,
            )
        rec = SricWalletRecharge.objects.get()
        self.assertEqual((rec.status, rec.origin_verified), ("credited", True))
        self.assertEqual(self.balance(), Decimal("5000.00"))
        self.assertTrue([m for m in mail.outbox if "fac.one@test.iitr.ac.in" in m.to])

    def test_marker_still_rejected_when_repeated_or_altered_and_public_relay_still_fails(self):
        def auth(**kw):
            return check_message(email_lib.message_from_bytes(make_email(csv_text(), **kw)), self.config).authenticated

        self.assertTrue(auth(marker=0))
        self.assertFalse(auth(marker=2))
        self.assertFalse(auth(marker=0, extra_headers={"mail_from_trusted_domains": "False"}))
        self.assertFalse(auth(marker=0, hops=[*INTERNAL_HOPS, PUBLIC_HOP]))
        self.assertFalse(auth(marker=1, hops=[*INTERNAL_HOPS, PUBLIC_HOP]))
        self.assertFalse(auth(marker=0, sender="no-reply@sric.example.org"))


class RecheckTests(_World):
    def _store_unverified(self, raw: bytes, uid: str = "7"):
        with mock.patch.object(svc, "check_message", return_value=AuthResult(False, "old rule")):
            svc.process_message(raw, uid=uid, folder="INBOX", config=self.config)
        return SricWalletRecharge.objects.latest("pk")

    def _recheck(self, messages: dict[str, bytes], *, apply: bool):
        with mock.patch.object(svc, "_imap_config", return_value=IMAP), mock.patch(
            "iic_booking.users.imap_fetch.connect_imap", return_value=FakeImap(messages)
        ), self.captureOnCommitCallbacks(execute=True):
            return svc.recheck_with_mailbox(apply=apply)

    def test_origin_unverified_row_is_rechecked_and_credited_once(self):
        raw = sric_portal_email(csv_text("PRJ-2/26-27,Prof. Test,123456,LED-901,IIC-000-002,10000"))
        rec = self._store_unverified(raw)
        self.assertEqual((rec.status, rec.review_reason), ("needs_review", "origin_unverified"))

        dry = self._recheck({"7": raw}, apply=False)
        self.assertEqual((dry["checked"], dry["changed"], dry["rows"][0]["would_be"]), (1, 0, "credited"))
        self.assertEqual(SricWalletRecharge.objects.get(pk=rec.pk).status, "needs_review")
        self.assertEqual(self.balance(), Decimal("0.00"))

        mail.outbox.clear()
        result = self._recheck({"7": raw}, apply=True)
        rec.refresh_from_db()
        self.assertEqual((result["credited"], rec.status, rec.origin_verified, rec.review_reason), (1, "credited", True, ""))
        self.assertEqual(self.balance(), Decimal("10000.00"))
        self.assertIn("rechecked", [h["action"] for h in rec.history])
        self.assertTrue(SricWalletMailMessage.objects.get(pk=rec.message_id).authenticated)
        self.assertEqual(len([m for m in mail.outbox if "fac.one@test.iitr.ac.in" in m.to]), 1)

        again = self._recheck({"7": raw}, apply=True)
        self.assertEqual((again["checked"], again["credited"]), (0, 0))
        self.assertEqual(SubWalletTransaction.objects.count(), 1)

    def test_different_email_at_the_same_uid_is_not_trusted(self):
        raw = sric_portal_email(csv_text("PRJ-3/26-27,Prof. Test,123456,LED-902,IIC-000-002,700"))
        rec = self._store_unverified(raw)
        other = sric_portal_email(csv_text("PRJ-3/26-27,Prof. Test,123456,LED-902,IIC-000-002,70000"))
        result = self._recheck({"7": other}, apply=True)
        rec.refresh_from_db()
        self.assertEqual((result["rows"][0]["origin_rechecked"], rec.status), (False, "needs_review"))
        self.assertEqual(self.balance(), Decimal("0.00"))

    def test_public_relay_row_stays_in_review(self):
        raw = make_email(csv_text("P,Prof. Test,123456,LED-903,IIC-000-002,100"), hops=[*INTERNAL_HOPS, PUBLIC_HOP])
        rec = self._store_unverified(raw)
        self._recheck({"7": raw}, apply=True)
        rec.refresh_from_db()
        self.assertEqual((rec.status, rec.review_reason), ("needs_review", "origin_unverified"))

    def test_faculty_registered_after_the_email_is_credited_by_the_next_scheduled_scan(self):
        with self.captureOnCommitCallbacks(execute=True):
            svc.process_message(
                sric_portal_email(csv_text("PRJ-4/26-27,Prof. Late,0042424,LED-904,IIC-000-002,20000")),
                uid="1", folder="INBOX", config=self.config,
            )
        rec = SricWalletRecharge.objects.get()
        self.assertEqual((rec.status, rec.review_reason, rec.origin_verified), ("needs_review", "unmatched_employee", True))
        late = User.objects.create_user(
            email="late.fac@test.iitr.ac.in", password="pass12345", email_verified=True, admin_approved=True,
            name="Late Faculty", user_type=UserType.FACULTY, emp_id="42424",
        )
        with mock.patch.object(svc, "_imap_config", return_value=IMAP), mock.patch(
            "iic_booking.users.imap_fetch.connect_imap", return_value=FakeImap({})
        ), self.captureOnCommitCallbacks(execute=True):
            result = svc.scan_mailbox()
        rec.refresh_from_db()
        self.assertEqual((result["rechecked"], result["credited"], rec.status, rec.matched_user_id), (1, 1, "credited", late.pk))
        self.assertEqual(self.balance(user=late), Decimal("20000.00"))

    def test_command_dry_run_prints_ids_only_and_changes_nothing(self):
        raw = sric_portal_email(csv_text("PRJ-5/26-27,Prof. Test,123456,LED-905,IIC-000-002,600"))
        rec = self._store_unverified(raw)
        out = StringIO()
        with mock.patch.object(svc, "_imap_config", return_value=IMAP), mock.patch(
            "iic_booking.users.imap_fetch.connect_imap", return_value=FakeImap({"7": raw})
        ):
            call_command("sric_wallet_recharge", "recheck-review", stdout=out)
        self.assertIn(f"row_id={rec.pk}", out.getvalue())
        self.assertIn("would_be=credited", out.getvalue())
        self.assertNotIn("123456", out.getvalue())
        self.assertNotIn("Prof. Test", out.getvalue())
        self.assertEqual(SricWalletRecharge.objects.get(pk=rec.pk).status, "needs_review")


class UnreadableMailAlertTests(_World):
    def test_parse_error_alerts_main_admin(self):
        msgs = {"3": sric_portal_email("Foo,Bar\n1,2\n")}
        mail.outbox.clear()
        with mock.patch.object(svc, "_imap_config", return_value=IMAP), mock.patch(
            "iic_booking.users.imap_fetch.connect_imap", return_value=FakeImap(msgs)
        ), self.captureOnCommitCallbacks(execute=True):
            svc.scan_mailbox()
        self.assertEqual(SricWalletMailMessage.objects.get().status, "parse_error")
        alerts = [m for m in mail.outbox if "main.admin@test.iitr.ac.in" in m.to and "could not be read" in m.subject]
        self.assertEqual(len(alerts), 1)
        self.assertIn("Missing column", alerts[0].body)
