"""Upload start must not 500 when booking/equipment FKs are missing."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from django.test import override_settings

from iic_booking.sync.models import AgentLifecycleStatus, AgentUploadSession, DepartmentSyncAgent
from iic_booking.sync.services.upload import UploadTransportService
from iic_booking.users.models import Department


@pytest.mark.django_db
def test_upload_start_omits_invalid_booking_and_equipment_fks(tmp_path, settings):
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    settings.MEDIA_ROOT = str(tmp_path / "media")

    department = Department.objects.create(
        name=f"UpDept-{uuid.uuid4().hex[:8]}",
        code=f"U{uuid.uuid4().hex[:5].upper()}",
    )
    agent = DepartmentSyncAgent.objects.create(
        agent_name="Upload FK Agent",
        department=department,
        machine_guid=uuid.uuid4(),
        status=AgentLifecycleStatus.ENROLLED,
        is_active=True,
    )

    payload = UploadTransportService().start(
        agent,
        agent_upload_id=uuid.uuid4(),
        file_name="result.txt",
        expected_size=12,
        equipment_id=9_999_999,
        booking_id=9_999_999,
    )
    session = AgentUploadSession.objects.get(id=payload["upload_id"])
    assert session.booking_id is None
    assert session.equipment_id is None
    assert session.status == "PENDING"
    assert (tmp_path / Path(session.server_path)).exists()
