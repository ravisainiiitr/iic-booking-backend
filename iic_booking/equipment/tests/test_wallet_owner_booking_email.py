"""Faculty wallet owner's and OIC's copies of a student's booking email name the student who booked."""

from __future__ import annotations

import re
from decimal import Decimal
from types import SimpleNamespace

import pytest

from iic_booking.communication.default_email_templates import get_default_email_templates
from iic_booking.communication.models import CommunicationTemplate
from iic_booking.communication.service import CommunicationService
from iic_booking.equipment import booking_events
from iic_booking.equipment.models import BookingEvent, BookingStatus, EquipmentManager
from iic_booking.users.models.user_type import UserType
from iic_booking.users.repositories.wallet_repository import WalletRepository
from iic_booking.users.tests.factories import UserFactory


@pytest.fixture
def sent(monkeypatch):
    calls = SimpleNamespace(emails=[], pushes=[], cc={})

    def _email(recipient, template=None, template_context=None, metadata=None, created_by=None, cc_emails=None):
        calls.emails.append((recipient, template, dict(template_context or {})))
        calls.cc[recipient] = list(cc_emails or [])
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
    return SimpleNamespace(
        student=student, faculty=faculty, oic=oic, booking=booking, equipment=equipment, state=state
    )


def _send(setup, created_by=None, **kwargs):
    event = booking_events.create_booking_event(
        booking=setup.booking, created_by=created_by or setup.student, send_notification=False, **kwargs
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


def _render(code, ctx):
    spec = next(t for t in get_default_email_templates() if t["code"] == code)
    template = CommunicationTemplate(
        code=spec["code"],
        name=spec["name"],
        communication_type="email",
        subject=spec["subject"],
        body_text=spec["body_text"],
        body_html=spec["body_html"],
    )
    return CommunicationService.render_template(template, ctx)


def test_faculty_gets_one_copy_of_the_student_email_with_booked_by(setup, sent):
    _send(setup, event_type="CREATED")

    faculty_ctx = _email_to(sent, setup.faculty)
    student_ctx = _email_to(sent, setup.student)
    assert faculty_ctx["user_name"] == "Prof. Ravi Kumar"
    assert faculty_ctx["booked_by_display"] == "Asha Verma (asha.verma@example.com)"
    assert faculty_ctx["comment"] == student_ctx["comment"]
    assert "Booked by" not in faculty_ctx["comment"]
    assert faculty_ctx["wallet_balance_after"] == student_ctx["wallet_balance_after"] == "₹500.00"

    assert student_ctx["user_name"] == "Asha Verma"
    assert student_ctx["booked_by_display"] == faculty_ctx["booked_by_display"]
    assert student_ctx["charged_to_display"] == faculty_ctx["charged_to_display"] == (
        "Prof. Ravi Kumar (ravi.kumar@example.com)"
    )
    assert sent.cc[setup.student] == []


def test_supervisor_email_renders_booked_by_row_and_prof_greeting(setup, sent):
    _send(setup, event_type="CREATED")

    faculty_out = _render("booking_created_email", _email_to(sent, setup.faculty))
    assert "Hello Prof. Ravi Kumar," in faculty_out["html_message"]
    assert "Booked by" in faculty_out["html_message"]
    assert "Asha Verma (asha.verma@example.com)" in faculty_out["html_message"]
    assert "- Booked by: Asha Verma (asha.verma@example.com)" in faculty_out["message"]
    assert "Booked by your student" not in faculty_out["html_message"]

    student_out = _render("booking_created_email", _email_to(sent, setup.student))
    assert "Hello Asha Verma," in student_out["html_message"]
    assert "- Booked by: Asha Verma (asha.verma@example.com)" in student_out["message"]
    assert "Booked by your student" not in student_out["html_message"]


def test_supervisor_booking_for_student_is_not_also_copied(setup, sent):
    _send(setup, event_type="CREATED", created_by=setup.faculty)

    assert _email_to(sent, setup.faculty)["booked_by_display"].startswith("Asha Verma")
    assert sent.cc[setup.student] == []


def test_other_creator_is_still_copied_on_the_student_email(setup, sent):
    clerk = UserFactory(user_type=UserType.FACULTY, name="Other Staff", admin_approved=True)

    _send(setup, event_type="CREATED", created_by=clerk)

    assert sent.cc[setup.student] == [clerk.email]
    assert _email_to(sent, setup.faculty)


def test_supervisor_who_is_also_the_oic_gets_one_email(setup, sent):
    EquipmentManager.objects.create(equipment=setup.equipment, manager=setup.faculty)

    _send(setup, event_type="CREATED")

    assert _email_to(sent, setup.faculty)["user_name"] == "Prof. Ravi Kumar"


@pytest.mark.parametrize(
    "name, expected",
    [
        ("Prof. Ravi Kumar", "Prof. Ravi Kumar"),
        ("prof Ravi Kumar", "prof Ravi Kumar"),
        ("Professor Ravi Kumar", "Professor Ravi Kumar"),
        ("Dr. Ravi Kumar", "Dr. Ravi Kumar"),
        ("Drona Rao", "Prof. Drona Rao"),
        ("", "ravi.kumar@example.com"),
    ],
)
def test_prof_prefix_is_not_doubled(setup, sent, name, expected):
    setup.faculty.name = name
    setup.faculty.save()

    _send(setup, event_type="CREATED")

    assert _email_to(sent, setup.faculty)["user_name"] == expected


def test_urgent_hold_confirmation_to_faculty_names_the_student(setup, sent):
    _send(
        setup,
        event_type="STATUS_CHANGED",
        previous_status=BookingStatus.HOLD,
        new_status=BookingStatus.BOOKED,
        metadata={"urgent_hold_converted": True},
    )

    faculty_ctx = _email_to(sent, setup.faculty)
    assert faculty_ctx["booked_by_display"] == "Asha Verma (asha.verma@example.com)"
    assert faculty_ctx["user_name"] == "Prof. Ravi Kumar"
    assert "Booked by" not in faculty_ctx["comment"]


def test_student_without_a_name_is_shown_once_by_email(setup, sent):
    setup.student.name = ""
    setup.student.save()

    _send(setup, event_type="CREATED")

    assert _email_to(sent, setup.faculty)["booked_by_display"] == "asha.verma@example.com"


def test_no_faculty_copy_when_student_pays_from_own_wallet(setup, sent):
    setup.state.wallet_owner = setup.student

    _send(setup, event_type="CREATED")

    assert setup.faculty not in [r for r, _t, _c in sent.emails]
    for recipient in (setup.student, setup.oic):
        ctx = _email_to(sent, recipient)
        assert ctx["booked_by_display"] == "Asha Verma (asha.verma@example.com)"
        assert ctx["charged_to_display"] == ""
        out = _render("booking_created_email", ctx)
        assert "Charged to wallet of" not in out["html_message"]
        assert "Charged to wallet of" not in out["message"]


def test_confirmation_to_oic_shows_booked_by_and_charged_to_rows(setup, sent):
    _send(setup, event_type="CREATED")

    oic_ctx = _email_to(sent, setup.oic)
    assert oic_ctx["user_name"] == "Meena OIC"
    assert oic_ctx["booked_by_display"] == "Asha Verma (asha.verma@example.com)"
    assert oic_ctx["charged_to_display"] == "Prof. Ravi Kumar (ravi.kumar@example.com)"
    assert "Booked by" not in oic_ctx["comment"]
    assert "Charged to the wallet of" not in oic_ctx["comment"]

    out = _render("booking_created_email", oic_ctx)
    assert "Charged to wallet of" in out["html_message"]
    assert "Prof. Ravi Kumar (ravi.kumar@example.com)" in out["html_message"]
    assert "- Charged to wallet of: Prof. Ravi Kumar (ravi.kumar@example.com)" in out["message"]


def test_urgent_hold_confirmation_to_oic_names_the_student(setup, sent):
    _send(
        setup,
        event_type="STATUS_CHANGED",
        previous_status=BookingStatus.HOLD,
        new_status=BookingStatus.BOOKED,
        metadata={"urgent_hold_converted": True},
    )

    assert _email_to(sent, setup.oic)["booked_by_display"].startswith("Asha Verma")


def test_cancellation_reason_to_oic_is_left_unchanged(setup, sent):
    _send(setup, event_type="CANCELLED", comment="Instrument down")

    assert _email_to(sent, setup.oic)["comment"] == "Instrument down"


@pytest.mark.parametrize(
    "event_kwargs, template",
    [
        ({"event_type": "CREATED"}, "booking_created_email"),
        ({"event_type": "CREATED", "metadata": {"from_waitlist": True}}, "booking_waitlist_confirmed_email"),
        ({"event_type": "CANCELLED", "comment": "Instrument down"}, "booking_cancelled_email"),
        ({"event_type": "RESCHEDULED"}, "booking_rescheduled_email"),
        ({"event_type": "COMPLETED"}, "booking_completed_email"),
        ({"event_type": "REFUNDED"}, "booking_refunded_email"),
        ({"event_type": "CHARGE_RECALCULATED"}, "booking_charge_recalculated_email"),
    ],
)
def test_every_recipient_sees_booked_by_and_charged_to_rows(setup, sent, event_kwargs, template):
    _send(setup, **event_kwargs)

    recipients = [setup.student, setup.oic]
    if event_kwargs["event_type"] in ("CREATED", "COMPLETED", "CHARGE_RECALCULATED"):
        recipients.append(setup.faculty)
    for recipient in recipients:
        sent_templates = [t for r, t, _c in sent.emails if r == recipient]
        assert sent_templates == [template], (recipient, sent_templates)
        out = _render(template, _email_to(sent, recipient))
        for body in (out["html_message"], out["message"]):
            assert "Booked by" in body
            assert "Asha Verma (asha.verma@example.com)" in body
            assert "Charged to wallet of" in body
            assert "Prof. Ravi Kumar (ravi.kumar@example.com)" in body


def test_created_note_drops_summary_lines_and_keeps_instructions(setup, sent):
    from iic_booking.communication.email_branding import build_booking_created_event_comment

    comment = build_booking_created_event_comment(
        equipment_name=setup.equipment.name,
        total_time_minutes=60,
        total_charge=100,
        booking_user=setup.student,
        created_by=setup.student,
    )
    _send(setup, event_type="CREATED", comment=comment)

    for recipient in (setup.student, setup.faculty, setup.oic):
        out = _render("booking_created_email", _email_to(sent, recipient))
        for body in (out["html_message"], out["message"]):
            assert "Booking created for" not in body
            assert not re.search(r"(?m)^\s*(Duration|Charges):", body)
            assert "Charges: ₹100.00" not in body
            assert "Booked by your student" not in body
            assert "This booking is charged to your wallet" not in body
            assert "Sample submission and collection" in body
            assert "You do not need to visit the laboratory" in body


def test_note_is_hidden_when_only_summary_lines_remain(setup, sent):
    _send(setup, event_type="CANCELLED", comment="Booking created for\nXRD\n\nDuration: 1 Hour\nCharges: ₹100.00")

    for recipient in (setup.student, setup.oic):
        ctx = _email_to(sent, recipient)
        assert ctx["comment"] == ""
        out = _render("booking_cancelled_email", ctx)
        assert "Reason" not in out["html_message"]
        assert "Reason:" not in out["message"]


def test_render_trims_note_even_when_sender_left_it_untrimmed(setup):
    out = _render(
        "booking_created_email",
        {
            "user_name": "Asha Verma",
            "booking_id": "IICX-1",
            "comment": "Booked by: Asha Verma (a@x).\nCharged to the wallet of: Ravi (r@x).\n\nBring the sample.",
        },
    )
    assert "Charged to the wallet of" not in out["html_message"]
    assert "Booked by: Asha" not in out["message"]
    assert "Bring the sample." in out["html_message"]


def test_reminder_and_not_utilized_emails_show_party_rows(setup, sent):
    from iic_booking.equipment.booking_not_utilized_service import send_booking_not_utilized_emails
    from iic_booking.equipment.booking_reminders import send_reminder_for_booking

    send_reminder_for_booking(setup.booking)
    send_booking_not_utilized_emails(setup.booking, [])

    for template in ("booking_reminder_email", "booking_not_utilized_email"):
        ctx = next(c for r, t, c in sent.emails if r == setup.student and t == template)
        out = _render(template, ctx)
        assert "Asha Verma (asha.verma@example.com)" in out["html_message"]
        assert "Prof. Ravi Kumar (ravi.kumar@example.com)" in out["html_message"]


def test_comment_email_keeps_the_author_text_verbatim():
    out = _render("booking_comment_email", {"user_name": "Asha", "comment": "Duration: please extend by 1 hour"})
    assert "Duration: please extend by 1 hour" in out["html_message"]


@pytest.mark.parametrize(
    "note, expected",
    [
        (
            "Booking created for\nPowder X-Ray Diffractometer (PXRD) [A]\n\nDuration: 1 Hour\nCharges: ₹100.00\n\n"
            "Sample submission and collection:\n• Morning: 10:00–10:30",
            "Sample submission and collection:\n• Morning: 10:00–10:30",
        ),
        (
            "Booked by: Akanksha Arya (a@x).\nCharged to the wallet of: Sanjeev Manhas (s@x).\n\nBooking created for\nPXRD",
            "",
        ),
        (
            "Booked by your student: Asha (a@x). This booking is charged to your wallet.\n\nKeep dry.",
            "Keep dry.",
        ),
        ("Hold created for\nSEM\n\nDuration: 2 Hours\nCharges: ₹10\n\nPayment pending: ₹10", "Payment pending: ₹10"),
        ("Booking created for TGA (135 minutes) on behalf of another user.", ""),
        ("Charges recalculated: previous ₹10.00, new ₹20.00.", "Charges recalculated: previous ₹10.00, new ₹20.00."),
        ("Instrument down", "Instrument down"),
    ],
)
def test_strip_booking_summary_from_note(note, expected):
    from iic_booking.communication.email_branding import strip_booking_summary_from_note

    assert strip_booking_summary_from_note(note) == expected
