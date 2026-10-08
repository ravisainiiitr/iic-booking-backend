"""Type B urgent requests without slots: amount on submission, OIC Approve & allocate on any day."""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment import api_views
from iic_booking.equipment.booking_events import send_booking_event_notification
from iic_booking.equipment.models import (
    BookingEvent,
    BookingStatus,
    ChargeProfile,
    DailySlot,
    Equipment,
    EquipmentManager,
    Holiday,
    SlotMaster,
    SlotStatus,
    UrgentBookingRequest,
    UrgentBookingRequestStatus,
    UrgentBookingRequestType,
)
from iic_booking.users.models.department import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus
from iic_booking.users.repositories.wallet_repository import WalletRepository
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

REASON = "Reviewer asked for revised spectra within a week."


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(api_views, "notify_waitlist_slots_available", lambda *a, **k: 0)
    monkeypatch.setattr(
        "iic_booking.communication.styled_transactional_emails._send", lambda *a, **k: None
    )
    sent = []
    monkeypatch.setattr(
        api_views.CommunicationService, "send_email", lambda *a, **k: sent.append(k.get("template"))
    )
    return sent


@pytest.fixture
def unlocked():
    with patch(
        "iic_booking.users.legacy_ledger.booking_lock.booking_is_locked", return_value=(False, "")
    ), patch(
        "iic_booking.users.legacy_ledger.booking_lock.department_equipment_booking_blocked",
        return_value=(False, ""),
    ), patch("iic_booking.users.rbac.user_has_permission", return_value=True):
        yield


def _client(user) -> APIClient:
    c = APIClient()
    c.force_authenticate(user=user)
    return c


def _dept():
    tag = uuid.uuid4().hex[:6].upper()
    return Department.objects.create(
        name=f"UTB-{tag}", code=f"UB{tag[:4]}", department_type="internal",
        equipment_booking_enabled=True, equipment_visibility_enabled=True,
    )


def _setup(*, balance="1000.00", supervisor_approved=True):
    dept = _dept()
    eq = Equipment.objects.create(
        name="Type B EQ", code=f"TB{uuid.uuid4().hex[:5].upper()}", slot_duration_minutes=60,
        user_rating_enabled=False, status="ACTIVE", internal_department=dept,
    )
    ChargeProfile.objects.create(
        equipment=eq, user_type=UserType.STUDENT, profile_type="SAMPLE", time_formula="A*45",
        primary_unit_charge=Decimal("100.00"),
    )
    faculty = UserFactory(user_type=UserType.FACULTY, department=dept, admin_approved=True, name="Prof Supervisor")
    wallet, _ = Wallet.objects.get_or_create(user=faculty)
    student = UserFactory(user_type=UserType.STUDENT, department=dept, admin_approved=True, name="Stu Dent")
    WalletJoinRequest.objects.create(
        student=student, faculty=faculty, wallet=wallet, status=WalletJoinRequestStatus.APPROVED
    )
    target, _ = WalletRepository.get_booking_wallet_target(student, dept)
    target.balance = Decimal(balance)
    target.save(update_fields=["balance"])
    oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    return eq, student, faculty, oic, target


def _slots(eq, day, hours, status=SlotStatus.AVAILABLE):
    out = []
    for i, h in enumerate(hours):
        start = timezone.make_aware(datetime.combine(day, time(h, 0)))
        master, _ = SlotMaster.objects.get_or_create(
            equipment=eq, slot_number=h,
            defaults={"open_time": time(h, 0), "close_time": time(h + 1, 0), "is_active": True},
        )
        out.append(DailySlot.objects.create(
            slot_master=master, date=day, start_datetime=start, end_datetime=start + timedelta(hours=1),
            status=status,
        ))
    return out


def _next_saturday():
    today = timezone.localdate()
    return today + timedelta(days=(5 - today.weekday()) % 7 or 7)


def _submit(student, eq, **extra):
    body = {
        "equipment_id": eq.pk,
        "request_type": "REVIEWER_URGENT",
        "disclaimer_accepted": True,
        "reviewer_comment": REASON,
        "input_values": {"A": 2},
        "preferred_schedule": "Any morning next week",
        **extra,
    }
    return _client(student).post("/api/urgent-booking-requests/create/", body, format="json")


