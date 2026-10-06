"""New support tickets are copied to the Main Administrator's alert list; portal feedback is Main Admin only."""

from __future__ import annotations

import pytest
from django.core import mail
from rest_framework.test import APIClient

from iic_booking.communication.models import CommunicationLog
from iic_booking.equipment.models import EquipmentManager
from iic_booking.support import ticket_alerts
from iic_booking.support.models import PortalFeedback, SupportNotificationSettings, Ticket
from iic_booking.users.models.test_account_email_settings import TestAccountEmailSettings
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

SETTINGS_URL = "/api/support/notification-settings/"
FEEDBACK_URL = "/api/portal-feedback/"


def _client(user=None):
    client = APIClient()
    if user is not None:
        client.force_authenticate(user)
    return client


@pytest.fixture
def inline_alerts(monkeypatch, settings):
    settings.FRONTEND_URL = "https://portal.test"
    monkeypatch.setattr(ticket_alerts, "_run_in_background", lambda fn: fn())


@pytest.fixture
def people(egs_factory):
    return {
        "admin": UserFactory(user_type=UserType.ADMIN, admin_approved=True),
        "dept_admin": UserFactory(user_type=UserType.DEPT_ADMIN, admin_approved=True),
        "oic": UserFactory(user_type=UserType.MANAGER, admin_approved=True),
        "student": egs_factory.student(),
    }


def _set_alert_emails(value: str, *, enabled: bool = True) -> None:
    cfg = SupportNotificationSettings.get_singleton()
    cfg.ticket_alert_emails = value
    cfg.ticket_alert_enabled = enabled
    cfg.save()


def _alerts():
    return [m for m in mail.outbox if m.subject.startswith("New support ticket #")]


def _raise_ticket(client, django_capture_on_commit_callbacks, **extra):
    payload = {"ticket_type": "booking", "subject": "Slot not visible", "description": "Cannot see Monday slots.",
               "priority": "high", **extra}
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post("/api/tickets/", payload, format="json")
    assert response.status_code == 201, response.content
    return response.json()["ticket_id"]


def _alert_logs():
    return CommunicationLog.objects.filter(metadata__kind=ticket_alerts.ALERT_LOG_KIND).order_by("pk")


@pytest.mark.django_db
def test_no_recipient_is_hard_coded(people, inline_alerts, django_capture_on_commit_callbacks):
    cfg = SupportNotificationSettings.get_singleton()
    assert cfg.ticket_alert_enabled is True
    assert cfg.ticket_alert_emails == ""
    assert ticket_alerts.configured_alert_emails() == []
    ticket_id = _raise_ticket(_client(people["student"]), django_capture_on_commit_callbacks)
    assert Ticket.objects.filter(pk=ticket_id).exists()
    assert _alerts() == []


@pytest.mark.django_db
def test_migration_seeds_row_without_recipients():
    import importlib

    from django.apps import apps

    migration = importlib.import_module("iic_booking.support.migrations.0009_support_notification_settings")
    SupportNotificationSettings.objects.all().delete()
    migration.seed_singleton(apps, None)
    row = SupportNotificationSettings.objects.get(pk=1)
    assert (row.ticket_alert_enabled, row.ticket_alert_emails) == (True, "")


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["student", "dept_admin", "oic"])
def test_notification_settings_are_main_admin_only(people, role):
    _set_alert_emails("desk@example.org")
    client = _client(people[role])
    assert client.get(SETTINGS_URL).status_code == 403
    put = client.put(SETTINGS_URL, {"ticket_alert_emails": "x@example.org"}, format="json")
    assert put.status_code == 403
    assert SupportNotificationSettings.get_singleton().ticket_alert_emails == "desk@example.org"
    assert _client().get(SETTINGS_URL).status_code in (401, 403)


@pytest.mark.django_db
def test_main_admin_reads_and_updates_recipients(people):
    _set_alert_emails("desk@example.org")
    client = _client(people["admin"])
    got = client.get(SETTINGS_URL)
    assert got.status_code == 200
    assert got.json()["ticket_alert_emails"] == ["desk@example.org"]
    assert got.json()["default_ticket_alert_emails"] == []

    bad = client.put(SETTINGS_URL, {"ticket_alert_emails": "ok@example.org, not-an-email"}, format="json")
    assert bad.status_code == 400
    assert bad.json()["invalid"] == ["not-an-email"]

    empty = client.put(SETTINGS_URL, {"ticket_alert_enabled": True, "ticket_alert_emails": []}, format="json")
    assert empty.status_code == 400

    ok = client.put(
        SETTINGS_URL,
        {"ticket_alert_enabled": True, "ticket_alert_emails": ["a@example.org", "A@example.org", "b@example.org"]},
        format="json",
    )
    assert ok.status_code == 200, ok.content
    assert ok.json()["ticket_alert_emails"] == ["a@example.org", "b@example.org"]
    assert SupportNotificationSettings.get_singleton().updated_by_id == people["admin"].pk

    off = client.put(SETTINGS_URL, {"ticket_alert_enabled": False, "ticket_alert_emails": ""}, format="json")
    assert off.status_code == 200
    assert off.json()["ticket_alert_enabled"] is False


