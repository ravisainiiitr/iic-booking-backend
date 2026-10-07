"""OIC "Mark as repeat & book": the original's parameters (and sample sets) may be edited; the repeat stays free,
its analysis time follows the edited inputs and every change is recorded on the repeat's history."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from iic_booking.equipment.models import (
    Booking,
    BookingEvent,
    BookingEventType,
    BookingStatus,
    ChargeProfile,
    DynamicInputField,
    DynamicInputFieldType,
    EquipmentManager,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def no_booking_locks():
    with patch(
        "iic_booking.users.legacy_ledger.booking_lock.booking_is_locked", return_value=(False, "")
    ), patch(
        "iic_booking.users.legacy_ledger.booking_lock.department_equipment_booking_blocked", return_value=(False, "")
    ):
        yield


@pytest.fixture
def setup(egs_factory):
    eq = egs_factory.equipment(time_formula="A*30")
    DynamicInputField.objects.create(
        equipment=eq, field_key="A", field_label="No. of Samples", field_type=DynamicInputFieldType.NUMERIC,
        options={"min": 1, "max": 10}, default_value="1", is_required=True,
    )
    DynamicInputField.objects.create(
        equipment=eq, field_key="D", field_label="Sample type", field_type=DynamicInputFieldType.TEXT,
    )
    student = egs_factory.student()
    oic = UserFactory(user_type=UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    original = Booking.objects.create(
        user=student,
        equipment=eq,
        charge_profile=ChargeProfile.objects.get(equipment=eq, user_type=UserType.STUDENT),
        status=BookingStatus.COMPLETED,
        completed_at=timezone.now(),
        total_charge=Decimal("20.00"),
        total_time_minutes=60,
        input_values={"A": 2, "D": "Powder"},
        virtual_booking_id=f"IIC{eq.code}{uuid.uuid4().hex[:4]}",
        user_type_snapshot=UserType.STUDENT,
    )
    return egs_factory, eq, student, oic, original


def _slots(f, eq, count):
    start = f.future(days=4, hour=10)
    return [f.slot(eq, start + timedelta(hours=i)).id for i in range(count)]


def _repeat(f, actor, original, **body):
    return f.client_for(actor).post(f"/api/bookings/{original.pk}/create-repeat-booking/", body, format="json")


def test_oic_edits_samples_repeat_stays_free_and_change_is_recorded(setup):
    f, eq, student, oic, original = setup

    res = _repeat(f, oic, original, slot_ids=_slots(f, eq, 2), input_values={"A": "3", "D": "Thin film"})

    assert res.status_code == 201, res.data
    repeat = Booking.objects.get(source_booking=original)
    assert repeat.user_id == student.pk
    assert repeat.total_charge == Decimal("0")
    assert repeat.input_values["A"] == 3 and repeat.input_values["D"] == "Thin film"
    assert repeat.total_time_minutes == 90
    event = BookingEvent.objects.get(booking=repeat, event_type=BookingEventType.REPEAT_SAMPLE_CREATED)
    assert event.created_by_id == oic.pk
    assert "No. of Samples: 2 → 3" in event.comment
    assert "Sample type: Powder → Thin film" in event.comment
    changes = {c["key"]: (c["old"], c["new"]) for c in event.metadata["input_changes"]}
    assert changes == {"A": ("2", "3"), "D": ("Powder", "Thin film")}
    assert event.metadata["inputs_changed_by_id"] == oic.pk
    assert event.metadata["original_input_values"] == {"A": 2, "D": "Powder"}
    original.refresh_from_db()
    assert original.input_values == {"A": 2, "D": "Powder"}


def test_edited_inputs_drive_the_required_slot_time(setup):
    f, eq, _student, oic, original = setup

    res = _repeat(f, oic, original, slot_ids=_slots(f, eq, 1), input_values={"A": 4, "D": "Powder"})

    assert res.status_code == 400
    assert "120 minutes required" in res.data["error"]
    assert not Booking.objects.filter(source_booking=original).exists()


def test_edited_inputs_are_validated_like_a_booking(setup):
    f, eq, _student, oic, original = setup

    res = _repeat(f, oic, original, slot_ids=_slots(f, eq, 2), input_values={"A": 11})

    assert res.status_code == 400
    assert "cannot be greater than 10" in res.data["error"]
    assert not Booking.objects.filter(source_booking=original).exists()


def test_oic_adds_a_sample_set(setup):
    f, eq, _student, oic, original = setup

    res = _repeat(
        f, oic, original, slot_ids=_slots(f, eq, 2),
        input_values={"A": 2, "D": "Powder", "_sample_sets": [{"A": 1, "D": "Pellet"}]},
    )

    assert res.status_code == 201, res.data
    repeat = Booking.objects.get(source_booking=original)
    assert repeat.input_values["_sample_sets"] == [{"A": 1, "D": "Pellet"}]
    assert repeat.total_time_minutes == 90
    assert repeat.total_charge == Decimal("0")
    event = BookingEvent.objects.get(booking=repeat, event_type=BookingEventType.REPEAT_SAMPLE_CREATED)
    assert {"key": "_sample_sets", "label": "Sample sets", "old": "1", "new": "2"} in event.metadata["input_changes"]


def test_unchanged_inputs_keep_the_original_duration(setup):
    f, eq, _student, oic, original = setup
    # The original was booked with an OIC-adjusted 60 minutes although the formula gives A*30 = 60 too;
    # make them differ to prove the stored duration is kept when nothing is edited.
    Booking.objects.filter(pk=original.pk).update(total_time_minutes=45)
    original.refresh_from_db()

    res = _repeat(f, oic, original, slot_ids=_slots(f, eq, 1), input_values={"A": 2, "D": "Powder", "_sample_sets": []})

    assert res.status_code == 201, res.data
    repeat = Booking.objects.get(source_booking=original)
    assert repeat.total_time_minutes == 45
    assert repeat.input_values == {"A": 2, "D": "Powder"}
    event = BookingEvent.objects.get(booking=repeat, event_type=BookingEventType.REPEAT_SAMPLE_CREATED)
    assert "Parameters changed" not in event.comment
    assert not (event.metadata or {}).get("input_changes")


def test_unknown_keys_keep_the_original_values(setup):
    f, eq, _student, oic, original = setup
    Booking.objects.filter(pk=original.pk).update(input_values={"A": 2, "D": "Powder", "legacy": "x"})
    original.refresh_from_db()

    res = _repeat(f, oic, original, slot_ids=_slots(f, eq, 2), input_values={"A": 3, "legacy": "changed", "Z": 9})

    assert res.status_code == 201, res.data
    repeat = Booking.objects.get(source_booking=original)
    assert repeat.input_values == {"A": 3, "D": "Powder", "legacy": "x"}


def test_booking_user_cannot_change_parameters(setup):
    f, eq, student, _oic, original = setup
    Booking.objects.filter(pk=original.pk).update(repeat_sample_enabled=True)
    original.refresh_from_db()

    res = _repeat(f, student, original, slot_ids=_slots(f, eq, 1), input_values={"A": 5})

    assert res.status_code == 201, res.data
    repeat = Booking.objects.get(source_booking=original)
    assert repeat.input_values == {"A": 2, "D": "Powder"}
    assert repeat.total_time_minutes == 60


def test_print_repeat_quantity_edit_scales_print_time(egs_factory, settings, tmp_path):
    from iic_booking.equipment.fabrication import QUANTITY_MARKER_KEY, RESERVED_KEYS, ensure_fabrication_quantity_inputs

    from .fabrication_helpers import funded_student, print_equipment, print_material, print_part

    settings.MEDIA_ROOT = str(tmp_path)
    settings.AWS_STORAGE_BUCKET_NAME = ""
    eq = print_equipment(egs_factory, hourly_rate="60.00")
    ensure_fabrication_quantity_inputs(eq)
    owner, _sub = funded_student(egs_factory)
    original = egs_factory.booking(
        owner, eq, egs_factory.future(days=-3), total_charge="46.00",
        input_values={"A": 1, QUANTITY_MARKER_KEY: True, "B": "PLA-FDM", "C": 30},
    )
    part = print_part(eq, owner, print_material(eq), weight="10.2", minutes=30, booking=original)
    original.print_analysis = part
    original.status = BookingStatus.COMPLETED
    original.total_time_minutes = 30
    original.save(update_fields=["print_analysis", "status", "total_time_minutes"])
    oic = UserFactory(user_type=UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq, manager=oic)

    too_many = _repeat(egs_factory, oic, original, slot_ids=_slots(egs_factory, eq, 1), input_values={"A": 1001})
    assert too_many.status_code == 400

    res = _repeat(egs_factory, oic, original, slot_ids=_slots(egs_factory, eq, 1), input_values={"A": 2})

    assert res.status_code == 201, res.data
    repeat = Booking.objects.get(source_booking=original)
    assert repeat.total_charge == Decimal("0")
    assert repeat.total_time_minutes == 60
    assert repeat.input_values["A"] == 2 and repeat.input_values["C"] == 60
    assert not any(key in repeat.input_values for key in RESERVED_KEYS)


def test_preview_returns_time_and_changes_without_saving(setup):
    f, _eq, _student, oic, original = setup
    url = f"/api/bookings/{original.pk}/repeat-booking-preview/"

    res = f.client_for(oic).post(url, {"input_values": {"A": 5, "D": "Powder"}}, format="json")
    assert res.status_code == 200, res.data
    assert res.data["total_time_minutes"] == 150
    assert res.data["total_charge"] == "0"
    assert [c["key"] for c in res.data["input_changes"]] == ["A"]

    unchanged = f.client_for(oic).post(url, {"input_values": {"A": 2, "D": "Powder"}}, format="json")
    assert unchanged.data == {"total_time_minutes": 60, "input_changes": [], "total_charge": "0"}

    bad = f.client_for(oic).post(url, {"input_values": {"A": 0}}, format="json")
    assert bad.status_code == 400
    assert "cannot be less than 1" in bad.data["error"]

    operator = UserFactory(user_type=UserType.OPERATOR)
    assert f.client_for(operator).post(url, {"input_values": {"A": 3}}, format="json").status_code == 403
    assert not Booking.objects.filter(source_booking=original).exists()


def test_only_repeat_managers_can_book_an_edited_repeat(setup):
    f, eq, _student, _oic, original = setup
    operator = UserFactory(user_type=UserType.OPERATOR)
    other_oic = UserFactory(user_type=UserType.MANAGER)
    EquipmentManager.objects.create(equipment=f.equipment(), manager=other_oic)
    admin = UserFactory(user_type=UserType.ADMIN, is_staff=True, is_superuser=True)

    assert _repeat(f, operator, original, input_values={"A": 3}).status_code == 403
    assert _repeat(f, other_oic, original, input_values={"A": 3}).status_code == 403
    assert not Booking.objects.filter(source_booking=original).exists()

    res = _repeat(f, admin, original, slot_ids=_slots(f, eq, 2), input_values={"A": 3})
    assert res.status_code == 201, res.data
    assert Booking.objects.get(source_booking=original).input_values["A"] == 3