def _pending_request(eq, student, faculty, **extra):
    res = _submit(student, eq, **extra)
    assert res.status_code == 201, res.content
    urg = UrgentBookingRequest.objects.get(pk=res.json()["id"])
    urg.supervisor_decision = "APPROVED"
    urg.save(update_fields=["supervisor_decision"])
    return urg


# ---------------------------------------------------------------- submission


def test_submit_without_slots_stores_inputs_required_time_and_amount(unlocked, _quiet):
    eq, student, faculty, _oic, _target = _setup()
    res = _submit(student, eq)
    assert res.status_code == 201, res.content
    body = res.json()
    assert body["requires_slot_allocation"] is True
    urg = UrgentBookingRequest.objects.get(pk=body["id"])
    assert urg.request_type == UrgentBookingRequestType.REVIEWER_URGENT
    assert urg.hold_booking_id is None
    assert urg.input_values == {"A": 2}
    assert urg.preferred_schedule == "Any morning next week"
    assert urg.supervisor_id == faculty.id and urg.pending_supervisor_approval

    from iic_booking.equipment.urgent_allocation import quote_urgent_charge

    expected = quote_urgent_charge(eq, student, {"A": 2})
    assert urg.duration_minutes == expected["required_minutes"] > 0
    assert urg.estimated_charge == expected["total_charge"]
    assert expected["urgent_surcharge_amount"] > 0
    assert any("urgent booking surcharge" in str(line["description"]).lower() for line in urg.estimated_charge_breakdown)
    assert Decimal(body["estimated_charge"]) == expected["total_charge"]
    assert body["required_minutes"] == urg.duration_minutes
    # The amount is shown to the user; nothing is debited on submission.
    assert _target.__class__.objects.get(pk=_target.pk).balance == Decimal("1000.00")
    assert "urgent_booking_request_submitted_user_email" in _quiet


def test_submit_without_inputs_or_slots_is_refused(unlocked):
    eq, student, _f, _o, _t = _setup()
    res = _submit(student, eq, input_values={})
    assert res.status_code == 400
    assert res.json()["code"] == "URGENT_INPUTS_REQUIRED"
    assert not UrgentBookingRequest.objects.exists()


def test_submit_accepts_input_values_as_json_text(unlocked):
    eq, student, _f, _o, _t = _setup()
    res = _submit(student, eq, input_values='{"A": "3"}')
    assert res.status_code == 201, res.content
    assert UrgentBookingRequest.objects.get(pk=res.json()["id"]).input_values == {"A": 3}


def test_oic_list_shows_requirement(unlocked):
    eq, student, faculty, oic, _t = _setup()
    urg = _pending_request(eq, student, faculty)
    rows = _client(oic).get("/api/urgent-booking-requests/").json()["urgent_requests"]
    row = next(r for r in rows if r["id"] == urg.id)
    assert row["requires_slot_allocation"] is True
    req = row["requirement"]
    assert req["required_minutes"] == urg.duration_minutes
    assert req["estimated_charge"] == f"{urg.estimated_charge:.2f}"
    assert req["preferred_schedule"] == "Any morning next week"
    assert req["input_values_by_key"] == {"A": 2}


# ---------------------------------------------------------------- allocation


def test_plain_accept_is_refused_for_request_without_slots(unlocked):
    eq, student, faculty, oic, _t = _setup()
    urg = _pending_request(eq, student, faculty)
    res = _client(oic).patch(f"/api/urgent-booking-requests/{urg.id}/", {"status": "APPROVED"}, format="json")
    assert res.status_code == 400
    assert res.json()["code"] == "SLOT_ALLOCATION_REQUIRED"


