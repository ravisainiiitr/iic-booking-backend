"""Wednesday 20:30 pre-generation of next week's slots, ahead of the 21:00 booking opening."""

from datetime import date, time, timedelta
from unittest.mock import patch

import pytest

from iic_booking.equipment.models import DailySlot, EquipmentStatus, SlotMaster, SlotStatus
from iic_booking.equipment.tasks import ensure_upcoming_slots, prepare_next_week_slots

MONDAY = date(2026, 10, 12)


def _with_master(equipment):
    SlotMaster.objects.create(
        equipment=equipment, slot_number=1, open_time=time(10, 0), close_time=time(11, 0), is_active=True
    )
    return equipment


def _dates(equipment):
    return {
        d: s
        for d, s in DailySlot.objects.filter(slot_master__equipment=equipment).values_list("date", "status")
    }


@pytest.mark.django_db
def test_prepares_whole_next_week_for_operational_equipment_only(egs_factory):
    active = _with_master(egs_factory.equipment(with_profile=False))
    repair = _with_master(egs_factory.equipment(with_profile=False, status=EquipmentStatus.REPAIR))
    no_masters = egs_factory.equipment(with_profile=False)

    created = prepare_next_week_slots(MONDAY.isoformat())

    rows = _dates(active)
    assert created == 7
    assert set(rows) == {MONDAY + timedelta(days=i) for i in range(7)}
    assert rows[MONDAY] == SlotStatus.AVAILABLE
    assert rows[MONDAY + timedelta(days=5)] == SlotStatus.NOT_AVAILABLE
    assert rows[MONDAY + timedelta(days=6)] == SlotStatus.NOT_AVAILABLE
    assert _dates(repair) == {}
    assert _dates(no_masters) == {}
    assert not SlotMaster.objects.filter(equipment=no_masters).exists()

    assert prepare_next_week_slots(MONDAY.isoformat()) == 0


@pytest.mark.django_db
def test_defaults_to_next_week_and_normalises_to_monday(egs_factory):
    active = _with_master(egs_factory.equipment(with_profile=False))

    with patch("iic_booking.equipment.tasks.timezone.localdate", return_value=date(2026, 10, 7)):
        prepare_next_week_slots()
    assert min(_dates(active)) == MONDAY

    prepare_next_week_slots((MONDAY + timedelta(days=9)).isoformat())
    assert min(d for d in _dates(active) if d > MONDAY + timedelta(days=6)) == MONDAY + timedelta(days=7)


@pytest.mark.django_db
def test_nightly_job_still_skips_closed_days(egs_factory):
    active = _with_master(egs_factory.equipment(with_profile=False))

    with patch("iic_booking.equipment.tasks.timezone.localdate", return_value=date(2026, 10, 7)):
        ensure_upcoming_slots()

    assert all(d.weekday() < 5 for d in _dates(active))
