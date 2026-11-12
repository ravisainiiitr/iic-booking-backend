"""M8: staff tools and the equipment page Discussions panel (and the public page staying free of Connect)."""

import json

import pytest

from iic_booking.connect.models import Community
from iic_booking.connect.models import DiscussionThread
from iic_booking.connect.models import ModerationAction
from iic_booking.connect.services import membership as msvc
from iic_booking.equipment.models import Equipment
from iic_booking.equipment.models import EquipmentManager
from iic_booking.equipment.models import EquipmentOperator
from iic_booking.users.models.user_type import UserType

from .conftest import API
from .conftest import client_for
from .conftest import make_booking
from .conftest import make_equipment
from .conftest import make_external
from .conftest import make_group
from .conftest import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def e(connect_on):
    g, other_g = make_group("Spectroscopy"), make_group("Rheology")
    eq = make_equipment(g, name="FTIR")
    eq_other = make_equipment(other_g, name="Rheometer")
    ungrouped = make_equipment(None, name="Ungrouped Balance")
    member, outsider, op, oic = make_user(), make_user(), make_user(UserType.OPERATOR), make_user(UserType.MANAGER)
    ext = make_external()
    make_booking(member, eq)
    make_booking(ext, eq)
    make_booking(outsider, eq_other)
    EquipmentOperator.objects.create(equipment=eq, operator=op)
    EquipmentManager.objects.create(equipment=ungrouped, manager=oic)
    msvc.sync_all(dry_run=False)
    return {"c": Community.objects.get(equipment_group_ref_id=g.pk), "eq": eq, "eq_other": eq_other, "ungrouped": ungrouped,
            "member": member, "outsider": outsider, "op": op, "oic": oic, "ext": ext}


def thread(user, community, title, **extra):
    data = {"community": community if isinstance(community, str) else community.slug, "title": title, "body": f"<p>{title}</p>", **extra}
    res = client_for(user).post(f"{API}/threads/", data, format="json")
    assert res.status_code == 201, res.content
    return res.json()["id"]


def panel(user, equipment):
    return client_for(user).get(f"{API}/equipment/{equipment.pk}/discussions/")


# --- Staff tools ---------------------------------------------------------------------------------------------------


def test_staff_flags_are_moderator_only_audited_and_never_touch_equipment(e):
    tid = thread(e["member"], e["c"], "FTIR baseline problem again", type="technical_issue", equipment_id=e["eq"].pk)
    url = f"{API}/threads/{tid}/staff/"
    assert client_for(e["member"]).patch(url, {"is_pinned": True}, format="json").status_code == 403
    assert client_for(e["outsider"]).patch(url, {"is_pinned": True}, format="json").status_code == 404
    status_before = Equipment.objects.get(pk=e["eq"].pk).status
    res = client_for(e["op"]).patch(url, {"is_pinned": True, "is_known_problem": True, "has_workaround": True, "resolved": True, "reason": "Recurring"}, format="json")
    assert res.status_code == 200
    body = res.json()
    assert body["is_pinned"] and body["is_known_problem"] and body["has_workaround"] and body["marked_resolved_at"]
    row = ModerationAction.objects.get(action="staff_flags", target_id=str(tid))
    assert row.after == {"is_pinned": True, "is_known_problem": True, "has_workaround": True, "resolved": True} and row.reason == "Recurring"
    assert Equipment.objects.get(pk=e["eq"].pk).status == status_before
    client_for(e["op"]).patch(url, {"is_outdated": True, "is_pinned": False}, format="json")
    t = DiscussionThread.objects.get(pk=tid)
    assert t.is_outdated and not t.is_pinned and t.pinned_at is None


def test_pinned_threads_come_first_in_the_community(e):
    first = thread(e["member"], e["c"], "Older thread to be pinned")
    thread(e["member"], e["c"], "Newer ordinary thread here")
    client_for(e["op"]).patch(f"{API}/threads/{first}/staff/", {"is_pinned": True}, format="json")
    ids = [t["id"] for t in client_for(e["member"]).get(f"{API}/threads/?community={e['c'].slug}").json()["results"]]
    assert ids[0] == first


