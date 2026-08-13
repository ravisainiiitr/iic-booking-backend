"""Phase R.2.3 — Trusted Department Auto-Approve tests."""

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
    ProvisioningSession,
    ProvisioningSessionStatus,
)
from iic_booking.device_provisioning import policy as policy_mod
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType

User = get_user_model()


@pytest.fixture
def department(db):
    return Department.objects.create(
        name="Chemistry",
        code="CHEM-R23",
        department_type=DepartmentType.INTERNAL,
    )


@pytest.fixture
def other_department(db):
    return Department.objects.create(
        name="Physics",
        code="PHY-R23",
        department_type=DepartmentType.INTERNAL,
    )


@pytest.fixture
def dept_admin(db, department):
    user = User.objects.create_user(
        email="dept-admin-r23@example.com",
        password="test-pass-12345",
        user_type=UserType.DEPT_ADMIN,
        name="Dept Admin",
        department=department,
    )
    Token.objects.get_or_create(user=user)
    return user


@pytest.fixture
def main_admin(db):
    user = User.objects.create_superuser(
        email="main-admin-r23@example.com",
        password="test-pass-12345",
        user_type=UserType.ADMIN,
        name="Main Admin",
    )
    Token.objects.get_or_create(user=user)
    return user


@pytest.fixture
def dept_client(dept_admin):
    client = APIClient()
    token = Token.objects.get(user=dept_admin)
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


@pytest.fixture
def admin_client(main_admin):
    client = APIClient()
    token = Token.objects.get(user=main_admin)
    client.credentials(HTTP_AUTHORIZATION=f"Token {token.key}")
    return client


@pytest.fixture
def anon_client():
    return APIClient()


def _payload(department_id=None, **overrides):
    data = {
        "device_type": DeviceType.DSA,
        "machine_guid": "GUID-R23-001",
        "hostname": "LAB-DSA-R23",
        "windows_version": "Windows 11 Pro",
        "cpu": "Intel",
        "ram_gb": 16,
        "mac_addresses": ["11:22:33:44:55:66"],
        "local_ips": ["10.10.1.50"],
        "application_version": "1.0.0",
        "bootstrap_public_key": "pk-test",
    }
    if department_id is not None:
        data["department_id"] = department_id
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_new_department_gets_trusted_default(department):
    policy = DepartmentProvisioningPolicy.objects.get(department=department)
    assert policy.provisioning_mode == ProvisioningMode.TRUSTED_AUTO_APPROVE


@pytest.mark.django_db
def test_missing_policy_lazy_trusted_backfill(dept_client, department, dept_admin):
    """Departments that predate R.2.3 get TRUSTED on first authenticated create."""
    DepartmentProvisioningPolicy.objects.filter(department=department).delete()
    assert not DepartmentProvisioningPolicy.objects.filter(department=department).exists()

    resp = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _payload(department_id=department.id, machine_guid="GUID-LAZY-POLICY"),
        format="json",
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == ProvisioningSessionStatus.APPROVED
    assert body["auto_approved"] is True
    policy = DepartmentProvisioningPolicy.objects.get(department=department)
    assert policy.provisioning_mode == ProvisioningMode.TRUSTED_AUTO_APPROVE


@pytest.mark.django_db
def test_trusted_auto_approve_install_login_finish(dept_client, department, dept_admin):
    resp = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _payload(department_id=department.id, machine_guid="GUID-TRUSTED"),
        format="json",
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == ProvisioningSessionStatus.APPROVED
    assert body["auto_approved"] is True
    assert body["session_proof"]
    assert DeviceAuditLog.objects.filter(action=AuditAction.AUTO_APPROVED).exists()
    assert DeviceAuditLog.objects.filter(action=AuditAction.PROVISIONING_STARTED).exists()

    claim = APIClient().post(
        f"/api/v1/provisioning/sessions/{body['id']}/claim/",
        {"session_proof": body["session_proof"]},
        format="json",
    )
    assert claim.status_code == 200
    assert claim.json()["access_token"]


@pytest.mark.django_db
def test_manual_department_stays_pending(dept_client, department, anon_client):
    DepartmentProvisioningPolicy.objects.filter(department=department).update(
        provisioning_mode=ProvisioningMode.MANUAL_APPROVAL
    )
    resp = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _payload(department_id=department.id, machine_guid="GUID-MANUAL"),
        format="json",
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == ProvisioningSessionStatus.PENDING
    assert resp.json()["auto_approved"] is False


