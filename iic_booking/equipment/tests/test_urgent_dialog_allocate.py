"""Urgent allocation on any day from a week view, previous slot status audit, Lab Operator email, detail extras."""

from __future__ import annotations

from datetime import date
from datetime import datetime
from datetime import time
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from iic_booking.communication.models import CommunicationLog
from iic_booking.communication.service import CommunicationService
from iic_booking.equipment import api_views
from iic_booking.equipment.booking_events import send_booking_event_notification
from iic_booking.equipment.models import BookingEvent
from iic_booking.equipment.models import DailySlot
from iic_booking.equipment.models import Equipment
from iic_booking.equipment.models import EquipmentOperator
from iic_booking.equipment.models import Holiday
from iic_booking.equipment.models import SlotMaster
from iic_booking.equipment.models import SlotStatus
from iic_booking.equipment.models import SlotStatusChangeLog
from iic_booking.equipment.tests.test_urgent_type_b_allocation import _client
from iic_booking.equipment.tests.test_urgent_type_b_allocation import _pending_request
from iic_booking.equipment.tests.test_urgent_type_b_allocation import _setup
from iic_booking.equipment.urgent_operator_alerts import (
    EMAIL_TEMPLATE_CODE as OPERATOR_TEMPLATE,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(api_views, "notify_waitlist_slots_available", lambda *a, **k: 0)
    monkeypatch.setattr("iic_booking.communication.styled_transactional_emails._send", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def unlocked():
    with patch(
        "iic_booking.users.legacy_ledger.booking_lock.booking_is_locked", return_value=(False, "")
    ), patch(
        "iic_booking.users.legacy_ledger.booking_lock.department_equipment_booking_blocked",
        return_value=(False, ""),
    ), patch("iic_booking.users.rbac.user_has_permission", return_value=True), patch(
        "iic_booking.equipment.booking_events._dispatch_booking_event_notification"
    ):
        yield


def _monday_weeks_ahead(weeks: int) -> date:
    today = timezone.localdate()
    return today - timedelta(days=today.weekday()) + timedelta(weeks=weeks)


def _masters(eq, hours):
    for h in hours:
        SlotMaster.objects.get_or_create(
            equipment=eq, slot_number=h,
            defaults={"open_time": time(h, 0), "close_time": time(h + 1, 0), "is_active": True},
        )


def _slot(eq, day, hour, status=SlotStatus.AVAILABLE):
    _masters(eq, [hour])
    start = timezone.make_aware(datetime.combine(day, time(hour, 0)))
    slot, _ = DailySlot.objects.update_or_create(
        slot_master=SlotMaster.objects.get(equipment=eq, slot_number=hour), date=day,
        defaults={"start_datetime": start, "end_datetime": start + timedelta(hours=1), "status": status},
    )
    return slot


def test_week_view_lists_every_day_of_a_week_not_yet_open_with_holidays():
    eq, student, faculty, oic, _t = _setup()
    urg = _pending_request(eq, student, faculty)
    _masters(eq, [9, 10])
    monday = _monday_weeks_ahead(10)
    Holiday.objects.create(date=monday + timedelta(days=2), reason="Festival", is_active=True)

    res = _client(oic).get(
        f"/api/urgent-booking-requests/{urg.id}/allocation-slots/",
        {"start_date": monday.isoformat(), "end_date": (monday + timedelta(days=6)).isoformat()},
    )
    assert res.status_code == 200, res.content
    body = res.json()
    assert {s["date"] for s in body["slots"]} == {(monday + timedelta(days=i)).isoformat() for i in range(7)}
    assert all(s["selectable"] for s in body["slots"])
    assert body["holidays"] == {(monday + timedelta(days=2)).isoformat(): "Festival"}

    too_long = _client(oic).get(
        f"/api/urgent-booking-requests/{urg.id}/allocation-slots/",
        {"start_date": monday.isoformat(), "end_date": (monday + timedelta(days=9)).isoformat()},
    )
    assert too_long.status_code == 400


def test_week_view_marks_booked_and_past_slots_not_selectable():
    eq, student, faculty, oic, _t = _setup()
    urg = _pending_request(eq, student, faculty)
    yesterday = timezone.localdate() - timedelta(days=1)
    past = _slot(eq, yesterday, 9)
    res = _client(oic).get(
        f"/api/urgent-booking-requests/{urg.id}/allocation-slots/",
        {"start_date": yesterday.isoformat(), "end_date": yesterday.isoformat()},
    )
    row = next(s for s in res.json()["slots"] if s["id"] == past.id)
    assert row["selectable"] is False and row["past"] is True


def _allocate(oic, urg, slots):
    return _client(oic).post(
        f"/api/urgent-booking-requests/{urg.id}/allocate/", {"slot_ids": [s.id for s in slots]}, format="json"
    )


def test_allocate_into_not_available_holiday_far_week_books_and_logs_previous_status():
    eq, student, faculty, oic, _t = _setup()
    urg = _pending_request(eq, student, faculty)
    day = _monday_weeks_ahead(12) + timedelta(days=5)
    Holiday.objects.create(date=day, reason="Closed", is_active=True)
    count = max(1, urg.slots_requested)
    slots = [_slot(eq, day, 8 + i, status=SlotStatus.NOT_AVAILABLE if i == 0 else SlotStatus.AVAILABLE) for i in range(count)]

    res = _allocate(oic, urg, slots)
    assert res.status_code == 200, res.content
    urg.refresh_from_db()
    assert set(DailySlot.objects.filter(id__in=[s.id for s in slots]).values_list("status", flat=True)) == {
        SlotStatus.BOOKED
    }
    created = BookingEvent.objects.get(booking=urg.hold_booking, event_type="CREATED")
    assert created.metadata["previous_slot_statuses"][str(slots[0].id)] == SlotStatus.NOT_AVAILABLE
    assert "Previous slot status" in created.comment
    log = SlotStatusChangeLog.objects.get(equipment=eq)
    assert log.new_status == SlotStatus.BOOKED
    assert log.previous_statuses == {SlotStatus.NOT_AVAILABLE: 1}
    assert log.slot_ids == [slots[0].id]


def test_allocate_all_available_slots_writes_no_status_log():
    eq, student, faculty, oic, _t = _setup()
    urg = _pending_request(eq, student, faculty)
    day = _monday_weeks_ahead(3) + timedelta(days=1)
    slots = [_slot(eq, day, 9 + i) for i in range(max(1, urg.slots_requested))]
    assert _allocate(oic, urg, slots).status_code == 200
    assert not SlotStatusChangeLog.objects.filter(equipment=eq).exists()


def test_booked_slot_is_refused():
    eq, student, faculty, oic, _t = _setup()
    urg = _pending_request(eq, student, faculty)
    day = _monday_weeks_ahead(4)
    slots = [_slot(eq, day, 9 + i) for i in range(max(1, urg.slots_requested))]
    slots[0].status = SlotStatus.BOOKED
    slots[0].save(update_fields=["status"])
    res = _allocate(oic, urg, slots)
    assert res.status_code == 400
    assert res.json()["code"] == "SLOTS_TAKEN"


def _operator(eq, *, active=True, role=EquipmentOperator.Role.PRIMARY):
    op = UserFactory(user_type=UserType.OPERATOR, admin_approved=True)
    if not active:
        type(op).objects.filter(pk=op.pk).update(is_active=False)
    EquipmentOperator.objects.create(equipment=eq, operator=op, role=role)
    return op


class _Log:
    status = CommunicationLog.CommunicationStatus.SENT
    error_message = ""


def test_allocation_emails_active_operators_once_with_slot_and_sample_details():
    eq, student, faculty, oic, _t = _setup()
    operator = _operator(eq)
    inactive = _operator(eq, active=False, role=EquipmentOperator.Role.SECONDARY)
    other_eq = Equipment.objects.create(name="Other", code="OTHERX1", slot_duration_minutes=60, status="ACTIVE")
    outsider = _operator(other_eq)
    urg = _pending_request(eq, student, faculty)
    day = _monday_weeks_ahead(2) + timedelta(days=6)
    slots = [_slot(eq, day, 9 + i) for i in range(max(1, urg.slots_requested))]
    assert _allocate(oic, urg, slots).status_code == 200
    urg.refresh_from_db()
    event = BookingEvent.objects.get(booking=urg.hold_booking, event_type="STATUS_CHANGED")

    sent = []

    def _send(*args, recipient=None, template=None, template_context=None, **kwargs):
        sent.append((recipient.id, template, template_context or {}))
        return _Log()

    with patch.object(CommunicationService, "send_email", side_effect=_send), patch.object(
        CommunicationService, "send_push_notification", return_value=None
    ):
        send_booking_event_notification(event)

    to_operator = [(t, ctx) for rid, t, ctx in sent if rid == operator.id]
    assert [t for t, _ in to_operator] == [OPERATOR_TEMPLATE]
    ctx = to_operator[0][1]
    assert ctx["link"].endswith(f"/booking-management?expand={urg.hold_booking_id}")
    assert ctx["allocated_slots"].count("\n") == len(slots) - 1
    assert f"{day:%d %b %Y}" in ctx["allocated_slots"]
    assert ctx["requester_category"] == str(student.get_user_type_display_label())
    assert ctx["request_id"] == urg.id
    assert ctx["sample_details"]
    for rid in (inactive.id, outsider.id):
        assert rid not in {r for r, _t, _c in sent}
    hold_confirmed = {rid for rid, t, _c in sent if t == "urgent_booking_hold_confirmed_email"}
    assert {student.id, faculty.id} <= hold_confirmed
    assert operator.id not in hold_confirmed


def test_detail_shows_supervisor_decision_date_category_and_wallet_check():
    eq, student, faculty, oic, _t = _setup()
    urg = _pending_request(eq, student, faculty)
    urg.supervisor_decided_at = timezone.now()
    urg.wallet_notes = "Please prioritise"
    urg.save(update_fields=["supervisor_decided_at", "wallet_notes"])
    body = _client(oic).get(f"/api/urgent-booking-requests/{urg.id}/detail/").json()
    assert body["supervisor_decided_at"]
    assert body["wallet_notes"] == "Please prioritise"
    assert body["requester_category"] == str(student.get_user_type_display_label())
    assert body["wallet_check"]["sufficient"] is True
    assert body["wallet_check"]["has_wallet"] is True
