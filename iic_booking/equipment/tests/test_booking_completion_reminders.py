"""Ended-but-not-completed bookings: OIC / Lab in-charge digest email, login popup item and dashboard list."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from iic_booking.communication.models import CommunicationTemplate
from iic_booking.communication.default_email_templates import get_default_email_templates
from iic_booking.communication.service import CommunicationService
from iic_booking.equipment.completion_reminders import (
    EMAIL_TEMPLATE_CODE,
    bookings_awaiting_completion_for_user,
    send_booking_completion_reminders,
)
from iic_booking.equipment.models import (
    BookingStatus,
    EquipmentManager,
    EquipmentOperator,
    EquipmentOperatorCoverage,
)
from iic_booking.equipment.pending_actions import collect_pending_actions
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


def _staff(user_type):
    return UserFactory(user_type=user_type, admin_approved=True)


@pytest.fixture
def setup(egs_factory):
    eq_a = egs_factory.equipment()
    eq_b = egs_factory.equipment()
    oic_a = _staff(UserType.MANAGER)
    operator_a = _staff(UserType.OPERATOR)
    oic_b = _staff(UserType.MANAGER)
    EquipmentManager.objects.create(equipment=eq_a, manager=oic_a)
    EquipmentOperator.objects.create(equipment=eq_a, operator=operator_a, role=EquipmentOperator.Role.PRIMARY)
    EquipmentManager.objects.create(equipment=eq_b, manager=oic_b)

    student = egs_factory.student()
    now = timezone.now()
    ended_a = egs_factory.booking(student, eq_a, now - timedelta(days=2))
    upcoming_a = egs_factory.booking(student, eq_a, now + timedelta(days=2))
    completed_a = egs_factory.booking(student, eq_a, now - timedelta(days=3))
    type(completed_a).objects.filter(pk=completed_a.pk).update(status=BookingStatus.COMPLETED)
    ended_b = egs_factory.booking(student, eq_b, now - timedelta(hours=5))
    return {
        "eq_a": eq_a,
        "oic_a": oic_a,
        "operator_a": operator_a,
        "oic_b": oic_b,
        "student": student,
        "ended_a": ended_a,
        "upcoming_a": upcoming_a,
        "completed_a": completed_a,
        "ended_b": ended_b,
    }


def _ids(qs):
    return [b.pk for b in qs]


@pytest.mark.django_db
def test_each_person_sees_only_ended_uncompleted_bookings_of_their_equipment(setup):
    assert _ids(bookings_awaiting_completion_for_user(setup["oic_a"])) == [setup["ended_a"].pk]
    assert _ids(bookings_awaiting_completion_for_user(setup["operator_a"])) == [setup["ended_a"].pk]
    assert _ids(bookings_awaiting_completion_for_user(setup["oic_b"])) == [setup["ended_b"].pk]
    assert _ids(bookings_awaiting_completion_for_user(setup["student"])) == []


@pytest.mark.django_db
def test_operator_on_covered_leave_is_replaced_by_acting_operator(setup):
    acting = _staff(UserType.OPERATOR)
    now = timezone.now()
    EquipmentOperatorCoverage.objects.create(
        equipment=setup["eq_a"],
        primary_operator=setup["operator_a"],
        acting_operator=acting,
        mode=EquipmentOperatorCoverage.Mode.SECONDARY_OPERATOR,
        starts_at=now - timedelta(days=1),
        ends_at=now + timedelta(days=1),
    )
    assert _ids(bookings_awaiting_completion_for_user(setup["operator_a"])) == []
    assert _ids(bookings_awaiting_completion_for_user(acting)) == [setup["ended_a"].pk]

    with patch.object(CommunicationService, "send_email") as send_email:
        send_booking_completion_reminders()
    recipients = {call.kwargs["recipient"].pk for call in send_email.call_args_list}
    assert acting.pk in recipients
    assert setup["operator_a"].pk not in recipients


@pytest.mark.django_db
def test_daily_digest_sends_one_email_per_responsible_person(setup):
    with patch.object(CommunicationService, "send_email") as send_email:
        sent = send_booking_completion_reminders()

    assert sent == 3
    by_user = {call.kwargs["recipient"].pk: call.kwargs for call in send_email.call_args_list}
    assert set(by_user) == {setup["oic_a"].pk, setup["operator_a"].pk, setup["oic_b"].pk}
    ctx = by_user[setup["oic_a"].pk]["template_context"]
    assert by_user[setup["oic_a"].pk]["template"] == EMAIL_TEMPLATE_CODE
    assert ctx["booking_count"] == "1"
    assert setup["ended_a"].virtual_booking_id in ctx["bookings_html"]
    assert setup["ended_a"].virtual_booking_id in ctx["bookings_text"]
    assert setup["upcoming_a"].virtual_booking_id not in ctx["bookings_html"]
    assert ctx["link"].endswith("/dashboard#bookings-awaiting-completion")


@pytest.mark.django_db
def test_no_email_when_nothing_is_overdue(egs_factory):
    eq = egs_factory.equipment()
    EquipmentManager.objects.create(equipment=eq, manager=_staff(UserType.MANAGER))
    egs_factory.booking(egs_factory.student(), eq, timezone.now() + timedelta(days=1))

    with patch.object(CommunicationService, "send_email") as send_email:
        assert send_booking_completion_reminders() == 0
    send_email.assert_not_called()


@pytest.mark.django_db
def test_login_popup_item_lists_all_overdue_bookings(setup, egs_factory):
    for days in (3, 4, 5, 6):
        egs_factory.booking(setup["student"], setup["eq_a"], timezone.now() - timedelta(days=days))

    items = {i["key"]: i for i in collect_pending_actions(setup["oic_a"])}
    item = items["bookings_awaiting_completion"]
    assert item["count"] == 5
    assert len(item["details"]) == 5
    assert item["link"] == "/dashboard#bookings-awaiting-completion"
    assert "bookings_awaiting_completion" not in {i["key"] for i in collect_pending_actions(setup["student"])}


@pytest.mark.django_db
def test_dashboard_endpoint_returns_scoped_rows(setup):
    client = egs_factory_client(setup["operator_a"])
    res = client.get("/api/bookings/awaiting-completion/")
    assert res.status_code == 200
    assert res.data["count"] == 1
    row = res.data["bookings"][0]
    assert row["booking_id"] == setup["ended_a"].pk
    assert row["link"] == f"/booking-management?expand={setup['ended_a'].pk}"
    assert row["overdue"]


def egs_factory_client(user):
    from rest_framework.test import APIClient

    client = APIClient()
    client.force_authenticate(user=user)
    return client


def test_digest_template_renders_booking_table():
    spec = next(t for t in get_default_email_templates() if t["code"] == EMAIL_TEMPLATE_CODE)
    template = CommunicationTemplate(
        code=spec["code"],
        name=spec["name"],
        communication_type="email",
        subject=spec["subject"],
        body_text=spec["body_text"],
        body_html=spec["body_html"],
    )
    out = CommunicationService.render_template(
        template,
        {
            "user_name": "Dr OIC",
            "booking_count": "2",
            "bookings_html": "<table><tr><td>IICXRD-1</td></tr></table>",
            "bookings_text": "- IICXRD-1",
            "link": "https://equip.example.test/dashboard#bookings-awaiting-completion",
        },
    )
    assert out["subject"] == "Reminder: 2 booking(s) awaiting completion"
    assert "<td>IICXRD-1</td>" in out["html_message"]
    assert "- IICXRD-1" in out["message"]
