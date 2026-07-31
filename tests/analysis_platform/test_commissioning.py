"""Phase 4 commissioning checks for the Analysis Platform harness."""

from __future__ import annotations

import pytest


@pytest.mark.django_db
def test_live_commissioning_payload_shape(apt_admin_api):
    res = apt_admin_api.get("/api/v1/analysis/operations/toolkit/live/")
    assert res.status_code == 200
    body = res.json()
    assert body["overall"] in {"GREEN", "AMBER", "RED"}
    for card in body["cards"]:
        assert card["status"] in {"GREEN", "AMBER", "RED"}
        assert "name" in card
        assert "detail" in card


@pytest.mark.django_db
def test_fault_catalog_and_dry_run(apt_admin_api, apt_seed):
    catalog = apt_admin_api.get("/api/v1/analysis/operations/toolkit/faults/")
    assert catalog.status_code == 200
    faults = catalog.json()["faults"]
    assert len(faults) >= 5
    assert "recovery" in catalog.json()

    ws_id = str(apt_seed.workstation.id)
    dry = apt_admin_api.post(
        "/api/v1/analysis/operations/toolkit/faults/inject/",
        {"fault_id": "agent_restart", "workstation_id": ws_id, "dry_run": True},
        format="json",
    )
    assert dry.status_code == 200
    assert dry.json()["would_inject"] is True


@pytest.mark.django_db
def test_evidence_zip_phase4_members(apt_admin_api, apt_seed):
    started = apt_admin_api.post(
        "/api/v1/analysis/operations/toolkit/runs/",
        {"workstation_id": str(apt_seed.workstation.id), "notes": "harness-p4"},
        format="json",
    )
    assert started.status_code == 201
    run_id = started.json()["commissioning_run_id"]
    evidence = apt_admin_api.get(f"/api/v1/analysis/operations/toolkit/runs/{run_id}/evidence/")
    assert evidence.status_code == 200
    assert evidence.content[:2] == b"PK"

    import io
    import zipfile

    with zipfile.ZipFile(io.BytesIO(evidence.content)) as zf:
        names = set(zf.namelist())
    for required in (
        "execution_timeline.json",
        "tunnel_metrics.json",
        "health_metrics.json",
        "configuration_snapshot.json",
        "commands.json",
    ):
        assert required in names
