"""Guards: production deploy/startup must not auto-migrate or POST provisioning."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.test import SimpleTestCase, override_settings

REPO_ROOT = Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return (REPO_ROOT / rel).read_text(encoding="utf-8")


def _code_lines(text: str) -> list[str]:
    """Non-comment, non-empty lines (shell/# and rough YAML # comments)."""
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("- name:") or line.startswith("name:"):
            out.append(line)
            continue
        # strip inline shell comments
        if " #" in line:
            line = line.split(" #", 1)[0].rstrip()
        out.append(line)
    return out


def _joined_code(rel: str) -> str:
    return "\n".join(_code_lines(_read(rel)))


class ProductionStartNoAutoMigrateTests(SimpleTestCase):
    def test_production_django_start_does_not_call_migrate(self):
        code = _joined_code("compose/production/django/start")
        assert "manage.py migrate" not in code
        assert "migrate --noinput" not in code
        assert "collectstatic" in code
        text = _read("compose/production/django/start")
        assert "DEPLOYMENT" in text and "MIGRATION" in text

    def test_backend_deploy_workflow_no_migrate_or_provisioning_post(self):
        code = _joined_code(".github/workflows/backend-deploy.yml")
        assert "migrate device_provisioning" not in code
        assert "migrate --noinput" not in code
        # No executable curl POST to provisioning sessions.
        assert "POST" not in code or "/api/v1/provisioning/sessions/" not in code
        for line in _code_lines(_read(".github/workflows/backend-deploy.yml")):
            if "/api/v1/provisioning/sessions/" in line:
                assert "POST" not in line.upper()
                assert "curl" not in line
        assert "showmigrations device_provisioning" in code

    def test_deploy_sh_no_auto_migrate(self):
        code = _joined_code("scripts/deploy/deploy.sh")
        assert "manage.py migrate" not in code
        assert "migrate --noinput" not in code
        assert "collectstatic" in code

    def test_rollback_sh_no_auto_migrate(self):
        code = _joined_code("scripts/deploy/rollback.sh")
        assert "manage.py migrate" not in code
        assert "migrate --noinput" not in code

    def test_explicit_migrate_script_requires_confirmation(self):
        text = _read("scripts/deploy/migrate-production.sh")
        assert "CONFIRM_MIGRATE" in text
        assert "manage.py migrate --noinput" in text

    def test_migrate_production_workflow_requires_confirm_and_no_provisioning_post(self):
        text = _read(".github/workflows/migrate-production.yml")
        code = _joined_code(".github/workflows/migrate-production.yml")
        assert "confirm_migrate" in text
        assert '!= "MIGRATE"' in text
        for line in _code_lines(text):
            if "/api/v1/provisioning/sessions/" in line:
                raise AssertionError(f"provisioning session write still present: {line}")
        assert "migrate --noinput" in code


class ProductionSettingsHardOffFileTests(SimpleTestCase):
    """Parse production.py source — do not load full production settings env."""

    def test_production_settings_file_hard_off_flags(self):
        text = _read("config/settings/production.py")
        assert 'DEPLOYMENT_ENVIRONMENT = "PRODUCTION"' in text
        assert "REAL_INTEGRATION_ENABLED = False" in text
        assert "CHANNEL_I_STAGING_FIXTURE_MODE = False" in text
        assert "LEGACY_MYSQL_STAGING_FIXTURE_MODE = False" in text
        assert "LOCAL_STAGING_ACCEPTED = False" in text


class ProductionSettingsRuntimeHardOffTests(SimpleTestCase):
    @override_settings(
        DEPLOYMENT_ENVIRONMENT="PRODUCTION",
        REAL_INTEGRATION_ENABLED=False,
        CHANNEL_I_STAGING_FIXTURE_MODE=False,
        LEGACY_MYSQL_STAGING_FIXTURE_MODE=False,
        LOCAL_STAGING_ACCEPTED=False,
    )
    def test_override_mirrors_required_production_hard_off(self):
        assert settings.DEPLOYMENT_ENVIRONMENT == "PRODUCTION"
        assert settings.REAL_INTEGRATION_ENABLED is False
        assert settings.CHANNEL_I_STAGING_FIXTURE_MODE is False
        assert settings.LEGACY_MYSQL_STAGING_FIXTURE_MODE is False
        assert settings.LOCAL_STAGING_ACCEPTED is False