@pytest.mark.django_db
def test_new_ticket_is_emailed_to_configured_recipients(people, egs_factory, inline_alerts,
                                                        django_capture_on_commit_callbacks):
    _set_alert_emails("desk@example.org; lead@example.org")
    equipment = egs_factory.equipment()
    EquipmentManager.objects.create(equipment=equipment, manager=people["oic"])
    student = people["student"]

    ticket_id = _raise_ticket(_client(student), django_capture_on_commit_callbacks, related_equipment=equipment.pk)

    alerts = _alerts()
    assert [a.to for a in alerts] == [["desk@example.org"], ["lead@example.org"]], "one separate copy per recipient"
    for alert in alerts:
        assert alert.cc == [] and alert.bcc == []
        assert f"#{ticket_id}" in alert.subject and "[High]" in alert.subject
        body = alert.body
        for expected in (student.email, "Booking Issues", "Cannot see Monday slots.", equipment.code,
                         f"https://portal.test/admin-settings/support?ticket={ticket_id}"):
            assert expected in body
        assert "Open ticket in portal" in alert.alternatives[0][0]
    others = [m for m in mail.outbox if m not in alerts]
    assert any(people["oic"].email in m.to for m in others), "OIC assignment email still sent"
    for m in others:
        assert not {"desk@example.org", "lead@example.org"} & {a.lower() for a in m.to + m.cc + m.bcc}

    logs = list(_alert_logs())
    assert [(l.recipient_email, l.status) for l in logs] == [
        ("desk@example.org", CommunicationLog.CommunicationStatus.SENT),
        ("lead@example.org", CommunicationLog.CommunicationStatus.SENT),
    ]
    assert all(l.metadata["ticket_id"] == ticket_id and l.sent_at for l in logs)


@pytest.mark.django_db
def test_invalid_stored_entries_are_skipped(people, inline_alerts, django_capture_on_commit_callbacks):
    SupportNotificationSettings.get_singleton()
    SupportNotificationSettings.objects.filter(pk=1).update(
        ticket_alert_emails="not-an-email, desk@example.org; bad@", ticket_alert_enabled=True
    )
    assert ticket_alerts.configured_alert_emails() == ["desk@example.org"]
    _raise_ticket(_client(people["student"]), django_capture_on_commit_callbacks)
    assert [a.to for a in _alerts()] == [["desk@example.org"]]


@pytest.mark.django_db
def test_model_clean_rejects_invalid_addresses_and_normalises():
    from django.core.exceptions import ValidationError

    cfg = SupportNotificationSettings.get_singleton()
    cfg.ticket_alert_emails = "desk@example.org, nope"
    with pytest.raises(ValidationError):
        cfg.full_clean()
    cfg.ticket_alert_emails = "desk@example.org;\nDESK@example.org  lead@example.org"
    cfg.full_clean()
    assert cfg.ticket_alert_emails == "desk@example.org, lead@example.org"


@pytest.mark.django_db
def test_chat_agent_ticket_is_forwarded(inline_alerts, settings, django_capture_on_commit_callbacks, db):
    settings.OPENAI_API_KEY = ""
    _set_alert_emails("desk@example.org")
    with django_capture_on_commit_callbacks(execute=True):
        res = _client().post(
            "/api/chat-agent/",
            {"message": "Please let me talk to a human about my sample", "public_email": "guest@example.org"},
            format="json",
        )
    assert res.status_code == 200
    ticket_id = res.json()["ticket_id"]
    assert ticket_id
    (alert,) = _alerts()
    assert alert.to == ["desk@example.org"] and f"#{ticket_id}" in alert.subject


@pytest.mark.django_db
def test_one_failed_copy_does_not_stop_the_others(people, inline_alerts, monkeypatch,
                                                  django_capture_on_commit_callbacks):
    _set_alert_emails("down@example.org, desk@example.org")
    real_send = ticket_alerts.send_mail

    def flaky(*args, **kwargs):
        if kwargs["recipient_list"] == ["down@example.org"]:
            raise ConnectionRefusedError("SMTP down")
        return real_send(*args, **kwargs)

    monkeypatch.setattr(ticket_alerts, "send_mail", flaky)
    _raise_ticket(_client(people["student"]), django_capture_on_commit_callbacks)
    assert [a.to for a in _alerts()] == [["desk@example.org"]]
    statuses = {l.recipient_email: l.status for l in _alert_logs()}
    assert statuses == {
        "down@example.org": CommunicationLog.CommunicationStatus.FAILED,
        "desk@example.org": CommunicationLog.CommunicationStatus.SENT,
    }


