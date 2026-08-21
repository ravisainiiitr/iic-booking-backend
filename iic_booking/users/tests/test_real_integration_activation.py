"""Extended REAL activation tooling tests (no invented credentials / no secrets in output)."""

from __future__ import annotations

import json

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings

from iic_booking.users.legacy_ledger.reader import assert_readonly_sql
from iic_booking.users.legacy_ledger.real_integration_activation import (
    format_activation_human,
    run_staging_activation,
    verify_fixture_isolation_under_real_intent,
)
from iic_booking.users.legacy_ledger.real_integration_guards import (
    EXPECTED_OMNIPORT_CALLBACK_PATH,
    LEGACY_WRONG_CALLBACK_MARKER,
    STATUS_BLOCKED,
    STATUS_INVALID,
    STATUS_NOT_AVAILABLE,
    STATUS_PASS,
    STATUS_VALID,
    assert_omniport_redirect_uri_valid,
    assert_real_channel_i_ready,
    assert_real_employee_claim_ready,
    build_real_integration_status,
    employee_id_claim_status,
    format_status_human,
    omniport_redirect_uri_status,
)


_STAGING_DB = {"default": {"HOST": "postgres", "NAME": "iic_booking_staging"}}


@override_settings(
    DEPLOYMENT_ENVIRONMENT="STAGING",
    REAL_INTEGRATION_ENABLED=False,
    CHANNEL_I_STAGING_FIXTURE_MODE=False,
    LEGACY_MYSQL_STAGING_FIXTURE_MODE=False,
    OMNIPORT_CLIENT_ID="",
    OMNIPORT_CLIENT_SECRET="",
    OMNIPORT_REDIRECT_URI="http://127.0.0.1:8180" + LEGACY_WRONG_CALLBACK_MARKER,
    OLD_MYSQL_HOST="",
    OLD_MYSQL_PASSWORD="",
    CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM="",
    STAGING_STORAGE_BACKEND="LOCAL_STAGING",
    USE_S3_MEDIA=False,
    DATABASES=_STAGING_DB,
)
class RedirectAndClaimValidationTests(SimpleTestCase):
    def test_legacy_redirect_invalid(self):
        status = omniport_redirect_uri_status()
        self.assertEqual(status["status"], STATUS_INVALID)
        self.assertTrue(status["is_legacy_wrong_path"])
        with self.assertRaises(ImproperlyConfigured) as ctx:
            assert_omniport_redirect_uri_valid()
        self.assertIn("CHANNEL-I REDIRECT URI INVALID", str(ctx.exception))

    def test_correct_redirect_accepted(self):
        with override_settings(
            OMNIPORT_REDIRECT_URI=f"http://127.0.0.1:8180{EXPECTED_OMNIPORT_CALLBACK_PATH}"
        ):
            status = omniport_redirect_uri_status()
            self.assertEqual(status["status"], STATUS_VALID)
            self.assertTrue(status["matches_expected"])
            assert_omniport_redirect_uri_valid()

    def test_empty_employee_claim_blocked(self):
        status = employee_id_claim_status()
        self.assertEqual(status["status"], STATUS_BLOCKED)
        with self.assertRaises(ImproperlyConfigured):
            assert_real_employee_claim_ready()

    def test_unrecognized_claim_rejected(self):
        with override_settings(CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM="email"):
            status = employee_id_claim_status()
            self.assertEqual(status["status"], STATUS_BLOCKED)
            self.assertIn("Unrecognized", status["reason"])
            with self.assertRaises(ImproperlyConfigured):
                assert_real_employee_claim_ready()

    def test_status_overall_not_ready(self):
        report = build_real_integration_status()
        self.assertEqual(report["Channel-I"], STATUS_BLOCKED)
        self.assertEqual(report["Redirect"], STATUS_INVALID)
        self.assertEqual(report["Legacy_MySQL"], STATUS_BLOCKED)
        self.assertEqual(report["Employee_Identity"], STATUS_BLOCKED)
        self.assertEqual(report["S3"], STATUS_NOT_AVAILABLE)
        self.assertEqual(report["Overall"], "NOT READY")
        human = format_status_human(report)
        blob = json.dumps(report, default=str) + human
        self.assertIn("NOT READY", human)
        self.assertNotIn("AKIA", blob)
        self.assertNotIn(LEGACY_WRONG_CALLBACK_MARKER + "SECRET", blob)


