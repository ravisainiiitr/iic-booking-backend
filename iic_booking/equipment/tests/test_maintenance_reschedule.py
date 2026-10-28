"""Bookings on hold for equipment maintenance are rescheduled automatically, fairly (``maintenance_reschedule``)."""

from __future__ import annotations

from datetime import datetime, time, timedelta
from io import StringIO

import pytest
from django.core.management import call_command
from django.utils import timezone

from iic_booking.equipment import maintenance_reschedule as mr
from iic_booking.equipment.maintenance_policy import put_booking_on_maintenance_hold
from iic_booking.equipment.models import (
    Booking,
    BookingDisruptionKind,
    BookingEvent,
    BookingStatus,
    DailySlot,
    EquipmentMaintenancePeriod,
    EquipmentStatus,
    Holiday,
    MaintenanceNotification,
    MaintenanceRescheduleSettings,
    SlotStatus,
)
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


@pytest.fixture(autouse=True)
def _locmem_email(settings):
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    settings.FRONTEND_URL = "https://portal.example"


@pytest.fixture
def lab():
    eq = _equipment()
    oic = _oic_for(eq)
    day = _future_weekday(14)
    now = timezone.make_aware(datetime.combine(day - timedelta(days=1), time(6)))
    EquipmentMaintenancePeriod.objects.create(equipment=eq, started_at=now - timedelta(days=3), ended_at=now)
    return eq, oic, day, now


def _at(day, hour):
    return timezone.make_aware(datetime.combine(day, time(hour)))


def _held(eq, user, day, hours=(9,), now=None):
    """A booking on the given past slots, put on hold for maintenance."""
    slots = {timezone.localtime(s.start_datetime).hour: s for s in _slots(eq, day)}
    booking = _book(eq, slots[hours[0]], user)
    DailySlot.objects.filter(pk__in=[slots[h].pk for h in hours]).update(status=SlotStatus.BOOKED, booking=booking)
    booking = Booking.objects.prefetch_related("daily_slots").select_related("equipment", "user").get(pk=booking.pk)
    assert put_booking_on_maintenance_hold(booking, now or timezone.now(), reason="test", send_email=False)
    return booking


def _starts(booking):
    booking.refresh_from_db()
    return [timezone.localtime(s.start_datetime) for s in booking.daily_slots.order_by("start_datetime")]


def _allocate(eq, now, capture):
    with capture(execute=True):
        return mr.allocate_for_equipment(eq, now=now)


def _past(n):
    d = timezone.localdate() - timedelta(days=n)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


# --- order and fairness -------------------------------------------------------------------------------------------


def test_holds_are_placed_in_order_of_the_original_slot_then_booking_time(lab, django_capture_on_commit_callbacks):
    eq, _oic, day, now = lab
    early, late, tie = _user(UserType.STUDENT), _user(UserType.STUDENT), _user(UserType.STUDENT)
    older_day, newer_day = _past(9), _past(2)
    b_late = _held(eq, late, newer_day, (9,))
    b_early = _held(eq, early, older_day, (10,))
    b_tie = _held(eq, tie, newer_day, (10,))

    res = _allocate(eq, now, django_capture_on_commit_callbacks)

    assert [x["booking"].pk for x in res.placed] == [b_early.pk, b_late.pk, b_tie.pk]
    assert [_starts(b)[0] for b in (b_early, b_late, b_tie)] == [_at(day, 9), _at(day, 10), _at(day, 11)]
    for b in (b_early, b_late, b_tie):
        b.refresh_from_db()
        assert b.status == BookingStatus.BOOKED
        assert not b.maintenance_auto_reschedule_pending and not b.maintenance_disruption_flag
        assert b.auto_rescheduled_at == now and b.auto_reschedule_extension_hours == 24


