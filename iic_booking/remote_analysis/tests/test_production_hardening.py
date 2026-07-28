"""Milestone 8 — Remote Analysis production hardening tests."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from iic_booking.remote_analysis.configuration_catalog import CONFIGURATION_CATALOG, catalog_as_markdown
from iic_booking.remote_analysis.production_hardening import mask_secret, parse_pagination
from iic_booking.remote_analysis.workspace.storage import StorageError, StorageManager


@pytest.mark.django_db
def test_health_liveness_allow_anonymous(client):
    response = client.get("/api/v1/analysis/health/live/")
    assert response.status_code == 200
    assert response.json()["probe"] == "liveness"


@pytest.mark.django_db
def test_health_readiness_database(client):
    response = client.get("/api/v1/analysis/health/ready/")
    assert response.status_code in (200, 503)
    body = response.json()
    assert body["probe"] == "readiness"
    assert "database" in body["checks"]


@pytest.mark.django_db
def test_health_combined(client):
    response = client.get("/api/v1/analysis/health/")
    assert response.status_code in (200, 503)
    body = response.json()
    assert "version" in body
    assert body["version"].startswith("1.0.0")


def test_mask_secret():
    assert mask_secret("abcdefgh") == "****efgh"
    assert mask_secret("") == ""
    assert mask_secret("ab") == "**"


def test_parse_pagination_clamps():
    class R:
        query_params = {"limit": "9999", "offset": "-5"}

    offset, limit = parse_pagination(R(), max_limit=200)
    assert offset == 0
    assert limit == 200


def test_configuration_catalog_nonempty():
    assert len(CONFIGURATION_CATALOG) >= 20
    md = catalog_as_markdown()
    assert "mock_guacamole" in md


@pytest.mark.django_db
def test_path_traversal_rejected(tmp_path):
    from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings

    settings_obj = RemoteAnalysisSettings.get_solo()
    settings_obj.workspace_root = str(tmp_path)
    settings_obj.save(update_fields=["workspace_root"])
    (tmp_path / "ws-harden").mkdir()
    storage = StorageManager(settings_obj)
    ws = type("WS", (), {"storage_key": "ws-harden"})()
    with pytest.raises(StorageError):
        storage.write_bytes(ws, "../escape.txt", b"nope")
    with pytest.raises(StorageError):
        storage.absolute_path(ws, "..", "escape.txt")


@pytest.mark.django_db
def test_permissions_deny_anonymous_dashboard():
    client = APIClient()
    response = client.get("/api/v1/analysis/dashboard/")
    assert response.status_code in (401, 403)


@pytest.mark.django_db
def test_migration_graph_includes_collaboration():
    from django.db.migrations.loader import MigrationLoader
    from django.db import connection

    loader = MigrationLoader(connection)
    names = {k[1] for k in loader.disk_migrations if k[0] == "remote_analysis"}
    assert "0006_collaboration_center" in names


@pytest.mark.django_db
def test_validate_remote_analysis_command(capsys):
    from django.core.management import call_command

    call_command("validate_remote_analysis")
    out = capsys.readouterr().out
    assert "Architecture Validation" in out
    assert "[FAIL]" not in out
