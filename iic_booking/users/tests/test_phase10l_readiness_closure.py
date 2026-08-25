"""Phase 10L — operator-gated production migration readiness tests."""

from __future__ import annotations

import uuid

from django.test import TestCase
from rest_framework.test import APIClient

from iic_booking.users.legacy_ledger.phase10g_readiness_closure import (
    VERDICT_NOT_READY,
    VERDICT_READY,
)
from iic_booking.users.legacy_ledger.phase10j_readiness_closure import inspect_operator_gates
from iic_booking.users.legacy_ledger.phase10l_readiness_closure import (
    build_migration_manifest_skeleton,
    build_phase10l_final_readiness,
    build_stage_machine,
)
from iic_booking.users.models import User
from iic_booking.users.models.user_type import UserType


def _user(user_type: str, **kwargs):
    email = kwargs.pop("email", None) or f"{user_type}-{uuid.uuid4().hex[:8]}@test.local"
    return User.objects.create_user(email=email, password="x", user_type=user_type, **kwargs)


class Phase10LStageMachineTests(TestCase):
    def test_continues_independent_stages_when_datetime_blocked(self):
        gates = inspect_operator_gates(
            datetime_validation={"ok": True, "totals": {"null_booking_date": 10, "zero_duration": 31}}
        )
        self.assertFalse(gates["discovery_allowed"])
        stages = build_stage_machine(
            gate_inspection=gates,
            discovery_executed=False,
            equipment_inventory={"count": 48},
            wallet_reconciliation={"mismatch_count": 41, "orphan_wallets": 18, "wallet_count": 1748},
            backup={"status": "BLOCKED", "backup_verified": False},
            release_plan={"push_executed": False, "reviewed_released": False},
            schema={"migrate_executed": False, "schema_migrate_authorized": False},
            dry_runs={"writes": 0, "smtp_sends": 0},
            manifest={"status": "BLOCKED_SKELETON", "full_dry_run_executed": False},
            raa={"status": "BLOCKED", "regression_executed": False},
        )
        by_name = {s["stage"]: s for s in stages}
        self.assertEqual(by_name["02_datetime_contract"]["status"], "OPERATOR_REQUIRED")
        self.assertEqual(by_name["04_production_discovery"]["status"], "BLOCKED")
        self.assertEqual(by_name["06_wallet_finance_ro"]["status"], "COMPLETE_RO")
        self.assertEqual(by_name["08_release_candidate_prep"]["status"], "PREP_COMPLETE_PUSH_STOPPED")
        self.assertTrue(by_name["06_wallet_finance_ro"]["safe_work_done"])
        self.assertFalse(by_name["02_datetime_contract"]["post_called"])

    def test_manifest_skeleton_blocked_but_useful(self):
        gates = inspect_operator_gates(datetime_validation={"ok": True, "totals": {}})
        manifest = build_migration_manifest_skeleton(
            gate_inspection=gates,
            discovery_status="DISCOVERY_BLOCKED_BY_DATETIME_APPROVAL",
            discovery_executed=False,
            wallet_reconciliation={"mismatch_count": 41, "wallet_count": 1748},
            equipment_inventory={"count": 48},
        )
        self.assertEqual(manifest["status"], "BLOCKED_SKELETON")
        self.assertFalse(manifest["full_dry_run_executed"])
        self.assertFalse(manifest["t0_included"])
        self.assertGreaterEqual(len(manifest["operator_checklist"]), 8)
        self.assertTrue(any(s["section"] == "legacy_mysql_ro_baseline" for s in manifest["completed_prep_sections"]))


class Phase10LGoNoGoTests(TestCase):
    def test_default_verdict_blockers_remain(self):
        report = build_phase10l_final_readiness(
            datetime_validation={
                "ok": True,
                "contract_approval_status": "OPERATOR_REQUIRED",
                "totals": {"null_booking_date": 10, "zero_duration": 31},
            },
            mysql_probe={"ok": True, "row_counts": {"users": 4337}, "live_financial_audit": {}},
            wallet_reconciliation={"mismatch_count": 41, "orphan_wallets": 18, "wallet_count": 1748},
            equipment_inventory={"count": 48},
        )
        self.assertEqual(report["verdict"], VERDICT_NOT_READY)
        self.assertNotEqual(report["verdict"], "READY FOR T0")
        self.assertNotEqual(report["verdict"], VERDICT_READY)
        self.assertEqual(report["phase"], "10L")
        self.assertFalse(report["t0_executed"])
        self.assertFalse(report["discovery_executed"])
        self.assertIn("datetime_unapproved", report["hard_refuse_reasons"])
        self.assertIn("migration_window_missing", report["hard_refuse_reasons"])
        self.assertEqual(report["production_safety"]["T0"], "NO")
        self.assertEqual(report["production_safety"]["DATETIME_CONTRACT_POST"], "NO")
        self.assertEqual(report["production_safety"]["OPENING_BALANCES"], "NO")
        self.assertTrue(any(s["stage"] == "06_wallet_finance_ro" for s in report["stage_machine"]))
        self.assertEqual(report["migration_manifest"]["status"], "BLOCKED_SKELETON")
        self.assertIn("RAA Booking Regression", report["gate_matrix"])


class Phase10LApiTests(TestCase):
    def setUp(self):
        self.admin = _user(UserType.ADMIN)
        self.faculty = _user(UserType.FACULTY)
        self.client = APIClient()

    def test_go_no_go_main_admin_only(self):
        self.client.force_authenticate(user=self.faculty)
        res = self.client.get("/api/portal-migration/admin/phase10l-go-no-go/")
        self.assertEqual(res.status_code, 403)

        self.client.force_authenticate(user=self.admin)
        res = self.client.get("/api/portal-migration/admin/phase10l-go-no-go/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data.get("phase"), "10L")
        self.assertFalse(res.data.get("t0_executed"))
        self.assertEqual(res.data.get("verdict"), VERDICT_NOT_READY)
        self.assertIn("stage_machine", res.data)
        self.assertIn("Migration Window", res.data.get("gate_matrix") or {})
