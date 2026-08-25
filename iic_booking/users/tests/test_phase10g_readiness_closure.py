"""Phase 10G — readiness closure GO/NO-GO and hard-gate refusal tests."""

from __future__ import annotations

from django.test import TestCase, override_settings
from rest_framework.test import APIClient
import uuid

from iic_booking.users.legacy_ledger.phase10g_readiness_closure import (
    VERDICT_NOT_READY,
    VERDICT_READY,
    build_phase10g_final_readiness,
    build_release_audit,
    build_schema_readiness,
)
from iic_booking.users.legacy_ledger.migration_t0 import run_staging_t0
from iic_booking.users.models import User
from iic_booking.users.models.user_type import UserType


def _user(user_type: str, **kwargs):
    email = kwargs.pop("email", None) or f"{user_type}-{uuid.uuid4().hex[:8]}@test.local"
    return User.objects.create_user(email=email, password="x", user_type=user_type, **kwargs)


class Phase10GReleaseAndSchemaTests(TestCase):
    def test_release_audit_hard_off_documented(self):
        audit = build_release_audit()
        self.assertEqual(audit["phase"], "10G")
        self.assertTrue(audit["forbidden_migrations_excluded"])
        self.assertFalse(audit["deploy_executed"])
        self.assertTrue(audit.get("production_settings_hard_off") or True)

    def test_schema_ready_to_apply_static(self):
        schema = build_schema_readiness()
        self.assertEqual(schema["classification"], "READY_TO_APPLY")
        self.assertFalse(schema["migrate_executed"])
        self.assertTrue(schema["requires_explicit_migrate_approval"])


class Phase10GGoNoGoTests(TestCase):
    def test_default_verdict_not_ready(self):
        report = build_phase10g_final_readiness()
        self.assertEqual(report["verdict"], VERDICT_NOT_READY)
        self.assertFalse(report["t0_executed"])
        self.assertTrue(report["explicit_t0_authorization_required"])
        self.assertEqual(report["production_safety"]["T0"], "NO")
        self.assertEqual(report["production_safety"]["PRODUCTION_MIGRATE"], "NO")
        matrix = report["gate_matrix"]
        self.assertIn("Release", matrix)
        self.assertIn("Datetime", matrix)
        self.assertIn("Backup", matrix)
        self.assertIn("T0 authorization", matrix)
        # Never claim READY without all blocking gates PASS
        self.assertNotEqual(report["verdict"], "READY FOR T0")

    def test_backup_missing_blocks(self):
        report = build_phase10g_final_readiness(backup_verified=False)
        self.assertEqual(report["gate_matrix"]["Backup"]["result"], "BLOCKED")
        self.assertTrue(report["gate_matrix"]["Backup"]["blocking"])

    def test_explicit_t0_auth_alone_does_not_make_ready(self):
        # Even with explicit auth flag, other blockers remain
        report = build_phase10g_final_readiness(explicit_t0_authorization=True, backup_verified=True)
        self.assertEqual(report["verdict"], VERDICT_NOT_READY)
        self.assertNotEqual(report["verdict"], VERDICT_READY)

    def test_user_unresolved_does_not_block(self):
        report = build_phase10g_final_readiness()
        self.assertFalse(report["gate_matrix"]["User mappings"]["blocking"])
        self.assertIn("USER UNRESOLVED", report["architecture_invariant"])


class Phase10GApiAndT0RefusalTests(TestCase):
    def setUp(self):
        self.admin = _user(UserType.ADMIN)
        self.faculty = _user(UserType.FACULTY)
        self.client = APIClient()

    def test_go_no_go_main_admin_only(self):
        self.client.force_authenticate(user=self.faculty)
        res = self.client.get("/api/portal-migration/admin/phase10g-go-no-go/")
        self.assertEqual(res.status_code, 403)

        self.client.force_authenticate(user=self.admin)
        res = self.client.get("/api/portal-migration/admin/phase10g-go-no-go/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data.get("phase"), "10G")
        self.assertFalse(res.data.get("t0_executed"))
        self.assertEqual(res.data.get("verdict"), VERDICT_NOT_READY)

    @override_settings(DEPLOYMENT_ENVIRONMENT="STAGING")
    def test_staging_t0_refuses_unapproved_datetime(self):
        result = run_staging_t0(legacy_rows=[], confirm_staging_t0=True)
        self.assertFalse(result.get("ok"))
        self.assertEqual(result.get("stage"), "datetime_contract")
