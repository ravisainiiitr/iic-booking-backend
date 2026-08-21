"""Tests for REAL vs FIXTURE integration guards and preflight (no secrets exposed)."""

from __future__ import annotations

import json

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from iic_booking.users.identity.channel_i_fixture import channel_i_fixture_mode_enabled
from iic_booking.users.legacy_ledger.real_integration_guards import (
    STATUS_ABSENT,
    STATUS_BLOCKED,
    STATUS_NOT_AVAILABLE,
    STATUS_PRESENT,
    assert_real_channel_i_ready,
    assert_real_legacy_mysql_ready,
    build_real_integration_preflight,
    employee_id_claim_status,
    format_preflight_human,
    s3_integration_status,
)
from iic_booking.users.legacy_ledger.snapshot_reader import get_legacy_reader


@override_settings(
    DEPLOYMENT_ENVIRONMENT="STAGING",
    REAL_INTEGRATION_ENABLED=False,
    CHANNEL_I_STAGING_FIXTURE_MODE=False,
    LEGACY_MYSQL_STAGING_FIXTURE_MODE=False,
    OMNIPORT_CLIENT_ID="",
    OMNIPORT_CLIENT_SECRET="",
    OLD_MYSQL_HOST="",
    OLD_MYSQL_USER="",
    OLD_MYSQL_DATABASE="",
    OLD_MYSQL_PASSWORD="",
    CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM="",
    STAGING_STORAGE_BACKEND="LOCAL_STAGING",
    USE_S3_MEDIA=False,
    AWS_ACCESS_KEY_ID="",
    AWS_SECRET_ACCESS_KEY="",
    AWS_STORAGE_BUCKET_NAME="",
    DATABASES={"default": {"HOST": "postgres", "NAME": "iic_booking_staging"}},
)
class MissingCredentialsGuardTests(SimpleTestCase):
    def test_missing_omniport_raises(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            assert_real_channel_i_ready()
        self.assertIn("OMNIPORT_CLIENT_ID", str(ctx.exception))
        self.assertNotIn("secret-value", str(ctx.exception).lower())

    def test_missing_mysql_raises(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            assert_real_legacy_mysql_ready()
        self.assertIn("OLD_MYSQL", str(ctx.exception))

    def test_s3_not_available(self):
        status = s3_integration_status()
        self.assertEqual(status["status"], STATUS_NOT_AVAILABLE)
        self.assertFalse(status.get("claim_pass"))

    def test_employee_id_blocked_when_empty(self):
        status = employee_id_claim_status()
        self.assertEqual(status["status"], STATUS_BLOCKED)
        self.assertEqual(status["wallet_identity"], STATUS_BLOCKED)

    def test_preflight_overall_not_ready(self):
        report = build_real_integration_preflight(backend_commit="f7783f9", frontend_commit="de71188")
        self.assertEqual(report["environment"], "STAGING")
        self.assertEqual(report["production_writes"], "NO")
        self.assertFalse(report["overall_ready_for_real_integration"])
        self.assertEqual(report["safety_result"], "NOT READY FOR REAL INTEGRATION")
        self.assertEqual(report["credentials_presence"]["OMNIPORT_CLIENT_ID"], STATUS_ABSENT)
        self.assertEqual(report["credentials_presence"]["OMNIPORT_CLIENT_SECRET"], STATUS_ABSENT)
        self.assertEqual(report["credentials_presence"]["OLD_MYSQL_PASSWORD"], STATUS_ABSENT)
        self.assertEqual(report["Channel-I"]["status"], STATUS_BLOCKED)
        self.assertEqual(report["Legacy_MySQL"]["status"], STATUS_BLOCKED)
        self.assertEqual(report["Staging_S3"]["status"], STATUS_NOT_AVAILABLE)
        self.assertEqual(report["Employee_Identity"]["status"], STATUS_BLOCKED)
        human = format_preflight_human(report)
        self.assertIn("NOT READY FOR REAL INTEGRATION", human)
        # No secret leakage patterns
        blob = json.dumps(report) + human
        self.assertNotIn("CHANGE_ME", blob)
        self.assertNotIn("AKIA", blob)


@override_settings(
    DEPLOYMENT_ENVIRONMENT="STAGING",
    REAL_INTEGRATION_ENABLED=True,
    CHANNEL_I_STAGING_FIXTURE_MODE=True,
    LEGACY_MYSQL_STAGING_FIXTURE_MODE=True,
    OMNIPORT_CLIENT_ID="id-present",
    OMNIPORT_CLIENT_SECRET="secret-present",
    OLD_MYSQL_HOST="mysql.example",
    OLD_MYSQL_USER="ro",
    OLD_MYSQL_DATABASE="admin",
    OLD_MYSQL_PASSWORD="pw",
    DATABASES={"default": {"HOST": "postgres", "NAME": "iic_booking_staging"}},
)
class RealPlusFixtureMustFailTests(SimpleTestCase):
    def test_channel_i_fixture_blocked_when_real_enabled(self):
        with self.assertRaises(ImproperlyConfigured):
            channel_i_fixture_mode_enabled()

    def test_get_legacy_reader_refuses_fixture_when_real_intent(self):
        with self.assertRaises(ImproperlyConfigured) as ctx:
            get_legacy_reader(require_real=True)
        self.assertIn("Refusing silent fixture", str(ctx.exception))

    def test_preflight_marks_ambiguous_fixture_with_real(self):
        report = build_real_integration_preflight()
        self.assertEqual(report["Channel-I"]["status"], STATUS_BLOCKED)
        self.assertEqual(report["Legacy_MySQL"]["status"], STATUS_BLOCKED)
        self.assertIn("FIXTURE", report["Channel-I"]["reason"].upper() + report["Legacy_MySQL"]["reason"].upper())


@override_settings(
    DEPLOYMENT_ENVIRONMENT="STAGING",
    REAL_INTEGRATION_ENABLED=False,
    CHANNEL_I_STAGING_FIXTURE_MODE=True,
    LEGACY_MYSQL_STAGING_FIXTURE_MODE=True,
    OMNIPORT_CLIENT_ID="",
    OMNIPORT_CLIENT_SECRET="",
    OLD_MYSQL_HOST="",
    DATABASES={"default": {"HOST": "postgres", "NAME": "iic_booking_staging"}},
)
class ExplicitFixtureModeTests(SimpleTestCase):
    def test_fixture_mode_allowed_when_not_real(self):
        self.assertTrue(channel_i_fixture_mode_enabled())

    def test_preflight_labels_fixture_not_real(self):
        report = build_real_integration_preflight()
        self.assertEqual(report["Channel-I"]["mode"], "FIXTURE")
        self.assertEqual(report["Channel-I"]["evidence_class"], "FIXTURE")
        self.assertEqual(report["Legacy_MySQL"]["evidence_class"], "FIXTURE")
        self.assertFalse(report["overall_ready_for_real_integration"])


@override_settings(
    DEPLOYMENT_ENVIRONMENT="STAGING",
    REAL_INTEGRATION_ENABLED=True,
    CHANNEL_I_STAGING_FIXTURE_MODE=False,
    LEGACY_MYSQL_STAGING_FIXTURE_MODE=False,
    OMNIPORT_CLIENT_ID="cid",
    OMNIPORT_CLIENT_SECRET="csec",
    OMNIPORT_REDIRECT_URI="http://127.0.0.1:8180/api/auth/omniport/callback/",
    OLD_MYSQL_HOST="h",
    OLD_MYSQL_USER="u",
    OLD_MYSQL_DATABASE="d",
    OLD_MYSQL_PASSWORD="p",
    CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM="operator_confirmed_map",
    STAGING_STORAGE_BACKEND="LOCAL_STAGING",
    USE_S3_MEDIA=False,
    DATABASES={"default": {"HOST": "postgres", "NAME": "iic_booking_staging"}},
)
class CredentialsPresentStillNotPassTests(SimpleTestCase):
    def test_configured_is_not_live_pass(self):
        report = build_real_integration_preflight()
        self.assertEqual(report["Channel-I"]["status"], "CONFIGURED")
        self.assertEqual(report["Legacy_MySQL"]["status"], "CONFIGURED")
        self.assertEqual(report["Employee_Identity"]["status"], "CONFIGURED")
        self.assertEqual(report["Staging_S3"]["status"], STATUS_NOT_AVAILABLE)
        self.assertFalse(report["overall_ready_for_real_integration"])
        self.assertEqual(report["credentials_presence"]["OMNIPORT_CLIENT_ID"], STATUS_PRESENT)
        self.assertEqual(report["credentials_presence"]["OLD_MYSQL_PASSWORD"], STATUS_PRESENT)
        # Secrets themselves must not appear in report
        dumped = json.dumps(report)
        self.assertNotIn("csec", dumped)
        self.assertNotIn('"p"', dumped)


@override_settings(
    DEPLOYMENT_ENVIRONMENT="PRODUCTION",
    REAL_INTEGRATION_ENABLED=True,
    DATABASES={"default": {"HOST": "postgres", "NAME": "x"}},
)
class ProductionGuardTests(SimpleTestCase):
    def test_real_assert_refuses_non_staging(self):
        with self.assertRaises(ImproperlyConfigured):
            assert_real_channel_i_ready()
