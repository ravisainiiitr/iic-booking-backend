"""Equipment flash messages: permissions, scheduling window, audience, sanitising, cache and query budget."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.core.cache import cache
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.equipment.flash_message_service import public_flash_messages
from iic_booking.equipment.flash_message_service import sanitize_flash_html
from iic_booking.equipment.models import EquipmentFlashMessage
from iic_booking.equipment.models import EquipmentFlashMessageAudit
from iic_booking.equipment.models import EquipmentTemporaryOIC
from iic_booking.equipment.serializers import EquipmentDetailSerializer
from iic_booking.equipment.tests.test_disruption_log import _client
from iic_booking.equipment.tests.test_disruption_log import _department
from iic_booking.equipment.tests.test_disruption_log import _equipment
from iic_booking.equipment.tests.test_disruption_log import _oic_for
from iic_booking.equipment.tests.test_disruption_log import _user
from iic_booking.users.models.user_type import UserType

pytestmark = pytest.mark.django_db

URL = "/api/equipments/flash-messages/"


@pytest.fixture(autouse=True)
def _clear_cache():
    cache.clear()
    yield
    cache.clear()


def _payload(eq, **extra):
    data = {
        "equipment": eq.pk,
        "message": "Sample submission closes at <b>4 PM</b> today",
        "tone": "NOTICE",
        "end_at": (timezone.now() + timedelta(days=1)).isoformat(),
    }
    data.update(extra)
    return data


def _create(user, eq, **extra):
    return _client(user).post(URL, _payload(eq, **extra), format="json")


def _msg(eq, **kwargs):
    now = timezone.now()
    defaults = {
        "message": "Hello",
        "start_at": now - timedelta(hours=1),
        "end_at": now + timedelta(days=1),
    }
    defaults.update(kwargs)
    return EquipmentFlashMessage.objects.create(equipment=eq, **defaults)


class _Anon:
    is_authenticated = False


def test_permission_matrix():
    dept, other_dept = _department("Chem"), _department("Phys")
    mine, covering, others = (
        _equipment(internal_department=dept),
        _equipment(internal_department=other_dept),
        _equipment(internal_department=other_dept),
    )
    oic = _oic_for(mine)
    EquipmentTemporaryOIC.objects.create(
        equipment=covering, primary_oic=_user(UserType.MANAGER), temporary_oic=oic,
        resume_at=timezone.now() + timedelta(days=2),
    )
    dept_admin = _user(UserType.DEPT_ADMIN, department=dept)
    admin = _user(UserType.ADMIN)

    for role in (UserType.OPERATOR, UserType.STUDENT, UserType.FACULTY, UserType.FINANCE):
        user = _user(role)
        assert _create(user, mine).status_code == 403
        assert _client(user).get(URL).status_code == 403
    assert APIClient().get(URL).status_code in (401, 403)

    assert _create(oic, others).status_code == 403
    assert _create(dept_admin, others).status_code == 403
    assert _create(oic, mine).status_code == 201
    assert _create(oic, covering).status_code == 201  # active temporary OIC
    assert _create(dept_admin, mine).status_code == 201
    assert _create(admin, others).status_code == 201

    other_msg = EquipmentFlashMessage.objects.filter(equipment=others).first()
    assert _client(oic).patch(f"{URL}{other_msg.pk}/", {"tone": "INFO"}, format="json").status_code == 403
    assert _client(oic).post(f"{URL}{other_msg.pk}/end/", {}, format="json").status_code == 403
    assert _client(dept_admin).post(f"{URL}{other_msg.pk}/extend/", {"days": 1}, format="json").status_code == 403
    assert _client(admin).get(f"{URL}999999/").status_code == 404

    assert {r["equipment_id"] for r in _client(oic).get(URL).data["results"]} == {mine.pk, covering.pk}
    assert {r["equipment_id"] for r in _client(dept_admin).get(URL).data["results"]} == {mine.pk}
    assert len(_client(admin).get(URL).data["results"]) == 4

    detail = _client(admin).get(f"{URL}{other_msg.pk}/").data
    assert detail["history"][0]["action"] == "created" and detail["history"][0]["actor_role"] == "MAIN_ADMIN"
    roles = set(EquipmentFlashMessageAudit.objects.values_list("actor_role", flat=True))
    assert {"OIC", "TEMP_OIC", "DEPT_ADMIN", "MAIN_ADMIN"} <= roles


def test_can_manage_flag_on_detail_payload():
    dept = _department("Chem")
    eq = _equipment(internal_department=dept)
    oic = _oic_for(eq)

    class _Req:
        def __init__(self, user):
            self.user = user

    def flag(user):
        return EquipmentDetailSerializer(context={"request": _Req(user)}).get_flash_messages_can_manage(eq)

    assert flag(oic) is True
    assert flag(_user(UserType.ADMIN)) is True
    assert flag(_user(UserType.DEPT_ADMIN, department=dept)) is True
    assert flag(_user(UserType.MANAGER)) is False
    assert flag(_user(UserType.STUDENT)) is False
    assert flag(_Anon()) is False


def test_validation_schedule_limits_and_sanitising():
    eq = _equipment()
    admin = _user(UserType.ADMIN)
    now = timezone.now()

    assert _create(admin, eq, end_at="").status_code == 400
    assert _create(admin, eq, end_at=(now - timedelta(minutes=5)).isoformat()).status_code == 400
    assert _create(admin, eq, end_at=(now + timedelta(days=31)).isoformat()).status_code == 400
    assert _create(admin, eq, message="x" * 301).status_code == 400
    assert _create(admin, eq, message="<b> </b>").status_code == 400
    assert _create(admin, eq, tone="LOUD").status_code == 400
    assert _create(admin, eq, audience="USER_TYPES", audience_user_types=[]).status_code == 400
    assert _create(admin, eq, link_url="javascript:alert(1)").status_code == 400
    assert _create(admin, eq, end_at=(now + timedelta(days=30)).isoformat()).status_code == 201

    res = _create(
        admin, eq,
        message='<p><script>alert(1)</script><b>Bold</b> <i>it</i> <u>u</u> <a href="javascript:x">bad</a> '
                '<a href="https://iitr.ac.in" onclick="x">ok</a><img src=x onerror=1></p>',
        link_url="https://iitr.ac.in/form", link_label="",
        show_on_modes=True,
    )
    assert res.status_code == 201
    html = res.data["message"]
    assert "<script" not in html and "onclick" not in html and "<img" not in html and "<u>" not in html
    assert "<strong>Bold</strong>" in html and "<em>it</em>" in html
    assert 'href="https://iitr.ac.in"' in html and "javascript" not in html
    assert res.data["link_label"] == "Learn more"
    assert res.data["show_on_modes"] is False  # not a multi-mode base instrument
    assert sanitize_flash_html("a < b & c") == "a &lt; b &amp; c"


def test_status_window_end_now_and_extend():
    eq = _equipment()
    admin = _user(UserType.ADMIN)
    now = timezone.now()
    live = _msg(eq, message="live")
    scheduled = _msg(eq, message="later", start_at=now + timedelta(days=1), end_at=now + timedelta(days=2))
    _msg(eq, message="past", start_at=now - timedelta(days=2), end_at=now - timedelta(days=1))
    _msg(eq, message="off", is_active=False)

    data = _client(admin).get(URL, {"with_options": 1}).data
    assert data["summary"] == {"live": 1, "scheduled": 1, "expired": 1, "off": 1}
    assert [r["status"] for r in data["results"]] == ["LIVE", "SCHEDULED", "OFF", "EXPIRED"]
    assert data["limits"]["message_max_chars"] == 300
    assert [r["message_plain"] for r in _client(admin).get(URL, {"status": "scheduled"}).data["results"]] == ["later"]

    assert [m["message"] for m in public_flash_messages(eq, _Anon())] == ["live"]

    res = _client(admin).post(f"{URL}{live.pk}/end/", {}, format="json")
    assert res.status_code == 200 and res.data["status"] == "EXPIRED"
    assert public_flash_messages(eq, _Anon()) == []  # cache invalidated on save
    assert _client(admin).post(f"{URL}{live.pk}/end/", {}, format="json").status_code == 400

    res = _client(admin).post(f"{URL}{live.pk}/extend/", {"days": 3}, format="json")
    assert res.status_code == 200 and res.data["status"] == "LIVE"
    assert [m["message"] for m in public_flash_messages(eq, _Anon())] == ["live"]
    assert _client(admin).post(f"{URL}{live.pk}/extend/", {"days": 45}, format="json").status_code == 400

    res = _client(admin).post(f"{URL}{scheduled.pk}/end/", {}, format="json")
    assert res.data["status"] == "EXPIRED"
    assert [h["action"] for h in _client(admin).get(f"{URL}{live.pk}/").data["history"]] == ["ended", "extended"]

    res = _client(admin).patch(f"{URL}{live.pk}/", {"is_active": False}, format="json")
    assert res.data["status"] == "OFF" and public_flash_messages(eq, _Anon()) == []


def test_audience_filtering():
    eq = _equipment()
    _msg(eq, message="all")
    _msg(eq, message="internal", audience="INTERNAL")
    _msg(eq, message="external", audience="EXTERNAL")
    _msg(eq, message="students", audience="USER_TYPES", audience_user_types=["student"])

    def seen(user):
        return {m["message"] for m in public_flash_messages(eq, user)}

    assert seen(_Anon()) == {"all"}
    assert seen(_user(UserType.STUDENT)) == {"all", "internal", "students"}
    assert seen(_user(UserType.INDIVIDUAL_STUDENT)) == {"all", "internal", "students"}
    assert seen(_user(UserType.FACULTY)) == {"all", "internal"}
    assert seen(_user(UserType.INSTITUTE)) == {"all", "external"}
    staff_view = public_flash_messages(eq, _oic_for(eq))
    assert {m["message"] for m in staff_view} == {"all", "internal", "external", "students"}
    assert all("audience_display" in m for m in staff_view)
    assert all("audience" not in m for m in public_flash_messages(eq, _user(UserType.STUDENT)))


def test_base_instrument_messages_show_on_modes_when_ticked():
    base = _equipment(enable_multi_mode=True)
    mode = _equipment(parent_equipment=base, internal_department=base.internal_department)
    admin = _user(UserType.ADMIN)
    assert _create(admin, base, message="family wide", show_on_modes=True).data["show_on_modes"] is True
    _create(admin, base, message="base only")
    _msg(mode, message="mode own")

    assert [m["message"] for m in public_flash_messages(mode, _Anon())] == ["mode own", "family wide"]
    assert [m["from_base_instrument"] for m in public_flash_messages(mode, _Anon())] == [False, True]
    assert {m["message"] for m in public_flash_messages(base, _Anon())} == {"family wide", "base only"}


def test_detail_payload_query_budget_and_cache(django_assert_max_num_queries):
    base = _equipment(enable_multi_mode=True)
    mode = _equipment(parent_equipment=base, internal_department=base.internal_department)
    for i in range(5):
        _msg(mode, message=f"m{i}")
        _msg(base, message=f"b{i}", show_on_modes=True)
    cache.clear()
    with django_assert_max_num_queries(2):
        assert len(public_flash_messages(mode, _Anon())) == 10
    with django_assert_max_num_queries(0):
        assert len(public_flash_messages(mode, _Anon())) == 10

    res = _client(_user(UserType.STUDENT)).get(f"/api/equipments/{mode.pk}/")
    assert res.status_code == 200, res.status_code
    assert len(res.data["flash_messages"]) == 10
    assert res.data["flash_messages_can_manage"] is False
