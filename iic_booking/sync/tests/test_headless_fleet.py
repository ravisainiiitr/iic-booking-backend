"""Headless DSA observability: diagnostics ingest, health/incidents, debounced alerts, fleet API, remote actions."""

from __future__ import annotations

import base64
import io
import uuid
import zipfile
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.sync.models import AgentCommand
from iic_booking.sync.models import AgentCommandStatus
from iic_booking.sync.models import AgentCommandType
from iic_booking.sync.models import AgentDiagnosticState
from iic_booking.sync.models import AgentHeartbeat
from iic_booking.sync.models import AgentLifecycleStatus
from iic_booking.sync.models import AgentSupportBundle
from iic_booking.sync.models import AlertEvent
from iic_booking.sync.models import DepartmentSyncAgent
from iic_booking.sync.models import SecurityAuditEvent
from iic_booking.sync.services import headless
from iic_booking.sync.services.dataplane import CommandService
from iic_booking.sync.services.heartbeat import HeartbeatService
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType

ALL_COMMANDS = [
    "RESTART_AGENT",
    "SYNCHRONIZE_BOOKINGS",
    "RETRY_FAILED_UPLOADS",
    "COLLECT_SUPPORT_BUNDLE",
    "REFRESH_EQUIPMENT_PCS",
    "RUN_DIAGNOSTICS",
]


def _agent(**kwargs) -> DepartmentSyncAgent:
    dept = Department.objects.create(name=f"HL-{uuid.uuid4().hex[:8]}", code=f"H{uuid.uuid4().hex[:6].upper()}")
    defaults = {
        "agent_name": "Headless Agent",
        "department": dept,
        "machine_guid": uuid.uuid4(),
        "status": AgentLifecycleStatus.ENROLLED,
        "is_active": True,
    }
    defaults.update(kwargs)
    return DepartmentSyncAgent.objects.create(**defaults)


def _diagnostics(**overrides) -> dict:
    diag = {
        "diagnostics_version": 1,
        "service": {"version": "1.0.7", "uptime_seconds": 3600, "running_as_service": True},
        "portal": {"heartbeat_failure_streak": 0},
        "enrollment": {"enrolled": True, "token_valid": True, "token_expires_in_hours": 700},
        "storage": {"data_drive": "C:\\", "data_drive_free_percent": 55.0, "sqlite_bytes": 10_000_000},
        "equipment_network": {
            "enabled": True,
            "adapter_found": True,
            "adapter_up": True,
            "adapter_ip": "10.250.0.1",
            "configured_ip": "10.250.0.1",
            "ip_matches_config": True,
        },
        "uploads": {"pending": 0, "failed": 0, "detected_files_failed": 0, "oldest_pending_age_minutes": None},
        "problems": {"errors_1h": 0, "warnings_1h": 0, "recent": []},
        "live_feed": {"active_sessions": 0},
        "ups": {"configured": False},
    }
    for key, value in overrides.items():
        diag[key] = {**diag.get(key, {}), **value}
    return diag


def _beat(agent, diagnostics=None, *, pcs=None, sent_at=None, capabilities=None):
    details = {"capabilities": {"remote_commands": capabilities if capabilities is not None else ALL_COMMANDS}}
    if diagnostics is not None:
        details["diagnostics"] = diagnostics
    payload = {
        "service_version": "1.0.7",
        "hostname": "DEPT-PC",
        "sent_at": (sent_at or timezone.now()).isoformat(),
        "details": details,
        "equipment_pcs": pcs or [],
    }
    return HeartbeatService().process(agent, payload)


def _admin():
    from iic_booking.users.tests.factories import UserFactory

    return UserFactory(user_type=UserType.ADMIN, admin_approved=True, email_verified=True)


