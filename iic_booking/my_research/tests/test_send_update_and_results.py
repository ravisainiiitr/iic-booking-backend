"""Unprompted member updates ("Send update") and read-through booking results inside projects."""

from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from iic_booking.equipment.models import BookingDataShare, BookingResultFile, BookingStatus
from iic_booking.my_research import group_services, tasks, views
from iic_booking.my_research.group_models import ResearchUpdateRequest, UpdateRequestStatus
from iic_booking.users.models.user_type import UserType

from .conftest import API, make_booking, make_client, make_equipment, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def groups_on(settings):
    settings.MY_RESEARCH_GROUPS_ENABLED = True
    settings.MY_RESEARCH_GROUPS_PILOT_EMAILS = ""
    return settings


@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(group_services, "_push", lambda recipient, title, *a, **k: sent.append((recipient.pk, title)))
    monkeypatch.setattr(group_services, "_email", lambda *a, **k: None)
    return sent


@pytest.fixture
def other_faculty(internal_dept):
    return make_user(user_type=UserType.FACULTY, department=internal_dept, name="Dr. XYZ")


@pytest.fixture
def group(faculty):
    resp = make_client(faculty).post(f"{API}/groups/", {"name": "Nanomaterials Lab"}, format="json")
    assert resp.status_code == 201, resp.data
    return resp.data


@pytest.fixture
def membership(faculty, group, student):
    resp = make_client(faculty).post(
        f"{API}/groups/{group['id']}/members/", {"user_id": student.pk, "member_type": "PHD", "confirm": True},
        format="json",
    )
    assert resp.status_code == 201, resp.data
    return resp.data


def send_update(user, group_id, **payload):
    body = {"work_completed": "Ran XRD on batch 3", "current_status": "Analysing peaks", **payload}
    return make_client(user).post(f"{API}/groups/{group_id}/updates/self/", body, format="json")


def faculty_counts(faculty, group_id):
    return make_client(faculty).get(f"{API}/groups/{group_id}/").data["counts"]


# ---------------------------------------------------------------- B1: Send update


