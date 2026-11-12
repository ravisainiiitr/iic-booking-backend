"""One-click DSA installer: setup keys + installer download trailer."""

from __future__ import annotations

import datetime
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.device_provisioning import setup_keys
from iic_booking.device_provisioning.models import AuditAction
from iic_booking.device_provisioning.models import DepartmentProvisioningPolicy
from iic_booking.device_provisioning.models import DeviceAuditLog
from iic_booking.device_provisioning.models import InstallerSetupKey
from iic_booking.device_provisioning.models import ProvisioningMode
from iic_booking.device_provisioning.models import ProvisioningSession
from iic_booking.device_provisioning.models import ProvisioningSessionStatus
from iic_booking.sync.installer.models import DsaInstallerRelease
from iic_booking.sync.installer.one_click import parse_trailer
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType

User = get_user_model()


@pytest.fixture(autouse=True)
def _clear_throttle_cache():
    from django.core.cache import cache

    cache.clear()  # anonymous provisioning throttle (30/hour) is shared across the suite


@pytest.fixture
def department(db):
    dept = Department.objects.create(name="Instrumentation", code="IICOC", department_type=DepartmentType.INTERNAL)
    DepartmentProvisioningPolicy.objects.create(
        department=dept, provisioning_mode=ProvisioningMode.TRUSTED_AUTO_APPROVE,
    )
    return dept


@pytest.fixture
def other_department(db):
    return Department.objects.create(name="Physics", code="PHYOC", department_type=DepartmentType.INTERNAL)


def _active(user):
    user.admin_approved = True  # is_active follows admin approval in this project
    user.save()
    return user


@pytest.fixture
def dept_admin(db, department):
    return _active(
        User.objects.create_user(
            email="oc-dept-admin@example.com",
            password="test-pass-12345",
            user_type=UserType.DEPT_ADMIN,
            name="Dept Admin",
            department=department,
        ),
    )


@pytest.fixture
def main_admin(db):
    return _active(
        User.objects.create_user(
            email="oc-main-admin@example.com",
            password="test-pass-12345",
            user_type=UserType.ADMIN,
            name="Main Admin",
        ),
    )


@pytest.fixture
def student(db, department):
    return User.objects.create_user(
        email="oc-student@example.com",
        password="test-pass-12345",
        user_type=UserType.STUDENT,
        name="Student",
        department=department,
    )


def _payload(**overrides):
    data = {
        "device_type": "dsa",
        "machine_guid": "GUID-ONE-CLICK",
        "hostname": "LAB-DSA-OC",
        "mac_addresses": ["00:11:22:33:44:55"],
        "local_ips": ["10.20.30.40"],
        "application_version": "1.0.7",
    }
    data.update(overrides)
    return data


@pytest.mark.django_db
def test_issue_stores_only_hash_and_caps_ttl(dept_admin, department):
    key, plaintext = setup_keys.issue(actor=dept_admin, department=department, ttl_hours=500)
    assert plaintext.startswith("dsk_")
    assert plaintext not in key.key_hash
    assert key.key_prefix == plaintext[:12]
    assert key.expires_at <= timezone.now() + timedelta(hours=24, minutes=1)
    assert DeviceAuditLog.objects.filter(action=AuditAction.SETUP_KEY_ISSUED).exists()


@pytest.mark.django_db
def test_issue_requires_provisioning_rights(student, department, dept_admin, other_department):
    with pytest.raises(PermissionError):
        setup_keys.issue(actor=student, department=department)
    with pytest.raises(PermissionError):
        setup_keys.issue(actor=dept_admin, department=other_department)


@pytest.mark.django_db
def test_setup_key_auto_approves_anonymous_installer_once(dept_admin, department):
    _key, plaintext = setup_keys.issue(actor=dept_admin, department=department)
    client = APIClient()  # installer: no sign-in

    resp = client.post("/api/v1/provisioning/sessions/", _payload(setup_key=plaintext), format="json")
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert body["status"] == ProvisioningSessionStatus.APPROVED
    assert body["session_proof"]
    session = ProvisioningSession.objects.get(pk=body["id"])
    assert session.department_id == department.pk
    assert session.requested_by_id == dept_admin.pk

    key = InstallerSetupKey.objects.get()
    assert key.used_at is not None
    assert key.used_session_id == session.pk

    again = client.post(
        "/api/v1/provisioning/sessions/",
        _payload(setup_key=plaintext, machine_guid="GUID-OTHER"),
        format="json",
    )
    assert again.status_code == 400
    assert again.json()["error"]["code"] == "invalid_setup_key"


