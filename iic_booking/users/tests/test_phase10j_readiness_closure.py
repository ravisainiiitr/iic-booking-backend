"""Phase 10J — operator-gated readiness closure and hard-gate refusal tests."""

from __future__ import annotations

import uuid

from django.test import TestCase
from rest_framework.test import APIClient

from iic_booking.users.legacy_ledger.phase10g_readiness_closure import (
    VERDICT_NOT_READY,
    VERDICT_READY,
)
from iic_booking.users.legacy_ledger.phase10j_readiness_closure import (
    build_phase10j_final_readiness,
    inspect_operator_gates,
)
from iic_booking.users.models import User
from iic_booking.users.models.user_type import UserType


def _user(user_type: str, **kwargs):
    email = kwargs.pop("email", None) or f"{user_type}-{uuid.uuid4().hex[:8]}@test.local"
    return User.objects.create_user(email=email, password="x", user_type=user_type, **kwargs)


class Phase10JGateInspectionTests(TestCase):
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
        self.assertEqual(
            gates["datetime_exception_classifications"]["null_booking_date"]["classification"],
            "EXCLUDED",
        )
        self.assertEqual(
            gates["datetime_exception_classifications"]["zero_duration"]["classification"],
            "MANUAL_REVIEW",
        )


class Phase10JGoNoGoTests(TestCase):
    def test_default_verdict_not_ready(self):
        report = build_phase10j_final_readiness(
            datetime_validation={
                "ok": True,
                "contract_approval_status": "OPERATOR_REQUIRED",
                "totals": {"null_booking_date": 10, "zero_duration": 31},
            },
            mysql_probe={"ok": True, "row_counts": {}, "live_financial_audit": {}},
        )
        self.assertEqual(report["verdict"], VERDICT_NOT_READY)
        self.assertNotEqual(report["verdict"], "READY FOR T0")
        self.assertEqual(report["phase"], "10J")
        self.assertFalse(report["t0_executed"])
        self.assertFalse(report["discovery_executed"])
        self.assertIn("datetime_unapproved", report["hard_refuse_reasons"])
        self.assertIn("migration_window_missing", report["hard_refuse_reasons"])
        self.assertEqual(report["production_safety"]["T0"], "NO")
        self.assertEqual(report["production_safety"]["DATETIME_CONTRACT_POST"], "NO")
        self.assertEqual(report["production_safety"]["MIGRATION_WINDOW_DATES_INVENTED"], "NO")
        self.assertIn("Datetime", report["gate_matrix"])
        self.assertIn("Migration Window", report["gate_matrix"])
        self.assertEqual(report["gate_matrix"]["Upcoming Bookings"]["result"], "BLOCKED")

    def test_backup_and_release_flags_alone_insufficient(self):
        report = build_phase10j_final_readiness(
            backup_verified=True,
            finance_reviewed=True,
            datetime_validation={"ok": True, "totals": {}},
            mysql_probe={"ok": True, "live_financial_audit": {}},
            release_plan={"reviewed_released": True, "local_backend_sha": "x", "local_frontend_sha": "y"},
        )
        self.assertEqual(report["verdict"], VERDICT_NOT_READY)
        self.assertNotEqual(report["verdict"], VERDICT_READY)
        self.assertEqual(report["gate_matrix"]["Backup"]["result"], "PASS")
        self.assertIn("datetime_unapproved", report["hard_refuse_reasons"])

    def test_user_unresolved_non_blocking(self):
        report = build_phase10j_final_readiness(
            datetime_validation={"ok": True, "totals": {}},
            mysql_probe={"ok": True, "live_financial_audit": {"users_total": 1}},
        )
        self.assertFalse(report["gate_matrix"]["Users"]["blocking"])


class Phase10JApiTests(TestCase):
    def setUp(self):
        self.admin = _user(UserType.ADMIN)
        self.faculty = _user(UserType.FACULTY)
        self.client = APIClient()

    def test_go_no_go_main_admin_only(self):
        self.client.force_authenticate(user=self.faculty)
        res = self.client.get("/api/portal-migration/admin/phase10j-go-no-go/")
        self.assertEqual(res.status_code, 403)

        self.client.force_authenticate(user=self.admin)
        res = self.client.get("/api/portal-migration/admin/phase10j-go-no-go/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data.get("phase"), "10J")
        self.assertFalse(res.data.get("t0_executed"))
        self.assertFalse(res.data.get("discovery_executed"))
        self.assertEqual(res.data.get("verdict"), VERDICT_NOT_READY)
        self.assertIn("Migration Window", res.data.get("gate_matrix") or {})
        self.assertIn("operator_gate_inspection", res.data)
