"""Message the lab: booking users write to the equipment's Lab Operator(s) and Officer In-Charge(s)."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.core import mail
from django.utils import timezone

from iic_booking.equipment import booking_events, booking_lab_messages
from iic_booking.equipment.models import (
    BookingEvent,
    BookingEventType,
    BookingStatus,
    EquipmentManager,
    EquipmentOperator,
    EquipmentTemporaryOIC,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory


@pytest.fixture
def pushes(monkeypatch):
    calls = []

    def _push(recipient, title=None, message=None, template=None, template_context=None, metadata=None,
              created_by=None):
        calls.append(SimpleNamespace(recipient=recipient, title=title, message=message, metadata=metadata))

    monkeypatch.setattr(
        booking_lab_messages.CommunicationService, "send_push_notification", staticmethod(_push)
    )
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
    oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    lab = UserFactory(user_type=UserType.OPERATOR, admin_approved=True)
    EquipmentManager.objects.create(equipment=equipment, manager=oic)
    EquipmentOperator.objects.create(equipment=equipment, operator=lab)
    return SimpleNamespace(factory=egs_factory, equipment=equipment, student=student, booking=booking, oic=oic,
                           lab=lab)


def _url(booking, suffix=""):
    return f"/api/bookings/{booking.pk}/lab-messages/{suffix}"


def _post(setup, user=None, **body):
    client = setup.factory.client_for(user or setup.student)
    payload = {"message": "My sample will reach the lab tomorrow morning.", **body}
    return client.post(_url(setup.booking), payload, format="json")


def _event(pk):
    return BookingEvent.objects.select_related("booking", "booking__user", "booking__equipment", "created_by").get(
        event_id=pk
    )


def _supervisor_for(student):
    from iic_booking.users.models.wallet import Wallet, WalletJoinRequest, WalletJoinRequestStatus

    faculty = UserFactory(user_type=UserType.FACULTY, admin_approved=True)
    WalletJoinRequest.objects.create(
        student=student, faculty=faculty, wallet=Wallet.objects.create(user=faculty),
        status=WalletJoinRequestStatus.APPROVED,
    )
    return faculty


# --- permissions -----------------------------------------------------------------------------------------------


def test_owner_can_post_and_message_is_saved(setup, dispatched, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        resp = _post(setup, reason="sample_delayed")

    assert resp.status_code == 201, resp.content
    msg = resp.data["message"]
    assert msg["sender_role"] == "Booking user"
    assert msg["reason"] == "Sample submission delayed"
    assert msg["is_mine"] is True
    assert resp.data["remaining_today"] == booking_lab_messages.DAILY_LIMIT - 1
    event = _event(msg["id"])
    assert event.event_type == BookingEventType.COMMENT
    assert event.created_by == setup.student
    assert event.metadata["lab_message"] == "user"
    assert event.metadata["comment_recipients"] == {"user": False, "oic": True, "lab_incharge": True}
    assert dispatched == [event.event_id]


def test_supervisor_with_approved_wallet_link_can_post(setup, dispatched):
    faculty = _supervisor_for(setup.student)

    resp = _post(setup, user=faculty)

    assert resp.status_code == 201, resp.content
    assert resp.data["message"]["sender_role"] == "Supervisor"


@pytest.mark.parametrize("who", ["other_student", "oic", "lab", "admin", "dept_admin", "finance"])
def test_others_and_staff_cannot_post(setup, dispatched, who):
    users = {
        "other_student": lambda: setup.factory.student(),
        "oic": lambda: setup.oic,
        "lab": lambda: setup.lab,
        "admin": lambda: UserFactory(user_type=UserType.ADMIN, admin_approved=True),
        "dept_admin": lambda: UserFactory(user_type=UserType.DEPT_ADMIN, admin_approved=True),
        "finance": lambda: UserFactory(user_type=UserType.FINANCE, admin_approved=True),
    }

    resp = _post(setup, user=users[who]())

    assert resp.status_code == 403, resp.content
    assert not BookingEvent.objects.filter(booking=setup.booking, metadata__has_key="lab_message").exists()
    assert dispatched == []


def test_staff_who_booked_for_themselves_still_cannot_use_user_endpoint(setup, dispatched):
    setup.booking.user = setup.oic
    setup.booking.save(update_fields=["user"])

    resp = _post(setup, user=setup.oic)

    assert resp.status_code == 403


def test_thread_flags_for_owner_staff_and_stranger(setup, dispatched):
    _post(setup)
    owner = setup.factory.client_for(setup.student).get(_url(setup.booking))
    oic = setup.factory.client_for(setup.oic).get(_url(setup.booking))
    stranger = setup.factory.client_for(setup.factory.student()).get(_url(setup.booking))

    assert owner.status_code == 200
    assert owner.data["viewer"] == "booking_user"
    assert oic.data["viewer"] == "staff"
    assert owner.data["can_post"] is True
    assert owner.data["can_reply"] is False
    assert [r["label"] for r in owner.data["reasons"]] == [
        "Sample submission delayed",
        "Unable to attend / reach on time",
        "Change in sample details",
        "Query about results",
        "Other",
    ]
    assert len(owner.data["messages"]) == 1
    assert oic.status_code == 200
    assert oic.data["can_post"] is False
    assert oic.data["can_reply"] is True
    assert oic.data["messages"][0]["is_mine"] is False
    assert stranger.status_code == 403


def test_thread_excludes_ordinary_comments(setup, dispatched):
    booking_events.create_booking_event(
        booking=setup.booking, event_type="COMMENT", created_by=setup.lab, comment="Internal note",
        send_notification=False,
    )
    _post(setup)

    data = setup.factory.client_for(setup.student).get(_url(setup.booking)).data

    assert [m["message"] for m in data["messages"]] == ["My sample will reach the lab tomorrow morning."]


def test_staff_reply_appears_in_thread_and_notifies_user(setup, dispatched):
    _post(setup)
    client = setup.factory.client_for(setup.lab)

    resp = client.post(_url(setup.booking, "reply/"), {"message": "Noted, we will hold your slot."}, format="json")

    assert resp.status_code == 201, resp.content
    event = _event(resp.data["message"]["id"])
    assert event.metadata["lab_message"] == "staff_reply"
    assert event.metadata["comment_recipients"] == {"user": True, "oic": True, "lab_incharge": True}
    thread = setup.factory.client_for(setup.student).get(_url(setup.booking)).data["messages"]
    assert [(m["sender_role"], m["kind"]) for m in thread] == [("Booking user", "user"), ("Lab Operator", "staff_reply")]


def test_only_equipment_staff_can_reply(setup, dispatched):
    other_oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    for user in (setup.student, other_oic):
        resp = setup.factory.client_for(user).post(_url(setup.booking, "reply/"), {"message": "Hi"}, format="json")
        assert resp.status_code == 403


# --- validation, rate limit, allowed statuses ------------------------------------------------------------------


@pytest.mark.parametrize(
    "body, error",
    [
        ({"message": "   "}, "Please write a message."),
        ({"message": "x" * 1001}, "within 1000 characters"),
        ({"reason": "made_up"}, "valid reason"),
    ],
)
def test_validation(setup, dispatched, body, error):
    resp = _post(setup, **body)

    assert resp.status_code == 400
    assert error in resp.data["error"]


def test_exactly_max_length_is_accepted(setup, dispatched):
    assert _post(setup, message="x" * 1000).status_code == 201


def test_rate_limit_per_booking_per_24_hours(setup, dispatched):
    for _ in range(booking_lab_messages.DAILY_LIMIT):
        assert _post(setup).status_code == 201

    resp = _post(setup)

    assert resp.status_code == 429
    assert "10 messages" in resp.data["error"]
    BookingEvent.objects.filter(booking=setup.booking).update(created_at=timezone.now() - timedelta(hours=25))
    assert _post(setup).status_code == 201


def _close(booking, status, days_ago):
    booking.status = status
    if status == BookingStatus.COMPLETED:
        booking.completed_at = timezone.now() - timedelta(days=days_ago)
    booking.save()
    if status != BookingStatus.COMPLETED:
        event = BookingEvent.objects.create(booking=booking, event_type="STATUS_CHANGED", new_status=status)
        BookingEvent.objects.filter(pk=event.pk).update(created_at=timezone.now() - timedelta(days=days_ago))


@pytest.mark.parametrize(
    "status, days_ago, allowed",
    [
        (BookingStatus.CANCELLED, 2, True),
        (BookingStatus.CANCELLED, 8, False),
        (BookingStatus.REFUNDED, 8, False),
        (BookingStatus.COMPLETED, 10, True),
        (BookingStatus.COMPLETED, 31, False),
    ],
)
def test_closed_booking_window(setup, dispatched, status, days_ago, allowed):
    _close(setup.booking, status, days_ago)

    resp = _post(setup)
    thread = setup.factory.client_for(setup.student).get(_url(setup.booking)).data

    assert resp.status_code == (201 if allowed else 400), resp.content
    assert thread["can_post"] is allowed
    assert bool(thread["closed_reason"]) is not allowed


def test_waitlisted_booking_cannot_be_messaged(setup, dispatched):
    setup.booking.status = BookingStatus.WAITLISTED
    setup.booking.save(update_fields=["status"])

    resp = _post(setup)

    assert resp.status_code == 400
    assert "waitlist" in resp.data["error"]


# --- notifications ---------------------------------------------------------------------------------------------


def _send(setup, user=None, **body):
    resp = _post(setup, user=user, **body)
    assert resp.status_code == 201, resp.content
    booking_events.send_booking_event_notification(_event(resp.data["message"]["id"]))
    return resp


def test_recipients_are_only_this_equipments_operators_and_oics(setup, dispatched, pushes):
    temp_oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    EquipmentTemporaryOIC.objects.create(
        equipment=setup.equipment, primary_oic=setup.oic, temporary_oic=temp_oic,
        resume_at=timezone.now() + timedelta(days=2),
    )
    expired_temp = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    EquipmentTemporaryOIC.objects.create(
        equipment=setup.equipment, primary_oic=setup.oic, temporary_oic=expired_temp,
        resume_at=timezone.now() - timedelta(days=1),
    )
    other_equipment = setup.factory.equipment()
    other_oic = UserFactory(user_type=UserType.MANAGER, admin_approved=True)
    other_lab = UserFactory(user_type=UserType.OPERATOR, admin_approved=True)
    EquipmentManager.objects.create(equipment=other_equipment, manager=other_oic)
    EquipmentOperator.objects.create(equipment=other_equipment, operator=other_lab)
    UserFactory(user_type=UserType.ADMIN, admin_approved=True)

    _send(setup, reason="sample_delayed")

    emailed = sorted(addr for m in mail.outbox for addr in m.to)
    assert emailed == sorted([setup.oic.email, setup.lab.email, temp_oic.email])
    assert sorted(p.recipient.id for p in pushes) == sorted([setup.oic.id, setup.lab.id, temp_oic.id])
    assert setup.student.email not in emailed


def test_email_content(setup, dispatched, pushes):
    resp = _send(setup, reason="unable_to_attend", message="Train delayed <b>2 hours</b>\nWill come at noon.")

    lab_mail = next(m for m in mail.outbox if m.to == [setup.lab.email])
    html_body = lab_mail.alternatives[0][0]
    ref = setup.booking.virtual_booking_id
    assert ref in lab_mail.subject
    assert setup.equipment.name in lab_mail.subject
    for text in (ref, "Booked by", setup.student.email, "Unable to attend / reach on time", "Open booking",
                 f"/booking-management?expand={setup.booking.pk}"):
        assert text in html_body, text
    assert "Train delayed &lt;b&gt;2 hours&lt;/b&gt;" in html_body
    assert "<b>2 hours</b>" not in html_body
    assert "Train delayed <b>2 hours</b>" in lab_mail.body
    assert "Booked by" in lab_mail.body
    push = next(p for p in pushes if p.recipient == setup.lab)
    assert push.title == f"Unable to attend / reach on time — {ref}"
    assert "Train delayed" in push.message
    assert push.metadata["link"].endswith(f"/booking-management?expand={setup.booking.pk}")
    assert resp.data["message"]["reason"] == "Unable to attend / reach on time"


def test_sender_is_never_emailed(setup, dispatched, pushes):
    faculty = _supervisor_for(setup.student)
    EquipmentOperator.objects.filter(equipment=setup.equipment).delete()

    _send(setup, user=faculty)

    assert [m.to for m in mail.outbox] == [[setup.oic.email]]
    assert faculty.email not in [a for m in mail.outbox for a in m.to]


def test_message_saved_and_in_app_sent_when_email_fails(setup, pushes, monkeypatch,
                                                         django_capture_on_commit_callbacks):
    def _boom(*args, **kwargs):
        raise RuntimeError("SMTP down")

    monkeypatch.setattr(booking_lab_messages.CommunicationService, "send_email", staticmethod(_boom))
    monkeypatch.setattr(
        booking_events,
        "_dispatch_booking_event_notification",
        lambda event_id: booking_events.send_booking_event_notification(_event(event_id)),
    )

    with django_capture_on_commit_callbacks(execute=True):
        resp = _post(setup)

    assert resp.status_code == 201, resp.content
    assert BookingEvent.objects.filter(pk=resp.data["message"]["id"]).exists()
    assert sorted(p.recipient.id for p in pushes) == sorted([setup.oic.id, setup.lab.id])


def test_warns_when_no_lab_staff_assigned(setup, dispatched):
    EquipmentManager.objects.filter(equipment=setup.equipment).delete()
    EquipmentOperator.objects.filter(equipment=setup.equipment).delete()

    resp = _post(setup)

    assert resp.status_code == 201
    assert resp.data["warnings"] and "saved on the booking" in resp.data["warnings"][0]


def test_template_row_is_created_on_first_send(setup, dispatched, pushes):
    from iic_booking.communication.models import CommunicationTemplate

    CommunicationTemplate.objects.filter(code=booking_lab_messages.EMAIL_TEMPLATE_CODE).delete()

    _send(setup)

    assert CommunicationTemplate.objects.filter(code=booking_lab_messages.EMAIL_TEMPLATE_CODE).count() == 1
    assert len(mail.outbox) == 2
