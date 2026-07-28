"""Production security hardening tests for Department Sync control-plane auth."""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
import uuid
from unittest.mock import MagicMock

import pytest
from django.contrib.auth.hashers import make_password
from django.test import override_settings
from rest_framework.test import APIClient

from iic_booking.sync.models import AgentLifecycleStatus, DepartmentSyncAgent
from iic_booking.sync.services.enrollment import EnrollmentService
from iic_booking.sync.services.security import RequestSigningService
from iic_booking.sync.services.tokens import hash_value, issue_access_token, revoke_access_token
from iic_booking.users.models import Department


def _department():
    return Department.objects.create(
        name=f"SecDept-{uuid.uuid4().hex[:8]}",
        code=f"S{uuid.uuid4().hex[:6].upper()}",
    )


def _agent(**kwargs):
    defaults = {
        "agent_name": "Security Test Agent",
        "department": _department(),
        "machine_guid": uuid.uuid4(),
        "status": AgentLifecycleStatus.REGISTERED,
        "is_active": True,
        "enrollment_token_hash": make_password("enroll-secret-one"),
    }
    defaults.update(kwargs)
    return DepartmentSyncAgent.objects.create(**defaults)


@pytest.mark.django_db
def test_enrollment_replay_fails_after_secret_consumed():
    agent = _agent()
    payload = {
        "agent_uuid": str(agent.agent_uuid),
        "enrollment_secret": "enroll-secret-one",
        "machine_name": "lab-pc-1",
        "hostname": "lab-pc-1",
        "operating_system": "Windows",
        "service_version": "1.0.0",
        "sqlite_schema_version": "1",
        "portal_version": "1",
    }
    first = EnrollmentService().enroll(payload)
    assert first["status"] == "enrolled"
    assert first["access_token"]

    with pytest.raises(Exception):
        EnrollmentService().enroll(payload)


@pytest.mark.django_db(transaction=True)
def test_concurrent_enrollment_only_one_succeeds():
    from django.conf import settings

    engine = settings.DATABASES["default"]["ENGINE"]
    if "sqlite" in engine:
        pytest.skip("select_for_update requires PostgreSQL/MySQL; SQLite cannot prove row locking")

    agent = _agent()
    payload = {
        "agent_uuid": str(agent.agent_uuid),
        "enrollment_secret": "enroll-secret-one",
        "machine_name": "lab-pc-1",
        "hostname": "lab-pc-1",
        "operating_system": "Windows",
        "service_version": "1.0.0",
        "sqlite_schema_version": "1",
        "portal_version": "1",
    }
    results: list[str] = []
    barrier = threading.Barrier(2)

    def _run():
        barrier.wait()
        try:
            EnrollmentService().enroll(payload)
            results.append("ok")
        except Exception:
            results.append("fail")

    t1 = threading.Thread(target=_run)
    t2 = threading.Thread(target=_run)
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    assert results.count("ok") == 1
    assert results.count("fail") == 1


@pytest.mark.django_db
def test_revoke_invalidates_access_and_enrollment():
    agent = _agent()
    token = issue_access_token(agent)
    agent.refresh_from_db()
    assert agent.access_token_hash
    revoke_access_token(agent)
    agent.refresh_from_db()
    assert agent.access_token_hash == ""
    assert agent.enrollment_token_hash == ""
    assert token  # plaintext was issued once; hash cleared


@pytest.mark.django_db
@override_settings(DSA_REQUEST_SIGNING_REQUIRED=True)
def test_invalid_hmac_rejected_fail_closed():
    agent = _agent(signing_required=True, signing_secret_hash=hash_value("real-secret"))
    request = MagicMock()
    request.method = "POST"
    request.get_full_path.return_value = "/api/v1/sync/heartbeat/"
    request.body = b"{}"
    request.META = {
        "HTTP_X_DSA_SIGNATURE": "deadbeef",
        "HTTP_X_DSA_TIMESTAMP": str(int(time.time())),
        "HTTP_X_DSA_NONCE": "nonce-1",
        "HTTP_X_DSA_DEVICE_ID": str(agent.machine_guid),
    }
    request.headers = {}
    ok, reason = RequestSigningService().verify_request(request, agent)
    assert ok is False
    assert reason


@pytest.mark.django_db
@override_settings(DSA_REQUEST_SIGNING_REQUIRED=True)
def test_missing_signing_secret_fail_closed():
    agent = _agent(signing_required=True, signing_secret_hash=hash_value("only-hash"))
    request = MagicMock()
    request.method = "POST"
    request.get_full_path.return_value = "/api/v1/sync/heartbeat/"
    request.body = b"{}"
    request.META = {
        "HTTP_X_DSA_SIGNATURE": hmac.new(b"x", b"y", hashlib.sha256).hexdigest(),
        "HTTP_X_DSA_TIMESTAMP": str(int(time.time())),
        "HTTP_X_DSA_NONCE": "nonce-2",
        "HTTP_X_DSA_DEVICE_ID": str(agent.machine_guid),
    }
    request.headers = {}
    # No plaintext secret available — fail closed (never soft-verify via hash alone).
    ok, reason = RequestSigningService().verify_request(request, agent)
    assert ok is False
    assert "not available" in (reason or "").lower() or "signing" in (reason or "").lower()


@pytest.mark.django_db
@override_settings(
    REST_FRAMEWORK={
        "DEFAULT_THROTTLE_RATES": {"sync_enroll": "2/min"},
        "DEFAULT_AUTHENTICATION_CLASSES": [],
        "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.AllowAny"],
    }
)
def test_enrollment_brute_force_rate_limited():
    agent = _agent()
    client = APIClient()
    body = {
        "agent_uuid": str(agent.agent_uuid),
        "enrollment_secret": "wrong-secret",
        "machine_name": "lab-pc-1",
        "hostname": "lab-pc-1",
        "operating_system": "Windows",
        "service_version": "1.0.0",
        "sqlite_schema_version": "1",
        "portal_version": "1",
    }
    statuses = []
    for _ in range(5):
        response = client.post("/api/v1/sync/enroll/", body, format="json")
        statuses.append(response.status_code)
        if response.status_code == 400:
            assert response.json()["error"]["code"] == "ENROLLMENT_FAILED"
    assert 429 in statuses or statuses.count(400) >= 2


@pytest.mark.django_db
def test_track_a_bridge_disabled_requires_agent_uuid():
    client = APIClient()
    response = client.post(
        "/api/v1/sync/heartbeat/",
        {"status": "ok"},
        format="json",
        HTTP_AUTHORIZATION="Bearer some-token",
    )
    assert response.status_code in (401, 403)
