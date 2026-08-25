"""Phase 8C — staging migration simulation + notification tests (no production)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import uuid
from unittest.mock import patch

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    Equipment,
    EquipmentManager,
    EquipmentStatus,
    SlotMaster,
    SlotStatus,
)
from iic_booking.users.legacy_ledger.booking_bridge import (
    LEGACY_MIGRATION_SLOT_BLOCKED,
    abort_migration_batch,
    reconcile_legacy_blocks,
)
from iic_booking.users.legacy_ledger.booking_lock import legacy_portal_mutating_booking_blocked
from iic_booking.users.legacy_ledger.equipment_mapping import validate_legacy_equipment_mappings
from iic_booking.users.legacy_ledger.migration_dry_run import migration_dry_run
from iic_booking.users.legacy_ledger.migration_emails import (
    SUBJECTS,
    build_migration_email,
    classify_migration_template,
)
from iic_booking.users.legacy_ledger.migration_notifications import (
    create_notification_batch,
    deliver_notification_recipient,
    queue_notification_batch,
    select_notification_candidates,
)
from iic_booking.users.legacy_ledger.migration_refund import issue_migration_refund
from iic_booking.users.legacy_ledger.migration_t0 import run_staging_t0
from iic_booking.users.models import Department, SubWallet, User, Wallet
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.portal_migration import (
    LegacyBookingBlock,
    LegacyBookingBlockStatus,
    LegacyBookingMigrationBatch,
    LegacyEquipmentMapping,
    LegacyEquipmentMappingStatus,
    MigrationBookingSettlement,
    MigrationNotificationBatch,
    MigrationNotificationRecipient,
    MigrationNotificationStatus,
    MigrationNotificationTemplate,
    MigrationSettlementStatus,
    PortalMigrationPhase,
    PortalMigrationState,
)
from iic_booking.users.models.user_type import UserType


def _user(user_type: str, **kwargs):
    email = kwargs.pop("email", None) or f"{user_type}-{uuid.uuid4().hex[:8]}@staging.test"
    return User.objects.create_user(
        email=email,
        password="test-pass-not-used",
        user_type=user_type,
        name=kwargs.pop("name", f"User {user_type}"),
        **kwargs,
    )


def _dept():
    return Department.objects.create(
        name=f"8C-{uuid.uuid4().hex[:6]}",
        code=f"C{uuid.uuid4().hex[:4].upper()}",
        department_type=DepartmentType.INTERNAL,
    )


def _equipment(dept, code=None):
    return Equipment.objects.create(
        name="8C EQ",
        code=code or f"C8{uuid.uuid4().hex[:4].upper()}",
        internal_department=dept,
        slot_duration_minutes=60,
        status=EquipmentStatus.ACTIVE,
    )


def _slot(equipment, start, end, status=SlotStatus.AVAILABLE):
    sm = SlotMaster.objects.create(
        equipment=equipment,
        slot_number=int(uuid.uuid4().int % 9000) + 1,
        open_time=start.time().replace(microsecond=0),
        close_time=end.time().replace(microsecond=0),
        is_active=True,
    )
    return DailySlot.objects.create(
        slot_master=sm,
        date=start.date(),
        start_datetime=start,
        end_datetime=end,
        status=status,
    )


def _wallet(user, dept, balance=Decimal("500")):
    w = Wallet.objects.create(user=user)
    return SubWallet.objects.create(wallet=w, department=dept, balance=balance)


@override_settings(DEPLOYMENT_ENVIRONMENT="STAGING")
class Phase8CEmailTests(TestCase):
    def test_classify_roles(self):
        self.assertEqual(
            classify_migration_template(_user(UserType.FACULTY))[0],
            MigrationNotificationTemplate.FACULTY_MIGRATION,
        )
        self.assertEqual(
            classify_migration_template(_user(UserType.STUDENT))[0],
            MigrationNotificationTemplate.STUDENT_MIGRATION,
        )
        self.assertEqual(
            classify_migration_template(_user(UserType.MANAGER))[0],
            MigrationNotificationTemplate.OIC_MIGRATION,
        )
        self.assertEqual(
            classify_migration_template(_user(UserType.ADMIN))[0],
            MigrationNotificationTemplate.ADMIN_MIGRATION,
        )
        tmpl, reason = classify_migration_template(_user(UserType.OPERATOR))
        self.assertIsNone(tmpl)
        self.assertIn("unsupported_role", reason)

    def test_email_subjects_and_content(self):
        for code, subject in SUBJECTS.items():
            content = build_migration_email(
                code,
                user_name="Preview",
                new_portal_url="https://staging.example/new",
                migration_datetime="01 Sep 2026, 09:00 IST",
                support_email="support@example.test",
            )
            self.assertEqual(content.subject, subject)
            self.assertIn("https://staging.example/new", content.html_body)
            self.assertIn("01 Sep 2026", content.html_body)
            self.assertNotIn("password", content.html_body.lower())
            self.assertNotIn("secret", content.html_body.lower())

    def test_notification_dry_run_sends_zero(self):
        faculty = _user(UserType.FACULTY)
        student = _user(UserType.STUDENT)
        oic = _user(UserType.MANAGER)
        admin = _user(UserType.ADMIN)
        operator = _user(UserType.OPERATOR)  # skipped
        qs = User.objects.filter(pk__in=[faculty.pk, student.pk, oic.pk, admin.pk, operator.pk])
        batch, report = create_notification_batch(dry_run=True, users=qs)
        self.assertTrue(batch.dry_run)
        self.assertEqual(report["faculty"], 1)
        self.assertEqual(report["students"], 1)
        self.assertEqual(report["oic"], 1)
        self.assertEqual(report["admin"], 1)
        self.assertGreaterEqual(report["skipped"], 1)
        self.assertEqual(len(mail.outbox), 0)
        q = queue_notification_batch(batch)
        self.assertEqual(q["queued"], 0)
        self.assertEqual(len(mail.outbox), 0)

    def test_idempotent_send(self):
        u = _user(UserType.FACULTY)
        batch, _ = create_notification_batch(dry_run=False, users=User.objects.filter(pk=u.pk))
        rec = batch.recipients.get()
        with patch("iic_booking.users.tasks.send_migration_notification_recipient.delay"):
            queue_notification_batch(batch)
        r1 = deliver_notification_recipient(rec.id)
        self.assertEqual(r1["status"], "SENT")
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("new platform", mail.outbox[0].subject.lower())
        r2 = deliver_notification_recipient(rec.id)
        self.assertTrue(r2.get("idempotent"))
        self.assertEqual(len(mail.outbox), 1)


@override_settings(DEPLOYMENT_ENVIRONMENT="STAGING")
class Phase8CStagingSimulationTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.dept = _dept()
        self.eq = _equipment(self.dept)
        self.eq2 = _equipment(self.dept)
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=101,
            old_equipment_code="OLD101",
            new_equipment=self.eq,
            department=self.dept,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=102,
            old_equipment_code="OLD102",
            new_equipment=self.eq2,
            department=self.dept,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        self.start = timezone.now().replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
        self.end_window = self.start + timedelta(days=7)
        state = PortalMigrationState.get_solo()
        state.migration_start_at = self.start
        state.migration_window_end_at = self.end_window
        state.new_portal_url = "https://staging.example.invalid/portal"
        state.booking_migration_mode = "PREPARATION"
        state.end_user_booking_enabled = True
        state.save()

        self.booking_start = self.start + timedelta(hours=2)
        self.booking_end = self.booking_start + timedelta(hours=1)
        overlap_start = self.booking_start + timedelta(minutes=30)
        outside = self.end_window + timedelta(days=1)

        self.fixture = [
            {
                "legacy_booking_id": 1,
                "old_equipment_id": 101,
                "start_at": self.booking_start,
                "end_at": self.booking_end,
                "status": "CONFIRMED",
                "amount": "100",
                "employee_id": "E1",
            },
            {
                "legacy_booking_id": 2,
                "old_equipment_id": 101,
                "start_at": self.start + timedelta(hours=3),
                "end_at": self.start + timedelta(hours=4),
                "status": "CONFIRMED",
                "amount": "50",
            },
            {
                "legacy_booking_id": 3,
                "old_equipment_id": 102,
                "start_at": self.booking_start,
                "end_at": self.booking_end,
                "status": "CONFIRMED",
            },
            {
                "legacy_booking_id": 4,
                "old_equipment_id": 999,
                "start_at": self.booking_start,
                "end_at": self.booking_end,
                "status": "CONFIRMED",
            },
            {
                "legacy_booking_id": 5,
                "old_equipment_id": 101,
                "start_at": self.booking_start,
                "end_at": self.booking_end,
                "status": "CANCELLED",
            },
            {
                "legacy_booking_id": 6,
                "old_equipment_id": 101,
                "start_at": self.booking_start,
                "end_at": self.booking_end,
                "status": "COMPLETED",
            },
            {
                "legacy_booking_id": 7,
                "old_equipment_id": 101,
                "start_at": outside,
                "end_at": outside + timedelta(hours=1),
                "status": "CONFIRMED",
            },
            {
                "legacy_booking_id": 8,
                "old_equipment_id": 101,
                "start_at": self.start + timedelta(hours=8),
                "end_at": self.start + timedelta(hours=9),
                "status": "CONFIRMED",
                "amount": "75",
            },
        ]
        # slots for arming eligible bookings
        for offset_h in (2, 3, 5, 8):
            s = self.start + timedelta(hours=offset_h)
            _slot(self.eq, s, s + timedelta(hours=1))
        _slot(self.eq2, self.booking_start, self.booking_end)

    def test_mapping_validation_ready(self):
        report = validate_legacy_equipment_mappings()
        self.assertTrue(report["ready"])

    def test_discovery_classifications(self):
        from iic_booking.users.legacy_ledger.booking_bridge import discover_legacy_bookings

        # Add intentional overlap conflict: second ACTIVE block preview via pre-arm
        from iic_booking.users.legacy_ledger.booking_bridge import arm_legacy_block

        arm_legacy_block(
            legacy_booking_id=900,
            equipment=self.eq,
            start_at=self.booking_start,
            end_at=self.booking_end,
        )
        report = discover_legacy_bookings(self.fixture)
        self.assertGreaterEqual(report["counts"]["eligible"], 1)
        self.assertEqual(report["counts"]["unmapped"], 1)
        self.assertEqual(report["counts"]["cancelled"], 1)
        self.assertEqual(report["counts"]["completed"], 1)
        self.assertEqual(report["counts"]["outside_window"], 1)  # outside window
        self.assertGreaterEqual(report["counts"]["conflicting"], 1)
        self.assertEqual(LegacyBookingBlock.objects.filter(legacy_booking_id=1).count(), 0)

    def test_dry_run_not_ready_with_unmapped(self):
        report = migration_dry_run(self.fixture)
        self.assertEqual(report["verdict"], "NOT READY")
        self.assertIn("settlement_count", report)
        self.assertIn("freeze_state", report)

    @patch("iic_booking.users.legacy_ledger.datetime_contract.validate_contract_for_discovery")
    @patch("iic_booking.users.legacy_ledger.datetime_contract.contract_blocks_t0")
    def test_staging_t0_and_freeze_and_blocks(self, mock_blocks, mock_gate):
        mock_blocks.return_value = False
        mock_gate.return_value = {"ready_for_discovery": True, "blockers": []}
        eligible_only = [r for r in self.fixture if r["legacy_booking_id"] in {2, 3, 8}]
        result = run_staging_t0(
            legacy_rows=eligible_only,
            confirm_staging_t0=True,
            queue_emails=False,
            email_dry_run=True,
        )
        self.assertTrue(result["ok"], result)
        state = PortalMigrationState.get_solo()
        self.assertEqual(state.booking_migration_mode, "ACTIVE")
        blocked, code, _ = legacy_portal_mutating_booking_blocked()
        self.assertTrue(blocked)
        self.assertEqual(code, "MIGRATION_BOOKING_DISABLED")

        faculty = _user(UserType.FACULTY)
        self.client.force_authenticate(faculty)
        res = self.client.post(
            "/api/portal-migration/legacy-portal/action-gate/",
            {"action": "create"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)
        self.assertEqual(res.data["code"], "MIGRATION_BOOKING_DISABLED")

        # new portal overlapping block → 409
        booker = _user(UserType.FACULTY, email=f"booker-{uuid.uuid4().hex[:6]}@staging.test")
        _wallet(booker, self.dept)
        ChargeProfile.objects.create(
            equipment=self.eq2, user_type=UserType.FACULTY, primary_unit_charge=Decimal("10"), is_active=True
        )
        # find a blocked slot on eq2
        slot = DailySlot.objects.filter(
            slot_master__equipment=self.eq2, status=SlotStatus.BLOCKED
        ).first()
        self.assertIsNotNone(slot)
        self.client.force_authenticate(booker)
        res = self.client.post(
            f"/api/equipments/{self.eq2.equipment_id}/book/",
            {"slot_ids": [slot.id], "number_of_samples": 1},
            format="json",
        )
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.data.get("code"), LEGACY_MIGRATION_SLOT_BLOCKED)

        recon = reconcile_legacy_blocks()
        self.assertTrue(recon["ok"])

        # abort
        batch = LegacyBookingMigrationBatch.objects.get(pk=result["migration_batch_id"])
        abort_migration_batch(batch)
        batch.refresh_from_db()
        self.assertEqual(batch.status, "ABORTED")
        self.assertTrue(LegacyBookingBlock.objects.filter(migration_batch=batch).exists())
        self.assertEqual(
            LegacyBookingBlock.objects.filter(
                migration_batch=batch, status=LegacyBookingBlockStatus.ACTIVE
            ).count(),
            0,
        )
        # settlements not reversed (none created — ensure model still intact)
        self.assertEqual(
            MigrationBookingSettlement.objects.filter(status=MigrationSettlementStatus.COMPLETED).count(),
            0,
        )

    def test_refund_authority_matrix_staging(self):
        faculty = _user(UserType.FACULTY)
        _wallet(faculty, self.dept, Decimal("0"))
        oic = _user(UserType.MANAGER)
        EquipmentManager.objects.create(equipment=self.eq, manager=oic)
        lab = _user(UserType.OPERATOR)
        admin = _user(UserType.ADMIN)
        profile = ChargeProfile.objects.create(
            equipment=self.eq, user_type=UserType.FACULTY, primary_unit_charge=Decimal("40")
        )
        booking = Booking.objects.create(
            user=faculty,
            equipment=self.eq,
            charge_profile=profile,
            status=BookingStatus.COMPLETED,
            total_charge=Decimal("40"),
            wallet_amount_applied=Decimal("40"),
            total_time_minutes=60,
            virtual_booking_id=f"IIC{self.eq.code}{uuid.uuid4().hex[:6]}",
        )
        state = PortalMigrationState.get_solo()
        state.end_user_booking_enabled = False
        state.phase = PortalMigrationPhase.FINANCIAL_FREEZE
        state.save()
        issue_migration_refund(booking=booking, actor=oic, reason="8c", confirm=True)
        self.client.force_authenticate(lab)
        res = self.client.post(f"/api/bookings/{booking.booking_id}/migration-refund/", {"confirm": True}, format="json")
        self.assertEqual(res.status_code, 403)
        self.client.force_authenticate(admin)
        # duplicate for same booking
        res = self.client.post(f"/api/bookings/{booking.booking_id}/migration-refund/", {"confirm": True}, format="json")
        self.assertEqual(res.status_code, 409)

    def test_email_preview_api(self):
        admin = _user(UserType.ADMIN, is_staff=True)
        self.client.force_authenticate(admin)
        res = self.client.get("/api/portal-migration/admin/email-preview/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(MigrationNotificationTemplate.FACULTY_MIGRATION, res.data)
        self.assertIn(MigrationNotificationTemplate.OIC_MIGRATION, res.data)


@override_settings(DEPLOYMENT_ENVIRONMENT="PRODUCTION")
class Phase8CProductionGuardTests(TestCase):
    def test_t0_refuses_production(self):
        with self.assertRaises(RuntimeError):
            run_staging_t0(legacy_rows=[], confirm_staging_t0=True)
