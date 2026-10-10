"""send_announcement: faculty selection, dry run, test copy, CC, attachment and the idempotent sent log."""

from __future__ import annotations

from io import StringIO
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.core.management import CommandError
from django.core.management import call_command
from django.test import TestCase
from django.test import override_settings

from iic_booking.communication.announcements import WALLET_RECHARGE_2026_10 as CAMPAIGN
from iic_booking.communication.announcements import AnnouncementError
from iic_booking.communication.announcements import SendOptions
from iic_booking.communication.announcements import log_key
from iic_booking.communication.announcements import parse_addresses
from iic_booking.communication.announcements import render
from iic_booking.communication.announcements import select_faculty
from iic_booking.communication.announcements import send_campaign
from iic_booking.communication.models import CommunicationLog
from iic_booking.users.models import User
from iic_booking.users.models import UserType
from iic_booking.users.models.wallet_sric_settings import WalletSricSettings

SLUG = CAMPAIGN.slug


def _user(email: str, user_type=UserType.FACULTY, is_active=True, **extra) -> User:
    user = User.objects.create_user(
        email=email,
        password="pass12345",
        name=extra.pop("name", "Asha Rao"),
        user_type=user_type,
        admin_approved=is_active,
        **extra,
    )
    assert user.is_active == is_active
    return user


def _run(*args) -> str:
    out = StringIO()
    call_command("send_announcement", SLUG, "--pause", "0", *args, stdout=out)
    return out.getvalue()


