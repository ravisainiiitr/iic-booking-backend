import uuid
from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.equipment.models import EquipmentPublicationClaimStatus
from iic_booking.my_research import group_views, tasks
from iic_booking.my_research.group_models import (
    GroupEventAction,
    ResearchGroupEvent,
    ResearchUpdateRequest,
    UpdateRequestStatus,
)
from iic_booking.users.models.user_type import UserType

from .conftest import API, make_booking, make_claim, make_client, make_department, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def groups_on(settings):
    settings.MY_RESEARCH_GROUPS_ENABLED = True
    settings.MY_RESEARCH_GROUPS_PILOT_EMAILS = ""
    return settings


@pytest.fixture
def other_faculty(internal_dept):
    return make_user(user_type=UserType.FACULTY, department=internal_dept, name="Dr. XYZ")


@pytest.fixture
def notified(monkeypatch):
    calls = []
    for name in (
        "notify_member_added", "notify_member_removed", "notify_activity_assigned", "notify_update_requested",
        "notify_update_submitted", "notify_update_reviewed",
    ):
        monkeypatch.setattr(group_views, name, lambda *a, _n=name, **k: calls.append((_n, a)))
    return calls


def create_group(user, name="Nanomaterials Lab", **extra):
    resp = make_client(user).post(f"{API}/groups/", {"name": name, **extra}, format="json")
    assert resp.status_code == 201, resp.data
    return resp.data


def add_member(owner, group_id, user, **extra):
    payload = {"user_id": user.pk, "member_type": "PHD", "confirm": True, **extra}
    return make_client(owner).post(f"{API}/groups/{group_id}/members/", payload, format="json")


@pytest.fixture
def group(faculty):
    return create_group(faculty)


@pytest.fixture
def membership(faculty, group, student):
    resp = add_member(faculty, group["id"], student)
    assert resp.status_code == 201, resp.data
    return resp.data


def create_activity(owner, group_id, **extra):
    payload = {"title": "XRD analysis of samples", **extra}
    resp = make_client(owner).post(f"{API}/groups/{group_id}/activities/", payload, format="json")
    assert resp.status_code == 201, resp.data
    return resp.data


def request_update(owner, group_id, users, **extra):
    payload = {"title": "Weekly progress", "assigned_user_ids": [u.pk for u in users], **extra}
    resp = make_client(owner).post(f"{API}/groups/{group_id}/update-requests/", payload, format="json")
    assert resp.status_code == 201, resp.data
    return resp.data["results"]


# ---------------------------------------------------------------- feature flag


class TestFlag:
    def test_disabled_groups_answer_404(self, settings, faculty):
        settings.MY_RESEARCH_GROUPS_ENABLED = False
        client = make_client(faculty)
        for url in (f"{API}/groups/home/", f"{API}/groups/", f"{API}/groups/{uuid.uuid4()}/"):
            resp = client.get(url)
            assert resp.status_code == 404
            assert resp.data["code"] == "my_research_groups_disabled"
        assert client.post(f"{API}/groups/", {"name": "X"}, format="json").status_code == 404

    def test_my_research_off_disables_groups(self, settings, faculty):
        settings.MY_RESEARCH_ENABLED = False
        assert make_client(faculty).get(f"{API}/groups/home/").status_code == 404

    def test_bootstrap_reports_groups_only_when_available(self, settings, faculty, student):
        data = make_client(faculty).get(f"{API}/bootstrap/").data
        assert data["groups_available"] is True and data["can_create_group"] is True
        assert make_client(student).get(f"{API}/bootstrap/").data["can_create_group"] is False
        settings.MY_RESEARCH_GROUPS_ENABLED = False
        data = make_client(faculty).get(f"{API}/bootstrap/").data
        assert data["groups_available"] is False and data["can_create_group"] is False
        settings.MY_RESEARCH_ENABLED = False
        assert make_client(faculty).get(f"{API}/bootstrap/").data == {"enabled": False, "eligible": True, "available": False}

    def test_disabled_flag_keeps_existing_workspaces_working(self, settings, student):
        settings.MY_RESEARCH_GROUPS_ENABLED = False
        resp = make_client(student).post(f"{API}/workspaces/", {"name": "Thesis"}, format="json")
        assert resp.status_code == 201

    def test_pilot_list_limits_creation(self, settings, faculty, other_faculty):
        settings.MY_RESEARCH_GROUPS_PILOT_EMAILS = faculty.email
        assert make_client(faculty).post(f"{API}/groups/", {"name": "A"}, format="json").status_code == 201
        assert make_client(other_faculty).post(f"{API}/groups/", {"name": "B"}, format="json").status_code == 403

    def test_reminder_task_is_noop_when_disabled(self, settings):
        settings.MY_RESEARCH_GROUPS_ENABLED = False
        assert tasks.group_update_reminders() == {"overdue": 0, "due_soon": 0}


