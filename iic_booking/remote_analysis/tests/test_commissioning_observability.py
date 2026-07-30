"""Commissioning run observability tests (engineering support)."""

from __future__ import annotations

import io
import zipfile

import pytest
from rest_framework.test import APIClient

from iic_booking.remote_analysis.operations.commissioning_observability import (
    STEP_CONNECTIVITY,
    STEP_RUN_STARTED,
    STEP_SELF_TEST,
    annotate_details,
    bind_run_context,
    build_evidence_bundle_bytes,
    capture_failure_snapshot,
    complete_run,
    end_step,
    begin_step,
    get_commissioning_run_id,
    persist_evidence_bundle,
    start_commissioning_run,
    timeline_payload,
)
from iic_booking.remote_analysis.operations_models import CommissioningFailureSnapshot, CommissioningRun
from iic_booking.users.tests.factories import UserFactory


@pytest.fixture
def api(ra_user):
    client = APIClient()
    client.force_authenticate(user=ra_user)
    return client


@pytest.mark.django_db
def test_run_id_propagation_in_context(ra_user):
    run = start_commissioning_run(actor=ra_user)
    assert get_commissioning_run_id() is None
    with bind_run_context(run):
        assert get_commissioning_run_id() == str(run.id)
        tagged = annotate_details("workspace created")
        assert str(run.id) in tagged
        assert tagged.startswith("[commissioning_run=")
    assert get_commissioning_run_id() is None


@pytest.mark.django_db
def test_timeline_generation(ra_user):
    run = start_commissioning_run(actor=ra_user)
    begin_step(run, STEP_CONNECTIVITY)
    end_step(run, STEP_CONNECTIVITY, success=True, meta={"checks": 3})
    complete_run(run, success=True)

    payload = timeline_payload(run)
    names = [s["name"] for s in payload["steps"]]
    assert STEP_RUN_STARTED in names
    assert STEP_CONNECTIVITY in names
    conn = next(s for s in payload["steps"] if s["name"] == STEP_CONNECTIVITY)
    assert conn["success"] is True
    assert conn["started_at"]
    assert conn["ended_at"]
    assert conn["duration_ms"] is not None
    assert conn["retry_count"] == 0


@pytest.mark.django_db
def test_failure_snapshot_creation(ra_user, eligible_workstation):
    run = start_commissioning_run(actor=ra_user, workstation_id=str(eligible_workstation.id))
    begin_step(run, STEP_SELF_TEST)
    end_step(run, STEP_SELF_TEST, success=False, error="probe failed")

    snaps = list(CommissioningFailureSnapshot.objects.filter(run=run))
    assert len(snaps) >= 1
    assert snaps[0].payload["commissioning_run_id"] == str(run.id)
    assert snaps[0].payload["error"] == "probe failed"
    assert snaps[0].payload["database_identifiers"]["run_id"] == str(run.id)
    run.refresh_from_db()
    assert run.status == "FAILED"


@pytest.mark.django_db
def test_evidence_bundle_generation(ra_user):
    run = start_commissioning_run(actor=ra_user)
    begin_step(run, STEP_CONNECTIVITY)
    end_step(run, STEP_CONNECTIVITY, success=True)
    capture_failure_snapshot(run, step_name="manual", error="n/a")  # still include snapshots section
    complete_run(run, success=True)

    raw = build_evidence_bundle_bytes(run, include_pdf=True)
    assert raw[:2] == b"PK"
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        names = set(zf.namelist())
    assert "commissioning_summary.json" in names
    assert "execution_timeline.json" in names
    assert "portal_logs.json" in names
    assert "agent_logs.json" in names
    assert "workspace_metadata.json" in names
    assert "api_summary.json" in names
    assert "checksum_results.json" in names
    assert "performance_metrics.json" in names
    assert "commissioning_report.pdf" in names or "commissioning_report_error.txt" in names

    path = persist_evidence_bundle(run)
    run.refresh_from_db()
    assert run.evidence_path == path
    assert "commissioning_runs" in path


@pytest.mark.django_db
def test_toolkit_run_apis_and_evidence_download(api, eligible_workstation, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()

    started = api.post(
        "/api/v1/analysis/operations/toolkit/runs/",
        {"workstation_id": str(eligible_workstation.id), "notes": "obs-test"},
        format="json",
    )
    assert started.status_code == 201, started.content
    run_id = started.json()["commissioning_run_id"]

    conn = api.post(
        "/api/v1/analysis/operations/toolkit/connectivity/",
        {"workstation_id": str(eligible_workstation.id), "commissioning_run_id": run_id},
        format="json",
    )
    assert conn.status_code == 200, conn.content
    body = conn.json()
    assert body["commissioning_run_id"] == run_id
    assert body["evidence_url"].endswith(f"/runs/{run_id}/evidence/")

    detail = api.get(f"/api/v1/analysis/operations/toolkit/runs/{run_id}/")
    assert detail.status_code == 200
    assert "timeline" in detail.json()
    assert any(s["name"] == STEP_CONNECTIVITY for s in detail.json()["timeline"]["steps"])

    timeline = api.get(f"/api/v1/analysis/operations/toolkit/runs/{run_id}/timeline/")
    assert timeline.status_code == 200
    assert timeline.json()["commissioning_run_id"] == run_id

    evidence = api.get(f"/api/v1/analysis/operations/toolkit/runs/{run_id}/evidence/")
    assert evidence.status_code == 200
    assert evidence["Content-Type"] == "application/zip"
    assert evidence.content[:2] == b"PK"

    listed = api.get("/api/v1/analysis/operations/toolkit/runs/")
    assert listed.status_code == 200
    assert any(r["commissioning_run_id"] == run_id for r in listed.json())


@pytest.mark.django_db
def test_evidence_requires_manage_permission(eligible_workstation):
    run = start_commissioning_run()
    persist_evidence_bundle(run)

    anon = APIClient()
    assert anon.get(f"/api/v1/analysis/operations/toolkit/runs/{run.id}/evidence/").status_code in {401, 403}

    student = UserFactory(user_type="student", admin_approved=True, email_verified=True)
    client = APIClient()
    client.force_authenticate(user=student)
    assert client.get(f"/api/v1/analysis/operations/toolkit/runs/{run.id}/evidence/").status_code == 403


@pytest.mark.django_db
def test_self_test_returns_run_id(api, eligible_workstation, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()

    st = api.post(
        "/api/v1/analysis/operations/toolkit/self-test/",
        {"workstation_id": str(eligible_workstation.id)},
        format="json",
    )
    assert st.status_code == 200, st.content
    body = st.json()
    assert body.get("commissioning_run_id")
    assert CommissioningRun.objects.filter(pk=body["commissioning_run_id"]).exists()
    evidence = api.get(body["evidence_url"])
    assert evidence.status_code == 200
    assert evidence.content[:2] == b"PK"
