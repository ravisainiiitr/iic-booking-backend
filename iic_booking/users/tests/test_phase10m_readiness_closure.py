"""Phase 10M — operator gate clearance checkpoint tests."""

from __future__ import annotations

import uuid

from django.test import TestCase
from rest_framework.test import APIClient

from iic_booking.users.legacy_ledger.phase10g_readiness_closure import VERDICT_READY
from iic_booking.users.legacy_ledger.phase10j_readiness_closure import inspect_operator_gates
from iic_booking.users.legacy_ledger.phase10m_readiness_closure import (
    VERDICT_OPERATOR_GATES,
    build_phase10m_final_readiness,
    maybe_run_production_discovery,
)
from iic_booking.users.models import User
from iic_booking.users.models.user_type import UserType


def _user(user_type: str, **kwargs):
    email = kwargs.pop("email", None) or f"{user_type}-{uuid.uuid4().hex[:8]}@test.local"
    return User.objects.create_user(email=email, password="x", user_type=user_type, **kwargs)


class Phase10MClearanceTests(TestCase):
    def test_discovery_not_attempted_without_gates(self):
        result = maybe_run_production_discovery(discovery_allowed=False)
        self.assertFalse(result["executed"])
        self.assertFalse(result["attempted"])

    def test_verdict_operator_gates_when_datetime_window_missing(self):
        report = build_phase10m_final_readiness(
            datetime_validation={
                "ok": True,
                "contract_approval_status": "OPERATOR_REQUIRED",
                "totals": {"null_booking_date": 10, "zero_duration": 31},
            },
            mysql_probe={"ok": True, "row_counts": {}, "live_financial_audit": {}},
            wallet_reconciliation={"mismatch_count": 41, "orphan_wallets": 18},
            equipment_inventory={"count": 48},
            auto_discovery_result={"executed": False, "attempted": False},
        )
        self.assertEqual(report["verdict"], VERDICT_OPERATOR_GATES)
        self.assertNotEqual(report["verdict"], "READY FOR T0")
        self.assertNotEqual(report["verdict"], VERDICT_READY)
        self.assertEqual(report["phase"], "10M")
        self.assertFalse(report["discovery_executed"])
        self.assertIn("datetime_contract", report["gates_not_cleared"])
        self.assertIn("migration_window", report["gates_not_cleared"])
        self.assertIn("legacy_mysql_ro", report["gates_cleared"])
        self.assertEqual(report["production_safety"]["DATETIME_CONTRACT_POST"], "NO")
        self.assertFalse(report["auto_discovery"]["attempted"])

    def test_inspect_gates_live_defaults(self):
        gates = inspect_operator_gates(datetime_validation={"ok": True, "totals": {}})
        self.assertFalse(gates["discovery_allowed"])
        self.assertFalse(gates["datetime_contract_approval"]["post_datetime_contract_called"])


class Phase10MApiTests(TestCase):
    def setUp(self):
        self.admin = _user(UserType.ADMIN)
        self.faculty = _user(UserType.FACULTY)
        self.client = APIClient()

    def test_go_no_go_main_admin_only(self):
        self.client.force_authenticate(user=self.faculty)
        res = self.client.get("/api/portal-migration/admin/phase10m-go-no-go/")
        self.assertEqual(res.status_code, 403)

        self.client.force_authenticate(user=self.admin)
        res = self.client.get("/api/portal-migration/admin/phase10m-go-no-go/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data.get("phase"), "10M")
        self.assertEqual(res.data.get("verdict"), VERDICT_OPERATOR_GATES)
        self.assertFalse(res.data.get("t0_executed"))
        self.assertIn("gates_cleared", res.data)
        self.assertIn("remaining_operator_actions", res.data)
