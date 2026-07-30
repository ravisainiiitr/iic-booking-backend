"""Unit tests for Guacamole environment overlays (Workstream 2)."""

from __future__ import annotations

import os
from unittest import mock

from django.test import SimpleTestCase

from iic_booking.remote_analysis.guacamole.settings_env import (
    overlay_from_environ,
    production_guacamole_configured,
)
from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings


class GuacamoleSettingsEnvTests(SimpleTestCase):
    def test_overlay_disables_mock_and_sets_urls(self):
        obj = RemoteAnalysisSettings(
            mock_guacamole=True,
            guacamole_api_url="",
            guacamole_base_url="",
            guacamole_admin_username="",
            guacamole_admin_password="",
        )
        with mock.patch.dict(
            os.environ,
            {
                "RA_MOCK_GUACAMOLE": "false",
                "RA_GUACAMOLE_API_URL": "https://guac-internal/guacamole",
                "RA_GUACAMOLE_BASE_URL": "https://guac.example/guacamole",
                "RA_GUACAMOLE_ADMIN_USERNAME": "guacadmin",
                "RA_GUACAMOLE_ADMIN_PASSWORD": "secret",
                "RA_GUACAMOLE_DATA_SOURCE": "postgresql",
                "RA_GUACAMOLE_VERIFY_TLS": "true",
            },
            clear=False,
        ):
            overlay_from_environ(obj)

        self.assertFalse(obj.mock_guacamole)
        self.assertEqual(obj.guacamole_api_url, "https://guac-internal/guacamole")
        self.assertEqual(obj.guacamole_base_url, "https://guac.example/guacamole")
        self.assertEqual(obj.guacamole_admin_username, "guacadmin")
        self.assertEqual(obj.guacamole_admin_password, "secret")
        self.assertEqual(obj.guacamole_data_source, "postgresql")
        self.assertTrue(obj.verify_tls)
        ok, problems = production_guacamole_configured(obj)
        self.assertTrue(ok)
        self.assertEqual(problems, [])

    def test_production_misconfigured_when_mock_off_without_api(self):
        obj = RemoteAnalysisSettings(mock_guacamole=False, guacamole_api_url="")
        ok, problems = production_guacamole_configured(obj)
        self.assertFalse(ok)
        self.assertIn("guacamole_api_url missing", problems)
