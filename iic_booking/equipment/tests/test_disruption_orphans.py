"""Slots put into a disruption status by automatic paths are recorded; the orphan repair links the rest."""

from __future__ import annotations

from datetime import timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from iic_booking.equipment.disruption_service import record_unrecorded_upcoming_slots
from iic_booking.equipment.maintenance_policy import record_freed_booking_slots
from iic_booking.equipment.models import (
    DailySlot,
    DisruptionEvent,
    DisruptionEventSlot,
    EquipmentStatus,
    Holiday,
    SlotStatus,
    SlotStatusChangeLog,
)
from iic_booking.equipment.slot_utils import SlotGenerator
from iic_booking.equipment.tests.test_disruption_log import _client
from iic_booking.equipment.tests.test_disruption_log import _equipment
from iic_booking.equipment.tests.test_disruption_log import _future_weekday
from iic_booking.equipment.tests.test_disruption_log import _oic_for
from iic_booking.equipment.tests.test_disruption_log import _slots

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_holidays():
    Holiday.objects.all().delete()


def _linked_event_ids(slots) -> set[int]:
    return set(
        DisruptionEventSlot.objects.filter(
            daily_slot_id__in=[s.pk for s in slots], released_at__isnull=True
        ).values_list("event_id", flat=True)
    )


def test_slots_generated_while_equipment_under_maintenance_join_its_event():
    eq = _equipment()
    eq.status = EquipmentStatus.REPAIR
    eq.save()
    event = DisruptionEvent.objects.get(equipment=eq, scope="EQUIPMENT")
    day = _future_weekday()
    SlotGenerator.generate_slots_for_week(eq, day, day)
    slots = list(DailySlot.objects.filter(slot_master__equipment=eq, date=day))
    assert len(slots) == 4 and {s.status for s in slots} == {SlotStatus.UNDER_MAINTENANCE}
    assert _linked_event_ids(slots) == {event.pk}
    assert DisruptionEvent.objects.filter(equipment=eq).count() == 1

    SlotGenerator.generate_slots_for_week(eq, day, day)
    assert DisruptionEventSlot.objects.filter(event=event).count() == 4


def test_equipment_maintenance_links_upcoming_slots_and_operational_releases_them():
    eq = _equipment()
    oic = _oic_for(eq)
    slots = _slots(eq, _future_weekday())
    res = _client(oic).patch(f"/api/equipments/{eq.pk}/", {"status": "REPAIR"}, format="json")
    assert res.status_code == 200, res.data
    event = DisruptionEvent.objects.get(equipment=eq)
    assert event.scope == "EQUIPMENT"
    assert set(DailySlot.objects.filter(pk__in=[s.pk for s in slots]).values_list("status", flat=True)) == {
        SlotStatus.UNDER_MAINTENANCE
    }
    assert _linked_event_ids(slots) == {event.pk}

    res = _client(oic).patch(f"/api/equipments/{eq.pk}/", {"status": "ACTIVE"}, format="json")
    assert res.status_code == 200
    event.refresh_from_db()
    assert event.ended_at is not None
    assert not _linked_event_ids(slots)
    assert DisruptionEventSlot.objects.filter(event=event, released_at__isnull=False).count() == 4


def test_freed_booking_slot_back_in_disruption_is_recorded_once():
    eq = _equipment()
    s9, s10, *_ = _slots(eq, _future_weekday())
    DailySlot.objects.filter(pk__in=[s9.pk, s10.pk]).update(status=SlotStatus.OPERATOR_ABSENT)
    record_freed_booking_slots(eq, [s9.pk, s10.pk])
    event = DisruptionEvent.objects.get(equipment=eq)
    assert event.disruption_type == "OPERATOR_ABSENT" and event.scope == "SLOTS"
    assert _linked_event_ids([s9, s10]) == {event.pk}
    log = SlotStatusChangeLog.objects.get(equipment=eq)
    assert log.previous_statuses == {SlotStatus.BOOKED: 2}

    record_freed_booking_slots(eq, [s9.pk, s10.pk])
    assert DisruptionEvent.objects.filter(equipment=eq).count() == 1


