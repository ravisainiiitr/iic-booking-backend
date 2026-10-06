"""`manage.py support_ticket_alerts`: masked status, env-only writes, match check and a no-send dry run."""

from __future__ import annotations

import json
from io import StringIO

import pytest
from django.core import mail
from django.core.management import CommandError, call_command

from iic_booking.support.models import SupportNotificationSettings, Ticket
from iic_booking.users.tests.factories import UserFactory

ENV = "SUPPORT_TICKET_ALERT_EMAILS"


def _run(*args) -> dict:
    out = StringIO()
    call_command("support_ticket_alerts", *args, "--json", stdout=out)
    return json.loads(out.getvalue())


@pytest.mark.django_db
def test_status_is_read_only_and_masked():
    cfg = SupportNotificationSettings.get_singleton()
    cfg.ticket_alert_emails = "desk@example.org"
    cfg.save()
    report = _run()
    assert report["changes"] == []
    assert report["state"]["recipients_masked"] == ["d***@example.org"]
    assert "desk@example.org" not in json.dumps(report)


@pytest.mark.django_db
def test_apply_from_env_needs_confirm(monkeypatch):
    monkeypatch.setenv(ENV, "desk@example.org")
    with pytest.raises(CommandError):
        _run("--set-from-env", ENV)
    assert SupportNotificationSettings.get_singleton().ticket_alert_emails == ""


@pytest.mark.django_db
def test_apply_from_env_stores_and_matches(monkeypatch):
    monkeypatch.setenv(ENV, "desk@example.org; Lead@example.org, desk@example.org")
    report = _run("--set-from-env", ENV, "--enabled", "on", "--confirm", "ALERTS", "--expect-from-env", ENV)
    cfg = SupportNotificationSettings.get_singleton()
    assert (cfg.ticket_alert_enabled, cfg.ticket_alert_emails) == (True, "desk@example.org, Lead@example.org")
    assert report["matches_expected"] is True
    assert report["changes"] == ["recipients -> 2 address(es)"]
    assert "desk@example.org" not in json.dumps(report)

    again = _run("--set-from-env", ENV, "--enabled", "on", "--confirm", "ALERTS")
    assert again["changes"] == []


@pytest.mark.django_db
@pytest.mark.parametrize("value", ["", "desk@example.org, not-an-email"])
def test_apply_rejects_empty_or_invalid_env(monkeypatch, value):
    cfg = SupportNotificationSettings.get_singleton()
    cfg.ticket_alert_emails = "keep@example.org"
    cfg.save()
    monkeypatch.setenv(ENV, value)
    with pytest.raises(CommandError):
        _run("--set-from-env", ENV, "--confirm", "ALERTS")
    assert SupportNotificationSettings.get_singleton().ticket_alert_emails == "keep@example.org"


@pytest.mark.django_db
def test_cannot_switch_on_without_recipients():
    cfg = SupportNotificationSettings.get_singleton()
    cfg.ticket_alert_enabled = False
    cfg.save()
    with pytest.raises(CommandError):
        _run("--enabled", "on", "--confirm", "ALERTS")


@pytest.mark.django_db
def test_mismatch_is_reported(monkeypatch):
    cfg = SupportNotificationSettings.get_singleton()
    cfg.ticket_alert_emails = "other@example.org"
    cfg.save()
    monkeypatch.setenv(ENV, "desk@example.org")
    assert _run("--expect-from-env", ENV)["matches_expected"] is False


@pytest.mark.django_db
def test_dry_run_sends_nothing():
    cfg = SupportNotificationSettings.get_singleton()
    cfg.ticket_alert_emails = "desk@example.org, lead@example.org"
    cfg.save()
    ticket = Ticket.objects.create(user=UserFactory(), subject="Help", description="d")

    report = _run("--dry-run-ticket", "latest")

    assert report["dry_run"]["ticket_id"] == ticket.ticket_id
    assert report["dry_run"]["would_send"] == 2
    assert report["dry_run"]["recipients_masked"] == ["d***@example.org", "l***@example.org"]
    assert report["dry_run"]["subject_starts_with"] == f"New support ticket #{ticket.ticket_id} [Medium]"
    assert mail.outbox == []