@pytest.mark.django_db
def test_restricted_allowed_and_blocked_subnet(dept_client, department):
    DepartmentProvisioningPolicy.objects.filter(department=department).update(
        provisioning_mode=ProvisioningMode.RESTRICTED_AUTO_APPROVE,
        allowed_networks=["10.10.0.0/16"],
    )

    allowed = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _payload(department_id=department.id, machine_guid="GUID-NET-OK", local_ips=["10.10.1.50"]),
        format="json",
        REMOTE_ADDR="10.10.1.50",
    )
    assert allowed.status_code == 201
    assert allowed.json()["status"] == ProvisioningSessionStatus.APPROVED

    blocked = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _payload(
            department_id=department.id,
            machine_guid="GUID-NET-BLOCK",
            hostname="LAB-BLOCK",
            mac_addresses=["aa:bb:cc:dd:ee:01"],
            local_ips=["192.168.9.9"],
        ),
        format="json",
        REMOTE_ADDR="192.168.9.9",
    )
    assert blocked.status_code == 201
    assert blocked.json()["status"] == ProvisioningSessionStatus.PENDING
    assert blocked.json()["auto_approve_reason"] == "network_not_allowed"


@pytest.mark.django_db
def test_duplicate_active_device_rejected(dept_client, department):
    first = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _payload(department_id=department.id, machine_guid="GUID-DUP"),
        format="json",
    )
    assert first.status_code == 201
    proof = first.json()["session_proof"]
    claim = APIClient().post(
        f"/api/v1/provisioning/sessions/{first.json()['id']}/claim/",
        {"session_proof": proof},
        format="json",
    )
    assert claim.status_code == 200

    second = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _payload(department_id=department.id, machine_guid="GUID-DUP"),
        format="json",
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "duplicate_active_device"


@pytest.mark.django_db
def test_revoked_device_reenroll_auto_approve(dept_client, department, admin_client):
    created = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _payload(department_id=department.id, machine_guid="GUID-REV"),
        format="json",
    )
    assert created.status_code == 201
    device_id = created.json()["device_uuid"]
    proof = created.json()["session_proof"]
    claim = APIClient().post(
        f"/api/v1/provisioning/sessions/{created.json()['id']}/claim/",
        {"session_proof": proof},
        format="json",
    )
    assert claim.status_code == 200

    revoke = admin_client.post(f"/api/v1/provisioning/devices/{device_id}/revoke/")
    assert revoke.status_code == 200
    assert revoke.json()["lifecycle"] == DeviceLifecycle.REVOKED

    reenroll = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _payload(department_id=department.id, machine_guid="GUID-REV", hostname="LAB-DSA-R23"),
        format="json",
    )
    assert reenroll.status_code == 201
    assert reenroll.json()["status"] == ProvisioningSessionStatus.APPROVED
    assert reenroll.json()["auto_approved"] is True


@pytest.mark.django_db
def test_device_code_mode(dept_client, department, admin_client):
    DepartmentProvisioningPolicy.objects.filter(department=department).update(
        provisioning_mode=ProvisioningMode.DEVICE_CODE
    )
    created = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _payload(department_id=department.id, machine_guid="GUID-CODE"),
        format="json",
    )
    assert created.status_code == 201
    assert created.json()["status"] == ProvisioningSessionStatus.PENDING
    code = created.json()["device_code"]
    assert code and "-" in code

    approved = admin_client.post(
        "/api/v1/provisioning/pending/approve-by-code/",
        {"device_code": code, "department_id": department.id},
        format="json",
    )
    assert approved.status_code == 200
    assert approved.json()["session"]["status"] == ProvisioningSessionStatus.APPROVED