def test_earlier_booking_wins_when_two_holds_share_the_original_time(lab, django_capture_on_commit_callbacks):
    eq, _oic, _day, now = lab
    first, second = _user(UserType.STUDENT), _user(UserType.STUDENT)
    a = _held(eq, first, _past(3), (9,))
    b = _held(eq, second, _past(3), (11,))
    Booking.objects.filter(pk=b.pk).update(created_at=timezone.now() - timedelta(days=30))
    DailySlot.objects.filter(booking=b).update(start_datetime=_at(_past(3), 9), end_datetime=_at(_past(3), 10))

    res = _allocate(eq, now, django_capture_on_commit_callbacks)

    assert [x["booking"].pk for x in res.placed] == [b.pk, a.pk]


def test_multi_slot_booking_gets_back_to_back_slots_of_the_same_length(lab, django_capture_on_commit_callbacks):
    eq, _oic, day, now = lab
    taken = _slots(eq, day)
    _book(eq, taken[1], _user(UserType.STUDENT))
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9, 10))

    _allocate(eq, now, django_capture_on_commit_callbacks)

    assert _starts(b) == [_at(day, 11), _at(day, 12)]
    assert {s.status for s in b.daily_slots.all()} == {SlotStatus.BOOKED}


def _next_working_day(d):
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _held_over_days(eq, user, days_hours, now):
    """A booking over several days (e.g. a whole day then the next morning), put on hold for maintenance."""
    picked = []
    for day, hours in days_hours:
        by_hour = {timezone.localtime(s.start_datetime).hour: s for s in _slots(eq, day)}
        picked += [by_hour[h] for h in hours]
    booking = _book(eq, picked[0], _user(UserType.STUDENT) if user is None else user)
    DailySlot.objects.filter(pk__in=[s.pk for s in picked]).update(status=SlotStatus.BOOKED, booking=booking)
    booking = Booking.objects.prefetch_related("daily_slots").select_related("equipment", "user").get(pk=booking.pk)
    assert put_booking_on_maintenance_hold(booking, now, reason="test", send_email=False)
    return booking


def test_booking_longer_than_a_day_is_placed_day_by_day(lab, django_capture_on_commit_callbacks):
    """Like 16 + 5 half-hour slots on two days: no single run of 21 exists, so each day's block gets its own day."""
    eq, _oic, day, now = lab
    first, second = _past(3), _next_working_day(_past(3))
    b = _held_over_days(eq, None, [(first, (9, 10, 11, 12)), (second, (9, 10))], now)

    res = _allocate(eq, now, django_capture_on_commit_callbacks)

    assert [x["booking"].pk for x in res.placed] == [b.pk] and not res.unplaced
    nxt = _next_working_day(day)
    assert _starts(b) == [_at(day, 9), _at(day, 10), _at(day, 11), _at(day, 12), _at(nxt, 9), _at(nxt, 10)]
    b.refresh_from_db()
    assert b.status == BookingStatus.BOOKED and not b.maintenance_auto_reschedule_pending
    assert b.auto_rescheduled_from == _at(first, 9)  # the full booking was moved, including its first day


def test_day_blocks_move_to_the_next_day_with_room(lab, django_capture_on_commit_callbacks):
    eq, _oic, day, now = lab
    _book(eq, _slots(eq, day)[0], _user(UserType.STUDENT))  # the first day has no room for a full-day block
    b = _held_over_days(eq, None, [(_past(3), (9, 10, 11, 12)), (_next_working_day(_past(3)), (9,))], now)

    _allocate(eq, now, django_capture_on_commit_callbacks)

    d1 = _next_working_day(day)
    d2 = _next_working_day(d1)
    assert _starts(b) == [_at(d1, 9), _at(d1, 10), _at(d1, 11), _at(d1, 12), _at(d2, 9)]


def test_multi_day_preview_shows_the_placement(lab):
    eq, _oic, day, now = lab
    b = _held_over_days(eq, None, [(_past(3), (9, 10, 11, 12)), (_next_working_day(_past(3)), (9, 10))], now)
    _slots(eq, day), _slots(eq, _next_working_day(day))  # the read-only preview does not generate slots

    preview = mr.allocation_preview(eq, now=now)

    row = next(r for r in preview["rows"] if r["booking_id"] == b.pk)
    assert row["slots"] == 6 and row["new_start"] == mr._iso(_at(day, 9))
    assert row["new_end"] == mr._iso(_at(_next_working_day(day), 11))


