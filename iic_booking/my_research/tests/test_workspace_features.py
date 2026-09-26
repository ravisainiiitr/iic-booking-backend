"""Folders, bookings, equipment, publications, archive/restore, search, and soft delete."""

import pytest

from iic_booking.equipment.models import Booking, BookingStatus
from iic_booking.my_research.models import (
    ActivityAction,
    FileStatus,
    ResearchActivity,
    ResearchFile,
    ResearchFolder,
    ResearchWorkspaceBooking,
)

from .conftest import API, make_booking, make_claim, make_client, make_equipment, upload_file

pytestmark = pytest.mark.django_db

PDF = b"%PDF-1.7\n" + b"0" * 100


def _folder(client, ws, name, parent=None):
    return client.post(f"{API}/workspaces/{ws}/folders/", {"name": name, "parent_id": parent}, format="json")


# ---------------------------------------------------------------- folders


def test_nested_folders_breadcrumbs_and_lazy_listing(student, workspace):
    client = make_client(student)
    ws = workspace["id"]
    sem = _folder(client, ws, "SEM").data
    raw = _folder(client, ws, "Raw Images", sem["id"]).data
    deep = _folder(client, ws, "2026-07-21", raw["id"]).data

    root = client.get(f"{API}/workspaces/{ws}/folders/").data
    assert [f["name"] for f in root["results"]] == ["SEM"]
    assert root["results"][0]["has_children"] is True
    assert root["breadcrumbs"] == []

    level2 = client.get(f"{API}/workspaces/{ws}/folders/", {"parent": raw["id"]}).data
    assert [f["name"] for f in level2["results"]] == ["2026-07-21"]
    assert [c["name"] for c in level2["breadcrumbs"]] == ["SEM", "Raw Images"]

    detail = client.get(f"{API}/folders/{deep['id']}/").data
    assert [c["name"] for c in detail["breadcrumbs"]] == ["SEM", "Raw Images", "2026-07-21"]


def test_duplicate_folder_names_case_insensitive(student, workspace):
    client = make_client(student)
    ws = workspace["id"]
    assert _folder(client, ws, "Reports").status_code == 201
    dup = _folder(client, ws, "reports")
    assert dup.status_code == 409
    assert dup.data["code"] == "duplicate_name"
    parent = _folder(client, ws, "SEM").data
    assert _folder(client, ws, "Reports", parent["id"]).status_code == 201


@pytest.mark.parametrize("bad", ["", "  ", ".", "..", "a/b", "a\\b", "x" * 121])
def test_invalid_folder_names(student, workspace, bad):
    assert _folder(make_client(student), workspace["id"], bad).status_code == 400


def test_folder_rename_and_move(student, workspace):
    client = make_client(student)
    ws = workspace["id"]
    a = _folder(client, ws, "A").data
    b = _folder(client, ws, "B").data
    resp = client.patch(f"{API}/folders/{b['id']}/", {"name": "B2", "parent_id": a["id"]}, format="json")
    assert resp.status_code == 200
    assert resp.data["parent_id"] == a["id"]
    assert [c["name"] for c in resp.data["breadcrumbs"]] == ["A", "B2"]
    actions = set(ResearchActivity.objects.filter(workspace_id=ws).values_list("action", flat=True))
    assert {ActivityAction.FOLDER_RENAMED, ActivityAction.FOLDER_MOVED} <= actions
    back = client.patch(f"{API}/folders/{b['id']}/", {"parent_id": None}, format="json")
    assert back.data["parent_id"] is None


def test_folder_cycles_are_rejected(student, workspace):
    client = make_client(student)
    ws = workspace["id"]
    a = _folder(client, ws, "A").data
    b = _folder(client, ws, "B", a["id"]).data
    c = _folder(client, ws, "C", b["id"]).data
    for target in (a["id"], b["id"], c["id"]):
        resp = client.patch(f"{API}/folders/{a['id']}/", {"parent_id": target}, format="json")
        assert resp.status_code == 400, target
        assert resp.data["code"] == "invalid_move"
    assert ResearchFolder.objects.get(pk=a["id"]).parent_id is None