@pytest.mark.django_db
def test_unauthenticated_never_auto_approves(anon_client, department):
    DepartmentProvisioningPolicy.objects.filter(department=department).update(
        provisioning_mode=ProvisioningMode.TRUSTED_AUTO_APPROVE
    )
    resp = anon_client.post(
        "/api/v1/provisioning/sessions/",
        _payload(department_id=department.id, machine_guid="GUID-ANON"),
        format="json",
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == ProvisioningSessionStatus.PENDING


@pytest.mark.django_db
def test_wrong_department_admin_denied(dept_client, department, other_department):
    DepartmentProvisioningPolicy.objects.get_or_create(
        department=other_department,
        defaults={"provisioning_mode": ProvisioningMode.TRUSTED_AUTO_APPROVE},
    )
    resp = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _payload(department_id=other_department.id, machine_guid="GUID-WRONG-DEPT"),
        format="json",
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == ProvisioningSessionStatus.PENDING
    assert resp.json()["auto_approve_reason"] == "administrator_not_in_department"


@pytest.mark.django_db
def test_policy_api_update(admin_client, department):
    resp = admin_client.put(
        f"/api/v1/provisioning/policies/{department.id}/",
        {
            "provisioning_mode": ProvisioningMode.RESTRICTED_AUTO_APPROVE,
            "allowed_networks": ["172.16.0.0/12"],
            "require_mfa": False,
        },
        format="json",
    )
    assert resp.status_code == 200
    assert resp.json()["provisioning_mode"] == ProvisioningMode.RESTRICTED_AUTO_APPROVE
    assert resp.json()["allowed_networks"] == ["172.16.0.0/12"]


@pytest.mark.django_db
def test_ip_in_allowed_networks_helper():
    assert policy_mod.ip_in_allowed_networks("10.1.2.3", ["10.1.0.0/16"])
    assert not policy_mod.ip_in_allowed_networks("192.168.1.1", ["10.1.0.0/16"])
    assert policy_mod.ip_in_allowed_networks(
        "8.8.8.8",
        ["10.0.0.0/8"],
        extra_ips=["10.2.3.4"],
    )


@pytest.mark.django_db
def test_department_administrator_login_mode_allows_dept_admin(department, dept_admin):
    policy, _ = DepartmentProvisioningPolicy.objects.get_or_create(
        department=department,
        defaults={"provisioning_mode": ProvisioningMode.DEPARTMENT_ADMINISTRATOR_LOGIN},
    )
    policy.provisioning_mode = ProvisioningMode.DEPARTMENT_ADMINISTRATOR_LOGIN
    policy.save(update_fields=["provisioning_mode"])
    decision = policy_mod.evaluate_auto_approve(
        user=dept_admin,
        department=department,
        policy=policy,
        fingerprint="fp-test",
        client_ip="10.10.1.50",
        local_ips=["10.10.1.50"],
        device_type=DeviceType.DSA,
        mfa_satisfied=False,
    )
    assert decision.allow is True, decision
    assert decision.reason == "department_administrator_login"
    assert decision.mode == ProvisioningMode.DEPARTMENT_ADMINISTRATOR_LOGIN


@pytest.mark.django_db
def test_department_administrator_login_mode_rejects_oic(department, db):
    oic = User.objects.create_user(
        email="oic-r23@example.com",
        password="test-pass-12345",
        user_type=UserType.MANAGER,
        name="OIC User",
        department=department,
    )
    policy, _ = DepartmentProvisioningPolicy.objects.get_or_create(
        department=department,
        defaults={"provisioning_mode": ProvisioningMode.DEPARTMENT_ADMINISTRATOR_LOGIN},
    )
    policy.provisioning_mode = ProvisioningMode.DEPARTMENT_ADMINISTRATOR_LOGIN
    policy.save(update_fields=["provisioning_mode"])
    decision = policy_mod.evaluate_auto_approve(
        user=oic,
        department=department,
        policy=policy,
        fingerprint="fp-oic",
        client_ip="10.10.1.50",
        local_ips=["10.10.1.50"],
        device_type=DeviceType.DSA,
        mfa_satisfied=False,
    )
    assert decision.allow is False
    assert decision.reason == "department_administrator_login_required"


@pytest.mark.django_db
def test_remove_retired_device(admin_client, department):
    device = ProvisionedDevice.objects.create(
        device_type=DeviceType.DSA,
        lifecycle=DeviceLifecycle.RETIRED,
        display_name="Retired DSA",
        hostname="RETIRED-HOST",
        fingerprint="fp-retired-remove-1",
        machine_guid="GUID-RETIRED-REMOVE",
        department=department,
    )
    resp = admin_client.delete(f"/api/v1/provisioning/devices/{device.id}/remove/")
    assert resp.status_code == 200
    assert resp.json().get("ok") is True
    assert not ProvisionedDevice.objects.filter(id=device.id).exists()
    assert DeviceAuditLog.objects.filter(action=AuditAction.DEVICE_REMOVED).exists()


@pytest.mark.django_db
def test_remove_active_device_rejected(admin_client, department):
    device = ProvisionedDevice.objects.create(
        device_type=DeviceType.DSA,
        lifecycle=DeviceLifecycle.ACTIVE,
        display_name="Active DSA",
        hostname="ACTIVE-HOST",
        fingerprint="fp-active-remove-1",
        machine_guid="GUID-ACTIVE-REMOVE",
        department=department,
    )
    resp = admin_client.delete(f"/api/v1/provisioning/devices/{device.id}/remove/")
    assert resp.status_code == 400
    assert ProvisionedDevice.objects.filter(id=device.id).exists()
