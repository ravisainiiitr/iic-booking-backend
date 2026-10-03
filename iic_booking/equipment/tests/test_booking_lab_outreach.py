"""Reminders and questions from the equipment's OIC / Lab Operator to the booking user, and the user's reply."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.core import mail
from django.utils import timezone

from iic_booking.equipment import booking_events, booking_lab_messages, booking_lab_outreach
from iic_booking.equipment.models import (
    BookingEvent,
    BookingSampleTrace,
    BookingStatus,
    EquipmentManager,
    EquipmentOperator,
    EquipmentTemporaryOIC,
    SampleTraceStatus,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


@pytest.fixture
def pushes(monkeypatch):
    calls = []

    def _push(recipient, title=None, message=None, template=None, template_context=None, metadata=None,
              created_by=None):
        calls.append(SimpleNamespace(recipient=recipient, title=title, message=message, metadata=metadata))

    for module in (booking_lab_messages, booking_lab_outreach):
        monkeypatch.setattr(module.CommunicationService, "send_push_notification", staticmethod(_push))
    return calls


@pytest.fixture
def dispatched(monkeypatch):
    calls = []
    monkeypatch.setattr(booking_events, "_dispatch_booking_event_notification", calls.append)
    return calls


@pytest.fixture
def setup(egs_factory):
    equipment = egs_factory.equipment()
    student = egs_factory.student()
    booking = egs_factory.booking(student, equipment, egs_factory.future())
    oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True, name="Dr. Asha Verma",
                      department=egs_factory.department)
    lab = UserFactory(user_type=UserType.OPERATOR, admin_approved=True, name="Ravi Kumar",
                      department=egs_factory.department)
    EquipmentManager.objects.create(equipment=equipment, manager=oic, honorific="Prof.")
    EquipmentOperator.objects.create(equipment=equipment, operator=lab)
    return SimpleNamespace(factory=egs_factory, equipment=equipment, student=student, booking=booking, oic=oic,
                           lab=lab)


def _url(booking, suffix=""):
    return f"/api/bookings/{booking.pk}/lab-messages/{suffix}"


def _send(setup, kind="reminder", user=None, **body):
    client = setup.factory.client_for(user or setup.lab)
    payload = {"message": "Please bring your sample by 10 AM tomorrow.", **body}
    return client.post(_url(setup.booking, f"{kind}/"), payload, format="json")


def _event(pk):
    return BookingEvent.objects.select_related("booking", "booking__user", "booking__equipment", "created_by").get(
        event_id=pk
    )


def _notify(resp):
    booking_events.send_booking_event_notification(_event(resp.data["message"]["id"]))


def _supervisor_for(student):
    from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus

    faculty = UserFactory(user_type=UserType.FACULTY, admin_approved=True)
    WalletJoinRequest.objects.create(
        student=student, faculty=faculty, wallet=Wallet.objects.create(user=faculty),
        status=WalletJoinRequestStatus.APPROVED,
    )
    return faculty


# --- permissions -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind, saved_kind", [("reminder", "staff_reminder"), ("question", "staff_question")])
def test_operator_sends_and_message_is_saved_on_the_booking(setup, dispatched, kind, saved_kind,
                                                            django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        resp = _send(setup, kind, preset="custom")

    assert resp.status_code == 201, resp.content
    event = _event(resp.data["message"]["id"])
    assert event.created_by == setup.lab
    assert event.metadata["lab_message"] == saved_kind
    assert event.metadata["comment_recipients"] == {"user": True, "oic": False, "lab_incharge": False}
    assert bool(event.metadata.get("question_open")) is (kind == "question")
    assert dispatched == [event.event_id]
    assert resp.data["message"]["sender_role"] == "Lab Operator"
    assert resp.data["duplicate"] is False


@pytest.mark.parametrize("who", ["oic", "temp_oic", "admin"])
def test_oic_temporary_oic_and_main_admin_can_send(setup, dispatched, who):
    temp = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    EquipmentTemporaryOIC.objects.create(
        equipment=setup.equipment, primary_oic=setup.oic, temporary_oic=temp,
        resume_at=timezone.now() + timedelta(days=2),
    )
    users = {
        "oic": setup.oic,
        "temp_oic": temp,
        "admin": UserFactory(user_type=UserType.ADMIN, admin_approved=True),
    }

    resp = _send(setup, "question", user=users[who])

    assert resp.status_code == 201, resp.content


@pytest.mark.parametrize(
    "who", ["owner", "other_student", "other_operator", "other_oic", "expired_temp_oic", "dept_admin", "finance"]
)
def test_others_cannot_send(setup, dispatched, who):
    other_eq = setup.factory.equipment()
    other_lab = UserFactory(user_type=UserType.OPERATOR, admin_approved=True)
    EquipmentOperator.objects.create(equipment=other_eq, operator=other_lab)
    other_oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    EquipmentManager.objects.create(equipment=other_eq, manager=other_oic)
    expired = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    EquipmentTemporaryOIC.objects.create(
        equipment=setup.equipment, primary_oic=setup.oic, temporary_oic=expired,
        resume_at=timezone.now() - timedelta(hours=1),
    )
    users = {
        "owner": setup.student,
        "other_student": setup.factory.student(),
        "other_operator": other_lab,
        "other_oic": other_oic,
        "expired_temp_oic": expired,
        "dept_admin": UserFactory(user_type=UserType.DEPT_ADMIN, admin_approved=True),
        "finance": UserFactory(user_type=UserType.FINANCE, admin_approved=True),
    }

    for kind in ("reminder", "question"):
        resp = _send(setup, kind, user=users[who])
        assert resp.status_code == 403, (kind, resp.content)
    assert not BookingEvent.objects.filter(booking=setup.booking, metadata__has_key="lab_message").exists()
    assert dispatched == []


def test_waitlisted_and_long_closed_bookings_cannot_be_messaged(setup, dispatched):
    setup.booking.status = BookingStatus.WAITLISTED
    setup.booking.save(update_fields=["status"])
    assert _send(setup).status_code == 400

    setup.booking.status = BookingStatus.COMPLETED
    setup.booking.completed_at = timezone.now() - timedelta(days=31)
    setup.booking.save(update_fields=["status", "completed_at"])
    resp = _send(setup)
    assert resp.status_code == 400
    assert "closed" in resp.data["error"]


@pytest.mark.parametrize(
    "body, error",
    [
        ({"message": "  "}, "Please write the question."),
        ({"message": "x" * 1001}, "within 1000 characters"),
        ({"reply_by": "2020-01-01"}, "cannot be in the past"),
        ({"reply_by": "not-a-date"}, "valid reply-by date"),
    ],
)
def test_question_validation(setup, dispatched, body, error):
    resp = _send(setup, "question", **body)

    assert resp.status_code == 400
    assert error in resp.data["error"]


# --- limits ----------------------------------------------------------------------------------------------------


def test_reminders_are_rate_limited_per_booking_per_24_hours(setup, dispatched):
    for i in range(booking_lab_outreach.REMINDER_DAILY_LIMIT):
        sender = setup.lab if i % 2 == 0 else setup.oic
        assert _send(setup, user=sender, message=f"Reminder number {i}").status_code == 201

    resp = _send(setup, user=setup.oic, message="One more")

    assert resp.status_code == 429
    assert f"Up to {booking_lab_outreach.REMINDER_DAILY_LIMIT} reminders" in resp.data["error"]
    assert _send(setup, "question", message="Questions have their own limit").status_code == 201
    BookingEvent.objects.filter(booking=setup.booking).update(created_at=timezone.now() - timedelta(hours=25))
    assert _send(setup, message="Next day").status_code == 201


def test_double_click_does_not_send_twice(setup, dispatched, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        first = _send(setup)
        second = _send(setup)

    assert first.status_code == 201
    assert second.status_code == 200
    assert second.data["duplicate"] is True
    assert second.data["message"]["id"] == first.data["message"]["id"]
    assert BookingEvent.objects.filter(booking=setup.booking, metadata__has_key="lab_message").count() == 1
    assert len(dispatched) == 1


def test_same_client_request_id_is_not_sent_twice(setup, dispatched):
    first = _send(setup, client_request_id="abc-123")
    retry = _send(setup, client_request_id="abc-123", message="Edited text after a timeout")

    assert first.status_code == 201
    assert retry.status_code == 200 and retry.data["duplicate"] is True
    assert BookingEvent.objects.filter(booking=setup.booking, metadata__has_key="lab_message").count() == 1


def test_same_text_after_the_duplicate_window_is_a_new_reminder(setup, dispatched):
    first = _send(setup)
    BookingEvent.objects.filter(pk=first.data["message"]["id"]).update(
        created_at=timezone.now() - timedelta(seconds=booking_lab_outreach.DUPLICATE_WINDOW_SECONDS + 5)
    )

    assert _send(setup).status_code == 201


# --- email and in-app to the user ------------------------------------------------------------------------------


def test_reminder_emails_only_the_booking_user(setup, dispatched, pushes):
    supervisor = _supervisor_for(setup.student)

    _notify(_send(setup, user=setup.oic, message="Your slot is tomorrow at <b>10:00</b>.\nPlease be on time."))

    assert len(mail.outbox) == 1
    email = mail.outbox[0]
    assert email.to == [setup.student.email]
    assert supervisor.email not in email.cc and supervisor.email not in email.to
    ref = setup.booking.virtual_booking_id
    assert email.subject == f"Reminder from the lab \u2013 booking {ref} \u2013 {setup.equipment.name}"
    html_body = email.alternatives[0][0]
    for text in (ref, setup.equipment.name, "Prof. Asha Verma, Officer In-Charge", "Reply in portal",
                 f"/my-bookings?booking={ref}", "Booked by", "Start time"):
        assert text in html_body, text
    assert "Your slot is tomorrow at &lt;b&gt;10:00&lt;/b&gt;." in html_body
    assert "<b>10:00</b>" not in html_body
    assert "Reply needed by" not in html_body
    assert "Your slot is tomorrow at <b>10:00</b>." in email.body
    assert [p.recipient for p in pushes] == [setup.student]
    assert pushes[0].title == f"Reminder from the lab \u2014 {ref}"
    assert pushes[0].metadata["link"].endswith(f"/my-bookings?booking={ref}")


def test_question_email_says_reply_needed_with_date(setup, dispatched, pushes):
    reply_by = (timezone.localdate() + timedelta(days=2)).isoformat()

    _notify(_send(setup, "question", message="Is the sample non-magnetic?", reply_by=reply_by))

    email = mail.outbox[0]
    assert email.subject.startswith("Question from the lab \u2013 reply needed \u2013 booking")
    html_body = email.alternatives[0][0]
    d = timezone.localdate() + timedelta(days=2)
    due = f"{d.day} {d.strftime('%b %Y')}"
    assert "Reply needed by" in html_body and due in html_body
    assert "Ravi Kumar, Lab Operator" in html_body
    assert "Please reply from the booking page" in html_body
    assert pushes[0].title.startswith("Question from the lab \u2014 reply needed")
    assert due in pushes[0].message
    assert pushes[0].metadata["notification_type"] == "warning"


def test_slot_times_are_hidden_for_slot_id_equipment(setup, dispatched, pushes):
    setup.equipment.weekly_view_display = "SLOT_ID"
    setup.equipment.save(update_fields=["weekly_view_display"])

    _notify(_send(setup))

    html_body = mail.outbox[0].alternatives[0][0]
    assert "Start time" not in html_body
    assert "Booking date" in html_body


def test_email_failure_keeps_the_message_and_still_notifies_in_app(setup, dispatched, pushes, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("SMTP down")

    monkeypatch.setattr(booking_lab_outreach.CommunicationService, "send_email", staticmethod(_boom))

    resp = _send(setup)
    _notify(resp)

    assert BookingEvent.objects.filter(pk=resp.data["message"]["id"]).exists()
    assert [p.recipient for p in pushes] == [setup.student]


def test_template_row_is_created_once_and_a_customised_row_is_kept(setup, dispatched, pushes):
    from iic_booking.communication.models import CommunicationTemplate

    CommunicationTemplate.objects.filter(code=booking_lab_outreach.EMAIL_TEMPLATE_CODE).delete()
    _notify(_send(setup, message="First"))
    row = CommunicationTemplate.objects.get(code=booking_lab_outreach.EMAIL_TEMPLATE_CODE)
    row.subject = "Custom: {{ message_heading }} {{ booking_id }}"
    row.save(update_fields=["subject"])

    _notify(_send(setup, message="Second"))

    assert CommunicationTemplate.objects.filter(code=booking_lab_outreach.EMAIL_TEMPLATE_CODE).count() == 1
    assert mail.outbox[-1].subject.startswith("Custom: Reminder from the lab")


# --- reply flow ------------------------------------------------------------------------------------------------


def _ask(setup, user=None, **body):
    resp = _send(setup, "question", user=user, **{"message": "Which detector do you want?", **body})
    assert resp.status_code == 201, resp.content
    return resp.data["message"]["id"]


def _reply(setup, question_id, user=None, message="Please use the EDS detector."):
    client = setup.factory.client_for(user or setup.student)
    return client.post(_url(setup.booking), {"message": message, "in_reply_to": question_id}, format="json")


def test_user_reply_marks_the_question_answered_and_notifies_the_asker(setup, dispatched, pushes):
    question_id = _ask(setup)

    resp = _reply(setup, question_id)

    assert resp.status_code == 201, resp.content
    assert resp.data["message"]["in_reply_to"] == question_id
    question = _event(question_id)
    assert question.metadata["question_open"] is False
    assert question.metadata["answered_by_event_id"] == resp.data["message"]["id"]
    _notify(resp)
    emailed = sorted(a for m in mail.outbox for a in m.to)
    assert emailed == sorted([setup.lab.email, setup.oic.email])
    lab_mail = next(m for m in mail.outbox if m.to == [setup.lab.email])
    assert "Which detector do you want?" in lab_mail.alternatives[0][0]
    assert "Please use the EDS detector." in lab_mail.alternatives[0][0]
    titles = {p.recipient.id: p.title for p in pushes}
    assert titles[setup.lab.id].startswith("Reply to your question")
    assert titles[setup.oic.id].startswith("Reply to lab question")


def test_admin_who_asked_gets_the_reply_too(setup, dispatched, pushes):
    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True)
    question_id = _ask(setup, user=admin)

    _notify(_reply(setup, question_id))

    assert admin.email in [a for m in mail.outbox for a in m.to]
    assert {p.recipient.id for p in pushes} == {setup.lab.id, setup.oic.id, admin.id}


def test_supervisor_can_answer_and_strangers_cannot(setup, dispatched):
    question_id = _ask(setup)

    assert _reply(setup, question_id, user=setup.factory.student()).status_code == 403
    assert _reply(setup, question_id, user=setup.oic).status_code == 403
    assert _reply(setup, question_id, user=_supervisor_for(setup.student)).status_code == 201
    assert _event(question_id).metadata["question_open"] is False


def test_reply_to_something_that_is_not_a_question_is_refused(setup, dispatched):
    reminder = _send(setup).data["message"]["id"]

    resp = _reply(setup, reminder)

    assert resp.status_code == 400
    assert _reply(setup, 999999).status_code == 400


def test_open_question_can_be_answered_after_the_message_window_closed(setup, dispatched):
    question_id = _ask(setup)
    setup.booking.status = BookingStatus.COMPLETED
    setup.booking.completed_at = timezone.now() - timedelta(days=40)
    setup.booking.save(update_fields=["status", "completed_at"])
    client = setup.factory.client_for(setup.student)

    assert client.post(_url(setup.booking), {"message": "New topic"}, format="json").status_code == 400
    assert _reply(setup, question_id).status_code == 201
    assert _reply(setup, question_id, message="And one more thing").status_code == 400


def test_staff_can_resolve_a_question_and_users_cannot(setup, dispatched):
    question_id = _ask(setup)

    denied = setup.factory.client_for(setup.student).post(_url(setup.booking, f"{question_id}/resolve/"))
    resolved = setup.factory.client_for(setup.oic).post(_url(setup.booking, f"{question_id}/resolve/"))

    assert denied.status_code == 403
    assert resolved.status_code == 200
    assert resolved.data["message"]["question_open"] is False
    assert resolved.data["message"]["resolved_at"]


# --- thread, lists and the awaiting-reply view -----------------------------------------------------------------


def test_thread_shows_open_questions_to_the_user_and_send_options_to_staff(setup, dispatched):
    question_id = _ask(setup)

    owner = setup.factory.client_for(setup.student).get(_url(setup.booking)).data
    lab = setup.factory.client_for(setup.lab).get(_url(setup.booking)).data

    assert owner["open_question_count"] == 1
    assert owner["can_answer"] is True
    assert owner["outreach"] is None
    question = next(m for m in owner["messages"] if m["id"] == question_id)
    assert question["kind"] == "staff_question" and question["question_open"] is True
    assert question["sender_name"] == "Ravi Kumar" and question["sender_role"] == "Lab Operator"
    outreach = lab["outreach"]
    assert outreach["can_send"] is True
    codes = [p["code"] for p in outreach["reminder"]["presets"]]
    assert codes[0] == "slot_upcoming" and "sample_pending" in codes and codes[-1] == "custom"
    assert "collect_results" not in codes
    assert outreach["question"]["remaining_today"] == booking_lab_outreach.QUESTION_DAILY_LIMIT - 1
    assert outreach["reminder"]["email_subject"].startswith("Reminder from the lab \u2013 booking ")
    assert outreach["recipient_name"]


def test_presets_follow_the_booking_state(setup, dispatched):
    BookingSampleTrace.objects.create(booking=setup.booking, status=SampleTraceStatus.SAMPLE_SENT)
    setup.booking.status = BookingStatus.COMPLETED
    setup.booking.completed_at = timezone.now()
    setup.booking.save(update_fields=["status", "completed_at"])

    codes = [p["code"] for p in booking_lab_outreach.reminder_presets(setup.booking)]

    assert codes == ["collect_results", "custom"]


def test_list_rows_and_awaiting_view_show_open_questions(setup, dispatched):
    other_booking = setup.factory.booking(setup.student, setup.equipment, setup.factory.future(days=5))
    _ask(setup)
    _ask(setup, message="Second question")

    rows = setup.factory.client_for(setup.lab).get("/api/bookings/", {"list_view": "1", "limit": 50}).data
    by_id = {r["real_booking_id"]: r for r in rows["bookings"]}
    assert by_id[setup.booking.pk]["lab_questions_open"] == 2
    assert by_id[other_booking.pk]["lab_questions_open"] == 0

    awaiting = setup.factory.client_for(setup.oic).get("/api/lab-questions/awaiting/").data
    assert awaiting["count"] == 2
    assert {i["booking_id"] for i in awaiting["items"]} == {setup.booking.pk}

    other_eq = setup.factory.equipment()
    other_lab = UserFactory(user_type=UserType.OPERATOR, admin_approved=True)
    EquipmentOperator.objects.create(equipment=other_eq, operator=other_lab)
    assert setup.factory.client_for(other_lab).get("/api/lab-questions/awaiting/").data["count"] == 0
    assert setup.factory.client_for(setup.student).get("/api/lab-questions/awaiting/").status_code == 403
    admin = UserFactory(user_type=UserType.ADMIN, admin_approved=True)
    assert setup.factory.client_for(admin).get("/api/lab-questions/awaiting/").data["count"] == 2


def test_history_keeps_who_sent_what(setup, dispatched):
    _send(setup, user=setup.oic, preset="slot_upcoming", message="Your slot is on Monday.")

    rows = setup.factory.client_for(setup.student).get(f"/api/bookings/{setup.booking.pk}/events/").data["events"]
    reminder = next(e for e in rows if (e.get("metadata") or {}).get("lab_message") == "staff_reminder")
    assert reminder["comment"] == "Your slot is on Monday."
    assert reminder["metadata"]["lab_message_preset"] == "slot_upcoming"
    assert reminder["metadata"]["sender_user_id"] == setup.oic.pk
