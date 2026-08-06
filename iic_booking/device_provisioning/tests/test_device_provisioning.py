"""Unit / API tests for unified Device Provisioning (Phase R.2.1)."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from iic_booking.device_provisioning.models import (
    AuditAction,
    DeviceAuditLog,
    DeviceBootstrapToken,
    DeviceLifecycle,
    DeviceType,
    ProvisionedDevice,
    ProvisioningSession,
    ProvisioningSessionStatus,
)
from iic_booking.device_provisioning import services
from iic_booking.users.models.user_type import UserType

User = get_user_model()


@pytest.fixture
def admin_user(db):
    user = User.objects.create_superuser(
        email="prov-admin@example.com",
        password="test-pass-12345",
        user_type=UserType.ADMIN,
        name="Provisioning Admin",
    )
    Token.objects.get_or_create(user=user)
    return user


@pytest.fixture
def admin_client(admin_user):
    client = APIClient()
    token = Token.objects.get(user=admin_user)
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


@pytest.fixture
def anon_client():
    return APIClient()


def _register_payload(**overrides):
    data = {
        "device_type": DeviceType.DSA,
        "machine_guid": "GUID-ABC-123",
        "hostname": "LAB-DSA-01",
        "windows_version": "Windows 11 Pro",
        "cpu": "Intel Xeon",
        "ram_gb": 32,
        "mac_addresses": ["aa:bb:cc:dd:ee:ff"],
        "local_ips": ["10.0.0.21"],
        "application_version": "1.0.0",
        "bootstrap_public_key": "ssh-test-key",
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_create_session_pending(anon_client):
    resp = anon_client.post("/api/v1/provisioning/sessions/", _register_payload(), format="json")
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "pending"
    assert body["session_proof"]
    assert "access_token" not in body
    assert ProvisioningSession.objects.count() == 1
    assert DeviceAuditLog.objects.filter(action=AuditAction.CREATED).exists()
    # Hash only at rest
    session = ProvisioningSession.objects.get()
    assert session.session_proof_hash
    assert body["session_proof"] not in session.session_proof_hash


@pytest.mark.django_db
def test_pending_requires_admin(anon_client, admin_client):
    assert anon_client.get("/api/v1/provisioning/pending/").status_code in {401, 403}
    assert admin_client.get("/api/v1/provisioning/pending/").status_code == 200


@pytest.mark.django_db
def test_full_provisioning_workflow(anon_client, admin_client, admin_user):
    created = anon_client.post("/api/v1/provisioning/sessions/", _register_payload(), format="json")
    assert created.status_code == 201
    session_id = created.json()["id"]
    proof = created.json()["session_proof"]

    pending = admin_client.get("/api/v1/provisioning/pending/")
    assert pending.status_code == 200
    assert pending.json()["count"] >= 1

    approve = admin_client.post(
        f"/api/v1/provisioning/pending/{session_id}/approve/",
        {"display_name": "DSA Lab 1"},
        format="json",
    )
    assert approve.status_code == 200
    assert "bootstrap_token" not in approve.json()
    assert "access_token" not in approve.json()
    device_uuid = approve.json()["device"]["id"]
    assert approve.json()["device"]["lifecycle"] == DeviceLifecycle.PROVISIONING

    poll = anon_client.get(
        f"/api/v1/provisioning/sessions/{session_id}/",
        HTTP_X_PROVISIONING_SESSION_PROOF=proof,
    )
    assert poll.status_code == 200
    assert poll.json()["status"] == ProvisioningSessionStatus.APPROVED

    claim = anon_client.post(
        f"/api/v1/provisioning/sessions/{session_id}/claim/",
        {"session_proof": proof},
        format="json",
    )
    assert claim.status_code == 200
    pack = claim.json()
    assert pack["device_uuid"] == device_uuid
    assert pack["access_token"]
    assert pack["configuration"]["device_type"] == DeviceType.DSA
    assert DeviceBootstrapToken.objects.filter(session_id=session_id, used_at__isnull=False).exists()

    device = ProvisionedDevice.objects.get(id=device_uuid)
    assert device.lifecycle == DeviceLifecycle.ACTIVE
    assert DeviceAuditLog.objects.filter(action=AuditAction.PROVISIONED).exists()

    # Second claim fails
    claim2 = anon_client.post(
        f"/api/v1/provisioning/sessions/{session_id}/claim/",
        {"session_proof": proof},
        format="json",
    )
    assert claim2.status_code == 400

    hb = anon_client.post(
        f"/api/v1/provisioning/devices/{device_uuid}/heartbeat/",
        {"status": "ok"},
        format="json",
        HTTP_AUTHORIZATION=f"Bearer {pack['access_token']}",
    )
    assert hb.status_code == 200

    retire = admin_client.post(f"/api/v1/provisioning/devices/{device_uuid}/retire/")
    assert retire.status_code == 200
    assert retire.json()["lifecycle"] == DeviceLifecycle.RETIRED


@pytest.mark.django_db
def test_reject_session(anon_client, admin_client):
    created = anon_client.post("/api/v1/provisioning/sessions/", _register_payload(hostname="X"), format="json")
    sid = created.json()["id"]
    resp = admin_client.post(
        f"/api/v1/provisioning/pending/{sid}/reject/",
        {"reason": "unknown host"},
        format="json",
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == ProvisioningSessionStatus.REJECTED


@pytest.mark.django_db
def test_equipment_pc_and_ra_types(anon_client, admin_client):
    for dtype in (DeviceType.EQUIPMENT_PC, DeviceType.REMOTE_ANALYSIS):
        created = anon_client.post(
            "/api/v1/provisioning/sessions/",
            _register_payload(device_type=dtype, hostname=f"host-{dtype}", machine_guid=f"g-{dtype}"),
            format="json",
        )
        assert created.status_code == 201
        sid = created.json()["id"]
        proof = created.json()["session_proof"]
        body = {"display_name": f"Dev {dtype}"}
        if dtype == DeviceType.REMOTE_ANALYSIS:
            body["workstation_role"] = "analysis"
        approve = admin_client.post(f"/api/v1/provisioning/pending/{sid}/approve/", body, format="json")
        assert approve.status_code == 200
        claim = anon_client.post(
            f"/api/v1/provisioning/sessions/{sid}/claim/",
            {"session_proof": proof},
            format="json",
        )
        assert claim.status_code == 200
        assert claim.json()["device_type"] == dtype


@pytest.mark.django_db
def test_console_and_audit(admin_client, anon_client):
    anon_client.post("/api/v1/provisioning/sessions/", _register_payload(), format="json")
    console = admin_client.get("/api/v1/provisioning/console/")
    assert console.status_code == 200
    assert console.json()["pending_installations"] >= 1
    audit = admin_client.get("/api/v1/provisioning/audit/")
    assert audit.status_code == 200
    assert audit.json()["count"] >= 1


@pytest.mark.django_db
def test_unsupported_device_type(anon_client):
    resp = anon_client.post(
        "/api/v1/provisioning/sessions/",
        _register_payload(device_type="toaster"),
        format="json",
    )
    assert resp.status_code == 400


@pytest.mark.django_db
def test_service_fingerprint_stable():
    a = services.compute_fingerprint(
        machine_guid="g1", hostname="h1", mac_addresses=["b", "a"], device_type="dsa"
    )
    b = services.compute_fingerprint(
        machine_guid="g1", hostname="h1", mac_addresses=["a", "b"], device_type="dsa"
    )
    assert a == b