def test_staff_can_move_a_multi_day_hold_by_hand(lab):
    eq, oic, day, now = lab
    b = _held_over_days(eq, None, [(_past(3), (9, 10, 11, 12)), (_next_working_day(_past(3)), (9, 10))], now)
    nxt = _next_working_day(day)
    _slots(eq, day), _slots(eq, nxt)

    r = _client(_user(UserType.ADMIN, is_staff=True)).post(f"/api/bookings/{b.pk}/reschedule/",
                          {"start_time": _at(day, 9).isoformat(), "end_time": _at(nxt, 11).isoformat()}, format="json")

    assert r.status_code == 200, r.content
    assert _starts(b) == [_at(day, 9), _at(day, 10), _at(day, 11), _at(day, 12), _at(nxt, 9), _at(nxt, 10)]


def test_multi_day_range_with_a_gap_is_refused(lab):
    eq, oic, day, now = lab
    b = _held_over_days(eq, None, [(_past(3), (9, 10, 11, 12)), (_next_working_day(_past(3)), (9, 10))], now)
    nxt = _next_working_day(day)
    other = _book(eq, _slots(eq, day)[2], _user(UserType.STUDENT))  # someone else holds 11:00 on the first day
    DailySlot.objects.filter(pk=_slots(eq, day)[2].pk).update(status=SlotStatus.BOOKED, booking=other)
    _slots(eq, nxt)

    r = _client(_user(UserType.ADMIN, is_staff=True)).post(f"/api/bookings/{b.pk}/reschedule/",
                          {"start_time": _at(day, 9).isoformat(), "end_time": _at(nxt, 11).isoformat()}, format="json")

    assert r.status_code == 400 and "needs 6" in r.json()["error"], r.content
    b.refresh_from_db()
    assert b.status == BookingStatus.DISRUPTION_PENDING  # nothing moved


def test_minimum_notice_and_never_the_same_day(lab, django_capture_on_commit_callbacks):
    eq, _oic, day, now = lab
    MaintenanceRescheduleSettings.objects.create(min_lead_hours=28)
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9,))

    _allocate(eq, now, django_capture_on_commit_callbacks)

    assert _starts(b)[0] == _at(day, 10)
    assert mr.earliest_new_start(_at(day, 6), MaintenanceRescheduleSettings(min_lead_hours=0)) == _at(
        day + timedelta(days=1), 0
    )


def test_holidays_and_weekends_are_skipped(lab, django_capture_on_commit_callbacks):
    eq, _oic, day, now = lab
    Holiday.objects.create(date=day, reason="Institute holiday", is_active=True)
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9,))

    _allocate(eq, now, django_capture_on_commit_callbacks)

    new = _starts(b)[0]
    expected = day + timedelta(days=1)
    while expected.weekday() >= 5:
        expected += timedelta(days=1)
    assert new == _at(expected, 9)


def test_mode_rules_are_respected(lab, django_capture_on_commit_callbacks, monkeypatch):
    from iic_booking.equipment import mode_utils

    eq, _oic, day, now = lab
    real = mode_utils.equipment_bookable_on_date

    def exclusive_at_nine(equipment, on_date, at_time=None):
        if on_date == day and at_time is not None and at_time.hour == 9:
            return False, "Another mode of this instrument is running exclusively at this time."
        return real(equipment, on_date, at_time)

    monkeypatch.setattr(mode_utils, "equipment_bookable_on_date", exclusive_at_nine)
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9,))

    _allocate(eq, now, django_capture_on_commit_callbacks)

    assert _starts(b)[0] == _at(day, 10)


def test_blocked_and_under_maintenance_slots_are_not_used(lab, django_capture_on_commit_callbacks):
    eq, _oic, day, now = lab
    s9, s10, *_ = _slots(eq, day)
    DailySlot.objects.filter(pk=s9.pk).update(status=SlotStatus.BLOCKED)
    DailySlot.objects.filter(pk=s10.pk).update(status=SlotStatus.UNDER_MAINTENANCE)
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9,))

    _allocate(eq, now, django_capture_on_commit_callbacks)

    assert _starts(b)[0] == _at(day, 11)


