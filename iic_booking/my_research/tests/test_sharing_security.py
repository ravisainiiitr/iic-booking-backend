"""Viewer sharing, read-only enforcement, revoke, and cross-workspace (IDOR) protection."""

import pytest

from iic_booking.my_research import views
from iic_booking.my_research.models import (
    ActivityAction,
    FileStatus,
    MemberRole,
    ResearchActivity,
    ResearchFile,
    ResearchFolder,
    ResearchWorkspaceMember,
)
from iic_booking.users.models.user_type import UserType

from .conftest import API, make_booking, make_claim, make_client, make_department, make_user, upload_file

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.7\n" + b"0" * 200


@pytest.fixture
def notifications(monkeypatch):
    calls = {"added": [], "removed": []}
    monkeypatch.setattr(views, "notify_viewer_added", lambda m: calls["added"].append(m.user_id))
    monkeypatch.setattr(views, "notify_viewer_removed", lambda m: calls["removed"].append(m.user_id))
    return calls


def _share(owner, workspace_id, user, confirm=True):
    return make_client(owner).post(
        f"{API}/workspaces/{workspace_id}/members/", {"user_id": user.pk, "confirm": confirm}, format="json"
    )


@pytest.fixture
def shared_setup(student, faculty, workspace, fake_s3, notifications, django_capture_on_commit_callbacks):
    owner = make_client(student)
    ws = workspace["id"]
    folder = owner.post(f"{API}/workspaces/{ws}/folders/", {"name": "SEM Images"}, format="json").data
    file_id, done = upload_file(owner, fake_s3, ws, "sample.pdf", PDF, folder_id=folder["id"])
    assert done.status_code == 200, done.data
    with django_capture_on_commit_callbacks(execute=True):
        resp = _share(student, ws, faculty)
    assert resp.status_code == 201, resp.data
    return {"ws": ws, "folder": folder, "file_id": file_id, "member_id": resp.data["id"]}


# ---------------------------------------------------------------- sharing


def test_share_requires_explicit_confirmation(student, faculty, workspace, notifications):
    resp = _share(student, workspace["id"], faculty, confirm=False)
    assert resp.status_code == 400
    assert resp.data["code"] == "confirmation_required"
    assert not ResearchWorkspaceMember.objects.filter(user=faculty).exists()


def test_owner_shares_with_faculty_and_viewer_is_notified(shared_setup, faculty, notifications):
    assert notifications["added"] == [faculty.pk]
    member = ResearchWorkspaceMember.objects.get(pk=shared_setup["member_id"])
    assert member.role == MemberRole.VIEWER
    assert ResearchActivity.objects.filter(
        workspace_id=shared_setup["ws"], action=ActivityAction.MEMBER_ADDED, target_id=str(faculty.pk)
    ).exists()

    home = make_client(faculty).get(f"{API}/")
    assert [w["id"] for w in home.data["shared_with_me"]] == [shared_setup["ws"]]
    assert home.data["shared_with_me"][0]["role"] == MemberRole.VIEWER
    assert home.data["my_workspaces"] == []


@pytest.mark.parametrize(
    "user_type,dept_type",
    [(UserType.EXTERNAL, None), (UserType.STARTUP_INCUBATED_IITR, None), (UserType.FACULTY, "external")],
)
def test_cannot_share_with_non_iitr_users(student, workspace, notifications, user_type, dept_type):
    target = make_user(user_type=user_type, department=make_department(dept_type) if dept_type else None)
    resp = _share(student, workspace["id"], target)
    assert resp.status_code == 400
    assert resp.data["code"] == "recipient_not_eligible"
    assert notifications["added"] == []


def test_viewer_notification_content(monkeypatch, student, faculty, workspace):
    from iic_booking.communication import service, styled_transactional_emails
    from iic_booking.my_research.services import notify_viewer_added

    pushes, emails = [], []
    monkeypatch.setattr(service.CommunicationService, "send_push_notification", staticmethod(lambda **kw: pushes.append(kw)))
    monkeypatch.setattr(styled_transactional_emails, "_send", lambda to, subject, text, html: emails.append((to, subject, text, html)))
    member = ResearchWorkspaceMember.objects.create(
        workspace_id=workspace["id"], user=faculty, role=MemberRole.VIEWER, added_by=student
    )
    notify_viewer_added(member)

    assert pushes[0]["recipient"] == faculty
    assert pushes[0]["title"] == "You have been given read-only access to a Research Workspace"
    assert pushes[0]["metadata"]["link"].endswith(f"/my-research/{workspace['id']}")
    to, subject, text, html = emails[0]
    assert to == faculty.email
    assert workspace["name"] in text
    assert "Read-only" in html and "Open Workspace" in html


