"""OIC Substitute for several equipment at once: per-pair validation, all-or-nothing saves, grouped
notifications (one message per recipient) and bulk cancel / revoke."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from iic_booking.communication.models import CommunicationLog
from iic_booking.equipment.models import (
    EquipmentManager,
    EquipmentOperator,
    EquipmentTemporaryOIC,
    EquipmentTemporaryOICEvent,
)
from iic_booking.equipment.oic_substitution import expire_due_substitutions
from iic_booking.users.models import Department
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

BASE = "/api/equipments/oic-substitutes/"
BULK = f"{BASE}bulk/"
BULK_END = f"{BASE}bulk-end/"
Status = EquipmentTemporaryOIC.Status


def _today(days=0):
    return (timezone.localdate() + timedelta(days=days)).isoformat()


@pytest.fixture
def lab(egs_factory):
    f = egs_factory
    other_dept = Department.objects.create(name="Bulk-Sub Other Dept", code="BSOD")

    def manager(dept=f.department, **kw):
        return UserFactory(user_type=UserType.MANAGER, admin_approved=True, department=dept, **kw)

    eq1 = f.equipment(name="Bulk XRD")
    eq2 = f.equipment(name="Bulk SEM")
    eq3 = f.equipment(name="Bulk TEM")
    other_eq = f.equipment(name="Bulk AFM")
    oic = manager(name="Alpha Oic")
    colleague = manager(name="Beta Colleague")
    colleague2 = manager(name="Gamma Colleague")
    other_oic = manager(name="Eta Other")
    outsider = manager(dept=other_dept, name="Delta Outsider")
    operator = UserFactory(user_type=UserType.OPERATOR, admin_approved=True, department=f.department)
    operator2 = UserFactory(user_type=UserType.OPERATOR, admin_approved=True, department=f.department)
    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True, is_staff=True)
    faculty = UserFactory(user_type=UserType.FACULTY, admin_approved=True, department=f.department)
    for eq in (eq1, eq2, eq3):
        EquipmentManager.objects.create(equipment=eq, manager=oic)
    EquipmentManager.objects.create(equipment=other_eq, manager=other_oic)
    EquipmentManager.objects.create(equipment=eq3, manager=other_oic)
    EquipmentOperator.objects.create(equipment=eq1, operator=operator)
    EquipmentOperator.objects.create(equipment=eq2, operator=operator)
    EquipmentOperator.objects.create(equipment=eq3, operator=operator2)
    return SimpleNamespace(
        f=f, eq1=eq1, eq2=eq2, eq3=eq3, other_eq=other_eq, oic=oic, colleague=colleague, colleague2=colleague2,
        other_oic=other_oic, outsider=outsider, operator=operator, operator2=operator2, admin=admin, faculty=faculty,
    )


def _client(lab, user):
    return lab.f.client_for(user)


def _bulk(lab, assignments, *, user=None, start=None, end=None, reason="Attending a conference"):
    payload = {
        "assignments": assignments,
        "start_date": start or _today(),
        "end_date": end or _today(2),
        "reason": reason,
    }
    return _client(lab, user or lab.oic).post(BULK, payload, format="json")


def _row(eq, *subs, **extra):
    return {"equipment_id": eq.pk, "substitute_ids": [s.pk for s in subs], **extra}


def _emails():
    return CommunicationLog.objects.filter(communication_type=CommunicationLog.CommunicationType.EMAIL).select_related(
        "template"
    )


def _standard(lab):
    return [_row(lab.eq1, lab.colleague), _row(lab.eq2, lab.colleague), _row(lab.eq3, lab.colleague2)]


# --------------------------------------------------------------------------- create


def test_bulk_assigns_groups_of_equipment_to_different_substitutes(lab):
    res = _bulk(lab, _standard(lab))
    assert res.status_code == 201, res.content
    rows = list(EquipmentTemporaryOIC.objects.order_by("equipment_id"))
    assert {(r.equipment_id, r.temporary_oic_id) for r in rows} == {
        (lab.eq1.pk, lab.colleague.pk),
        (lab.eq2.pk, lab.colleague.pk),
        (lab.eq3.pk, lab.colleague2.pk),
    }
    assert len({r.batch_id for r in rows}) == 1
    assert all(r.primary_oic == lab.oic and r.reason == "Attending a conference" for r in rows)
    events = EquipmentTemporaryOICEvent.objects.filter(action=EquipmentTemporaryOICEvent.Action.CREATED)
    assert events.count() == 3
    assert all(e.details["bulk"] is True and e.actor == lab.oic for e in events)
    assert len(res.data["items"]) == 3
    assert "3 equipment" in res.data["message"]


def test_row_dates_override_the_shared_period(lab):
    res = _bulk(
        lab,
        [_row(lab.eq1, lab.colleague), _row(lab.eq2, lab.colleague, start_date=_today(5), end_date=_today(6))],
    )
    assert res.status_code == 201, res.content
    by_eq = {r.equipment_id: r for r in EquipmentTemporaryOIC.objects.all()}
    assert timezone.localtime(by_eq[lab.eq1.pk].resume_at).date().isoformat() == _today(3)
    assert timezone.localtime(by_eq[lab.eq2.pk].start_at).date().isoformat() == _today(5)
    assert timezone.localtime(by_eq[lab.eq2.pk].resume_at).date().isoformat() == _today(7)


def test_bulk_is_all_or_nothing_and_reports_every_problem_per_row(lab):
    assert _bulk(lab, [_row(lab.eq2, lab.colleague2)], start=_today(1), end=_today(3)).status_code == 201
    before = EquipmentTemporaryOIC.objects.count()

    res = _bulk(
        lab,
        [
            _row(lab.eq1, lab.colleague),  # 0: valid on its own
            _row(lab.eq2, lab.colleague2),  # 1: overlaps the existing substitution
            _row(lab.eq3, lab.other_oic),  # 2: already an OIC of eq3
            _row(lab.other_eq, lab.colleague),  # 3: not my equipment
            _row(lab.eq1, lab.colleague2),  # 4: equipment listed twice
        ],
    )
    assert res.status_code == 400, res.content
    assert "Nothing was assigned" in res.data["error"]
    by_index = {}
    for e in res.data["row_errors"]:
        by_index.setdefault(e["index"], []).append(e["message"])
    assert set(by_index) == {1, 2, 3, 4}
    assert "overlapping" in by_index[1][0]
    assert "already an OIC" in by_index[2][0]
    assert "equipment you are the OIC of" in by_index[3][0]
    assert "listed twice" in by_index[4][0]
    assert EquipmentTemporaryOIC.objects.count() == before


@pytest.mark.parametrize(
    "case, message",
    [
        ("outsider", "your department"),
        ("self", "yourself"),
        ("faculty", "your department"),
        ("no_substitute", "Select a substitute"),
        ("past_start", "past"),
        ("end_before_start", "on or after"),
    ],
)
def test_bulk_pair_validation(lab, case, message):
    row = _row(lab.eq2, lab.colleague)
    if case in ("outsider", "faculty"):
        row = _row(lab.eq2, getattr(lab, case))
    elif case == "self":
        row = _row(lab.eq2, lab.oic)
    elif case == "no_substitute":
        row = _row(lab.eq2)
    elif case == "past_start":
        row["start_date"], row["end_date"] = _today(-1), _today(1)
    elif case == "end_before_start":
        row["start_date"], row["end_date"] = _today(3), _today(2)
    res = _bulk(lab, [_row(lab.eq1, lab.colleague), row])
    assert res.status_code == 400, res.content
    assert [e["index"] for e in res.data["row_errors"]] == [1]
    assert message.lower() in res.data["row_errors"][0]["message"].lower()
    assert not EquipmentTemporaryOIC.objects.exists()


def test_bulk_requires_reason_and_rows_and_an_oic(lab):
    assert _bulk(lab, _standard(lab), reason=" ").status_code == 400
    assert _bulk(lab, []).status_code == 400
    assert _bulk(lab, _standard(lab), user=lab.faculty).status_code == 403
    assert _bulk(lab, _standard(lab), user=lab.admin).status_code == 403
    assert lab.f.client_for(None).post(BULK, {}, format="json").status_code in (401, 403)
    assert lab.f.client_for(None).post(BULK_END, {}, format="json").status_code in (401, 403)
    assert not EquipmentTemporaryOIC.objects.exists()


def test_candidate_list_can_return_the_whole_department(lab):
    res = _client(lab, lab.oic).get(f"{BASE}candidates/", {"limit": 500})
    assert res.status_code == 200
    assert {c["id"] for c in res.data["candidates"]} == {lab.colleague.pk, lab.colleague2.pk, lab.other_oic.pk}
    options = _client(lab, lab.oic).get(f"{BASE}options/").data
    assert {e["id"] for e in options["equipments"]} == {lab.eq1.pk, lab.eq2.pk, lab.eq3.pk}
    assert options["max_bulk_rows"] >= 3


# --------------------------------------------------------------------------- notifications


def test_bulk_create_sends_one_message_per_recipient(lab, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        res = _bulk(lab, _standard(lab), reason="Medical leave")
    assert res.status_code == 201, res.content

    emails = list(_emails())
    per_recipient = Counter(e.recipient_id for e in emails)
    assert per_recipient == {lab.colleague.pk: 1, lab.colleague2.pk: 1, lab.operator.pk: 1, lab.operator2.pk: 1, lab.oic.pk: 1}
    codes = {e.recipient_id: e.template.code for e in emails}
    # Two equipment for Beta: one combined email. One equipment for Gamma: the usual single email.
    assert codes[lab.colleague.pk] == "oic_substitute_bulk_assigned_email"
    assert codes[lab.colleague2.pk] == "oic_substitute_assigned_email"
    # The operator covers eq1 and eq2: one combined email; operator2 covers only eq3.
    assert codes[lab.operator.pk] == "oic_substitute_bulk_lab_staff_email"
    assert codes[lab.operator2.pk] == "oic_substitute_lab_staff_email"
    assert codes[lab.oic.pk] == "oic_substitute_bulk_oic_copy_email"

    combined = next(e for e in emails if e.recipient_id == lab.colleague.pk)
    assert lab.eq1.name in combined.message and lab.eq2.name in combined.message
    assert lab.eq3.name not in combined.message
    assert "Medical leave" in combined.message
    assert timezone.localdate().strftime("%d-%m-%Y") in combined.message
    summary = next(e for e in emails if e.recipient_id == lab.oic.pk)
    assert all(eq.name in summary.message for eq in (lab.eq1, lab.eq2, lab.eq3))

    push = CommunicationLog.objects.exclude(communication_type=CommunicationLog.CommunicationType.EMAIL)
    push_counts = Counter(push.values_list("recipient_id", flat=True))
    assert push_counts[lab.colleague.pk] == 1 and push_counts[lab.operator.pk] == 1


def test_bulk_for_one_equipment_uses_the_single_messages(lab, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        assert _bulk(lab, [_row(lab.eq1, lab.colleague, lab.colleague2)]).status_code == 201
    codes = Counter(e.template.code for e in _emails())
    assert codes == {
        "oic_substitute_assigned_email": 2,
        "oic_substitute_lab_staff_email": 1,
        "oic_substitute_oic_copy_email": 1,
    }


# --------------------------------------------------------------------------- bulk end


def test_bulk_revoke_and_cancel_with_grouped_notifications(lab, django_capture_on_commit_callbacks):
    assert _bulk(lab, [_row(lab.eq1, lab.colleague), _row(lab.eq2, lab.colleague)]).status_code == 201
    assert _bulk(lab, [_row(lab.eq3, lab.colleague)], start=_today(3), end=_today(4)).status_code == 201
    ids = list(EquipmentTemporaryOIC.objects.values_list("pk", flat=True))
    CommunicationLog.objects.all().delete()

    assert _client(lab, lab.oic).post(BULK_END, {"ids": ids, "reason": ""}, format="json").status_code == 400
    assert EquipmentTemporaryOIC.objects.filter(status=Status.ACTIVE).count() == 3

    with django_capture_on_commit_callbacks(execute=True):
        res = _client(lab, lab.oic).post(BULK_END, {"ids": ids, "reason": "Back from leave"}, format="json")
    assert res.status_code == 200, res.content
    statuses = dict(EquipmentTemporaryOIC.objects.values_list("equipment_id", "status"))
    assert statuses == {lab.eq1.pk: Status.REVOKED, lab.eq2.pk: Status.REVOKED, lab.eq3.pk: Status.CANCELLED}
    assert all(
        d.end_reason == "Back from leave" and d.ended_by == lab.oic for d in EquipmentTemporaryOIC.objects.all()
    )
    assert EquipmentTemporaryOICEvent.objects.filter(
        action__in=[EquipmentTemporaryOICEvent.Action.REVOKED, EquipmentTemporaryOICEvent.Action.CANCELLED]
    ).count() == 3

    emails = list(_emails())
    assert Counter(e.recipient_id for e in emails) == {lab.colleague.pk: 1, lab.operator.pk: 1, lab.operator2.pk: 1, lab.oic.pk: 1}
    codes = {e.recipient_id: e.template.code for e in emails}
    assert codes[lab.colleague.pk] == "oic_substitute_bulk_ended_email"
    assert codes[lab.operator.pk] == "oic_substitute_bulk_lab_staff_email"
    assert codes[lab.operator2.pk] == "oic_substitute_lab_staff_email"
    assert codes[lab.oic.pk] == "oic_substitute_bulk_oic_copy_email"
    ended = next(e for e in emails if e.recipient_id == lab.colleague.pk)
    assert "Back from leave" in ended.message and lab.eq3.name in ended.message


def test_bulk_end_is_all_or_nothing(lab):
    assert _bulk(lab, _standard(lab)).status_code == 201
    first, *rest = EquipmentTemporaryOIC.objects.order_by("pk")
    assert _client(lab, lab.oic).post(f"{BASE}{first.pk}/end/", {"reason": "Done"}, format="json").status_code == 200
    res = _client(lab, lab.oic).post(
        BULK_END, {"ids": [first.pk, *(r.pk for r in rest)], "reason": "All done"}, format="json"
    )
    assert res.status_code == 409, res.content
    assert [e["id"] for e in res.data["row_errors"]] == [first.pk]
    assert all(r.status == Status.ACTIVE for r in EquipmentTemporaryOIC.objects.filter(pk__in=[r.pk for r in rest]))


def test_bulk_end_scope(lab):
    assert _bulk(lab, _standard(lab)).status_code == 201
    ids = list(EquipmentTemporaryOIC.objects.values_list("pk", flat=True))
    other = _client(lab, lab.colleague).post(BULK_END, {"ids": ids, "reason": "x"}, format="json")
    assert other.status_code == 404
    assert _client(lab, lab.faculty).post(BULK_END, {"ids": ids, "reason": "x"}, format="json").status_code == 403
    res = _client(lab, lab.admin).post(BULK_END, {"ids": ids, "reason": "Policy review"}, format="json")
    assert res.status_code == 200, res.content
    assert not EquipmentTemporaryOIC.objects.filter(status=Status.ACTIVE).exists()
    assert all(
        e.details.get("by_main_admin") is True
        for e in EquipmentTemporaryOICEvent.objects.filter(action=EquipmentTemporaryOICEvent.Action.REVOKED)
    )


def test_expiry_of_a_bulk_assignment_sends_one_message_per_recipient(lab):
    assert _bulk(lab, _standard(lab)).status_code == 201
    EquipmentTemporaryOIC.objects.update(
        start_at=timezone.now() - timedelta(days=1), resume_at=timezone.now() - timedelta(minutes=5)
    )
    CommunicationLog.objects.all().delete()
    assert expire_due_substitutions() == 3
    emails = list(_emails())
    assert Counter(e.recipient_id for e in emails) == {
        lab.colleague.pk: 1, lab.colleague2.pk: 1, lab.operator.pk: 1, lab.operator2.pk: 1, lab.oic.pk: 1,
    }
    codes = {e.recipient_id: e.template.code for e in emails}
    assert codes[lab.colleague.pk] == "oic_substitute_bulk_ended_email"
    assert codes[lab.colleague2.pk] == "oic_substitute_ended_email"
