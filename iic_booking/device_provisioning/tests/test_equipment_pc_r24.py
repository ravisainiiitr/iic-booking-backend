"""Phase R.2.4 — Equipment PC Zero-Touch provisioning tests."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from iic_booking.device_provisioning.models import (
    AuditAction,
    DepartmentProvisioningPolicy,
    DeviceAssignment,
    DeviceAuditLog,
    DeviceLifecycle,
    DeviceType,
    ProvisionedDevice,
    ProvisioningMode,
    ProvisioningSessionStatus,
)
from iic_booking.equipment.models import Equipment
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType

User = get_user_model()


@pytest.fixture
def department(db):
    return Department.objects.create(
        name="Metallurgy",
        code="META-R24",
        department_type=DepartmentType.INTERNAL,
    )


@pytest.fixture
def dept_admin(db, department):
    user = User.objects.create_user(
        email="epc-admin@example.com",
        password="test-pass-12345",
        user_type=UserType.DEPT_ADMIN,
        name="EPC Admin",
        department=department,
    )
    Token.objects.get_or_create(user=user)
    return user


@pytest.fixture
def admin_client(db):
    user = User.objects.create_superuser(
        email="epc-main@example.com",
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


@pytest.fixture
def equipment(db, department):
    return Equipment.objects.create(
        name="SEM Lab 1",
        code="SEM-R24-01",
        internal_department=department,
        dsa_enabled=True,
        dsa_share_name="Results",
    )


def _epc_payload(department_id, equipment_id=None, **overrides):
    data = {
        "device_type": DeviceType.EQUIPMENT_PC,
        "machine_guid": "EPC-GUID-001",
        "hostname": "LAB-EPC-01",
        "windows_version": "Windows 11",
        "mac_addresses": ["aa:11:22:33:44:55"],
        "local_ips": ["10.10.2.20"],
        "application_version": "1.0.0",
        "department_id": department_id,
        "bootstrap_public_key": "pk",
    }
    if equipment_id is not None:
        data["equipment_id"] = equipment_id
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_unassigned_equipment_list(dept_client, department, equipment):
    resp = dept_client.get(f"/api/v1/provisioning/unassigned-equipment/?department_id={department.id}")
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.json()["results"]]
    assert equipment.id in ids


@pytest.mark.django_db
def test_trusted_auto_approve_with_equipment(dept_client, department, equipment):
    DepartmentProvisioningPolicy.objects.filter(department=department).update(
        provisioning_mode=ProvisioningMode.TRUSTED_AUTO_APPROVE
    )
    resp = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _epc_payload(department.id, equipment.id),
        format="json",
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == ProvisioningSessionStatus.APPROVED
    assert body["auto_approved"] is True

    claim = APIClient().post(
        f"/api/v1/provisioning/sessions/{body['id']}/claim/",
        {"session_proof": body["session_proof"]},
        format="json",
    )
    assert claim.status_code == 200
    pack = claim.json()
    assert pack["access_token"]
    assert pack["configuration"]["folders"]["results"]
    assert pack["configuration"]["share_name"] == "Results"
    assert DeviceAuditLog.objects.filter(action=AuditAction.EQUIPMENT_SELECTED).exists()
    assert DeviceAuditLog.objects.filter(action=AuditAction.PROVISION_COMPLETED).exists()

    # Assigned equipment disappears from unassigned list
    listed = dept_client.get(f"/api/v1/provisioning/unassigned-equipment/?department_id={department.id}")
    ids = [r["id"] for r in listed.json()["results"]]
    assert equipment.id not in ids


@pytest.mark.django_db
def test_duplicate_equipment_assignment_prevented(dept_client, department, equipment):
    DepartmentProvisioningPolicy.objects.filter(department=department).update(
        provisioning_mode=ProvisioningMode.TRUSTED_AUTO_APPROVE
    )
    first = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _epc_payload(department.id, equipment.id, machine_guid="EPC-A"),
        format="json",
    )
    assert first.status_code == 201
    second = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _epc_payload(
            department.id,
            equipment.id,
            machine_guid="EPC-B",
            hostname="LAB-EPC-02",
            mac_addresses=["bb:11:22:33:44:55"],
        ),
        format="json",
    )
    # approve_session raises equipment_already_assigned
    assert second.status_code == 400
    assert second.json()["error"]["code"] == "equipment_already_assigned"


@pytest.mark.django_db
def test_replace_releases_equipment(dept_client, admin_client, department, equipment):
    DepartmentProvisioningPolicy.objects.filter(department=department).update(
        provisioning_mode=ProvisioningMode.TRUSTED_AUTO_APPROVE
    )
    created = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _epc_payload(department.id, equipment.id),
        format="json",
    )
    assert created.status_code == 201
    claim = APIClient().post(
        f"/api/v1/provisioning/sessions/{created.json()['id']}/claim/",
        {"session_proof": created.json()["session_proof"]},
        format="json",
    )
    device_id = claim.json()["device_uuid"]

    replaced = admin_client.post(f"/api/v1/provisioning/devices/{device_id}/replace/")
    assert replaced.status_code == 200
    assert replaced.json()["device"]["lifecycle"] == DeviceLifecycle.REVOKED
    assert DeviceAuditLog.objects.filter(action=AuditAction.DEVICE_REPLACED).exists()

    listed = dept_client.get(f"/api/v1/provisioning/unassigned-equipment/?department_id={department.id}")
    ids = [r["id"] for r in listed.json()["results"]]
    assert equipment.id in ids


@pytest.mark.django_db
def test_epc_without_equipment_stays_pending_when_trusted(dept_client, department):
    DepartmentProvisioningPolicy.objects.filter(department=department).update(
        provisioning_mode=ProvisioningMode.TRUSTED_AUTO_APPROVE
    )
    resp = dept_client.post(
        "/api/v1/provisioning/sessions/",
        _epc_payload(department.id),
        format="json",
    )
    assert resp.status_code == 201
    assert resp.json()["status"] == ProvisioningSessionStatus.PENDING
    assert resp.json()["auto_approve_reason"] == "equipment_required"
