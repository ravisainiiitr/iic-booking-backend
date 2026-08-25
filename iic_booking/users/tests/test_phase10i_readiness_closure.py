"""Phase 10I — readiness closure GO/NO-GO and hard-gate refusal tests."""

from __future__ import annotations

import uuid

from django.test import TestCase
from rest_framework.test import APIClient

from iic_booking.users.legacy_ledger.phase10g_readiness_closure import (
    VERDICT_NOT_READY,
    VERDICT_READY,
)
from iic_booking.users.legacy_ledger.phase10i_readiness_closure import (
    build_datetime_review,
    build_phase10i_final_readiness,
)
from iic_booking.users.models import User
from iic_booking.users.models.user_type import UserType


def _user(user_type: str, **kwargs):
    email = kwargs.pop("email", None) or f"{user_type}-{uuid.uuid4().hex[:8]}@test.local"
    return User.objects.create_user(email=email, password="x", user_type=user_type, **kwargs)


class Phase10IDatetimeReviewTests(TestCase):
    def test_null_dates_excluded_zero_manual_review(self):
        review = build_datetime_review(
            datetime_validation={
                "ok": True,
                "totals": {
                    "total_bookings": 58063,
                    "null_booking_date": 10,
                    "zero_duration": 31,
                },
                "migration_window": {"configured": False},
            }
        )
        self.assertEqual(review["DATETIME_CONTRACT"], "OPERATOR_REQUIRED")
        self.assertFalse(review["approval_endpoint_called"])
        self.assertEqual(review["null_booking_date"]["classification_default"], "EXCLUDED")
        self.assertEqual(review["zero_duration"]["classification_default"], "MANUAL_REVIEW")
        self.assertIn("OPERATOR_REQUIRED", review["stop_condition"])


class Phase10IGoNoGoTests(TestCase):
    def test_default_verdict_not_ready(self):
        report = build_phase10i_final_readiness(
            datetime_validation={"ok": True, "contract_approval_status": "OPERATOR_REQUIRED", "totals": {}},
            mysql_probe={"ok": True, "row_counts": {}, "live_financial_audit": {}},
        )
        self.assertEqual(report["verdict"], VERDICT_NOT_READY)
        self.assertFalse(report["t0_executed"])
        self.assertNotEqual(report["verdict"], "READY FOR T0")
        self.assertEqual(report["phase"], "10I")
        matrix = report["gate_matrix"]
        self.assertIn("Migration Window", matrix)
        self.assertIn("Datetime", matrix)
        self.assertIn("Backup", matrix)
        self.assertIn("T0 Authorization", matrix)
        self.assertIn("datetime_unapproved", report["hard_refuse_reasons"])
        self.assertIn("migration_window_missing", report["hard_refuse_reasons"])
        self.assertIn("backup_unverified", report["hard_refuse_reasons"])
        self.assertEqual(report["production_safety"]["T0"], "NO")
        self.assertEqual(report["production_safety"]["PRODUCTION_MIGRATE"], "NO")
        self.assertEqual(report["discovery_executed"], False)

    def test_backup_flag_alone_insufficient(self):
        report = build_phase10i_final_readiness(
            backup_verified=True,
            datetime_validation={"ok": True, "contract_approval_status": "OPERATOR_REQUIRED", "totals": {}},
            mysql_probe={"ok": True, "live_financial_audit": {}},
            release_plan={"reviewed_released": True, "local_backend_sha": "x", "local_frontend_sha": "y"},
        )
        self.assertEqual(report["verdict"], VERDICT_NOT_READY)
        self.assertNotEqual(report["verdict"], VERDICT_READY)
        self.assertEqual(report["gate_matrix"]["Backup"]["result"], "PASS")

    def test_user_unresolved_non_blocking(self):
        report = build_phase10i_final_readiness(
            datetime_validation={"ok": True, "totals": {}},
            mysql_probe={"ok": True, "live_financial_audit": {"users_total": 1}},
        )
        self.assertFalse(report["gate_matrix"]["Users"]["blocking"])


class Phase10IApiTests(TestCase):
    def setUp(self):
        self.admin = _user(UserType.ADMIN)
        self.faculty = _user(UserType.FACULTY)
        self.client = APIClient()

    def test_go_no_go_main_admin_only(self):
        self.client.force_authenticate(user=self.faculty)
        res = self.client.get("/api/portal-migration/admin/phase10i-go-no-go/")
        self.assertEqual(res.status_code, 403)

        self.client.force_authenticate(user=self.admin)
        res = self.client.get("/api/portal-migration/admin/phase10i-go-no-go/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data.get("phase"), "10I")
        self.assertFalse(res.data.get("t0_executed"))
        self.assertEqual(res.data.get("verdict"), VERDICT_NOT_READY)
        self.assertIn("Migration Window", res.data.get("gate_matrix") or {})
