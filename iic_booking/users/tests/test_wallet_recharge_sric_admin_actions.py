"""Cash-book matching cutoff, Main Admin soft delete and SRIC reminders on wallet recharge requests."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from io import StringIO

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.wallet import (
    WalletRechargeMode,
    WalletRechargeParseEntry,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
)
from iic_booking.users.models.wallet_sric_settings import WalletSricSettings
from iic_booking.users.wallet_recharge_admin_actions import CREDITED_BLOCK_MESSAGE
from iic_booking.users.wallet_recharge_import import (
    CashbookIndex,
    CashbookMatchError,
    link_cashbook_entry_to_request,
    match_pending_recharge_requests_to_parse_entries,
    split_cashbook_rows_by_cutoff,
)
from iic_booking.users.wallet_recharge_workflow import (
    RechargeAlreadyProcessed,
    approve_request,
    send_sric_approval_email,
)

User = get_user_model()
GRANT = "IIC-000-002"
BASE = "/api/admin/wallet-recharge-requests/"
SRIC = "sric.office@test.iitr.ac.in"
BILLS = "bills@test.iitr.ac.in"


class _Base(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(
            name="IIC Sric Admin Test", code="ISAT", department_type=DepartmentType.INTERNAL
        )
        self.faculty = User.objects.create_user(
            email="fac.sricadmin@test.iitr.ac.in",
            password="pass12345",
            name="Faculty Sric",
            user_type=UserType.FACULTY,
            department=self.dept,
            emp_id="E7001",
        )
        self.admin = User.objects.create_user(
            email="main.admin.sric@test.iitr.ac.in", password="pass12345", name="Main", user_type=UserType.ADMIN
        )
        self.finance = User.objects.create_user(
            email="aic.sric@test.iitr.ac.in",
            password="pass12345",
            name="AIC",
            user_type=UserType.FINANCE,
            department=self.dept,
        )
        s = WalletSricSettings.get_singleton()
        s.recipient_emails = SRIC
        s.bill_section_emails = BILLS
        s.project_grant_cc_emails = "accounts.cc@test.iitr.ac.in"
        s.save()
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _request(self, status=WalletRechargeRequestStatus.PENDING, amount="5000.00", **extra):
        wallet, _ = Wallet.objects.get_or_create(user=self.faculty)
        fields = {
            "user": self.faculty,
            "wallet": wallet,
            "department": self.dept,
            "amount": Decimal(amount),
            "status": status,
            "user_otp_verified": True,
            "employee_number": "E7001",
            "department_grant_code": GRANT,
            "project_grant_code": "PRJ-77",
        }
        fields.update(extra)
        return WalletRechargeRequest.objects.create(**fields)

    def _entry(self, receipt="R-1", dated=date(2026, 10, 2), amount="5,000.00", emp="E7001"):
        return WalletRechargeParseEntry.objects.create(
            receipt_no=receipt, dated=dated, emp_no=emp, amount=amount, credited_to_project_no=GRANT, name="X"
        )

    @staticmethod
    def _html(message):
        return next(body for body, mime in message.alternatives if mime == "text/html")


class CashbookCutoffTests(_Base):
    def test_default_cutoff_is_portal_launch(self):
        self.assertEqual(WalletSricSettings.get_singleton().cashbook_match_from_date, date(2026, 9, 30))

    def test_split_rows_counts_before_and_undated(self):
        rows = [{"dated": date(2026, 9, 29)}, {"dated": date(2026, 9, 30)}, {"dated": None}, {"dated": date(2026, 10, 1)}]
        kept, before, undated = split_cashbook_rows_by_cutoff(rows)
        self.assertEqual(([r["dated"] for r in kept], before, undated), ([date(2026, 9, 30), date(2026, 10, 1)], 1, 1))

    def test_pre_cutoff_and_undated_entries_never_flag_or_match(self):
        req = self._request()
        old = self._entry(receipt="R-OLD", dated=date(2026, 9, 29))
        self._entry(receipt="R-NODATE", dated=None)
        self.assertEqual(CashbookIndex().candidates_for(req), [])
        self.assertEqual(match_pending_recharge_requests_to_parse_entries(), (0, []))
        with self.assertRaisesMessage(CashbookMatchError, "before 30-09-2026"):
            link_cashbook_entry_to_request(req.pk, old.pk)
        req.refresh_from_db()
        self.assertEqual(req.status, WalletRechargeRequestStatus.PENDING)

        res = self.client.get(BASE, {"cashbook": "received"})
        self.assertEqual(res.json()["results"], [])

        on_day = self._entry(receipt="R-NEW", dated=date(2026, 9, 30))
        self.assertEqual([c["entry"].pk for c in CashbookIndex().candidates_for(req)], [on_day.pk])
        self.assertEqual(match_pending_recharge_requests_to_parse_entries()[0], 1)

    def test_cutoff_setting_is_main_admin_configurable(self):
        req = self._request()
        old = self._entry(receipt="R-OLD", dated=date(2026, 9, 20))
        res = self.client.patch(
            "/api/admin/wallet-sric-settings/1/", {"cashbook_match_from_date": "2026-09-15"}, format="json"
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["cashbook_match_from_date"], "2026-09-15")
        self.assertEqual([c["entry"].pk for c in CashbookIndex().candidates_for(req)], [old.pk])

        dept_admin = User.objects.create_user(
            email="dept.admin.sric@test.iitr.ac.in",
            password="pass12345",
            name="DA",
            user_type=UserType.DEPT_ADMIN,
            department=self.dept,
        )
        client = APIClient()
        client.force_authenticate(dept_admin)
        client.patch("/api/admin/wallet-sric-settings/1/", {"cashbook_match_from_date": "2026-01-01"}, format="json")
        self.assertEqual(WalletSricSettings.get_singleton().cashbook_match_from_date, date(2026, 9, 15))

    def test_upload_ignores_pre_cutoff_rows_and_reports_count(self):
        User.objects.filter(pk=self.faculty.pk).update(emp_id="100777")
        req = self._request(amount="3000.00", employee_number="100777")
        txt = (
            "|Sl No|Dated|Receipt No|Credited to Project No.|Amount(Rs)|Payment Details|Received From|\n"
            "|1 |Sep 20, 2026|R-801|IIC-000-002|3,000.00|NEFT|PROF-TEST EMP NO-100777 DEPT-OF PHYSICS|\n"
            "|2 |Oct 03, 2026|R-802|IIC-000-002|3,000.00|NEFT|PROF-TEST EMP NO-100777 DEPT-OF PHYSICS|\n"
        )
        upload = SimpleUploadedFile("cashbook.txt", txt.encode(), content_type="text/plain")
        res = self.client.post(f"{BASE}cashbook-upload/", {"file": upload}, format="multipart")
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual((body["parsed"], body["stored"], body["ignored_before_cutoff"]), (2, 1, 1))
        self.assertEqual(body["cutoff_date"], "2026-09-30")
        self.assertFalse(WalletRechargeParseEntry.objects.filter(receipt_no="R-801").exists())
        req.refresh_from_db()
        self.assertEqual(req.cashbook_receipt_no, "R-802")

    def test_cleanup_command_reports_and_only_clears_unapproved_links(self):
        pending = self._request()
        self._entry(receipt="R-OLD", dated=date(2026, 9, 1))
        approved = self._request(
            status=WalletRechargeRequestStatus.APPROVED,
            amount="700.00",
            cashbook_receipt_no="R-A",
            cashbook_receipt_date=date(2026, 9, 2),
        )
        cancelled = self._request(
            status=WalletRechargeRequestStatus.CANCELLED,
            amount="800.00",
            cashbook_receipt_no="R-C",
            cashbook_receipt_date=date(2026, 9, 3),
        )
        out = StringIO()
        call_command("recharge_cashbook_cutoff_cleanup", stdout=out)
        text = out.getvalue()
        self.assertIn("mode=DRY RUN", text)
        self.assertIn("entries_before_cutoff=1", text)
        self.assertIn(f"entry_received_flags_cleared_ids={{'PENDING': [{pending.pk}]}}", text)
        self.assertIn("precutoff_links_on_approved_untouched=1", text)
        self.assertIn("precutoff_links_on_unapproved=1", text)
        cancelled.refresh_from_db()
        self.assertEqual(cancelled.cashbook_receipt_no, "R-C")

        call_command("recharge_cashbook_cutoff_cleanup", "--apply", stdout=StringIO())
        cancelled.refresh_from_db()
        approved.refresh_from_db()
        self.assertEqual(cancelled.cashbook_receipt_no, "")
        self.assertEqual(approved.cashbook_receipt_no, "R-A")


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class DeleteRequestTests(_Base):
    def _delete(self, req, client=None, **data):
        return (client or self.client).post(f"{BASE}{req.pk}/delete-request/", data, format="json")

    def test_only_main_admin_may_delete(self):
        req = self._request()
        finance = APIClient()
        finance.force_authenticate(self.finance)
        self.assertEqual(self._delete(req, finance, reason="dup").status_code, 403)
        req.refresh_from_db()
        self.assertFalse(req.is_deleted)

    def test_reason_required(self):
        req = self._request()
        res = self._delete(req, reason="  ")
        self.assertEqual(res.status_code, 400)

    def test_pending_delete_cancels_hides_and_audits(self):
        req = self._request()
        other = self._request(amount="900.00")
        res = self._delete(req, reason="Duplicate request")
        self.assertEqual(res.status_code, 200, res.content)
        req.refresh_from_db()
        self.assertTrue(req.is_deleted)
        self.assertEqual(req.status, WalletRechargeRequestStatus.CANCELLED)
        self.assertEqual(req.deleted_by, self.admin)
        self.assertEqual(req.deletion_reason, "Duplicate request")
        log = req.audit_logs.get(action="deleted")
        self.assertEqual((log.from_status, log.to_status, log.message), ("PENDING", "CANCELLED", "Duplicate request"))
        self.assertEqual(mail.outbox, [])

        ids = [r["id"] for r in self.client.get(BASE).json()["results"]]
        self.assertEqual(ids, [other.pk])
        ids = [r["id"] for r in self.client.get(BASE, {"status": "CANCELLED"}).json()["results"]]
        self.assertEqual(ids, [])
        shown = self.client.get(BASE, {"show_deleted": "1"}).json()["results"]
        self.assertEqual({r["id"] for r in shown}, {req.pk, other.pk})
        self.assertTrue(next(r for r in shown if r["id"] == req.pk)["is_deleted"])

        finance = APIClient()
        finance.force_authenticate(self.finance)
        ids = [r["id"] for r in finance.get(BASE, {"show_deleted": "1"}).json()["results"]]
        self.assertEqual(ids, [other.pk])

        owner = APIClient()
        owner.force_authenticate(self.faculty)
        mine = owner.get("/api/wallet/recharge-requests/")
        if mine.status_code == 200:
            self.assertNotIn(req.pk, [r["id"] for r in mine.json().get("requests", [])])

        with self.assertRaises(RechargeAlreadyProcessed):
            approve_request(req, actor_email="sric@test")
        self.assertEqual(self._delete(req, reason="again").status_code, 409)

    def test_inform_requester_optional(self):
        req = self._request()
        with self.captureOnCommitCallbacks(execute=True):
            self._delete(req, reason="Raised by mistake", inform_requester=True)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.faculty.email])
        self.assertIn("Raised by mistake", mail.outbox[0].body)
        self.assertIn(req.transaction_number, mail.outbox[0].subject)

    def test_credited_requests_are_blocked(self):
        approved = self._request(status=WalletRechargeRequestStatus.APPROVED, wallet_credited_at=timezone.now())
        declined_credit = self._request(
            status=WalletRechargeRequestStatus.CANCELLED, decline_credit_amount=Decimal("5000.00")
        )
        for req in (approved, declined_credit):
            res = self._delete(req, reason="cleanup")
            self.assertEqual(res.status_code, 409)
            self.assertEqual(res.json()["error"], CREDITED_BLOCK_MESSAGE)
            self.assertEqual(res.json()["code"], "CREDITED")
            req.refresh_from_db()
            self.assertFalse(req.is_deleted)
        row = self.client.get(f"{BASE}{approved.pk}/").json()
        self.assertEqual(row["delete_blocked_reason"], CREDITED_BLOCK_MESSAGE)

    def test_rejected_request_with_link_releases_cashbook_entry(self):
        entry = self._entry(receipt="R-REL")
        req = self._request(
            status=WalletRechargeRequestStatus.REJECTED,
            cashbook_parse_entry=entry,
            cashbook_receipt_no="R-REL",
            cashbook_receipt_date=entry.dated,
        )
        self.assertEqual(self._delete(req, reason="wrong link").status_code, 200)
        req.refresh_from_db()
        self.assertEqual((req.cashbook_receipt_no, req.cashbook_parse_entry_id), ("", None))
        fresh = self._request()
        self.assertEqual([c["entry"].pk for c in CashbookIndex().candidates_for(fresh)], [entry.pk])


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class SricReminderTests(_Base):
    def _send(self, req, client=None, **data):
        return (client or self.client).post(f"{BASE}{req.pk}/sric-reminder/", data, format="json")

    def test_preview_lists_recipients_and_full_body_without_sending(self):
        req = self._request()
        res = self.client.get(
            f"{BASE}{req.pk}/sric-reminder-preview/", {"note": "Kindly expedite", "extra_cc": "dean.cc@test.iitr.ac.in"}
        )
        self.assertEqual(res.status_code, 200, res.content)
        body = res.json()
        self.assertEqual(body["to"], [SRIC])
        self.assertEqual(body["cc"], [self.faculty.email, "dean.cc@test.iitr.ac.in"])
        self.assertEqual(body["reminder_number"], 1)
        self.assertTrue(body["eligible"])
        self.assertTrue(body["subject"].startswith(f"Reminder #1: [{req.transaction_number}]"))
        for needle in (req.transaction_number, "5,000.00", GRANT, "PRJ-77", "Kindly expedite", "Reminder #1"):
            self.assertIn(needle, body["html"])
        self.assertNotIn(req.action_token, body["html"])
        self.assertEqual(mail.outbox, [])

    def test_send_repeats_original_email_with_links_and_copies_requester(self):
        req = self._request()
        send_sric_approval_email(req)
        original = mail.outbox[0]
        mail.outbox = []
        req.refresh_from_db()

        res = self._send(req, note="Pending for two weeks", extra_cc=["dean.cc@test.iitr.ac.in"])
        self.assertEqual(res.status_code, 200, res.content)
        self.assertEqual(res.json()["reminder_number"], 1)
        approver, copy = mail.outbox
        self.assertEqual(approver.to, [SRIC])
        self.assertEqual(approver.subject, f"Reminder #1: {original.subject}")
        self.assertIn(req.action_token, self._html(approver))
        for line in original.body.split("Copy sent to")[0].splitlines():
            self.assertIn(line, approver.body)
        self.assertIn("Pending for two weeks", approver.body)
        self.assertIn("REMINDER #1", approver.body)

        self.assertEqual((copy.to, copy.cc), ([self.faculty.email], ["dean.cc@test.iitr.ac.in"]))
        self.assertNotIn(req.action_token, self._html(copy))
        self.assertNotIn(req.action_token, copy.body)
        self.assertIn(req.transaction_number, copy.body)

        req.refresh_from_db()
        self.assertEqual((req.sric_reminder_count, req.sric_reminder_last_sent_by), (1, self.admin))
        log = req.audit_logs.get(action="sric_reminder_sent")
        self.assertEqual(log.metadata["reminder_number"], 1)
        self.assertEqual(log.metadata["recipients"], [SRIC])

    def test_cooldown_blocks_double_send_then_numbers_next(self):
        req = self._request()
        self.assertEqual(self._send(req).status_code, 200)
        res = self._send(req)
        self.assertEqual(res.status_code, 429)
        self.assertGreater(res.json()["cooldown_seconds"], 0)
        self.assertEqual(len(mail.outbox), 2)
        WalletRechargeRequest.objects.filter(pk=req.pk).update(
            sric_reminder_last_sent_at=timezone.now() - timedelta(minutes=11)
        )
        res = self._send(req)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["reminder_number"], 2)
        self.assertTrue(mail.outbox[-2].subject.startswith("Reminder #2:"))

    def test_approved_not_received_has_no_action_links(self):
        req = self._request(
            status=WalletRechargeRequestStatus.APPROVED,
            recharge_mode=WalletRechargeMode.DIRECT_CASH_DEPOSIT,
            responded_at=timezone.now(),
        )
        self.assertEqual(self._send(req).status_code, 200)
        approver = mail.outbox[0]
        self.assertEqual(approver.to, [BILLS])
        self.assertNotIn(req.action_token, self._html(approver))
        self.assertIn("not yet appeared in the SRIC cash-book", approver.body)

    def test_not_eligible_or_not_main_admin(self):
        rejected = self._request(status=WalletRechargeRequestStatus.REJECTED)
        self.assertEqual(self._send(rejected).status_code, 400)
        received = self._request(
            status=WalletRechargeRequestStatus.APPROVED, amount="600.00", cashbook_receipt_no="R-9",
            cashbook_receipt_date=date(2026, 10, 1),
        )
        self.assertEqual(self._send(received).status_code, 400)
        pending = self._request(amount="650.00")
        finance = APIClient()
        finance.force_authenticate(self.finance)
        self.assertEqual(self._send(pending, finance).status_code, 403)
        self.assertEqual(self._send(pending, extra_cc="not-an-email").status_code, 400)
        self.assertEqual(mail.outbox, [])