@pytest.mark.django_db
def test_heartbeat_stores_diagnostics_once_and_keeps_rows_small():
    agent = _agent()
    _beat(agent, _diagnostics(), sent_at=timezone.now() + timedelta(seconds=30))

    state = AgentDiagnosticState.objects.get(sync_agent=agent)
    assert state.diagnostics["service"]["version"] == "1.0.7"
    assert 25 <= state.clock_skew_seconds <= 35
    assert state.health == "healthy"
    hb = AgentHeartbeat.objects.get(sync_agent=agent)
    assert "diagnostics" not in hb.details
    assert "RETRY_FAILED_UPLOADS" in state.capabilities["remote_commands"]


@pytest.mark.django_db
def test_old_agent_without_diagnostics_is_flagged_not_broken():
    agent = _agent()
    _beat(agent, None, capabilities=[])
    state = AgentDiagnosticState.objects.get(sync_agent=agent)
    assert state.health == "healthy"
    assert any(r["code"] == "NO_DIAGNOSTICS" for r in state.reasons)


@pytest.mark.django_db
def test_stuck_uploads_open_an_incident_that_resolves_when_cleared():
    agent = _agent()
    _beat(agent, _diagnostics(uploads={"pending": 3, "oldest_pending_age_minutes": 95, "last_error": "HTTP 503"}))
    state = AgentDiagnosticState.objects.get(sync_agent=agent)
    assert state.health == "critical"
    incident = AlertEvent.objects.get(sync_agent=agent, rule_code="UPLOADS_STUCK")
    assert incident.status == "NEW"
    assert "95 min" in incident.message

    _beat(agent, _diagnostics())
    incident.refresh_from_db()
    assert incident.status == "RESOLVED"
    assert AgentDiagnosticState.objects.get(sync_agent=agent).health == "healthy"


@pytest.mark.django_db
def test_disk_network_token_skew_and_error_spike_rules():
    agent = _agent()
    _beat(
        agent,
        _diagnostics(
            storage={"data_drive_free_percent": 4.0},
            equipment_network={"adapter_up": False},
            enrollment={"token_valid": False},
            problems={"errors_1h": 45},
        ),
        sent_at=timezone.now() - timedelta(minutes=10),
    )
    codes = {r["code"]: r["severity"] for r in AgentDiagnosticState.objects.get(sync_agent=agent).reasons}
    assert codes["DISK_LOW"] == "critical"
    assert codes["EQUIPMENT_NETWORK_DOWN"] == "critical"
    assert codes["TOKEN_INVALID"] == "critical"
    assert codes["ERROR_SPIKE"] == "warning"
    assert codes["CLOCK_SKEW"] == "warning"


@pytest.mark.django_db
def test_equipment_pc_offline_is_tracked_per_pc():
    agent = _agent()
    now = timezone.now()
    pcs = [
        {"id": "a", "hostname": "LAB-PC-1", "registration_status": "registered", "last_seen": now.isoformat(),
         "share_ok": True},
        {"id": "b", "hostname": "LAB-PC-2", "registration_status": "registered",
         "last_seen": (now - timedelta(hours=2)).isoformat()},
        {"id": "c", "hostname": "OLD-PC", "registration_status": "unassigned"},
    ]
    _beat(agent, _diagnostics(), pcs=pcs)
    incidents = AlertEvent.objects.filter(sync_agent=agent, rule_code="EQPC_OFFLINE")
    assert [i.details["subject"] for i in incidents] == ["LAB-PC-2"]


@pytest.mark.django_db
def test_offline_alert_is_debounced_and_notified_once(settings):
    settings.DSA_ALERT_GRACE_MINUTES = 10
    admin = _admin()
    agent = _agent()
    _beat(agent, _diagnostics())
    DepartmentSyncAgent.objects.filter(pk=agent.pk).update(last_heartbeat_at=timezone.now() - timedelta(minutes=20))

    t0 = timezone.now()
    with patch("iic_booking.communication.service.CommunicationService.send_email") as send, patch(
        "iic_booking.communication.in_app.notify_in_app",
    ) as bell:
        headless.run_all(now=t0)
        assert AgentDiagnosticState.objects.get(sync_agent=agent).health == "offline"
        assert AlertEvent.objects.filter(sync_agent=agent, rule_code="DSA_OFFLINE", status="NEW").count() == 1
        assert send.call_count == 0  # still inside the grace period

        headless.run_all(now=t0 + timedelta(minutes=11))
        assert send.call_count == 1
        assert send.call_args.kwargs["recipient"] == admin
        assert send.call_args.kwargs["template"] == headless.EMAIL_TEMPLATE
        assert bell.call_count == 1

        headless.run_all(now=t0 + timedelta(minutes=30))
        assert send.call_count == 1  # not repeated before DSA_ALERT_REPEAT_HOURS

        headless.run_all(now=t0 + timedelta(hours=13))
        assert send.call_count == 2  # reminder


