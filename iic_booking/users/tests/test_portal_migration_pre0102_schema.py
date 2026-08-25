"""Regression: portal-migration admin APIs must not 500 when users.0102 is absent."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from iic_booking.users.legacy_ledger.schema_gate import (
    clear_portal_bridge_schema_cache,
    portal_bridge_schema_status,
    schema_pending_payload,
)
from iic_booking.users.models.user_type import UserType

User = get_user_model()


def _pending_schema(**overrides):
    base = {
        "ready": False,
        "code": "SCHEMA_PENDING",
        "gate": "OPERATOR_REQUIRED",
        "pending_migrations": ["0101", "0102", "0103", "0104"],
        "missing_columns": ["migration_start_at", "migration_window_end_at", "booking_migration_mode", "new_portal_url"],
        "missing_tables": ["users_legacyequipmentmapping", "users_legacybookingblock"],
        "has_migration_start_at": False,
        "has_legacy_equipment_mapping_table": False,
        "has_legacy_booking_block_table": False,
        "detail": "users.0101–0104 not fully applied",
        "migrate_authorized": False,
        "migrate_executed": False,
    }
    base.update(overrides)
    return base


@override_settings(ROOT_URLCONF="config.urls")
class PortalMigrationPre0102SchemaTests(TestCase):
    def setUp(self):
        clear_portal_bridge_schema_cache()
        self.admin = User.objects.create_user(
            email="admin-schema@example.com",
            password="x",
            user_type=UserType.ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(user=self.admin)

    def tearDown(self):
        clear_portal_bridge_schema_cache()

    def test_schema_pending_payload_shape(self):
        with patch(
            "iic_booking.users.legacy_ledger.schema_gate.portal_bridge_schema_status_cached",
            return_value=(
                False,
                ("0101", "0102", "0103", "0104"),
                ("migration_start_at",),
                ("users_legacyequipmentmapping",),
                False,
                False,
                False,
            ),
        ):
            clear_portal_bridge_schema_cache()
            st = portal_bridge_schema_status()
            self.assertEqual(st["code"], "SCHEMA_PENDING")
            self.assertFalse(st["has_migration_start_at"])
            payload = schema_pending_payload(endpoint="test")
            self.assertEqual(payload["code"], "SCHEMA_PENDING")
            self.assertEqual(payload["gate"], "OPERATOR_REQUIRED")

    @patch(
        "iic_booking.users.api.portal_legacy_bridge_views.bridge_schema_ready_for_orm",
        return_value=False,
    )
    @patch(
        "iic_booking.users.api.portal_legacy_bridge_views.portal_bridge_schema_status",
        return_value=_pending_schema(),
    )
    def test_equipment_mappings_returns_503_not_500(self, _st, _ready):
        res = self.client.get("/api/portal-migration/admin/equipment-mappings/")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.data.get("code"), "SCHEMA_PENDING")
        self.assertIn("datetime_contract", res.data)

    @patch(
        "iic_booking.users.api.portal_legacy_bridge_views.bridge_schema_ready_for_orm",
        return_value=False,
    )
    @patch(
        "iic_booking.users.api.portal_legacy_bridge_views.portal_bridge_schema_status",
        return_value=_pending_schema(),
    )
    def test_legacy_bookings_returns_503_not_500(self, _st, _ready):
        res = self.client.get("/api/portal-migration/admin/legacy-bookings/")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.data.get("code"), "SCHEMA_PENDING")

    @patch(
        "iic_booking.users.api.portal_legacy_bridge_views.portal_bridge_schema_status",
        return_value=_pending_schema(),
    )
    def test_datetime_contract_get_200_file_based(self, _st):
        res = self.client.get("/api/portal-migration/admin/datetime-contract/")
        self.assertEqual(res.status_code, 200)
        self.assertIn("approval_status", res.data)
        self.assertEqual(res.data.get("schema", {}).get("code"), "SCHEMA_PENDING")

    @patch(
        "iic_booking.users.api.portal_legacy_bridge_views.portal_bridge_schema_status",
        return_value=_pending_schema(),
    )
    @patch(
        "iic_booking.users.api.portal_migration_views.portal_bridge_schema_status",
        return_value=_pending_schema(),
    )
    @patch(
        "iic_booking.users.api.portal_migration_views.safe_portal_migration_state",
    )
    def test_admin_state_window_patch_503(self, mock_safe, _st_views, _st_bridge):
        mock_safe.return_value = (
            type("S", (), {"end_user_booking_enabled": True, "phase": "PREPARATION"})(),
            _pending_schema(),
        )
        res = self.client.patch(
            "/api/portal-migration/admin/state/",
            {"migration_start_at": "2026-09-01T00:00:00+05:30"},
            format="json",
        )
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.data.get("code"), "SCHEMA_PENDING")

    @patch(
        "iic_booking.users.api.portal_legacy_bridge_views.portal_bridge_schema_status",
        return_value=_pending_schema(),
    )
    def test_phase10m_go_no_go_not_500(self, _st):
        res = self.client.get("/api/portal-migration/admin/phase10m-go-no-go/")
        self.assertIn(res.status_code, (200, 503))
        self.assertNotEqual(res.status_code, 500)
        if res.status_code == 200:
            self.assertTrue(
                res.data.get("code") == "SCHEMA_PENDING"
                or res.data.get("schema", {}).get("code") == "SCHEMA_PENDING"
                or res.data.get("t0_executed") is False
            )