class TestSendUpdate:
    def test_member_can_send_and_managers_are_notified(
        self, faculty, other_faculty, group, membership, student, pushes, django_capture_on_commit_callbacks
    ):
        make_client(faculty).post(
            f"{API}/groups/{group['id']}/members/",
            {"user_id": other_faculty.pk, "member_type": "OTHER", "role": "MANAGER", "confirm": True},
            format="json",
        )
        pushes.clear()
        with django_capture_on_commit_callbacks(execute=True):
            resp = send_update(student, group["id"], progress_percent=40, title="Week 3 progress")
        assert resp.status_code == 201, resp.data
        assert resp.data["status"] == "SUBMITTED"
        assert resp.data["is_unprompted"] is True and resp.data["origin"] == "member"
        assert resp.data["title"] == "Week 3 progress"
        assert resp.data["submission"]["progress_percent"] == 40
        assert resp.data["requested_by"]["id"] == student.pk
        assert sorted(pk for pk, _ in pushes) == sorted([faculty.pk, other_faculty.pk])
        assert student.pk not in [pk for pk, _ in pushes]

    def test_default_title_and_content_required(self, group, membership, student):
        resp = send_update(student, group["id"])
        assert resp.status_code == 201 and resp.data["title"] == "Progress update"
        assert send_update(student, group["id"], work_completed="", current_status="").status_code == 400

    def test_non_member_gets_404_and_managers_cannot_self_send(
        self, faculty, other_faculty, other_student, group, membership
    ):
        assert send_update(other_student, group["id"]).status_code == 404
        assert send_update(other_faculty, group["id"]).status_code == 404
        resp = send_update(faculty, group["id"])
        assert resp.status_code == 403 and resp.data["code"] == "group_member_only"
        assert not ResearchUpdateRequest.objects.exists()

    def test_removed_or_ineligible_member_gets_404(self, faculty, group, membership, student):
        make_client(faculty).delete(f"{API}/groups/{group['id']}/members/{membership['id']}/")
        assert send_update(student, group["id"]).status_code == 404

    def test_archived_group_is_read_only(self, faculty, group, membership, student):
        make_client(faculty).post(f"{API}/groups/{group['id']}/archive/")
        assert send_update(student, group["id"]).status_code == 409

    def test_activity_must_be_assigned_to_sender(self, faculty, group, membership, student):
        unassigned = make_client(faculty).post(
            f"{API}/groups/{group['id']}/activities/", {"title": "Someone else's"}, format="json"
        ).data
        assert send_update(student, group["id"], activity_id=unassigned["id"]).status_code == 404
        mine = make_client(faculty).post(
            f"{API}/groups/{group['id']}/activities/", {"title": "XRD", "assignee_user_ids": [student.pk]},
            format="json",
        ).data
        resp = send_update(student, group["id"], activity_id=mine["id"], progress_percent=55)
        assert resp.status_code == 201 and resp.data["activity"]["id"] == mine["id"]
        act = make_client(student).get(f"{API}/activities/{mine['id']}/").data
        assert act["my_assignment"]["progress_percent"] == 55

    def test_managers_see_it_in_review_and_can_mark_reviewed(self, faculty, group, membership, student, pushes,
                                                            django_capture_on_commit_callbacks):
        sent = send_update(student, group["id"]).data
        fac = make_client(faculty)
        home = fac.get(f"{API}/groups/home/").data["needs_attention"]
        row = next(r for r in home["update_requests"] if r["id"] == sent["id"])
        assert row["is_unprompted"] is True and row["permissions"]["can_review"] is True
        assert row["permissions"]["can_cancel"] is False
        assert row["submission"]["work_completed"] == "Ran XRD on batch 3"
        submitted = fac.get(f"{API}/groups/{group['id']}/updates/?state=submitted").data["results"]
        assert [r["id"] for r in submitted] == [sent["id"]]
        with django_capture_on_commit_callbacks(execute=True):
            reviewed = fac.post(f"{API}/update-requests/{sent['id']}/review/", {"comment": "Thanks"}, format="json")
        assert reviewed.status_code == 200 and reviewed.data["status"] == "REVIEWED"
        assert (student.pk, "Research update reviewed") in pushes
        history = make_client(student).get(f"{API}/groups/{group['id']}/updates/?state=history").data["results"]
        assert [r["id"] for r in history] == [sent["id"]]

    def test_not_counted_as_pending_or_overdue(self, faculty, group, membership, student, monkeypatch):
        send_update(student, group["id"])
        counts = faculty_counts(faculty, group["id"])
        assert counts["pending_updates"] == 0 and counts["overdue_updates"] == 0
        assert counts["awaiting_review"] == 1
        draft = make_client(student).post(f"{API}/groups/{group['id']}/updates/self/", {"draft": True}, format="json")
        assert draft.status_code == 201 and draft.data["status"] == "PENDING"
        # Guard: even an unsubmitted, past-due self request never becomes pending/overdue or reminds anyone.
        ResearchUpdateRequest.objects.filter(pk=draft.data["id"]).update(due_date=timezone.localdate() - timedelta(days=3))
        counts = faculty_counts(faculty, group["id"])
        assert counts["pending_updates"] == 0 and counts["overdue_updates"] == 0
        sent = []
        monkeypatch.setattr(group_services, "notify_update_overdue", lambda r: sent.append(r.pk))
        assert tasks.group_update_reminders()["overdue"] == 0 and sent == []
        members = make_client(faculty).get(f"{API}/groups/{group['id']}/members/").data["results"]
        assert members[0]["open_update_requests"] == 0
        student_home = make_client(student).get(f"{API}/groups/home/").data
        assert student_home["my_work"]["update_requests"] == []
        assert student_home["member_groups"][0]["counts"]["my_open_requests"] == 0
        fac = make_client(faculty)
        listed = fac.get(f"{API}/groups/{group['id']}/updates/").data["results"]
        assert draft.data["id"] not in [r["id"] for r in listed]
        assert fac.get(f"{API}/update-requests/{draft.data['id']}/").status_code == 404

    def test_draft_attachments_then_send(self, fake_s3, faculty, group, membership, student, pushes,
                                         django_capture_on_commit_callbacks):
        client = make_client(student)
        draft = client.post(f"{API}/groups/{group['id']}/updates/self/", {"draft": True}, format="json").data
        body = b"%PDF-1.7 progress notes"
        init = client.post(
            f"{API}/update-requests/{draft['id']}/attachments/", {"filename": "notes.pdf", "size": len(body)},
            format="json",
        )
        assert init.status_code == 201, init.data
        fake_s3.put(fake_s3.presigned[-1]["params"]["Key"], body)
        att_id = init.data["attachment"]["id"]
        assert client.post(f"{API}/update-attachments/{att_id}/complete/").status_code == 200
        assert make_client(faculty).post(f"{API}/update-attachments/{att_id}/download/").status_code == 404
        with django_capture_on_commit_callbacks(execute=True):
            resp = send_update(student, group["id"], request_id=draft["id"], attachment_ids=[att_id])
        assert resp.status_code == 201, resp.data
        assert resp.data["id"] == draft["id"] and resp.data["status"] == "SUBMITTED"
        assert [a["id"] for a in resp.data["attachments"]] == [att_id]
        assert [pk for pk, _ in pushes] == [faculty.pk]
        assert make_client(faculty).post(f"{API}/update-attachments/{att_id}/download/").status_code == 200
        assert send_update(student, group["id"], request_id=draft["id"]).status_code == 404

    def test_new_draft_replaces_old_one_and_foreign_drafts_rejected(self, faculty, group, membership, student,
                                                                   other_student):
        client = make_client(student)
        first = client.post(f"{API}/groups/{group['id']}/updates/self/", {"draft": True}, format="json").data
        second = client.post(f"{API}/groups/{group['id']}/updates/self/", {"draft": True}, format="json").data
        assert ResearchUpdateRequest.objects.get(pk=first["id"]).status == UpdateRequestStatus.CANCELLED
        assert ResearchUpdateRequest.objects.get(pk=second["id"]).status == UpdateRequestStatus.PENDING
        assert send_update(student, group["id"], request_id=first["id"]).status_code == 404
        make_client(faculty).post(
            f"{API}/groups/{group['id']}/members/", {"user_id": other_student.pk, "member_type": "PHD", "confirm": True},
            format="json",
        )
        assert send_update(other_student, group["id"], request_id=second["id"]).status_code == 404
        listed = make_client(faculty).get(f"{API}/groups/{group['id']}/updates/?state=history").data["results"]
        assert listed == []

    def test_attachment_from_another_request_rejected(self, group, membership, student):
        resp = send_update(student, group["id"], attachment_ids=["7c1f7c63-2a53-4e0a-9d5d-4c0a6a1d2f11"])
        assert resp.status_code == 404
        assert not ResearchUpdateRequest.objects.exists()