def test_daily_safety_net_records_upcoming_slots_but_respects_deleted_entries():
    eq = _equipment()
    s9, s10, s11, s12 = _slots(eq, _future_weekday())
    DailySlot.objects.filter(pk__in=[s9.pk, s10.pk, s12.pk]).update(status=SlotStatus.UNDER_MAINTENANCE)
    deleted = DisruptionEvent.objects.create(
        equipment=eq, disruption_type="UNDER_MAINTENANCE", scope="SLOTS", start_at=s12.start_datetime,
        end_at=s12.end_datetime, is_deleted=True,
    )
    DisruptionEventSlot.objects.create(
        event=deleted, daily_slot=s12, start_datetime=s12.start_datetime, end_datetime=s12.end_datetime
    )
    stats = record_unrecorded_upcoming_slots()
    assert stats["opened"] == 1
    event = DisruptionEvent.objects.get(equipment=eq, is_deleted=False)
    assert _linked_event_ids([s9, s10]) == {event.pk}
    assert _linked_event_ids([s12]) == {deleted.pk}
    assert record_unrecorded_upcoming_slots()["opened"] == 0


def test_repair_links_orphans_and_is_idempotent():
    eq = _equipment()
    oic = _oic_for(eq)
    day = _future_weekday()
    s9, s10, s11, s12 = _slots(eq, day)
    next_day = _slots(eq, day + timedelta(days=1 if day.weekday() < 4 else 3))
    DailySlot.objects.filter(pk__in=[s9.pk, s10.pk]).update(status=SlotStatus.UNDER_MAINTENANCE)
    DailySlot.objects.filter(pk=next_day[0].pk).update(status=SlotStatus.OPERATOR_ABSENT)
    SlotStatusChangeLog.objects.create(
        equipment=eq, new_status=SlotStatus.OPERATOR_ABSENT, slot_ids=[next_day[0].pk], slot_count=1,
        changed_by=oic, changed_at=timezone.now(),
    )
    DailySlot.objects.filter(pk=s12.pk).update(status=SlotStatus.UNDER_MAINTENANCE)
    deleted = DisruptionEvent.objects.create(
        equipment=eq, disruption_type="UNDER_MAINTENANCE", scope="SLOTS", start_at=s12.start_datetime,
        end_at=s12.end_datetime, is_deleted=True,
    )
    DisruptionEventSlot.objects.create(
        event=deleted, daily_slot=s12, start_datetime=s12.start_datetime, end_datetime=s12.end_datetime
    )

    whole = _equipment()
    whole.status = EquipmentStatus.REPAIR
    whole.save()
    whole_event = DisruptionEvent.objects.get(equipment=whole, scope="EQUIPMENT")
    whole_slots = _slots(whole, day)
    DailySlot.objects.filter(pk__in=[s.pk for s in whole_slots]).update(status=SlotStatus.UNDER_MAINTENANCE)
    statuses_before = dict(DailySlot.objects.values_list("id", "status"))

    out = StringIO()
    call_command("repair_disruption_orphans", "--focus-code", "NOPE", stdout=out)
    text = out.getvalue()
    assert "mode=DRY RUN" in text and "orphan_slots=7" in text
    assert "linked_to_equipment_event=4" in text and "new_events=2" in text and "deleted_event_only=1" in text
    assert "eq=NOPE nothing to repair" in text
    assert DisruptionEvent.objects.count() == 2

    call_command("repair_disruption_orphans", "--apply", stdout=StringIO())
    um = DisruptionEvent.objects.get(equipment=eq, is_deleted=False, disruption_type="UNDER_MAINTENANCE")
    assert um.backfilled and um.source == "BACKFILL" and um.reason == "" and um.started_by_id is None
    assert _linked_event_ids([s9, s10]) == {um.pk}
    absent = DisruptionEvent.objects.get(equipment=eq, disruption_type="OPERATOR_ABSENT")
    assert absent.started_by_id == oic.pk and absent.started_by_role == "OIC"
    assert _linked_event_ids([s12]) == {deleted.pk}
    assert _linked_event_ids(whole_slots) == {whole_event.pk}
    assert not DisruptionEvent.objects.filter(equipment=whole, scope="SLOTS").exists()
    assert dict(DailySlot.objects.values_list("id", "status")) == statuses_before

    rerun = StringIO()
    call_command("repair_disruption_orphans", "--apply", stdout=rerun)
    assert "orphan_slots=0" in rerun.getvalue()
    assert DisruptionEvent.objects.count() == 4