@pytest.mark.django_db
def test_expired_revoked_and_wrong_type_keys_are_rejected(dept_admin, department):
    client = APIClient()
    _k1, expired = setup_keys.issue(actor=dept_admin, department=department)
    InstallerSetupKey.objects.filter(key_prefix=expired[:12]).update(
        expires_at=timezone.now() - timedelta(minutes=1),
    )
    k2, revoked = setup_keys.issue(actor=dept_admin, department=department)
    setup_keys.revoke(k2)
    _k3, wrong_type = setup_keys.issue(actor=dept_admin, department=department)

    for raw, extra in (
        (expired, {}),
        (revoked, {}),
        (wrong_type, {"device_type": "equipment_pc"}),
        ("dsk_not-a-real-key-at-all-xxxxxxxxxx", {}),
    ):
        resp = client.post(
            "/api/v1/provisioning/sessions/",
            _payload(setup_key=raw, machine_guid=f"GUID-{raw[-6:]}", **extra),
            format="json",
        )
        assert resp.status_code == 400, raw
        assert resp.json()["error"]["code"] == "invalid_setup_key"

    assert not ProvisioningSession.objects.exists()


@pytest.mark.django_db
def test_key_not_burnt_when_registration_fails(dept_admin, department):
    _key, plaintext = setup_keys.issue(actor=dept_admin, department=department)
    client = APIClient()
    bad = client.post(
        "/api/v1/provisioning/sessions/",
        _payload(setup_key=plaintext, device_type="dsa", equipment_id="not-a-number"),
        format="json",
    )
    assert bad.status_code == 400
    assert InstallerSetupKey.objects.get().used_at is None

    ok = client.post("/api/v1/provisioning/sessions/", _payload(setup_key=plaintext), format="json")
    assert ok.status_code == 201


@pytest.mark.django_db
def test_one_click_download_appends_trailer_with_fresh_key(main_admin, department, settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    settings.STORAGES = {
        **settings.STORAGES,
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    }
    rel = DsaInstallerRelease.objects.create(
        version="1.0.7",
        release_date=datetime.date(2026, 11, 13),
        is_latest=True,
        is_active=True,
    )
    rel.file.save("DepartmentSyncAgentSetup.exe", ContentFile(b"MZ-fake-installer-bytes"), save=True)

    admin_client = APIClient()
    admin_client.force_authenticate(main_admin)
    ticket = admin_client.post(
        "/api/v1/sync/installer/releases/latest/one-click-ticket/",
        {"department_id": department.pk, "ttl_hours": 2},
        format="json",
    )
    assert ticket.status_code == 200, ticket.content
    url = ticket.json()["url"]
    assert "dsk_" not in url  # the key is never in a URL
    assert InstallerSetupKey.objects.count() == 0  # minted at download time

    anon = APIClient()
    path = url.split("://", 1)[-1].split("/", 1)[-1]
    resp = anon.get("/" + path)
    assert resp.status_code == 200
    blob = b"".join(resp.streaming_content)
    assert blob.startswith(b"MZ-fake-installer-bytes")
    trailer = parse_trailer(blob)
    assert trailer["setup_key"].startswith("dsk_")
    assert trailer["department_id"] == department.pk
    assert int(resp["Content-Length"]) == len(blob)
    assert "no-store" in resp["Cache-Control"]
    assert InstallerSetupKey.objects.count() == 1

    replay = anon.get("/" + path)
    assert replay.status_code == 403  # single-use ticket


@pytest.mark.django_db
def test_one_click_ticket_denied_for_dept_admin(dept_admin, department):
    client = APIClient()
    client.force_authenticate(dept_admin)
    resp = client.post(
        "/api/v1/sync/installer/releases/latest/one-click-ticket/",
        {"department_id": department.pk},
        format="json",
    )
    assert resp.status_code == 403  # Main Admin only