# ---------------------------------------------------------------- B3: booking results in projects


@pytest.fixture
def media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    return settings


@pytest.fixture
def viewer_notifications(monkeypatch):
    monkeypatch.setattr(views, "notify_viewer_added", lambda m: None)
    monkeypatch.setattr(views, "notify_viewer_removed", lambda m: None)


def add_result_file(booking, name="xrd_pattern.csv"):
    return BookingResultFile.objects.create(booking=booking, file=SimpleUploadedFile(name, b"2theta,intensity"),
                                            original_name=name)


def link(owner, workspace_id, booking):
    resp = make_client(owner).post(
        f"{API}/workspaces/{workspace_id}/bookings/", {"booking_ids": [booking.booking_id]}, format="json"
    )
    assert resp.status_code == 200, resp.data


class TestWorkspaceBookingResults:
    def test_owner_sees_results_with_existing_download_urls(self, media, student, workspace, equipment):
        booking = make_booking(student, equipment)
        brf = add_result_file(booking)
        empty = make_booking(student, equipment)
        link(student, workspace["id"], booking)
        link(student, workspace["id"], empty)
        resp = make_client(student).get(f"{API}/workspaces/{workspace['id']}/booking-results/")
        assert resp.status_code == 200
        rows = {r["booking_id"]: r for r in resp.data["results"]}
        row = rows[booking.booking_id]
        assert row["can_view"] is True and row["has_results"] is True and row["locked_code"] is None
        assert [f["name"] for f in row["files"]] == ["xrd_pattern.csv"]
        assert row["files"][0]["download_url"] == f"/api/bookings/{booking.booking_id}/results/files/{brf.pk}/"
        assert row["results_path"] == f"/bookings/{booking.booking_id}/results/"
        assert rows[empty.booking_id]["has_results"] is False and rows[empty.booking_id]["files"] == []

    def test_viewer_without_results_access_sees_nothing(self, media, student, faculty, workspace, equipment,
                                                        viewer_notifications):
        booking = make_booking(student, equipment)
        add_result_file(booking)
        link(student, workspace["id"], booking)
        share = make_client(student).post(
            f"{API}/workspaces/{workspace['id']}/members/", {"user_id": faculty.pk, "confirm": True}, format="json"
        )
        assert share.status_code == 201, share.data
        resp = make_client(faculty).get(f"{API}/workspaces/{workspace['id']}/booking-results/")
        assert resp.status_code == 200
        assert resp.data["results"] == [{"booking_id": booking.booking_id, "can_view": False}]

    def test_viewer_with_existing_data_share_sees_results(self, media, student, faculty, workspace, equipment,
                                                         viewer_notifications):
        booking = make_booking(student, equipment)
        add_result_file(booking)
        link(student, workspace["id"], booking)
        make_client(student).post(
            f"{API}/workspaces/{workspace['id']}/members/", {"user_id": faculty.pk, "confirm": True}, format="json"
        )
        BookingDataShare.objects.create(booking=booking, shared_by=student, shared_with=faculty)
        [row] = make_client(faculty).get(f"{API}/workspaces/{workspace['id']}/booking-results/").data["results"]
        assert row["can_view"] is True and len(row["files"]) == 1

    def test_results_gates_still_apply(self, media, student, workspace, internal_dept):
        rated = make_equipment(internal_dept, user_rating_enabled=True)
        booking = make_booking(student, rated)
        add_result_file(booking)
        link(student, workspace["id"], booking)
        [row] = make_client(student).get(f"{API}/workspaces/{workspace['id']}/booking-results/").data["results"]
        assert row["can_view"] is True and row["locked_code"] == "rating_required" and row["files"] == []

    def test_outsider_gets_404(self, student, other_student, workspace):
        assert make_client(other_student).get(f"{API}/workspaces/{workspace['id']}/booking-results/").status_code == 404


