"""My shortcuts (Module 7): each user's own groups of dashboard buttons, saved per user, never affecting others or
the OIC/Admin menu layout."""

import pytest
from rest_framework.test import APIClient

from iic_booking.users.dashboard_shortcuts import MAX_GROUPS, SHORTCUTS_KEY
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db

URL = "/api/profiles/me/dashboard-shortcuts/"


def _client(user):
    c = APIClient()
    c.force_authenticate(user)
    return c


def _group(gid, name="Daily", items=("browse_equipment",), icon="calendar", collapsed=False):
    return {"id": gid, "name": name, "icon": icon, "items": list(items), "collapsed": collapsed}


@pytest.mark.parametrize("user_type", [UserType.STUDENT, UserType.FACULTY, UserType.EXTERNAL])
def test_internal_and_external_users_save_and_read_their_groups(user_type):
    user = UserFactory(user_type=user_type)
    body = {"groups": [_group("g1", items=["browse_equipment", "view_bookings"]),
                       _group("g2", "Money", ["wallet_management"], "wallet", collapsed=True)]}
    saved = _client(user).put(URL, body, format="json")
    assert saved.status_code == 200, saved.data
    got = _client(user).get(URL).json()["groups"]
    assert [g["id"] for g in got] == ["g1", "g2"]  # group order kept
    assert got[0]["items"] == ["browse_equipment", "view_bookings"] and got[1]["collapsed"] is True


def test_one_users_groups_never_reach_another_user():
    a, b = UserFactory(user_type=UserType.STUDENT), UserFactory(user_type=UserType.STUDENT)
    _client(a).put(URL, {"groups": [_group("mine")]}, format="json")
    assert _client(b).get(URL).json() == {"groups": []}


def test_a_button_appears_in_one_group_only_and_unknown_icons_fall_back():
    user = UserFactory(user_type=UserType.STUDENT)
    body = {"groups": [_group("g1", items=["view_bookings", "view_bookings"]),
                       _group("g2", "Again", ["view_bookings", "view_results"], "rocket")]}
    groups = _client(user).put(URL, body, format="json").json()["groups"]
    assert groups[0]["items"] == ["view_bookings"] and groups[1]["items"] == ["view_results"]
    assert groups[1]["icon"] == "star"


@pytest.mark.parametrize("body,message", [
    ({"groups": [_group("g1"), _group("g1")]}, "unique id"),
    ({"groups": [_group("g1", name="   ")]}, "needs a name"),
    ({"groups": [_group(f"g{i}") for i in range(MAX_GROUPS + 1)]}, "At most"),
    ({"groups": "nope"}, "must be a list"),
    ({"groups": [_group("bad id!")]}, "unique id"),
])
def test_invalid_layouts_are_refused(body, message):
    res = _client(UserFactory(user_type=UserType.STUDENT)).put(URL, body, format="json")
    assert res.status_code == 400 and message in res.json()["error"]


def test_restore_default_clears_the_groups_and_keeps_the_rest_of_the_layout():
    user = UserFactory(user_type=UserType.MANAGER)
    user.dashboard_menu_layout = {"groups": [], "order": ["x"], "equipment_order": [5]}
    user.save(update_fields=["dashboard_menu_layout"])
    _client(user).put(URL, {"groups": [_group("g1")]}, format="json")
    assert _client(user).delete(URL).json() == {"groups": []}
    user.refresh_from_db()
    assert SHORTCUTS_KEY not in user.dashboard_menu_layout
    assert user.dashboard_menu_layout["equipment_order"] == [5] and user.dashboard_menu_layout["order"] == ["x"]


def test_saving_the_oic_menu_keeps_the_shortcut_groups():
    oic = UserFactory(user_type=UserType.MANAGER)
    _client(oic).put(URL, {"groups": [_group("g1")]}, format="json")
    res = _client(oic).put("/api/profiles/me/dashboard-menu-layout/", {"groups": [], "order": ["a"]}, format="json")
    assert res.status_code == 200
    oic.refresh_from_db()
    assert oic.dashboard_menu_layout[SHORTCUTS_KEY][0]["id"] == "g1"


def test_signed_out_visitors_cannot_read_or_save():
    assert APIClient().get(URL).status_code in (401, 403)
    assert APIClient().put(URL, {"groups": []}, format="json").status_code in (401, 403)
