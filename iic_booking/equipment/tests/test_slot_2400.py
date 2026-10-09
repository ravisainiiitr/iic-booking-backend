"""Slot Master close time 24:00 (stored as 00:00 = midnight at the end of the slot's day) and overlap rules."""

from datetime import date, datetime, time, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from iic_booking.communication.email_branding import (
    format_email_end_datetime,
    format_local_end_dt,
    slot_end_on_start_day,
    strftime_slot_end,
)
from iic_booking.equipment.admin import SlotCloseTimeFormField
from iic_booking.equipment.equipment_addition_requests import EquipmentAdditionRequestCreateSerializer
from iic_booking.equipment.models import (
    DailySlot,
    SlotMaster,
    format_slot_close_time,
    normalize_slot_close_input,
    slot_master_range_label,
    slot_masters_conflict,
)
from iic_booking.equipment.serializers import SlotMasterWriteSerializer
from iic_booking.equipment.slot_block_rules import equipment_slot_times
from iic_booking.equipment.slot_utils import SlotGenerator

MONDAY = date(2026, 10, 12)


def test_normalize_and_format_close_time():
    assert normalize_slot_close_input("24:00") == "00:00"
    assert normalize_slot_close_input(" 24:00:00 ") == "00:00"
    assert normalize_slot_close_input("12:00") == "12:00"
    assert normalize_slot_close_input(time(12)) == time(12)
    assert format_slot_close_time(time(0)) == "24:00"
    assert format_slot_close_time(time(23, 59)) == "23:59"
    assert format_slot_close_time(None) == ""


def test_range_labels():
    assert slot_master_range_label(time(12), time(0)) == "12:00–24:00"
    assert slot_master_range_label(time(0), time(0)) == "00:00–24:00"
    assert slot_master_range_label(time(18), time(2)) == "18:00–02:00 (+1 day)"
    assert slot_master_range_label(time(9), time(17)) == "09:00–17:00"


@pytest.mark.parametrize("value", ["24:00", "24:00:00"])
def test_api_write_serializer_accepts_2400(value):
    ser = SlotMasterWriteSerializer(data={"slot_number": 2, "open_time": "12:00", "close_time": value, "is_active": True})
    assert ser.is_valid(), ser.errors
    assert ser.validated_data["close_time"] == time(0)


def test_api_write_serializer_still_rejects_bad_times():
    ser = SlotMasterWriteSerializer(data={"slot_number": 1, "open_time": "12:00", "close_time": "24:30", "is_active": True})
    assert not ser.is_valid()
    assert "close_time" in ser.errors


def test_admin_close_field_parses_and_shows_2400():
    field = SlotCloseTimeFormField()
    assert field.clean("24:00") == time(0)
    assert field.clean("24:00:00") == time(0)
    assert field.clean("12:00") == time(12)
    assert field.widget.format_value(time(0)) == "24:00"
    assert field.widget.format_value(time(12)) != "24:00"


def test_addition_request_accepts_2400_end():
    ser = EquipmentAdditionRequestCreateSerializer()
    assert ser.validate_slot_end_time("24:00") == time(0)


def test_overlap_rules():
    # Touching slots are fine, including at midnight.
    assert slot_masters_conflict([(time(0), time(12)), (time(12), time(0))]) is None
    assert slot_masters_conflict([(time(9), time(13)), (time(13), time(17)), (time(17), time(0))]) is None
    assert slot_masters_conflict([(time(18), time(2)), (time(2), time(10))]) is None
    # Overlaps on the same day, and across midnight.
    msg = slot_masters_conflict([(time(0), time(12)), (time(11), time(0))])
    assert msg and "must not overlap" in msg and "11:00–24:00" in msg
    assert slot_masters_conflict([(time(18), time(2)), (time(1), time(3))])
    assert slot_masters_conflict([(time(22), time(0)), (time(23), time(1))])
    # A full-day slot must be alone.
    assert "only active slot" in slot_masters_conflict([(time(0), time(0)), (time(9), time(10))])


@pytest.mark.django_db
def test_model_clean_rejects_overlap_and_allows_touching(egs_factory):
    eq = egs_factory.equipment(with_profile=False)
    SlotMaster.objects.create(equipment=eq, slot_number=1, open_time=time(0), close_time=time(12), is_active=True)
    second = SlotMaster(equipment=eq, slot_number=2, open_time=time(12), close_time=time(0), is_active=True)
    second.clean()
    second.save()

    third = SlotMaster(equipment=eq, slot_number=3, open_time=time(10), close_time=time(14), is_active=True)
    with pytest.raises(ValidationError) as exc:
        third.clean()
    assert "must not overlap" in str(exc.value)
    third.is_active = False
    third.clean()


@pytest.mark.django_db
def test_generated_12_to_24_slot_ends_exactly_at_midnight(egs_factory):
    eq = egs_factory.equipment(with_profile=False, slot_duration_minutes=720)
    SlotMaster.objects.create(equipment=eq, slot_number=1, open_time=time(0), close_time=time(12), is_active=True)
    SlotMaster.objects.create(equipment=eq, slot_number=2, open_time=time(12), close_time=time(0), is_active=True)
    SlotGenerator.generate_slots_for_week(eq, MONDAY, MONDAY + timedelta(days=1))
    slots = list(DailySlot.objects.filter(slot_master__equipment=eq).order_by("start_datetime"))
    assert len(slots) == 4
    for first, nxt in zip(slots, slots[1:]):
        assert first.end_datetime == nxt.start_datetime
    second = slots[1]
    end = timezone.localtime(second.end_datetime)
    assert end.date() == MONDAY + timedelta(days=1) and end.time() == time(0)
    assert second.end_datetime - second.start_datetime == timedelta(hours=12)

    assert [r["end_time"] for r in equipment_slot_times(eq)] == ["12:00", "24:00"]


def test_end_formatting_reads_midnight_as_2400():
    tz = timezone.get_current_timezone()
    start = timezone.make_aware(datetime(2026, 10, 12, 12, 0), tz)
    midnight = timezone.make_aware(datetime(2026, 10, 13, 0, 0), tz)
    overnight = timezone.make_aware(datetime(2026, 10, 13, 2, 0), tz)

    assert strftime_slot_end(midnight, "%H:%M") == "24:00"
    assert strftime_slot_end(midnight, "%d %b %Y, %I:%M %p") == "12 Oct 2026, 24:00"
    assert strftime_slot_end(overnight, "%H:%M") == "02:00"
    assert format_local_end_dt(midnight) == "2026-10-12 24:00"
    assert format_email_end_datetime(midnight) == "12 Oct 2026, 24:00"
    assert format_email_end_datetime(overnight) == "13 Oct 2026, 02:00 AM"

    assert slot_end_on_start_day(start, midnight)
    assert not slot_end_on_start_day(start, overnight)
    day_start = timezone.make_aware(datetime(2026, 10, 12, 0, 0), tz)
    assert slot_end_on_start_day(day_start, midnight)
