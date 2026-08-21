"""PostgreSQL tests for administrator-approved Wallet Credit Facility V2."""

from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from iic_booking.users.models import Department, DepartmentType, UserType, Wallet
from iic_booking.users.models.wallet import SubWallet, SubWalletTransaction
from iic_booking.users.models.wallet_credit_facility import (
    WalletCreditFacility,
    WalletCreditFacilityStatus,
    WalletCreditLedgerEntry,
    WalletCreditLedgerKind,
    WalletCreditPolicy,
)
from iic_booking.users.wallet_credit_facility_v2 import (
    WalletCreditError,
    approve_facility,
    assert_user_may_request_credit,
    create_and_submit_request,
    post_credit,
    reconcile_facility,
    reject_facility,
    repay_from_wallet,
)
from iic_booking.users.wallet_credit_facility import (
    subwallet_minimum_balance_after_debit,
    try_activate_credit_facility_after_otp_verify,
)
from iic_booking.users.models.wallet import WalletRechargeRequest

User = get_user_model()


@override_settings(WALLET_CREDIT_FACILITY_V2_ENABLED=True)
class WalletCreditFacilityV2Tests(TestCase):
    def setUp(self):
        self.dept = Department.objects.create(
            name="IIC Credit Test",
            code="ICCT",
            department_type=DepartmentType.INTERNAL,
        )
        self.faculty = User.objects.create_user(
            email="faculty.wcv2@test.iitr.ac.in",
            password="pass12345",
            name="Faculty WCV2",
            user_type=UserType.FACULTY,
            department=self.dept,
            emp_id="E1001",
            internal_id="CI-1001",
            designation="Assistant Professor",
        )
        self.student = User.objects.create_user(
            email="student.wcv2@test.iitr.ac.in",
            password="pass12345",
            name="Student WCV2",
            user_type=UserType.STUDENT,
            department=self.dept,
        )
        self.unknown = User.objects.create_user(
            email="unknown.wcv2@test.iitr.ac.in",
            password="pass12345",
            name="Unknown WCV2",
            user_type="",
            department=self.dept,
        )
        self.admin = User.objects.create_user(
            email="admin.wcv2@test.iitr.ac.in",
            password="pass12345",
            name="Admin WCV2",
            user_type=UserType.ADMIN,
        )
        self.finance = User.objects.create_user(
            email="finance.wcv2@test.iitr.ac.in",
            password="pass12345",
            name="Finance WCV2",
            user_type=UserType.FINANCE,
        )
        self.other_faculty = User.objects.create_user(
            email="other.faculty.wcv2@test.iitr.ac.in",
            password="pass12345",
            name="Other Faculty",
            user_type=UserType.FACULTY,
            department=self.dept,
        )
        self.wallet = Wallet.objects.create(user=self.faculty)
        self.sub = SubWallet.objects.create(
            wallet=self.wallet,
            department=self.dept,
            balance=Decimal("500.00"),
        )
        policy = WalletCreditPolicy.get_solo()
        policy.enabled = True
        policy.max_credit_amount = Decimal("50000.00")
        policy.min_request_amount = Decimal("100.00")
        policy.max_outstanding_amount = Decimal("50000.00")
        policy.max_credit_duration_days = 30
        policy.save()
        self.client = APIClient()

    def test_student_blocked_service(self):
        with self.assertRaises(WalletCreditError) as ctx:
            assert_user_may_request_credit(self.student)
        self.assertEqual(ctx.exception.code, "CREDIT_NOT_ALLOWED_FOR_USER_TYPE")
        self.assertEqual(ctx.exception.status, 403)

    def test_unknown_user_type_blocked(self):
        with self.assertRaises(WalletCreditError) as ctx:
            assert_user_may_request_credit(self.unknown)
        self.assertEqual(ctx.exception.code, "USER_TYPE_UNKNOWN")

    def test_faculty_can_request(self):
        assert_user_may_request_credit(self.faculty)
        facility = create_and_submit_request(
            user=self.faculty,
            requested_amount=Decimal("10000"),
            purpose="Equipment booking buffer",
            remarks="audit test",
        )
        self.assertEqual(facility.status, WalletCreditFacilityStatus.SUBMITTED)
        self.assertEqual(facility.requested_amount, Decimal("10000.00"))
        self.assertIsNone(facility.approved_amount)
        self.assertIn("employee_id", facility.profile_snapshot)
        self.assertEqual(facility.profile_snapshot.get("employee_id"), "E1001")

    def test_duplicate_active_request_blocked(self):
        create_and_submit_request(
            user=self.faculty,
            requested_amount=Decimal("1000"),
            purpose="first",
        )
        with self.assertRaises(WalletCreditError) as ctx:
            create_and_submit_request(
                user=self.faculty,
                requested_amount=Decimal("2000"),
                purpose="second",
            )
        self.assertEqual(ctx.exception.code, "ACTIVE_CREDIT_EXISTS")

    def test_admin_approve_reduce_reject_and_credit_posting(self):
        facility = create_and_submit_request(
            user=self.faculty,
            requested_amount=Decimal("10000"),
            purpose="need credit",
        )
        bal_before = self.sub.balance
        txn_count_before = SubWalletTransaction.objects.filter(sub_wallet=self.sub).count()

        with self.assertRaises(WalletCreditError):
            approve_facility(
                facility=facility,
                actor=self.faculty,
                approved_amount=Decimal("7500"),
                due_date=None,
                reason="self approve",
            )

        facility = approve_facility(
            facility=facility,
            actor=self.admin,
            approved_amount=Decimal("7500"),
            due_date=None,
            reason="Reduced after profile review",
        )
        self.assertEqual(facility.requested_amount, Decimal("10000.00"))
        self.assertEqual(facility.approved_amount, Decimal("7500.00"))
        self.assertEqual(facility.status, WalletCreditFacilityStatus.APPROVED)

        facility = post_credit(facility=facility, actor=self.admin)
        self.assertEqual(facility.status, WalletCreditFacilityStatus.CREDITED)
        self.assertEqual(facility.outstanding_amount, Decimal("7500.00"))
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.balance, bal_before + Decimal("7500.00"))
        self.assertEqual(
            SubWalletTransaction.objects.filter(sub_wallet=self.sub).count(),
            txn_count_before + 1,
        )

        # Idempotent second post
        facility2 = post_credit(facility=facility, actor=self.admin)
        self.assertEqual(facility2.status, WalletCreditFacilityStatus.CREDITED)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.balance, bal_before + Decimal("7500.00"))
        self.assertEqual(
            WalletCreditLedgerEntry.objects.filter(
                facility=facility, kind=WalletCreditLedgerKind.WALLET_CREDIT
            ).count(),
            1,
        )

        # Partial repayment
        payment = repay_from_wallet(facility=facility, actor=self.faculty, amount=Decimal("3000"))
        facility.refresh_from_db()
        self.assertEqual(facility.outstanding_amount, Decimal("4500.00"))
        self.assertEqual(facility.status, WalletCreditFacilityStatus.PARTIALLY_SETTLED)
        self.assertTrue(payment.receipt_number.startswith("WCR-"))

        # Full repayment
        repay_from_wallet(facility=facility, actor=self.faculty, amount=Decimal("4500"))
        facility.refresh_from_db()
        self.assertEqual(facility.outstanding_amount, Decimal("0.00"))
        self.assertEqual(facility.status, WalletCreditFacilityStatus.CLEARED)
        report = reconcile_facility(facility)
        self.assertTrue(report["consistent"])

    def test_reject_requires_reason(self):
        facility = create_and_submit_request(
            user=self.faculty,
            requested_amount=Decimal("1000"),
            purpose="x",
        )
        with self.assertRaises(WalletCreditError):
            reject_facility(facility=facility, actor=self.admin, reason="")
        facility = reject_facility(facility=facility, actor=self.admin, reason="Incomplete docs")
        self.assertEqual(facility.status, WalletCreditFacilityStatus.REJECTED)

    def test_api_student_forbidden(self):
        self.client.force_authenticate(user=self.student)
        res = self.client.post(
            "/api/wallet/credit-requests/",
            {"requested_amount": "1000", "purpose": "try"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data.get("code"), "CREDIT_NOT_ALLOWED_FOR_USER_TYPE")

    def test_api_idor(self):
        facility = create_and_submit_request(
            user=self.faculty,
            requested_amount=Decimal("1000"),
            purpose="idor",
        )
        self.client.force_authenticate(user=self.other_faculty)
        res = self.client.get(f"/api/wallet/credit-requests/{facility.id}/")
        self.assertEqual(res.status_code, 404)

    def test_api_admin_approve_flow(self):
        facility = create_and_submit_request(
            user=self.faculty,
            requested_amount=Decimal("5000"),
            purpose="admin api",
        )
        self.client.force_authenticate(user=self.admin)
        res = self.client.post(
            f"/api/admin/wallet-credit/{facility.id}/approve/",
            {"approved_amount": "4000", "reason": "reduce", "post_credit": True},
            format="json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["requested_amount"], "5000.00")
        self.assertEqual(res.data["approved_amount"], "4000.00")
        self.assertEqual(res.data["status"], WalletCreditFacilityStatus.CREDITED)
        self.assertIn("channel_i_profile", res.data)

    def test_old_automatic_credit_disabled(self):
        self.assertEqual(subwallet_minimum_balance_after_debit(self.sub), Decimal("0.00"))
        req = mock.Mock()
        req.credit_facility_opted_in = True
        req.pk = 1
        with mock.patch.object(WalletRechargeRequest.objects, "filter") as filt:
            filt.return_value.update.return_value = 1
            try_activate_credit_facility_after_otp_verify(req)
            filt.assert_called()

    def test_feature_flag_off_blocks(self):
        with override_settings(WALLET_CREDIT_FACILITY_V2_ENABLED=False):
            with self.assertRaises(WalletCreditError) as ctx:
                create_and_submit_request(
                    user=self.faculty,
                    requested_amount=Decimal("1000"),
                    purpose="flag off",
                )
            self.assertEqual(ctx.exception.code, "FEATURE_DISABLED")

    def test_audit_immutable(self):
        facility = create_and_submit_request(
            user=self.faculty,
            requested_amount=Decimal("1000"),
            purpose="audit",
        )
        ev = facility.audit_events.first()
        with self.assertRaises(ValueError):
            ev.action = "TAMPER"
            ev.save()
