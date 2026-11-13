"""Regression: finalize with a processing time crashed (``agent.uuid`` does not exist; the field is ``agent_uuid``), so
the DSA retried the same finalize forever (RESULT-2007, HTTP 500)."""

from __future__ import annotations

import uuid

import pytest

from iic_booking.equipment.tests.test_booking_results_dsa import _agent, _booking, _equipment
from iic_booking.sync.models import EquipmentResult
from iic_booking.sync.services.result_completion import ResultCompletionService
from iic_booking.users.tests.factories import UserFactory


@pytest.mark.django_db
def test_finalize_with_a_processing_time_records_the_agent_and_succeeds():
    agent = _agent()
    equipment = _equipment()
    booking = _booking(UserFactory(), equipment)
    upload_id = uuid.uuid4()
    EquipmentResult.objects.create(sync_agent=agent, booking=booking, equipment=equipment, agent_upload_id=upload_id,
                                   source_file_name="spectrum.csv", processed_by="Department Sync Agent")

    out = ResultCompletionService().finalize(agent, agent_upload_id=upload_id, booking_id=booking.pk,
                                             processing_duration_ms=1234)

    assert out["decision"] == "finalized"
    assert out["processing_duration_ms"] == 1234
    assert out["processed_by"] == f"DepartmentSyncAgent:{agent.agent_uuid}"
