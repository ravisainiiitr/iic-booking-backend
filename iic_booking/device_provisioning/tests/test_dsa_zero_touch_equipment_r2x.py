"""Phase R.2.x — DSA zero-touch equipment tree without legacy enrollment secret."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from iic_booking.device_provisioning.models import (
    DeviceType,
)
from iic_booking.equipment.models import Equipment
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType

User = get_user_model()


@pytest.fixture
def department(db):
    return Department.objects.create(
        name="Chemistry",
        code="CHEM-R2X",
        department_type=DepartmentType.INTERNAL,
    )


@pytest.fixture
def dept_admin(db, department):
    user = User.objects.create_user(
        email="dept-admin-r2x@example.com",
        password="test-pass-12345",
        user_type=UserType.DEPT_ADMIN,
        name="Dept Admin",
        department=department,
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
def anon_client():
    return APIClient()


@pytest.fixture
def equipment(db, department):
    return Equipment.objects.create(
        name="FESEM R2X",
        code="FESEM-R2X",
        internal_department=department,
        dsa_enabled=True,
    )


def _session_payload(department_id, **overrides):
    data = {
        "device_type": DeviceType.DSA,
        "machine_guid": "GUID-R2X-DSA-001",
        "hostname": "LAB-DSA-R2X",
        "windows_version": "Windows 11 Pro",
        "cpu": "Intel",
        "ram_gb": 16,
        "mac_addresses": ["11:22:33:44:55:66"],
        "local_ips": ["10.10.1.50"],
        "application_version": "1.0.0",
        "department_id": department_id,
        "bootstrap_public_key": "r2x-bootstrap-pub",
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_claim_embeds_equipment_tree(dept_client, anon_client, department, equipment):
    created = dept_client.post("/api/v1/provisioning/sessions/", _session_payload(department.id), format="json")
    assert created.status_code == 201, created.content
    body = created.json()
    assert body.get("auto_approved") or body.get("status") == "approved"

    claim = anon_client.post(
        f"/api/v1/provisioning/sessions/{body['id']}/claim/",
        {"session_proof": body["session_proof"]},
        format="json",
    )
    assert claim.status_code == 200, claim.content
    pack = claim.json()
    assert pack["access_token"]
    assert pack["agent_uuid"]
    tree = pack.get("equipment_tree") or {}
    assert tree.get("departments")
    titles = {d["name"] for d in tree["departments"]}
    assert "Chemistry" in titles
    # No enrollment secret in claim pack
    assert "enrollment_secret" not in pack
    assert "enrollment_token" not in pack


@pytest.mark.django_db
def test_provisioning_equipment_tree_with_bearer_only(dept_client, anon_client, department, equipment):
    created = dept_client.post("/api/v1/provisioning/sessions/", _session_payload(department.id), format="json")
    body = created.json()
    claim = anon_client.post(
        f"/api/v1/provisioning/sessions/{body['id']}/claim/",
        {"session_proof": body["session_proof"]},
        format="json",
    )
    token = claim.json()["access_token"]

    # Bearer only — no Agent UUID, no enrollment secret
    tree = anon_client.get(
        "/api/v1/provisioning/dsa/equipment-tree/",
        HTTP_AUTHORIZATION=f"Bearer {token}",
    )
    assert tree.status_code == 200, tree.content
    assert tree.json()["auth"] == "device_bearer"
    assert any(e["name"] == "FESEM R2X" for d in tree.json()["departments"] for e in d["equipment"])


@pytest.mark.django_db
def test_provisioning_equipment_tree_with_access_token_header(
    dept_client, anon_client, department, equipment
):
    created = dept_client.post("/api/v1/provisioning/sessions/", _session_payload(department.id), format="json")
    body = created.json()
    claim = anon_client.post(
        f"/api/v1/provisioning/sessions/{body['id']}/claim/",
        {"session_proof": body["session_proof"]},
        format="json",
    )
    token = claim.json()["access_token"]

    # X-Agent-Access-Token only (Authorization stripped by some proxies)
    tree = anon_client.get(
        "/api/v1/provisioning/dsa/equipment-tree/",
        HTTP_X_AGENT_ACCESS_TOKEN=token,
    )
    assert tree.status_code == 200, tree.content
    assert tree.json()["count"] >= 1


@pytest.mark.django_db
def test_legacy_equipment_tree_accepts_bearer_without_secret(
    dept_client, anon_client, department, equipment
):
    created = dept_client.post("/api/v1/provisioning/sessions/", _session_payload(department.id), format="json")
    body = created.json()
    claim = anon_client.post(
        f"/api/v1/provisioning/sessions/{body['id']}/claim/",
        {"session_proof": body["session_proof"]},
        format="json",
    )
    pack = claim.json()
    tree = anon_client.get(
        "/api/v1/sync/installer/equipment-tree/",
        HTTP_X_AGENT_UUID=pack["agent_uuid"],
        HTTP_AUTHORIZATION=f"Bearer {pack['access_token']}",
    )
    assert tree.status_code == 200, tree.content


@pytest.mark.django_db
def test_legacy_equipment_tree_rejects_uuid_without_credentials(anon_client):
    resp = anon_client.get(
        "/api/v1/sync/installer/equipment-tree/",
        HTTP_X_AGENT_UUID="11111111-1111-1111-1111-111111111111",
    )
    assert resp.status_code == 403
    assert "enrollment secret" in resp.json()["detail"].lower() or "access token" in resp.json()["detail"].lower()


@pytest.mark.django_db
def test_provisioning_equipment_tree_with_portal_token(dept_client, department, equipment):
    tree = dept_client.get(f"/api/v1/provisioning/dsa/equipment-tree/?department_id={department.id}")
    assert tree.status_code == 200, tree.content
    assert tree.json()["auth"] == "portal_token"