def test_cannot_share_with_self_or_twice(student, faculty, workspace, notifications):
    assert _share(student, workspace["id"], student).status_code == 400
    assert _share(student, workspace["id"], faculty).status_code == 201
    dup = _share(student, workspace["id"], faculty)
    assert dup.status_code == 409
    assert dup.data["code"] == "already_member"


def test_viewer_can_read_everything(shared_setup, faculty, fake_s3):
    viewer = make_client(faculty)
    ws, folder_id, file_id = shared_setup["ws"], shared_setup["folder"]["id"], shared_setup["file_id"]

    detail = viewer.get(f"{API}/workspaces/{ws}/")
    assert detail.status_code == 200
    assert detail.data["role"] == MemberRole.VIEWER
    assert detail.data["permissions"]["read_only"] is True
    assert detail.data["permissions"]["can_upload"] is False
    assert detail.data["permissions"]["can_share"] is False

    assert viewer.get(f"{API}/workspaces/{ws}/folders/").status_code == 200
    assert viewer.get(f"{API}/folders/{folder_id}/").status_code == 200
    files = viewer.get(f"{API}/workspaces/{ws}/files/", {"folder": folder_id})
    assert [f["id"] for f in files.data["results"]] == [file_id]
    assert viewer.get(f"{API}/files/{file_id}/").status_code == 200
    for path in ("members/", "activity/", "bookings/", "equipment/", "publications/"):
        assert viewer.get(f"{API}/workspaces/{ws}/{path}").status_code == 200, path
    assert viewer.get(f"{API}/workspaces/{ws}/search/", {"q": "sample"}).data["files"][0]["id"] == file_id

    download = viewer.post(f"{API}/files/{file_id}/download/", {"disposition": "inline"}, format="json")
    assert download.status_code == 200
    assert download.data["disposition"] == "inline"
    assert download.data["expires_in"] == 300
    assert fake_s3.presigned[-1]["op"] == "get_object"
    assert fake_s3.presigned[-1]["expires"] == 300


def test_viewer_cannot_modify_anything(shared_setup, faculty, student, equipment):
    viewer = make_client(faculty)
    ws, folder_id, file_id = shared_setup["ws"], shared_setup["folder"]["id"], shared_setup["file_id"]
    other = make_user(user_type=UserType.STUDENT, department=faculty.department)
    booking = make_booking(student, equipment)
    claim = make_claim(student)

    attempts = [
        viewer.patch(f"{API}/workspaces/{ws}/", {"name": "Hijack"}, format="json"),
        viewer.post(f"{API}/workspaces/{ws}/archive/", {}, format="json"),
        viewer.post(f"{API}/workspaces/{ws}/folders/", {"name": "New"}, format="json"),
        viewer.patch(f"{API}/folders/{folder_id}/", {"name": "Renamed"}, format="json"),
        viewer.delete(f"{API}/folders/{folder_id}/"),
        viewer.post(f"{API}/workspaces/{ws}/uploads/initiate/", {"filename": "x.pdf", "size": 10}, format="json"),
        viewer.patch(f"{API}/files/{file_id}/", {"name": "renamed.pdf"}, format="json"),
        viewer.delete(f"{API}/files/{file_id}/"),
        viewer.post(f"{API}/workspaces/{ws}/members/", {"user_id": other.pk, "confirm": True}, format="json"),
        viewer.delete(f"{API}/workspaces/{ws}/members/{shared_setup['member_id']}/"),
        viewer.post(f"{API}/workspaces/{ws}/bookings/", {"booking_ids": [booking.booking_id]}, format="json"),
        viewer.get(f"{API}/workspaces/{ws}/linkable-bookings/"),
        viewer.post(f"{API}/workspaces/{ws}/publications/", {"claim_ids": [claim.claim_id]}, format="json"),
        viewer.get(f"{API}/workspaces/{ws}/linkable-publications/"),
    ]
    for resp in attempts:
        assert resp.status_code == 403, (resp.status_code, resp.data)
        assert resp.data["code"] == "read_only"

    assert ResearchFile.objects.get(pk=file_id).display_name == "sample.pdf"
    assert ResearchFile.objects.get(pk=file_id).status == FileStatus.AVAILABLE
    assert ResearchFolder.objects.get(pk=folder_id).name == "SEM Images"
    assert not ResearchWorkspaceMember.objects.filter(user=other).exists()


