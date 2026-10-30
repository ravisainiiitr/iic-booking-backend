"""Saving equipment from the form updates slot masters in place: daily slots (and the bookings on them) survive."""

from __future__ import annotations

from datetime import time, timedelta

import pytest
from django.utils import timezone

from iic_booking.equipment.models import DailySlot, SlotMaster
from iic_booking.external_services.tests.conftest import Factory, client_for, make_user
from iic_booking.users.models.user_type import UserType

pytestmark = pytest.mark.django_db


@pytest.fixture
def world():
    f = Factory()
    eq = f.equipment()
    day = timezone.localdate() + timedelta(days=3)
    booked = f.booking(make_user(UserType.EXTERNAL), eq, f.at(day, 9))
    free = f.slot(eq, f.at(day, 10))
    admin = make_user(UserType.ADMIN, is_staff=True)
    return {"eq": eq, "booked": booked, "free": free, "admin": admin}


def _rows(eq, changes=None):
    rows = []
    for m in SlotMaster.objects.filter(equipment=eq).order_by("slot_number"):
        row = {"id": m.pk, "slot_number": m.slot_number, "slot_name": m.slot_name or "", "is_active": m.is_active,
               "open_time": m.open_time.strftime("%H:%M"), "close_time": m.close_time.strftime("%H:%M")}
        row.update((changes or {}).get(m.slot_number, {}))
        rows.append(row)
    return rows


def _save(world, rows):
    return client_for(world["admin"]).patch(f"/api/admin/equipment/{world['eq'].pk}/", {"slot_masters": rows}, format="json")


def test_saving_keeps_booked_and_free_daily_slots(world):
    before = set(DailySlot.objects.filter(slot_master__equipment=world["eq"]).values_list("pk", flat=True))
    rows = _rows(world["eq"])
    for r in rows:
        r.pop("id")  # the form before this fix sent no ids: matched by slot number
    r = _save(world, rows)
    assert r.status_code == 200, r.content
    after = set(DailySlot.objects.filter(slot_master__equipment=world["eq"]).values_list("pk", flat=True))
    assert before == after
    assert world["booked"].daily_slots.count() == 1


def test_renumbering_and_renaming_update_in_place(world):
    masters = list(SlotMaster.objects.filter(equipment=world["eq"]).order_by("slot_number"))
    a, b = masters[0], masters[1]
    a_num, b_num = a.slot_number, b.slot_number
    r = _save(world, _rows(world["eq"], {a.slot_number: {"slot_number": b.slot_number, "slot_name": "Morning"},
                                         b.slot_number: {"slot_number": a.slot_number}}))
    assert r.status_code == 200, r.content
    a.refresh_from_db()
    b.refresh_from_db()
    assert (a.slot_number, a.slot_name) == (b_num, "Morning") and b.slot_number == a_num
    assert world["booked"].daily_slots.count() == 1


def test_duplicate_slot_numbers_are_refused_with_a_message(world):
    rows = _rows(world["eq"])
    rows[1]["slot_number"] = rows[0]["slot_number"]
    r = _save(world, rows)
    assert r.status_code == 400 and "slot_masters" in r.json(), r.content


def test_removed_master_keeps_its_booking_and_drops_free_future_slots(world):
    booked_master = world["booked"].daily_slots.get().slot_master
    free_master = world["free"].slot_master
    rows = [r for r in _rows(world["eq"]) if r["id"] not in (booked_master.pk, free_master.pk)]
    r = _save(world, rows)
    assert r.status_code == 200, r.content
    booked_master.refresh_from_db()
    assert booked_master.is_active is False and world["booked"].daily_slots.count() == 1
    assert not SlotMaster.objects.filter(pk=free_master.pk).exists()
    assert not DailySlot.objects.filter(pk=world["free"].pk).exists()
