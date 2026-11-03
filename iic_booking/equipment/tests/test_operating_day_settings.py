"""Operating day start: sent with the week's slots, shown on Equipment Configuration, changed only by the Main Admin."""

from __future__ import annotations

from datetime import time, timedelta

import pytest
from django.utils import timezone

from iic_booking.equipment.models import DailySlot, EquipmentManager, SlotMaster, SlotStatus
from iic_booking.equipment.slot_utils import SlotGenerator
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _six_hour_masters(eq):
    for i, (o, c) in enumerate([(9, 15), (15, 21), (21, 3), (3, 9)], 1):
        SlotMaster.objects.create(equipment=eq, slot_number=i, open_time=time(o), close_time=time(c), is_active=True)


def _workday(offset=7):
    d = timezone.localdate() + timedelta(days=offset)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _url(eq):
    return f"/api/oic/equipment-settings/{eq.pk}/"


def test_slots_payload_names_the_operating_day_start(egs_factory):
    eq = egs_factory.equipment(operating_day_start=time(9), with_profile=False)
    _six_hour_masters(eq)
    day = _workday()
    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True)
    res = egs_factory.client_for(admin).get(f"/api/equipments/{eq.pk}/slots/", {"start_date": day.isoformat(), "end_date": day.isoformat()})
    assert res.status_code == 200
    assert res.data["operating_day_start"] == "09:00"


def test_settings_row_shows_it_and_only_the_main_admin_changes_it(egs_factory):
    eq = egs_factory.equipment(with_profile=False)
    _six_hour_masters(eq)
    oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    rows = egs_factory.client_for(oic).get("/api/oic/equipment-settings/").data["equipments"]
    row = next(r for r in rows if r["equipment_id"] == eq.pk)
    assert row["settings"]["operating_day_start"] is None

    res = egs_factory.client_for(oic).patch(_url(eq), {"operating_day_start": "09:00"}, format="json")
    assert res.status_code == 400 and "operating_day_start" in res.data["errors"]
    # Sending the unchanged value (the whole form) is fine for the OIC.
    assert egs_factory.client_for(oic).patch(_url(eq), {"operating_day_start": ""}, format="json").status_code == 200

    day = _workday()
    SlotGenerator.generate_daily_slots(eq, day, allow_holiday=True)
    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True)
    res = egs_factory.client_for(admin).patch(_url(eq), {"operating_day_start": "09:00"}, format="json")
    assert res.status_code == 200, res.data
    eq.refresh_from_db()
    assert eq.operating_day_start == time(9)
    early = DailySlot.objects.get(slot_master__equipment=eq, date=day, slot_master__open_time=time(3))
    assert timezone.localtime(early.start_datetime).date() == day + timedelta(days=1)


def test_change_refused_while_a_booked_slot_would_move(egs_factory):
    eq = egs_factory.equipment(with_profile=False)
    _six_hour_masters(eq)
    day = _workday()
    SlotGenerator.generate_daily_slots(eq, day, allow_holiday=True)
    DailySlot.objects.filter(slot_master__equipment=eq, date=day, slot_master__open_time=time(3)).update(status=SlotStatus.BOOKED)
    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True)
    res = egs_factory.client_for(admin).patch(_url(eq), {"operating_day_start": "09:00"}, format="json")
    assert res.status_code == 400 and "would move" in res.data["errors"]["operating_day_start"]
    eq.refresh_from_db()
    assert eq.operating_day_start is None
