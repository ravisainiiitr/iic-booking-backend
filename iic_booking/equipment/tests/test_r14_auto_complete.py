"""R14 equipment auto-complete: per-equipment flag, RAA skip, race safety."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import uuid

import pytest
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import (
    Booking,
    BookingEvent,
    BookingEventType,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    Equipment,
    SlotMaster,
)
from iic_booking.equipment.tasks import auto_complete_bookings_with_data_after_end
from iic_booking.remote_analysis.constants import ReservationStatus, SessionStatus, WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
from iic_booking.remote_analysis.session_models import RemoteDesktopSession
from iic_booking.sync.models import (
    AgentLifecycleStatus,
    BookingWorkspace,
    DepartmentSyncAgent,
    EquipmentResult,
    ResultAttachment,
)
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _department():
    return Department.objects.create(
        name=f"R14Dept-{uuid.uuid4().hex[:8]}",
        code=f"R14{uuid.uuid4().hex[:4].upper()}",
    )


def _equipment(**kwargs):
    defaults = {
        "name": "R14 FE-SEM",
        "code": f"R14{uuid.uuid4().hex[:4].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
        "enable_remote_analysis": True,
        "auto_complete_booking": False,
    }
    defaults.update(kwargs)
    return Equipment.objects.create(**defaults)


def _booking(owner, equipment, *, status=BookingStatus.BOOKED):
    profile = ChargeProfile.objects.create(
        equipment=equipment,
        user_type=UserType.STUDENT,
        primary_unit_charge=Decimal("10.00"),
    )
    return Booking.objects.create(
        user=owner,
        equipment=equipment,
        charge_profile=profile,
        status=status,
        total_charge=Decimal("10.00"),
        total_time_minutes=60,
        virtual_booking_id=f"IIC{equipment.code}2026{uuid.uuid4().hex[:5].upper()}",
    )


def _attach_slot(booking: Booking, *, ended_hours_ago: int) -> None:
    now = timezone.now()
    start = now - timedelta(hours=ended_hours_ago + 1)
    end = now - timedelta(hours=ended_hours_ago)
    slot_master = SlotMaster.objects.create(
        equipment=booking.equipment,
        slot_number=1,
        open_time=start.time().replace(microsecond=0),
        close_time=end.time().replace(microsecond=0),
        is_active=True,
    )
    DailySlot.objects.create(
        slot_master=slot_master,
        date=start.date(),
        start_datetime=start,
        end_datetime=end,
        status="BOOKED",
        booking=booking,
    )


def _agent():
    return DepartmentSyncAgent.objects.create(
        agent_name="R14 Agent",
        department=_department(),
        machine_guid=uuid.uuid4(),
        status=AgentLifecycleStatus.ENROLLED,
        is_active=True,
    )


def _seed_dsa_attachment(*, booking, agent, storage_root, filename: str, content: bytes):
    from pathlib import Path

    relative = Path(str(agent.agent_uuid)) / str(uuid.uuid4()) / filename
    absolute = Path(storage_root) / relative
    absolute.parent.mkdir(parents=True, exist_ok=True)
    absolute.write_bytes(content)
    result = EquipmentResult.objects.create(
        sync_agent=agent,
        booking=booking,
        equipment=booking.equipment,
        agent_upload_id=uuid.uuid4(),
        source_file_name=filename,
        processed_by="Department Sync Agent",
    )
    ResultAttachment.objects.create(
        result=result,
        file_name=filename,
        size_bytes=len(content),
        storage_path=str(relative).replace("\\", "/"),
        attachment_kind="primary",
    )


def _seed_workspace(booking, agent):
    BookingWorkspace.objects.create(
        sync_agent=agent,
        booking=booking,
        equipment=booking.equipment,
        workspace_name=booking.virtual_booking_id,
        relative_folder=f"{booking.equipment.code}/Active/{booking.virtual_booking_id}",
        expected_result_folder=f"{booking.equipment.code}/Active/{booking.virtual_booking_id}/Results",
        sample_folder=f"{booking.equipment.code}/Active/{booking.virtual_booking_id}/Samples",
    )


@pytest.mark.django_db
@override_settings(AWS_STORAGE_BUCKET_NAME="")
def test_auto_complete_disabled_equipment_never_completes(tmp_path, settings):
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    owner = UserFactory()
    equipment = _equipment(auto_complete_booking=False)
    booking = _booking(owner, equipment)
    _attach_slot(booking, ended_hours_ago=1)
    agent = _agent()
    _seed_workspace(booking, agent)
    _seed_dsa_attachment(
        booking=booking, agent=agent, storage_root=tmp_path, filename="result.dm4", content=b"data"
    )

    changed = auto_complete_bookings_with_data_after_end()
    booking.refresh_from_db()
    assert changed == 0
    assert booking.status == BookingStatus.BOOKED


@pytest.mark.django_db
@override_settings(AWS_STORAGE_BUCKET_NAME="")
def test_auto_complete_skips_when_no_result_data(tmp_path, settings):
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    owner = UserFactory()
    equipment = _equipment(auto_complete_booking=True)
    booking = _booking(owner, equipment)
    _attach_slot(booking, ended_hours_ago=1)
    agent = _agent()
    _seed_workspace(booking, agent)
    _seed_dsa_attachment(
        booking=booking,
        agent=agent,
        storage_root=tmp_path,
        filename="scratch.tmp",
        content=b"temp",
    )

    changed = auto_complete_bookings_with_data_after_end()
    booking.refresh_from_db()
    assert changed == 0
    assert booking.status == BookingStatus.BOOKED


@pytest.mark.django_db
@override_settings(AWS_STORAGE_BUCKET_NAME="")
def test_auto_complete_completes_when_result_data_exists(tmp_path, settings, monkeypatch):
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    owner = UserFactory()
    equipment = _equipment(auto_complete_booking=True)
    booking = _booking(owner, equipment)
    _attach_slot(booking, ended_hours_ago=1)
    agent = _agent()
    _seed_workspace(booking, agent)
    _seed_dsa_attachment(
        booking=booking, agent=agent, storage_root=tmp_path, filename="spectrum.csv", content=b"ok"
    )

    import iic_booking.equipment.api_views as api_views

    monkeypatch.setattr(api_views, "_send_completion_email_with_attachments", lambda *_a, **_k: None)
    changed = auto_complete_bookings_with_data_after_end()
    booking.refresh_from_db()
    assert changed == 1
    assert booking.status == BookingStatus.COMPLETED
    event = BookingEvent.objects.filter(booking=booking, event_type=BookingEventType.COMPLETED).first()
    assert event is not None
    assert event.metadata.get("completion_source") == "AUTO_COMPLETE"


@pytest.mark.django_db
@override_settings(AWS_STORAGE_BUCKET_NAME="")
def test_manual_then_auto_complete_is_safe(tmp_path, settings, monkeypatch):
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    owner = UserFactory()
    admin = UserFactory(user_type=UserType.ADMIN)
    equipment = _equipment(auto_complete_booking=True)
    booking = _booking(owner, equipment)
    _attach_slot(booking, ended_hours_ago=1)
    agent = _agent()
    _seed_workspace(booking, agent)
    _seed_dsa_attachment(
        booking=booking, agent=agent, storage_root=tmp_path, filename="result.csv", content=b"ok"
    )

    client = APIClient()
    client.force_authenticate(user=admin)
    assert client.post(f"/api/bookings/{booking.pk}/complete/", {}).status_code == 200

    import iic_booking.equipment.api_views as api_views

    emails = {"n": 0}

    def _fake(*_a, **_k):
        emails["n"] += 1

    monkeypatch.setattr(api_views, "_send_completion_email_with_attachments", _fake)
    changed = auto_complete_bookings_with_data_after_end()
    booking.refresh_from_db()
    assert changed == 0
    assert booking.status == BookingStatus.COMPLETED
    assert BookingEvent.objects.filter(booking=booking, event_type=BookingEventType.COMPLETED).count() == 1
    assert emails["n"] == 0


@pytest.mark.django_db
@override_settings(AWS_STORAGE_BUCKET_NAME="")
def test_auto_complete_skips_active_raa_session(tmp_path, settings):
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    owner = UserFactory()
    equipment = _equipment(auto_complete_booking=True)
    booking = _booking(owner, equipment)
    _attach_slot(booking, ended_hours_ago=1)
    agent = _agent()
    _seed_workspace(booking, agent)
    _seed_dsa_attachment(
        booking=booking, agent=agent, storage_root=tmp_path, filename="result.csv", content=b"ok"
    )

    ws = AnalysisWorkstation.objects.create(
        agent_id=f"r14-{uuid.uuid4().hex[:8]}",
        hostname="R14-PC",
        display_name="R14 PC",
        status=WorkstationStatus.AVAILABLE,
        enabled=True,
        health_score=90,
        last_heartbeat=timezone.now(),
        supports_rdp=True,
    )
    reservation = AnalysisReservation.objects.create(
        user=owner,
        booking=booking,
        workstation=ws,
        status=ReservationStatus.ACTIVE,
        requested_start=timezone.now() - timedelta(hours=1),
        requested_end=timezone.now() + timedelta(hours=1),
    )
    RemoteDesktopSession.objects.create(
        reservation=reservation,
        booking=booking,
        user=owner,
        workstation=ws,
        status=SessionStatus.ACTIVE,
    )

    changed = auto_complete_bookings_with_data_after_end()
    booking.refresh_from_db()
    assert changed == 0
    assert booking.status == BookingStatus.BOOKED
    session = RemoteDesktopSession.objects.get(booking=booking)
    assert session.status == SessionStatus.ACTIVE