def test_folder_depth_limit(settings, student, workspace):
    settings.MY_RESEARCH_MAX_FOLDER_DEPTH = 3
    client = make_client(student)
    ws = workspace["id"]
    one = _folder(client, ws, "1").data
    two = _folder(client, ws, "2", one["id"]).data
    three = _folder(client, ws, "3", two["id"]).data
    assert _folder(client, ws, "4", three["id"]).status_code == 400
    other = _folder(client, ws, "X").data
    sub = _folder(client, ws, "Y", other["id"]).data
    assert client.patch(f"{API}/folders/{other['id']}/", {"parent_id": two["id"]}, format="json").status_code == 400
    assert sub


def test_delete_folder_soft_deletes_subtree_and_keeps_objects(student, workspace, fake_s3):
    client = make_client(student)
    ws = workspace["id"]
    top = _folder(client, ws, "Top").data
    child = _folder(client, ws, "Child", top["id"]).data
    f1, _ = upload_file(client, fake_s3, ws, "a.pdf", PDF, folder_id=top["id"])
    f2, _ = upload_file(client, fake_s3, ws, "b.pdf", PDF, folder_id=child["id"])
    keep, _ = upload_file(client, fake_s3, ws, "root.pdf", PDF)

    resp = client.delete(f"{API}/folders/{top['id']}/")
    assert resp.status_code == 200
    assert ResearchFolder.objects.filter(pk__in=[top["id"], child["id"]], deleted_at__isnull=False).count() == 2
    for fid in (f1, f2):
        row = ResearchFile.objects.get(pk=fid)
        assert row.status == FileStatus.DELETED
        assert row.storage_key in fake_s3.objects
    assert ResearchFile.objects.get(pk=keep).status == FileStatus.AVAILABLE
    assert client.get(f"{API}/folders/{child['id']}/").status_code == 404
    assert client.get(f"{API}/files/{f2}/").status_code == 404
    # The name becomes reusable after deletion.
    assert _folder(client, ws, "Top").status_code == 201


def test_file_rename_move_and_soft_delete(student, workspace, fake_s3):
    client = make_client(student)
    ws = workspace["id"]
    folder = _folder(client, ws, "Results").data
    file_id, _ = upload_file(client, fake_s3, ws, "raw.pdf", PDF)
    resp = client.patch(f"{API}/files/{file_id}/", {"name": "Final.pdf", "folder_id": folder["id"]}, format="json")
    assert resp.status_code == 200
    assert resp.data["name"] == "Final.pdf"
    assert resp.data["folder_id"] == folder["id"]
    row = ResearchFile.objects.get(pk=file_id)
    assert row.original_name == "raw.pdf"
    assert "raw.pdf" in row.storage_key

    assert client.delete(f"{API}/files/{file_id}/").status_code == 200
    row.refresh_from_db()
    assert row.status == FileStatus.DELETED and row.deleted_by == student
    assert row.storage_key in fake_s3.objects


def test_file_sorting_and_pagination(student, workspace, fake_s3):
    client = make_client(student)
    ws = workspace["id"]
    for name in ("b.pdf", "a.pdf", "c.pdf"):
        upload_file(client, fake_s3, ws, name, PDF)
    names = [f["name"] for f in client.get(f"{API}/workspaces/{ws}/files/", {"sort": "name"}).data["results"]]
    assert names == ["a.pdf", "b.pdf", "c.pdf"]
    page = client.get(f"{API}/workspaces/{ws}/files/", {"page_size": 2}).data
    assert len(page["results"]) == 2 and page["pagination"]["has_next"] is True


# ---------------------------------------------------------------- archive