def test_viewer_cannot_complete_owner_upload(shared_setup, student, faculty, fake_s3):
    init = make_client(student).post(
        f"{API}/workspaces/{shared_setup['ws']}/uploads/initiate/", {"filename": "r.pdf", "size": len(PDF)}, format="json"
    )
    file_id = init.data["file"]["id"]
    viewer = make_client(faculty)
    assert viewer.post(f"{API}/uploads/{file_id}/complete/", {}, format="json").status_code in (403, 404)
    assert viewer.post(f"{API}/uploads/{file_id}/abort/", {}, format="json").status_code in (403, 404)
    assert ResearchFile.objects.get(pk=file_id).status == FileStatus.PENDING_UPLOAD


def test_revoke_removes_access_immediately(shared_setup, student, faculty, notifications, django_capture_on_commit_callbacks):
    ws, folder_id, file_id = shared_setup["ws"], shared_setup["folder"]["id"], shared_setup["file_id"]
    with django_capture_on_commit_callbacks(execute=True):
        resp = make_client(student).delete(f"{API}/workspaces/{ws}/members/{shared_setup['member_id']}/")
    assert resp.status_code == 200
    assert notifications["removed"] == [faculty.pk]
    member = ResearchWorkspaceMember.objects.get(pk=shared_setup["member_id"])
    assert member.revoked_at is not None and member.revoked_by == student

    viewer = make_client(faculty)
    assert viewer.get(f"{API}/workspaces/{ws}/").status_code == 404
    assert viewer.get(f"{API}/folders/{folder_id}/").status_code == 404
    assert viewer.get(f"{API}/files/{file_id}/").status_code == 404
    assert viewer.post(f"{API}/files/{file_id}/download/", {}, format="json").status_code == 404
    assert viewer.get(f"{API}/").data["shared_with_me"] == []

    # Re-sharing after revoke creates a fresh active membership.
    assert _share(student, ws, faculty).status_code == 201


def test_owner_cannot_be_removed(student, workspace):
    owner_member = ResearchWorkspaceMember.objects.get(workspace_id=workspace["id"], role=MemberRole.OWNER)
    resp = make_client(student).delete(f"{API}/workspaces/{workspace['id']}/members/{owner_member.pk}/")
    assert resp.status_code == 400


def test_viewer_booking_view_hides_financial_and_contact_data(shared_setup, student, faculty, equipment):
    booking = make_booking(student, equipment)
    make_client(student).post(
        f"{API}/workspaces/{shared_setup['ws']}/bookings/", {"booking_ids": [booking.booking_id]}, format="json"
    )
    rows = make_client(faculty).get(f"{API}/workspaces/{shared_setup['ws']}/bookings/").data["results"]
    assert len(rows) == 1
    row = rows[0]
    assert row["equipment_name"] == equipment.name
    forbidden = {"total_charge", "charge", "amount", "wallet", "invoice", "payment", "phone", "user", "email", "remarks"}
    assert not forbidden & set(row.keys())
    assert "123.45" not in str(row)


# ---------------------------------------------------------------- IDOR


@pytest.fixture
def two_workspaces(student, other_student, fake_s3):
    mine = make_client(student).post(f"{API}/workspaces/", {"name": "Mine"}, format="json").data
    theirs_client = make_client(other_student)
    theirs = theirs_client.post(f"{API}/workspaces/", {"name": "Theirs"}, format="json").data
    their_folder = theirs_client.post(f"{API}/workspaces/{theirs['id']}/folders/", {"name": "Secret"}, format="json").data
    their_file, done = upload_file(theirs_client, fake_s3, theirs["id"], "secret.pdf", PDF)
    assert done.status_code == 200
    my_folder = make_client(student).post(f"{API}/workspaces/{mine['id']}/folders/", {"name": "A"}, format="json").data
    return {"mine": mine["id"], "theirs": theirs["id"], "their_folder": their_folder["id"],
            "their_file": their_file, "my_folder": my_folder["id"]}