@pytest.mark.django_db
def test_recovery_resolves_and_tells_admins_once():
    _admin()
    agent = _agent()
    _beat(agent, _diagnostics())
    DepartmentSyncAgent.objects.filter(pk=agent.pk).update(last_heartbeat_at=timezone.now() - timedelta(minutes=30))
    agent.refresh_from_db()
    with patch("iic_booking.communication.service.CommunicationService.send_email"), patch(
        "iic_booking.communication.in_app.notify_in_app",
    ) as bell:
        headless.run_all(now=timezone.now())
        AlertEvent.objects.filter(sync_agent=agent).update(created_at=timezone.now() - timedelta(minutes=30))
        headless.run_all(now=timezone.now())
        assert bell.call_count == 1
        _beat(agent, _diagnostics())  # back online
        headless.run_all(now=timezone.now())
        assert bell.call_count == 2
        assert "recovered" in bell.call_args.kwargs["title"]
        headless.run_all(now=timezone.now())
        assert bell.call_count == 2
    assert AlertEvent.objects.get(sync_agent=agent, rule_code="DSA_OFFLINE").status == "RESOLVED"


@pytest.mark.django_db
def test_fleet_endpoints_are_main_admin_only():
    from iic_booking.users.tests.factories import UserFactory

    agent = _agent()
    _beat(agent, _diagnostics())
    client = APIClient()
    staff = UserFactory(user_type=UserType.DEPT_ADMIN, is_staff=True)
    client.force_authenticate(staff)
    assert client.get("/api/v1/sync/admin/fleet/").status_code == 403
    assert client.post(f"/api/v1/sync/admin/fleet/{agent.id}/actions/", {"action": "restart_service"}).status_code == 403

    client.force_authenticate(_admin())
    resp = client.get("/api/v1/sync/admin/fleet/")
    assert resp.status_code == 200
    row = next(r for r in resp.json()["results"] if r["id"] == str(agent.id))
    assert row["health"] == "healthy"
    assert row["version"] == "1.0.7"

    detail = client.get(f"/api/v1/sync/admin/fleet/{agent.id}/").json()
    assert detail["diagnostics"]["equipment_network"]["adapter_ip"] == "10.250.0.1"
    assert {a["key"] for a in detail["actions"] if a["supported"]} >= {"restart_service", "support_bundle"}


@pytest.mark.django_db
def test_fleet_action_queues_audited_command_and_blocks_duplicates():
    agent = _agent()
    _beat(agent, _diagnostics())
    client = APIClient()
    client.force_authenticate(_admin())
    url = f"/api/v1/sync/admin/fleet/{agent.id}/actions/"

    resp = client.post(url, {"action": "retry_failed_uploads", "reason": "stuck since morning"}, format="json")
    assert resp.status_code == 201, resp.content
    cmd = AgentCommand.objects.get(sync_agent=agent, command_type=AgentCommandType.RETRY_FAILED_UPLOADS)
    assert cmd.status == AgentCommandStatus.PENDING
    assert cmd.payload["reason"] == "stuck since morning"
    assert SecurityAuditEvent.objects.filter(sync_agent=agent, event_code="DSA-REMOTE-ACTION").exists()

    assert client.post(url, {"action": "retry_failed_uploads"}, format="json").status_code == 409