# --- booking update, emails, idempotency --------------------------------------------------------------------------


def test_reschedule_records_history_emails_the_user_and_staff_once(lab, mailoutbox, django_capture_on_commit_callbacks):
    eq, oic, day, now = lab
    user = _user(UserType.STUDENT)
    b = _held(eq, user, _past(2), (10,))
    original = _at(_past(2), 10)
    charge = b.total_charge

    _allocate(eq, now, django_capture_on_commit_callbacks)
    again = _allocate(eq, now + timedelta(minutes=5), django_capture_on_commit_callbacks)

    assert again.placed == [] and again.unplaced == []
    b.refresh_from_db()
    assert b.total_charge == charge and b.auto_rescheduled_from == original
    event = BookingEvent.objects.get(booking=b, metadata__maintenance_auto_reschedule=True, new_status=BookingStatus.BOOKED)
    assert event.created_by_id is None
    assert event.comment.startswith(f"Auto-rescheduled after maintenance (originally {original:%a %d %b %Y, %I:%M %p})")
    user_mail = [m for m in mailoutbox if user.email in m.to]
    assert len(user_mail) == 1 and user_mail[0].subject.startswith(f"Booking {b.virtual_booking_id} rescheduled")
    body = user_mail[0].body
    assert "earliest free slot" in body and "full refund" in body and f"{_at(day, 9):%a %d %b %Y, %I:%M %p}" in body
    staff = [m.subject for m in mailoutbox if oic.email in m.to]
    assert staff == [f"Rescheduled automatically: {eq.name} – 1 booking"]
    assert MaintenanceNotification.objects.filter(kind="RESCHEDULED", booking=b).count() == 1
    assert not any("you may reschedule" in m.subject for m in mailoutbox)


def test_booking_that_does_not_fit_stays_on_hold_and_staff_are_told(lab, mailoutbox, django_capture_on_commit_callbacks):
    eq, oic, day, now = lab
    MaintenanceRescheduleSettings.objects.create(horizon_days=1)
    for s in _slots(eq, day):
        _book(eq, s, _user(UserType.STUDENT))
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9,))

    res = _allocate(eq, now, django_capture_on_commit_callbacks)

    assert [x.pk for x in res.unplaced] == [b.pk]
    b.refresh_from_db()
    assert b.status == BookingStatus.DISRUPTION_PENDING and b.maintenance_auto_reschedule_pending
    assert [m.subject for m in mailoutbox if oic.email in m.to] == [
        f"No free slot yet: {eq.name} – 1 booking still on hold"
    ]


def test_daily_check_retries_bookings_still_on_hold(lab, django_capture_on_commit_callbacks):
    from iic_booking.equipment import maintenance_auto

    eq, _oic, day, now = lab
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9,))

    with django_capture_on_commit_callbacks(execute=True):
        stats = maintenance_auto.daily_check(now=now)

    assert stats["rescheduled"] == 1 and stats["still_on_hold"] == 0
    assert _starts(b)[0] == _at(day, 9)


def test_equipment_still_under_maintenance_is_not_allocated(lab, django_capture_on_commit_callbacks):
    from iic_booking.equipment.models import Equipment

    eq, _oic, _day, now = lab
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9,))
    Equipment.objects.filter(pk=eq.pk).update(status=EquipmentStatus.REPAIR)

    res = _allocate(Equipment.objects.get(pk=eq.pk), now, django_capture_on_commit_callbacks)

    assert res.placed == [] and res.unplaced == []
    b.refresh_from_db()
    assert b.status == BookingStatus.DISRUPTION_PENDING


# --- marking Operational: placed before anyone else can book ------------------------------------------------------