def test_allocation_slots_list_any_day_with_weekend_and_holiday_notes(unlocked):
    eq, student, faculty, oic, _t = _setup()
    urg = _pending_request(eq, student, faculty)
    saturday = _next_saturday()
    Holiday.objects.create(date=saturday, reason="Festival")
    _slots(eq, saturday, [9, 10], status=SlotStatus.NOT_AVAILABLE)
    res = _client(oic).get(f"/api/urgent-booking-requests/{urg.id}/allocation-slots/?date={saturday:%Y-%m-%d}")
    assert res.status_code == 200, res.content
    body = res.json()
    assert body["is_weekend"] is True and body["holiday"] == "Festival"
    assert body["required_minutes"] == urg.duration_minutes
    assert len(body["slots"]) >= 2 and all(s["selectable"] for s in body["slots"])
    notes = body["slots"][0]["notes"]
    assert "Weekend" in notes and any(n.startswith("Holiday") for n in notes)


def test_quote_rechecks_amount_and_wallet(unlocked):
    eq, student, faculty, oic, _t = _setup()
    urg = _pending_request(eq, student, faculty)
    hours = list(range(9, 9 + max(1, urg.slots_requested)))
    slots = _slots(eq, _next_saturday(), hours, status=SlotStatus.UNDER_MAINTENANCE)
    res = _client(oic).post(
        f"/api/urgent-booking-requests/{urg.id}/allocation-quote/", {"slot_ids": [s.id for s in slots]}, format="json"
    )
    assert res.status_code == 200, res.content
    q = res.json()
    assert q["covers_required_time"] is True
    assert q["total_charge"] == f"{urg.estimated_charge:.2f}"
    assert q["amount_changed"] is False
    assert q["wallet"]["sufficient"] is True and q["can_allocate"] is True
    assert any("Weekend" in w and "Under Maintenance" in w for w in q["warnings"])


def test_allocate_any_day_books_debits_and_audits(unlocked, django_capture_on_commit_callbacks):
    eq, student, faculty, oic, target = _setup()
    urg = _pending_request(eq, student, faculty)
    hours = list(range(9, 9 + max(1, urg.slots_requested)))
    slots = _slots(eq, _next_saturday(), hours, status=SlotStatus.NOT_AVAILABLE)
    with patch.object(api_views, "create_booking_event", wraps=api_views.create_booking_event) as ev, patch(
        "iic_booking.equipment.booking_events._dispatch_booking_event_notification"
    ):
        res = _client(oic).post(
            f"/api/urgent-booking-requests/{urg.id}/allocate/",
            {"slot_ids": [s.id for s in slots], "expected_total": f"{urg.estimated_charge:.2f}", "admin_notes": "Saturday run"},
            format="json",
        )
    assert res.status_code == 200, res.content
    urg.refresh_from_db()
    assert urg.status == UrgentBookingRequestStatus.APPROVED
    assert urg.decided_by_id == oic.id and urg.decided_at is not None
    assert urg.admin_notes == "Saturday run"
    booking = urg.hold_booking
    assert booking.status == BookingStatus.BOOKED
    assert booking.total_charge == urg.estimated_charge
    assert booking.input_values == {"A": 2}
    assert booking.virtual_booking_id
    assert sorted(booking.daily_slots.values_list("id", flat=True)) == sorted(s.id for s in slots)
    assert set(DailySlot.objects.filter(id__in=[s.id for s in slots]).values_list("status", flat=True)) == {SlotStatus.BOOKED}
    target.refresh_from_db()
    assert target.balance == Decimal("1000.00") - urg.estimated_charge
    created = BookingEvent.objects.get(booking=booking, event_type="CREATED")
    assert created.metadata["urgent_slot_allocation"] is True
    assert created.metadata["allocated_by_id"] == oic.id
    assert sorted(created.metadata["slot_ids"]) == sorted(s.id for s in slots)
    converted = BookingEvent.objects.get(booking=booking, event_type="STATUS_CHANGED")
    assert converted.metadata.get("urgent_hold_converted") is True
    assert converted.previous_status == BookingStatus.HOLD and converted.new_status == BookingStatus.BOOKED
    assert any(c.kwargs.get("send_notification") for c in ev.call_args_list)


