"""notify_template_health: one email per user whose templates would fail or likely fail when booking opens."""

from __future__ import annotations

from io import StringIO

import pytest
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError

from iic_booking.communication.models import CommunicationLog
from iic_booking.equipment.management.commands.notify_template_health import NOTICE_KEY, mask_email
from iic_booking.equipment.models import BookingInputTemplate

from .test_booking_template_health import _equipment, _saved
from .test_booking_template_preferred_slot import _student_with_wallet, no_portal_lock  # noqa: F401

OPENS = ["--opens-at", "2026-10-07 21:00"]


@pytest.fixture(autouse=True)
def _setup(settings):
    settings.FRONTEND_URL = "https://portal.example"
    cache.clear()
    yield
    cache.clear()


def _active(user):
    type(user).objects.filter(pk=user.pk).update(is_active=True)
    return user


def _run(*args):
    out = StringIO()
    call_command("notify_template_health", *OPENS, *args, stdout=out)
    return out.getvalue()


@pytest.fixture
def owners(egs_factory, no_portal_lock):
    eq = _equipment(egs_factory)
    over, _sub = _student_with_wallet(egs_factory)
    low, _sub = _student_with_wallet(egs_factory, balance="1.00")
    clean, _sub = _student_with_wallet(egs_factory)
    for user in (over, low, clean):
        _active(user)
    _saved(over, eq, {"A": "11"}, name="XRD over")
    _saved(over, eq, {"A": "0"}, name="XRD zero")
    _saved(low, eq, {"A": "2"}, name="Weekly run")
    _saved(clean, eq, {"A": "2"}, name="Fine")
    return eq, over, low, clean


@pytest.mark.django_db
def test_dry_run_lists_masked_recipients_and_sends_nothing(owners):
    _eq, over, low, clean = owners

    out = _run()

    assert "Recipients: 2" in out
    assert mask_email(over.email) in out and mask_email(low.email) in out
    assert over.email not in out and low.email not in out and mask_email(clean.email) not in out
    assert "numeric_max: 1" in out and "numeric_min: 1" in out and "wallet_low: 1" in out
    assert "DRY RUN" in out
    assert mail.outbox == []
    assert not CommunicationLog.objects.exists()


@pytest.mark.django_db
def test_send_groups_templates_per_user_and_does_not_repeat(owners):
    eq, over, low, _clean = owners

    out = _run("--send")

    assert "SENT: 2 email(s); failed: 0" in out
    assert over.email not in out
    by_to = {m.to[0]: m for m in mail.outbox}
    assert len(mail.outbox) == 2 and set(by_to) == {over.email, low.email}
    grouped = by_to[over.email]
    assert "XRD over" in grouped.body and "XRD zero" in grouped.body
    assert "Wednesday, 7 October at 9:00 pm" in grouped.body and "week of 12 October" in grouped.body
    assert f"equipment_id={eq.pk}&mode=template" in grouped.body and "fix=A" in grouped.body
    html = grouped.alternatives[0][0]
    assert "XRD over" in html and "Open the template" in html
    wallet = by_to[low.email].body
    assert "Your wallet balance may not cover the estimated charge of ₹10. Please recharge, or ask your supervisor." in wallet
    assert "https://portal.example/wallet" in wallet
    log = CommunicationLog.objects.get(recipient=over)
    assert log.status == CommunicationLog.CommunicationStatus.SENT
    over_ids = list(BookingInputTemplate.objects.filter(user=over).order_by("pk").values_list("pk", flat=True))
    assert sorted(log.metadata[NOTICE_KEY]["keys"]) == [f"{over_ids[0]}:numeric_max", f"{over_ids[1]}:numeric_min"]

    again = _run("--send")
    assert "Recipients: 0" in again and "Already emailed (skipped): 3 issue(s)" in again
    assert len(mail.outbox) == 2


@pytest.mark.django_db
def test_failed_send_is_retried_and_guard_blocks_large_runs(owners, monkeypatch):
    from iic_booking.equipment.management.commands import notify_template_health as cmd

    with pytest.raises(CommandError, match="more than --max-recipients=1"):
        _run("--send", "--max-recipients", "1")
    assert mail.outbox == [] and not CommunicationLog.objects.exists()

    def boom(**kwargs):
        raise OSError("smtp down for someone@example.com")

    real_send = cmd.send_mail
    monkeypatch.setattr(cmd, "send_mail", boom)
    out = StringIO()
    with pytest.raises(CommandError, match="2 email"):
        call_command("notify_template_health", *OPENS, "--send", stdout=out)
    assert "someone@example.com" not in out.getvalue() and "FAILED" in out.getvalue()
    monkeypatch.setattr(cmd, "send_mail", real_send)

    assert "Recipients: 2" in _run()


def test_mask_email():
    assert mask_email("ravi.saini@iitr.ac.in") == "r***@iitr.ac.in"
    assert mask_email("") == "***"
