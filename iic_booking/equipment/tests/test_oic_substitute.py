"""OIC Substitute: same-department search and validation, access only within the period, immediate
revoke, automatic expiry, notifications and the audit trail."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from iic_booking.communication.in_app import equipment_oic_users
from iic_booking.communication.models import CommunicationLog
from iic_booking.equipment.api_views import _user_can_act_as_oic_for_equipment
from iic_booking.equipment.models import (
    EquipmentManager,
    EquipmentOperator,
    EquipmentTemporaryOIC,
    EquipmentTemporaryOICEvent,
    InventoryRequest,
)
from iic_booking.equipment.oic_substitution import expire_due_substitutions
from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic
from iic_booking.equipment.tasks import expire_oic_substitutions
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

BASE = "/api/equipments/oic-substitutes/"
Status = EquipmentTemporaryOIC.Status


def _today(days=0):
    return (timezone.localdate() + timedelta(days=days)).isoformat()


@pytest.fixture
def lab(egs_factory):
    f = egs_factory
    other_dept = Department.objects.create(name="OIC-Sub Other Dept", code="OSOD")

    def manager(dept=f.department, **kw):
        return UserFactory(user_type=UserType.MANAGER, admin_approved=True, department=dept, **kw)

    eq = f.equipment(name="Sub XRD")
    other_eq = f.equipment(name="Sub SEM")
    oic = manager(name="Alpha Oic")
    colleague = manager(name="Beta Colleague")
    colleague2 = manager(name="Gamma Colleague")
    outsider = manager(dept=other_dept, name="Delta Outsider")
    inactive = manager(name="Epsilon Inactive")
    type(inactive).objects.filter(pk=inactive.pk).update(is_active=False)
    inactive.refresh_from_db()
    faculty = UserFactory(user_type=UserType.FACULTY, admin_approved=True, department=f.department, name="Zeta Faculty")
    operator = UserFactory(user_type=UserType.OPERATOR, admin_approved=True, department=f.department)
    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True, is_staff=True)
    EquipmentManager.objects.create(equipment=eq, manager=oic)
    EquipmentManager.objects.create(equipment=other_eq, manager=colleague2)
    EquipmentOperator.objects.create(equipment=eq, operator=operator)
    return SimpleNamespace(
        f=f, eq=eq, other_eq=other_eq, oic=oic, colleague=colleague, colleague2=colleague2, outsider=outsider,
        inactive=inactive, faculty=faculty, operator=operator, admin=admin,
    )


def _client(lab, user):
    return lab.f.client_for(user)


def _create(lab, *, user=None, subs=None, start=None, end=None, reason="Attending a conference", eq=None):
    payload = {
        "equipment_id": (eq or lab.eq).pk,
        "substitute_ids": [s.pk for s in (subs if subs is not None else [lab.colleague])],
        "start_date": start or _today(),
        "end_date": end or _today(2),
        "reason": reason,
    }
    return _client(lab, user or lab.oic).post(BASE, payload, format="json")


def _end(lab, delegation_id, *, user=None, reason="Back early"):
    return _client(lab, user or lab.oic).post(f"{BASE}{delegation_id}/end/", {"reason": reason}, format="json")


# --------------------------------------------------------------------------- search and validation


def test_candidate_search_returns_only_active_oics_of_my_department(lab):
    res = _client(lab, lab.oic).get(f"{BASE}candidates/")
    assert res.status_code == 200, res.content
    ids = {c["id"] for c in res.data["candidates"]}
    assert ids == {lab.colleague.pk, lab.colleague2.pk}

    narrowed = _client(lab, lab.oic).get(f"{BASE}candidates/", {"search": "Beta"})
    assert [c["id"] for c in narrowed.data["candidates"]] == [lab.colleague.pk]
    assert _client(lab, lab.oic).get(f"{BASE}candidates/", {"search": "Delta"}).data["candidates"] == []


def test_legacy_oic_user_list_is_restricted_to_my_department(lab):
    res = _client(lab, lab.oic).get("/api/equipments/temporary-oic/oic-users/")
    assert res.status_code == 200
    assert {u["id"] for u in res.data["oic_users"]} == {lab.colleague.pk, lab.colleague2.pk}


def test_only_oic_or_admin_can_open(lab):
    assert _client(lab, lab.faculty).get(BASE).status_code == 403
    assert _client(lab, lab.faculty).get(f"{BASE}candidates/").status_code == 403
    assert _client(lab, lab.admin).get(f"{BASE}candidates/").status_code == 403
    assert lab.f.client_for(None).get(BASE).status_code in (401, 403)


def test_options_list_only_permanent_oic_equipment(lab):
    res = _client(lab, lab.oic).get(f"{BASE}options/")
    assert res.status_code == 200
    assert [e["id"] for e in res.data["equipments"]] == [lab.eq.pk]
    assert res.data["department"]["id"] == lab.f.department.pk


@pytest.mark.parametrize(
    "case, expected_status, message",
    [
        ("outsider", 400, "your department"),
        ("inactive", 400, "your department"),
        ("faculty", 400, "your department"),
        ("self", 400, "yourself"),
        ("already_oic", 400, "already an OIC"),
        ("not_my_equipment", 403, "equipment you are the OIC"),
        ("past_start", 400, "past"),
        ("end_before_start", 400, "on or after"),
        ("no_reason", 400, "reason is required"),
        ("no_substitute", 400, "at least one"),
    ],
)
def test_create_validation(lab, case, expected_status, message):
    kwargs = {}
    if case in ("outsider", "inactive", "faculty"):
        kwargs["subs"] = [getattr(lab, case)]
    elif case == "self":
        kwargs["subs"] = [lab.oic]
    elif case == "already_oic":
        EquipmentManager.objects.create(equipment=lab.eq, manager=lab.colleague)
    elif case == "not_my_equipment":
        kwargs["eq"] = lab.other_eq
    elif case == "past_start":
        kwargs["start"] = _today(-1)
    elif case == "end_before_start":
        kwargs.update(start=_today(3), end=_today(2))
    elif case == "no_reason":
        kwargs["reason"] = "   "
    elif case == "no_substitute":
        kwargs["subs"] = []
    res = _create(lab, **kwargs)
    assert res.status_code == expected_status, res.content
    assert message.lower() in res.data["error"].lower()
    assert not EquipmentTemporaryOIC.objects.exists()


def test_non_oic_cannot_create(lab):
    res = _create(lab, user=lab.faculty)
    assert res.status_code == 403


def test_overlapping_substitution_for_same_equipment_and_substitute_is_prevented(lab):
    assert _create(lab, start=_today(1), end=_today(3)).status_code == 201
    clash = _create(lab, start=_today(3), end=_today(5))
    assert clash.status_code == 409, clash.content
    assert "overlapping" in clash.data["error"]
    assert _create(lab, start=_today(4), end=_today(5)).status_code == 201
    assert _create(lab, subs=[lab.colleague2], start=_today(1), end=_today(3)).status_code == 201


# --------------------------------------------------------------------------- access within the period


def test_substitute_has_oic_access_only_within_the_period(lab):
    res = _create(lab, start=_today(1), end=_today(2))
    assert res.status_code == 201, res.content
    row = EquipmentTemporaryOIC.objects.get()
    assert timezone.localtime(row.start_at).hour == 0
    assert timezone.localtime(row.resume_at).date().isoformat() == _today(3)
    assert res.data["items"][0]["status"] == "scheduled"

    assert lab.eq.pk not in get_equipment_ids_managed_by_oic(lab.colleague.pk)
    assert not _user_can_act_as_oic_for_equipment(lab.colleague, lab.eq)

    inside = row.start_at + timedelta(hours=1)
    assert EquipmentTemporaryOIC.objects.active(inside).filter(pk=row.pk).exists()
    assert not EquipmentTemporaryOIC.objects.active(row.resume_at).filter(pk=row.pk).exists()
    assert not EquipmentTemporaryOIC.objects.active(row.start_at - timedelta(seconds=1)).filter(pk=row.pk).exists()

    EquipmentTemporaryOIC.objects.filter(pk=row.pk).update(start_at=timezone.now() - timedelta(minutes=1))
    assert lab.eq.pk in get_equipment_ids_managed_by_oic(lab.colleague.pk)
    assert _user_can_act_as_oic_for_equipment(lab.colleague, lab.eq)
    assert InventoryRequest.is_user_authorized_for_equipment(lab.colleague, lab.eq)
    assert lab.colleague in equipment_oic_users(lab.eq)
    # The permanent OIC keeps access.
    assert _user_can_act_as_oic_for_equipment(lab.oic, lab.eq)

    EquipmentTemporaryOIC.objects.filter(pk=row.pk).update(resume_at=timezone.now() - timedelta(seconds=1))
    assert lab.eq.pk not in get_equipment_ids_managed_by_oic(lab.colleague.pk)
    assert not _user_can_act_as_oic_for_equipment(lab.colleague, lab.eq)


def test_starting_today_gives_access_immediately_and_reaches_oic_views(lab):
    assert _create(lab).status_code == 201
    assert _user_can_act_as_oic_for_equipment(lab.colleague, lab.eq)
    res = _client(lab, lab.colleague).get("/api/admin/equipment/waitlist-all/")
    assert res.status_code == 200, getattr(res, "data", res.content)
    option_ids = {o.get("equipment_id", o.get("id")) for o in res.data["filters"]["equipment_options"]}
    assert lab.eq.pk in option_ids


def test_revoke_takes_effect_immediately_and_is_recorded(lab, django_capture_on_commit_callbacks):
    assert _create(lab).status_code == 201
    row = EquipmentTemporaryOIC.objects.get()
    assert _user_can_act_as_oic_for_equipment(lab.colleague, lab.eq)

    assert _end(lab, row.pk, reason="").status_code == 400
    with django_capture_on_commit_callbacks(execute=True):
        res = _end(lab, row.pk, reason="Returned from leave early")
    assert res.status_code == 200, res.content
    assert res.data["item"]["status"] == Status.REVOKED

    row.refresh_from_db()
    assert row.status == Status.REVOKED
    assert row.ended_by == lab.oic and row.end_reason == "Returned from leave early" and row.ended_at
    assert not _user_can_act_as_oic_for_equipment(lab.colleague, lab.eq)
    assert lab.eq.pk not in get_equipment_ids_managed_by_oic(lab.colleague.pk)
    assert _user_can_act_as_oic_for_equipment(lab.oic, lab.eq)
    assert _end(lab, row.pk).status_code == 409


def test_cancel_scheduled_substitution(lab):
    assert _create(lab, start=_today(2), end=_today(4)).status_code == 201
    row = EquipmentTemporaryOIC.objects.get()
    res = _end(lab, row.pk, reason="Leave cancelled")
    assert res.status_code == 200
    row.refresh_from_db()
    assert row.status == Status.CANCELLED
    assert row.events.last().action == EquipmentTemporaryOICEvent.Action.CANCELLED


def test_only_granting_oic_or_main_admin_can_end(lab):
    assert _create(lab, subs=[lab.colleague, lab.colleague2]).status_code == 201
    first, second = EquipmentTemporaryOIC.objects.order_by("temporary_oic__name")
    assert first.batch_id == second.batch_id
    assert _end(lab, first.pk, user=lab.colleague).status_code == 404
    assert _end(lab, first.pk, user=lab.faculty).status_code == 403
    res = _end(lab, first.pk, user=lab.admin, reason="Policy review")
    assert res.status_code == 200
    first.refresh_from_db()
    assert first.status == Status.REVOKED and first.ended_by == lab.admin
    assert first.events.last().details == {"by_main_admin": True}
    second.refresh_from_db()
    assert second.status == Status.ACTIVE


# --------------------------------------------------------------------------- expiry


def test_expiry_job_marks_expired_and_notifies_once(lab):
    assert _create(lab).status_code == 201
    row = EquipmentTemporaryOIC.objects.get()
    EquipmentTemporaryOIC.objects.filter(pk=row.pk).update(
        start_at=timezone.now() - timedelta(days=1), resume_at=timezone.now() - timedelta(minutes=5)
    )
    CommunicationLog.objects.all().delete()

    assert expire_oic_substitutions() == 1
    row.refresh_from_db()
    assert row.status == Status.EXPIRED
    assert row.ended_at == row.resume_at and row.ended_by is None
    event = row.events.last()
    assert event.action == EquipmentTemporaryOICEvent.Action.EXPIRED and event.actor is None

    emailed = set(
        CommunicationLog.objects.filter(communication_type=CommunicationLog.CommunicationType.EMAIL).values_list(
            "recipient_id", "template__code"
        )
    )
    assert (lab.colleague.pk, "oic_substitute_ended_email") in emailed
    assert (lab.operator.pk, "oic_substitute_lab_staff_email") in emailed
    assert (lab.oic.pk, "oic_substitute_oic_copy_email") in emailed

    assert expire_due_substitutions() == 0


def test_long_past_expiry_is_recorded_without_emails(lab):
    assert _create(lab).status_code == 201
    EquipmentTemporaryOIC.objects.update(
        start_at=timezone.now() - timedelta(days=10), resume_at=timezone.now() - timedelta(days=5)
    )
    CommunicationLog.objects.all().delete()
    assert expire_due_substitutions() == 1
    assert EquipmentTemporaryOIC.objects.get().status == Status.EXPIRED
    assert not CommunicationLog.objects.exists()


# --------------------------------------------------------------------------- notifications


def test_create_notifies_substitutes_lab_incharges_and_copies_the_oic(lab, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        res = _create(lab, subs=[lab.colleague, lab.colleague2], reason="Medical leave")
    assert res.status_code == 201, res.content

    emails = CommunicationLog.objects.filter(communication_type=CommunicationLog.CommunicationType.EMAIL)
    by_code = {}
    for log in emails.select_related("template"):
        by_code.setdefault(log.template.code, set()).add(log.recipient_id)
    assert by_code["oic_substitute_assigned_email"] == {lab.colleague.pk, lab.colleague2.pk}
    assert by_code["oic_substitute_lab_staff_email"] == {lab.operator.pk}
    assert by_code["oic_substitute_oic_copy_email"] == {lab.oic.pk}
    assigned = emails.filter(template__code="oic_substitute_assigned_email", recipient=lab.colleague).get()
    assert lab.eq.name in assigned.subject
    assert "Medical leave" in assigned.message

    push = CommunicationLog.objects.exclude(communication_type=CommunicationLog.CommunicationType.EMAIL)
    push_recipients = set(push.values_list("recipient_id", flat=True))
    assert {lab.colleague.pk, lab.colleague2.pk, lab.operator.pk} <= push_recipients


def test_revoke_notifies_substitute_and_lab_incharges(lab, django_capture_on_commit_callbacks):
    assert _create(lab).status_code == 201
    row = EquipmentTemporaryOIC.objects.get()
    CommunicationLog.objects.all().delete()
    with django_capture_on_commit_callbacks(execute=True):
        assert _end(lab, row.pk, reason="Project finished").status_code == 200
    emailed = set(
        CommunicationLog.objects.filter(communication_type=CommunicationLog.CommunicationType.EMAIL).values_list(
            "recipient_id", "template__code"
        )
    )
    assert emailed == {
        (lab.colleague.pk, "oic_substitute_ended_email"),
        (lab.operator.pk, "oic_substitute_lab_staff_email"),
        (lab.oic.pk, "oic_substitute_oic_copy_email"),
    }
    ended = CommunicationLog.objects.get(template__code="oic_substitute_ended_email")
    assert "Project finished" in ended.message


# --------------------------------------------------------------------------- audit trail


def test_audit_trail_is_kept_and_listed(lab):
    assert _create(lab, reason="Conference travel").status_code == 201
    row = EquipmentTemporaryOIC.objects.get()
    assert _end(lab, row.pk, reason="Trip cancelled").status_code == 200

    assert EquipmentTemporaryOIC.objects.filter(pk=row.pk).exists()
    events = list(row.events.order_by("created_at", "id"))
    assert [e.action for e in events] == ["created", "revoked"]
    assert events[0].actor == lab.oic and events[0].reason == "Conference travel"
    assert events[1].actor == lab.oic and events[1].reason == "Trip cancelled"

    mine = _client(lab, lab.oic).get(BASE)
    assert mine.status_code == 200
    item = mine.data["granted"][0]
    assert item["status"] == Status.REVOKED and item["reason"] == "Conference travel"
    assert item["end_reason"] == "Trip cancelled" and item["can_end"] is False
    assert [e["action"] for e in item["events"]] == ["created", "revoked"]

    assigned = _client(lab, lab.colleague).get(BASE)
    assert [i["id"] for i in assigned.data["assigned_to_me"]] == [row.pk]
    assert assigned.data["granted"] == []

    everything = _client(lab, lab.admin).get(BASE)
    assert everything.data["scope"] == "admin"
    assert [i["id"] for i in everything.data["items"]] == [row.pk]
    assert _client(lab, lab.admin).get(BASE, {"status": "active"}).data["items"] == []
    assert len(_client(lab, lab.admin).get(BASE, {"status": "past"}).data["items"]) == 1


def test_legacy_cancel_keeps_the_row_and_requires_a_reason(lab):
    res = _client(lab, lab.oic).post(
        "/api/equipments/temporary-oic/",
        {
            "equipment_id": lab.eq.pk,
            "temporary_oic_id": lab.colleague.pk,
            "resume_at": (timezone.now() + timedelta(days=1)).isoformat(),
            "reason": "Short leave",
        },
        format="json",
    )
    assert res.status_code == 201, res.content
    row = EquipmentTemporaryOIC.objects.get()
    assert _user_can_act_as_oic_for_equipment(lab.colleague, lab.eq)

    url = f"/api/equipments/temporary-oic/{row.pk}/cancel/"
    assert _client(lab, lab.oic).delete(url).status_code == 400
    assert _client(lab, lab.oic).delete(url, {"reason": "Back"}, format="json").status_code == 200
    row.refresh_from_db()
    assert row.status == Status.REVOKED
    assert not _user_can_act_as_oic_for_equipment(lab.colleague, lab.eq)


def test_legacy_create_enforces_department(lab):
    res = _client(lab, lab.oic).post(
        "/api/equipments/temporary-oic/",
        {
            "equipment_id": lab.eq.pk,
            "temporary_oic_id": lab.outsider.pk,
            "resume_at": (timezone.now() + timedelta(days=1)).isoformat(),
            "reason": "Leave",
        },
        format="json",
    )
    assert res.status_code == 400
    assert not EquipmentTemporaryOIC.objects.exists()
