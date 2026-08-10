from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
import uuid

import pytest
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import (
    Booking,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    Equipment,
    SlotMaster,
)
from iic_booking.equipment.tasks import auto_complete_bookings_with_data_after_end
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
        name=f"R7Dept-{uuid.uuid4().hex[:8]}",
        code=f"R7{uuid.uuid4().hex[:4].upper()}",
    )


def _equipment(**kwargs):
    defaults = {
        "name": "R7 PXRD",
        "code": f"R7{uuid.uuid4().hex[:4].upper()}",
        "slot_duration_minutes": 60,
        "user_rating_enabled": False,
        "enable_remote_analysis": True,
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
        virtual_booking_id=f"IIC{equipment.code}2026{uuid.uuid4().hex[:4]}",
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
        agent_name="R7 Agent",
        department=_department(),
        machine_guid=uuid.uuid4(),
        status=AgentLifecycleStatus.ENROLLED,
        is_active=True,
    )


def _seed_dsa_attachment(*, booking, agent, storage_root: Path, filename: str, content: bytes):
    relative = Path(str(agent.agent_uuid)) / str(uuid.uuid4()) / filename
    absolute = storage_root / relative
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


def _seed_workspace(booking: Booking, agent: DepartmentSyncAgent):
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
def test_auto_completion_skips_workspace_ready_only(tmp_path, settings):
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    owner = UserFactory()
    equipment = _equipment()
    booking = _booking(owner, equipment, status=BookingStatus.BOOKED)
    _attach_slot(booking, ended_hours_ago=1)
    agent = _agent()
    _seed_workspace(booking, agent)
    _seed_dsa_attachment(
        booking=booking,
        agent=agent,
        storage_root=tmp_path,
        filename="workspace-ready",
        content=b"ready",
    )

    changed = auto_complete_bookings_with_data_after_end()
    booking.refresh_from_db()

    assert changed == 0
    assert booking.status == BookingStatus.BOOKED


@pytest.mark.django_db
@override_settings(AWS_STORAGE_BUCKET_NAME="")
def test_auto_completion_completes_when_data_exists(tmp_path, settings, monkeypatch):
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    owner = UserFactory()
    equipment = _equipment()
    booking = _booking(owner, equipment, status=BookingStatus.BOOKED)
    _attach_slot(booking, ended_hours_ago=1)
    agent = _agent()
    _seed_workspace(booking, agent)
    _seed_dsa_attachment(
        booking=booking,
        agent=agent,
        storage_root=tmp_path,
        filename="sample001.dm4",
        content=b"dm4-data",
    )

    calls = {"count": 0}

    def _fake_email(_booking, _files):
        calls["count"] += 1

    import iic_booking.equipment.api_views as api_views

    monkeypatch.setattr(api_views, "_send_completion_email_with_attachments", _fake_email)
    changed = auto_complete_bookings_with_data_after_end()
    booking.refresh_from_db()

    assert changed == 1
    assert booking.status == BookingStatus.COMPLETED
    assert booking.completed_at is not None
    assert calls["count"] == 1


@pytest.mark.django_db
@override_settings(AWS_STORAGE_BUCKET_NAME="")
def test_auto_completion_skips_when_workspace_missing(tmp_path, settings):
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    owner = UserFactory()
    equipment = _equipment()
    booking = _booking(owner, equipment, status=BookingStatus.BOOKED)
    _attach_slot(booking, ended_hours_ago=1)
    agent = _agent()
    _seed_dsa_attachment(
        booking=booking,
        agent=agent,
        storage_root=tmp_path,
        filename="result.csv",
        content=b"a,b\n1,2\n",
    )

    changed = auto_complete_bookings_with_data_after_end()
    booking.refresh_from_db()
    assert changed == 0
    assert booking.status == BookingStatus.BOOKED


@pytest.mark.django_db
@override_settings(AWS_STORAGE_BUCKET_NAME="")
def test_auto_completion_is_idempotent(tmp_path, settings, monkeypatch):
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    owner = UserFactory()
    equipment = _equipment()
    booking = _booking(owner, equipment, status=BookingStatus.BOOKED)
    _attach_slot(booking, ended_hours_ago=1)
    agent = _agent()
    _seed_workspace(booking, agent)
    _seed_dsa_attachment(
        booking=booking,
        agent=agent,
        storage_root=tmp_path,
        filename="result.csv",
        content=b"ok",
    )

    calls = {"count": 0}

    def _fake_email(_booking, _files):
        calls["count"] += 1

    import iic_booking.equipment.api_views as api_views

    monkeypatch.setattr(api_views, "_send_completion_email_with_attachments", _fake_email)

    first = auto_complete_bookings_with_data_after_end()
    second = auto_complete_bookings_with_data_after_end()
    booking.refresh_from_db()

    assert first == 1
    assert second == 0
    assert booking.status == BookingStatus.COMPLETED
    assert calls["count"] == 1


@pytest.mark.django_db
@override_settings(AWS_STORAGE_BUCKET_NAME="")
def test_manual_completion_before_slot_end_exposes_results_immediately(tmp_path, settings):
    settings.DSA_UPLOAD_STORAGE_ROOT = str(tmp_path)
    settings.MEDIA_ROOT = str(tmp_path / "media")
    owner = UserFactory()
    admin = UserFactory(user_type=UserType.ADMIN)
    equipment = _equipment()
    booking = _booking(owner, equipment, status=BookingStatus.BOOKED)

    now = timezone.now()
    slot_master = SlotMaster.objects.create(
        equipment=equipment,
        slot_number=1,
        open_time=(now - timedelta(minutes=30)).time().replace(microsecond=0),
        close_time=(now + timedelta(hours=1)).time().replace(microsecond=0),
        is_active=True,
    )
    DailySlot.objects.create(
        slot_master=slot_master,
        date=now.date(),
        start_datetime=now - timedelta(minutes=30),
        end_datetime=now + timedelta(hours=1),
        status="BOOKED",
        booking=booking,
    )

    agent = _agent()
    _seed_dsa_attachment(
        booking=booking,
        agent=agent,
        storage_root=tmp_path,
        filename="early-finish.txt",
        content=b"done",
    )

    operator_client = APIClient()
    operator_client.force_authenticate(user=admin)
    complete = operator_client.post(f"/api/bookings/{booking.pk}/complete/", {})
    assert complete.status_code == 200

    booking.refresh_from_db()
    assert booking.status == BookingStatus.COMPLETED

    user_client = APIClient()
    user_client.force_authenticate(user=owner)
    listed = user_client.get(f"/api/bookings/{booking.pk}/results/")
    assert listed.status_code == 200
    assert listed.json()["exists"] is True


@pytest.mark.django_db
def test_results_available_event_does_not_force_complete():
    owner = UserFactory()
    equipment = _equipment()
    booking = _booking(owner, equipment, status=BookingStatus.BOOKED)

    from iic_booking.equipment.api_views import _apply_results_available_event_and_completed_status

    _apply_results_available_event_and_completed_status(booking)
    booking.refresh_from_db()
    assert booking.status == BookingStatus.BOOKED

