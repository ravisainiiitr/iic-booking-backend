"""In-app calls: recipients, authorisation, state machine, busy/missed, rate limits, devices, push and signalling."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.calls import conf, service, silent
from iic_booking.calls.consumers import CallConsumer
from iic_booking.calls.models import CallDevice, CallSession, CallSilentHours, CallStatus
from iic_booking.equipment.models import Booking, BookingStatus, ChargeProfile, Equipment, EquipmentManager, EquipmentOperator, Holiday
from iic_booking.users.models import Department
from iic_booking.users.models.department import DepartmentType
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _user(user_type, **kw):
    return UserFactory(user_type=user_type, admin_approved=True, is_test_account=kw.pop("test", True), **kw)


@pytest.fixture
def on(settings):
    settings.CALLS_ENABLED = True
    settings.CALLS_TEST_ACCOUNTS_ONLY = True
    settings.CALLS_TURN_URLS = []
    settings.CHANNEL_LAYERS = {"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}
    cache.clear()
    return settings


class World:
    def __init__(self):
        tag = uuid.uuid4().hex[:5].upper()
        self.dept = Department.objects.create(name=f"Calls Dept {tag}", code=f"C{tag}", department_type=DepartmentType.INTERNAL,
                                              equipment_visibility_enabled=True, equipment_booking_enabled=True)
        self.other_dept = Department.objects.create(name=f"Other Dept {tag}", code=f"O{tag}", department_type=DepartmentType.INTERNAL)
        self.eq = Equipment.objects.create(name=f"XRD {tag}", code=f"X{tag}", slot_duration_minutes=60, internal_department=self.dept,
                                           user_rating_enabled=False, status="ACTIVE")
        self.student = _user(UserType.STUDENT, department=self.dept)
        self.other_student = _user(UserType.STUDENT, department=self.dept)
        self.operator = _user(UserType.OPERATOR, department=self.dept)
        self.oic = _user(UserType.MANAGER, department=self.dept)
        self.accounts = _user(UserType.FINANCE, department=self.dept)
        self.foreign_accounts = _user(UserType.FINANCE, department=self.other_dept)
        self.real_operator = _user(UserType.OPERATOR, department=self.dept, test=False)
        EquipmentOperator.objects.create(equipment=self.eq, operator=self.operator)
        EquipmentOperator.objects.create(equipment=self.eq, operator=self.real_operator,
                                         role=EquipmentOperator.Role.SECONDARY)
        EquipmentManager.objects.create(equipment=self.eq, manager=self.oic)
        profile = ChargeProfile.objects.create(equipment=self.eq, user_type=UserType.STUDENT, primary_unit_charge=Decimal("10"))
        self.booking = Booking.objects.create(user=self.student, equipment=self.eq, charge_profile=profile, status=BookingStatus.BOOKED,
                                              total_charge=Decimal("10"), total_time_minutes=60, virtual_booking_id=f"IIC{tag}")


@pytest.fixture
def w(on, db):
    return World()


def api(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def call(w, role="operator", user=None, **extra):
    return api(user or w.student).post("/api/calls/", {"context": "booking", "id": w.booking.pk, "role": role, **extra}, format="json")


# --- recipients -----------------------------------------------------------------------------------------------------

def test_options_follow_the_equipment_staff_and_department(w):
    data = api(w.student).get("/api/calls/options/", {"context": "booking", "id": w.booking.pk}).json()
    people = {r["role"]: [p["id"] for p in r["people"]] for r in data["roles"]}
    assert people["operator"] == [w.operator.pk]  # the real (non-test) operator is left out during the pilot
    assert people["oic"] == [w.oic.pk]
    assert people["accounts"] == [w.accounts.pk]  # never another department's Accounts In Charge
    inv = api(w.student).get("/api/calls/options/", {"context": "invoice", "id": w.booking.pk}).json()
    assert [r["role"] for r in inv["roles"]] == ["accounts"]
    assert api(w.other_student).get("/api/calls/options/", {"context": "booking", "id": w.booking.pk}).status_code == 404


def test_fallbacks_when_nobody_is_reachable(w):
    roles = {r["role"]: r for r in api(w.student).get("/api/calls/options/", {"context": "booking", "id": w.booking.pk}).json()["roles"]}
    assert roles["operator"]["fallback"] == "ticket"  # nobody online and no app installed
    cache.set(f"calls:presence:{w.oic.pk}", 1, 60)
    roles = {r["role"]: r for r in api(w.student).get("/api/calls/options/", {"context": "booking", "id": w.booking.pk}).json()["roles"]}
    assert roles["operator"]["fallback"] == "oic" and roles["oic"]["people"][0]["available"] is True


def test_closed_unless_enabled_and_test_account_during_pilot(w, on):
    real = _user(UserType.STUDENT, test=False)
    assert api(real).get("/api/calls/config/").json()["enabled"] is False
    on.CALLS_ENABLED = False
    assert api(w.student).get("/api/calls/config/").json() == {"enabled": False, "ice_servers": [], "turn": False, "ring_seconds": 45}


# --- lifecycle ------------------------------------------------------------------------------------------------------

def test_ring_accept_end_with_duration_and_history(w):
    res = call(w)
    assert res.status_code == 201 and res.json()["status"] == "ringing" and res.json()["other"]["id"] == w.operator.pk
    cid = res.json()["id"]
    assert api(w.student).post(f"/api/calls/{cid}/accept/").status_code == 404  # only the callee can accept
    assert api(w.oic).post(f"/api/calls/{cid}/accept/").status_code == 404  # nor anyone else
    assert api(w.operator).post(f"/api/calls/{cid}/accept/", {"platform": "android"}).json()["status"] == "accepted"
    CallSession.objects.filter(pk=cid).update(answered_at=timezone.now() - timedelta(seconds=75))
    ended = api(w.student).post(f"/api/calls/{cid}/end/").json()
    assert ended["status"] == "ended" and ended["duration_seconds"] >= 75
    assert [c["id"] for c in api(w.operator).get("/api/calls/").json()["results"]] == [cid]
    assert api(w.other_student).get("/api/calls/").json()["results"] == []  # history is private
    assert api(w.other_student).get(f"/api/calls/{cid}/").status_code == 404
    kinds = list(CallSession.objects.get(pk=cid).events.values_list("kind", flat=True))
    assert kinds == ["ringing", "accepted", "ended"]


def test_first_answer_wins_and_repeats_are_idempotent(w):
    cid = call(w).json()["id"]
    assert api(w.operator).post(f"/api/calls/{cid}/accept/").json()["status"] == "accepted"
    assert api(w.operator).post(f"/api/calls/{cid}/decline/").json()["status"] == "accepted"  # too late to decline
    assert api(w.operator).post(f"/api/calls/{cid}/accept/").json()["status"] == "accepted"  # same again: same result
    assert CallSession.objects.get(pk=cid).events.filter(kind="accepted").count() == 1


def test_decline_cancel_timeout_and_missed(w):
    a = call(w).json()["id"]
    assert api(w.operator).post(f"/api/calls/{a}/decline/").json()["status"] == "declined"
    b = call(w, role="oic").json()["id"]
    assert api(w.student).post(f"/api/calls/{b}/cancel/").json()["status"] == "cancelled"
    c = call(w, role="accounts").json()["id"]
    CallSession.objects.filter(pk=c).update(created_at=timezone.now() - timedelta(seconds=conf.ring_seconds() + 10))
    assert api(w.student).get(f"/api/calls/{c}/").json()["status"] == "missed"
    assert api(w.accounts).post(f"/api/calls/{c}/accept/").json()["status"] == "missed"  # too late


def test_busy_when_the_callee_is_already_in_a_call(w):
    first = call(w).json()["id"]
    api(w.operator).post(f"/api/calls/{first}/accept/")
    busy = call(w)
    assert busy.status_code == 201 and busy.json()["status"] == "busy"


def test_failure_is_recorded_with_a_safe_code(w):
    cid = call(w).json()["id"]
    api(w.operator).post(f"/api/calls/{cid}/accept/")
    res = api(w.student).post(f"/api/calls/{cid}/fail/", {"code": "ice_failed<script>"}, format="json").json()
    assert res["status"] == "failed" and res["failure_code"] == "ice_failedscript"


def test_never_calls_a_person_the_client_names_outside_the_candidates(w):
    assert call(w, callee_id=w.foreign_accounts.pk, role="accounts").status_code == 400
    assert call(w, callee_id=w.real_operator.pk).status_code == 400  # not a test account during the pilot
    assert call(w, role="nobody").status_code == 400
    assert api(w.other_student).post("/api/calls/", {"context": "booking", "id": w.booking.pk, "role": "operator"},
                                     format="json").status_code == 404


def test_rate_limited(w):
    codes = []
    for _ in range(4):
        r = call(w)
        codes.append(r.status_code)
        if r.status_code == 201:
            api(w.student).post(f"/api/calls/{r.json()['id']}/cancel/")
    assert codes[:3] == [201, 201, 201] and codes[3] == 429


# --- native decline, devices and push -----------------------------------------------------------------------------

def test_decline_from_the_native_incoming_screen_with_the_action_token(w):
    session = CallSession.objects.get(pk=call(w).json()["id"])
    token = service.action_token(session)
    assert APIClient().post(f"/api/calls/{session.pk}/device-decline/", {"token": "forged"}, format="json").status_code == 400
    other = CallSession.objects.get(pk=call(w, role="oic").json()["id"])
    assert APIClient().post(f"/api/calls/{other.pk}/device-decline/", {"token": token}, format="json").status_code == 400
    assert APIClient().post(f"/api/calls/{session.pk}/device-decline/", {"token": token}, format="json").json() == {"status": "declined"}


def test_devices_register_refresh_and_revoke(w):
    url = "/api/calls/devices/"
    assert api(w.operator).post(url, {"platform": "android", "device_id": "inst-1", "token": "t1"}, format="json").json() == {"ok": True}
    assert api(w.operator).post(url, {"platform": "android", "device_id": "inst-1", "token": "t2"}, format="json").status_code == 200
    device = CallDevice.objects.get(device_id="inst-1")
    assert device.token == "t2" and device.user == w.operator
    # Another account signing in on the same phone takes the install over.
    api(w.oic).post(url, {"platform": "android", "device_id": "inst-1", "token": "t3"}, format="json")
    assert CallDevice.objects.get(device_id="inst-1").user == w.oic
    api(w.oic).delete(url, {"platform": "android", "device_id": "inst-1"}, format="json")
    assert CallDevice.objects.get(device_id="inst-1").revoked_at is not None
    assert api(w.oic).post(url, {"platform": "fax", "device_id": "x", "token": "t"}, format="json").status_code == 400


def test_ringing_pushes_every_device_and_drops_dead_tokens(w, django_capture_on_commit_callbacks):
    CallDevice.objects.create(user=w.operator, platform="android", device_id="a", token="good")
    CallDevice.objects.create(user=w.operator, platform="ios", device_id="b", token="dead")
    sent = []

    def fcm(token, data, ttl):
        sent.append(("fcm", token, data["type"], data["call_id"]))
        return "sent"

    with patch("iic_booking.calls.push.send_fcm", fcm), \
            patch("iic_booking.calls.push.send_apns_voip", lambda token, data, ttl: "gone"), \
            django_capture_on_commit_callbacks(execute=True):
        cid = call(w).json()["id"]
    assert sent == [("fcm", "good", "incoming_call", cid)]
    assert CallDevice.objects.get(device_id="b").revoked_at is not None


def test_turn_credentials_are_short_lived_and_hide_the_secret(w, on):
    assert conf.ice_servers(w.student) == [{"urls": on.CALLS_STUN_URLS}]
    on.CALLS_TURN_URLS = ["turn:turn.example.org:3478"]
    on.CALLS_TURN_SECRET = "s3cret"
    servers = conf.ice_servers(w.student, now=1_000_000)
    turn = servers[-1]
    assert turn["username"] == f"{1_000_000 + on.CALLS_TURN_TTL_SECONDS}:{w.student.pk}"
    assert "s3cret" not in json.dumps(servers)


# --- signalling -------------------------------------------------------------------------------------------------------

class FakeLayer:
    def __init__(self):
        self.sent = []

    async def group_send(self, group, message):
        self.sent.append((group, message))


def _consumer(user):
    c = CallConsumer()
    c.user_id = user.pk
    c.channel_layer = FakeLayer()
    return c


@pytest.mark.django_db(transaction=True)
def test_signals_only_between_participants_of_an_accepted_call(on):
    w = World()
    cid = call(w).json()["id"]
    offer = json.dumps({"type": "signal", "call_id": cid, "payload": {"kind": "offer", "sdp": "v=0", "evil": 1}})
    caller = _consumer(w.student)
    asyncio.run(caller.receive(offer))
    assert caller.channel_layer.sent == []  # not before the callee answers
    api(w.operator).post(f"/api/calls/{cid}/accept/")
    asyncio.run(caller.receive(offer))
    group, message = caller.channel_layer.sent[0]
    assert group == f"calls.user.{w.operator.pk}"
    assert message["message"] == {"type": "signal", "call_id": cid, "payload": {"kind": "offer", "sdp": "v=0"}}
    outsider = _consumer(w.oic)
    asyncio.run(outsider.receive(offer))
    assert outsider.channel_layer.sent == []
    asyncio.run(caller.receive(json.dumps({"type": "signal", "call_id": cid, "payload": {"kind": "shell", "sdp": "x"}})))
    asyncio.run(caller.receive("x" * 20000))
    assert len(caller.channel_layer.sent) == 1


@pytest.mark.django_db(transaction=True)
def test_websocket_refuses_users_outside_the_pilot(on):
    from rest_framework.authtoken.models import Token

    real = _user(UserType.STUDENT, test=False)
    pilot = _user(UserType.STUDENT)
    c = CallConsumer()
    assert asyncio.run(c._authenticate(Token.objects.create(user=real).key)) is None
    assert asyncio.run(c._authenticate(Token.objects.create(user=pilot).key)) == pilot
    assert asyncio.run(c._authenticate("nonsense")) is None


# --- lab staff calling the booking user -----------------------------------------------------------------------------
def _roles(user, w, context="booking", pk=None):
    data = api(user).get("/api/calls/options/", {"context": context, "id": pk or w.booking.pk})
    return {r["role"]: r for r in data.json().get("roles", [])}


def test_only_the_staff_of_the_equipment_may_call_the_booking_user(w):
    for staff in (w.operator, w.oic):
        assert _roles(staff, w)["user"]["people"][0]["id"] == w.student.pk
        assert call(w, role="user", user=staff, callee_id=w.student.pk).status_code == 201
        CallSession.objects.all().delete()

    assert "user" not in _roles(w.student, w)  # the user's own view: staff roles only
    assert call(w, role="user", user=w.student).status_code == 400

    outsider = _user(UserType.OPERATOR, department=w.dept)  # another instrument's operator
    other = Equipment.objects.create(name="Other", code=f"Z{uuid.uuid4().hex[:5]}", slot_duration_minutes=60,
                                     internal_department=w.dept, user_rating_enabled=False, status="ACTIVE")
    EquipmentOperator.objects.create(equipment=other, operator=outsider)
    assert call(w, role="user", user=outsider).status_code == 404

    admin = _user(UserType.DEPT_ADMIN, department=w.dept)  # can see the booking, is not its lab staff
    assert "user" not in _roles(admin, w)
    assert call(w, role="user", user=admin).status_code == 400


def test_staff_cannot_call_someone_else_as_the_booking_user(w):
    resp = call(w, role="user", user=w.operator, callee_id=w.other_student.pk)
    assert resp.status_code == 400 and not CallSession.objects.filter(caller=w.operator).exists()


def test_booking_user_role_only_in_the_booking_context(w):
    assert "user" not in _roles(w.operator, w, context="invoice")
    assert "user" not in _roles(w.operator, w, context="equipment", pk=w.eq.pk)
    resp = api(w.operator).post("/api/calls/", {"context": "invoice", "id": w.booking.pk, "role": "user"}, format="json")
    assert resp.status_code == 400


def test_inactive_booking_user_is_not_offered(w):
    type(w.student).objects.filter(pk=w.student.pk).update(is_active=False)  # save() recalculates the flag
    assert _roles(w.operator, w)["user"]["people"] == []
    assert call(w, role="user", user=w.operator).status_code == 404


def test_pilot_test_account_rule_covers_the_booking_user(w):
    real = _user(UserType.STUDENT, department=w.dept, test=False)
    Booking.objects.filter(pk=w.booking.pk).update(user=real)
    assert _roles(w.operator, w)["user"]["people"] == []


def test_unreachable_booking_user_has_no_fallback_and_reachable_shows(w):
    role = _roles(w.operator, w)["user"]
    assert role["fallback"] == "" and role["people"][0]["available"] is False
    cache.set(f"calls:presence:{w.student.pk}", 1, 60)
    assert _roles(w.operator, w)["user"]["people"][0]["available"] is True


def test_the_called_user_is_not_told_for_the_booking_user(w, django_capture_on_commit_callbacks):
    CallDevice.objects.create(user=w.student, platform="android", device_id="s", token="t")
    pushed = []
    with patch("iic_booking.calls.push.send_fcm", lambda token, data, ttl: pushed.append(data) or "sent"), \
            patch("iic_booking.calls.push.fcm_configured", lambda: True), \
            django_capture_on_commit_callbacks(execute=True):
        created = call(w, role="user", user=w.operator, callee_id=w.student.pk).json()
    assert created["role_label"] == "Booking user"  # the caller's own screen
    assert pushed[0]["role_label"] == "" and pushed[0]["booking_ref"] == w.booking.virtual_booking_id
    session = CallSession.objects.get(pk=created["id"])
    assert service.serialize(session, w.student)["role_label"] == ""


def test_staff_can_call_many_users_but_not_one_user_repeatedly(w):
    def book(n):
        users = []
        for _ in range(n):
            u = _user(UserType.STUDENT, department=w.dept)
            users.append(Booking.objects.create(user=u, equipment=w.eq, charge_profile=w.booking.charge_profile,
                                                status=BookingStatus.BOOKED, total_charge=Decimal("10"), total_time_minutes=60))
        return users

    many = service.CALLS_PER_10_MIN + 4  # more than the ordinary limit
    for booking in book(many):
        resp = api(w.operator).post("/api/calls/", {"context": "booking", "id": booking.pk, "role": "user"}, format="json")
        assert resp.status_code == 201, resp.content
        CallSession.objects.all().delete()  # the operator has hung up: not busy

    for attempt in range(service.SAME_CALLEE_PER_10_MIN):
        assert call(w, role="user", user=w.operator).status_code == 201
        CallSession.objects.all().delete()
    refused = call(w, role="user", user=w.operator)
    assert refused.status_code == 429 and refused.json()["code"] == "rate_limited"


def test_ordinary_limit_still_applies_to_everyone_else(w):
    with patch.object(service, "SAME_CALLEE_PER_10_MIN", 100):
        for _ in range(service.CALLS_PER_10_MIN):
            assert call(w).status_code == 201
            CallSession.objects.all().delete()
        assert call(w).status_code == 429


def test_staff_overall_cap_stops_a_runaway_client(w):
    with patch.object(service, "SAME_CALLEE_PER_10_MIN", 1000), patch.object(service, "STAFF_CALLS_TO_USERS_PER_10_MIN", 2):
        for _ in range(2):
            assert call(w, role="user", user=w.operator).status_code == 201
            CallSession.objects.all().delete()
        assert call(w, role="user", user=w.operator).status_code == 429


def test_accounts_in_charge_may_call_the_user_of_a_booking_they_handle(w):
    external = sorted(UserType.get_external_user_codes())[0]
    Booking.objects.filter(pk=w.booking.pk).update(user_type_snapshot=external)
    assert _roles(w.accounts, w)["user"]["people"][0]["id"] == w.student.pk
    assert call(w, role="user", user=w.accounts, callee_id=w.student.pk).status_code == 201
    assert "user" not in _roles(w.foreign_accounts, w)  # another department's Accounts In Charge


# --- silent hours -----------------------------------------------------------------------------------------------------
WEDNESDAY_11AM = timezone.make_aware(datetime(2026, 11, 4, 11, 0))
WEDNESDAY_7PM = timezone.make_aware(datetime(2026, 11, 4, 19, 0))
SATURDAY_11AM = timezone.make_aware(datetime(2026, 11, 7, 11, 0))


def _silent(w, **kw):
    return CallSilentHours.objects.create(equipment=w.eq, enabled=True, **kw)


@pytest.fixture
def at():
    def _at(moment):
        return patch("iic_booking.calls.silent._now", lambda: moment)
    return _at


@pytest.mark.parametrize("role", ["operator", "oic", "accounts"])
def test_users_cannot_call_any_staff_outside_office_hours(w, at, role):
    _silent(w)
    with at(WEDNESDAY_7PM):
        resp = call(w, role=role)
    assert resp.status_code == 403 and resp.json()["code"] == "silent_hours"
    assert "Thu 5 Nov at 9:00 AM" in resp.json()["error"]
    assert not CallSession.objects.filter(caller=w.student).exists()


def test_users_can_call_during_office_hours_and_edges_are_exact(w, at):
    _silent(w)
    with at(WEDNESDAY_11AM):
        assert call(w).status_code == 201
    CallSession.objects.all().delete()
    rule = CallSilentHours.objects.get(equipment=w.eq)
    assert silent.is_silent(rule, timezone.make_aware(datetime(2026, 11, 4, 8, 59)))
    assert not silent.is_silent(rule, timezone.make_aware(datetime(2026, 11, 4, 9, 0)))
    assert not silent.is_silent(rule, timezone.make_aware(datetime(2026, 11, 4, 17, 29)))
    assert silent.is_silent(rule, timezone.make_aware(datetime(2026, 11, 4, 17, 30)))


def test_weekends_and_holidays_are_silent_by_default_and_can_be_excluded(w, at):
    rule = _silent(w)
    assert silent.is_silent(rule, SATURDAY_11AM)
    Holiday.objects.update_or_create(date=date(2026, 11, 4), defaults={"is_active": True})
    assert silent.is_silent(rule, WEDNESDAY_11AM)
    rule.silent_weekends = rule.silent_holidays = False
    assert not silent.is_silent(rule, SATURDAY_11AM) and not silent.is_silent(rule, WEDNESDAY_11AM)


def test_inactive_holiday_does_not_count(w):
    rule = _silent(w)
    Holiday.objects.update_or_create(date=date(2026, 11, 4), defaults={"is_active": False})
    assert not silent.is_silent(rule, WEDNESDAY_11AM)


def test_next_opening_skips_the_weekend_and_holidays(w):
    rule = _silent(w)
    Holiday.objects.update_or_create(date=date(2026, 11, 9), defaults={"is_active": True})  # the Monday
    assert "Tue 10 Nov at 9:00 AM" in silent.describe_block(rule, SATURDAY_11AM)["message"]


def test_off_by_default_and_when_switched_off(w, at):
    with at(WEDNESDAY_7PM):
        assert call(w).status_code == 201  # no rule at all
    CallSession.objects.all().delete()
    CallSilentHours.objects.create(equipment=w.eq, enabled=False)
    with at(WEDNESDAY_7PM):
        assert call(w).status_code == 201


def test_staff_are_not_held_back(w, at):
    _silent(w)
    with at(WEDNESDAY_7PM):
        assert call(w, role="user", user=w.operator, callee_id=w.student.pk).status_code == 201
        CallSession.objects.all().delete()
        assert call(w, role="oic", user=w.operator).status_code == 201
        CallSession.objects.all().delete()
        assert _roles(w.oic, w)  # still offered
        assert api(w.oic).get("/api/calls/options/", {"context": "booking", "id": w.booking.pk}).json()["silent_hours"] is None


def test_the_call_options_say_why(w, at):
    _silent(w)
    with at(WEDNESDAY_7PM):
        data = api(w.student).get("/api/calls/options/", {"context": "booking", "id": w.booking.pk}).json()
    assert data["silent_hours"]["active"] is True and "Thu 5 Nov" in data["silent_hours"]["message"]
    with at(WEDNESDAY_11AM):
        data = api(w.student).get("/api/calls/options/", {"context": "booking", "id": w.booking.pk}).json()
    assert data["silent_hours"] is None


def test_a_mode_follows_its_base_equipment_unless_it_has_its_own_rule(w, at):
    mode = Equipment.objects.create(name="Mode", code=f"M{uuid.uuid4().hex[:5]}", slot_duration_minutes=60, internal_department=w.dept,
                                    user_rating_enabled=False, status="ACTIVE", parent_equipment=w.eq)
    _silent(w)
    assert silent.is_silent(silent.rule_for(mode), WEDNESDAY_7PM)
    CallSilentHours.objects.create(equipment=mode, enabled=False)
    assert not silent.is_silent(silent.rule_for(mode), WEDNESDAY_7PM)


def _put(user, w, **kw):
    body = {"enabled": True, "office_start": "09:00", "office_end": "17:30", "silent_weekends": True, "silent_holidays": True, **kw}
    return api(user).put(f"/api/calls/silent-hours/{w.eq.pk}/", body, format="json")


def test_only_the_officer_in_charge_sets_silent_hours(w):
    url = f"/api/calls/silent-hours/{w.eq.pk}/"
    defaults = api(w.oic).get(url).json()
    assert defaults["enabled"] is False and defaults["office_start"] == "09:00" and defaults["office_end"] == "17:30"
    assert defaults["silent_weekends"] is True and defaults["silent_holidays"] is True

    assert _put(w.oic, w, office_end="18:00").status_code == 200
    rule = CallSilentHours.objects.get(equipment=w.eq)
    assert rule.enabled and rule.office_end == time(18, 0) and rule.updated_by == w.oic

    other_oic = _user(UserType.MANAGER, department=w.dept)
    for outsider in (w.student, w.operator, w.accounts, other_oic):
        assert _put(outsider, w, enabled=False).status_code == 403
        assert api(outsider).get(url).status_code == 403
    assert CallSilentHours.objects.get(equipment=w.eq).enabled is True


@pytest.mark.parametrize("bad", [{"office_start": "18:00", "office_end": "09:00"}, {"office_start": "09:00", "office_end": "09:00"},
                                 {"office_start": "soon"}, {"office_end": ""}, {"enabled": "yes"}, {"silent_weekends": None}])
def test_silent_hours_reject_bad_values(w, bad):
    resp = _put(w.oic, w, **bad)
    assert resp.status_code == 400 and not CallSilentHours.objects.filter(equipment=w.eq).exists()


# --- stopping the ring on a closed app ---------------------------------------------------------------------------------
def test_phone_stops_ringing_when_the_call_stops_ringing(w, django_capture_on_commit_callbacks):
    CallDevice.objects.create(user=w.operator, platform="android", device_id="a", token="good")
    CallDevice.objects.create(user=w.operator, platform="ios", device_id="b", token="iphone")
    sent = []

    def fcm(token, data, ttl):
        sent.append((token, data["type"], data["call_id"]))
        return "sent"

    with patch("iic_booking.calls.push.send_fcm", fcm), patch("iic_booking.calls.push.fcm_configured", lambda: True), \
            patch("iic_booking.calls.push.send_apns_voip", lambda token, data, ttl: "sent"), \
            django_capture_on_commit_callbacks(execute=True):
        cid = call(w).json()["id"]
    assert sent == [("good", "incoming_call", cid)]

    sent.clear()
    with patch("iic_booking.calls.push.send_fcm", fcm), patch("iic_booking.calls.push.fcm_configured", lambda: True), \
            django_capture_on_commit_callbacks(execute=True):
        service.cancel(w.student, cid)
        service.cancel(w.student, cid)  # repeated: nothing more is sent
    assert sent == [("good", "call_ended", cid)]  # never to iOS: a VoIP push must report a new call


def test_answering_stops_the_other_devices_but_ending_the_call_does_not_push(w, django_capture_on_commit_callbacks):
    CallDevice.objects.create(user=w.operator, platform="android", device_id="a", token="good")
    sent = []

    def fcm(token, data, ttl):
        sent.append(data["type"])
        return "sent"

    with patch("iic_booking.calls.push.send_fcm", fcm), patch("iic_booking.calls.push.fcm_configured", lambda: True), \
            django_capture_on_commit_callbacks(execute=True):
        cid = call(w).json()["id"]
        service.accept(w.operator, cid)
        service.end(w.operator, cid)
    assert sent == ["incoming_call", "call_ended"]


def test_firebase_key_from_the_environment(settings):
    import base64

    from iic_booking.calls import push

    settings.CALLS_FCM_SERVICE_ACCOUNT_FILE = ""
    settings.CALLS_FCM_SERVICE_ACCOUNT_B64 = ""
    assert not push.fcm_configured()
    settings.CALLS_FCM_SERVICE_ACCOUNT_B64 = base64.b64encode(json.dumps({"project_id": "p"}).encode()).decode()
    assert push.fcm_configured() and push._fcm_account()["project_id"] == "p"
    settings.CALLS_FCM_SERVICE_ACCOUNT_B64 = "bm90IGpzb24="  # valid base64, not JSON
    assert not push.fcm_configured()


# --- who is calling, about what -------------------------------------------------------------------------------------
def test_incoming_call_shows_the_caller_photo_department_and_instrument(w):
    from iic_booking.users.models import User

    User.objects.filter(pk=w.student.pk).update(profile_picture="profile_pictures/caller.jpg", designation="PhD scholar")
    body = call(w).json()
    assert body["other"]["id"] == w.operator.pk and body["other"]["photo_url"] == ""  # no photo: the screen uses initials
    assert body["context"] == {"equipment_id": w.eq.pk, "equipment_name": w.eq.name, "equipment_department": w.dept.name,
                               "booking_ref": w.booking.virtual_booking_id}

    incoming = service.serialize(CallSession.objects.get(pk=body["id"]), w.operator)
    other = incoming["other"]
    assert incoming["direction"] == "incoming"
    assert other["photo_url"].endswith(f"/api/users/{w.student.pk}/profile-picture/") and other["photo_url"].startswith("http")
    assert other["department"] == w.dept.name and other["designation"] == "PhD scholar" and other["user_type_label"]


def test_call_about_an_instrument_has_no_booking_reference(w):
    r = api(w.student).post("/api/calls/", {"context": "equipment", "id": w.eq.pk, "role": "oic"}, format="json")
    assert r.status_code == 201, r.content
    assert r.json()["context"] == {"equipment_id": w.eq.pk, "equipment_name": w.eq.name,
                                   "equipment_department": w.dept.name, "booking_ref": ""}


def test_push_carries_the_caller_card_and_history_leaves_out_the_instrument(w, django_capture_on_commit_callbacks):
    CallDevice.objects.create(user=w.operator, platform="android", device_id="a", token="good")
    seen = {}

    def fcm(token, data, ttl):
        seen.update(data)
        return "sent"

    with patch("iic_booking.calls.push.send_fcm", fcm), django_capture_on_commit_callbacks(execute=True):
        cid = call(w).json()["id"]
    assert seen["caller_department"] == w.dept.name and seen["equipment_name"] == w.eq.name
    assert seen["equipment_department"] == w.dept.name and seen["booking_ref"] == w.booking.virtual_booking_id
    assert seen["caller_photo_url"] == "" and seen["caller_type"]
    assert all(isinstance(v, str) for v in seen.values())

    service.cancel(w.student, cid)
    history = api(w.student).get("/api/calls/").json()["results"]
    assert history[0]["context"] is None and history[0]["other"]["department"] == w.dept.name
