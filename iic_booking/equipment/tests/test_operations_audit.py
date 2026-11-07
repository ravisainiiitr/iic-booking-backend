"""The Main Administrator's operations audit: waitlist, slot exchange, urgent requests and refunds, with who did it."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment import operations_audit as audit
from iic_booking.equipment.models import BookingEventType, UrgentBookingRequest, UrgentBookingRequestStatus, UrgentBookingRequestType, WaitlistEntry
from iic_booking.equipment.operations_audit_models import OperationsAuditEvent, OperationsAuditImmutable
from iic_booking.equipment.booking_events import create_booking_event
from iic_booking.equipment.waitlist import opt_out_of_waitlist
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db
URL = "/api/admin/operations-audit/"


def _api(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


@pytest.fixture
def admin():
    return UserFactory(user_type=UserType.ADMIN)


@pytest.fixture
def equipment_of(egs_factory):
    return lambda **fields: egs_factory.equipment(**fields)


@pytest.fixture
def booking_of(egs_factory):
    def make(user=None):
        return egs_factory.booking(user or UserFactory(), egs_factory.equipment(), egs_factory.future())
    return make


@pytest.fixture
def oic():
    return UserFactory(user_type=UserType.MANAGER, name="Officer One")


def _rows(admin, **params):
    answer = _api(admin).get(URL, params)
    assert answer.status_code == 200
    return answer.json()["results"]


@pytest.mark.parametrize("user_type", [UserType.MANAGER, UserType.DEPT_ADMIN, UserType.FINANCE, UserType.FACULTY])
def test_only_the_main_administrator_can_read_it(user_type):
    assert _api(UserFactory(user_type=user_type)).get(URL).status_code == 403


def test_rows_cannot_be_changed_or_deleted():
    row = OperationsAuditEvent.objects.create(action="waitlist_auto_cleared")
    with pytest.raises(OperationsAuditImmutable):
        row.save()
    with pytest.raises(OperationsAuditImmutable):
        row.delete()
    with pytest.raises(OperationsAuditImmutable):
        OperationsAuditEvent.objects.all().delete()
    with pytest.raises(OperationsAuditImmutable):
        OperationsAuditEvent.objects.all().update(detail="x")


def test_an_audit_failure_does_not_stop_the_action():
    audit.record("not-a-problem", equipment=object(), actor=object())  # bad values: logged, not raised


def test_staff_clear_records_who_and_each_person(admin, oic, equipment_of):
    equipment = equipment_of(name="Test XRD")
    waiting = UserFactory(name="Waiting User")
    WaitlistEntry.objects.create(user=waiting, equipment=equipment, status="ACTIVE")
    audit.record_waitlist_entries(WaitlistEntry.objects.filter(equipment=equipment).select_related("equipment", "user"), "waitlist_staff_cleared", actor=oic)
    row = _rows(admin, category="waitlist")[0]
    assert (row["action"], row["actor_name"], row["actor_role"], row["equipment"], row["affected_user"]) == (
        "Waitlist cleared by staff", "Officer One", "Officer In Charge", "Test XRD", "Waiting User")


def test_automatic_clear_says_system(admin, equipment_of):
    from iic_booking.equipment import waitlist

    equipment = equipment_of(name="Test SEM", waitlist_queue_depth=5, slot_window_reference_weekday=2, slot_window_reference_time=timezone.localtime().time())
    WaitlistEntry.objects.create(user=UserFactory(name="Queue User"), equipment=equipment, status="ACTIVE")
    from unittest.mock import patch

    with patch.object(waitlist, "waitlist_closed_window", return_value=True), patch.object(waitlist, "notify_waitlist_slots_available"):
        assert waitlist.clear_waitlist_due_before_reference() == 1
    assert not WaitlistEntry.objects.filter(equipment=equipment).exists()
    row = _rows(admin, category="waitlist")[0]
    assert (row["action"], row["actor_name"], row["actor_role"], row["affected_user"]) == ("Waitlist cleared automatically", "System", audit.SYSTEM_ROLE, "Queue User")


def test_user_leaving_the_waitlist_is_recorded(admin, equipment_of):
    person = UserFactory(name="Leaving User")
    entry = WaitlistEntry.objects.create(user=person, equipment=equipment_of(), status="ACTIVE")
    opt_out_of_waitlist(entry)
    row = _rows(admin, category="waitlist")[0]
    assert (row["action"], row["actor_name"], row["actor_role"]) == ("User left the waitlist", "Leaving User", "User")


def test_urgent_type_a_and_b_with_the_decider(admin, oic, equipment_of):
    equipment = equipment_of()
    now = timezone.now()
    UrgentBookingRequest.objects.create(user=UserFactory(), equipment=equipment, request_type=UrgentBookingRequestType.NO_SLOT, status=UrgentBookingRequestStatus.APPROVED, decided_by=oic, decided_at=now)
    UrgentBookingRequest.objects.create(user=UserFactory(), equipment=equipment, request_type=UrgentBookingRequestType.REVIEWER_URGENT, status=UrgentBookingRequestStatus.REJECTED, decided_by=oic, decided_at=now)
    actions = {r["action"]: r for r in _rows(admin, category="urgent")}
    assert "Type A: requested" in actions and "Type B: requested" in actions
    assert actions["Type A: approved"]["actor_name"] == "Officer One" and actions["Type A: approved"]["actor_role"] == "Officer In Charge"
    assert actions["Type B: rejected"]["actor_role"] == "Officer In Charge"


def test_refund_shows_who_issued_it_and_the_amount(admin, oic, booking_of):
    booking = booking_of()
    create_booking_event(booking=booking, event_type=BookingEventType.REFUNDED, created_by=oic, comment="Instrument fault", metadata={"refund_amount": "1500.00"}, send_notification=False)
    row = _rows(admin, category="refund")[0]
    assert row["action"] == "Refund to wallet" and row["actor_name"] == "Officer One" and Decimal(row["amount"]) == Decimal("1500.00")


def test_automatic_refund_says_system(admin, booking_of):
    create_booking_event(booking=booking_of(), event_type=BookingEventType.REFUNDED, created_by=None, system_actor=True, comment="Auto", metadata={"refund_amount": "10"}, send_notification=False)
    assert _rows(admin, category="refund")[0]["actor_role"] == audit.SYSTEM_ROLE


def test_slot_exchange_shows_both_people(admin, booking_of):
    from iic_booking.equipment.slot_exchange_models import SlotExchangeRequest, SlotExchangeStatus

    first, second = UserFactory(name="First Person"), UserFactory(name="Second Person")
    SlotExchangeRequest.objects.create(requester=first, responder=second, requester_booking=booking_of(first), responder_booking=booking_of(second), status=SlotExchangeStatus.ACCEPTED,
                                       expires_at=timezone.now() + timedelta(days=1), responded_at=timezone.now())
    rows = {r["action"]: r for r in _rows(admin, category="slot_exchange")}
    assert rows["Requested"]["actor_name"] == "First Person" and rows["Requested"]["affected_user"] == "Second Person"
    assert rows["Exchanged"]["actor_name"] == "Second Person"


def test_filters_search_paging_and_export(admin, oic, equipment_of):
    equipment = equipment_of(name="Findable Instrument")
    audit.record("waitlist_staff_cleared", equipment=equipment, actor=oic, subject=UserFactory(name="=Formula"))
    audit.record("waitlist_auto_cleared", equipment=equipment_of(name="Other"))
    assert len(_rows(admin, q="findable")) == 1
    long_ago = (timezone.localdate() - timedelta(days=400)).isoformat()
    assert _rows(admin, date_from=long_ago, date_to=long_ago) == []
    answer = _api(admin).get(URL, {"page_size": 1})
    assert answer.json()["count"] == 2 and answer.json()["num_pages"] == 2
    exported = _api(admin).get(URL, {"export": "csv"})
    assert exported["Content-Type"].startswith("text/csv")
    body = exported.content.decode("utf8")
    assert "Findable Instrument" in body and "'=Formula" in body


def test_csv_is_refused_for_others():
    assert _api(UserFactory(user_type=UserType.MANAGER)).get(URL, {"export": "csv"}).status_code == 403