# ---------------------------------------------------------------- groups


class TestGroups:
    def test_faculty_creates_group(self, faculty):
        data = create_group(faculty, description="Thin films", short_code="NML")
        assert data["my_role"] == "OWNER"
        assert data["permissions"]["can_manage"] is True
        assert data["counts"]["members"] == 0
        assert ResearchGroupEvent.objects.filter(action=GroupEventAction.GROUP_CREATED).count() == 1

    def test_student_cannot_create_group(self, student):
        resp = make_client(student).post(f"{API}/groups/", {"name": "Mine"}, format="json")
        assert resp.status_code == 403
        assert resp.data["code"] == "my_research_groups_faculty_only"

    def test_student_cannot_create_even_if_payload_claims_faculty(self, student):
        resp = make_client(student).post(
            f"{API}/groups/", {"name": "Mine", "role": "OWNER", "user_type": "faculty"}, format="json"
        )
        assert resp.status_code == 403

    def test_external_and_external_department_faculty_blocked(self, external_user):
        ext_faculty = make_user(user_type=UserType.FACULTY, department=make_department("external"))
        for user in (external_user, ext_faculty):
            resp = make_client(user).get(f"{API}/groups/home/")
            assert resp.status_code == 403
            assert make_client(user).post(f"{API}/groups/", {"name": "X"}, format="json").status_code == 403

    def test_name_required(self, faculty):
        assert make_client(faculty).post(f"{API}/groups/", {"name": "  "}, format="json").status_code == 400

    def test_foreign_faculty_gets_404(self, group, other_faculty):
        client = make_client(other_faculty)
        assert client.get(f"{API}/groups/{group['id']}/").status_code == 404
        assert client.patch(f"{API}/groups/{group['id']}/", {"name": "Hijack"}, format="json").status_code == 404
        assert client.get(f"{API}/groups/{group['id']}/members/").status_code == 404
        assert client.post(f"{API}/groups/{group['id']}/archive/").status_code == 404

    def test_unknown_group_and_foreign_group_look_identical(self, group, other_faculty):
        client = make_client(other_faculty)
        foreign = client.get(f"{API}/groups/{group['id']}/")
        missing = client.get(f"{API}/groups/{uuid.uuid4()}/")
        assert foreign.status_code == missing.status_code == 404
        assert foreign.data["code"] == missing.data["code"] == "not_found"

    def test_home_lists_managed_and_member_groups(self, faculty, group, membership, student):
        home = make_client(faculty).get(f"{API}/groups/home/").data
        assert [g["id"] for g in home["managed_groups"]] == [group["id"]]
        assert home["needs_attention"] is not None
        home = make_client(student).get(f"{API}/groups/home/").data
        assert home["can_create"] is False
        assert [g["id"] for g in home["member_groups"]] == [group["id"]]
        assert home["managed_groups"] == [] and home["needs_attention"] is None
        assert "pending_updates" not in home["member_groups"][0]["counts"]

    def test_member_cannot_edit_or_archive(self, group, membership, student):
        client = make_client(student)
        assert client.patch(f"{API}/groups/{group['id']}/", {"name": "X"}, format="json").status_code == 403
        assert client.post(f"{API}/groups/{group['id']}/archive/").status_code == 403

    def test_archive_makes_group_read_only_and_keeps_history(self, faculty, group, membership, student, other_student):
        activity = create_activity(faculty, group["id"], assignee_user_ids=[student.pk])
        resp = make_client(faculty).post(f"{API}/groups/{group['id']}/archive/")
        assert resp.status_code == 200 and resp.data["status"] == "ARCHIVED"
        assert add_member(faculty, group["id"], other_student).status_code == 409
        client = make_client(faculty)
        assert client.post(f"{API}/groups/{group['id']}/activities/", {"title": "New"}, format="json").status_code == 409
        resp = client.post(
            f"{API}/groups/{group['id']}/update-requests/", {"title": "U", "assigned_user_ids": [student.pk]}, format="json"
        )
        assert resp.status_code == 409
        assert make_client(student).patch(
            f"{API}/activities/{activity['id']}/", {"my_progress_percent": 50}, format="json"
        ).status_code == 409
        detail = client.get(f"{API}/groups/{group['id']}/").data
        assert detail["status"] == "ARCHIVED" and detail["permissions"]["can_manage"] is False
        assert len(client.get(f"{API}/groups/{group['id']}/activities/").data["results"]) == 1
        assert len(client.get(f"{API}/groups/{group['id']}/members/").data["results"]) == 1


# ---------------------------------------------------------------- members


