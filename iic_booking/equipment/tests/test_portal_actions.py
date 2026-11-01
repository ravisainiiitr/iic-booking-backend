"""Portal actions: one catalogue for search and the IITR Research Assistant, limited to the user's role."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment import portal_actions as P
from iic_booking.equipment.models import EquipmentManager
from iic_booking.research_copilot.tests.test_booking_assistant import _Lab, _actions, _new_conv, _post, flags  # noqa: F401
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


@pytest.fixture
def lab(flags):  # noqa: F811
    world = _Lab()
    world.admin = UserFactory(user_type=UserType.ADMIN, is_active=True)
    world.oic = UserFactory(user_type=UserType.MANAGER, is_active=True, department=world.department)
    EquipmentManager.objects.create(equipment=world.xrd, manager=world.oic)
    return world


# --- catalogue -------------------------------------------------------------------------------------------------------

def test_every_action_is_well_formed():
    assert len(P.BY_KEY) == len(P.ACTIONS)
    for a in P.ACTIONS:
        assert a.roles and a.keywords and a.title
        assert a.path or a.switch, a.key
        if a.equipment:
            assert a.equipment in ("manage", "book") and (a.switch or "{id}" in a.path), a.key


def test_actions_follow_the_role(lab):
    admin = {a.key for a in P.available(lab.admin)}
    student = {a.key for a in P.available(lab.student)}
    oic = {a.key for a in P.available(lab.oic)}
    assert {"slot_exchange", "change_slot_status", "user_management", "wallet_ledger"} <= admin
    assert "slot_exchange" not in oic and "change_slot_status" in oic and "user_management" not in oic
    assert {"my_bookings", "recharge", "my_results"} <= student
    assert not student & {"slot_exchange", "change_slot_status", "user_management", "booking_management"}


def test_matching_understands_requests(lab):
    assert P.match(lab.admin, "enable slot exchange")[0].key == "slot_exchange"
    assert P.match(lab.admin, "how do I block slots")[0].key == "change_slot_status"
    assert P.match(lab.student, "recharge my wallet")[0].key == "recharge"
    assert P.match(lab.student, "enable slot exchange") == [] or P.match(lab.student, "enable slot exchange")[0].key != "slot_exchange"
    assert P.desired_state("please disable slot exchange") is False
    assert P.desired_state("turn on split booking") is True
    assert P.desired_state("slot exchange") is None


def test_equipment_named_in_the_request(lab):
    action = P.BY_KEY["slot_exchange"]
    assert P.equipment_named_in(lab.admin, action, "enable slot exchange for FESEM-01")["equipment_id"] == lab.fesem.pk
    assert P.equipment_named_in(lab.admin, action, "enable slot exchange for transmission")["equipment_id"] == lab.tem.pk
    assert P.equipment_named_in(lab.admin, action, "enable slot exchange") is None
    # An OIC only chooses among their own instruments.
    change = P.BY_KEY["change_slot_status"]
    assert [r["id"] for r in P.equipment_choices(lab.oic, change)] == [lab.xrd.pk]


# --- API -------------------------------------------------------------------------------------------------------------

def test_search_offers_the_action_with_the_named_instrument(lab):
    body = _client(lab.admin).get("/api/search/", {"q": "slot exchange fesem"}).json()
    top = body["actions"][0]
    assert top["key"] == "slot_exchange" and top["switch"] is True and top["equipment"]["id"] == lab.fesem.pk
    assert _client(lab.student).get("/api/search/", {"q": "slot exchange"}).json()["actions"] == [] or all(
        a["key"] != "slot_exchange" for a in _client(lab.student).get("/api/search/", {"q": "slot exchange"}).json()["actions"])


def test_main_admin_switches_slot_exchange_and_others_cannot(lab):
    url = "/api/portal-actions/slot_exchange/run/"
    res = _client(lab.admin).post(url, {"equipment_id": lab.xrd.pk, "on": True}, format="json")
    assert res.status_code == 200 and res.json()["on"] is True
    lab.xrd.refresh_from_db()
    assert lab.xrd.slot_exchange_enabled is True
    assert _client(lab.oic).post(url, {"equipment_id": lab.xrd.pk, "on": False}, format="json").status_code == 404
    assert _client(lab.student).post(url, {"equipment_id": lab.xrd.pk, "on": False}, format="json").status_code == 404
    lab.xrd.refresh_from_db()
    assert lab.xrd.slot_exchange_enabled is True
    choices = _client(lab.admin).get("/api/portal-actions/slot_exchange/equipment/", {"q": "xrd"}).json()["equipment"]
    assert choices == [{"id": lab.xrd.pk, "name": lab.xrd.name, "code": lab.xrd.code, "on": True}]


# --- IITR Research Assistant -----------------------------------------------------------------------------------------

def _reply(resp):
    body = resp.json()
    return body["message"]["content"], _actions(body)


def test_assistant_asks_which_instrument_then_switches_after_the_button(lab):
    conv = _new_conv(lab, user=lab.admin)
    text, buttons = _reply(_post(lab, conv, "enable slot exchange", user=lab.admin))
    assert "which instrument" in text.lower()
    assert any(b.get("action_type") == "ba_portal" and b["payload"]["equipment_id"] == lab.fesem.pk for b in buttons)
    # Typing the instrument continues the same request.
    text, buttons = _reply(_post(lab, conv, "FESEM-01", user=lab.admin))
    assert "off" in text and "FESEM" in text
    turn_on = next(b for b in buttons if b.get("action_type") == "ba_portal")
    assert turn_on["payload"] == {"action": "slot_exchange", "equipment_id": lab.fesem.pk, "on": 1}
    lab.fesem.refresh_from_db()
    assert lab.fesem.slot_exchange_enabled is False  # nothing changes until the button is pressed
    resp = _post(lab, conv, turn_on["label"], action={"type": "ba_portal", "payload": turn_on["payload"]}, user=lab.admin)
    text, _ = _reply(resp)
    assert "now **on**" in text
    lab.fesem.refresh_from_db()
    assert lab.fesem.slot_exchange_enabled is True


def test_assistant_goes_straight_to_the_named_instrument_and_links_pages(lab):
    conv = _new_conv(lab, user=lab.oic)
    text, buttons = _reply(_post(lab, conv, "block slots on XRD-B1", user=lab.oic))
    link = next(b for b in buttons if b.get("href"))
    assert link["href"] == f"/change-slot-status?equipment_id={lab.xrd.pk}"
    conv = _new_conv(lab)
    text, buttons = _reply(_post(lab, conv, "where can I recharge my wallet"))
    assert any(b.get("href") == "/wallet/direct-recharge" for b in buttons) or "recharge" in text.lower()


def test_assistant_refuses_a_forged_switch_button(lab):
    conv = _new_conv(lab)
    payload = {"action": "slot_exchange", "equipment_id": lab.fesem.pk, "on": 1}
    text, _ = _reply(_post(lab, conv, "Turn on", action={"type": "ba_portal", "payload": payload}))
    assert "not available" in text
    lab.fesem.refresh_from_db()
    assert lab.fesem.slot_exchange_enabled is False
