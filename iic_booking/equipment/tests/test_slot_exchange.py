"""Slot Exchange between internal users: rules, the request / accept / decline / withdraw flow, and the API."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.equipment import slot_exchange as sx
from iic_booking.equipment.models import BookingSampleTrace, BookingStatus, DailySlot, SampleTraceStatus
from iic_booking.equipment.slot_exchange_models import SlotExchangeRequest, SlotExchangeStatus
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(sx, "notify", lambda req, kind: calls.append((req.pk, kind)))
    return calls


@pytest.fixture
def lab(egs_factory):
    eq = egs_factory.equipment(slot_exchange_enabled=True)
    alice, bob = egs_factory.student(), egs_factory.student()
    # Alice: two ₹100 slots on day 3 at 10:00 and 11:00. Bob: one ₹100 slot on day 4 at 10:00.
    a = egs_factory.booking(alice, eq, egs_factory.future(days=3, hour=10), total_charge="200.00", slot_count=2)
    b = egs_factory.booking(bob, eq, egs_factory.future(days=4, hour=10), total_charge="100.00", slot_count=1)
    return egs_factory, eq, alice, bob, a, b


def _slots(booking):
    return list(booking.daily_slots.order_by("start_datetime"))


def test_partial_exchange_of_one_slot_for_one_of_the_same_charge(lab, sent, django_capture_on_commit_callbacks):
    factory, eq, alice, bob, a, b = lab
    a1, a2 = _slots(a)
    (b1,) = _slots(b)
    with django_capture_on_commit_callbacks(execute=True):
        req = sx.create_request(bob, [b1.pk], [a2.pk], "Can we swap?")
    assert req.status == SlotExchangeStatus.PENDING and req.responder == alice
    assert sent == [(req.pk, "requested")]
    with django_capture_on_commit_callbacks(execute=True):
        sx.accept_request(req.pk, alice)
    assert sent[-1] == (req.pk, "accepted")

    assert {s.pk for s in _slots(a)} == {a1.pk, b1.pk}
    assert {s.pk for s in _slots(b)} == {a2.pk}
    a.refresh_from_db()
    b.refresh_from_db()
    assert str(a.total_charge) == "200.00" and str(b.total_charge) == "100.00"  # no money moves
    assert SlotExchangeRequest.objects.get(pk=req.pk).status == SlotExchangeStatus.ACCEPTED
    assert a.events.filter(metadata__slot_exchange_id=req.pk).exists()


def test_charge_must_match(lab, sent):
    factory, eq, alice, bob, a, b = lab
    b.total_charge = "150.00"
    b.save(update_fields=["total_charge"])
    with pytest.raises(sx.ExchangeError) as e:
        sx.create_request(bob, [_slots(b)[0].pk], [_slots(a)[0].pk])
    assert e.value.code == "CHARGE_MISMATCH"


def test_counts_must_match(lab, sent):
    factory, eq, alice, bob, a, b = lab
    with pytest.raises(sx.ExchangeError) as e:
        sx.create_request(bob, [_slots(b)[0].pk], [s.pk for s in _slots(a)])
    assert e.value.code == "COUNT_MISMATCH"


def test_equipment_must_have_exchange_enabled(lab, sent):
    factory, eq, alice, bob, a, b = lab
    eq.slot_exchange_enabled = False
    eq.save(update_fields=["slot_exchange_enabled"])
    with pytest.raises(sx.ExchangeError) as e:
        sx.create_request(bob, [_slots(b)[0].pk], [_slots(a)[0].pk])
    assert e.value.code == "EXCHANGE_DISABLED"


def test_external_users_cannot_exchange(lab, sent):
    factory, eq, alice, bob, a, b = lab
    outsider = UserFactory(user_type=UserType.EXTERNAL, admin_approved=True)
    with pytest.raises(sx.ExchangeError) as e:
        sx.create_request(outsider, [_slots(b)[0].pk], [_slots(a)[0].pk])
    assert e.value.code == "NOT_INTERNAL"


def test_cannot_give_someone_elses_slot(lab, sent):
    factory, eq, alice, bob, a, b = lab
    carol = factory.student()
    with pytest.raises(sx.ExchangeError) as e:
        sx.create_request(carol, [_slots(b)[0].pk], [_slots(a)[0].pk])
    assert e.value.code == "NOT_YOUR_BOOKING"


def test_not_after_sample_accepted(lab, sent):
    factory, eq, alice, bob, a, b = lab
    BookingSampleTrace.objects.create(booking=a, status=SampleTraceStatus.SAMPLE_ACCEPTED)
    with pytest.raises(sx.ExchangeError) as e:
        sx.create_request(bob, [_slots(b)[0].pk], [_slots(a)[0].pk])
    assert e.value.code == "SAMPLE_ACCEPTED"


def test_not_within_five_minutes_of_the_slot(lab, sent):
    factory, eq, alice, bob, a, b = lab
    first = _slots(a)[0]
    with pytest.raises(sx.ExchangeError) as e:
        sx.create_request(bob, [_slots(b)[0].pk], [first.pk], now=first.start_datetime - timedelta(minutes=4))
    assert e.value.code in ("TOO_LATE", "ALREADY_STARTED")
    req = sx.create_request(bob, [_slots(b)[0].pk], [first.pk], now=first.start_datetime - timedelta(minutes=10))
    assert req.expires_at == first.start_datetime - timedelta(minutes=5)


def test_expired_request_cannot_be_accepted(lab, sent):
    factory, eq, alice, bob, a, b = lab
    req = sx.create_request(bob, [_slots(b)[0].pk], [_slots(a)[0].pk])
    SlotExchangeRequest.objects.filter(pk=req.pk).update(expires_at=timezone.now() - timedelta(seconds=1))
    with pytest.raises(sx.ExchangeError) as e:
        sx.accept_request(req.pk, alice)
    assert e.value.code == "NOT_PENDING"
    assert SlotExchangeRequest.objects.get(pk=req.pk).status == SlotExchangeStatus.EXPIRED


def test_accept_fails_cleanly_when_the_booking_changed(lab, sent):
    factory, eq, alice, bob, a, b = lab
    req = sx.create_request(bob, [_slots(b)[0].pk], [_slots(a)[0].pk])
    a.status = BookingStatus.CANCELLED
    a.save(update_fields=["status"])
    with pytest.raises(sx.ExchangeError):
        sx.accept_request(req.pk, alice)
    req.refresh_from_db()
    assert req.status == SlotExchangeStatus.FAILED and req.failure_reason
    assert DailySlot.objects.get(pk=_slots(b)[0].pk).booking_id == b.pk  # nothing moved


def test_only_the_responder_can_accept_and_only_the_requester_can_withdraw(lab, sent):
    factory, eq, alice, bob, a, b = lab
    req = sx.create_request(bob, [_slots(b)[0].pk], [_slots(a)[0].pk])
    with pytest.raises(sx.ExchangeError):
        sx.accept_request(req.pk, bob)
    with pytest.raises(sx.ExchangeError):
        sx.cancel_request(req.pk, alice)
    sx.cancel_request(req.pk, bob)
    assert SlotExchangeRequest.objects.get(pk=req.pk).status == SlotExchangeStatus.CANCELLED


def test_decline_keeps_slots(lab, sent):
    factory, eq, alice, bob, a, b = lab
    req = sx.create_request(bob, [_slots(b)[0].pk], [_slots(a)[0].pk])
    sx.decline_request(req.pk, alice, "Sorry, I need that time.")
    req.refresh_from_db()
    assert req.status == SlotExchangeStatus.DECLINED and req.response_note
    assert len(_slots(a)) == 2 and len(_slots(b)) == 1


def test_other_requests_for_moved_slots_are_closed(lab, sent):
    factory, eq, alice, bob, a, b = lab
    carol = factory.student()
    c = factory.booking(carol, eq, factory.future(days=5, hour=10), total_charge="100.00")
    target = _slots(a)[0]
    first = sx.create_request(bob, [_slots(b)[0].pk], [target.pk])
    second = sx.create_request(carol, [_slots(c)[0].pk], [target.pk])
    sx.accept_request(first.pk, alice)
    assert SlotExchangeRequest.objects.get(pk=second.pk).status == SlotExchangeStatus.FAILED


# --- API -----------------------------------------------------------------------------------------------------


def test_api_flow(lab, sent):
    factory, eq, alice, bob, a, b = lab
    b1 = _slots(b)[0]
    a2 = _slots(a)[1]
    resp = factory.client_for(bob).post(
        "/api/slot-exchanges/", {"offered_slot_ids": [b1.pk], "requested_slot_ids": [a2.pk]}, format="json"
    )
    assert resp.status_code == 201, resp.content
    req_id = resp.json()["id"]
    listing = factory.client_for(alice).get("/api/slot-exchanges/").json()
    assert listing["pending_incoming"] == 1
    row = listing["results"][0]
    assert row["direction"] == "incoming" and row["can_respond"]
    assert row["you_give"][0]["id"] == a2.pk and row["you_get"][0]["id"] == b1.pk
    assert row["other_user"]["email"] == bob.email
    resp = factory.client_for(alice).post(f"/api/slot-exchanges/{req_id}/accept/")
    assert resp.status_code == 200, resp.content
    assert resp.json()["status"] == SlotExchangeStatus.ACCEPTED


def test_api_options_show_booker_and_my_bookings(lab, sent):
    factory, eq, alice, bob, a, b = lab
    resp = factory.client_for(bob).get(f"/api/slot-exchanges/options/?slot_id={_slots(a)[0].pk}")
    assert resp.status_code == 200, resp.content
    body = resp.json()
    assert body["booker"]["email"] == alice.email
    assert body["exchange_available"] is True
    assert [m["booking_id"] for m in body["my_bookings"]] == [b.pk]


def test_api_options_refused_for_external_viewer(lab, sent):
    factory, eq, alice, bob, a, b = lab
    outsider = UserFactory(user_type=UserType.EXTERNAL, admin_approved=True)
    resp = factory.client_for(outsider).get(f"/api/slot-exchanges/options/?slot_id={_slots(a)[0].pk}")
    assert resp.status_code == 403


def test_only_main_admin_can_switch_slot_exchange(lab):
    from rest_framework.test import APIRequestFactory

    from iic_booking.equipment.serializers import EquipmentAdminWriteSerializer

    factory, eq, alice, bob, a, b = lab
    request = APIRequestFactory().patch("/")
    request.user = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    bad = EquipmentAdminWriteSerializer(eq, data={"slot_exchange_enabled": False}, partial=True,
                                        context={"request": request})
    assert not bad.is_valid() and "slot_exchange_enabled" in bad.errors
    request.user = UserFactory(user_type=UserType.ADMIN, is_staff=True, admin_approved=True)
    ok = EquipmentAdminWriteSerializer(eq, data={"slot_exchange_enabled": False}, partial=True,
                                       context={"request": request})
    assert ok.is_valid(), ok.errors


def test_django_admin_equipment_page_has_the_switch(lab):
    from django.test import Client

    factory, eq, alice, bob, a, b = lab
    admin_user = UserFactory(user_type=UserType.ADMIN, is_staff=True, is_superuser=True, admin_approved=True)
    client = Client()
    client.force_login(admin_user)
    resp = client.get(f"/admin/equipment/equipment/{eq.pk}/change/")
    assert resp.status_code == 200
    assert b'name="slot_exchange_enabled"' in resp.content
