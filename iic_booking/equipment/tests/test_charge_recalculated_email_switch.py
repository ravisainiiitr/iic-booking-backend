"""The "Booking Charges Updated" email is switched off by deactivating its template: the charge-recalculated
event and the in-app notice still go out, money-movement emails are untouched."""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from django.apps import apps as django_apps

from iic_booking.communication.default_email_templates import get_default_email_templates
from iic_booking.communication.models import CommunicationLog, CommunicationTemplate
from iic_booking.equipment import booking_events
from iic_booking.equipment.models import BookingEvent, BookingEventType

from .test_print_actuals_charge import _set_actuals, _setup

CODE = "booking_charge_recalculated_email"
mig = importlib.import_module("iic_booking.communication.migrations.0056_switch_off_charge_recalculated_email")


@pytest.fixture(autouse=True)
def media_tmp(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path)
    return tmp_path


@pytest.fixture
def sent(monkeypatch):
    calls = SimpleNamespace(emails=[], pushes=[])

    def _email(recipient, template=None, template_context=None, metadata=None, created_by=None, cc_emails=None):
        calls.emails.append((recipient, template))
        return SimpleNamespace(status=CommunicationLog.CommunicationStatus.SENT)

    def _push(recipient, title=None, message=None, template=None, template_context=None, metadata=None,
              created_by=None):
        calls.pushes.append((recipient, template, message))

    monkeypatch.setattr(booking_events.CommunicationService, "send_email", staticmethod(_email))
    monkeypatch.setattr(booking_events.CommunicationService, "send_push_notification", staticmethod(_push))
    return calls


def _template(active: bool) -> CommunicationTemplate:
    spec = next(t for t in get_default_email_templates() if t["code"] == CODE)
    return CommunicationTemplate.objects.create(
        code=CODE,
        name=spec["name"],
        communication_type="email",
        subject=spec["subject"],
        body_text=spec["body_text"],
        body_html=spec["body_html"],
        is_active=active,
    )


def _notify_charge_event(booking):
    event = BookingEvent.objects.select_related("booking", "booking__user", "booking__equipment").get(
        booking=booking, event_type=BookingEventType.CHARGE_RECALCULATED
    )
    booking_events.send_booking_event_notification(event)
    return event


@pytest.mark.django_db
def test_print_actuals_record_the_event_but_send_no_charge_email(egs_factory, sent):
    _template(active=False)
    _eq, owner, _wallet, oic, booking, _parts = _setup(egs_factory)

    resp = _set_actuals(egs_factory, oic, booking, actual_weight_grams=40, actual_time_minutes=60)
    assert resp.status_code == 200, resp.data
    event = _notify_charge_event(booking)

    assert event.metadata["extra_amount"] == "72.00"
    assert [t for _r, t in sent.emails if t == CODE] == []
    owner_push = [m for r, t, m in sent.pushes if r == owner and t == "booking_charge_recalculated_push"]
    assert len(owner_push) == 1 and "Extra ₹72.00 to pay" in owner_push[0]
    assert any(r == oic for r, t, _m in sent.pushes if t == "booking_charge_recalculated_push")


@pytest.mark.django_db
def test_refund_from_actuals_sends_no_charge_email_either(egs_factory, sent):
    _template(active=False)
    _eq, owner, _wallet, oic, booking, _parts = _setup(egs_factory)

    assert _set_actuals(egs_factory, oic, booking, actual_weight_grams=5, actual_time_minutes=10).status_code == 200
    event = _notify_charge_event(booking)

    assert event.metadata["refund_status"] == "awaiting_oic_confirmation"
    assert [t for _r, t in sent.emails if t == CODE] == []
    assert any(r == owner for r, _t, _m in sent.pushes)


@pytest.mark.django_db
def test_reactivating_the_template_sends_the_email_again(egs_factory, sent):
    _template(active=True)
    _eq, owner, _wallet, oic, booking, _parts = _setup(egs_factory)

    assert _set_actuals(egs_factory, oic, booking, actual_weight_grams=40, actual_time_minutes=60).status_code == 200
    _notify_charge_event(booking)

    assert (owner, CODE) in sent.emails
    assert (oic, CODE) in sent.emails


@pytest.mark.django_db
def test_only_the_charge_email_is_switchable():
    CommunicationTemplate.objects.create(
        code="booking_cancelled_email", name="Cancelled", communication_type="email",
        subject="s", body_text="b", is_active=False,
    )
    assert booking_events.event_email_switched_off("booking_cancelled_email") is False
    assert booking_events.event_email_switched_off(CODE) is False
    _template(active=False)
    assert booking_events.event_email_switched_off(CODE) is True


def test_catalog_keeps_the_charge_email_off_and_money_emails_on():
    specs = {t["code"]: t for t in get_default_email_templates()}
    assert specs[CODE]["is_active"] is False
    for code in ("wallet_debit_email", "wallet_credit_email", "booking_refunded_email"):
        assert specs[code]["is_active"] is True


@pytest.mark.django_db
def test_migration_switches_the_template_off_and_back_on():
    tpl = _template(active=True)
    mig.forwards(django_apps, None)
    tpl.refresh_from_db()
    assert tpl.is_active is False
    mig.backwards(django_apps, None)
    tpl.refresh_from_db()
    assert tpl.is_active is True
