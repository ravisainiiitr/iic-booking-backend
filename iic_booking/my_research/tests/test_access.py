"""Eligibility and feature-flag gating for My Research."""

import pytest

from iic_booking.my_research.models import MemberRole, ResearchWorkspace, ResearchWorkspaceMember
from iic_booking.users.models.user_type import UserType

from .conftest import API, make_client, make_department, make_user

pytestmark = pytest.mark.django_db


def test_flag_defaults_to_disabled(monkeypatch):
    import environ

    monkeypatch.delenv("MY_RESEARCH_ENABLED", raising=False)
    source = open(__import__("config.settings.base", fromlist=["x"]).__file__, encoding="utf-8").read()
    assert 'MY_RESEARCH_ENABLED = env.bool("MY_RESEARCH_ENABLED", default=False)' in source
    assert environ.Env().bool("MY_RESEARCH_ENABLED", default=False) is False


def test_disabled_flag_hides_everything(settings, student):
    settings.MY_RESEARCH_ENABLED = False
    client = make_client(student)

    boot = client.get(f"{API}/bootstrap/")
    assert boot.status_code == 200
    assert boot.data == {"enabled": False, "eligible": True, "available": False}

    for method, path in [
        ("get", f"{API}/"),
        ("get", f"{API}/workspaces/"),
        ("post", f"{API}/workspaces/"),
    ]:
        resp = getattr(client, method)(path, {"name": "x"} if method == "post" else None, format="json")
        assert resp.status_code == 404, path
        assert resp.data["code"] == "my_research_disabled"
    assert ResearchWorkspace.objects.count() == 0


def test_disabled_flag_blocks_existing_workspace_urls(settings, student, workspace):
    settings.MY_RESEARCH_ENABLED = False
    resp = make_client(student).get(f"{API}/workspaces/{workspace['id']}/")
    assert resp.status_code == 404
    assert resp.data["code"] == "my_research_disabled"


def test_student_can_create_workspace(student):
    client = make_client(student)
    boot = client.get(f"{API}/bootstrap/")
    assert boot.data["available"] is True
    assert boot.data["can_create"] is True

    resp = client.post(f"{API}/workspaces/", {"name": "Graphene Thin Films", "description": "PhD"}, format="json")
    assert resp.status_code == 201
    assert resp.data["role"] == MemberRole.OWNER
    ws = ResearchWorkspace.objects.get(pk=resp.data["id"])
    assert ws.owner == student
    assert ResearchWorkspaceMember.objects.filter(workspace=ws, user=student, role=MemberRole.OWNER).exists()


def test_individual_student_is_eligible(internal_dept):
    user = make_user(user_type=UserType.INDIVIDUAL_STUDENT, department=internal_dept)
    assert make_client(user).post(f"{API}/workspaces/", {"name": "WS"}, format="json").status_code == 201


def test_internal_faculty_can_create_workspace(faculty):
    assert make_client(faculty).post(f"{API}/workspaces/", {"name": "Lab"}, format="json").status_code == 201


@pytest.mark.parametrize(
    "user_type",
    [
        UserType.EXTERNAL,
        UserType.RND,
        UserType.INSTITUTE,
        UserType.STARTUP_INCUBATED_IITR,
        UserType.EXTERNAL_STARTUP_MSME,
        UserType.OTHER,
    ],
)
def test_non_iitr_users_are_denied(user_type):
    user = make_user(user_type=user_type)
    client = make_client(user)
    boot = client.get(f"{API}/bootstrap/")
    assert boot.data["available"] is False
    assert boot.data["eligible"] is False

    for resp in (
        client.get(f"{API}/"),
        client.post(f"{API}/workspaces/", {"name": "Nope"}, format="json"),
    ):
        assert resp.status_code == 403
        assert resp.data["code"] == "my_research_not_eligible"
    assert not ResearchWorkspace.objects.filter(owner=user).exists()