class TestMembers:
    def test_add_member_requires_confirmation(self, faculty, group, student):
        resp = make_client(faculty).post(
            f"{API}/groups/{group['id']}/members/", {"user_id": student.pk, "member_type": "PHD"}, format="json"
        )
        assert resp.status_code == 400 and resp.data["code"] == "confirmation_required"

    def test_add_member_and_notify(self, faculty, group, student, notified, django_capture_on_commit_callbacks):
        with django_capture_on_commit_callbacks(execute=True):
            resp = add_member(faculty, group["id"], student, member_type="MTECH")
        assert resp.status_code == 201
        assert resp.data["member_type_label"] == "M.Tech"
        assert resp.data["user"]["email"] == student.email
        assert [c[0] for c in notified] == ["notify_member_added"]

    def test_reject_external_and_unknown_users(self, faculty, group, external_user):
        assert add_member(faculty, group["id"], external_user).status_code == 400
        resp = make_client(faculty).post(
            f"{API}/groups/{group['id']}/members/", {"user_id": 999999, "confirm": True}, format="json"
        )
        assert resp.status_code == 400
        assert add_member(faculty, group["id"], faculty).status_code == 400

    def test_duplicate_member_rejected(self, faculty, group, membership, student):
        resp = add_member(faculty, group["id"], student)
        assert resp.status_code == 409 and resp.data["code"] == "already_member"

    def test_invalid_member_type(self, faculty, group, student):
        assert add_member(faculty, group["id"], student, member_type="PROFESSOR").status_code == 400

    def test_student_cannot_add_or_remove(self, faculty, group, membership, student, other_student):
        assert add_member(student, group["id"], other_student).status_code == 403
        resp = make_client(student).delete(f"{API}/groups/{group['id']}/members/{membership['id']}/")
        assert resp.status_code == 403

    def test_member_directory_hides_email_from_members(self, group, membership, student):
        data = make_client(student).get(f"{API}/groups/{group['id']}/members/").data
        assert "email" not in data["results"][0]["user"]
        assert "active_activities" not in data["results"][0]

    def test_remove_member_cancels_open_work_and_can_rejoin(self, faculty, group, membership, student):
        activity = create_activity(faculty, group["id"], assignee_user_ids=[student.pk])
        [req] = request_update(faculty, group["id"], [student])
        resp = make_client(faculty).delete(f"{API}/groups/{group['id']}/members/{membership['id']}/")
        assert resp.status_code == 200
        assert ResearchUpdateRequest.objects.get(pk=req["id"]).status == UpdateRequestStatus.CANCELLED
        assert make_client(student).get(f"{API}/groups/{group['id']}/").status_code == 404
        assert make_client(student).get(f"{API}/activities/{activity['id']}/").status_code == 404
        assert add_member(faculty, group["id"], student).status_code == 201
        members = make_client(faculty).get(f"{API}/groups/{group['id']}/members/?include_left=1").data["results"]
        assert sorted(m["status"] for m in members) == ["ACTIVE", "LEFT"]

    def test_only_owner_adds_faculty_managers(self, faculty, group, other_faculty, student):
        assert add_member(faculty, group["id"], student, role="MANAGER").status_code == 400
        resp = add_member(faculty, group["id"], other_faculty, role="MANAGER", member_type="OTHER")
        assert resp.status_code == 201
        manager = make_client(other_faculty)
        assert manager.get(f"{API}/groups/{group['id']}/").data["permissions"]["can_manage"] is True
        assert add_member(other_faculty, group["id"], student).status_code == 201
        assert manager.post(f"{API}/groups/{group['id']}/archive/").status_code == 403

    def test_member_detail_is_manager_only(self, faculty, group, membership, student):
        assert make_client(student).get(f"{API}/groups/{group['id']}/members/{membership['id']}/").status_code == 403
        data = make_client(faculty).get(f"{API}/groups/{group['id']}/members/{membership['id']}/").data
        assert data["user"]["id"] == student.pk
        assert {"activities", "update_requests", "workspaces", "recent_events"} <= set(data)

    def test_member_detail_never_lists_unrelated_private_workspaces(self, faculty, group, membership, student, workspace):
        data = make_client(faculty).get(f"{API}/groups/{group['id']}/members/{membership['id']}/").data
        assert data["workspaces"] == []

    def test_member_id_from_other_group_is_404(self, faculty, group, other_faculty, student):
        other = create_group(other_faculty, "Other lab")
        foreign = add_member(other_faculty, other["id"], student).data
        client = make_client(faculty)
        assert client.get(f"{API}/groups/{group['id']}/members/{foreign['id']}/").status_code == 404
        assert client.delete(f"{API}/groups/{group['id']}/members/{foreign['id']}/").status_code == 404


# ---------------------------------------------------------------- categories


