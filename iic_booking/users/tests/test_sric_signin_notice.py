"""SRIC recharges for faculty who have not signed in yet: they are emailed to sign in with Channel i.

All people, ids, ledgers and addresses are invented.
"""

from __future__ import annotations

from unittest import mock

from django.core import mail

from iic_booking.users import sric_signin_notice as notice
from iic_booking.users.models import UserType
from iic_booking.users.models.portal_migration import LegacyWalletAccountMapping
from iic_booking.users.models.sric_wallet_recharge import SricWalletRecharge
from iic_booking.users.tests.test_sric_wallet_recharge import IMAP, FakeImap, User, _World, csv_text, make_email
from iic_booking.users import sric_wallet_recharge as svc

NEW_FACULTY_ROW = "PRJ-7/26-27,Prof. New Faculty,0100684,LED-700,IIC-000-002,2500"


def _to(address):
    return [m for m in mail.outbox if address in m.to]


class SigninNoticeScanTests(_World):
    def scan(self, messages):
        with mock.patch.object(svc, "_imap_config", return_value=IMAP), mock.patch(
            "iic_booking.users.imap_fetch.connect_imap", return_value=FakeImap(messages)
        ), self.captureOnCommitCallbacks(execute=True):
            return svc.scan_mailbox()

    def setUp(self):
        super().setUp()
        LegacyWalletAccountMapping.objects.create(employee_id="100684", old_email="new.faculty@old.iitr.ac.in")

    def test_unmatched_employee_is_emailed_once_with_channel_i_sign_in_link(self):
        rows = csv_text(NEW_FACULTY_ROW, "PRJ-8/26-27,Prof. New Faculty,100684,LED-701,IIC-000-002,500")
        with mock.patch.object(notice, "_old_portal_email", return_value="") as old_portal:
            self.scan({"5": make_email(rows)})
            self.scan({"5": make_email(rows), "6": make_email(rows)})

        recs = list(SricWalletRecharge.objects.filter(status="needs_review").order_by("pk"))
        self.assertEqual([r.review_reason for r in recs], ["unmatched_employee", "unmatched_employee"])
        sent = _to("new.faculty@old.iitr.ac.in")
        self.assertEqual(len(sent), 1)
        message = sent[0]
        self.assertIn("Sign in to the new IIC portal", message.subject)
        self.assertIn(recs[0].reference, message.subject)
        self.assertIn(recs[1].reference, message.subject)
        self.assertIn("Channel i", message.body)
        self.assertIn("/login", message.body)
        self.assertIn("LED-700", message.body)
        self.assertIn("Sign in with Channel i", message.alternatives[0][0])
        for rec in recs:
            self.assertEqual(rec.signin_notice_email, "new.faculty@old.iitr.ac.in")
            self.assertIsNotNone(rec.signin_notice_sent_at)
            self.assertIn("signin_notice_sent", [h["action"] for h in rec.history])
        old_portal.assert_not_called()

    def test_matched_faculty_and_switched_off_setting_send_nothing(self):
        self.config.signin_notice_enabled = False
        self.config.save()
        self.scan({"5": make_email(csv_text(NEW_FACULTY_ROW))})
        self.assertFalse(_to("new.faculty@old.iitr.ac.in"))

        self.config.signin_notice_enabled = True
        self.config.save()
        mail.outbox.clear()
        self.scan({"7": make_email(csv_text("PRJ-9/26-27,Prof. Test,123456,LED-702,IIC-000-002,100"), message_id="<m7@sric.example>")})
        self.assertEqual(SricWalletRecharge.objects.get(ledger_id="LED-702").status, "credited")
        self.assertFalse([m for m in mail.outbox if "Sign in to the new IIC portal" in m.subject])

    def test_unknown_address_falls_back_to_old_portal_then_skips(self):
        LegacyWalletAccountMapping.objects.all().delete()
        with mock.patch.object(notice, "_old_portal_email", return_value="from.old.db@old.iitr.ac.in"):
            self.scan({"5": make_email(csv_text(NEW_FACULTY_ROW))})
        self.assertEqual(len(_to("from.old.db@old.iitr.ac.in")), 1)

        with mock.patch.object(notice, "_old_portal_email", return_value=""):
            self.scan({"8": make_email(csv_text("PRJ-1/26-27,Prof. Unknown,555000,LED-703,IIC-000-002,100"), message_id="<m8@sric.example>")})
        self.assertIsNone(SricWalletRecharge.objects.get(ledger_id="LED-703").signin_notice_sent_at)