def test_operational_places_holds_before_the_freed_slots_are_bookable(mailoutbox, django_capture_on_commit_callbacks):
    eq = _equipment()
    oic = _oic_for(eq)
    MaintenanceRescheduleSettings.objects.create(min_lead_hours=0)
    user = _user(UserType.STUDENT)
    tomorrow = timezone.localdate() + timedelta(days=1)
    while tomorrow.weekday() >= 5:
        tomorrow += timedelta(days=1)
    with django_capture_on_commit_callbacks(execute=True):
        assert _client(oic).patch(f"/api/equipments/{eq.pk}/", {"status": "REPAIR", "disruption_reason": "Pump"},
                                  format="json").status_code == 200
    first = _slots(eq, tomorrow)[0]
    DailySlot.objects.filter(pk=first.pk).update(status=SlotStatus.UNDER_MAINTENANCE)
    b = _held(eq, user, _past(2), (9,))

    with django_capture_on_commit_callbacks(execute=True):
        res = _client(oic).patch(f"/api/equipments/{eq.pk}/", {"status": "ACTIVE"}, format="json")

    assert res.status_code == 200, res.data
    first.refresh_from_db()
    assert first.booking_id == b.pk and first.status == SlotStatus.BOOKED
    b.refresh_from_db()
    assert b.status == BookingStatus.BOOKED and b.auto_rescheduled_at is not None
    assert any(m.subject.startswith(f"Booking {b.virtual_booking_id} rescheduled") for m in mailoutbox)
    assert not any("you may reschedule" in m.subject for m in mailoutbox)


# --- refund any time before the move; no 5-day auto refund --------------------------------------------------------


def test_user_can_cancel_for_a_full_refund_but_not_reschedule_while_on_hold(lab, django_capture_on_commit_callbacks):
    from unittest.mock import patch

    eq, _oic, day, now = lab
    user = _user(UserType.STUDENT)
    b = _held(eq, user, _past(2), (9,))
    client = _client(user)

    blocked = client.post(
        f"/api/bookings/{b.pk}/user-reschedule/",
        {"start_time": _at(day, 9).isoformat(), "end_time": _at(day, 10).isoformat()},
        format="json",
    )
    assert blocked.status_code == 400 and blocked.data.get("maintenance_auto_reschedule") is True

    result = {"released_slot_ids": [], "refund_transaction": None, "refund_amount": 0, "is_full_cancel": True}
    with patch("iic_booking.equipment.api_views.perform_booking_cancellation", return_value=result) as cancel:
        ok = client.post(f"/api/bookings/{b.pk}/user-cancel/", {}, format="json")
    assert ok.status_code == 200, ok.data
    assert cancel.call_args.kwargs["should_refund"] is True


def test_refunded_hold_is_not_rescheduled(lab, django_capture_on_commit_callbacks):
    eq, _oic, _day, now = lab
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9,))
    Booking.objects.filter(pk=b.pk).update(status=BookingStatus.REFUNDED, maintenance_auto_reschedule_pending=False)

    res = _allocate(eq, now, django_capture_on_commit_callbacks)

    assert res.placed == []


def test_maintenance_holds_are_never_auto_refunded(lab):
    from iic_booking.equipment.maintenance_policy import auto_cancel_dry_run, expired_disruption_bookings

    eq, _oic, _day, now = lab
    hold = _held(eq, _user(UserType.STUDENT), _past(2), (9,))
    legacy = _held(eq, _user(UserType.STUDENT), _past(3), (9,))
    absent = _book(eq, _slots(eq, _past(4))[0], _user(UserType.STUDENT))
    past_deadline = timezone.now() - timedelta(days=1)
    Booking.objects.filter(pk=hold.pk).update(maintenance_decision_deadline_at=past_deadline)
    Booking.objects.filter(pk=legacy.pk).update(
        maintenance_decision_deadline_at=past_deadline, maintenance_auto_reschedule_pending=False
    )
    Booking.objects.filter(pk=absent.pk).update(
        status=BookingStatus.DISRUPTION_PENDING, disruption_kind=BookingDisruptionKind.OPERATOR_ABSENT,
        maintenance_disruption_flag=True, maintenance_decision_deadline_at=past_deadline,
    )

    assert list(expired_disruption_bookings(timezone.now()).values_list("pk", flat=True)) == [absent.pk]
    assert [r["booking_id"] for r in auto_cancel_dry_run()["rows"]] == [absent.pk]


