"""Phase 10K — operator gate execution readiness + hard-gate refusal tests."""

from __future__ import annotations

import uuid

from django.test import TestCase
from rest_framework.test import APIClient

from iic_booking.users.legacy_ledger.phase10g_readiness_closure import VERDICT_READY
from iic_booking.users.legacy_ledger.phase10j_readiness_closure import inspect_operator_gates
from iic_booking.users.legacy_ledger.phase10k_readiness_closure import (
    VERDICT_NOT_READY_OPERATOR_GATES,
    blocked_discovery_artifact,
    build_phase10k_final_readiness,
    confirm_0102_provides_migration_start_at,
)
from iic_booking.users.models import User
from iic_booking.users.models.user_type import UserType


def _user(user_type: str, **kwargs):
    email = kwargs.pop("email", None) or f"{user_type}-{uuid.uuid4().hex[:8]}@test.local"
    return User.objects.create_user(email=email, password="x", user_type=user_type, **kwargs)


class Phase10KGateInspectionTests(TestCase):
    def test_datetime_and_window_operator_required(self):
        gates = inspect_operator_gates(
            datetime_validation={
                "ok": True,
                "totals": {"null_booking_date": 10, "zero_duration": 31},
            }
        )
        self.assertTrue(gates["datetime_contract_approval"]["operator_required"])
        self.assertTrue(gates["migration_window"]["operator_required"])
        self.assertFalse(gates["discovery_allowed"])
        self.assertFalse(gates["datetime_contract_approval"]["post_datetime_contract_called"])

    def test_0102_provides_migration_start_at(self):
        info = confirm_0102_provides_migration_start_at()
        self.assertTrue(info["migration_start_at_confirmed"])
        self.assertIn("PortalMigrationState.migration_start_at", info["provides_fields"])
        self.assertFalse(info["migrate_executed"])

    def test_blocked_discovery_artifact(self):
        art = blocked_discovery_artifact(
            datetime_status="OPERATOR_REQUIRED",
            window_configured=False,
        )
        self.assertFalse(art["executed"])
        self.assertEqual(art["status"], "DISCOVERY_BLOCKED_BY_DATETIME_APPROVAL")
        self.assertEqual(art["writes"], 0)


class Phase10KGoNoGoTests(TestCase):
    def test_default_verdict_operator_gates_remain(self):
        report = build_phase10k_final_readiness(
            datetime_validation={
                "ok": True,
                "contract_approval_status": "OPERATOR_REQUIRED",
                "totals": {"null_booking_date": 10, "zero_duration": 31},
            },
            mysql_probe={"ok": True, "row_counts": {}, "live_financial_audit": {}},
        )
        self.assertEqual(report["verdict"], VERDICT_NOT_READY_OPERATOR_GATES)
        self.assertNotEqual(report["verdict"], "READY FOR T0")
        self.assertNotEqual(report["verdict"], VERDICT_READY)
        self.assertEqual(report["phase"], "10K")
        self.assertFalse(report["t0_executed"])
        self.assertFalse(report["discovery_executed"])
        self.assertIn("datetime_unapproved", report["hard_refuse_reasons"])
        self.assertIn("migration_window_missing", report["hard_refuse_reasons"])
        self.assertEqual(report["production_safety"]["T0"], "NO")
        self.assertEqual(report["production_safety"]["DATETIME_CONTRACT_POST"], "NO")
        self.assertEqual(report["production_safety"]["MIGRATION_WINDOW_DATES_INVENTED"], "NO")
        self.assertEqual(report["production_safety"]["RAA_PATCH_AROUND_0102"], "NO")
        self.assertEqual(report["gate_matrix"]["Upcoming Bookings"]["result"], "BLOCKED")
        self.assertEqual(report["gate_matrix"]["RAA Booking Regression"]["result"], "BLOCKED")
        self.assertTrue(report["users_0102_migration_start_at"]["migration_start_at_confirmed"])
        self.assertFalse((report.get("discovery_artifact") or {}).get("executed"))

    def test_backup_and_release_flags_alone_insufficient(self):
        report = build_phase10k_final_readiness(
            backup_verified=True,
            finance_reviewed=True,
            datetime_validation={"ok": True, "totals": {}},
            mysql_probe={"ok": True, "live_financial_audit": {}},
            release_plan={"reviewed_released": True, "local_backend_sha": "x", "local_frontend_sha": "y"},
        )
        self.assertEqual(report["verdict"], VERDICT_NOT_READY_OPERATOR_GATES)
        self.assertEqual(report["gate_matrix"]["Backup"]["result"], "PASS")
        self.assertIn("datetime_unapproved", report["hard_refuse_reasons"])


class Phase10KApiTests(TestCase):
    def setUp(self):
        self.admin = _user(UserType.ADMIN)
        self.faculty = _user(UserType.FACULTY)
        self.client = APIClient()

    def test_go_no_go_main_admin_only(self):
        self.client.force_authenticate(user=self.faculty)
        res = self.client.get("/api/portal-migration/admin/phase10k-go-no-go/")
        self.assertEqual(res.status_code, 403)

        self.client.force_authenticate(user=self.admin)
        res = self.client.get("/api/portal-migration/admin/phase10k-go-no-go/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data.get("phase"), "10K")
        self.assertFalse(res.data.get("t0_executed"))
        self.assertFalse(res.data.get("discovery_executed"))
        self.assertEqual(res.data.get("verdict"), VERDICT_NOT_READY_OPERATOR_GATES)
        self.assertIn("Migration Window", res.data.get("gate_matrix") or {})
        self.assertIn("operator_gate_inspection", res.data)
        self.assertIn("RAA Booking Regression", res.data.get("gate_matrix") or {})