class TestMyBookings:
    def test_unfiled_and_recent_lists_are_scoped_to_the_user(self, media, student, other_student, workspace, equipment):
        filed = make_booking(student, equipment)
        unfiled = make_booking(student, equipment)
        add_result_file(unfiled)
        upcoming = make_booking(student, equipment, status=BookingStatus.BOOKED)
        cancelled = make_booking(student, equipment, status=BookingStatus.CANCELLED)
        foreign = make_booking(other_student, equipment)
        link(student, workspace["id"], filed)
        client = make_client(student)

        resp = client.get(f"{API}/my-bookings/?unfiled=1")
        assert resp.status_code == 200
        assert [r["booking_id"] for r in resp.data["results"]] == [unfiled.booking_id]
        assert resp.data["unfiled_count"] == 1
        row = resp.data["results"][0]
        assert row["projects"] == [] and row["results"]["has_results"] is True
        assert "files" not in row["results"] and "total_charge" not in row

        recent = {r["booking_id"]: r for r in client.get(f"{API}/my-bookings/").data["results"]}
        assert set(recent) == {filed.booking_id, unfiled.booking_id, upcoming.booking_id}
        assert recent[filed.booking_id]["projects"] == [
            {"id": workspace["id"], "name": workspace["name"], "status": "ACTIVE"}
        ]
        assert cancelled.booking_id not in recent and foreign.booking_id not in recent

        other = make_client(other_student).get(f"{API}/my-bookings/?unfiled=1").data
        assert [r["booking_id"] for r in other["results"]] == [foreign.booking_id]

    def test_external_users_are_refused(self, external_user):
        assert make_client(external_user).get(f"{API}/my-bookings/").status_code == 403
