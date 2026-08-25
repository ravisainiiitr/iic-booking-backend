"""Phase 10E — production qualification preparation tests."""

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
from iic_booking.users.legacy_ledger.datetime_contract import (
    APPROVAL_APPROVED,
    APPROVAL_OPERATORS_REQUIRED,
    contract_approval_status,
    datetime_contract_ui_payload,
    load_datetime_contract,
    validate_contract_for_discovery,
)
from iic_booking.users.legacy_ledger.legacy_conflict_analysis import analyze_booking_conflicts
from iic_booking.users.legacy_ledger.legacy_equipment_mapping_import import (
    validate_equipment_mapping_file,
)
from iic_booking.users.legacy_ledger.legacy_upcoming_discovery import discover_upcoming_legacy_week
from iic_booking.users.legacy_ledger.test_account_dry_run import test_account_cleanup_dry_run
from iic_booking.users.legacy_ledger.booking_bridge import arm_legacy_block, discover_legacy_bookings
from iic_booking.users.legacy_ledger.legacy_user_resolution import resolve_legacy_blocks_for_channel_i_user
from iic_booking.users.management.commands.migration_production_legacy_qualification import build_phase10_report
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
        name=f"10E-{uuid.uuid4().hex[:6]}",
        code=f"E{uuid.uuid4().hex[:3].upper()}",
        department_type=DepartmentType.INTERNAL,
    )


def _equipment(dept):
    return Equipment.objects.create(
        name="10E EQ",
        code=f"E10{uuid.uuid4().hex[:3].upper()}",
        internal_department=dept,
        slot_duration_minutes=60,
        status=EquipmentStatus.ACTIVE,
    )


def _row(**kw):
    start = timezone.now() + timedelta(days=4)
    base = {
        "legacy_booking_id": 7001,
        "old_equipment_id": 101,
        "start_at": start,
        "end_at": start + timedelta(hours=2),
        "status": "CONFIRMED",
        "employee_id": "EMP10E",
        "legacy_user_id": 1,
    }
    base.update(kw)
    return base


class DatetimeContractTests(TestCase):
    def test_operator_required_blocks_discovery(self):
        contract = load_datetime_contract()
        if contract.get("ok"):
            self.assertEqual(contract_approval_status(contract), APPROVAL_OPERATORS_REQUIRED)
            gate = validate_contract_for_discovery(contract)
            self.assertFalse(gate["ready_for_discovery"])
        ui = datetime_contract_ui_payload(contract if contract.get("ok") else {"_status": "OPERATOR_REQUIRED"})
        self.assertTrue(ui["blocks_t0"])

    def test_approved_contract_allows_discovery_gate(self):
        approved = {
            "_status": APPROVAL_APPROVED,
            "approved_by": "operator@test",
            "approved_at_utc": "2026-08-22T00:00:00Z",
            "booking_date_column": "booking_date",
            "duration_column": "time_required",
            "datetime_strategy": "CANDIDATE_BOOKING_DATE_DATETIME_PLUS_DURATION",
            "time_required_semantics": "MINUTES",
            "booking_id": "id",
            "user_id": "user_id",
            "equipment_id": "equipment_id",
            "status_column": "status",
        }
        gate = validate_contract_for_discovery(approved)
        self.assertTrue(gate["ready_for_discovery"])


class DiscoveryAndConflictTests(TestCase):
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
        state.save()

    def test_unresolved_user_valid_slot_ready(self):
        disc = discover_legacy_bookings([_row(employee_id="NO_USER")])
        self.assertEqual(disc["counts"]["eligible"], 1)
        self.assertEqual(disc["eligible"][0]["user_mapping_status"], LegacyUserMappingStatus.UNRESOLVED)

    def test_unmapped_equipment_not_ready(self):
        disc = discover_legacy_bookings([_row(old_equipment_id=9999)])
        self.assertEqual(disc["counts"]["unmapped"], 1)

    def test_conflict_detection_readonly(self):
        start = timezone.now() + timedelta(days=5)
        rows = [
            _row(legacy_booking_id=1, start_at=start, end_at=start + timedelta(hours=1)),
            _row(legacy_booking_id=2, start_at=start, end_at=start + timedelta(hours=1)),
        ]
        report = analyze_booking_conflicts(rows)
        self.assertGreaterEqual(report["conflict_count"], 1)

    def test_upcoming_discovery_blocks_without_approved_contract(self):
        report = discover_upcoming_legacy_week()
        self.assertFalse(report.get("ok"))
        self.assertEqual(report.get("error"), "datetime_contract_operator_required")

    def test_upcoming_discovery_with_fixture_rows(self):
        report = discover_upcoming_legacy_week(legacy_rows=[_row()])
        self.assertTrue(report.get("ok"))
        self.assertEqual(report.get("blocks_created"), 0)


