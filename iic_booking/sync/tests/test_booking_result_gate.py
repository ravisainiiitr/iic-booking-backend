"""DSA uploads/imports are refused for terminal and suspended bookings (server backs up DSA 1.0.7)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from iic_booking.equipment.models import BookingStatus
from iic_booking.sync.models import AgentLifecycleStatus, AgentUploadSession, DepartmentSyncAgent
from iic_booking.sync.services import booking_result_gate as gate
from iic_booking.sync.services.upload import UploadTransportService
from iic_booking.users.models import Department


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (BookingStatus.REFUNDED, "terminal"),
        (BookingStatus.CANCELLED, "terminal"),
        (BookingStatus.BOOKING_NOT_UTILIZED, "terminal"),
        (BookingStatus.OTHER_DISRUPTION, "suspended"),
        (BookingStatus.UNDER_MAINTENANCE, "suspended"),
        (BookingStatus.HOLD, "suspended"),
        (BookingStatus.DISRUPTION_PENDING, "suspended"),
        (BookingStatus.ABSENT, "suspended"),
        (BookingStatus.BOOKED, None),
        (BookingStatus.PROCESSING, None),
        (BookingStatus.COMPLETED, None),  # late files after completion keep working
    ],
)
def test_blocked_reason(status, reason):
    assert gate.blocked_reason(status) == reason


def test_ensure_raises_409_with_stable_code():
    with pytest.raises(gate.BookingNotAcceptingResultsError) as exc:
        gate.ensure_booking_accepts_results(SimpleNamespace(status=BookingStatus.REFUNDED))
    assert exc.value.status_code == 409
    assert exc.value.code == "BOOKING_NOT_ACCEPTING_RESULTS"
    gate.ensure_booking_accepts_results(SimpleNamespace(status=BookingStatus.COMPLETED))
    gate.ensure_booking_accepts_results(None)


def _agent():
    department = Department.objects.create(
        name=f"GateDept-{uuid.uuid4().hex[:8]}",
        code=f"G{uuid.uuid4().hex[:5].upper()}",
    )
    return DepartmentSyncAgent.objects.create(
        agent_name="Gate Agent",
        department=department,
        machine_guid=uuid.uuid4(),
        status=AgentLifecycleStatus.ENROLLED,
        is_active=True,
    )


class _FakeQs:
    def __init__(self, status):
        self._status = status

    def only(self, *_):
        return self

    def first(self):
        return SimpleNamespace(status=self._status)


@pytest.mark.django_db
@pytest.mark.parametrize("status", [BookingStatus.REFUNDED, BookingStatus.UNDER_MAINTENANCE])
def test_upload_start_refused_for_closed_or_suspended_booking(tmp_path, settings, monkeypatch, status):
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    settings.MEDIA_ROOT = str(tmp_path / "media")
    agent = _agent()
    monkeypatch.setattr(UploadTransportService, "_resolve_booking_fk", staticmethod(lambda _bid: 1719))
    from iic_booking.equipment import models as equipment_models

    monkeypatch.setattr(equipment_models.Booking.objects, "filter", lambda **_kw: _FakeQs(status))

    with pytest.raises(gate.BookingNotAcceptingResultsError):
        UploadTransportService().start(
            agent,
            agent_upload_id=uuid.uuid4(),
            file_name="late-result.txt",
            expected_size=12,
            booking_id=1719,
        )
    assert not AgentUploadSession.objects.filter(sync_agent=agent).exists()
