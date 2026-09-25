"""Cross-equipment rescheduling within an Equipment Group (both reschedule endpoints + options endpoint)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

from iic_booking.equipment import equipment_group_service as egs
from iic_booking.equipment.models import (
    BookingEvent,
    BookingEventType,
    DailySlot,
    DynamicInputField,
    DynamicInputFieldType,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _setup(egs_factory, **group_switches):
    group = egs_factory.group(cross_rescheduling_enabled=True, **group_switches)
    source = egs_factory.equipment(group)
    target = egs_factory.equipment(group)
    owner = egs_factory.student()
    booking = egs_factory.booking(owner, source, egs_factory.future(days=4, hour=10))
    new_start = egs_factory.future(days=5, hour=11)
    target_slot = egs_factory.slot(target, new_start)
    return group, source, target, owner, booking, target_slot


def _body(slot, target=None):
    body = {"start_time": slot.start_datetime.isoformat(), "end_time": slot.end_datetime.isoformat()}
    if target is not None:
        body["target_equipment_id"] = target.pk
    return body


@pytest.mark.django_db
def test_user_cross_reschedule_moves_booking_and_keeps_charge(egs_factory, egs_flags_on, egs_quiet_side_effects):
    _, source, target, owner, booking, target_slot = _setup(egs_factory)
    old_slot_ids = list(booking.daily_slots.values_list("id", flat=True))

    res = egs_factory.client_for(owner).post(
        f"/api/bookings/{booking.pk}/user-reschedule/", _body(target_slot, target), format="json"
    )

    assert res.status_code == 200, res.data
    assert res.data["cross_equipment"] is True
    booking.refresh_from_db()
    assert booking.equipment_id == target.pk
    assert booking.charge_profile.equipment_id == target.pk
    assert booking.total_charge == Decimal("10.00")
    target_slot.refresh_from_db()
    assert target_slot.booking_id == booking.pk and target_slot.status == "BOOKED"
    assert not DailySlot.objects.filter(id__in=old_slot_ids, booking=booking).exists()

    event = BookingEvent.objects.filter(booking=booking, event_type=BookingEventType.RESCHEDULED).latest("pk")
    assert event.metadata["cross_equipment"] is True
    assert event.metadata["previous_equipment_id"] == source.pk
    assert event.metadata["new_equipment_id"] == target.pk
    assert event.metadata["charged_amount"] == "10.00"
    assert target.name in event.comment
    assert egs_quiet_side_effects.waitlist == [(source.pk, old_slot_ids)]


@pytest.mark.django_db
def test_staff_cross_reschedule_uses_same_rules(egs_factory, egs_flags_on, egs_quiet_side_effects):
    _, _, target, _, booking, target_slot = _setup(egs_factory)
    admin = UserFactory(user_type=UserType.ADMIN, is_staff=True)

    res = egs_factory.client_for(admin).post(
        f"/api/bookings/{booking.pk}/reschedule/", _body(target_slot, target), format="json"
    )
    assert res.status_code == 200, res.data
    booking.refresh_from_db()
    assert booking.equipment_id == target.pk


@pytest.mark.django_db
def test_different_group_is_rejected(egs_factory, egs_flags_on, egs_quiet_side_effects):
    _, source, _, owner, booking, _ = _setup(egs_factory)
    other_group = egs_factory.group(cross_rescheduling_enabled=True)
    stranger = egs_factory.equipment(other_group)
    stranger_slot = egs_factory.slot(stranger, egs_factory.future(days=5, hour=12))

    res = egs_factory.client_for(owner).post(
        f"/api/bookings/{booking.pk}/user-reschedule/", _body(stranger_slot, stranger), format="json"
    )
    assert res.status_code == 400
    assert res.data["code"] == "DIFFERENT_GROUP"
    booking.refresh_from_db()
    assert booking.equipment_id == source.pk


@pytest.mark.django_db
def test_incompatible_inputs_are_rejected(egs_factory, egs_flags_on, egs_quiet_side_effects):
    _, source, target, owner, booking, target_slot = _setup(egs_factory)
    DynamicInputField.objects.create(
        equipment=target, field_key="A", field_label="Detector", field_type=DynamicInputFieldType.TEXT,
        is_required=True,
    )

    res = egs_factory.client_for(owner).post(
        f"/api/bookings/{booking.pk}/user-reschedule/", _body(target_slot, target), format="json"
    )
    assert res.status_code == 400
    assert res.data["code"] == "INPUTS_INCOMPATIBLE"
    booking.refresh_from_db()
    assert booking.equipment_id == source.pk


@pytest.mark.django_db
def test_slot_taken_during_lock_changes_nothing(egs_factory, egs_flags_on, egs_quiet_side_effects):
    from iic_booking.equipment.slot_utils import SlotAvailabilityChecker

    _, source, target, owner, booking, target_slot = _setup(egs_factory)
    rival = egs_factory.booking(egs_factory.student(), source, egs_factory.future(days=6, hour=9))
    old_slot_ids = list(booking.daily_slots.values_list("id", flat=True))

    def grab_then_approve(slot):
        DailySlot.objects.filter(pk=target_slot.pk).update(booking=rival, status="BOOKED")
        return True

    with patch.object(SlotAvailabilityChecker, "is_slot_available", staticmethod(grab_then_approve)):
        res = egs_factory.client_for(owner).post(
            f"/api/bookings/{booking.pk}/user-reschedule/", _body(target_slot, target), format="json"
        )

    assert res.status_code == 409
    assert res.data["code"] == "SLOT_TAKEN"
    booking.refresh_from_db()
    assert booking.equipment_id == source.pk
    assert set(booking.daily_slots.values_list("id", flat=True)) == set(old_slot_ids)


@pytest.mark.django_db
def test_wrong_slot_count_is_rejected(egs_factory, egs_flags_on, egs_quiet_side_effects):
    _, source, target, owner, booking, target_slot = _setup(egs_factory)
    second = egs_factory.slot(target, target_slot.end_datetime)

    res = egs_factory.client_for(owner).post(
        f"/api/bookings/{booking.pk}/user-reschedule/",
        {"start_time": target_slot.start_datetime.isoformat(), "end_time": second.end_datetime.isoformat(),
         "target_equipment_id": target.pk},
        format="json",
    )
    assert res.status_code == 400
    assert res.data["code"] == "SLOT_COUNT_MISMATCH"
    booking.refresh_from_db()
    assert booking.equipment_id == source.pk


@pytest.mark.django_db
def test_same_equipment_target_uses_existing_path(egs_factory, egs_flags_on, egs_quiet_side_effects):
    _, source, _, owner, booking, _ = _setup(egs_factory)
    same_slot = egs_factory.slot(source, egs_factory.future(days=5, hour=15))

    with patch.object(egs, "perform_cross_equipment_reschedule") as cross:
        res = egs_factory.client_for(owner).post(
            f"/api/bookings/{booking.pk}/user-reschedule/", _body(same_slot, source), format="json"
        )
    cross.assert_not_called()
    assert res.status_code == 200, res.data
    booking.refresh_from_db()
    assert booking.equipment_id == source.pk


@pytest.mark.django_db
def test_reschedule_options_endpoint(egs_factory, egs_flags_on):
    _, source, target, owner, booking, _ = _setup(egs_factory)

    res = egs_factory.client_for(owner).get(f"/api/bookings/{booking.pk}/reschedule-options/")
    assert res.status_code == 200, res.data
    assert res.data["cross_rescheduling_enabled"] is True
    ids = [o["equipment_id"] for o in res.data["options"]]
    assert ids == [source.pk, target.pk]
    assert res.data["options"][0]["is_original"] is True

    stranger = egs_factory.student()
    denied = egs_factory.client_for(stranger).get(f"/api/bookings/{booking.pk}/reschedule-options/")
    assert denied.status_code == 403
