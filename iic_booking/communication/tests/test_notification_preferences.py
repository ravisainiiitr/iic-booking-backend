"""Per-user notification settings: categories, the settings API and enforcement in CommunicationService."""

from __future__ import annotations

import pytest
from rest_framework.test import APIClient

from iic_booking.communication import notification_preferences as prefs
from iic_booking.communication.models import CommunicationLog, CommunicationTemplate, NotificationPreference
from iic_booking.communication.service import CommunicationService
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("registration_approved_email", "account"),
        ("booking_confirmed_email", "bookings"),
        ("booking_cancelled_push", "bookings"),
        ("booking_completed_email", "results"),
        ("booking_reminder_email", "reminders"),
        ("wallet_low_balance_email", "reminders"),
        ("wallet_credit_email", "payments"),
        ("waitlist_slots_available_email", "waitlist"),
        ("booking_completion_overdue_staff_email", "staff"),
        ("training_call_published_email", "training"),
        ("operator_unavailable_email", "bookings"),
        ("support_ticket_resolution_email", prefs.OTHER),
    ],
)
def test_category_for_template(code, expected):
    assert prefs.category_for(code) == expected


def test_category_for_metadata_and_title():
    assert prefs.category_for(None, {"notification_category": "waitlist"}) == "waitlist"
    assert prefs.category_for("admin_bulk_email", {"staff_recipient": True, "event_type": "COMMENT"}) == "messages"
    assert prefs.category_for("admin_bulk_email", {"staff_recipient": True, "event_type": "CREATED"}) == "staff"
    assert prefs.category_for(None, {}, "Reminder: your slot starts soon") == "reminders"
    assert prefs.category_for(None, {}, "Hello") == prefs.OTHER


def _email_template(code):
    return CommunicationTemplate.objects.create(
        code=code, name=code, communication_type="email", subject="Subject", body_text="Body", is_active=True
    )


def test_switched_off_email_is_not_sent_but_locked_category_is():
    user = UserFactory(admin_approved=True, user_type=UserType.STUDENT)
    _email_template("booking_reminder_email")
    _email_template("booking_confirmed_email")
    NotificationPreference.objects.create(user=user, category="reminders", email_enabled=False)
    NotificationPreference.objects.create(user=user, category="bookings", email_enabled=False)

    assert CommunicationService.send_email(user, template="booking_reminder_email") is None
    log = CommunicationService.send_email(user, template="booking_confirmed_email")
    assert log is not None
    assert log.metadata["notification_category"] == "bookings"


def test_switched_off_in_app_is_not_stored_and_category_is_recorded():
    user = UserFactory(admin_approved=True, user_type=UserType.STUDENT)
    NotificationPreference.objects.create(user=user, category="messages", in_app_enabled=False)

    skipped = CommunicationService.send_push_notification(user, title="New comment on your booking", message="Hi")
    assert skipped is None
    kept = CommunicationService.send_push_notification(user, title="Results ready", message="Download now")
    assert kept.metadata["notification_category"] == "results"
    assert CommunicationLog.objects.filter(recipient=user).count() == 1


def test_preferences_api_round_trip_ignores_locked_rows():
    user = UserFactory(admin_approved=True, user_type=UserType.STUDENT)
    client = APIClient()
    client.force_authenticate(user=user)

    body = client.get("/api/notifications/preferences/").json()
    keys = [c["key"] for c in body["categories"]]
    assert "staff" not in keys and "reminders" in keys
    assert all(c["email"] and c["in_app"] for c in body["categories"])

    resp = client.put(
        "/api/notifications/preferences/",
        {"categories": [{"key": "reminders", "email": False}, {"key": "bookings", "email": False}]},
        format="json",
    )
    assert resp.status_code == 200
    rows = {c["key"]: c for c in resp.json()["categories"]}
    assert rows["reminders"]["email"] is False and rows["reminders"]["in_app"] is True
    assert rows["bookings"]["email"] is True
    assert not NotificationPreference.objects.filter(user=user, category="bookings").exists()

    assert client.put("/api/notifications/preferences/", {"categories": "x"}, format="json").status_code == 400


def test_staff_see_lab_operations_category():
    staff = UserFactory(admin_approved=True, user_type=UserType.MANAGER)
    client = APIClient()
    client.force_authenticate(user=staff)
    keys = [c["key"] for c in client.get("/api/notifications/preferences/").json()["categories"]]
    assert "staff" in keys


def test_inbox_filters_and_pages():
    user = UserFactory(admin_approved=True, user_type=UserType.STUDENT)
    for title in ("Results ready", "Results ready again", "Reminder: slot tomorrow"):
        CommunicationService.send_push_notification(user, title=title, message="m")
    read = CommunicationLog.objects.filter(recipient=user, subject="Results ready").get()
    read.status = CommunicationLog.CommunicationStatus.READ
    read.save(update_fields=["status"])

    client = APIClient()
    client.force_authenticate(user=user)
    body = client.get("/api/notifications/inbox/", {"page_size": 5}).json()
    assert body["count"] == 3 and body["unread_count"] == 2
    assert body["category_counts"] == {"results": 2, "reminders": 1}

    results = client.get("/api/notifications/inbox/", {"category": "results"}).json()
    assert [r["title"] for r in results["results"]] == ["Results ready again", "Results ready"]
    unread = client.get("/api/notifications/inbox/", {"unread": "1", "q": "results"}).json()
    assert [r["title"] for r in unread["results"]] == ["Results ready again"]

    listed = client.get("/api/notifications/").json()
    assert {r["category"] for r in listed} == {"results", "reminders"}
