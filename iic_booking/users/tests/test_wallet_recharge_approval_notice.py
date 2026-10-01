"""Wallet recharge: "Project Already Closed" decline reason, approval notice to the configured offices
naming the approving email address, and requester details on Direct Transfer emails."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from iic_booking.communication.default_email_templates import get_default_email_templates
from iic_booking.communication.models import CommunicationTemplate
from iic_booking.communication.wallet_notifications import send_wallet_recharge_request_notifications
from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.test_account_email_settings import TestAccountEmailSettings
from iic_booking.users.models.wallet import (
    WalletRechargeMode,
    WalletRechargeParseEntry,
    WalletRechargeRejectionReason,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
)
from iic_booking.users.models.wallet_sric_settings import WalletCashbookMailboxMessage, WalletSricSettings
from iic_booking.users.wallet_recharge_import import link_cashbook_entry_to_request
from iic_booking.users.wallet_recharge_workflow import (
    notify_stakeholders_of_decision,
    reject_request,
    send_sric_approval_email,
    serialize_request_public,
)

User = get_user_model()
GRANT = "IIC-000-002"
SRIC_A = "sric.a@test.iitr.ac.in"
SRIC_B = "sric.b@test.iitr.ac.in"
BILLS = "bills@test.iitr.ac.in"
AR = "ar.sric@test.iitr.ac.in"
DEAN = "dean.sric@test.iitr.ac.in"
QA_INBOX = "qa.inbox@example.com"
ACTION_RE = re.compile(r"/wallet/recharge-action/([^/\s\"]+)/(approve|reject)")


def _html(message) -> str:
    return next((body for body, mime in message.alternatives if mime == "text/html"), "")


def _delivered(messages) -> set[str]:
    return {addr for m in messages for addr in (m.to + m.cc + m.bcc)}


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class RechargeApprovalNoticeTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(
            name="IIC Notice Test", code="INTS", department_type=DepartmentType.INTERNAL
        )
        self.faculty = User.objects.create_user(
            email="prof.notice@test.iitr.ac.in",
            password="pass12345",
            name="Prof. Supervisor",
            user_type=UserType.FACULTY,
            department=self.dept,
            emp_id="E5005",
            designation="Associate Professor",
        )
        self.faculty_wallet, _ = Wallet.objects.get_or_create(user=self.faculty)
        self.student = User.objects.create_user(
            email="student.notice@test.iitr.ac.in",
            password="pass12345",
            name="Riya Student",
            user_type=UserType.STUDENT,
            department=self.dept,
            emp_id="21112233",
        )
        WalletSricSettings.objects.update_or_create(
            pk=1,
            defaults={
                "recipient_emails": f"{SRIC_A}\n{SRIC_B}",
                "bill_section_emails": BILLS,
                "ar_sric_emails": AR,
                "dean_sric_emails": DEAN,
            },
        )

    def _request(self, user=None, mode=WalletRechargeMode.PROJECT_GRANT, amount="1000.00"):
        user = user or self.faculty
        return WalletRechargeRequest.objects.create(
            user=user,
            wallet=self.faculty_wallet,
            department=self.dept,
            amount=Decimal(amount),
            user_otp_verified=True,
            recharge_mode=mode,
            employee_number=user.emp_id,
            department_grant_code=GRANT,
            project_grant_code="PRJ-77" if mode == WalletRechargeMode.PROJECT_GRANT else "",
        )

    def _action_token(self, message, action="approve") -> str:
        return next(m.group(1) for m in ACTION_RE.finditer(message.body) if m.group(2) == action)

    def _decision_note(self, label="Approved by"):
        return next(m for m in mail.outbox if f"{label}:" in m.body and "Wallet recharge request" in m.body)

    # Decline reason -----------------------------------------------------------------------------

    def test_project_closed_reason_offered_and_shown_in_decline_email(self):
        req = self._request()
        values = [c["value"] for c in serialize_request_public(req)["rejection_reason_choices"]]
        self.assertEqual(values, ["wrong_project_grant", "insufficient_balance", "project_closed", "other"])

        resp = APIClient().post(
            f"/api/wallet/recharge-action/{req.action_token}/reject/",
            {"reason_code": "project_closed"},
            format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        req.refresh_from_db()
        self.assertEqual(req.rejection_reason_code, WalletRechargeRejectionReason.PROJECT_CLOSED)
        self.assertEqual(req.response_message, "Project Already Closed")
        faculty_mail = next(m for m in mail.outbox if self.faculty.email in m.to)
        self.assertIn("Project Already Closed", faculty_mail.body)

    def test_project_closed_on_plain_rejection_reaches_requester_and_offices(self):
        s = WalletSricSettings.get_singleton()
        s.decline_converts_to_credit = False
        s.save()
        declined = reject_request(
            self._request(), reason_code=WalletRechargeRejectionReason.PROJECT_CLOSED, actor_email=SRIC_B
        )
        self.assertEqual(declined.status, WalletRechargeRequestStatus.REJECTED)
        notify_stakeholders_of_decision(declined)
        self.assertTrue(any("Project Already Closed" in m.body for m in mail.outbox if self.faculty.email in m.to))
        note = self._decision_note("Declined by")
        self.assertIn("Decline reason: Project Already Closed", note.body)
        self.assertIn(SRIC_B, note.body)
        self.assertTrue({SRIC_A, SRIC_B, AR, DEAN} <= set(note.to))

    # Approval notice ----------------------------------------------------------------------------

    def test_email_link_approval_records_the_clicking_mailbox(self):
        req = self._request()
        self.assertEqual(send_sric_approval_email(req), 2)
        approvals = [m for m in mail.outbox if "Approve (credits wallet immediately)" in m.body]
        self.assertEqual(sorted(m.to[0] for m in approvals), [SRIC_A, SRIC_B])
        self.assertTrue(all(len(m.to) == 1 for m in approvals))
        for_b = next(m for m in approvals if m.to == [SRIC_B])
        token = self._action_token(for_b)
        self.assertNotEqual(token, self._action_token(next(m for m in approvals if m.to == [SRIC_A])))

        mail.outbox.clear()
        client = APIClient()
        self.assertEqual(client.get(f"/api/wallet/recharge-action/{token}/").status_code, 200)
        resp = client.post(f"/api/wallet/recharge-action/{token}/approve/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        req.refresh_from_db()
        self.assertEqual(req.status, WalletRechargeRequestStatus.APPROVED)
        self.assertEqual(req.approved_by_email, SRIC_B)

        note = self._decision_note()
        self.assertIn(f"Approved by: {SRIC_B} (SRIC Office email link)", note.body)
        self.assertIn(SRIC_B, _html(note))
        self.assertTrue({SRIC_A, SRIC_B, AR, DEAN} <= set(note.to))
        self.assertNotIn(self.faculty.email, note.to)

    def test_tampered_or_plain_token_still_approves_without_naming_an_address(self):
        req = self._request()
        resp = APIClient().post(
            f"/api/wallet/recharge-action/{req.action_token}.forged:value/approve/", {}, format="json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        req.refresh_from_db()
        self.assertEqual(req.approved_by_email, "sric-email-approval")
        note = self._decision_note()
        self.assertIn(f"SRIC Office email link (sent to {SRIC_A}, {SRIC_B})", note.body)

    def test_admin_portal_approval_names_the_signed_in_admin(self):
        admin = User.objects.create_user(
            email="admin.notice@test.iitr.ac.in", password="pass12345", name="Admin", user_type=UserType.ADMIN
        )
        req = self._request()
        client = APIClient()
        client.force_authenticate(admin)
        resp = client.post(f"/api/admin/wallet-recharge-requests/{req.id}/approve/", {}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        note = self._decision_note()
        self.assertIn(f"Approved by: {admin.email} (signed in to the IIC portal)", note.body)
        self.assertTrue({SRIC_A, SRIC_B, AR, DEAN} <= set(note.to))

    def test_cashbook_auto_match_names_the_cashbook_sender(self):
        req = self._request()
        WalletCashbookMailboxMessage.objects.create(
            folder="INBOX", uid="9001", from_addr="SRIC Cash Book <cashbook@sric.test.iitr.ac.in>"
        )
        entry = WalletRechargeParseEntry.objects.create(
            receipt_no="R-777", dated=date(2026, 9, 20), emp_no="E5005", amount="1,000.00",
            credited_to_project_no=GRANT, name="X", source_imap_uid="9001",
        )
        with self.captureOnCommitCallbacks(execute=True):
            link_cashbook_entry_to_request(req.pk, entry.pk, actor_email="sric-cashbook-auto")
        req.refresh_from_db()
        self.assertEqual(req.approved_by_email, "cashbook@sric.test.iitr.ac.in")
        note = self._decision_note()
        self.assertIn(
            "Approved by: cashbook@sric.test.iitr.ac.in (SRIC cash-book receipt R-777, matched automatically "
            "from the SRIC cash-book email)",
            note.body,
        )

    # Direct transfer requester details --------------------------------------------------------

    def test_direct_transfer_emails_carry_student_and_supervisor_details(self):
        req = self._request(user=self.student, mode=WalletRechargeMode.DIRECT_CASH_DEPOSIT)
        self.assertEqual(send_sric_approval_email(req), 1)
        approval = next(m for m in mail.outbox if m.to == [BILLS])
        copy = next(m for m in mail.outbox if self.student.email in m.to)
        for message in (approval, copy):
            for line in (
                "User Name: Riya Student",
                "Enrollment Number: 21112233",
                "Department: IIC Notice Test",
                "Supervisor Name: Prof. Supervisor",
                "Supervisor Employee ID: E5005",
            ):
                self.assertIn(line, message.body)
            self.assertIn("Supervisor Employee ID:</span> E5005", _html(message))

        mail.outbox.clear()
        token = self._action_token(approval)
        APIClient().post(f"/api/wallet/recharge-action/{token}/approve/", {}, format="json")
        note = self._decision_note()
        self.assertIn(f"Approved by: {BILLS} (SRIC Bill Section email link)", note.body)
        self.assertIn("Enrollment Number: 21112233", note.body)
        self.assertIn("Supervisor Employee ID: E5005", note.body)
        self.assertIn(BILLS, note.to)
        self.assertIn(AR, note.to)

    def test_direct_transfer_by_faculty_shows_own_employee_id_and_self_supervisor(self):
        req = self._request(mode=WalletRechargeMode.DIRECT_CASH_DEPOSIT)
        send_sric_approval_email(req)
        approval = next(m for m in mail.outbox if m.to == [BILLS])
        self.assertIn("Employee ID: E5005", approval.body)
        self.assertIn("Designation: Associate Professor", approval.body)
        self.assertIn("Supervisor: Self (the requester is the supervisor)", approval.body)
        self.assertNotIn("Supervisor Employee ID", approval.body)
        self.assertNotIn("Enrollment Number", approval.body)

    def test_project_grant_emails_keep_the_existing_layout(self):
        send_sric_approval_email(self._request())
        approval = next(m for m in mail.outbox if m.to == [SRIC_A])
        self.assertNotIn("Supervisor Employee ID", approval.body)
        self.assertIn("Employee / ID: E5005", approval.body)

    def test_requester_template_includes_details_block_for_direct_transfer(self):
        spec = next(t for t in get_default_email_templates() if t["code"] == "wallet_recharge_approved_email")
        keys = ("code", "name", "subject", "body_text", "body_html", "description", "variable_help")
        fields = {k: spec[k] for k in keys}
        CommunicationTemplate.objects.create(
            communication_type=CommunicationTemplate.CommunicationType.EMAIL, is_active=True, **fields
        )
        self.assertIn("requester_details_html", spec["variable_help"])

        req = self._request(user=self.student, mode=WalletRechargeMode.DIRECT_CASH_DEPOSIT)
        req.approved_by_email = BILLS
        req.save(update_fields=["approved_by_email"])
        send_wallet_recharge_request_notifications(req, "APPROVED")
        sent = next(m for m in mail.outbox if self.student.email in m.to)
        html = sent.alternatives[0][0] if sent.alternatives else ""
        self.assertIn("Supervisor Employee ID:</span> E5005", html)
        self.assertIn("Enrollment Number: 21112233", sent.body)
        self.assertIn(BILLS, html)

        mail.outbox.clear()
        send_wallet_recharge_request_notifications(self._request(), "APPROVED")
        sent = next(m for m in mail.outbox if self.faculty.email in m.to)
        self.assertNotIn("Requester details", sent.body)

    # Test-account redirect ----------------------------------------------------------------------

    def test_test_account_mail_still_goes_only_to_the_test_inbox(self):
        TestAccountEmailSettings.objects.update_or_create(pk=1, defaults={"recipient_emails": QA_INBOX})
        tester = User.objects.create_user(
            email="test.faculty.notice@iic-booking.test", password="pass12345", name="Test Faculty",
            user_type=UserType.FACULTY, department=self.dept, emp_id="T0001", is_test_account=True,
        )
        wallet, _ = Wallet.objects.get_or_create(user=tester)
        req = WalletRechargeRequest.objects.create(
            user=tester, wallet=wallet, department=self.dept, amount=Decimal("300.00"),
            user_otp_verified=True, recharge_mode=WalletRechargeMode.DIRECT_CASH_DEPOSIT,
            employee_number="T0001", department_grant_code=GRANT,
        )
        send_sric_approval_email(req)
        approval = next(m for m in mail.outbox if "Approve (credits wallet immediately)" in m.body)
        self.assertEqual(approval.to, [QA_INBOX])
        APIClient().post(f"/api/wallet/recharge-action/{self._action_token(approval)}/approve/", {}, format="json")
        delivered = _delivered(mail.outbox)
        self.assertIn(QA_INBOX, delivered)
        self.assertFalse(delivered & {SRIC_A, SRIC_B, BILLS, AR, DEAN})
        req.refresh_from_db()
        self.assertEqual(req.approved_by_email, QA_INBOX)
