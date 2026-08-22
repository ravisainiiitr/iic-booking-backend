"""Phase 8A — migration refund / settlement authority tests."""

from __future__ import annotations

from decimal import Decimal
import uuid

from django.test import TestCase
from rest_framework.test import APIClient

from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    ChargeProfile,
    Equipment,
    EquipmentManager,
)
from iic_booking.users.legacy_ledger.migration_refund import (
    ALREADY_PROCESSED,
    issue_migration_refund,
    migration_settlement_window_open,
)
from iic_booking.users.models import Department, SubWallet, User, Wallet
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.portal_migration import (
    MigrationBookingSettlement,
    MigrationSettlementStatus,
    MigrationSettlementType,
    PortalMigrationPhase,
    PortalMigrationState,
)
from iic_booking.users.models.user_type import UserType


def _user(user_type: str, **kwargs):
    email = kwargs.pop("email", None) or f"{user_type}-{uuid.uuid4().hex[:8]}@test.local"
    return User.objects.create_user(
        email=email,
        password="test-pass-not-used",
        user_type=user_type,
        **kwargs,
    )


def _dept(code=None):
    return Department.objects.create(
        name=f"MigDept-{uuid.uuid4().hex[:8]}",
        code=code or f"MD{uuid.uuid4().hex[:4].upper()}",
        department_type=DepartmentType.INTERNAL,
    )


def _equipment(dept):
    return Equipment.objects.create(
        name="Mig EQ",
        code=f"MQ{uuid.uuid4().hex[:4].upper()}",
        internal_department=dept,
        slot_duration_minutes=60,
    )


def _booking(user, equipment, *, charge=Decimal("100.00"), status=BookingStatus.COMPLETED):
    profile = ChargeProfile.objects.create(
        equipment=equipment,
        user_type=UserType.STUDENT,
        primary_unit_charge=charge,
    )
    return Booking.objects.create(
        user=user,
        equipment=equipment,
        charge_profile=profile,
        status=status,
        total_charge=charge,
        wallet_amount_applied=charge,
        total_time_minutes=60,
        virtual_booking_id=f"IIC{equipment.code}{uuid.uuid4().hex[:6]}",
    )


def _wallet_for(user, dept, balance=Decimal("0.00")):
    wallet = Wallet.objects.create(user=user)
    return SubWallet.objects.create(wallet=wallet, department=dept, balance=balance)


def _open_migration_window():
    state = PortalMigrationState.get_solo()
    state.end_user_booking_enabled = False
    state.phase = PortalMigrationPhase.FINANCIAL_FREEZE
    state.save()
    return state


class MigrationRefundAuthorityTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.dept = _dept()
        self.other_dept = _dept()
        self.equipment = _equipment(self.dept)
        self.other_equipment = _equipment(self.other_dept)
        self.student = _user(UserType.STUDENT)
        self.faculty = _user(UserType.FACULTY)
        self.lab = _user(UserType.OPERATOR)
        self.dept_admin = _user(UserType.DEPT_ADMIN)
        self.oic = _user(UserType.MANAGER)
        self.main_admin = _user(UserType.ADMIN)
        EquipmentManager.objects.create(equipment=self.equipment, manager=self.oic)
        self.booking_user = _user(UserType.FACULTY)
        self.sub = _wallet_for(self.booking_user, self.dept, Decimal("50.00"))
        self.booking = _booking(self.booking_user, self.equipment)
        _open_migration_window()

    def _url(self, booking_id=None):
        bid = booking_id or self.booking.booking_id
        return f"/api/bookings/{bid}/migration-refund/"

    def test_window_open(self):
        self.assertTrue(migration_settlement_window_open())

    def test_oic_can_issue_eligible_refund(self):
        self.client.force_authenticate(self.oic)
        before = self.sub.balance
        res = self.client.post(self._url(), {"confirm": True, "reason": "settlement"}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.sub.refresh_from_db()
        self.assertEqual(self.sub.balance, before + Decimal("100.00"))
        st = MigrationBookingSettlement.objects.get(booking=self.booking, status=MigrationSettlementStatus.COMPLETED)
        self.assertEqual(st.settlement_type, MigrationSettlementType.MIGRATION_REFUND)
        self.assertTrue(st.reference.startswith("MIG-REF-"))
        self.assertEqual(st.processed_by_id, self.oic.id)
        self.assertEqual(st.wallet_transaction_id, st.wallet_transaction.id)

    def test_main_administrator_can_issue_refund(self):
        self.client.force_authenticate(self.main_admin)
        res = self.client.post(self._url(), {"confirm": True}, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["settlement"]["status"], "COMPLETED")

    def test_normal_user_cannot_issue_refund(self):
        self.client.force_authenticate(self.student)
        res = self.client.post(self._url(), {"confirm": True}, format="json")
        self.assertEqual(res.status_code, 403)

    def test_faculty_cannot_issue_refund(self):
        self.client.force_authenticate(self.faculty)
        res = self.client.post(self._url(), {"confirm": True}, format="json")
        self.assertEqual(res.status_code, 403)

    def test_lab_in_charge_cannot_issue_refund(self):
        self.client.force_authenticate(self.lab)
        res = self.client.post(self._url(), {"confirm": True}, format="json")
        self.assertEqual(res.status_code, 403)

    def test_dept_admin_cannot_issue_refund(self):
        self.client.force_authenticate(self.dept_admin)
        res = self.client.post(self._url(), {"confirm": True}, format="json")
        self.assertEqual(res.status_code, 403)

    def test_duplicate_refund_rejected(self):
        self.client.force_authenticate(self.main_admin)
        self.assertEqual(self.client.post(self._url(), {"confirm": True}, format="json").status_code, 200)
        res = self.client.post(self._url(), {"confirm": True}, format="json")
        self.assertEqual(res.status_code, 409)
        self.assertIn(ALREADY_PROCESSED, res.data["error"])
        self.assertEqual(
            MigrationBookingSettlement.objects.filter(
                booking=self.booking, status=MigrationSettlementStatus.COMPLETED
            ).count(),
            1,
        )

    def test_already_completed_cannot_refund_again(self):
        issue_migration_refund(booking=self.booking, actor=self.main_admin, confirm=True)
        with self.assertRaisesMessage(Exception, ALREADY_PROCESSED):
            issue_migration_refund(booking=self.booking, actor=self.main_admin, confirm=True)

    def test_invalid_booking_rejected(self):
        self.client.force_authenticate(self.main_admin)
        res = self.client.post("/api/bookings/99999999/migration-refund/", {"confirm": True}, format="json")
        self.assertEqual(res.status_code, 404)

    def test_non_refundable_booking_rejected(self):
        self.booking.status = BookingStatus.REFUNDED
        self.booking.save(update_fields=["status"])
        self.client.force_authenticate(self.main_admin)
        res = self.client.post(self._url(), {"confirm": True}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["error_code"], "non_refundable")

    def test_zero_refundable_amount_rejected(self):
        self.booking.total_charge = Decimal("0")
        self.booking.wallet_amount_applied = Decimal("0")
        self.booking.save(update_fields=["total_charge", "wallet_amount_applied"])
        self.client.force_authenticate(self.main_admin)
        res = self.client.post(self._url(), {"confirm": True}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["error_code"], "zero_amount")

    def test_refund_creates_ledger_credit(self):
        self.client.force_authenticate(self.main_admin)
        self.client.post(self._url(), {"confirm": True}, format="json")
        st = MigrationBookingSettlement.objects.get(status=MigrationSettlementStatus.COMPLETED)
        self.assertIsNotNone(st.wallet_transaction_id)
        self.assertEqual(st.wallet_transaction.transaction_type, "credit")
        self.assertEqual(st.wallet_transaction.amount, Decimal("100.00"))
        self.assertIn("Migration refund", st.wallet_transaction.description)

    def test_refund_is_audited(self):
        self.client.force_authenticate(self.oic)
        self.client.post(self._url(), {"confirm": True, "reason": "clear pending"}, format="json")
        st = MigrationBookingSettlement.objects.get(status=MigrationSettlementStatus.COMPLETED)
        self.assertEqual(st.processed_by_id, self.oic.id)
        self.assertEqual(st.processed_by_role, "Officer-in-Charge")
        self.assertEqual(st.legacy_booking_id, self.booking.booking_id)
        self.assertEqual(st.user_id, self.booking_user.id)
        self.assertEqual(st.original_amount, Decimal("100.00"))
        self.assertEqual(st.refund_amount, Decimal("100.00"))
        self.assertEqual(st.reason, "clear pending")
        self.assertIsNotNone(st.processed_at)
        self.assertTrue(st.reference)

    def test_failed_refund_does_not_become_completed(self):
        # No wallet → failure path
        Wallet.objects.filter(user=self.booking_user).delete()
        self.client.force_authenticate(self.main_admin)
        res = self.client.post(self._url(), {"confirm": True}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(
            MigrationBookingSettlement.objects.filter(
                booking=self.booking, status=MigrationSettlementStatus.COMPLETED
            ).exists()
        )

    def test_retry_idempotent_after_completed(self):
        self.client.force_authenticate(self.main_admin)
        self.client.post(self._url(), {"confirm": True}, format="json")
        bal = SubWallet.objects.get(wallet__user=self.booking_user, department=self.dept).balance
        res = self.client.post(self._url(), {"confirm": True}, format="json")
        self.assertEqual(res.status_code, 409)
        bal2 = SubWallet.objects.get(wallet__user=self.booking_user, department=self.dept).balance
        self.assertEqual(bal, bal2)

    def test_refund_does_not_unlock_old_booking_creation(self):
        state = PortalMigrationState.get_solo()
        self.assertFalse(state.end_user_booking_enabled)
        self.client.force_authenticate(self.main_admin)
        res = self.client.post(self._url(), {"confirm": True}, format="json")
        self.assertEqual(res.status_code, 200)
        state.refresh_from_db()
        self.assertFalse(state.end_user_booking_enabled)
        self.assertTrue(res.data["safety"]["end_user_booking_enabled_unchanged"])

    def test_refund_does_not_create_new_booking(self):
        before = Booking.objects.count()
        self.client.force_authenticate(self.main_admin)
        res = self.client.post(self._url(), {"confirm": True}, format="json")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(Booking.objects.count(), before)
        self.assertFalse(res.data["safety"]["new_booking_created"])
        self.booking.refresh_from_db()
        # Settlement credit only — does not force REFUNDED / free slots
        self.assertEqual(self.booking.status, BookingStatus.COMPLETED)

    def test_main_admin_sees_all_department_equipment_in_report(self):
        other_user = _user(UserType.FACULTY)
        _wallet_for(other_user, self.other_dept)
        other_booking = _booking(other_user, self.other_equipment)
        self.client.force_authenticate(self.main_admin)
        res = self.client.get("/api/portal-migration/admin/settlements/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["scope"], "all_departments")
        ids = {r["booking_id"] for r in res.data["results"]}
        self.assertIn(self.booking.booking_id, ids)
        self.assertIn(other_booking.booking_id, ids)

    def test_oic_sees_only_authorized_scope(self):
        other_user = _user(UserType.FACULTY)
        _wallet_for(other_user, self.other_dept)
        other_booking = _booking(other_user, self.other_equipment)
        self.client.force_authenticate(self.oic)
        # OIC cannot refund out-of-scope booking
        res = self.client.post(
            f"/api/bookings/{other_booking.booking_id}/migration-refund/",
            {"confirm": True},
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        report = self.client.get("/api/portal-migration/admin/settlements/")
        self.assertEqual(report.status_code, 200)
        self.assertEqual(report.data["scope"], "oic_equipment")
        ids = {r["booking_id"] for r in report.data["results"]}
        self.assertIn(self.booking.booking_id, ids)
        self.assertNotIn(other_booking.booking_id, ids)

    def test_backend_rbac_enforced_without_confirm(self):
        self.client.force_authenticate(self.main_admin)
        res = self.client.post(self._url(), {"confirm": False}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["error_code"], "confirmation_required")

    def test_window_closed_rejects(self):
        state = PortalMigrationState.get_solo()
        state.end_user_booking_enabled = True
        state.legacy_ledger_frozen = False
        state.phase = PortalMigrationPhase.NEW_PORTAL_ACTIVE
        state.save()
        self.client.force_authenticate(self.main_admin)
        res = self.client.post(self._url(), {"confirm": True}, format="json")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["error_code"], "window_closed")