def test_allocation_confirmation_emails_user_and_supervisor(unlocked):
    eq, student, faculty, oic, _t = _setup()
    urg = _pending_request(eq, student, faculty)
    slots = _slots(eq, _next_saturday(), list(range(9, 9 + max(1, urg.slots_requested))))
    with patch("iic_booking.equipment.booking_events._dispatch_booking_event_notification"):
        res = _client(oic).post(
            f"/api/urgent-booking-requests/{urg.id}/allocate/", {"slot_ids": [s.id for s in slots]}, format="json"
        )
    assert res.status_code == 200, res.content
    urg.refresh_from_db()
    event = BookingEvent.objects.get(booking=urg.hold_booking, event_type="STATUS_CHANGED")

    from iic_booking.communication.models import CommunicationLog

    sent = []

    class _Log:
        status = CommunicationLog.CommunicationStatus.SENT
        error_message = ""

    def _send(recipient=None, template=None, **kwargs):
        sent.append((recipient.email, template))
        return _Log()

    with patch("iic_booking.equipment.booking_events.CommunicationService.send_email", side_effect=_send), patch(
        "iic_booking.equipment.booking_events.CommunicationService.send_push_notification", return_value=None
    ):
        send_booking_event_notification(event)
    recipients = {email for email, template in sent if template == "urgent_booking_hold_confirmed_email"}
    assert student.email in recipients and faculty.email in recipients


def test_insufficient_balance_blocks_and_shows_shortfall(unlocked):
    eq, student, faculty, oic, target = _setup(balance="10.00")
    urg = _pending_request(eq, student, faculty)
    slots = _slots(eq, _next_saturday(), list(range(9, 9 + max(1, urg.slots_requested))))
    ids = [s.id for s in slots]
    q = _client(oic).post(f"/api/urgent-booking-requests/{urg.id}/allocation-quote/", {"slot_ids": ids}, format="json").json()
    assert q["wallet"]["sufficient"] is False and q["can_allocate"] is False
    assert Decimal(q["wallet"]["shortfall"]) == urg.estimated_charge - Decimal("10.00")

    res = _client(oic).post(f"/api/urgent-booking-requests/{urg.id}/allocate/", {"slot_ids": ids}, format="json")
    assert res.status_code == 400
    assert res.json()["code"] == "INSUFFICIENT_BALANCE"
    urg.refresh_from_db()
    target.refresh_from_db()
    assert urg.status == UrgentBookingRequestStatus.PENDING and urg.hold_booking_id is None
    assert target.balance == Decimal("10.00")
    assert not DailySlot.objects.filter(id__in=ids, booking__isnull=False).exists()


def test_allocate_refuses_stale_amount_short_slots_and_taken_slots(unlocked):
    eq, student, faculty, oic, _t = _setup()
    urg = _pending_request(eq, student, faculty)
    c = _client(oic)
    url = f"/api/urgent-booking-requests/{urg.id}/allocate/"
    slots = _slots(eq, _next_saturday(), list(range(9, 9 + max(1, urg.slots_requested))))
    ids = [s.id for s in slots]

    stale = c.post(url, {"slot_ids": ids, "expected_total": "1.00"}, format="json")
    assert stale.status_code == 400 and stale.json()["code"] == "AMOUNT_CHANGED"
    assert stale.json()["quote"]["total_charge"] == f"{urg.estimated_charge:.2f}"

    if urg.duration_minutes > 60:
        short = c.post(url, {"slot_ids": ids[:1]}, format="json")
        assert short.status_code == 400 and short.json()["code"] == "SLOTS_TOO_SHORT"

    DailySlot.objects.filter(id=ids[0]).update(status=SlotStatus.BOOKED)
    taken = c.post(url, {"slot_ids": ids}, format="json")
    assert taken.status_code == 400 and taken.json()["code"] == "SLOTS_TAKEN"
    urg.refresh_from_db()
    assert urg.status == UrgentBookingRequestStatus.PENDING