def test_archive_blocks_changes_and_restore_reenables(student, faculty, workspace, fake_s3):
    client = make_client(student)
    ws = workspace["id"]
    folder = _folder(client, ws, "F").data
    file_id, _ = upload_file(client, fake_s3, ws, "a.pdf", PDF)
    archived = client.post(f"{API}/workspaces/{ws}/archive/", {}, format="json")
    assert archived.status_code == 200
    assert archived.data["permissions"]["read_only"] is True

    for resp in (
        client.patch(f"{API}/workspaces/{ws}/", {"name": "x"}, format="json"),
        _folder(client, ws, "G"),
        client.patch(f"{API}/folders/{folder['id']}/", {"name": "F2"}, format="json"),
        client.delete(f"{API}/folders/{folder['id']}/"),
        client.post(f"{API}/workspaces/{ws}/uploads/initiate/", {"filename": "b.pdf", "size": 5}, format="json"),
        client.patch(f"{API}/files/{file_id}/", {"name": "z.pdf"}, format="json"),
        client.delete(f"{API}/files/{file_id}/"),
    ):
        assert resp.status_code == 409, resp.data
        assert resp.data["code"] == "workspace_archived"

    assert client.get(f"{API}/workspaces/{ws}/files/").status_code == 200
    assert client.post(f"{API}/files/{file_id}/download/", {}, format="json").status_code == 200
    assert client.get(f"{API}/workspaces/", {}).data["results"] == []

    assert client.post(f"{API}/workspaces/{ws}/archive/", {}, format="json").status_code == 409
    assert client.post(f"{API}/workspaces/{ws}/restore/", {}, format="json").status_code == 200
    assert _folder(client, ws, "G").status_code == 201


# ---------------------------------------------------------------- bookings / equipment


def test_link_bookings_equipment_tab_and_upload_results(student, workspace, fake_s3, internal_dept):
    client = make_client(student)
    ws = workspace["id"]
    sem = make_equipment(internal_dept, name="FE-SEM")
    xrd = make_equipment(internal_dept, name="XRD")
    b1 = make_booking(student, sem)
    b2 = make_booking(student, sem, status=BookingStatus.BOOKED)
    b3 = make_booking(student, xrd)

    linkable = client.get(f"{API}/workspaces/{ws}/linkable-bookings/").data["results"]
    assert {b["booking_id"] for b in linkable} == {b1.booking_id, b2.booking_id, b3.booking_id}

    resp = client.post(f"{API}/workspaces/{ws}/bookings/", {"booking_ids": [b1.booking_id, b2.booking_id]}, format="json")
    assert resp.status_code == 200
    assert sorted(resp.data["linked"]) == sorted([b1.booking_id, b2.booking_id])
    again = client.post(f"{API}/workspaces/{ws}/bookings/", {"booking_ids": [b1.booking_id]}, format="json")
    assert again.data["already_linked"] == [b1.booking_id]

    # "Upload Results" from a booking links it automatically.
    file_id, done = upload_file(client, fake_s3, ws, "xrd.pdf", PDF, booking_id=b3.booking_id)
    assert done.status_code == 200
    assert done.data["booking"]["booking_id"] == b3.booking_id
    assert ResearchWorkspaceBooking.objects.filter(workspace_id=ws, booking=b3).exists()

    equipment = client.get(f"{API}/workspaces/{ws}/equipment/").data["results"]
    by_name = {e["name"]: e for e in equipment}
    assert by_name["FE-SEM"]["bookings"] == 2 and by_name["FE-SEM"]["files"] == 0
    assert by_name["XRD"]["bookings"] == 1 and by_name["XRD"]["files"] == 1

    per_booking = client.get(f"{API}/workspaces/{ws}/files/", {"booking": b3.booking_id}).data["results"]
    assert [f["id"] for f in per_booking] == [file_id]
    rows = client.get(f"{API}/workspaces/{ws}/bookings/").data["results"]
    assert {r["booking_id"]: r["file_count"] for r in rows}[b3.booking_id] == 1

    selector = client.get(f"{API}/workspaces/", {"booking": b3.booking_id}).data["results"]
    assert selector == [{"id": ws, "name": workspace["name"], "booking_linked": True}]


