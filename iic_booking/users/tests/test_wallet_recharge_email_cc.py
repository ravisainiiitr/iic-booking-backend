"""Wallet recharge approval email: debit grant highlight and CC copy without action links."""

from __future__ import annotations

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings

from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.wallet import WalletRechargeMode, WalletRechargeRequest
from iic_booking.users.models.wallet_sric_settings import WalletSricSettings
from iic_booking.users.wallet_recharge_workflow import send_sric_approval_email

User = get_user_model()


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class RechargeEmailCcTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(
            name="IIC Email Test", code="IETS", department_type=DepartmentType.INTERNAL
        )
        self.faculty = User.objects.create_user(
            email="fac.cc@test.iitr.ac.in",
            password="pass12345",
            name="Test IITR Faculty",
            user_type=UserType.FACULTY,
            department=self.dept,
            emp_id="E9001",
        )
        s = WalletSricSettings.get_singleton()
        s.recipient_emails = "sric.office@test.iitr.ac.in"
        s.bill_section_emails = "bills@test.iitr.ac.in"
        s.project_grant_cc_emails = "accounts@test.iitr.ac.in, FAC.CC@test.iitr.ac.in"
        s.cash_deposit_cc_emails = "cash.cc1@test.iitr.ac.in\ncash.cc2@test.iitr.ac.in"
        s.save()

    def _request(self, mode):
        wallet, _ = Wallet.objects.get_or_create(user=self.faculty)
        return WalletRechargeRequest.objects.create(
            user=self.faculty,
            wallet=wallet,
            department=self.dept,
            amount=Decimal("500.00"),
            user_otp_verified=True,
            recharge_mode=mode,
            project_details="PRJ001" if mode == WalletRechargeMode.PROJECT_GRANT else "",
        )

    @staticmethod
    def _html(message):
        return next(body for body, mime in message.alternatives if mime == "text/html")

    def test_project_grant_debit_highlight_and_cc_copy(self):
        req = self._request(WalletRechargeMode.PROJECT_GRANT)
        self.assertEqual(send_sric_approval_email(req), 1)
        self.assertEqual(len(mail.outbox), 2)

        approval, copy = mail.outbox
        self.assertEqual(approval.to, ["sric.office@test.iitr.ac.in"])
        self.assertEqual(approval.cc, [])
        approval_html = self._html(approval)
        self.assertIn('class="grant-highlight grant-debit">Project Grant Code for Debit', approval_html)
        self.assertIn('<span class="grant-code">PRJ001</span>', approval_html)
        self.assertIn(req.action_token, approval_html)

        self.assertEqual(copy.to, ["fac.cc@test.iitr.ac.in"])
        self.assertEqual(copy.cc, ["accounts@test.iitr.ac.in"])
        copy_html = self._html(copy)
        self.assertIn("PRJ001", copy_html)
        self.assertNotIn(req.action_token, copy_html)
        self.assertNotIn(req.action_token, copy.body)

    def test_cash_deposit_cc_copy_with_next_steps(self):
        req = self._request(WalletRechargeMode.DIRECT_CASH_DEPOSIT)
        send_sric_approval_email(req)
        approval, copy = mail.outbox
        self.assertEqual(approval.to, ["bills@test.iitr.ac.in"])
        self.assertEqual(copy.to, ["fac.cc@test.iitr.ac.in"])
        self.assertEqual(copy.cc, ["cash.cc1@test.iitr.ac.in", "cash.cc2@test.iitr.ac.in"])
        self.assertIn("Next steps", self._html(copy))
        self.assertNotIn(req.action_token, self._html(copy))
        self.assertNotIn("Project Grant Code for Debit", self._html(approval))

    def test_requester_is_copied_even_without_configured_cc(self):
        s = WalletSricSettings.get_singleton()
        s.project_grant_cc_emails = ""
        s.save()
        send_sric_approval_email(self._request(WalletRechargeMode.PROJECT_GRANT))
        copy = mail.outbox[1]
        self.assertEqual((copy.to, copy.cc), (["fac.cc@test.iitr.ac.in"], []))