# --- Equipment panel -----------------------------------------------------------------------------------------------


def test_member_sees_sections_for_the_equipment(e):
    q = thread(e["member"], e["c"], "FTIR detector noise question", type="question", equipment_id=e["eq"].pk)
    thread(e["member"], e["c"], "Unrelated group chat thread")
    ann = thread(e["op"], e["c"], "FTIR maintenance next week", type="announcement", equipment_id=e["eq"].pk)
    known = thread(e["member"], e["c"], "FTIR purge leak known issue", type="technical_issue", equipment_id=e["eq"].pk)
    client_for(e["op"]).patch(f"{API}/threads/{known}/staff/", {"is_known_problem": True}, format="json")
    res = panel(e["member"], e["eq"])
    assert res.status_code == 200
    body = res.json()
    assert body["available"] and body["community"]["slug"] == e["c"].slug and body["can_post"]
    assert {t["id"] for t in body["recent"]} == {q, ann, known}
    assert q in [t["id"] for t in body["questions"]]
    assert [t["id"] for t in body["known_problems"]] == [known]
    assert [t["id"] for t in body["announcements"]] == [ann]
    assert body["counts"]["threads"] == 3
    assert "availability" in body["note"]


def test_non_member_gets_no_threads(e):
    thread(e["member"], e["c"], "FTIR private group question", equipment_id=e["eq"].pk)
    res = panel(e["outsider"], e["eq"])
    assert res.status_code == 200
    body = res.json()
    assert body["available"] is False and body["reason"] == "members_only"
    assert "recent" not in body and "counts" not in body


def test_external_member_sees_group_panel(e):
    thread(e["member"], e["c"], "FTIR question for everyone", equipment_id=e["eq"].pk)
    assert panel(e["ext"], e["eq"]).json()["available"] is True


def test_invisible_equipment_is_404(e):
    hidden = make_equipment(None, name="Test-only rig", visible_to_test_accounts_only=True)
    assert panel(e["member"], hidden).status_code == 404
    assert client_for(e["member"]).get(f"{API}/equipment/999999/discussions/").status_code == 404


def test_ungrouped_equipment_uses_general_instrumentation(e):
    res = panel(e["member"], e["ungrouped"]).json()
    assert res["available"] and res["community"]["kind"] == "general" and res["category"]["slug"] == "general-instrumentation"
    ext = panel(e["ext"], e["ungrouped"]).json()
    assert ext["available"] is False and ext["reason"] == "internal_only"


def test_oic_badge_in_general_for_their_instrument(e):
    tid = thread(e["oic"], "general", "Balance calibration schedule", equipment_id=e["ungrouped"].pk)
    detail = client_for(e["member"]).get(f"{API}/threads/{tid}/").json()
    assert detail["author"]["badge"] == "Officer-in-Charge" and detail["opening_post"]["is_verified_response"]
    other = thread(e["oic"], "general", "A general question without equipment")
    assert client_for(e["member"]).get(f"{API}/threads/{other}/").json()["author"]["badge"] == ""


def test_flag_off_and_unauthenticated(e, settings):
    assert client_for().get(f"{API}/equipment/{e['eq'].pk}/discussions/").status_code in (401, 403)
    settings.IITR_CONNECT_ENABLED = False
    assert panel(e["member"], e["eq"]).status_code == 404


def test_public_equipment_page_never_carries_connect_content(e):
    thread(e["member"], e["c"], "Secret FTIR group discussion", equipment_id=e["eq"].pk)
    res = client_for().get(f"/api/equipments/{e['eq'].pk}/public/")
    assert res.status_code == 200, res.content
    text = json.dumps(res.json()).lower()
    assert "secret ftir" not in text and "discussion" not in text and "connect" not in text
