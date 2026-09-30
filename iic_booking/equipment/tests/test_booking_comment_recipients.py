"""Add Comment: selectively notify the booking user, Officer In Charge and Lab Operator."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from iic_booking.equipment import booking_events
from iic_booking.equipment.models import BookingEvent, EquipmentManager, EquipmentOperator
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


@pytest.fixture
def sent(monkeypatch):
    calls = SimpleNamespace(emails=[], pushes=[], dispatched=[])

    def _email(recipient, template=None, template_context=None, metadata=None, created_by=None, cc_emails=None):
        calls.emails.append((recipient, template, dict(template_context or {})))
        from iic_booking.communication.models import CommunicationLog

        return SimpleNamespace(status=CommunicationLog.CommunicationStatus.SENT)

    def _push(recipient, title=None, message=None, template=None, template_context=None, metadata=None,
              created_by=None):
        calls.pushes.append((recipient, title, message))

    monkeypatch.setattr(booking_events.CommunicationService, "send_email", staticmethod(_email))
    monkeypatch.setattr(booking_events.CommunicationService, "send_push_notification", staticmethod(_push))
    monkeypatch.setattr(booking_events, "_dispatch_booking_event_notification", calls.dispatched.append)
    return calls


@pytest.fixture
def setup(egs_factory):
    equipment = egs_factory.equipment()
    student = egs_factory.student()
    booking = egs_factory.booking(student, equipment, egs_factory.future())
    oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    lab = UserFactory(user_type=UserType.OPERATOR, admin_approved=True)
    EquipmentManager.objects.create(equipment=equipment, manager=oic)
    return SimpleNamespace(factory=egs_factory, equipment=equipment, student=student, booking=booking, oic=oic,
                           lab=lab)


def _post(setup, **body):
    client = setup.factory.client_for(setup.student)
    return client.post(
        f"/api/bookings/{setup.booking.pk}/events/comment/",
        {"comment": "Please use the cryo stage.", **body},
        format="json",
    )


def _event(pk):
    return BookingEvent.objects.select_related("booking", "booking__user", "booking__equipment", "created_by").get(
        event_id=pk
    )


def test_oic_only_skips_booking_user(setup, sent, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        resp = _post(setup, send_notification=False, notify_oic=True, notify_lab_incharge=False)

    assert resp.status_code == 201, resp.content
    assert resp.data["warnings"] == []
    event_id = resp.data["event"]["event_id"]
    assert sent.dispatched == [event_id]
    event = _event(event_id)
    assert event.metadata["comment_recipients"] == {"user": False, "oic": True, "lab_incharge": False}

    booking_events.send_booking_event_notification(event)

    assert [r for r, _t, _c in sent.emails] == [setup.oic]
    _r, template, ctx = sent.emails[0]
    assert template == "admin_bulk_email"
    assert "Please use the cryo stage." in ctx["body"]
    assert setup.booking.virtual_booking_id in ctx["subject"]
    assert [r for r, _t, _m in sent.pushes] == [setup.oic]


def test_user_and_lab_incharge(setup, sent):
    EquipmentOperator.objects.create(equipment=setup.equipment, operator=setup.lab)

    resp = _post(setup, send_notification=True, notify_oic=False, notify_lab_incharge=True)

    assert resp.status_code == 201, resp.content
    booking_events.send_booking_event_notification(_event(resp.data["event"]["event_id"]))

    recipients = [r for r, _t, _c in sent.emails]
    assert setup.student in recipients
    assert setup.lab in recipients
    assert setup.oic not in recipients


def test_warns_when_selected_role_is_unassigned(setup, sent):
    resp = _post(setup, send_notification=False, notify_lab_incharge=True)

    assert resp.status_code == 201, resp.content
    assert resp.data["warnings"] == ["No Lab Operator is assigned to this equipment."]


def test_nothing_selected_sends_nothing(setup, sent, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        resp = _post(setup, send_notification=False)

    assert resp.status_code == 201, resp.content
    assert sent.dispatched == []


def test_legacy_comment_without_selection_notifies_user_only(setup, sent):
    event = booking_events.create_booking_event(
        booking=setup.booking,
        event_type="COMMENT",
        created_by=setup.student,
        comment="Legacy comment",
        send_notification=False,
    )

    booking_events.send_booking_event_notification(_event(event.event_id))

    assert [r for r, _t, _c in sent.emails] == [setup.student]


def test_author_is_not_notified_about_own_comment(setup, sent):
    EquipmentOperator.objects.create(equipment=setup.equipment, operator=setup.lab)
    event = booking_events.create_booking_event(
        booking=setup.booking,
        event_type="COMMENT",
        created_by=setup.lab,
        comment="Sample received late",
        metadata={"comment_recipients": {"user": False, "oic": True, "lab_incharge": True}},
        send_notification=False,
    )

    booking_events.send_booking_event_notification(_event(event.event_id))

    assert [r for r, _t, _c in sent.emails] == [setup.oic]