def test_cannot_open_other_users_workspace_folder_or_file(student, two_workspaces):
    client = make_client(student)
    t = two_workspaces
    for path in (
        f"{API}/workspaces/{t['theirs']}/",
        f"{API}/workspaces/{t['theirs']}/files/",
        f"{API}/workspaces/{t['theirs']}/folders/",
        f"{API}/workspaces/{t['theirs']}/members/",
        f"{API}/workspaces/{t['theirs']}/activity/",
        f"{API}/workspaces/{t['theirs']}/search/?q=secret",
        f"{API}/folders/{t['their_folder']}/",
        f"{API}/files/{t['their_file']}/",
        f"{API}/files/{t['their_file']}/preview/",
    ):
        assert client.get(path).status_code == 404, path
    assert client.post(f"{API}/files/{t['their_file']}/download/", {}, format="json").status_code == 404
    assert client.patch(f"{API}/files/{t['their_file']}/", {"name": "x.pdf"}, format="json").status_code == 404
    assert client.delete(f"{API}/files/{t['their_file']}/").status_code == 404
    assert client.delete(f"{API}/folders/{t['their_folder']}/").status_code == 404
    assert client.post(f"{API}/workspaces/{t['theirs']}/archive/", {}, format="json").status_code == 404
    assert ResearchFile.objects.get(pk=t["their_file"]).status == FileStatus.AVAILABLE


def test_unknown_workspace_id_returns_404(student):
    assert make_client(student).get(f"{API}/workspaces/00000000-0000-0000-0000-000000000000/").status_code == 404


def test_cannot_create_folder_or_upload_into_foreign_folder(student, two_workspaces):
    client = make_client(student)
    t = two_workspaces
    resp = client.post(
        f"{API}/workspaces/{t['mine']}/folders/", {"name": "X", "parent_id": t["their_folder"]}, format="json"
    )
    assert resp.status_code == 404
    resp = client.post(
        f"{API}/workspaces/{t['mine']}/uploads/initiate/",
        {"filename": "a.pdf", "size": 10, "folder_id": t["their_folder"]},
        format="json",
    )
    assert resp.status_code == 404


def test_cannot_move_folder_or_file_across_workspaces(student, two_workspaces, fake_s3):
    client = make_client(student)
    t = two_workspaces
    resp = client.patch(f"{API}/folders/{t['my_folder']}/", {"parent_id": t["their_folder"]}, format="json")
    assert resp.status_code == 404
    assert ResearchFolder.objects.get(pk=t["my_folder"]).parent_id is None

    my_file, done = upload_file(client, fake_s3, t["mine"], "mine.pdf", PDF)
    resp = client.patch(f"{API}/files/{my_file}/", {"folder_id": t["their_folder"]}, format="json")
    assert resp.status_code == 404
    assert ResearchFile.objects.get(pk=my_file).folder_id is None


def test_cannot_link_another_users_booking(student, other_student, workspace, equipment, fake_s3):
    foreign = make_booking(other_student, equipment)
    client = make_client(student)
    resp = client.post(f"{API}/workspaces/{workspace['id']}/bookings/", {"booking_ids": [foreign.booking_id]}, format="json")
    assert resp.status_code == 404
    resp = client.post(
        f"{API}/workspaces/{workspace['id']}/uploads/initiate/",
        {"filename": "a.pdf", "size": 10, "booking_id": foreign.booking_id},
        format="json",
    )
    assert resp.status_code == 404
    linkable = client.get(f"{API}/workspaces/{workspace['id']}/linkable-bookings/").data["results"]
    assert foreign.booking_id not in [b["booking_id"] for b in linkable]


def test_cannot_link_another_users_publication(student, other_student, workspace):
    foreign = make_claim(other_student)
    resp = make_client(student).post(
        f"{API}/workspaces/{workspace['id']}/publications/", {"claim_ids": [foreign.claim_id]}, format="json"
    )
    assert resp.status_code == 404


def test_cannot_remove_member_of_another_workspace(student, other_student, faculty, two_workspaces, notifications):
    t = two_workspaces
    theirs_member = _share(other_student, t["theirs"], faculty).data["id"]
    resp = make_client(student).delete(f"{API}/workspaces/{t['mine']}/members/{theirs_member}/")
    assert resp.status_code == 404
    resp = make_client(student).delete(f"{API}/workspaces/{t['theirs']}/members/{theirs_member}/")
    assert resp.status_code == 404
    assert ResearchWorkspaceMember.objects.get(pk=theirs_member).revoked_at is None


def test_file_booking_association_must_belong_to_workspace(student, other_student, two_workspaces, equipment, fake_s3):
    client = make_client(student)
    t = two_workspaces
    my_file, _ = upload_file(client, fake_s3, t["mine"], "m.pdf", PDF)
    their_booking = make_booking(other_student, equipment)
    make_client(other_student).post(
        f"{API}/workspaces/{t['theirs']}/bookings/", {"booking_ids": [their_booking.booking_id]}, format="json"
    )
    resp = client.patch(f"{API}/files/{my_file}/", {"booking_id": their_booking.booking_id}, format="json")
    assert resp.status_code == 400
    assert ResearchFile.objects.get(pk=my_file).booking_id is None
