"""A base instrument Under Maintenance takes its modes with it, with the same maintenance rules."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.equipment.models import (
    BookingStatus,
    DailySlot,
    DisruptionEvent,
    Equipment,
    EquipmentStatus,
    SlotMaster,
    SlotStatus,
)
from iic_booking.equipment.mode_family_service import link_mode_for_schedule
from iic_booking.equipment.tests.test_disruption_log import (
    _book,
    _client,
    _equipment,
    _future_weekday,
    _no_holidays,  # noqa: F401
    _oic_for,
    _slots,
    _user,
)
from iic_booking.users.models.user_type import UserType

pytestmark = pytest.mark.django_db


def _family():
    base = _equipment()
    mode = _equipment(internal_department=base.internal_department, parent_equipment=base)
    Equipment.objects.filter(pk=base.pk).update(enable_multi_mode=True)
    base.refresh_from_db()
    return base, mode


def _patch(user, eq, payload):
    return _client(user).patch(f"/api/equipments/{eq.pk}/", payload, format="json")


def _slot_later_today(eq) -> DailySlot:
    start = timezone.now() + timedelta(hours=1)
    return DailySlot.objects.create(
        slot_master=SlotMaster.objects.filter(equipment=eq).first(),
        date=timezone.localdate(),
        start_datetime=start,
        end_datetime=start + timedelta(hours=1),
        status=SlotStatus.AVAILABLE,
    )


def test_base_under_maintenance_puts_mode_under_maintenance_with_all_rules():
    base, mode = _family()
    oic = _oic_for(base)
    day = _future_weekday()
    _slots(mode, day)
    booking = _book(mode, _slot_later_today(mode), _user(UserType.STUDENT))

    res = _patch(oic, base, {"status": "REPAIR", "disruption_reason": "Vacuum leak"})

    assert res.status_code == 200, res.data
    assert [m["equipment_id"] for m in res.data["modes_status_changed"]] == [mode.pk]
    mode.refresh_from_db()
    assert mode.status == EquipmentStatus.REPAIR and mode.status_follows_parent
    event = DisruptionEvent.objects.get(equipment=mode)
    assert event.started_by_id == oic.pk
    assert base.code in event.reason and "Vacuum leak" in event.reason
    free = DailySlot.objects.filter(slot_master__equipment=mode, date=day, booking__isnull=True)
    assert set(free.values_list("status", flat=True)) == {SlotStatus.UNDER_MAINTENANCE}
    booking.refresh_from_db()
    assert booking.status == BookingStatus.DISRUPTION_PENDING and booking.maintenance_disruption_flag


def test_base_back_operational_restores_modes_it_took_down_only():
    base, mode = _family()
    own = _equipment(internal_department=base.internal_department, parent_equipment=base,
                     status=EquipmentStatus.INACTIVE)
    oic = _oic_for(base)
    _patch(oic, base, {"status": "REPAIR"})
    own.refresh_from_db()
    assert own.status == EquipmentStatus.INACTIVE and not own.status_follows_parent

    res = _patch(oic, base, {"status": "ACTIVE"})

    assert res.status_code == 200, res.data
    mode.refresh_from_db()
    own.refresh_from_db()
    assert mode.status == EquipmentStatus.ACTIVE and not mode.status_follows_parent
    assert DisruptionEvent.objects.get(equipment=mode).ended_at is not None
    assert own.status == EquipmentStatus.INACTIVE


def test_mode_cannot_be_made_operational_while_base_under_maintenance():
    base, mode = _family()
    oic = _oic_for(base)
    _patch(oic, base, {"status": "REPAIR"})
    admin = _user(UserType.ADMIN)

    res = _patch(admin, mode, {"status": "ACTIVE"})

    assert res.status_code == 400
    assert res.data["code"] == "BASE_UNDER_MAINTENANCE"
    mode.refresh_from_db()
    assert mode.status == EquipmentStatus.REPAIR


def test_mode_status_changed_directly_no_longer_follows_base():
    base, mode = _family()
    admin = _user(UserType.ADMIN)
    _patch(admin, base, {"status": "REPAIR"})
    _patch(admin, mode, {"status": "INACTIVE"})
    mode.refresh_from_db()
    assert mode.status == EquipmentStatus.INACTIVE and not mode.status_follows_parent

    _patch(admin, base, {"status": "ACTIVE"})

    mode.refresh_from_db()
    assert mode.status == EquipmentStatus.INACTIVE


def test_mode_linked_to_base_under_maintenance_follows_it(django_capture_on_commit_callbacks):
    base = _equipment(status=EquipmentStatus.REPAIR)
    mode = _equipment(internal_department=base.internal_department)
    admin = _user(UserType.ADMIN)

    with django_capture_on_commit_callbacks(execute=True):
        link_mode_for_schedule(base, mode, admin)

    mode.refresh_from_db()
    assert mode.status == EquipmentStatus.REPAIR and mode.status_follows_parent
    assert DisruptionEvent.objects.filter(equipment=mode).exists()
