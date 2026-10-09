"""Full 24-hour slots: a Slot Master with Close = Open runs from its start to the same time next day."""

from datetime import date, time, timedelta
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from iic_booking.equipment.admin import SlotMasterInlineFormSet
from iic_booking.equipment.calculators import ChargeCalculationEngine
from iic_booking.equipment.models import (
    ChargeProfile,
    DailySlot,
    Holiday,
    SlotMaster,
    slot_masters_conflict,
    slot_master_duration_minutes,
)
from iic_booking.equipment.serializers import EquipmentAdminWriteSerializer
from iic_booking.equipment.slot_block_rules import equipment_slot_times
from iic_booking.equipment.slot_utils import SlotGenerator
from iic_booking.equipment.template_health import weekly_slot_rows

MONDAY = date(2026, 10, 12)


def _master(equipment, number=1, open_t=time(0), close_t=time(0), active=True):
    return SlotMaster.objects.create(
        equipment=equipment, slot_number=number, open_time=open_t, close_time=close_t, is_active=active
    )


def _minutes(slot):
    return int((slot.end_datetime - slot.start_datetime).total_seconds() // 60)


def test_aware_datetimes_full_day_and_overnight():
    start, end = SlotGenerator._aware_slot_datetimes(MONDAY, time(0), time(0))
    assert end - start == timedelta(hours=24)
    assert timezone.localtime(end).date() == MONDAY + timedelta(days=1)
    assert timezone.localtime(end).time() == time(0)

    start, end = SlotGenerator._aware_slot_datetimes(MONDAY, time(9), time(9))
    assert end - start == timedelta(hours=24)

    start, end = SlotGenerator._aware_slot_datetimes(MONDAY, time(18), time(0))
    assert end - start == timedelta(hours=6)

    start, end = SlotGenerator._aware_slot_datetimes(MONDAY, time(9), time(10))
    assert end - start == timedelta(hours=1)


def test_slot_master_duration_minutes():
    assert slot_master_duration_minutes(time(0), time(0)) == 1440
    assert slot_master_duration_minutes(time(9, 30), time(9, 30)) == 1440
    assert slot_master_duration_minutes(time(0), time(23, 59)) == 1439
    assert slot_master_duration_minutes(time(18), time(0)) == 360
    assert slot_master_duration_minutes(time(9), time(10, 30)) == 90


@pytest.mark.django_db
def test_generated_full_day_slots_are_contiguous_and_owned_by_start_date(egs_factory):
    eq = egs_factory.equipment(with_profile=False, slot_duration_minutes=1440)
    _master(eq)
    Holiday.objects.create(date=MONDAY + timedelta(days=2), reason="Test holiday", is_active=True)

    SlotGenerator.generate_slots_for_week(eq, MONDAY, MONDAY + timedelta(days=6))
    slots = list(DailySlot.objects.filter(slot_master__equipment=eq).order_by("start_datetime"))

    # Holiday (Wed) and weekend dates are skipped by their start date.
    assert [s.date for s in slots] == [MONDAY, MONDAY + timedelta(days=1), MONDAY + timedelta(days=3), MONDAY + timedelta(days=4)]
    for s in slots:
        assert _minutes(s) == 1440
        assert timezone.localtime(s.start_datetime).date() == s.date
        assert timezone.localtime(s.start_datetime).time() == time(0)
    assert slots[0].end_datetime == slots[1].start_datetime
    assert slots[2].end_datetime == slots[3].start_datetime


@pytest.mark.django_db
def test_admin_grid_generates_full_day_slots_on_holidays_as_not_available(egs_factory):
    eq = egs_factory.equipment(with_profile=False)
    _master(eq)
    saturday = MONDAY + timedelta(days=5)
    created = SlotGenerator.generate_daily_slots(eq, saturday, allow_holiday=True)
    assert len(created) == 1
    slot = DailySlot.objects.get(slot_master__equipment=eq, date=saturday)
    assert slot.status == "NOT_AVAILABLE"
    assert _minutes(slot) == 1440


@pytest.mark.django_db
def test_hour_charge_for_one_full_day_slot_is_24_hours(egs_factory):
    eq = egs_factory.equipment(unit_charge="10.00", slot_duration_minutes=1440)
    _master(eq)
    SlotGenerator.generate_daily_slots(eq, MONDAY)
    slot = DailySlot.objects.get(slot_master__equipment=eq, date=MONDAY)
    total_minutes = _minutes(slot)
    assert total_minutes == 1440

    profile = ChargeProfile.objects.get(equipment=eq)
    charge, _breakdown = ChargeCalculationEngine.calculate_charge(profile, {}, total_minutes)
    assert Decimal(charge) == Decimal("240.00")


@pytest.mark.django_db
def test_clean_allows_full_day_only_as_sole_active_slot(egs_factory):
    eq = egs_factory.equipment(with_profile=False)
    full = SlotMaster(equipment=eq, slot_number=1, open_time=time(0), close_time=time(0), is_active=True)
    full.clean()
    full.save()

    other = SlotMaster(equipment=eq, slot_number=2, open_time=time(9), close_time=time(10), is_active=True)
    with pytest.raises(ValidationError) as exc:
        other.clean()
    assert "only active slot" in str(exc.value)

    other.is_active = False
    other.clean()
    other.save()

    full.refresh_from_db()
    full.clean()

    second_full = SlotMaster(equipment=eq, slot_number=3, open_time=time(6), close_time=time(6), is_active=True)
    with pytest.raises(ValidationError):
        second_full.clean()


@pytest.mark.django_db
def test_clean_rejects_full_day_when_other_slots_are_active(egs_factory):
    eq = egs_factory.equipment(with_profile=False)
    _master(eq, 1, time(9), time(10))
    full = SlotMaster(equipment=eq, slot_number=2, open_time=time(0), close_time=time(0), is_active=True)
    with pytest.raises(ValidationError) as exc:
        full.clean()
    assert "close_time" in exc.value.message_dict


def test_slot_masters_conflict_rules():
    assert slot_masters_conflict([(time(0), time(0))]) is None
    assert slot_masters_conflict([(time(9), time(10)), (time(10), time(11))]) is None
    assert slot_masters_conflict([(time(0), time(0)), (time(9), time(10))])
    assert slot_masters_conflict([(time(0), time(0)), (None, None)]) is None


def _serializer_slots(*rows):
    return [
        {"slot_number": i + 1, "open_time": o, "close_time": c, "is_active": a}
        for i, (o, c, a) in enumerate(rows)
    ]


def test_write_serializer_validates_full_day_slots():
    ser = EquipmentAdminWriteSerializer()
    rows = _serializer_slots((time(0), time(0), True))
    assert ser.validate_slot_masters(rows) == rows
    rows = _serializer_slots((time(0), time(0), True), (time(9), time(10), False))
    assert ser.validate_slot_masters(rows) == rows
    from rest_framework.exceptions import ValidationError as DRFValidationError

    with pytest.raises(DRFValidationError):
        ser.validate_slot_masters(_serializer_slots((time(0), time(0), True), (time(9), time(10), True)))


@pytest.mark.django_db
def test_admin_inline_formset_checks_rows_together(egs_factory):
    from django.forms import inlineformset_factory

    from iic_booking.equipment.admin import SlotMasterInlineForm
    from iic_booking.equipment.models import Equipment

    eq = egs_factory.equipment(with_profile=False)
    first = _master(eq, 1, time(0), time(23, 59))
    second = _master(eq, 2, time(9), time(10))
    FormSet = inlineformset_factory(
        Equipment, SlotMaster, form=SlotMasterInlineForm, formset=SlotMasterInlineFormSet, fk_name="equipment", extra=0
    )

    def data(second_active):
        d = {
            "slot_masters-TOTAL_FORMS": "2",
            "slot_masters-INITIAL_FORMS": "2",
            "slot_masters-0-id": str(first.pk),
            "slot_masters-0-equipment": str(eq.pk),
            "slot_masters-0-slot_number": "1",
            "slot_masters-0-open_time": "00:00",
            "slot_masters-0-close_time": "00:00",
            "slot_masters-0-is_active": "on",
            "slot_masters-1-id": str(second.pk),
            "slot_masters-1-equipment": str(eq.pk),
            "slot_masters-1-slot_number": "2",
            "slot_masters-1-open_time": "09:00",
            "slot_masters-1-close_time": "10:00",
        }
        if second_active:
            d["slot_masters-1-is_active"] = "on"
        return d

    blocked = FormSet(data(True), instance=eq, prefix="slot_masters")
    assert not blocked.is_valid()
    assert "only active slot" in " ".join(blocked.non_form_errors())

    # Deactivating the other slot in the same submit is accepted although it is still active in the database.
    ok = FormSet(data(False), instance=eq, prefix="slot_masters")
    assert ok.is_valid(), (ok.errors, ok.non_form_errors())
    ok.save()
    first.refresh_from_db()
    assert first.is_full_day


@pytest.mark.django_db
def test_default_slot_master_is_full_day(egs_factory):
    eq = egs_factory.equipment(with_profile=False)
    [master] = SlotGenerator.ensure_slot_masters_exist(eq)
    assert master.open_time == time(0) and master.close_time == time(0)
    assert master.is_full_day


@pytest.mark.django_db
def test_slot_time_listings_report_24_hours(egs_factory):
    eq = egs_factory.equipment(with_profile=False)
    _master(eq)
    assert equipment_slot_times(eq) == [
        {"time": "00:00", "end_time": "24:00", "duration_minutes": 1440, "name": "Slot 1"}
    ]
    assert weekly_slot_rows(eq, None) == [(0, 1440)]