class EquipmentMappingImportTests(TestCase):
    def setUp(self):
        self.dept = _dept()
        self.eq = _equipment(self.dept)

    def test_explicit_mapping_validation(self):
        report = validate_equipment_mapping_file(
            [{"legacy_equipment_id": 55, "new_equipment_id": self.eq.equipment_id, "approved_by": "admin"}],
            required_legacy_ids={55},
        )
        self.assertTrue(report["valid"])

    def test_unmapped_required_blocks(self):
        report = validate_equipment_mapping_file([], required_legacy_ids={99})
        self.assertFalse(report["valid"])
        self.assertIn(99, report["missing_required_legacy_ids"])


class EnrichmentAndDryRunTests(TestCase):
    def setUp(self):
        self.dept = _dept()
        self.eq = _equipment(self.dept)
        self.start = (timezone.now() + timedelta(days=7)).replace(minute=0, second=0, microsecond=0)
        self.end = self.start + timedelta(hours=1)

    def test_user_enrichment_does_not_change_slot(self):
        user = _user(UserType.FACULTY, emp_id="ENRICH10E")
        block = arm_legacy_block(
            legacy_booking_id=7100,
            equipment=self.eq,
            start_at=self.start,
            end_at=self.end,
            legacy_employee_id="ENRICH10E",
            user_mapping_status=LegacyUserMappingStatus.UNRESOLVED,
        )
        slots_before = list(block.slot_ids or [])
        resolve_legacy_blocks_for_channel_i_user(user)
        block.refresh_from_db()
        self.assertEqual(block.slot_ids, slots_before)

    def test_test_account_dry_run_zero_writes(self):
        _user(UserType.STUDENT, is_test_account=True)
        report = test_account_cleanup_dry_run()
        self.assertEqual(report["writes_performed"], 0)
        self.assertGreaterEqual(report["test_users"], 1)


class QualificationReportTests(TestCase):
    def test_build_phase10_report_not_ready(self):
        report = build_phase10_report()
        self.assertEqual(report["phase"], "10E")
        self.assertEqual(report["audit_mode"], "READ_ONLY")
        self.assertEqual(report["verdict"], "NOT READY — BLOCKERS LISTED")
        self.assertIn("datetime_contract_operator_required", report["blockers"])


class ApiTests(TestCase):
    def setUp(self):
        self.admin = _user(UserType.ADMIN)
        self.oic = _user(UserType.MANAGER)
        self.dept = _dept()
        self.eq = _equipment(self.dept)
        EquipmentManager.objects.create(equipment=self.eq, manager=self.oic)
        self.client = APIClient()

    def test_datetime_contract_api(self):
        self.client.force_authenticate(user=self.admin)
        res = self.client.get("/api/portal-migration/admin/datetime-contract/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("approval_status", res.data)

    def test_oic_can_view_scoped_equipment_mappings(self):
        LegacyEquipmentMapping.objects.create(
            old_equipment_id=201,
            new_equipment=self.eq,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        self.client.force_authenticate(user=self.oic)
        res = self.client.get("/api/portal-migration/admin/equipment-mappings/")
        self.assertEqual(res.status_code, 200)

    def test_admin_unmap_and_retire_and_delete_mapping(self):
        m = LegacyEquipmentMapping.objects.create(
            old_equipment_id=8801,
            old_equipment_name="Gone Instrument",
            new_equipment=self.eq,
            status=LegacyEquipmentMappingStatus.ACTIVE,
        )
        self.client.force_authenticate(user=self.admin)
        unmap = self.client.patch(
            f"/api/portal-migration/admin/equipment-mappings/{m.id}/",
            {"status": "UNMAPPED", "new_equipment_id": None},
            format="json",
        )
        self.assertEqual(unmap.status_code, 200, unmap.content)
        m.refresh_from_db()
        self.assertEqual(m.status, LegacyEquipmentMappingStatus.UNMAPPED)
        self.assertIsNone(m.new_equipment_id)

        retire = self.client.patch(
            f"/api/portal-migration/admin/equipment-mappings/{m.id}/",
            {
                "status": "RETIRED",
                "new_equipment_id": None,
                "mapping_reason": "no longer exists",
            },
            format="json",
        )
        self.assertEqual(retire.status_code, 200, retire.content)
        m.refresh_from_db()
        self.assertEqual(m.status, LegacyEquipmentMappingStatus.RETIRED)

        deleted = self.client.delete(f"/api/portal-migration/admin/equipment-mappings/{m.id}/")
        self.assertEqual(deleted.status_code, 200, deleted.content)
        self.assertFalse(LegacyEquipmentMapping.objects.filter(pk=m.id).exists())

    def test_test_account_dry_run_api(self):
        self.client.force_authenticate(user=self.admin)
        res = self.client.get("/api/portal-migration/admin/test-account-dry-run/")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data.get("dry_run"))
