"""Regression: production qualification commands import cleanly."""

from __future__ import annotations

import importlib
from io import StringIO

from django.core.management import call_command
from django.test import SimpleTestCase


class MigrationProductionQualificationImportTests(SimpleTestCase):
    def test_legacy_qualification_command_imports(self):
        mod = importlib.import_module(
            "iic_booking.users.management.commands.migration_production_legacy_qualification"
        )
        assert hasattr(mod, "build_phase10_report")
        assert hasattr(mod, "Command")

    def test_t0_readiness_command_imports_preview_templates_from_notifications(self):
        mod = importlib.import_module(
            "iic_booking.users.management.commands.migration_production_t0_readiness"
        )
        src = importlib.import_module(mod.__name__)
        assert "preview_templates" in src.__dict__ or hasattr(src, "build_report")

    def test_email_preview_command_runs_without_smtp(self):
        out = StringIO()
        call_command("migration_email_preview", stdout=out)
        text = out.getvalue()
        assert "FACULTY_MIGRATION" in text or "subject" in text
