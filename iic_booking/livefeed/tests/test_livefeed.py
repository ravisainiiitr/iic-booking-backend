"""Live feed: started by the agent only during a slot, watched only by the booking owner, signalled offer → answer,
ended by the operator, the slot end or a silent agent."""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile, DailySlot, Equipment, SlotMaster
from iic_booking.livefeed import service
from iic_booking.livefeed.models import LiveFeedPeer, LiveFeedSession
from iic_booking.sync.authentication import DepartmentSyncAgentAuthentication, SyncAgentUser
from iic_booking.sync.models import AgentAssignment, AgentLifecycleStatus, DepartmentSyncAgent, EquipmentSyncProfile
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db
OFFER = "v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\ns=-\r\n"
ANSWER = "v=0\r\no=- 2 2 IN IP4 10.0.0.2\r\ns=-\r\n"


def _agent(dept, *equipment):
    agent = DepartmentSyncAgent.objects.create(agent_name=f"A-{uuid.uuid4().hex[:5]}", department=dept,
                                               machine_guid=uuid.uuid4(), status=AgentLifecycleStatus.ENROLLED,
                                               is_active=True)
    for eq in equipment:
        AgentAssignment.objects.create(sync_agent=agent, sync_profile=EquipmentSyncProfile.objects.create(equipment=eq),
                                       is_active=True)
    return agent


def _booking(owner, eq, start, end, status=BookingStatus.BOOKED):
    profile = ChargeProfile.objects.create(equipment=eq, user_type=UserType.STUDENT, primary_unit_charge=Decimal("10"))
    b = Booking.objects.create(user=owner, equipment=eq, charge_profile=profile, status=status,
                               total_charge=Decimal("10"), total_time_minutes=60,
                               virtual_booking_id=f"IIC{eq.code}{uuid.uuid4().hex[:6]}")
    sm = SlotMaster.objects.create(equipment=eq, slot_number=SlotMaster.objects.filter(equipment=eq).count() + 1,
                                   open_time=start.time().replace(microsecond=0),
                                   close_time=end.time().replace(microsecond=0), is_active=True)
    DailySlot.objects.create(slot_master=sm, date=start.date(), start_datetime=start, end_datetime=end, status="BOOKED",
                             booking=b)
    return b


@pytest.fixture
def world():
    dept = Department.objects.create(name=f"Live Dept {uuid.uuid4().hex[:5]}", code=f"L{uuid.uuid4().hex[:5]}")
    eq = Equipment.objects.create(name="FE-SEM", code=f"F{uuid.uuid4().hex[:5]}", slot_duration_minutes=60,
                                  internal_department=dept, user_rating_enabled=False)
    other_eq = Equipment.objects.create(name="XRD", code=f"X{uuid.uuid4().hex[:5]}", slot_duration_minutes=60,
                                        internal_department=dept, user_rating_enabled=False)
    owner, stranger = UserFactory(user_type=UserType.STUDENT), UserFactory(user_type=UserType.STUDENT)
    now = timezone.now()
    booking = _booking(owner, eq, now - timedelta(minutes=20), now + timedelta(minutes=40))
    return {"eq": eq, "other_eq": other_eq, "owner": owner, "stranger": stranger, "booking": booking,
            "agent": _agent(dept, eq), "other_agent": _agent(dept, other_eq)}


def as_agent(agent):
    def fake(self, request):
        request.sync_agent = agent
        return SyncAgentUser(agent), None
    return patch.object(DepartmentSyncAgentAuthentication, "authenticate", fake)


def agent_call(agent, method, url, data=None):
    with as_agent(agent):
        c = APIClient()
        return getattr(c, method)(url, data, format="json") if data is not None else getattr(c, method)(url)


def user_client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def start(w, **kw):
    body = {"equipment_id": w["eq"].pk, "sources": ["screen", "camera"], "audio": True, "equipment_pc": "SEM-PC", **kw}
    return agent_call(w["agent"], "post", "/api/v1/sync/live/start/", body)


def test_full_flow_owner_watches_and_operator_stops(world):
    res = start(world)
    assert res.status_code == 201, res.content
    sid = res.json()["session_id"]
    assert res.json()["viewer"] == (world["owner"].get_full_name() or "The booking owner")

    owner = user_client(world["owner"])
    listed = owner.get("/api/live/sessions/").json()
    assert [s["id"] for s in listed["results"]] == [sid]
    assert listed["results"][0]["sources"] == ["screen", "camera"]
    assert user_client(world["stranger"]).get("/api/live/sessions/").json()["results"] == []

    peer = owner.post(f"/api/live/sessions/{sid}/peers/", {"kind": "screen", "sdp": OFFER}, format="json").json()
    assert peer["status"] == "offered"
    poll = agent_call(world["agent"], "get", f"/api/v1/sync/live/{sid}/poll/").json()
    assert poll["status"] == "active" and poll["offers"] == [{"peer_id": peer["peer_id"], "kind": "screen", "sdp": OFFER}]

    assert agent_call(world["agent"], "post", f"/api/v1/sync/live/{sid}/answer/",
                      {"peer_id": peer["peer_id"], "sdp": ANSWER}).status_code == 200
    got = owner.get(f"/api/live/peers/{peer['peer_id']}/").json()
    assert (got["status"], got["sdp"]) == ("answered", ANSWER)
    poll = agent_call(world["agent"], "get", f"/api/v1/sync/live/{sid}/poll/").json()
    assert poll["offers"] == [] and poll["open"] == [peer["peer_id"]]

    agent_call(world["agent"], "post", f"/api/v1/sync/live/{sid}/stop/", {})
    got = owner.get(f"/api/live/peers/{peer['peer_id']}/").json()
    assert (got["status"], got["session_status"]) == ("closed", "ended")
    assert owner.get("/api/live/sessions/").json()["results"] == []