class TestCategories:
    def test_create_rename_deactivate_preserves_history(self, faculty, group, membership, student):
        client = make_client(faculty)
        cat = client.post(f"{API}/groups/{group['id']}/categories/", {"name": "Thin Films"}, format="json").data
        assert client.post(
            f"{API}/groups/{group['id']}/categories/", {"name": "thin films"}, format="json"
        ).status_code == 409
        resp = client.patch(
            f"{API}/groups/{group['id']}/members/{membership['id']}/", {"category_id": cat["id"]}, format="json"
        )
        assert resp.data["category"]["name"] == "Thin Films"
        resp = client.patch(f"{API}/groups/{group['id']}/categories/{cat['id']}/", {"name": "Coatings"}, format="json")
        assert resp.status_code == 200 and resp.data["name"] == "Coatings"
        resp = client.patch(f"{API}/groups/{group['id']}/categories/{cat['id']}/", {"active": False}, format="json")
        assert resp.data["active"] is False
        member = client.get(f"{API}/groups/{group['id']}/members/").data["results"][0]
        assert member["category"]["name"] == "Coatings"
        names = [c["name"] for c in make_client(student).get(f"{API}/groups/{group['id']}/categories/").data["results"]]
        assert names == []
        assert client.post(f"{API}/groups/{group['id']}/categories/", {"name": "Coatings"}, format="json").status_code == 201

    def test_reorder(self, faculty, group):
        client = make_client(faculty)
        a = client.post(f"{API}/groups/{group['id']}/categories/", {"name": "A"}, format="json").data
        b = client.post(f"{API}/groups/{group['id']}/categories/", {"name": "B"}, format="json").data
        resp = client.post(f"{API}/groups/{group['id']}/categories/reorder/", {"ids": [b["id"], a["id"]]}, format="json")
        assert [c["name"] for c in resp.data["results"]] == ["B", "A"]

    def test_student_cannot_manage_categories(self, group, membership, student):
        resp = make_client(student).post(f"{API}/groups/{group['id']}/categories/", {"name": "X"}, format="json")
        assert resp.status_code == 403

    def test_foreign_category_id_is_404(self, faculty, group, membership, other_faculty):
        other = create_group(other_faculty, "Other lab")
        cat = make_client(other_faculty).post(f"{API}/groups/{other['id']}/categories/", {"name": "X"}, format="json").data
        client = make_client(faculty)
        assert client.patch(f"{API}/groups/{group['id']}/categories/{cat['id']}/", {"name": "Y"}, format="json").status_code == 404
        resp = client.patch(
            f"{API}/groups/{group['id']}/members/{membership['id']}/", {"category_id": cat["id"]}, format="json"
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------- activities


class TestActivities:
    def test_create_assign_and_notify(self, faculty, group, membership, student, notified,
                                      django_capture_on_commit_callbacks):
        due = (timezone.localdate() + timedelta(days=3)).isoformat()
        with django_capture_on_commit_callbacks(execute=True):
            data = create_activity(faculty, group["id"], assignee_user_ids=[student.pk], due_date=due, priority="HIGH")
        assert data["assignee_count"] == 1 and data["priority"] == "HIGH" and data["due_date"].isoformat() == due
        assert [c[0] for c in notified] == ["notify_activity_assigned"]

    def test_assign_only_group_members(self, faculty, group, other_student):
        resp = make_client(faculty).post(
            f"{API}/groups/{group['id']}/activities/", {"title": "X", "assignee_user_ids": [other_student.pk]}, format="json"
        )
        assert resp.status_code == 400 and resp.data["code"] == "assignee_not_member"

    def test_multi_member_progress_is_per_person(self, faculty, group, membership, student, other_student):
        add_member(faculty, group["id"], other_student)
        activity = create_activity(faculty, group["id"], assignee_user_ids=[student.pk, other_student.pk])
        resp = make_client(student).patch(
            f"{API}/activities/{activity['id']}/", {"my_progress_percent": 60, "my_status": "IN_PROGRESS"}, format="json"
        )
        assert resp.status_code == 200
        assert resp.data["my_assignment"]["progress_percent"] == 60
        assert len(resp.data["assignees"]) == 1
        other = make_client(other_student).get(f"{API}/activities/{activity['id']}/").data
        assert other["my_assignment"]["progress_percent"] == 0
        full = make_client(faculty).get(f"{API}/activities/{activity['id']}/").data
        assert sorted(a["progress_percent"] for a in full["assignees"]) == [0, 60]

    def test_student_cannot_change_faculty_fields(self, faculty, group, membership, student):
        activity = create_activity(faculty, group["id"], assignee_user_ids=[student.pk])
        client = make_client(student)
        for payload in ({"title": "Mine"}, {"due_date": "2030-01-01"}, {"status": "COMPLETED"},
                        {"assignee_user_ids": []}, {"priority": "LOW"}):
            assert client.patch(f"{API}/activities/{activity['id']}/", payload, format="json").status_code == 403
        assert client.patch(
            f"{API}/activities/{activity['id']}/", {"my_status": "COMPLETED"}, format="json"
        ).status_code == 400
        assert client.post(f"{API}/groups/{group['id']}/activities/", {"title": "X"}, format="json").status_code == 403

    def test_reassign_and_complete(self, faculty, group, membership, student, other_student):
        add_member(faculty, group["id"], other_student)
        activity = create_activity(faculty, group["id"], assignee_user_ids=[student.pk])
        client = make_client(faculty)
        resp = client.patch(
            f"{API}/activities/{activity['id']}/", {"assignee_user_ids": [other_student.pk]}, format="json"
        )
        assert [a["user"]["id"] for a in resp.data["assignees"]] == [other_student.pk]
        assert make_client(student).get(f"{API}/activities/{activity['id']}/").status_code == 404
        resp = client.patch(f"{API}/activities/{activity['id']}/", {"status": "COMPLETED"}, format="json")
        assert resp.data["status"] == "COMPLETED" and resp.data["completed_at"] is not None

    def test_members_see_only_their_activities(self, faculty, group, membership, student, other_student):
        add_member(faculty, group["id"], other_student)
        create_activity(faculty, group["id"], title="Mine", assignee_user_ids=[student.pk])
        create_activity(faculty, group["id"], title="Theirs", assignee_user_ids=[other_student.pk])
        titles = [a["title"] for a in make_client(student).get(f"{API}/groups/{group['id']}/activities/").data["results"]]
        assert titles == ["Mine"]

    def test_due_before_start_rejected(self, faculty, group):
        resp = make_client(faculty).post(
            f"{API}/groups/{group['id']}/activities/",
            {"title": "X", "start_date": "2030-02-01", "due_date": "2030-01-01"}, format="json",
        )
        assert resp.status_code == 400

    def test_activity_idor(self, faculty, group, other_faculty, student, membership):
        activity = create_activity(faculty, group["id"])
        client = make_client(other_faculty)
        assert client.get(f"{API}/activities/{activity['id']}/").status_code == 404
        assert client.patch(f"{API}/activities/{activity['id']}/", {"title": "X"}, format="json").status_code == 404
        assert make_client(student).get(f"{API}/activities/{activity['id']}/").status_code == 404
        assert client.get(f"{API}/activities/{uuid.uuid4()}/").status_code == 404

    def test_booking_link_restricted_to_group_members(self, faculty, group, membership, student, other_student, equipment):
        own = make_booking(student, equipment)
        foreign = make_booking(other_student, equipment)
        client = make_client(faculty)
        data = create_activity(faculty, group["id"], booking_id=own.booking_id, equipment_id=equipment.equipment_id)
        assert data["booking"]["booking_id"] == own.booking_id and data["equipment"]["name"] == equipment.name
        resp = client.post(
            f"{API}/groups/{group['id']}/activities/", {"title": "X", "booking_id": foreign.booking_id}, format="json"
        )
        assert resp.status_code == 404
        ids = [b["booking_id"] for b in client.get(f"{API}/groups/{group['id']}/linkable-bookings/").data["results"]]
        assert own.booking_id in ids and foreign.booking_id not in ids


# ---------------------------------------------------------------- update requests


class TestUpdates:
    def test_full_request_submit_review_cycle(self, faculty, group, membership, student, notified,
                                              django_capture_on_commit_callbacks):
        activity = create_activity(faculty, group["id"], assignee_user_ids=[student.pk])
        due = (timezone.localdate() + timedelta(days=2)).isoformat()
        with django_capture_on_commit_callbacks(execute=True):
            [req] = request_update(faculty, group["id"], [student], due_date=due, activity_id=activity["id"])
        assert req["status"] == "PENDING"
        pending = make_client(student).get(f"{API}/groups/{group['id']}/updates/?state=pending").data["results"]
        assert [r["id"] for r in pending] == [req["id"]]
        with django_capture_on_commit_callbacks(execute=True):
            resp = make_client(student).post(
                f"{API}/update-requests/{req['id']}/submit/",
                {"work_completed": "Ran XRD", "current_status": "Analysing", "blockers": "", "next_steps": "SEM",
                 "progress_percent": 70},
                format="json",
            )
        assert resp.status_code == 200 and resp.data["status"] == "SUBMITTED"
        assert resp.data["submission"]["work_completed"] == "Ran XRD"
        act = make_client(student).get(f"{API}/activities/{activity['id']}/").data
        assert act["my_assignment"]["progress_percent"] == 70
        assert make_client(student).post(
            f"{API}/update-requests/{req['id']}/submit/", {"work_completed": "again"}, format="json"
        ).status_code == 409
        with django_capture_on_commit_callbacks(execute=True):
            resp = make_client(faculty).post(
                f"{API}/update-requests/{req['id']}/review/", {"comment": "Good"}, format="json"
            )
        assert resp.data["status"] == "REVIEWED" and resp.data["review_comment"] == "Good"
        history = make_client(student).get(f"{API}/groups/{group['id']}/updates/?state=history").data["results"]
        assert [r["id"] for r in history] == [req["id"]]
        assert [c[0] for c in notified] == [
            "notify_update_requested", "notify_update_submitted", "notify_update_reviewed",
        ]
        assert "score" not in resp.data and "rank" not in resp.data

    def test_submission_requires_content(self, faculty, group, membership, student):
        [req] = request_update(faculty, group["id"], [student])
        resp = make_client(student).post(f"{API}/update-requests/{req['id']}/submit/", {"blockers": "x"}, format="json")
        assert resp.status_code == 400

    def test_past_due_date_and_recurrence_rejected(self, faculty, group, membership, student):
        client = make_client(faculty)
        past = (timezone.localdate() - timedelta(days=1)).isoformat()
        base = {"title": "U", "assigned_user_ids": [student.pk]}
        assert client.post(f"{API}/groups/{group['id']}/update-requests/", {**base, "due_date": past},
                           format="json").status_code == 400
        assert client.post(f"{API}/groups/{group['id']}/update-requests/", {**base, "recurrence": "WEEKLY"},
                           format="json").status_code == 400

    def test_overdue_is_computed_and_notified_once(self, faculty, group, membership, student, monkeypatch):
        [req] = request_update(faculty, group["id"], [student])
        ResearchUpdateRequest.objects.filter(pk=req["id"]).update(due_date=timezone.localdate() - timedelta(days=2))
        overdue = make_client(faculty).get(f"{API}/groups/{group['id']}/updates/?state=overdue").data["results"]
        assert overdue[0]["status"] == "OVERDUE" and overdue[0]["days_overdue"] == 2
        sent = []
        monkeypatch.setattr("iic_booking.my_research.group_services.notify_update_overdue", lambda r: sent.append(r.pk))
        assert tasks.group_update_reminders()["overdue"] == 1
        assert tasks.group_update_reminders()["overdue"] == 0
        assert len(sent) == 1
        assert ResearchUpdateRequest.objects.get(pk=req["id"]).status == UpdateRequestStatus.OVERDUE
        resp = make_client(student).post(
            f"{API}/update-requests/{req['id']}/submit/", {"current_status": "Late but done"}, format="json"
        )
        assert resp.status_code == 200

    def test_activity_due_reminder_once(self, faculty, group, membership, student, monkeypatch):
        create_activity(faculty, group["id"], assignee_user_ids=[student.pk],
                        due_date=(timezone.localdate() + timedelta(days=1)).isoformat())
        sent = []
        monkeypatch.setattr("iic_booking.my_research.group_services.notify_activity_due", lambda a: sent.append(a.pk))
        assert tasks.group_update_reminders()["due_soon"] == 1
        assert tasks.group_update_reminders()["due_soon"] == 0
        assert len(sent) == 1

    def test_cancel(self, faculty, group, membership, student):
        [req] = request_update(faculty, group["id"], [student])
        assert make_client(student).post(f"{API}/update-requests/{req['id']}/cancel/").status_code == 403
        resp = make_client(faculty).post(f"{API}/update-requests/{req['id']}/cancel/")
        assert resp.data["status"] == "CANCELLED"
        assert make_client(student).post(
            f"{API}/update-requests/{req['id']}/submit/", {"current_status": "x"}, format="json"
        ).status_code == 409

    def test_members_cannot_see_each_others_updates(self, faculty, group, membership, student, other_student):
        add_member(faculty, group["id"], other_student)
        [req] = request_update(faculty, group["id"], [other_student])
        client = make_client(student)
        assert client.get(f"{API}/update-requests/{req['id']}/").status_code == 404
        assert client.post(f"{API}/update-requests/{req['id']}/submit/", {"current_status": "x"},
                           format="json").status_code == 404
        assert client.get(f"{API}/groups/{group['id']}/updates/").data["results"] == []
        feed = client.get(f"{API}/groups/{group['id']}/feed/").data["results"]
        assert all(e["subject"] is None or e["subject"]["id"] == student.pk for e in feed)

    def test_feed_hides_titles_of_activities_member_is_not_assigned_to(
        self, faculty, group, membership, student, other_student
    ):
        add_member(faculty, group["id"], other_student)
        create_activity(faculty, group["id"], title="Unassigned secret plan")
        create_activity(faculty, group["id"], title="Other student's work", assignee_user_ids=[other_student.pk])
        create_activity(faculty, group["id"], title="My own task", assignee_user_ids=[student.pk])
        client = make_client(student)
        labels = {e["target_label"] for e in client.get(f"{API}/groups/{group['id']}/feed/").data["results"]}
        labels |= {e["target_label"] for e in client.get(f"{API}/groups/home/").data["recent_events"]}
        assert "My own task" in labels
        assert "Unassigned secret plan" not in labels
        assert "Other student's work" not in labels
        faculty_labels = {
            e["target_label"] for e in make_client(faculty).get(f"{API}/groups/{group['id']}/feed/").data["results"]
        }
        assert {"Unassigned secret plan", "Other student's work", "My own task"} <= faculty_labels

    def test_student_cannot_request_updates(self, group, membership, student):
        resp = make_client(student).post(
            f"{API}/groups/{group['id']}/update-requests/", {"title": "U", "assigned_user_ids": [student.pk]}, format="json"
        )
        assert resp.status_code == 403

    def test_update_idor_for_foreign_faculty(self, faculty, group, membership, student, other_faculty):
        [req] = request_update(faculty, group["id"], [student])
        client = make_client(other_faculty)
        assert client.get(f"{API}/update-requests/{req['id']}/").status_code == 404
        assert client.post(f"{API}/update-requests/{req['id']}/review/").status_code == 404
        assert client.post(f"{API}/update-requests/{req['id']}/cancel/").status_code == 404


class TestAttachments:
    def _upload(self, client, fake, req_id, name="notes.pdf", body=b"%PDF-1.7 progress notes"):
        resp = client.post(
            f"{API}/update-requests/{req_id}/attachments/", {"filename": name, "size": len(body)}, format="json"
        )
        assert resp.status_code == 201, resp.data
        key = fake.presigned[-1]["params"]["Key"]
        fake.put(key, body)
        return resp.data["attachment"]["id"], key

    def test_attachment_upload_submit_download(self, fake_s3, faculty, group, membership, student):
        [req] = request_update(faculty, group["id"], [student])
        client = make_client(student)
        att_id, key = self._upload(client, fake_s3, req["id"])
        assert key.startswith(f"research/groups/{group['id']}/update-requests/{req['id']}/")
        done = client.post(f"{API}/update-attachments/{att_id}/complete/")
        assert done.status_code == 200 and done.data["status"] == "AVAILABLE"
        resp = client.post(
            f"{API}/update-requests/{req['id']}/submit/", {"current_status": "See notes", "attachment_ids": [att_id]},
            format="json",
        )
        assert resp.status_code == 200 and [a["id"] for a in resp.data["attachments"]] == [att_id]
        dl = make_client(faculty).post(f"{API}/update-attachments/{att_id}/download/")
        assert dl.status_code == 200
        assert fake_s3.presigned[-1]["params"]["ResponseContentDisposition"].startswith("attachment")
        assert client.delete(f"{API}/update-attachments/{att_id}/").status_code == 409

    def test_executable_rejected(self, fake_s3, faculty, group, membership, student):
        [req] = request_update(faculty, group["id"], [student])
        client = make_client(student)
        att_id, key = self._upload(client, fake_s3, req["id"], name="data.bin", body=b"MZ\x90\x00binary")
        resp = client.post(f"{API}/update-attachments/{att_id}/complete/")
        assert resp.status_code == 422
        assert key not in fake_s3.objects

    def test_blocked_extension_and_size(self, fake_s3, settings, faculty, group, membership, student):
        [req] = request_update(faculty, group["id"], [student])
        client = make_client(student)
        url = f"{API}/update-requests/{req['id']}/attachments/"
        assert client.post(url, {"filename": "setup.exe", "size": 10}, format="json").status_code == 400
        settings.MY_RESEARCH_GROUP_ATTACHMENT_MAX_SIZE = 100
        assert client.post(url, {"filename": "big.pdf", "size": 101}, format="json").status_code == 413

    def test_attachment_idor(self, fake_s3, faculty, group, membership, student, other_student, other_faculty):
        add_member(faculty, group["id"], other_student)
        [req] = request_update(faculty, group["id"], [student])
        att_id, _ = self._upload(make_client(student), fake_s3, req["id"])
        make_client(student).post(f"{API}/update-attachments/{att_id}/complete/")
        for user in (other_student, other_faculty):
            client = make_client(user)
            assert client.post(f"{API}/update-attachments/{att_id}/download/").status_code == 404
            assert client.delete(f"{API}/update-attachments/{att_id}/").status_code == 404
        assert make_client(other_student).post(
            f"{API}/update-requests/{req['id']}/attachments/", {"filename": "a.pdf", "size": 3}, format="json"
        ).status_code == 404

    def test_cannot_submit_someone_elses_attachment(self, fake_s3, faculty, group, membership, student, other_student):
        add_member(faculty, group["id"], other_student)
        [mine, theirs] = request_update(faculty, group["id"], [student, other_student])
        att_id, _ = self._upload(make_client(other_student), fake_s3, theirs["id"])
        make_client(other_student).post(f"{API}/update-attachments/{att_id}/complete/")
        resp = make_client(student).post(
            f"{API}/update-requests/{mine['id']}/submit/", {"current_status": "x", "attachment_ids": [att_id]},
            format="json",
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------- workspace separation (critical)


class TestWorkspaceSeparation:
    def test_membership_does_not_grant_workspace_access(self, faculty, group, membership, student, workspace):
        client = make_client(faculty)
        assert client.get(f"{API}/workspaces/{workspace['id']}/").status_code == 404
        assert client.get(f"{API}/workspaces/{workspace['id']}/files/").status_code == 404
        home = client.get(f"{API}/").data
        assert workspace["id"] not in [w["id"] for w in home.get("shared_with_me", [])]

    def test_faculty_cannot_link_students_private_workspace(self, faculty, group, membership, workspace):
        resp = make_client(faculty).post(
            f"{API}/groups/{group['id']}/workspaces/", {"workspace_ids": [workspace["id"]]}, format="json"
        )
        assert resp.status_code == 404
        resp = make_client(faculty).post(
            f"{API}/groups/{group['id']}/activities/", {"title": "X", "workspace_id": workspace["id"]}, format="json"
        )
        assert resp.status_code == 404

    def test_linked_workspace_stays_private(self, faculty, group, membership, student, other_student):
        add_member(faculty, group["id"], other_student)
        ws = make_client(faculty).post(f"{API}/workspaces/", {"name": "Group data"}, format="json").data
        resp = make_client(faculty).post(
            f"{API}/groups/{group['id']}/workspaces/", {"workspace_ids": [ws["id"]]}, format="json"
        )
        assert resp.status_code == 200
        listed = make_client(student).get(f"{API}/groups/{group['id']}/workspaces/").data["results"]
        assert listed[0]["name"] == "Group data" and listed[0]["accessible"] is False
        assert make_client(student).get(f"{API}/workspaces/{ws['id']}/").status_code == 404
        assert make_client(student).get(f"{API}/workspaces/{ws['id']}/files/").status_code == 404
        activity = create_activity(faculty, group["id"], workspace_id=ws["id"], assignee_user_ids=[student.pk])
        seen = make_client(student).get(f"{API}/activities/{activity['id']}/").data
        assert seen["workspace"]["accessible"] is False
        assert make_client(student).get(f"{API}/workspaces/{ws['id']}/").status_code == 404

    def test_workspace_shared_separately_is_accessible(self, faculty, group, membership, student):
        ws = make_client(faculty).post(f"{API}/workspaces/", {"name": "Shared"}, format="json").data
        make_client(faculty).post(f"{API}/groups/{group['id']}/workspaces/", {"workspace_ids": [ws["id"]]}, format="json")
        shared = make_client(faculty).post(
            f"{API}/workspaces/{ws['id']}/members/", {"user_id": student.pk, "confirm": True}, format="json"
        )
        assert shared.status_code == 201, shared.data
        listed = make_client(student).get(f"{API}/groups/{group['id']}/workspaces/").data["results"]
        assert listed[0]["accessible"] is True
        assert make_client(student).get(f"{API}/workspaces/{ws['id']}/").status_code == 200

    def test_member_can_link_own_workspace_only_via_faculty(self, faculty, group, membership, student, workspace):
        resp = make_client(student).post(
            f"{API}/groups/{group['id']}/workspaces/", {"workspace_ids": [workspace["id"]]}, format="json"
        )
        assert resp.status_code == 403

    def test_unlink_foreign_workspace_id_is_404(self, faculty, group):
        assert make_client(faculty).delete(f"{API}/groups/{group['id']}/workspaces/{uuid.uuid4()}/").status_code == 404


class TestPublications:
    def test_link_own_and_approved_member_claims(self, faculty, group, membership, student, other_student):
        own = make_claim(faculty, title="Faculty paper")
        approved = make_claim(student, title="Student approved")
        approved.status = EquipmentPublicationClaimStatus.APPROVED
        approved.save(update_fields=["status"])
        pending = make_claim(student, title="Student pending")
        outsider = make_claim(other_student, title="Outsider")
        client = make_client(faculty)
        linkable = {c["title"] for c in client.get(f"{API}/groups/{group['id']}/linkable-publications/").data["results"]}
        assert linkable == {"Faculty paper", "Student approved"}
        resp = client.post(
            f"{API}/groups/{group['id']}/publications/",
            {"claim_ids": [own.claim_id, approved.claim_id, pending.claim_id, outsider.claim_id]}, format="json",
        )
        assert {c["title"] for c in resp.data["results"]} == {"Faculty paper", "Student approved"}
        client.post(f"{API}/groups/{group['id']}/publications/", {"claim_ids": [own.claim_id]}, format="json")
        assert len(client.get(f"{API}/groups/{group['id']}/publications/").data["results"]) == 2
        assert client.delete(f"{API}/groups/{group['id']}/publications/{outsider.claim_id}/").status_code == 404
