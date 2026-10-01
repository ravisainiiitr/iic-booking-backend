"""OIC and Lab in-charge can see and reply to tickets marked to them, but not edit other users' tickets."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from iic_booking.equipment.models import EquipmentManager, EquipmentOperator
from iic_booking.support.models import Ticket
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


@pytest.fixture
def setup(egs_factory, monkeypatch):
    from iic_booking.support import api_views

    monkeypatch.setattr(api_views, "_send_ticket_update_email", lambda *a, **k: None)
    equipment = egs_factory.equipment()
    other_equipment = egs_factory.equipment()
    student = egs_factory.student()
    oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    other_oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    operator = UserFactory(user_type=UserType.OPERATOR, admin_approved=True)
    EquipmentManager.objects.create(equipment=equipment, manager=oic)
    EquipmentOperator.objects.create(equipment=equipment, operator=operator)
    for_equipment = Ticket.objects.create(
        user=student, subject="Peak missing", description="d", related_equipment=equipment
    )
    assigned_only = Ticket.objects.create(
        user=student, subject="General", description="d", related_equipment=other_equipment, assigned_to=oic
    )
    unrelated = Ticket.objects.create(
        user=student, subject="Other lab", description="d", related_equipment=other_equipment, assigned_to=other_oic
    )
    own = Ticket.objects.create(user=oic, subject="My own", description="d")
    return locals()


def _client(user):
    client = APIClient()
    client.force_authenticate(user)
    return client


def _ids(response):
    assert response.status_code == 200, response.content
    return {t["ticket_id"] for t in response.json()["tickets"]}


@pytest.mark.django_db
def test_oic_sees_assigned_and_equipment_tickets(setup):
    client = _client(setup["oic"])
    marked = _ids(client.get("/api/tickets/", {"scope": "assigned", "status": "all"}))
    assert marked == {setup["for_equipment"].ticket_id, setup["assigned_only"].ticket_id}
    mine = _ids(client.get("/api/tickets/", {"scope": "mine", "status": "all"}))
    assert mine == {setup["own"].ticket_id}


@pytest.mark.django_db
def test_lab_incharge_sees_tickets_for_mapped_equipment(setup):
    marked = _ids(_client(setup["operator"]).get("/api/tickets/", {"scope": "assigned", "status": "all"}))
    assert marked == {setup["for_equipment"].ticket_id}


@pytest.mark.django_db
def test_oic_can_open_and_reply_but_not_edit(setup):
    client = _client(setup["oic"])
    ticket_id = setup["for_equipment"].ticket_id
    assert client.get(f"/api/tickets/{ticket_id}/").status_code == 200
    reply = client.post(f"/api/tickets/{ticket_id}/comments/create/", {"comment": "Looking into it"}, format="json")
    assert reply.status_code == 201, reply.content
    edit = client.patch(f"/api/tickets/{ticket_id}/", {"description": "changed"}, format="json")
    assert edit.status_code == 403
    assert client.get(f"/api/tickets/{setup['unrelated'].ticket_id}/").status_code == 403


@pytest.mark.django_db
def test_student_still_sees_only_own_tickets(setup):
    seen = _ids(_client(setup["student"]).get("/api/tickets/", {"status": "all"}))
    assert seen == {
        setup["for_equipment"].ticket_id,
        setup["assigned_only"].ticket_id,
        setup["unrelated"].ticket_id,
    }