def test_unlinking_booking_keeps_files(student, workspace, fake_s3, equipment):
    client = make_client(student)
    ws = workspace["id"]
    booking = make_booking(student, equipment)
    file_id, _ = upload_file(client, fake_s3, ws, "r.pdf", PDF, booking_id=booking.booking_id)
    assert client.delete(f"{API}/workspaces/{ws}/bookings/{booking.booking_id}/").status_code == 200
    row = ResearchFile.objects.get(pk=file_id)
    assert row.status == FileStatus.AVAILABLE
    assert client.delete(f"{API}/workspaces/{ws}/bookings/{booking.booking_id}/").status_code == 404


def test_booking_linked_to_folder_from_workspace(student, workspace, equipment):
    client = make_client(student)
    ws = workspace["id"]
    sem = _folder(client, ws, "SEM").data
    raw = _folder(client, ws, "Raw", sem["id"]).data
    b1 = make_booking(student, equipment)
    b2 = make_booking(student, equipment)

    resp = client.post(
        f"{API}/workspaces/{ws}/bookings/", {"booking_ids": [b1.booking_id], "folder_id": raw["id"]}, format="json"
    )
    assert resp.status_code == 200 and resp.data["linked"] == [b1.booking_id]
    client.post(f"{API}/workspaces/{ws}/bookings/", {"booking_ids": [b2.booking_id]}, format="json")

    link = ResearchWorkspaceBooking.objects.get(workspace_id=ws, booking=b1)
    assert str(link.folder_id) == raw["id"]
    rows = {r["booking_id"]: r for r in client.get(f"{API}/workspaces/{ws}/bookings/").data["results"]}
    assert rows[b1.booking_id]["folder_id"] == raw["id"]
    assert [c["name"] for c in rows[b1.booking_id]["folder_path"]] == ["SEM", "Raw"]
    assert rows[b2.booking_id]["folder_id"] is None

    in_folder = client.get(f"{API}/workspaces/{ws}/bookings/", {"folder": raw["id"]}).data["results"]
    assert [r["booking_id"] for r in in_folder] == [b1.booking_id]
    activity = ResearchActivity.objects.filter(workspace_id=ws, action=ActivityAction.BOOKING_LINKED, target_id=str(b1.booking_id)).get()
    assert activity.details["folder_name"] == "Raw"


def test_booking_link_rejects_foreign_or_bad_folder(student, workspace, equipment, faculty):
    client = make_client(student)
    ws = workspace["id"]
    booking = make_booking(student, equipment)
    other_ws = make_client(faculty).post(f"{API}/workspaces/", {"name": "Other"}, format="json").data["id"]
    foreign = _folder(make_client(faculty), other_ws, "Theirs").data
    url = f"{API}/workspaces/{ws}/bookings/"
    assert client.post(url, {"booking_ids": [booking.booking_id], "folder_id": foreign["id"]}, format="json").status_code == 404
    assert client.post(url, {"booking_ids": [booking.booking_id], "folder_id": "nope"}, format="json").status_code == 400
    assert not ResearchWorkspaceBooking.objects.filter(workspace_id=ws, booking=booking).exists()


def test_booking_link_does_not_modify_booking(student, workspace, equipment):
    booking = make_booking(student, equipment)
    before = Booking.objects.filter(pk=booking.pk).values().get()
    make_client(student).post(f"{API}/workspaces/{workspace['id']}/bookings/", {"booking_ids": [booking.booking_id]}, format="json")
    after = Booking.objects.filter(pk=booking.pk).values().get()
    assert before == after


# ---------------------------------------------------------------- publications


