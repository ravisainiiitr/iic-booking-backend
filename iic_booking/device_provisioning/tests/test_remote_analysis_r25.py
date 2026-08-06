"""Phase R.2.5 — Remote Analysis Agent Zero-Touch provisioning tests."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from iic_booking.device_provisioning.models import (
    AuditAction,
    DepartmentProvisioningPolicy,
    DeviceAuditLog,
    DeviceLifecycle,
    DeviceType,
    ProvisionedDevice,
    ProvisioningMode,
    ProvisioningSessionStatus,
)
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType

User = get_user_model()


@pytest.fixture
def department(db):
    return Department.objects.create(
        name="Analysis Lab",
        code="ANAL-R25",
        department_type=DepartmentType.INTERNAL,
    )


@pytest.fixture
def dept_admin(db, department):
    user = User.objects.create_user(
        email="raa-admin@example.com",
        password="test-pass-12345",
        user_type=UserType.DEPT_ADMIN,
        name="RAA Admin",
        department=department,
    )
    Token.objects.get_or_create(user=user)
    return user


@pytest.fixture
def admin_client(db):
    user = User.objects.create_superuser(
        email="raa-main@example.com",
        password="test-pass-12345",
        user_type=UserType.ADMIN,
        name="Main",
    )
    Token.objects.get_or_create(user=user)
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.get(user=user).key}")
    return client


@pytest.fixture
def dept_client(dept_admin):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.get(user=dept_admin).key}")
    return client


def _raa_payload(department_id, **overrides):
    data = {
        "device_type": DeviceType.REMOTE_ANALYSIS,
        "machine_guid": "RAA-GUID-001",
        "hostname": "LAB-RAA-01",
        "windows_version": "Windows 11 Pro",
        "cpu": "Intel Xeon",
        "ram_gb": 64,
        "mac_addresses": ["aa:bb:cc:dd:ee:01"],
        "local_ips": ["10.10.3.30"],
        "application_version": "1.0.1",
        "department_id": department_id,
        "bootstrap_public_key": "pk-raa",
        "display_name": "LAB-RAA-01",
        "inventory": {
            "agent_id": "raa-lab-01",
            "cpu_cores": 16,
            "disk_free_gb": 400,
            "gpu": "NVIDIA RTX A4000",
        },
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_trusted_auto_approve_raa_claim_bridges_workstation(dept_client, department):
    DepartmentProvisioningPolicy.objects.filter(department=department).update(
        provisioning_mode=ProvisioningMode.TRUSTED_AUTO_APPROVE
    )
    resp = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _raa_payload(department.id),
        format="json",
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == ProvisioningSessionStatus.APPROVED
    assert body["auto_approved"] is True
    assert body["session_proof"]

    claim = APIClient().post(
        f"/api/v1/provisioning/sessions/{body['id']}/claim/",
        {"session_proof": body["session_proof"]},
        format="json",
    )
    assert claim.status_code == 200
    pack = claim.json()
    assert pack["access_token"]
    assert pack["device_type"] == DeviceType.REMOTE_ANALYSIS
    assert pack["agent_id"] == "raa-lab-01"
    assert pack["workstation_id"]
    assert pack["configuration"]["requires_windows_password"] is True
    assert pack["configuration"]["tunnel_policy"]["enabled"] is True
    assert pack["configuration"]["remote_analysis_settings"]["local_health_port"] == 5088

    ws = AnalysisWorkstation.objects.get(agent_id="raa-lab-01")
    assert str(ws.id) == pack["workstation_id"]
    assert ws.hostname == "LAB-RAA-01"
    assert DeviceAuditLog.objects.filter(action=AuditAction.PROVISION_COMPLETED).exists()
    assert DeviceAuditLog.objects.filter(action=AuditAction.AUTO_APPROVED).exists()


@pytest.mark.django_db
def test_manual_approval_raa(dept_client, department, admin_client):
    DepartmentProvisioningPolicy.objects.filter(department=department).update(
        provisioning_mode=ProvisioningMode.MANUAL_APPROVAL
    )
    created = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _raa_payload(department.id, machine_guid="RAA-MANUAL"),
        format="json",
    )
    assert created.status_code == 201
    body = created.json()
    assert body["auto_approved"] is False
    assert body["status"] == ProvisioningSessionStatus.PENDING

    approved = admin_client.post(f"/api/v1/provisioning/sessions/{body['id']}/approve/")
    assert approved.status_code == 200

    claim = APIClient().post(
        f"/api/v1/provisioning/sessions/{body['id']}/claim/",
        {"session_proof": body["session_proof"]},
        format="json",
    )
    assert claim.status_code == 200
    assert claim.json()["access_token"]


@pytest.mark.django_db
def test_restricted_network_denies_auto_approve(dept_client, department):
    DepartmentProvisioningPolicy.objects.filter(department=department).update(
        provisioning_mode=ProvisioningMode.RESTRICTED_AUTO_APPROVE,
        allowed_networks=["10.99.0.0/16"],
    )
    resp = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _raa_payload(
            department.id,
            machine_guid="RAA-RESTRICT",
            local_ips=["10.10.3.30"],
        ),
        format="json",
    )
    assert resp.status_code == 201
    assert resp.json()["auto_approved"] is False
    assert resp.json()["status"] == ProvisioningSessionStatus.PENDING


@pytest.mark.django_db
def test_device_code_mode_raa(dept_client, department):
    DepartmentProvisioningPolicy.objects.filter(department=department).update(
        provisioning_mode=ProvisioningMode.DEVICE_CODE
    )
    resp = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _raa_payload(department.id, machine_guid="RAA-CODE"),
        format="json",
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["auto_approved"] is False
    assert body.get("device_code")


@pytest.mark.django_db
def test_duplicate_active_raa_blocked(dept_client, department):
    DepartmentProvisioningPolicy.objects.filter(department=department).update(
        provisioning_mode=ProvisioningMode.TRUSTED_AUTO_APPROVE
    )
    first = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _raa_payload(department.id),
        format="json",
    )
    assert first.status_code == 201
    claim = APIClient().post(
        f"/api/v1/provisioning/sessions/{first.json()['id']}/claim/",
        {"session_proof": first.json()["session_proof"]},
        format="json",
    )
    assert claim.status_code == 200

    second = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _raa_payload(department.id, hostname="LAB-RAA-DUP"),
        format="json",
    )
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "duplicate_active_device"


@pytest.mark.django_db
def test_replace_raa_allows_reprovision(dept_client, admin_client, department):
    DepartmentProvisioningPolicy.objects.filter(department=department).update(
        provisioning_mode=ProvisioningMode.TRUSTED_AUTO_APPROVE
    )
    created = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _raa_payload(department.id),
        format="json",
    )
    claim = APIClient().post(
        f"/api/v1/provisioning/sessions/{created.json()['id']}/claim/",
        {"session_proof": created.json()["session_proof"]},
        format="json",
    )
    device_id = claim.json()["device_uuid"]

    replaced = admin_client.post(f"/api/v1/provisioning/devices/{device_id}/replace/")
    assert replaced.status_code == 200
    assert ProvisionedDevice.objects.get(id=device_id).lifecycle == DeviceLifecycle.REVOKED
    assert DeviceAuditLog.objects.filter(action=AuditAction.DEVICE_REPLACED).exists()

    again = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _raa_payload(department.id, hostname="LAB-RAA-REPLACEMENT"),
        format="json",
    )
    assert again.status_code == 201
    assert again.json()["auto_approved"] is True
