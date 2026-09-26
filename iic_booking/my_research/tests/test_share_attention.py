import pytest

from iic_booking.equipment.pending_actions import collect_pending_actions

from .conftest import API, make_client

pytestmark = pytest.mark.django_db


def _keys(user):
    return {i["key"]: i for i in collect_pending_actions(user)}


def test_shared_workspace_is_pending_until_viewer_opens_it(student, faculty, workspace, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        resp = make_client(student).post(
            f"{API}/workspaces/{workspace['id']}/members/", {"user_id": faculty.id, "confirm": True}, format="json"
        )
    assert resp.status_code == 201, resp.data

    item = _keys(faculty)["workspaces_shared"]
    assert item["count"] == 1
    assert item["link"] == "/my-research"
    assert workspace["name"] in item["details"][0]

    assert make_client(student).get(f"{API}/workspaces/{workspace['id']}/").status_code == 200
    assert "workspaces_shared" in _keys(faculty)

    assert make_client(faculty).get(f"{API}/workspaces/{workspace['id']}/").status_code == 200
    assert "workspaces_shared" not in _keys(faculty)