@override_settings(
    DEPLOYMENT_ENVIRONMENT="STAGING",
    REAL_INTEGRATION_ENABLED=False,
    CHANNEL_I_STAGING_FIXTURE_MODE=False,
    LEGACY_MYSQL_STAGING_FIXTURE_MODE=False,
    OMNIPORT_CLIENT_ID="",
    OMNIPORT_CLIENT_SECRET="",
    OMNIPORT_REDIRECT_URI=f"http://127.0.0.1:8180{EXPECTED_OMNIPORT_CALLBACK_PATH}",
    OLD_MYSQL_HOST="",
    CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM="",
    DATABASES=_STAGING_DB,
)
class ActivateStopsWithoutRealModeTests(SimpleTestCase):
    def test_activate_blocked_when_real_mode_off(self):
        report = run_staging_activation(
            backend_commit="f7783f9",
            frontend_commit="de71188",
            run_tests=False,
            attempt_live_probes=False,
        )
        self.assertIn("BLOCKED", report["verdict"])
        self.assertEqual(report["safety_result"], "NOT READY FOR REAL INTEGRATION")
        self.assertTrue(report["never_edits_env"])
        self.assertTrue(report["waiting_for_operator"] or "REAL MODE NOT ENABLED" in (report.get("stop_reason") or ""))
        human = format_activation_human(report)
        self.assertNotIn("AKIA", human)
        dumped = json.dumps(report, default=str)
        self.assertNotIn("password=", dumped)


@override_settings(
    DEPLOYMENT_ENVIRONMENT="STAGING",
    REAL_INTEGRATION_ENABLED=True,
    CHANNEL_I_STAGING_FIXTURE_MODE=True,
    LEGACY_MYSQL_STAGING_FIXTURE_MODE=True,
    OMNIPORT_CLIENT_ID="cid",
    OMNIPORT_CLIENT_SECRET="csec",
    OMNIPORT_REDIRECT_URI=f"http://127.0.0.1:8180{EXPECTED_OMNIPORT_CALLBACK_PATH}",
    OLD_MYSQL_HOST="h",
    OLD_MYSQL_USER="u",
    OLD_MYSQL_DATABASE="d",
    OLD_MYSQL_PASSWORD="p",
    CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM="operator_confirmed_map",
    DATABASES=_STAGING_DB,
)
class ActivateFixtureWithRealFailsTests(SimpleTestCase):
    def test_activate_stops_on_fixture_flags(self):
        report = run_staging_activation(run_tests=False, attempt_live_probes=False)
        self.assertIn("BLOCKED", report["verdict"])
        self.assertIn("Fixture", report.get("stop_reason") or "")


@override_settings(
    DEPLOYMENT_ENVIRONMENT="STAGING",
    DATABASES=_STAGING_DB,
)
class FixtureIsolationAndReadonlySqlTests(SimpleTestCase):
    def test_fixture_isolation_pass(self):
        result = verify_fixture_isolation_under_real_intent()
        self.assertEqual(result["status"], STATUS_PASS)

    def test_readonly_sql_allows_select(self):
        assert_readonly_sql("SELECT 1")
        assert_readonly_sql("SHOW TABLES")

    def test_readonly_sql_rejects_writes(self):
        for sql in (
            "INSERT INTO users VALUES (1)",
            "UPDATE user_wallet SET balance=0",
            "DELETE FROM wallet_transactions",
            "ALTER TABLE users ADD x INT",
            "DROP TABLE users",
            "TRUNCATE wallet_transactions",
            "SELECT 1; DELETE FROM users",
        ):
            with self.assertRaises(ValueError):
                assert_readonly_sql(sql)