class SigninNoticeRulesTests(_World):
    def test_account_never_signed_in_counts_but_signed_in_account_does_not(self):
        never = User.objects.create_user(
            email="never@test.iitr.ac.in", password="pass12345", name="Never", user_type=UserType.FACULTY, emp_id="424242"
        )
        rec = SricWalletRecharge(
            status="needs_review", review_reason="inactive_user", matched_user=never, employee_id="424242", ledger_id="L1"
        )
        self.assertTrue(notice.needs_signin_notice(rec))
        self.assertEqual(notice.find_notice_address(rec), ("never@test.iitr.ac.in", notice.SOURCE_PORTAL_ACCOUNT))

        never.last_login = never.date_joined
        self.assertFalse(notice.needs_signin_notice(rec))
        self.assertFalse(notice.needs_signin_notice(SricWalletRecharge(status="needs_review", review_reason="over_auto_limit")))
        self.assertFalse(
            notice.needs_signin_notice(SricWalletRecharge(status="needs_review", review_reason="unmatched_employee", is_test=True))
        )


class SigninNoticeApiTests(_World):
    def setUp(self):
        super().setUp()
        self.config.signin_notice_enabled = False
        self.config.save()
        with mock.patch.object(notice, "_old_portal_email", return_value=""):
            self.process(csv_text(NEW_FACULTY_ROW))
        self.rec = SricWalletRecharge.objects.get(ledger_id="LED-700")

    def test_admin_sends_to_entered_address_when_none_is_known(self):
        client = self.admin_client()
        url = f"/api/admin/sric-wallet-recharges/{self.rec.pk}/signin-notice/"
        listed = client.get("/api/admin/sric-wallet-recharges/").json()
        row = next(r for r in listed["results"] if r["id"] == self.rec.pk)
        self.assertTrue(row["can_send_signin_notice"])

        with mock.patch.object(notice, "_old_portal_email", return_value=""):
            missing = client.post(url, {}, format="json")
        self.assertEqual((missing.status_code, missing.json()["code"]), (400, "EMAIL_REQUIRED"))
        self.assertEqual(client.post(url, {"email": "not-an-email"}, format="json").json()["code"], "INVALID_EMAIL")

        sent = client.post(url, {"email": "typed@iitr.ac.in"}, format="json")
        self.assertEqual(sent.status_code, 200, sent.content)
        self.assertEqual(sent.json()["row"]["signin_notice_email"], "typed@iitr.ac.in")
        self.assertEqual(len(_to("typed@iitr.ac.in")), 1)

        faculty = self.admin_client()
        faculty.force_authenticate(self.faculty)
        self.assertEqual(faculty.post(url, {"email": "x@iitr.ac.in"}, format="json").status_code, 403)

    def test_rows_not_waiting_for_sign_in_are_refused(self):
        credited = SricWalletRecharge.objects.create(
            status="credited", ledger_id="LED-999", financial_year="2026-27", employee_id="123456"
        )
        response = self.admin_client().post(
            f"/api/admin/sric-wallet-recharges/{credited.pk}/signin-notice/", {"email": "a@iitr.ac.in"}, format="json"
        )
        self.assertEqual((response.status_code, response.json()["code"]), (409, "NOT_WAITING_FOR_SIGNIN"))