@pytest.mark.django_db
def test_assignee_on_alert_list_is_not_mailed_twice(people, egs_factory, inline_alerts,
                                                    django_capture_on_commit_callbacks):
    equipment = egs_factory.equipment()
    EquipmentManager.objects.create(equipment=equipment, manager=people["oic"])
    _set_alert_emails(f"{people['oic'].email}, desk@example.org")

    _raise_ticket(_client(people["student"]), django_capture_on_commit_callbacks, related_equipment=equipment.pk)

    assert [a.to for a in _alerts()] == [["desk@example.org"]]


@pytest.mark.django_db
def test_public_ticket_alert_marks_requester_as_public(inline_alerts, django_capture_on_commit_callbacks, db):
    _set_alert_emails("desk@example.org")
    _raise_ticket(_client(), django_capture_on_commit_callbacks, public_name="Visitor",
                  public_email="visitor@example.org")
    (alert,) = _alerts()
    assert "Public (not signed in)" in alert.body and "visitor@example.org" in alert.body


@pytest.mark.django_db
def test_test_account_ticket_goes_to_test_inbox_only(people, inline_alerts, django_capture_on_commit_callbacks):
    _set_alert_emails("desk@example.org")
    redirect = TestAccountEmailSettings.get_singleton()
    redirect.recipient_emails = "qa-inbox@example.org"
    redirect.save()
    student = people["student"]
    student.is_test_account = True
    student.save(update_fields=["is_test_account"])

    _raise_ticket(_client(student), django_capture_on_commit_callbacks)

    assert [a.to for a in _alerts()] == [["qa-inbox@example.org"]]


@pytest.mark.django_db
def test_disabled_alerts_send_nothing(people, inline_alerts, django_capture_on_commit_callbacks):
    _set_alert_emails("desk@example.org", enabled=False)
    _raise_ticket(_client(people["student"]), django_capture_on_commit_callbacks)
    assert _alerts() == []
    assert not _alert_logs().exists()


@pytest.mark.django_db
def test_ticket_is_created_when_alert_email_fails(people, inline_alerts, monkeypatch,
                                                  django_capture_on_commit_callbacks):
    _set_alert_emails("desk@example.org")

    def boom(*args, **kwargs):
        raise ConnectionRefusedError("SMTP down")

    monkeypatch.setattr(ticket_alerts, "send_mail", boom)
    ticket_id = _raise_ticket(_client(people["student"]), django_capture_on_commit_callbacks)
    assert Ticket.objects.filter(pk=ticket_id).exists()
    (log,) = _alert_logs()
    assert log.status == CommunicationLog.CommunicationStatus.FAILED and "SMTP down" in log.error_message


@pytest.mark.django_db
def test_ticket_is_created_when_alert_scheduling_fails(people, monkeypatch, django_capture_on_commit_callbacks):
    def boom(ticket):
        raise RuntimeError("queue down")

    monkeypatch.setattr(ticket_alerts, "schedule_new_ticket_alert", boom)
    ticket_id = _raise_ticket(_client(people["student"]), django_capture_on_commit_callbacks)
    assert Ticket.objects.filter(pk=ticket_id).exists()


def _feedback(user, overall, text=""):
    return PortalFeedback.objects.create(
        user=user, overall_rating=overall, ease_of_booking=3, website_usability=4,
        equipment_booking_experience=5, suggestions=text,
    )


@pytest.mark.django_db
@pytest.mark.parametrize("role", ["student", "dept_admin", "oic"])
def test_feedback_list_is_main_admin_only(people, role):
    _feedback(people["student"], 4)
    assert _client(people[role]).get(FEEDBACK_URL).status_code == 403
    assert _client(people[role]).get(FEEDBACK_URL, {"export": "csv"}).status_code == 403


@pytest.mark.django_db
def test_main_admin_lists_sorts_filters_and_exports_feedback(people):
    low = _feedback(people["student"], 2, "=HYPERLINK(bad)")
    high = _feedback(people["oic"], 5, "Great")
    client = _client(people["admin"])

    res = client.get(FEEDBACK_URL, {"ordering": "-overall_rating"})
    assert res.status_code == 200
    data = res.json()
    assert [r["feedback_id"] for r in data["feedback"]] == [high.feedback_id, low.feedback_id]
    assert data["stats"]["total"] == 2
    assert data["stats"]["avg_overall"] == 3.5
    assert data["stats"]["rating_distribution"]["5"] == 1

    only_two = client.get(FEEDBACK_URL, {"rating": 2}).json()
    assert [r["feedback_id"] for r in only_two["feedback"]] == [low.feedback_id]

    csv_res = client.get(FEEDBACK_URL, {"export": "csv", "ordering": "overall_rating"})
    assert csv_res.status_code == 200
    assert csv_res["Content-Type"].startswith("text/csv")
    text = csv_res.content.decode("utf-8-sig")
    lines = text.strip().splitlines()
    assert lines[0].startswith("Feedback ID,Name,Email")
    assert people["student"].email in lines[1]
    assert "'=HYPERLINK(bad)" in lines[1]