def test_hold_has_no_decision_deadline_and_no_operational_follow_up(lab, monkeypatch):
    from iic_booking.equipment import tasks

    calls = []
    monkeypatch.setattr(tasks.send_manual_disruption_operational_followup, "apply_async", lambda *a, **k: calls.append(a))
    eq, _oic, _day, _now = lab
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9,))

    b.refresh_from_db()
    assert b.maintenance_decision_deadline_at is None and b.maintenance_auto_reschedule_pending
    assert calls == []
    assert tasks.send_manual_disruption_operational_followup(b.pk) is False


# --- deadlines after the move -------------------------------------------------------------------------------------


def test_sample_deadline_is_extended_but_never_after_the_slot(lab, django_capture_on_commit_callbacks):
    from iic_booking.equipment.models import Equipment
    from iic_booking.equipment.sample_submission_deadline_reminders import compute_sample_submission_deadline

    eq, _oic, day, now = lab
    Equipment.objects.filter(pk=eq.pk).update(sample_submission_lead_hours=48)
    eq.refresh_from_db()
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9,))
    _allocate(eq, now, django_capture_on_commit_callbacks)
    b = Booking.objects.select_related("equipment").get(pk=b.pk)

    extended = compute_sample_submission_deadline(b)
    Booking.objects.filter(pk=b.pk).update(auto_rescheduled_at=None)
    normal = compute_sample_submission_deadline(Booking.objects.select_related("equipment").get(pk=b.pk))
    assert extended == min(normal + timedelta(hours=24), _at(day, 9))

    Equipment.objects.filter(pk=eq.pk).update(sample_submission_lead_hours=12)
    Booking.objects.filter(pk=b.pk).update(auto_rescheduled_at=now)
    capped = compute_sample_submission_deadline(Booking.objects.select_related("equipment").get(pk=b.pk))
    assert capped == _at(day, 9)
    payload = mr.auto_reschedule_payload(Booking.objects.select_related("equipment").get(pk=b.pk))
    assert payload["note"].startswith("Auto-rescheduled after maintenance — originally ")
    assert "sample deadline extended to" in payload["note"]


def test_not_utilized_threshold_is_extended(lab, django_capture_on_commit_callbacks):
    from iic_booking.equipment.booking_not_utilized_service import auto_not_utilized_hours, not_utilized_allowed_from
    from iic_booking.equipment.models import Equipment

    eq, _oic, day, now = lab
    Equipment.objects.filter(pk=eq.pk).update(booking_not_utilize_window_hours=24)
    eq.refresh_from_db()
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9,))
    _allocate(eq, now, django_capture_on_commit_callbacks)
    b = Booking.objects.select_related("equipment").get(pk=b.pk)

    assert not_utilized_allowed_from(b) == _at(day, 10) + timedelta(hours=48)
    assert auto_not_utilized_hours(b.equipment, b) == 48
    assert auto_not_utilized_hours(b.equipment) == 24


def test_full_refund_cancel_window_after_the_move(lab, django_capture_on_commit_callbacks):
    eq, _oic, day, now = lab
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9,))
    _allocate(eq, now, django_capture_on_commit_callbacks)
    b = Booking.objects.select_related("equipment").get(pk=b.pk)

    threshold = b.equipment.reschedule_hours_threshold or 48
    expected = min(max(_at(day, 9) - timedelta(hours=threshold), now + timedelta(hours=24)), _at(day, 9))
    assert mr.cancel_full_refund_until(b) == expected


# --- serializers --------------------------------------------------------------------------------------------------


def test_booking_and_slot_payloads_carry_the_note(lab, django_capture_on_commit_callbacks):
    from iic_booking.equipment.serializers import BookingListSerializer, BookingSerializer, DailySlotSerializer

    eq, _oic, day, now = lab
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9,))
    assert BookingSerializer(b).data["maintenance_auto_reschedule_pending"] is True
    _allocate(eq, now, django_capture_on_commit_callbacks)
    b = Booking.objects.get(pk=b.pk)

    detail = BookingSerializer(b).data
    row = BookingListSerializer(b).data
    slot = DailySlotSerializer(b.daily_slots.first()).data
    assert datetime.fromisoformat(detail["auto_reschedule"]["original_start"]) == _at(_past(2), 9)
    assert row["auto_reschedule"]["note"] == detail["auto_reschedule"]["note"]
    assert slot["booking_auto_reschedule_note"] == detail["auto_reschedule"]["note"]


