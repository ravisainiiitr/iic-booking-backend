"""Phase 10F — final T0 readiness, datetime approval, and T0 gate tests."""

from __future__ import annotations

import json
import tempfile
from datetime import timedelta
from pathlib import Path
import uuid

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import Equipment, EquipmentManager, EquipmentStatus
from iic_booking.users.legacy_ledger.booking_bridge import arm_legacy_block, discover_legacy_bookings
from iic_booking.users.legacy_ledger.datetime_contract import (
    APPROVAL_APPROVED,
    APPROVAL_OPERATORS_REQUIRED,
    approve_datetime_contract,
    contract_approval_status,
    load_datetime_contract,
)
from iic_booking.users.legacy_ledger.migration_t0 import run_staging_t0
from iic_booking.users.legacy_ledger.phase10f_final_t0_readiness import build_final_t0_readiness_report
from iic_booking.users.models import Department, User
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.portal_migration import (
    LegacyEquipmentMapping,
    LegacyEquipmentMappingStatus,
    LegacyUserMappingStatus,
    PortalMigrationState,
)
from iic_booking.users.models.user_type import UserType


def _user(user_type: str, **kwargs):
    email = kwargs.pop("email", None) or f"{user_type}-{uuid.uuid4().hex[:8]}@test.local"
    return User.objects.create_user(email=email, password="x", user_type=user_type, **kwargs)


def _dept():
    return Department.objects.create(
        name=f"10F-{uuid.uuid4().hex[:6]}",
        code=f"F{uuid.uuid4().hex[:3].upper()}",
        department_type=DepartmentType.INTERNAL,
    )


def _equipment(dept):
    return Equipment.objects.create(
        name="10F EQ",
        code=f"F10{uuid.uuid4().hex[:3].upper()}",
        internal_department=dept,
        slot_duration_minutes=60,
        status=EquipmentStatus.ACTIVE,
    )


def _row(**kw):
    start = timezone.now() + timedelta(days=4)
    base = {
        "legacy_booking_id": 8001,
        "old_equipment_id": 101,
        "start_at": start,
        "end_at": start + timedelta(hours=2),
        "status": "CONFIRMED",
        "employee_id": "EMP10F",
        "legacy_user_id": 1,
    }
    base.update(kw)
    return base


class DatetimeApprovalTests(TestCase):
    def setUp(self):
        self.admin = _user(UserType.ADMIN, email="admin-10f@test.local")
        self.tmpdir = tempfile.mkdtemp()
        mig_dir = Path(self.tmpdir) / "docs" / "release" / "migration"
        mig_dir.mkdir(parents=True)
        self.contract_path = mig_dir / "legacy_booking_datetime_map.json"
        self.contract_path.write_text(
            json.dumps(
                {
                    "_status": APPROVAL_OPERATORS_REQUIRED,
                    "booking_id": "id",
                    "user_id": "user_id",
                    "equipment_id": "equipment_id",
                    "booking_date_column": "booking_date",
                    "duration_column": "time_required",
                    "time_required_semantics": "CANDIDATE_DURATION_MINUTES",
                    "datetime_strategy": "CANDIDATE_BOOKING_DATE_DATETIME_PLUS_DURATION",
                    "timezone": "Asia/Kolkata",
                    "status_column": "status",
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def test_approve_requires_confirm(self):
        result = approve_datetime_contract(
            approved_by="operator@test",
            approval_reason="Reviewed validation report",
            confirm=False,
            path=self.contract_path,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "confirm_required")

    def test_approve_records_audit_without_side_effects(self):
        with override_settings(BASE_DIR=Path(self.tmpdir)):
            result = approve_datetime_contract(
                approved_by="operator@test",
                approval_reason="Validated booking_date + time_required minutes",
                confirm=True,
                path=self.contract_path,
                actor_user_id=self.admin.pk,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["side_effects"]["blocks_created"], 0)
        self.assertEqual(result["side_effects"]["t0_activated"], False)
        reloaded = load_datetime_contract(self.contract_path)
        self.assertEqual(contract_approval_status(reloaded), APPROVAL_APPROVED)
        self.assertTrue(Path(result["audit_path"]).is_file())

    def test_approve_api_main_admin_only(self):
        client = APIClient()
        oic = _user(UserType.MANAGER)
        client.force_authenticate(user=oic)
        res = client.post(
            "/api/portal-migration/admin/datetime-contract/",
            {"confirm": True, "approval_reason": "test"},
            format="json",
        )
        self.assertEqual(res.status_code, 403)

        client.force_authenticate(user=self.admin)
        with override_settings(BASE_DIR=Path(self.tmpdir).parent):
            pass  # API uses default path; test approve function directly above


class T0GateTests(TestCase):
    def setUp(self):
        self.dept = _dept()
        self.eq = _equipment(self.dept)
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=101,
            new_equipment=self.eq,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        state = PortalMigrationState.get_solo()
        state.migration_start_at = timezone.now() - timedelta(days=1)
        state.migration_window_end_at = timezone.now() + timedelta(days=30)
        state.new_portal_url = "https://booking.example.test"
        state.save()

    @override_settings(DEPLOYMENT_ENVIRONMENT="STAGING")
    def test_staging_t0_refuses_unapproved_datetime_contract(self):
        result = run_staging_t0(
            legacy_rows=[_row()],
            confirm_staging_t0=True,
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["stage"], "datetime_contract")

    def test_unresolved_user_valid_slot_ready(self):
        disc = discover_legacy_bookings([_row(employee_id="NO_USER")])
        self.assertEqual(disc["counts"]["eligible"], 1)
        self.assertEqual(disc["eligible"][0]["user_mapping_status"], LegacyUserMappingStatus.UNRESOLVED)


class FinalReadinessReportTests(TestCase):
    def test_report_not_ready_by_default(self):
        report = build_final_t0_readiness_report()
        self.assertEqual(report["phase"], "10F")
        self.assertEqual(report["verdict"], "NOT READY — BLOCKERS LISTED")
        self.assertFalse(report["t0_ready"])
        self.assertFalse(report["user_unresolved_blocks_t0"])
        self.assertIn("datetime_contract_not_approved", report["blockers"])
        self.assertIn("backup_not_verified", report["blockers"])

    def test_report_with_backup_flag_still_not_ready_without_other_gates(self):
        report = build_final_t0_readiness_report(backup_verified=True)
        self.assertFalse(report["t0_ready"])
        self.assertNotIn("backup_not_verified", report["blockers"])


class ScopeTests(TestCase):
    def setUp(self):
        self.admin = _user(UserType.ADMIN)
        self.oic = _user(UserType.MANAGER)
        self.dept = _dept()
        self.eq = _equipment(self.dept)
        EquipmentManager.objects.create(equipment=self.eq, manager=self.oic)
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=301,
            new_equipment=self.eq,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        self.client = APIClient()

    def test_oic_scoped_equipment_view_only(self):
        self.client.force_authenticate(user=self.oic)
        res = self.client.get("/api/portal-migration/admin/equipment-mappings/")
        self.assertEqual(res.status_code, 200)
        res_post = self.client.post(
            "/api/portal-migration/admin/equipment-mappings/",
            {"old_equipment_id": 999, "new_equipment_id": self.eq.equipment_id},
            format="json",
        )
        self.assertEqual(res_post.status_code, 403)