def test_allocate_waits_for_supervisor_and_respects_weekly_cap(unlocked):
    eq, student, faculty, oic, _t = _setup()
    res = _submit(student, eq)
    urg = UrgentBookingRequest.objects.get(pk=res.json()["id"])
    slots = _slots(eq, _next_saturday(), list(range(9, 9 + max(1, urg.slots_requested))))
    ids = [s.id for s in slots]
    c = _client(oic)
    pending = c.post(f"/api/urgent-booking-requests/{urg.id}/allocate/", {"slot_ids": ids}, format="json")
    assert pending.status_code == 400 and pending.json()["code"] == "SUPERVISOR_APPROVAL_PENDING"

    urg.supervisor_decision = "APPROVED"
    urg.save(update_fields=["supervisor_decision"])
    eq.max_surcharge_urgent_requests_per_week = 1
    eq.save(update_fields=["max_surcharge_urgent_requests_per_week"])
    other = UserFactory(user_type=UserType.STUDENT, admin_approved=True)
    UrgentBookingRequest.objects.create(
        user=other, equipment=eq, request_type=UrgentBookingRequestType.REVIEWER_URGENT,
        status=UrgentBookingRequestStatus.APPROVED, decided_at=timezone.now(),
    )
    capped = c.post(f"/api/urgent-booking-requests/{urg.id}/allocate/", {"slot_ids": ids}, format="json")
    assert capped.status_code == 400 and capped.json()["code"] == "URGENT_WEEKLY_CAP_REACHED"


def test_reject_request_without_slots_notifies_and_books_nothing(unlocked, _quiet):
    eq, student, faculty, oic, target = _setup()
    urg = _pending_request(eq, student, faculty)
    _quiet.clear()
    res = _client(oic).patch(
        f"/api/urgent-booking-requests/{urg.id}/", {"status": "REJECTED", "admin_notes": "No operator"}, format="json"
    )
    assert res.status_code == 200, res.content
    urg.refresh_from_db()
    target.refresh_from_db()
    assert urg.status == UrgentBookingRequestStatus.REJECTED and urg.hold_booking_id is None
    assert target.balance == Decimal("1000.00")
    assert "urgent_booking_admin_decision_user_email" in _quiet


def test_operator_outside_equipment_cannot_allocate(unlocked):
    eq, student, faculty, _oic, _t = _setup()
    urg = _pending_request(eq, student, faculty)
    stranger = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    res = _client(stranger).post(f"/api/urgent-booking-requests/{urg.id}/allocate/", {"slot_ids": [1]}, format="json")
    assert res.status_code == 403
    assert _client(student).post(
        f"/api/urgent-booking-requests/{urg.id}/allocation-quote/", {"slot_ids": [1]}, format="json"
    ).status_code == 403


def test_existing_type_b_with_held_slots_still_accepts(unlocked):
    eq, student, faculty, oic, target = _setup()
    hold_slots = _slots(eq, timezone.localdate() + timedelta(days=3), [11])
    from iic_booking.equipment.models import Booking

    hold = Booking.objects.create(
        user=student, equipment=eq, charge_profile=ChargeProfile.objects.get(equipment=eq),
        status=BookingStatus.HOLD, total_charge=Decimal("150.00"), total_time_minutes=60,
        user_type_snapshot=UserType.STUDENT,
    )
    DailySlot.objects.filter(id=hold_slots[0].id).update(booking=hold, status=SlotStatus.BOOKED)
    urg = UrgentBookingRequest.objects.create(
        user=student, equipment=eq, request_type=UrgentBookingRequestType.REVIEWER_URGENT,
        disclaimer_accepted=True, reviewer_comment=REASON, supervisor=faculty,
        supervisor_approval_required=True, supervisor_decision="APPROVED", hold_booking=hold,
    )
    with patch("iic_booking.equipment.booking_events._dispatch_booking_event_notification"):
        res = _client(oic).patch(f"/api/urgent-booking-requests/{urg.id}/", {"status": "APPROVED"}, format="json")
    assert res.status_code == 200, res.content
    hold.refresh_from_db()
    target.refresh_from_db()
    assert hold.status == BookingStatus.BOOKED
    assert target.balance == Decimal("850.00")