@pytest.mark.django_db
def test_restart_has_cooldown_and_old_agents_refuse_new_actions():
    agent = _agent()
    _beat(agent, _diagnostics())
    client = APIClient()
    client.force_authenticate(_admin())
    url = f"/api/v1/sync/admin/fleet/{agent.id}/actions/"
    assert client.post(url, {"action": "restart_service"}, format="json").status_code == 201
    AgentCommand.objects.filter(sync_agent=agent).update(status=AgentCommandStatus.COMPLETED)
    assert client.post(url, {"action": "restart_service"}, format="json").status_code == 429
    assert client.post(url, {"action": "restart_service", "force": True}, format="json").status_code == 201

    old = _agent()
    _beat(old, None, capabilities=None)
    AgentDiagnosticState.objects.filter(sync_agent=old).update(capabilities={})
    old_url = f"/api/v1/sync/admin/fleet/{old.id}/actions/"
    assert client.post(old_url, {"action": "support_bundle"}, format="json").status_code == 409
    assert client.post(old_url, {"action": "resync_bookings"}, format="json").status_code == 201


@pytest.mark.django_db
def test_support_bundle_is_stored_apart_and_downloadable():
    agent = _agent()
    cmd = AgentCommand.objects.create(
        sync_agent=agent, command_type=AgentCommandType.COLLECT_SUPPORT_BUNDLE, status=AgentCommandStatus.ACKNOWLEDGED,
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("diagnostics.json", "{}")
    raw = base64.b64encode(buf.getvalue()).decode()
    CommandService().complete(
        agent,
        cmd.id,
        result_payload={"decision": "bundle_collected", "bundle_zip_base64": raw, "bundle_file_name": "b.zip"},
    )
    cmd.refresh_from_db()
    assert "bundle_zip_base64" not in cmd.result_payload
    bundle = AgentSupportBundle.objects.get(command=cmd)
    assert cmd.result_payload["bundle_id"] == str(bundle.id)

    client = APIClient()
    client.force_authenticate(_admin())
    resp = client.get(f"/api/v1/sync/admin/fleet/bundles/{bundle.id}/download/")
    assert resp.status_code == 200
    assert resp["Content-Type"] == "application/zip"
    assert zipfile.ZipFile(io.BytesIO(resp.content)).namelist() == ["diagnostics.json"]


@pytest.mark.django_db
def test_bad_bundle_is_rejected_not_stored():
    agent = _agent()
    cmd = AgentCommand.objects.create(
        sync_agent=agent, command_type=AgentCommandType.COLLECT_SUPPORT_BUNDLE, status=AgentCommandStatus.ACKNOWLEDGED,
    )
    CommandService().complete(
        agent, cmd.id, result_payload={"bundle_zip_base64": base64.b64encode(b"not a zip").decode()},
    )
    cmd.refresh_from_db()
    assert "bundle_error" in cmd.result_payload
    assert not AgentSupportBundle.objects.filter(command=cmd).exists()


@pytest.mark.django_db
def test_retention_trims_old_heartbeats_only():
    agent = _agent()
    old = timezone.now() - timedelta(days=45)
    AgentHeartbeat.objects.create(sync_agent=agent, reported_at=old)
    AgentHeartbeat.objects.create(sync_agent=agent, reported_at=timezone.now())
    out = headless.apply_retention()
    assert out["heartbeats"] == 1
    assert AgentHeartbeat.objects.filter(sync_agent=agent).count() == 1


@pytest.mark.django_db
def test_equipment_pc_status_comes_from_heartbeat_freshness_not_registration_status():
    """Live test 13 Nov 2026: cable unplugged, DSA still said "Online" with last_seen null."""
    agent = _agent()
    now = timezone.now()
    stale = (now - timedelta(minutes=3)).isoformat()
    pcs = [
        # DSA 1.0.7+: its own freshness verdict wins over the stored "Online".
        {"id": "a", "hostname": "DSATEST-LAPTOP", "registration_status": "Online", "online": False,
         "last_heartbeat_at": stale, "last_seen": None, "last_status_at": stale},
        # Older agent: no verdict, last_seen null but last_status_at fresh -> online.
        {"id": "b", "hostname": "LAB-PC-OK", "registration_status": "Online", "last_seen": None,
         "last_status_at": now.isoformat()},
        # Older agent, nothing ever received -> offline even though status says Online.
        {"id": "c", "hostname": "LAB-PC-NEVER", "registration_status": "Online"},
    ]
    _beat(agent, _diagnostics(), pcs=pcs)
    subjects = sorted(
        a.details["subject"] for a in AlertEvent.objects.filter(sync_agent=agent, rule_code="EQPC_OFFLINE", status="NEW")
    )
    assert subjects == ["DSATEST-LAPTOP", "LAB-PC-NEVER"]

    client = APIClient()
    client.force_authenticate(_admin())
    detail = client.get(f"/api/v1/sync/admin/fleet/{agent.id}/").json()
    verdicts = {pc["hostname"]: pc["online"] for pc in detail["equipment_pcs"]}
    assert verdicts == {"DSATEST-LAPTOP": False, "LAB-PC-OK": True, "LAB-PC-NEVER": False}
    assert detail["agent"]["equipment_pcs"] == {"total": 3, "online": 1}

    # Cable back: the next heartbeat reports it online and the incident resolves.
    pcs[0] = {**pcs[0], "online": True, "last_heartbeat_at": timezone.now().isoformat()}
    _beat(agent, _diagnostics(), pcs=pcs)
    laptop = AlertEvent.objects.get(sync_agent=agent, rule_code="EQPC_OFFLINE", details__subject="DSATEST-LAPTOP")
    assert laptop.status == "RESOLVED"


@pytest.mark.django_db
def test_connectivity_probe_does_not_wipe_diagnostics_or_clock_skew():
    """Live 13 Nov: the DSA's bare connectivity-probe heartbeat flipped the fleet row to 'no diagnostics'."""
    agent = _agent()
    _beat(agent, _diagnostics(), sent_at=timezone.now() + timedelta(seconds=30))
    HeartbeatService().process(
        agent,
        {"hostname": "DEPT-PC", "configuration_version": None, "schema_version": None,
         "status_message": "connectivity_probe"},
    )
    state = AgentDiagnosticState.objects.get(sync_agent=agent)
    assert "stale" not in state.diagnostics
    assert state.diagnostics["service"]["version"] == "1.0.7"
    assert 25 <= state.clock_skew_seconds <= 35
    assert not any(r["code"] == "NO_DIAGNOSTICS" for r in state.reasons)


@pytest.mark.django_db
def test_running_version_comes_from_diagnostics_not_enrollment_version():
    """Live 13 Nov: the fleet showed v1.0.6 for a DSA running 1.0.8 (heartbeat service_version is stale)."""
    agent = _agent()
    diag = _diagnostics(service={"version": "1.0.8+090c2341"})
    details = {"capabilities": {"remote_commands": ALL_COMMANDS}, "diagnostics": diag}
    HeartbeatService().process(
        agent,
        {"service_version": "1.0.6", "sent_at": timezone.now().isoformat(), "details": details, "equipment_pcs": []},
    )
    agent.refresh_from_db()
    assert agent.version == "1.0.8"
    client = APIClient()
    client.force_authenticate(_admin())
    row = next(r for r in client.get("/api/v1/sync/admin/fleet/").json()["results"] if r["id"] == str(agent.id))
    assert row["version"] == "1.0.8"


@pytest.mark.django_db
def test_admin_agents_list_uses_latest_heartbeat_per_agent():
    from iic_booking.sync.services.agent_management import _latest_heartbeat_map

    a, b = _agent(), _agent()
    now = timezone.now()
    for i in range(5):
        AgentHeartbeat.objects.create(sync_agent=a, reported_at=now - timedelta(minutes=i), queue_size=i)
    AgentHeartbeat.objects.create(sync_agent=b, reported_at=now - timedelta(hours=1), queue_size=99)
    latest = _latest_heartbeat_map([a.id, b.id, uuid.uuid4()])
    assert latest[a.id].queue_size == 0
    assert latest[b.id].queue_size == 99
    assert len(latest) == 2