@override_settings(
    FRONTEND_URL="https://equip.iitr.ac.in",
    DEFAULT_FROM_EMAIL="IIT Roorkee <no-reply@iicbooking.iitr.ac.in>",
)
class SendAnnouncementTests(TestCase):
    def setUp(self):
        cache.clear()
        self.f1 = _user("asha.rao@ce.iitr.ac.in", name="Asha Rao")
        self.f2 = _user("vikram@iitr.ac.in", name="Vikram Singh")
        _user("old.prof@iitr.ac.in", is_active=False)
        _user("tester@iitr.ac.in", is_test_account=True)
        _user("visiting@gmail.com")
        _user("student1@iitr.ac.in", user_type=UserType.STUDENT)
        _user("operator@iitr.ac.in", user_type=UserType.OPERATOR)

    def test_selects_only_active_iitr_faculty(self):
        selection = select_faculty()
        self.assertEqual(
            [u.pk for u in selection["recipients"]], [self.f1.pk, self.f2.pk]
        )
        self.assertEqual(selection["excluded"]["inactive"], 1)
        self.assertEqual(selection["excluded"]["test_account"], 1)
        self.assertEqual(selection["excluded"]["not_iitr_email"], 1)

    def test_dry_run_sends_nothing_and_masks_addresses(self):
        out = _run("--cc", "head.iic@iitr.ac.in")
        self.assertEqual(len(mail.outbox), 0)
        self.assertIn('"to_send_now": 2', out)
        self.assertIn('"copies_per_cc_address": 2', out)
        self.assertIn("a***@ce.iitr.ac.in", out)
        self.assertNotIn("asha.rao@", out)
        self.assertNotIn("head.iic@", out)
        self.assertIn("DRY RUN", out)

    def test_test_copy_goes_to_one_address_without_cc_by_default(self):
        _run("--test-to", "reviewer@iitr.ac.in", "--cc", "head.iic@iitr.ac.in")
        self.assertEqual(len(mail.outbox), 1)
        msg = mail.outbox[0]
        self.assertEqual(msg.to, ["reviewer@iitr.ac.in"])
        self.assertEqual(msg.cc, [])
        self.assertTrue(msg.subject.startswith("[TEST] "))
        self.assertFalse(CommunicationLog.objects.exists())

        _run(
            "--test-to",
            "reviewer@iitr.ac.in",
            "--cc",
            "head.iic@iitr.ac.in",
            "--test-include-cc",
        )
        self.assertEqual(mail.outbox[1].cc, ["head.iic@iitr.ac.in"])

    def test_send_requires_confirm(self):
        with self.assertRaises(CommandError):
            _run("--send")
        with self.assertRaises(CommandError):
            _run("--send", "--confirm", "yes")
        self.assertEqual(len(mail.outbox), 0)

    def test_send_one_email_per_faculty_with_cc_and_pdf_then_skip_on_rerun(self):
        _run(
            "--send",
            "--confirm",
            SLUG,
            "--cc",
            "head.iic@iitr.ac.in",
            "--bcc",
            "archive@iitr.ac.in",
            "--reply-to",
            "iicbooking@iitr.ac.in",
            "--batch-size",
            "1",
        )
        self.assertEqual(len(mail.outbox), 2)
        first = mail.outbox[0]
        self.assertEqual(first.to, ["asha.rao@ce.iitr.ac.in"])
        self.assertEqual(first.cc, ["head.iic@iitr.ac.in"])
        self.assertEqual(first.bcc, ["archive@iitr.ac.in"])
        self.assertEqual(first.reply_to, ["iicbooking@iitr.ac.in"])
        self.assertEqual(first.subject, CAMPAIGN.subject)
        html = first.alternatives[0][0]
        self.assertIn("Asha Rao", html)
        self.assertIn("New Wallet Recharge", html)
        self.assertEqual(
            [a[0] for a in first.attachments], ["Wallet_Recharge_Guide.pdf"]
        )
        self.assertEqual(first.attachments[0][2], "application/pdf")
        self.assertEqual(
            CommunicationLog.objects.filter(
                provider_message_id=log_key(CAMPAIGN, self.f1.pk), status="sent"
            ).count(),
            1,
        )

        out = _run("--send", "--confirm", SLUG)
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn('"already_sent": 2', out)
        self.assertIn('"to_send_now": 0', out)

    def test_failure_is_logged_and_retried_on_next_run(self):
        real_send = mail.EmailMultiAlternatives.send

        def flaky(message, *args, **kwargs):
            if message.to == ["vikram@iitr.ac.in"]:
                raise ConnectionError("smtp down")
            return real_send(message, *args, **kwargs)

        with mock.patch.object(mail.EmailMultiAlternatives, "send", flaky):
            _run("--send", "--confirm", SLUG)
        self.assertEqual(len(mail.outbox), 1)
        row = CommunicationLog.objects.get(
            provider_message_id=log_key(CAMPAIGN, self.f2.pk)
        )
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.error_message, "ConnectionError")

        _run("--send", "--confirm", SLUG)
        self.assertEqual(
            [m.to for m in mail.outbox],
            [["asha.rao@ce.iitr.ac.in"], ["vikram@iitr.ac.in"]],
        )
        row.refresh_from_db()
        self.assertEqual(row.status, "sent")

    def test_limit_sends_a_first_tranche(self):
        _run("--send", "--confirm", SLUG, "--limit", "1")
        self.assertEqual(len(mail.outbox), 1)
        _run("--send", "--confirm", SLUG)
        self.assertEqual(len(mail.outbox), 2)

    def test_concurrent_run_is_refused(self):
        cache.add(f"announcement:{SLUG}:lock", "1", 60)
        with self.assertRaises(AnnouncementError):
            send_campaign(
                CAMPAIGN,
                SendOptions(cc=[], bcc=[], reply_to=[], pause_seconds=0),
                out=lambda s: None,
            )
        self.assertEqual(len(mail.outbox), 0)

    def test_cc_tokens_expand_from_sric_settings(self):
        s = WalletSricSettings.get_singleton()
        s.dean_sric_emails = "dean.sric@iitr.ac.in"
        s.ar_sric_emails = "ar.sric@iitr.ac.in, dean.sric@iitr.ac.in"
        s.save()
        self.assertEqual(
            parse_addresses("dean_sric; ar_sric head.iic@iitr.ac.in"),
            ["dean.sric@iitr.ac.in", "ar.sric@iitr.ac.in", "head.iic@iitr.ac.in"],
        )
        with self.assertRaises(AnnouncementError):
            parse_addresses("not-an-address")

    def test_rendered_email_has_every_section_and_no_unfilled_placeholders(self):
        text, html = render(
            CAMPAIGN, recipient_name="Prof. Asha Rao", has_attachment=True
        )
        for needle in (
            "Dear Prof. Asha Rao",
            "Ledger",
            "New Wallet Recharge",
            "Receiver Type",
            "Submit Recharge",
            "Direct Cash Deposit / Bank Transfer",
            "Send OTP",
            "Verify & Submit",
            "Transaction ID",
            "SRIC Bill Section",
            "supervisor",
            "30 September 2026",
            "https://equip.iitr.ac.in/wallet/recharge-from-project",
            "https://rnd.iitr.ac.in",
            "iicbooking@iitr.ac.in",
            "Head, Institute Instrumentation Centre",
            "attached PDF guide",
        ):
            self.assertIn(needle, text)
        self.assertIn("Verify &amp; Submit", html)
        self.assertNotIn("{{", html + text)
        self.assertNotIn("{%", html + text)