def test_visiting_faculty_from_external_department_is_denied():
    visiting = make_user(user_type=UserType.FACULTY, department=make_department("external"))
    resp = make_client(visiting).post(f"{API}/workspaces/", {"name": "Visiting"}, format="json")
    assert resp.status_code == 403
    assert resp.data["code"] == "my_research_not_eligible"


def test_faculty_without_department_is_denied():
    user = make_user(user_type=UserType.FACULTY, department=None)
    assert make_client(user).get(f"{API}/").status_code == 403


def test_inactive_student_is_denied(internal_dept):
    user = make_user(user_type=UserType.STUDENT, department=internal_dept, force_inactive=True)
    assert user.is_active is False
    assert make_client(user).get(f"{API}/").status_code == 403


def test_unauthenticated_requests_are_rejected(workspace):
    client = make_client()
    for path in (f"{API}/bootstrap/", f"{API}/", f"{API}/workspaces/{workspace['id']}/"):
        assert client.get(path).status_code in (401, 403), path


def test_external_user_cannot_reach_existing_workspace_by_url(external_user, workspace):
    """Direct API calls by non-eligible users are refused before any workspace lookup."""
    client = make_client(external_user)
    ws = workspace["id"]
    for path in (
        f"{API}/workspaces/{ws}/",
        f"{API}/workspaces/{ws}/files/",
        f"{API}/workspaces/{ws}/folders/",
        f"{API}/workspaces/{ws}/members/",
    ):
        resp = client.get(path)
        assert resp.status_code == 403, path
        assert resp.data["code"] == "my_research_not_eligible"


def test_user_who_lost_eligibility_loses_shared_access(student, faculty, workspace):
    ResearchWorkspaceMember.objects.create(
        workspace_id=workspace["id"], user=faculty, role=MemberRole.VIEWER, added_by=student
    )
    assert make_client(faculty).get(f"{API}/workspaces/{workspace['id']}/").status_code == 200
    faculty.department = make_department("external")
    faculty.save(update_fields=["department"])
    assert make_client(faculty).get(f"{API}/workspaces/{workspace['id']}/").status_code == 403


def test_pilot_list_limits_creation_but_not_shared_viewing(settings, student, faculty, workspace):
    settings.MY_RESEARCH_PILOT_EMAILS = student.email.upper()
    assert make_client(student).post(f"{API}/workspaces/", {"name": "Second"}, format="json").status_code == 201

    denied = make_client(faculty).post(f"{API}/workspaces/", {"name": "Not pilot"}, format="json")
    assert denied.status_code == 403
    assert denied.data["code"] == "my_research_pilot_only"
    assert make_client(faculty).get(f"{API}/bootstrap/").data["can_create"] is False

    ResearchWorkspaceMember.objects.create(
        workspace_id=workspace["id"], user=faculty, role=MemberRole.VIEWER, added_by=student
    )
    assert make_client(faculty).get(f"{API}/workspaces/{workspace['id']}/").status_code == 200


def test_workspace_name_validation(student):
    client = make_client(student)
    assert client.post(f"{API}/workspaces/", {"name": "  "}, format="json").status_code == 400
    assert client.post(f"{API}/workspaces/", {"name": "x" * 201}, format="json").status_code == 400


def test_control_characters_are_stripped_from_free_text(student):
    client = make_client(student)
    resp = client.post(
        f"{API}/workspaces/", {"name": "Nano\x00coat\x1f", "description": "line one\nline\x00 two\t."}, format="json"
    )
    assert resp.status_code == 201, resp.data
    workspace = ResearchWorkspace.objects.get(pk=resp.data["id"])
    assert workspace.name == "Nanocoat"
    assert workspace.description == "line one\nline two\t."

    patched = client.patch(f"{API}/workspaces/{workspace.pk}/", {"name": "Re\x00named"}, format="json")
    assert patched.status_code == 200
    workspace.refresh_from_db()
    assert workspace.name == "Renamed"

    assert client.get(f"{API}/workspaces/{workspace.pk}/search/", {"q": "ab\x00c"}).status_code == 200
    assert client.get(f"{API}/workspaces/{workspace.pk}/linkable-bookings/", {"q": "\x00x"}).status_code == 200