@override_settings(
    DEPLOYMENT_ENVIRONMENT="STAGING",
    REAL_INTEGRATION_ENABLED=True,
    CHANNEL_I_STAGING_FIXTURE_MODE=False,
    LEGACY_MYSQL_STAGING_FIXTURE_MODE=False,
    OMNIPORT_CLIENT_ID="cid",
    OMNIPORT_CLIENT_SECRET="csec",
    OMNIPORT_REDIRECT_URI=f"http://127.0.0.1:8180{EXPECTED_OMNIPORT_CALLBACK_PATH}",
    OLD_MYSQL_HOST="h",
    OLD_MYSQL_USER="u",
    OLD_MYSQL_DATABASE="d",
    OLD_MYSQL_PASSWORD="p",
    CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM="username",
    STAGING_STORAGE_BACKEND="LOCAL_STAGING",
    USE_S3_MEDIA=False,
    LOCAL_STAGING_ACCEPTED=False,
    DATABASES=_STAGING_DB,
)
class LocalStagingAcceptanceTests(SimpleTestCase):
    def test_s3_not_available_is_not_pass(self):
        from iic_booking.users.legacy_ledger.real_integration_guards import s3_integration_status

        s3 = s3_integration_status()
        self.assertEqual(s3["status"], STATUS_NOT_AVAILABLE)
        self.assertFalse(s3.get("claim_pass"))
        self.assertFalse(s3.get("accepted_limitation"))

    def test_s3_blocks_without_acceptance(self):
        from iic_booking.users.legacy_ledger.real_integration_guards import (
            build_real_integration_preflight,
            s3_blocks_real_activation,
        )

        self.assertTrue(s3_blocks_real_activation())
        report = build_real_integration_preflight(include_live_probes=False)
        self.assertTrue(any("S3" in r for r in report["blocked_reasons"]))
        self.assertFalse(report["overall_ready_for_real_integration"])

    def test_local_staging_accepted_removes_s3_blocker_but_not_pass(self):
        from iic_booking.users.legacy_ledger.real_integration_guards import (
            build_real_integration_preflight,
            s3_blocks_real_activation,
            s3_integration_status,
        )

        with override_settings(LOCAL_STAGING_ACCEPTED=True):
            s3 = s3_integration_status()
            self.assertEqual(s3["status"], STATUS_NOT_AVAILABLE)
            self.assertTrue(s3.get("accepted_limitation"))
            self.assertFalse(s3.get("claim_pass"))
            self.assertFalse(s3_blocks_real_activation(s3))
            report = build_real_integration_preflight(include_live_probes=False)
            self.assertFalse(any("S3" in r for r in report["blocked_reasons"]))
            # Still not GO — live probes not run / not PASS
            self.assertFalse(report["overall_ready_for_real_integration"])
            self.assertIn("ACCEPTED LIMITATION", s3["note"])


