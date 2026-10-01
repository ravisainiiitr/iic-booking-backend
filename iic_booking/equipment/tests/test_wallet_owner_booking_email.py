"""Faculty wallet owner's and OIC's copies of a student's booking email name the student who booked."""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from iic_booking.equipment import booking_events
from iic_booking.equipment.models import BookingEvent, BookingStatus, EquipmentManager
from iic_booking.users.models.user_type import UserType
from iic_booking.users.repositories.wallet_repository import WalletRepository
from iic_booking.users.tests.factories import UserFactory


@pytest.fixture
def sent(monkeypatch):
    calls = SimpleNamespace(emails=[], pushes=[])

    def _email(recipient, template=None, template_context=None, metadata=None, created_by=None, cc_emails=None):
        calls.emails.append((recipient, template, dict(template_context or {})))
        from iic_booking.communication.models import CommunicationLog

        return SimpleNamespace(status=CommunicationLog.CommunicationStatus.SENT)

    def _push(recipient, title=None, message=None, template=None, template_context=None, metadata=None,
              created_by=None):
        calls.pushes.append((recipient, title, message))

    monkeypatch.setattr(booking_events.CommunicationService, "send_email", staticmethod(_email))
    monkeypatch.setattr(booking_events.CommunicationService, "send_push_notification", staticmethod(_push))
    return calls


@pytest.fixture
def setup(egs_factory, monkeypatch):
    equipment = egs_factory.equipment()
    student = egs_factory.student()
    student.name, student.email = "Asha Verma", "asha.verma@example.com"
    student.save()
    faculty = UserFactory(
        user_type=UserType.FACULTY, name="Ravi Kumar", email="ravi.kumar@example.com", admin_approved=True
    )
    oic = UserFactory(user_type=UserType.MANAGER, name="Meena OIC", admin_approved=True)
    EquipmentManager.objects.create(equipment=equipment, manager=oic)
    booking = egs_factory.booking(student, equipment, egs_factory.future())
    state = SimpleNamespace(wallet_owner=faculty)

    def _target(user, department):
        wallet = SimpleNamespace(user=state.wallet_owner)
        return SimpleNamespace(wallet=wallet, balance=Decimal("500.00"), refresh_from_db=lambda: None), True

    monkeypatch.setattr(WalletRepository, "get_booking_wallet_target", staticmethod(_target))
    return SimpleNamespace(student=student, faculty=faculty, oic=oic, booking=booking, state=state)


def _send(setup, **kwargs):
    event = booking_events.create_booking_event(
        booking=setup.booking, created_by=setup.student, send_notification=False, **kwargs
    )
    booking_events.send_booking_event_notification(
        BookingEvent.objects.select_related("booking", "booking__user", "booking__equipment").get(
            event_id=event.event_id
        )
    )


def _email_to(sent, recipient):
    matches = [ctx for r, _t, ctx in sent.emails if r == recipient]
    assert len(matches) == 1, [r for r, _t, _c in sent.emails]
    return matches[0]


def test_confirmation_to_faculty_names_the_student(setup, sent):
    _send(setup, event_type="CREATED")

    faculty_ctx = _email_to(sent, setup.faculty)
    assert faculty_ctx["user_name"] == "Ravi Kumar"
    assert faculty_ctx["student_name"] == "Asha Verma"
    assert faculty_ctx["comment"].startswith(
        "Booked by your student: Asha Verma (asha.verma@example.com). This booking is charged to your wallet."
    )

    student_ctx = _email_to(sent, setup.student)
    assert "Booked by your student" not in student_ctx["comment"]


def test_urgent_hold_confirmation_to_faculty_names_the_student(setup, sent):
    _send(
        setup,
        event_type="STATUS_CHANGED",
        previous_status=BookingStatus.HOLD,
        new_status=BookingStatus.BOOKED,
        metadata={"urgent_hold_converted": True},
    )

    assert "Booked by your student: Asha Verma" in _email_to(sent, setup.faculty)["comment"]


def test_student_without_a_name_is_shown_once_by_email(setup, sent):
    setup.student.name = ""
    setup.student.save()

    _send(setup, event_type="CREATED")

    assert _email_to(sent, setup.faculty)["comment"].startswith(
        "Booked by your student: asha.verma@example.com. This booking"
    )


def test_no_faculty_copy_when_student_pays_from_own_wallet(setup, sent):
    setup.state.wallet_owner = setup.student

    _send(setup, event_type="CREATED")

    assert setup.faculty not in [r for r, _t, _c in sent.emails]
    assert "Charged to the wallet of" not in _email_to(sent, setup.oic)["comment"]


def test_confirmation_to_oic_names_the_student_and_wallet_owner(setup, sent):
    _send(setup, event_type="CREATED")

    oic_ctx = _email_to(sent, setup.oic)
    assert oic_ctx["user_name"] == "Meena OIC"
    assert oic_ctx["comment"].startswith(
        "Booked by: Asha Verma (asha.verma@example.com).\n"
        "Charged to the wallet of: Ravi Kumar (ravi.kumar@example.com)."
    )
    assert "Booked by your student" not in oic_ctx["comment"]


def test_urgent_hold_confirmation_to_oic_names_the_student(setup, sent):
    _send(
        setup,
        event_type="STATUS_CHANGED",
        previous_status=BookingStatus.HOLD,
        new_status=BookingStatus.BOOKED,
        metadata={"urgent_hold_converted": True},
    )

    assert _email_to(sent, setup.oic)["comment"].startswith("Booked by: Asha Verma")


def test_cancellation_reason_to_oic_is_left_unchanged(setup, sent):
    _send(setup, event_type="CANCELLED", comment="Instrument down")

    assert _email_to(sent, setup.oic)["comment"] == "Instrument down"