# --- production helpers -------------------------------------------------------------------------------------------


def test_hold_policy_command_converts_only_holds_the_mechanism_made(lab):
    eq, _oic, _day, now = lab
    period = EquipmentMaintenancePeriod.objects.get(equipment=eq)
    mine = _held(eq, _user(UserType.STUDENT), _past(2), (9,))
    legacy = _held(eq, _user(UserType.STUDENT), _past(3), (9,))
    Booking.objects.filter(pk__in=[mine.pk, legacy.pk]).update(
        maintenance_auto_reschedule_pending=False, maintenance_decision_deadline_at=timezone.now() + timedelta(days=4)
    )
    MaintenanceNotification.objects.create(period=period, kind="DAY", booking=mine, dedupe_key=f"DAY:{mine.pk}")

    dry = StringIO()
    call_command("maintenance_auto", "--hold-policy", stdout=dry)
    assert f"to convert=1 ids=[{mine.pk}]" in dry.getvalue() and "Dry run only" in dry.getvalue()
    mine.refresh_from_db()
    assert not mine.maintenance_auto_reschedule_pending

    out = StringIO()
    call_command("maintenance_auto", "--hold-policy", "--apply", stdout=out)
    call_command("maintenance_auto", "--hold-policy", "--apply", stdout=StringIO())
    assert "converted=1" in out.getvalue()
    mine.refresh_from_db()
    legacy.refresh_from_db()
    assert mine.maintenance_auto_reschedule_pending and mine.maintenance_decision_deadline_at is None
    assert not legacy.maintenance_auto_reschedule_pending
    assert BookingEvent.objects.filter(booking=mine, metadata__hold_policy_update=True).count() == 1


def _legacy(booking):
    Booking.objects.filter(pk=booking.pk).update(
        maintenance_auto_reschedule_pending=False, maintenance_decision_deadline_at=timezone.now() + timedelta(days=2)
    )


def _hold_update_logs(booking):
    from iic_booking.communication.models import CommunicationLog

    return CommunicationLog.objects.filter(
        template__code="booking_equipment_maintenance_hold_update_email", recipient=booking.user
    )


def test_hold_update_under_maintenance_converts_and_emails_each_user_once(
    lab, mailoutbox, django_capture_on_commit_callbacks
):
    eq, _oic, _day, now = lab
    legacy = _held(eq, _user(UserType.STUDENT), _past(3), (9,))
    _legacy(legacy)
    already = _held(eq, _user(UserType.STUDENT), _past(2), (9,))
    type(eq).objects.filter(pk=eq.pk).update(status=EquipmentStatus.REPAIR)

    rows = {r["booking_id"]: r for r in mr.hold_update_review([legacy.pk, already.pk], now)}
    assert rows[legacy.pk]["action"] == "convert_notify" and rows[already.pk]["action"] == "notify"
    assert not mailoutbox

    with django_capture_on_commit_callbacks(execute=True):
        out = mr.apply_hold_update([legacy.pk, already.pk], now)
    with django_capture_on_commit_callbacks(execute=True):
        again = mr.apply_hold_update([legacy.pk, already.pk], now)

    assert out["converted"] == [legacy.pk] and sorted(out["notified"]) == sorted([legacy.pk, already.pk])
    assert not out["rescheduled"] and not again["converted"] and not again["notified"]
    assert set(again["skipped"]) == {legacy.pk, already.pk}
    legacy.refresh_from_db()
    assert legacy.maintenance_auto_reschedule_pending and legacy.maintenance_decision_deadline_at is None
    assert legacy.status == BookingStatus.DISRUPTION_PENDING
    for b in (legacy, already):
        assert _hold_update_logs(b).count() == 1
        assert BookingEvent.objects.filter(booking=b, metadata__hold_update_notice=True).count() == 1
    subjects = [m.subject for m in mailoutbox]
    assert len(subjects) == 2 and all(s.startswith("Update: booking") for s in subjects)
    assert "no deadline" in mailoutbox[0].body and "full refund" in mailoutbox[0].body