@override_settings(
    DEPLOYMENT_ENVIRONMENT="STAGING",
    REAL_INTEGRATION_ENABLED=True,
    CHANNEL_I_STAGING_FIXTURE_MODE=False,
    LEGACY_MYSQL_STAGING_FIXTURE_MODE=False,
    OMNIPORT_CLIENT_ID="cid",
    OMNIPORT_CLIENT_SECRET="csec",
    OMNIPORT_REDIRECT_URI=f"http://127.0.0.1:8180{EXPECTED_OMNIPORT_CALLBACK_PATH}",
    OLD_MYSQL_HOST="h",
    OLD_MYSQL_USER="u",
    OLD_MYSQL_DATABASE="d",
    OLD_MYSQL_PASSWORD="p",
    CHANNEL_I_AUTHORITATIVE_EMPLOYEE_ID_CLAIM="username",
    LOCAL_STAGING_ACCEPTED=True,
    DATABASES=_STAGING_DB,
)
class LiveChannelIEvidenceMockTests(SimpleTestCase):
    def test_missing_profile_requires_operator_oauth(self):
        from unittest.mock import MagicMock, patch

        from iic_booking.users.legacy_ledger.real_integration_live_evidence import (
            probe_live_channel_i_identity,
        )

        qs = MagicMock()
        qs.exclude.return_value = qs
        qs.order_by.return_value.first.return_value = None
        with patch(
            "iic_booking.users.models.channel_i_identity.ChannelIIdentityProfile.objects"
        ) as objects:
            objects.exclude.return_value = qs
            probe = probe_live_channel_i_identity()
        self.assertEqual(probe["live_oauth"], "OPERATOR ACTION REQUIRED")
        self.assertFalse(probe.get("claim_pass"))
        self.assertNotEqual(probe.get("status"), STATUS_PASS)

    def test_verified_username_match_is_real_pass(self):
        from unittest.mock import MagicMock, patch

        from iic_booking.users.legacy_ledger.reader import OldMySQLReader
        from iic_booking.users.legacy_ledger.real_integration_live_evidence import (
            channel_i_from_live_probe,
            employee_identity_from_live_probe,
            probe_live_channel_i_identity,
        )

        profile = MagicMock()
        profile.channel_i_username = "100001"
        profile.student_enrolment_number = ""
        profile.last_channel_i_sync = "2026-08-21"
        profile.has_student_payload = False
        profile.has_faculty_payload = False

        qs = MagicMock()
        qs.exclude.return_value = qs
        qs.order_by.return_value.first.return_value = profile

        reader = MagicMock(spec=OldMySQLReader)
        reader._real_integration_old_mysql_reader = True
        reader.__enter__ = MagicMock(return_value=reader)
        reader.__exit__ = MagicMock(return_value=False)
        reader.fetchall.return_value = [("100001",)]

        with patch(
            "iic_booking.users.models.channel_i_identity.ChannelIIdentityProfile.objects"
        ) as objects, patch(
            "iic_booking.users.legacy_ledger.snapshot_reader.get_legacy_reader",
            return_value=reader,
        ):
            objects.exclude.return_value = qs
            probe = probe_live_channel_i_identity()

        self.assertEqual(probe["status"], STATUS_PASS)
        self.assertEqual(probe["evidence_class"], "REAL")
        self.assertEqual(probe["live_oauth"], STATUS_PASS)
        self.assertEqual(probe["live_userinfo"], STATUS_PASS)
        self.assertEqual(probe["claim"], "username")
        self.assertTrue(probe["claim_pass"])
        self.assertEqual(probe["exact_match_count"], 1)
        self.assertEqual(probe["fixture_fallback"], "NONE")
        # Claim *value* must never appear in the probe payload (claim name is OK).
        self.assertNotIn("100001", json.dumps(probe, default=str))
        ch = channel_i_from_live_probe(probe)
        emp = employee_identity_from_live_probe(probe)
        self.assertEqual(ch["status"], STATUS_PASS)
        self.assertEqual(ch["evidence_class"], "REAL")
        self.assertEqual(emp["status"], STATUS_PASS)
        self.assertTrue(emp["claim_pass"])
        self.assertEqual(emp["wallet_identity"], STATUS_PASS)


@override_settings(
    DEPLOYMENT_ENVIRONMENT="PRODUCTION",
    REAL_INTEGRATION_ENABLED=True,
    DATABASES={"default": {"HOST": "postgres", "NAME": "x"}},
)
class ProductionRefuseTests(SimpleTestCase):
    def test_status_builder_refuses_production(self):
        with self.assertRaises(ImproperlyConfigured):
            build_real_integration_status()

    def test_activate_refuses_production(self):
        with self.assertRaises(ImproperlyConfigured):
            run_staging_activation(run_tests=False, attempt_live_probes=False)

    def test_channel_i_assert_refuses_production(self):
        with self.assertRaises(ImproperlyConfigured):
            assert_real_channel_i_ready()