def test_cannot_start_without_a_slot_in_progress_or_for_another_agents_instrument(world):
    world["booking"].daily_slots.update(start_datetime=timezone.now() + timedelta(hours=2),
                                        end_datetime=timezone.now() + timedelta(hours=3))
    res = start(world)
    assert res.status_code == 409 and "No booking slot" in res.json()["error"]

    res = agent_call(world["agent"], "post", "/api/v1/sync/live/start/",
                     {"equipment_id": world["other_eq"].pk, "sources": ["screen"]})
    assert res.status_code == 409 and "not assigned" in res.json()["error"]


def test_slot_starting_within_ten_minutes_counts(world):
    world["booking"].daily_slots.update(start_datetime=timezone.now() + timedelta(minutes=8),
                                        end_datetime=timezone.now() + timedelta(minutes=68))
    assert start(world).status_code == 201


def test_cancelled_booking_has_no_feed(world):
    Booking.objects.filter(pk=world["booking"].pk).update(status=BookingStatus.CANCELLED)
    assert start(world).status_code == 409


def test_stranger_cannot_offer_or_read_peers(world):
    sid = start(world).json()["session_id"]
    stranger = user_client(world["stranger"])
    assert stranger.post(f"/api/live/sessions/{sid}/peers/", {"kind": "screen", "sdp": OFFER},
                         format="json").status_code == 404
    peer = user_client(world["owner"]).post(f"/api/live/sessions/{sid}/peers/", {"kind": "camera", "sdp": OFFER},
                                            format="json").json()
    assert stranger.get(f"/api/live/peers/{peer['peer_id']}/").status_code == 404


def test_only_shared_sources_and_one_connection_per_source(world):
    sid = start(world, sources=["screen"]).json()["session_id"]
    owner = user_client(world["owner"])
    assert owner.post(f"/api/live/sessions/{sid}/peers/", {"kind": "camera", "sdp": OFFER},
                      format="json").status_code == 400
    assert owner.post(f"/api/live/sessions/{sid}/peers/", {"kind": "screen", "sdp": "garbage"},
                      format="json").status_code == 400
    first = owner.post(f"/api/live/sessions/{sid}/peers/", {"kind": "screen", "sdp": OFFER}, format="json").json()
    second = owner.post(f"/api/live/sessions/{sid}/peers/", {"kind": "screen", "sdp": OFFER}, format="json").json()
    assert LiveFeedPeer.objects.get(pk=first["peer_id"]).status == "closed"
    assert LiveFeedPeer.objects.get(pk=second["peer_id"]).status == "offered"


def test_agent_cannot_touch_another_agents_session(world):
    sid = start(world).json()["session_id"]
    assert agent_call(world["other_agent"], "get", f"/api/v1/sync/live/{sid}/poll/").status_code == 404
    assert agent_call(world["other_agent"], "post", f"/api/v1/sync/live/{sid}/stop/", {}).status_code == 404


def test_slot_end_silent_agent_and_unanswered_offers_expire(world):
    sid = start(world).json()["session_id"]
    s = LiveFeedSession.objects.get(pk=sid)
    peer = service.offer(world["owner"], s, "screen", OFFER)
    LiveFeedPeer.objects.filter(pk=peer.pk).update(created_at=timezone.now() - timedelta(seconds=31))
    service.expire()
    peer.refresh_from_db()
    assert peer.status == "failed" and "did not answer" in peer.error

    LiveFeedSession.objects.filter(pk=sid).update(last_agent_poll_at=timezone.now() - timedelta(seconds=61))
    service.expire()
    s.refresh_from_db()
    assert s.status == "ended" and "stopped responding" in s.end_reason

    sid = start(world).json()["session_id"]
    service.expire(now=timezone.now() + timedelta(minutes=41))
    assert LiveFeedSession.objects.get(pk=sid).end_reason == "The booking slot ended."


def test_new_start_replaces_the_previous_session(world):
    first = start(world).json()["session_id"]
    second = start(world).json()["session_id"]
    assert LiveFeedSession.objects.get(pk=first).status == "ended"
    assert LiveFeedSession.objects.get(pk=second).status == "active"


def test_agent_can_report_a_failed_answer(world):
    sid = start(world).json()["session_id"]
    peer = user_client(world["owner"]).post(f"/api/live/sessions/{sid}/peers/", {"kind": "camera", "sdp": OFFER},
                                            format="json").json()
    agent_call(world["agent"], "post", f"/api/v1/sync/live/{sid}/answer/",
               {"peer_id": peer["peer_id"], "error": "Camera not reachable"})
    got = user_client(world["owner"]).get(f"/api/live/peers/{peer['peer_id']}/").json()
    assert (got["status"], got["error"]) == ("failed", "Camera not reachable")