def test_hold_update_on_operational_equipment_reschedules_right_away(
    lab, mailoutbox, django_capture_on_commit_callbacks
):
    eq, _oic, day, now = lab
    _slots(eq, day)
    legacy = _held(eq, _user(UserType.STUDENT), _past(3), (9,))
    _legacy(legacy)

    row = mr.hold_update_review([legacy.pk], now)[0]
    assert row["action"] == "convert_reschedule"
    assert datetime.fromisoformat(row["new_start"]) == _at(day, 9)

    with django_capture_on_commit_callbacks(execute=True):
        out = mr.apply_hold_update([legacy.pk], now)

    assert out["converted"] == [legacy.pk] and not out["notified"] and not out["still_on_hold"]
    assert [r["booking_id"] for r in out["rescheduled"]] == [legacy.pk]
    assert _starts(legacy) == [_at(day, 9)]
    legacy.refresh_from_db()
    assert legacy.status == BookingStatus.BOOKED and legacy.auto_rescheduled_at == now
    assert not _hold_update_logs(legacy).exists()
    assert any("rescheduled" in m.subject for m in mailoutbox)


def test_hold_update_skips_bookings_that_are_not_waiting_maintenance_holds(lab, mailoutbox):
    eq, _oic, _day, now = lab
    chosen = _held(eq, _user(UserType.STUDENT), _past(3), (9,))
    Booking.objects.filter(pk=chosen.pk).update(status=BookingStatus.REFUNDED)
    absent = _held(eq, _user(UserType.STUDENT), _past(2), (9,))
    Booking.objects.filter(pk=absent.pk).update(disruption_kind=BookingDisruptionKind.OPERATOR_ABSENT)

    out = mr.apply_hold_update([chosen.pk, absent.pk, 999999], now)

    assert set(out["skipped"]) == {chosen.pk, absent.pk, 999999}
    assert "already chose" in out["skipped"][chosen.pk] and "not equipment maintenance" in out["skipped"][absent.pk]
    assert not out["converted"] and not mailoutbox


def test_hold_update_command_dry_run_changes_nothing(lab, mailoutbox):
    eq, _oic, _day, _now = lab
    legacy = _held(eq, _user(UserType.STUDENT), _past(3), (9,))
    _legacy(legacy)
    type(eq).objects.filter(pk=eq.pk).update(status=EquipmentStatus.REPAIR)

    out = StringIO()
    call_command("maintenance_auto", "--hold-update", str(legacy.pk), stdout=out)

    assert "action=convert_notify" in out.getvalue() and "Dry run only" in out.getvalue()
    legacy.refresh_from_db()
    assert not legacy.maintenance_auto_reschedule_pending and not mailoutbox


def test_enable_refund_job_registers_a_single_row(lab):
    from django_celery_beat.models import PeriodicTask

    call_command("maintenance_auto", "--enable-refund-job", stdout=StringIO())
    assert not PeriodicTask.objects.filter(task="equipment.process_maintenance_booking_deadlines").exists()
    call_command("maintenance_auto", "--enable-refund-job", "--apply", stdout=StringIO())
    row = PeriodicTask.objects.get(task="equipment.process_maintenance_booking_deadlines")
    PeriodicTask.objects.create(name="dupe", task=row.task, crontab=row.crontab)
    call_command("maintenance_auto", "--enable-refund-job", "--apply", stdout=StringIO())

    row = PeriodicTask.objects.get(task="equipment.process_maintenance_booking_deadlines")
    assert row.enabled and row.crontab.minute == "*/15"


def test_reschedule_preview_is_read_only(lab):
    eq, _oic, _day, _now = lab
    b = _held(eq, _user(UserType.STUDENT), _past(2), (9,))

    out = StringIO()
    call_command("maintenance_auto", "--reschedule-preview", stdout=out)

    assert f"booking={b.pk}" in out.getvalue()
    b.refresh_from_db()
    assert b.status == BookingStatus.DISRUPTION_PENDING