def test_publications_link_and_unlink(student, workspace, equipment):
    client = make_client(student)
    ws = workspace["id"]
    claim = make_claim(student, equipment=equipment)
    linkable = client.get(f"{API}/workspaces/{ws}/linkable-publications/").data["results"]
    assert [c["claim_id"] for c in linkable] == [claim.claim_id]
    resp = client.post(f"{API}/workspaces/{ws}/publications/", {"claim_ids": [claim.claim_id]}, format="json")
    assert resp.data["linked"] == [claim.claim_id]
    pubs = client.get(f"{API}/workspaces/{ws}/publications/").data["results"]
    assert pubs[0]["title"] == claim.title
    assert pubs[0]["equipment"][0]["name"] == equipment.name
    assert client.delete(f"{API}/workspaces/{ws}/publications/{claim.claim_id}/").status_code == 200
    assert client.get(f"{API}/workspaces/{ws}/publications/").data["results"] == []


# ---------------------------------------------------------------- search / home / activity


def test_search_is_scoped_to_workspace(student, other_student, workspace, fake_s3, equipment):
    client = make_client(student)
    ws = workspace["id"]
    folder = _folder(client, ws, "Corrosion Tests").data
    upload_file(client, fake_s3, ws, "corrosion-summary.pdf", PDF, folder_id=folder["id"])
    booking = make_booking(student, equipment)
    client.post(f"{API}/workspaces/{ws}/bookings/", {"booking_ids": [booking.booking_id]}, format="json")
    claim = make_claim(student, title="Corrosion resistant coatings")
    client.post(f"{API}/workspaces/{ws}/publications/", {"claim_ids": [claim.claim_id]}, format="json")

    other = make_client(other_student)
    other_ws = other.post(f"{API}/workspaces/", {"name": "Other"}, format="json").data["id"]
    upload_file(other, fake_s3, other_ws, "corrosion-secret.pdf", PDF)

    result = client.get(f"{API}/workspaces/{ws}/search/", {"q": "corrosion"}).data
    assert [f["name"] for f in result["folders"]] == ["Corrosion Tests"]
    assert [f["name"] for f in result["files"]] == ["corrosion-summary.pdf"]
    assert result["files"][0]["folder_path"] == ["Corrosion Tests"]
    assert [p["claim_id"] for p in result["publications"]] == [claim.claim_id]

    by_equipment = client.get(f"{API}/workspaces/{ws}/search/", {"q": equipment.name}).data
    assert [b["booking_id"] for b in by_equipment["bookings"]] == [booking.booking_id]
    by_id = client.get(f"{API}/workspaces/{ws}/search/", {"q": str(booking.booking_id)}).data
    assert [b["booking_id"] for b in by_id["bookings"]] == [booking.booking_id]
    assert client.get(f"{API}/workspaces/{ws}/search/", {"q": "c"}).data["files"] == []


def test_home_stats_and_recent_activity(student, workspace, fake_s3, equipment):
    client = make_client(student)
    ws = workspace["id"]
    _folder(client, ws, "F")
    upload_file(client, fake_s3, ws, "a.pdf", PDF)
    booking = make_booking(student, equipment)
    client.post(f"{API}/workspaces/{ws}/bookings/", {"booking_ids": [booking.booking_id]}, format="json")

    home = client.get(f"{API}/").data
    card = home["my_workspaces"][0]
    assert card["stats"]["files"] == 1
    assert card["stats"]["folders"] == 1
    assert card["stats"]["bookings"] == 1
    assert card["stats"]["equipment"] == 1
    assert card["stats"]["storage_bytes"] == len(PDF)
    assert home["storage"]["used_bytes"] == len(PDF)
    actions = [a["action"] for a in home["recent_activity"]]
    assert ActivityAction.FILE_UPLOADED in actions and ActivityAction.BOOKING_LINKED in actions

    activity = client.get(f"{API}/workspaces/{ws}/activity/").data
    assert activity["results"][0]["action"] == ActivityAction.BOOKING_LINKED
    assert activity["results"][-1]["action"] == ActivityAction.WORKSPACE_CREATED


def test_workspace_update(student, workspace):
    client = make_client(student)
    resp = client.patch(f"{API}/workspaces/{workspace['id']}/", {"name": "Renamed", "description": "d"}, format="json")
    assert resp.status_code == 200
    assert resp.data["name"] == "Renamed"
    assert resp.data["permissions"]["can_edit"] is True
