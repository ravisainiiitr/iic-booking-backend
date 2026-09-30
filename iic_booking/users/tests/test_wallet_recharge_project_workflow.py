"""Project Grant recharge: undertaking, project ownership/activity, OTP idempotency, project API."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.users.models import Department, DepartmentType, Project, UserType, Wallet
from iic_booking.users.models.wallet import (
    SubWallet,
    WalletRechargeMode,
    WalletRechargeRequest,
    WalletRechargeRequestAuditLog,
)
from iic_booking.users.models.wallet_sric_settings import WalletSricSettings
from iic_booking.users.wallet_recharge_undertaking import (
    CASH_UNDERTAKING_IITR_FACULTY,
    PROJECT_GRANT_UNDERTAKING,
)

User = get_user_model()

SEND_OTP = "/api/wallet/recharge-request/send-otp/"
VERIFY = "/api/wallet/recharge-request/"
PROJECTS = "/api/projects/"


@override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class ProjectGrantRechargeTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(
            name="Recharge Dept", code="RCD", department_type=DepartmentType.INTERNAL
        )
        self.faculty = self._user("fac.rc@test.iitr.ac.in", UserType.FACULTY)
        self.other_faculty = self._user("fac.other@test.iitr.ac.in", UserType.FACULTY)
        self.student = self._user("stu.rc@test.iitr.ac.in", UserType.STUDENT)
        wallet, _ = Wallet.objects.get_or_create(user=self.faculty)
        SubWallet.objects.create(wallet=wallet, department=self.dept, balance=Decimal("0"))
        self.project = Project.objects.create(
            faculty=self.faculty, name="Advanced Materials", project_code="IITR/ABC/2026/001", agency="DST"
        )
        WalletSricSettings.objects.update_or_create(pk=1, defaults={"project_grant_recharge_enabled": True})
        self.api = APIClient()
        self.api.force_authenticate(self.faculty)

    def _user(self, email, user_type):
        return User.objects.create_user(
            email=email, password="pass12345", name=email.split("@")[0], user_type=user_type, department=self.dept
        )

    def _send(self, **overrides):
        body = {
            "amount": "1000.00",
            "department_id": self.dept.id,
            "project_id": self.project.id,
            "recharge_mode": "project_grant",
            "undertaking_accepted": True,
        }
        body.update(overrides)
        return self.api.post(SEND_OTP, body, format="json")

    def _otp(self, request_id):
        return WalletRechargeRequest.objects.get(pk=request_id).user_otp_code

    # --- undertaking -----------------------------------------------------

    def test_project_grant_requires_undertaking(self):
        res = self._send(undertaking_accepted=False)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "undertaking_required")
        self.assertFalse(WalletRechargeRequest.objects.exists())
        self.assertEqual(len(mail.outbox), 0)

    def test_happy_path_records_undertaking_with_project_snapshot(self):
        res = self._send()
        self.assertEqual(res.status_code, 200, res.data)
        rid = res.data["request_id"]
        draft = WalletRechargeRequest.objects.get(pk=rid)
        self.assertTrue(draft.undertaking_accepted)
        self.assertEqual(draft.project_id, self.project.id)
        self.assertFalse(draft.user_otp_verified)

        res = self.api.post(VERIFY, {"request_id": rid, "user_otp": self._otp(rid)}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        req = WalletRechargeRequest.objects.get(pk=rid)
        self.assertTrue(req.user_otp_verified)
        self.assertEqual(req.project_grant_code, "IITR/ABC/2026/001")

        log = WalletRechargeRequestAuditLog.objects.get(request=req, action="undertaking_accepted")
        self.assertEqual(log.actor_id, self.faculty.id)
        self.assertEqual(log.message, PROJECT_GRANT_UNDERTAKING)
        self.assertEqual(log.metadata["project_code"], "IITR/ABC/2026/001")
        self.assertEqual(log.metadata["project_id"], self.project.id)
        self.assertEqual(log.metadata["amount"], "1000.00")
        self.assertTrue(log.metadata["user_otp_verified"])
        self.assertIsNotNone(log.created_at)

    def test_verify_rejects_legacy_draft_without_undertaking(self):
        wallet = Wallet.objects.get(user=self.faculty)
        draft = WalletRechargeRequest.objects.create(
            user=self.faculty, wallet=wallet, department=self.dept, amount=Decimal("500"),
            project=self.project, recharge_mode=WalletRechargeMode.PROJECT_GRANT, undertaking_accepted=False,
        )
        otp = draft.generate_user_otp()
        res = self.api.post(VERIFY, {"request_id": draft.id, "user_otp": otp}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "undertaking_required")
        self.assertFalse(WalletRechargeRequest.objects.filter(pk=draft.id).exists())

    # --- project ownership / activity -------------------------------------

    def test_cannot_use_another_faculty_project(self):
        foreign = Project.objects.create(
            faculty=self.other_faculty, name="Foreign", project_code="FOREIGN-1", agency="SERB"
        )
        res = self._send(project_id=foreign.id)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "project_inactive")
        self.assertFalse(WalletRechargeRequest.objects.exists())

    def test_project_required_for_project_grant(self):
        res = self._send(project_id=None)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "project_required")

    def test_inactive_project_rejected(self):
        Project.objects.filter(pk=self.project.pk).update(is_active=False)
        res = self._send()
        self.assertEqual(res.status_code, 400)
        self.assertFalse(WalletRechargeRequest.objects.exists())

    def test_expired_project_still_flagged_active_is_rejected(self):
        Project.objects.filter(pk=self.project.pk).update(end_date=timezone.localdate() - timedelta(days=1))
        res = self._send()
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "project_inactive")
        self.assertFalse(WalletRechargeRequest.objects.exists())

    def test_project_deactivated_between_otp_and_submit(self):
        rid = self._send().data["request_id"]
        otp = self._otp(rid)
        Project.objects.filter(pk=self.project.pk).update(is_active=False)
        res = self.api.post(VERIFY, {"request_id": rid, "user_otp": otp}, format="json")
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data["code"], "project_inactive")
        self.assertFalse(WalletRechargeRequest.objects.filter(pk=rid).exists())
        self.assertFalse(WalletRechargeRequestAuditLog.objects.filter(action="undertaking_accepted").exists())

    # --- amount / tampering ------------------------------------------------

    def test_amount_validation(self):
        for bad in ("99.99", "0", "-100", "abc", "100.123"):
            res = self._send(amount=bad)
            self.assertEqual(res.status_code, 400, bad)
        self.assertFalse(WalletRechargeRequest.objects.exists())

    def test_amount_cannot_be_changed_at_verify(self):
        rid = self._send(amount="1000.00").data["request_id"]
        res = self.api.post(
            VERIFY,
            {"request_id": rid, "user_otp": self._otp(rid), "amount": "999999", "project_id": 12345},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        req = WalletRechargeRequest.objects.get(pk=rid)
        self.assertEqual(req.amount, Decimal("1000.00"))
        self.assertEqual(req.project_id, self.project.id)

    def test_wrong_otp_does_not_submit(self):
        rid = self._send().data["request_id"]
        otp = self._otp(rid)
        wrong = "000000" if otp != "000000" else "111111"
        res = self.api.post(VERIFY, {"request_id": rid, "user_otp": wrong}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(WalletRechargeRequest.objects.get(pk=rid).user_otp_verified)

    def test_other_user_cannot_verify_request(self):
        rid = self._send().data["request_id"]
        otp = self._otp(rid)
        intruder = APIClient()
        intruder.force_authenticate(self.other_faculty)
        res = intruder.post(VERIFY, {"request_id": rid, "user_otp": otp}, format="json")
        self.assertEqual(res.status_code, 404)
        self.assertFalse(WalletRechargeRequest.objects.get(pk=rid).user_otp_verified)

    # --- duplicate submission -----------------------------------------------

    def test_repeated_submit_is_idempotent(self):
        rid = self._send().data["request_id"]
        otp = self._otp(rid)
        first = self.api.post(VERIFY, {"request_id": rid, "user_otp": otp}, format="json")
        self.assertEqual(first.status_code, 201)
        mails_after_first = len(mail.outbox)
        second = self.api.post(VERIFY, {"request_id": rid, "user_otp": otp}, format="json")
        self.assertEqual(second.status_code, 200)
        self.assertTrue(second.data["already_submitted"])
        self.assertEqual(second.data["request"]["id"], rid)
        self.assertEqual(len(mail.outbox), mails_after_first)
        self.assertEqual(WalletRechargeRequest.objects.filter(user=self.faculty).count(), 1)
        self.assertEqual(
            WalletRechargeRequestAuditLog.objects.filter(request_id=rid, action="undertaking_accepted").count(), 1
        )

    def test_resending_otp_replaces_unverified_draft(self):
        self._send()
        self._send()
        self.assertEqual(WalletRechargeRequest.objects.filter(user=self.faculty).count(), 1)

    # --- direct cash / students ------------------------------------------------

    def test_direct_cash_ignores_project_and_records_cash_undertaking(self):
        res = self._send(recharge_mode="direct_cash_deposit", undertaking_accepted=False)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "undertaking_required")

        rid = self._send(recharge_mode="direct_cash_deposit").data["request_id"]
        self.assertIsNone(WalletRechargeRequest.objects.get(pk=rid).project_id)
        res = self.api.post(VERIFY, {"request_id": rid, "user_otp": self._otp(rid)}, format="json")
        self.assertEqual(res.status_code, 201)
        log = WalletRechargeRequestAuditLog.objects.get(request_id=rid, action="undertaking_accepted")
        self.assertEqual(log.message, CASH_UNDERTAKING_IITR_FACULTY)
        self.assertIsNone(log.metadata["project_id"])

    def test_student_cannot_use_project_grant(self):
        api = APIClient()
        api.force_authenticate(self.student)
        res = api.post(
            SEND_OTP,
            {
                "amount": "1000.00",
                "department_id": self.dept.id,
                "project_id": self.project.id,
                "recharge_mode": "project_grant",
                "undertaking_accepted": True,
            },
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertFalse(WalletRechargeRequest.objects.exists())


class ProjectApiTests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(name="Proj Dept", code="PJD", department_type=DepartmentType.INTERNAL)
        self.faculty = User.objects.create_user(
            email="fac.proj@test.iitr.ac.in", password="pass12345", name="Fac", user_type=UserType.FACULTY,
            department=self.dept,
        )
        self.api = APIClient()
        self.api.force_authenticate(self.faculty)

    def test_create_project_is_owned_and_active(self):
        res = self.api.post(
            PROJECTS,
            {"name": "Inline", "project_code": "INL-1", "agency": "DST", "start_date": "2026-01-01",
             "end_date": "2099-03-31"},
            format="json",
        )
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(res.data["is_active"])
        self.assertFalse(res.data["is_expired"])
        self.assertEqual(Project.objects.get(pk=res.data["id"]).faculty_id, self.faculty.id)

    def test_faculty_field_in_payload_is_ignored(self):
        other = User.objects.create_user(
            email="fac.proj2@test.iitr.ac.in", password="pass12345", name="Fac2", user_type=UserType.FACULTY,
            department=self.dept,
        )
        res = self.api.post(
            PROJECTS, {"name": "X", "project_code": "X-1", "agency": "DST", "faculty": other.id}, format="json"
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(Project.objects.get(pk=res.data["id"]).faculty_id, self.faculty.id)

    def test_required_fields_and_date_order(self):
        res = self.api.post(PROJECTS, {"name": " ", "project_code": "", "agency": "DST"}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertIn("name", res.data)
        self.assertIn("project_code", res.data)

        res = self.api.post(
            PROJECTS,
            {"name": "Y", "project_code": "Y-1", "agency": "DST", "start_date": "2026-05-01", "end_date": "2026-04-01"},
            format="json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("non_field_errors", res.data)
        self.assertFalse(Project.objects.exists())

    def test_past_end_date_creates_inactive_project(self):
        res = self.api.post(
            PROJECTS,
            {"name": "Old", "project_code": "OLD-1", "agency": "DST", "start_date": "2020-01-01",
             "end_date": "2021-01-01"},
            format="json",
        )
        self.assertEqual(res.status_code, 201)
        self.assertFalse(res.data["is_active"])

    def test_non_faculty_cannot_create_or_list(self):
        student = User.objects.create_user(
            email="stu.proj@test.iitr.ac.in", password="pass12345", name="Stu", user_type=UserType.STUDENT,
            department=self.dept,
        )
        api = APIClient()
        api.force_authenticate(student)
        self.assertEqual(api.get(PROJECTS).status_code, 403)
        self.assertEqual(api.post(PROJECTS, {"name": "S", "project_code": "S", "agency": "A"}, format="json").status_code, 403)

    def test_cannot_read_or_edit_another_faculty_project(self):
        other = User.objects.create_user(
            email="fac.proj3@test.iitr.ac.in", password="pass12345", name="Fac3", user_type=UserType.FACULTY,
            department=self.dept,
        )
        foreign = Project.objects.create(faculty=other, name="F", project_code="F-1", agency="DST")
        self.assertEqual(self.api.get(f"{PROJECTS}{foreign.id}/").status_code, 404)
        self.assertEqual(
            self.api.patch(f"{PROJECTS}{foreign.id}/", {"name": "hijack"}, format="json").status_code, 404
        )
        foreign.refresh_from_db()
        self.assertEqual(foreign.name, "F")
