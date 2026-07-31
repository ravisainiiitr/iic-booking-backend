"""Admin-only Commissioning & Diagnostics Toolkit tests."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from iic_booking.users.tests.factories import UserFactory


@pytest.fixture
def api(ra_user):
    client = APIClient()
    client.force_authenticate(user=ra_user)
    return client


@pytest.mark.django_db
def test_toolkit_html_and_dashboard(api, eligible_workstation):
    html = api.get("/api/v1/analysis/operations/toolkit/?view=html")
    assert html.status_code == 200
    assert b"Commissioning" in html.content
    assert b"Self-test" in html.content

    dash = api.get("/api/v1/analysis/operations/toolkit/dashboard/")
    assert dash.status_code == 200
    body = dash.json()
    assert "overview" in body
    assert "workstations" in body
    assert body["overview"]["workstations_total"] >= 1


@pytest.mark.django_db
def test_toolkit_agent_diagnostics(api, eligible_workstation):
    res = api.get(
        f"/api/v1/analysis/operations/toolkit/agent/?workstation_id={eligible_workstation.id}"
    )
    assert res.status_code == 200
    body = res.json()
    assert body["workstation"]["hostname"] == eligible_workstation.hostname
    assert "token" in body
    assert "machine" in body


@pytest.mark.django_db
def test_toolkit_connectivity_and_self_test(api, eligible_workstation, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()

    conn = api.post(
        "/api/v1/analysis/operations/toolkit/connectivity/",
        {"workstation_id": str(eligible_workstation.id)},
        format="json",
    )
    assert conn.status_code == 200, conn.content
    assert conn.json()["overall"] in {"PASS", "FAIL"}
    assert "file_upload" in conn.json()["results"]

    st = api.post(
        "/api/v1/analysis/operations/toolkit/self-test/",
        {"workstation_id": str(eligible_workstation.id)},
        format="json",
    )
    assert st.status_code == 200, st.content
    body = st.json()
    assert body["overall"] == "PASS"
    assert body["summary"]["fail"] == 0


@pytest.mark.django_db
def test_toolkit_health_logs_report_monitoring(api, eligible_workstation, ra_settings, tmp_path):
    ra_settings.workspace_root = str(tmp_path)
    ra_settings.save()

    health = api.get("/api/v1/analysis/operations/toolkit/health-report/")
    assert health.status_code == 200
    assert health.json()["overall"] in {"GREEN", "AMBER", "RED"}
    assert "components" in health.json()

    logs = api.get("/api/v1/analysis/operations/toolkit/logs/?since_hours=24")
    assert logs.status_code == 200
    assert "entries" in logs.json()

    report = api.get("/api/v1/analysis/operations/toolkit/report/")
    assert report.status_code == 200
    assert report.json()["title"]

    pdf = api.get("/api/v1/analysis/operations/toolkit/report/?export=pdf")
    assert pdf.status_code == 200
    assert pdf["Content-Type"] == "application/pdf"
    assert pdf.content[:4] == b"%PDF"

    mon = api.get("/api/v1/analysis/operations/toolkit/monitoring/")
    assert mon.status_code == 200
    assert len(mon.json()["recommendations"]) >= 5


@pytest.mark.django_db
def test_toolkit_requires_manage_permission():
    anon = APIClient()
    assert anon.get("/api/v1/analysis/operations/toolkit/dashboard/").status_code in {401, 403}

    student = UserFactory(user_type="student", admin_approved=True, email_verified=True)
    client = APIClient()
    client.force_authenticate(user=student)
    assert client.get("/api/v1/analysis/operations/toolkit/dashboard/").status_code == 403


@pytest.mark.django_db
def test_toolkit_anonymous_html_redirects(settings):
    settings.FRONTEND_URL = "https://portal.example"
    client = APIClient()
    res = client.get("/api/v1/analysis/operations/toolkit/?view=html")
    assert res.status_code == 302
    assert res["Location"].startswith("https://portal.example/login?")


@pytest.mark.django_db
def test_live_commissioning_dashboard_and_timeline(api, eligible_workstation):
    live = api.get("/api/v1/analysis/operations/toolkit/live/")
    assert live.status_code == 200
    body = live.json()
    assert body["overall"] in {"GREEN", "AMBER", "RED"}
    assert len(body["cards"]) >= 8
    names = {c["name"] for c in body["cards"]}
    assert "Gateway Status" in names
    assert "Connected Agents" in names
    assert "Guacamole Session State" in names

    html = api.get("/api/v1/analysis/operations/toolkit/live/?view=html")
    assert html.status_code == 200
    assert b"Live Commissioning" in html.content
    assert b"GREEN" in html.content or b"AMBER" in html.content or b"RED" in html.content

    tl = api.get("/api/v1/analysis/operations/toolkit/live/timeline/")
    assert tl.status_code == 200
    assert "events" in tl.json()


@pytest.mark.django_db
def test_fault_injection_dry_run_and_heartbeat(api, eligible_workstation):
    catalog = api.get("/api/v1/analysis/operations/toolkit/faults/")
    assert catalog.status_code == 200
    assert any(f["id"] == "heartbeat_loss" for f in catalog.json()["faults"])

    dry = api.post(
        "/api/v1/analysis/operations/toolkit/faults/inject/",
        {"fault_id": "heartbeat_loss", "workstation_id": str(eligible_workstation.id), "dry_run": True},
        format="json",
    )
    assert dry.status_code == 200
    assert dry.json()["dry_run"] is True

    inj = api.post(
        "/api/v1/analysis/operations/toolkit/faults/inject/",
        {"fault_id": "heartbeat_loss", "workstation_id": str(eligible_workstation.id)},
        format="json",
    )
    assert inj.status_code == 200, inj.content
    assert inj.json()["ok"] is True
    assert inj.json()["commissioning_run_id"]

    eligible_workstation.refresh_from_db()
    assert eligible_workstation.last_heartbeat is not None

    hint = api.post(
        "/api/v1/analysis/operations/toolkit/faults/inject/",
        {"fault_id": "gateway_restart_hint"},
        format="json",
    )
    assert hint.status_code == 200
    assert hint.json().get("operator_action_required") is True

    html = api.get("/api/v1/analysis/operations/toolkit/faults/?view=html")
    assert html.status_code == 200
    assert b"Fault Injection" in html.content