"""The legacy approve / reject by request id (/api/wallet/recharge-requests/<id>/approve|reject/) used to accept
anyone, signed in or not. Only a signed-in approver of that request may decide it now; email approvers keep their
personal token links."""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.wallet import (
    SubWallet,
    WalletRechargeMode,
    WalletRechargeRequest,
    WalletRechargeRequestStatus,
)

User = get_user_model()
APPROVER_EMAIL = "bills.legacy@iitr.ac.in"
APPROVERS = "iic_booking.users.wallet_recharge_workflow.get_recharge_approver_emails"


def _user(email, user_type, department=None, **extra):
    return User.objects.create_user(
        email=email,
        password="pass12345",
        name=email.split("@")[0],
        user_type=user_type,
        department=department,
        admin_approved=True,
        email_verified=True,
        **extra,
    )


@mock.patch(APPROVERS, return_value=[APPROVER_EMAIL])
@mock.patch("iic_booking.users.wallet_recharge_workflow.notify_stakeholders_of_decision", return_value=None)
class LegacyRechargeDecisionAuthTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="IIC Legacy", code="IICLEG", department_type=DepartmentType.INTERNAL)
        self.other = Department.objects.create(name="Physics Legacy", code="PHYLEG", department_type=DepartmentType.INTERNAL)
        self.faculty = _user("faculty.legacy@iitr.ac.in", UserType.FACULTY, self.dept, emp_id="L0001")
        self.wallet, _ = Wallet.objects.get_or_create(user=self.faculty)
        SubWallet.objects.get_or_create(wallet=self.wallet, department=self.dept, defaults={"balance": Decimal("0.00")})
        self.admin = _user("main.admin.legacy@iitr.ac.in", UserType.ADMIN)
        self.dept_admin = _user("dadmin.legacy@iitr.ac.in", UserType.DEPT_ADMIN, self.dept)
        self.accounts_other = _user("accounts.phy.legacy@iitr.ac.in", UserType.FINANCE, self.other)
        self.listed = _user(APPROVER_EMAIL, UserType.FINANCE)
        self.stranger = _user("someone.legacy@iitr.ac.in", UserType.FACULTY, self.other, emp_id="L0002")

    def request_(self, mode=WalletRechargeMode.DIRECT_CASH_DEPOSIT):
        return WalletRechargeRequest.objects.create(
            user=self.faculty,
            wallet=self.wallet,
            department=self.dept,
            amount=Decimal("700.00"),
            user_otp_verified=True,
            recharge_mode=mode,
            employee_number="L0001",
            department_grant_code="G-1",
            project_grant_code="PRJ-1" if mode == WalletRechargeMode.PROJECT_GRANT else "",
        )

    def post(self, user, req, action, data=None):
        client = APIClient()
        if user is not None:
            client.force_authenticate(user)
        return client.post(f"/api/wallet/recharge-requests/{req.pk}/{action}/", data or {}, format="json")

    def status_of(self, req):
        return WalletRechargeRequest.objects.get(pk=req.pk).status

    def balance(self):
        return SubWallet.objects.get(wallet=self.wallet, department=self.dept).balance

    def test_anonymous_cannot_approve_or_reject(self, *_):
        for mode in (WalletRechargeMode.DIRECT_CASH_DEPOSIT, WalletRechargeMode.PROJECT_GRANT):
            req = self.request_(mode)
            self.assertIn(self.post(None, req, "approve").status_code, (401, 403))
            self.assertIn(self.post(None, req, "reject", {"reason_text": "no"}).status_code, (401, 403))
            self.assertEqual(self.status_of(req), WalletRechargeRequestStatus.PENDING)
        self.assertEqual(self.balance(), Decimal("0.00"))

    def test_requester_and_unrelated_users_are_refused(self, *_):
        req = self.request_()
        for user in (self.faculty, self.stranger, self.accounts_other):
            res = self.post(user, req, "approve")
            self.assertEqual(res.status_code, 403, (user.email, res.data))
            self.assertEqual(res.data["code"], "not_an_approver")
            self.assertEqual(self.post(user, req, "reject", {"reason_text": "no"}).status_code, 403)
        self.assertEqual(self.status_of(req), WalletRechargeRequestStatus.PENDING)
        self.assertEqual(self.balance(), Decimal("0.00"))

    def test_main_admin_approves_once(self, *_):
        req = self.request_()
        res = self.post(self.admin, req, "approve")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.status_of(req), WalletRechargeRequestStatus.APPROVED)
        self.assertEqual(self.balance(), Decimal("700.00"))
        again = self.post(self.admin, req, "approve")
        self.assertTrue(again.data.get("already_processed"))
        self.assertEqual(self.balance(), Decimal("700.00"))

    def test_department_admin_of_the_request_rejects(self, *_):
        req = self.request_()
        res = self.post(self.dept_admin, req, "reject", {"reason_text": "Wrong grant code"})
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.status_of(req), WalletRechargeRequestStatus.REJECTED)

    def test_user_on_the_configured_approver_list_may_decide(self, *_):
        req = self.request_()
        res = self.post(self.listed, req, "approve")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(self.balance(), Decimal("700.00"))
