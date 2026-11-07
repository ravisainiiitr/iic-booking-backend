"""WhatsApp notifications: opt-in, gating (off / not opted in / pilot numbers), delivery through the Cloud API (fake
HTTP), the signed webhook (statuses, STOP) and the booking-event hook. No network."""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import patch

import pytest
from rest_framework.test import APIClient

from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory
from iic_booking.whatsapp import service
from iic_booking.whatsapp.client import WhatsAppError, send_template
from iic_booking.whatsapp.models import (
    WhatsAppConsentEvent,
    WhatsAppInbound,
    WhatsAppMessage,
    WhatsAppOptIn,
    WhatsAppTemplate,
    WhatsAppWebhookEvent,
)
from iic_booking.whatsapp.template_catalogue import CATALOGUE

pytestmark = pytest.mark.django_db

SECRET = "unit-test-app-secret-not-real"


# booking_confirmed takes five values, in the order the template at Meta declares them.
CONFIRMED_VALUES = ["Asha Verma", "B1", "XRD", "10 Nov 2026", "10:00 AM"]
WABA = "3000000000000333"


@pytest.fixture
def on(settings, db):
    settings.WHATSAPP_WABA_ID = WABA
    for template in CATALOGUE:  # sending needs an approved template: see test_templates.py for the gate itself
        WhatsAppTemplate.objects.create(waba_id=WABA, name=template.name, language=template.language, status="APPROVED")
    settings.WHATSAPP_ENABLED = True
    settings.WHATSAPP_PHONE_NUMBER_ID = "1234567890"
    settings.WHATSAPP_ACCESS_TOKEN = "unit-test-token-not-real"
    settings.WHATSAPP_APP_SECRET = SECRET
    settings.WHATSAPP_VERIFY_TOKEN = "unit-test-verify"
    settings.WHATSAPP_PILOT_NUMBERS = []
    settings.WHATSAPP_ALLOW_ALL = True
    return settings


class FakeResponse:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeSession:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, json=None, timeout=None, headers=None):
        self.calls.append({"url": url, "json": json, "headers": headers})
        return self.responses.pop(0)


def _api(user=None):
    c = APIClient()
    if user is not None:
        c.force_authenticate(user)
    return c


def _opted_in(phone="+919876543210"):
    user = UserFactory(user_type=UserType.STUDENT, phone_number="9876543210")
    service.set_opt_in(user, opted_in=True, phone=phone)
    return user


@pytest.mark.parametrize("raw, expected", [("9876543210", "+919876543210"), ("+91 98765-43210", "+919876543210"),
                                           ("09876543210", "+919876543210"), ("+44 7700 900123", "+447700900123")])
def test_numbers_are_normalised_to_e164(raw, expected):
    assert service.normalize_phone(raw) == expected


def test_too_short_numbers_are_refused():
    with pytest.raises(service.OptInError):
        service.normalize_phone("12345")


def test_users_opt_in_and_out_from_their_profile(on):
    user = UserFactory(user_type=UserType.STUDENT, phone_number="9876543210")
    me = _api(user).get("/api/whatsapp/me/").json()
    assert me["available"] is True and me["opted_in"] is False and me["phone"] == "+919876543210"
    resp = _api(user).put("/api/whatsapp/me/", {"opted_in": True}, format="json")
    assert resp.status_code == 200 and resp.json()["opted_in"] is True
    assert _api(user).put("/api/whatsapp/me/", {"opted_in": True, "phone": "12"}, format="json").status_code == 400
    assert _api(user).put("/api/whatsapp/me/", {"opted_in": False}, format="json").json()["opted_in"] is False


def test_nothing_is_queued_while_whatsapp_is_off(settings):
    settings.WHATSAPP_ENABLED = False
    user = _opted_in()
    assert service.notify(user, "booking_confirmed", CONFIRMED_VALUES, event_key="k1") is None


def test_nothing_is_queued_without_opt_in(on):
    user = UserFactory(user_type=UserType.STUDENT)
    assert service.notify(user, "booking_confirmed", CONFIRMED_VALUES, event_key="k1") is None


def test_pilot_numbers_limit_who_receives(on):
    on.WHATSAPP_PILOT_NUMBERS = ["+911111111111"]
    user = _opted_in()
    assert service.notify(user, "booking_confirmed", CONFIRMED_VALUES, event_key="k1") is None


def test_a_message_is_sent_once_per_event(on):
    user = _opted_in()
    with patch("iic_booking.whatsapp.tasks.deliver_whatsapp.delay") as delay:
        first = service.notify(user, "booking_confirmed", CONFIRMED_VALUES, event_key="k1")
        again = service.notify(user, "booking_confirmed", CONFIRMED_VALUES, event_key="k1")
    assert first is not None and again is None
    assert first.template == "iic_booking_confirmed" and first.to_e164 == "+919876543210"


def test_delivery_sends_the_template_and_records_the_message_id(on):
    user = _opted_in()
    with patch("iic_booking.whatsapp.tasks.deliver_whatsapp.delay"):
        msg = service.notify(user, "booking_confirmed", CONFIRMED_VALUES, event_key="k1")
    session = FakeSession(FakeResponse(200, {"messages": [{"id": "wamid.ABC"}]}))
    assert service.deliver(msg.pk, session=session) == "sent"
    msg.refresh_from_db()
    assert msg.wamid == "wamid.ABC"
    sent = session.calls[0]
    assert sent["url"].endswith("/1234567890/messages") and sent["headers"]["Authorization"].startswith("Bearer ")
    assert sent["json"]["to"] == "919876543210" and sent["json"]["template"]["name"] == "iic_booking_confirmed"
    assert [p["text"] for p in sent["json"]["template"]["components"][0]["parameters"]] == CONFIRMED_VALUES
    assert sent["json"]["template"]["language"]["code"] == "en"


def test_a_refused_message_is_marked_failed_and_a_server_error_is_retried(on):
    user = _opted_in()
    with patch("iic_booking.whatsapp.tasks.deliver_whatsapp.delay"):
        msg = service.notify(user, "booking_cancelled", CONFIRMED_VALUES[:4], event_key="k1")
    refused = FakeSession(FakeResponse(400, {"error": {"code": 132001, "message": "Template name does not exist"}}))
    assert service.deliver(msg.pk, session=refused) == "failed"
    msg.refresh_from_db()
    assert "132001" in msg.error
    with pytest.raises(WhatsAppError) as exc:
        send_template("+919876543210", "t", [], session=FakeSession(FakeResponse(503, {})))
    assert exc.value.retryable


def test_a_user_who_opts_out_before_delivery_gets_nothing(on):
    user = _opted_in()
    with patch("iic_booking.whatsapp.tasks.deliver_whatsapp.delay"):
        msg = service.notify(user, "booking_confirmed", CONFIRMED_VALUES, event_key="k1")
    service.set_opt_in(user, opted_in=False)
    assert service.deliver(msg.pk, session=FakeSession()) == "skipped"


def _signed(payload):
    raw = json.dumps(payload).encode()
    sig = "sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, sig


def test_webhook_subscription_check(on):
    ok = _api().get("/api/whatsapp/webhook/", {"hub.mode": "subscribe", "hub.verify_token": "unit-test-verify",
                                               "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.content == b"42"
    assert _api().get("/api/whatsapp/webhook/", {"hub.mode": "subscribe", "hub.verify_token": "wrong"}).status_code == 403


def test_webhook_needs_a_valid_signature_and_updates_statuses_and_stop(on):
    user = _opted_in()
    msg = WhatsAppMessage.objects.create(user=user, to_e164="+919876543210", event="booking_confirmed",
                                         template="iic_booking_confirmed", event_key="k1", status="sent", wamid="wamid.X")
    payload = {"entry": [{"changes": [{"value": {
        "statuses": [{"id": "wamid.X", "status": "delivered"}],
        "messages": [{"from": "919876543210", "type": "text", "text": {"body": "STOP"}}],
    }}]}]}
    raw, sig = _signed(payload)
    bad = _api().post("/api/whatsapp/webhook/", raw, content_type="application/json", HTTP_X_HUB_SIGNATURE_256="sha256=00")
    assert bad.status_code == 403
    resp = _api().post("/api/whatsapp/webhook/", raw, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=sig)
    assert resp.status_code == 200
    msg.refresh_from_db()
    assert msg.status == "delivered"
    assert WhatsAppOptIn.objects.get(user=user).opted_in is False


def _incoming(**message):
    return {"entry": [{"changes": [{"value": {"messages": [{"from": "919876543210", "id": "wamid.IN1", **message}]}}]}]}


@pytest.mark.parametrize("message", [
    {"type": "text", "text": {"body": " Stop "}},
    {"type": "button", "button": {"text": "STOP"}},
    {"type": "interactive", "interactive": {"type": "button_reply", "button_reply": {"id": "x", "title": "Stop"}}},
    {"type": "interactive", "interactive": {"type": "list_reply", "list_reply": {"id": "x", "title": "unsubscribe"}}},
])
def test_stop_is_honoured_however_it_is_sent(on, message):
    user = _opted_in()
    result = service.handle_webhook(_incoming(**message))
    assert result["replies"] == 1
    assert WhatsAppOptIn.objects.get(user=user).opted_in is False
    assert WhatsAppInbound.objects.get().action == "opt_out"


def test_incoming_message_is_kept_once_and_does_not_change_consent(on):
    user = _opted_in()
    payload = _incoming(type="text", text={"body": "When is my slot?"})
    assert service.handle_webhook(payload)["inbound"] == 1
    assert service.handle_webhook(payload)["inbound"] == 0  # Meta re-sends
    row = WhatsAppInbound.objects.get()
    assert (row.user, row.from_e164, row.kind, row.text, row.action) == (user, "+919876543210", "text", "When is my slot?", "")
    assert WhatsAppOptIn.objects.get(user=user).opted_in is True


def test_a_message_without_text_is_recorded_with_its_kind(on):
    _opted_in()
    service.handle_webhook(_incoming(type="image", image={"id": "media1"}))
    row = WhatsAppInbound.objects.get()
    assert (row.kind, row.text) == ("image", "")


def test_messages_from_strangers_are_not_stored(on):
    assert service.handle_webhook(_incoming(type="text", text={"body": "hello"}))["inbound"] == 0
    assert not WhatsAppInbound.objects.exists()


def test_start_opts_back_in_only_someone_who_had_opted_in(on):
    user = _opted_in()
    service.handle_webhook(_incoming(type="text", text={"body": "STOP"}))
    assert service.handle_webhook(_incoming(id="wamid.IN2", type="text", text={"body": "START"}))["replies"] == 1
    assert WhatsAppOptIn.objects.get(user=user).opted_in is True
    assert service.handle_webhook(
        {"entry": [{"changes": [{"value": {"messages": [{"from": "919800000000", "id": "w3", "type": "text", "text": {"body": "START"}}]}}]}]}
    )["replies"] == 0


def test_stop_survives_when_the_message_cannot_be_stored(on):
    from django.db import DatabaseError

    user = _opted_in()
    with patch("iic_booking.whatsapp.service.WhatsAppInbound.objects.get_or_create", side_effect=DatabaseError("no table")):
        result = service.handle_webhook(_incoming(type="text", text={"body": "STOP"}))
    assert (result["replies"], result["inbound"]) == (1, 0)
    assert WhatsAppOptIn.objects.get(user=user).opted_in is False


@pytest.mark.parametrize("payload", [[], "x", {"entry": "x"}, {"entry": ["x"]}, {"entry": [{"changes": [{"value": "x"}]}]},
                                     {"entry": [{"changes": [{"value": {"messages": ["x"], "statuses": [1]}}]}]}])
def test_odd_payloads_do_not_break_the_webhook(on, payload):
    raw, sig = _signed(payload)
    resp = _api().post("/api/whatsapp/webhook/", raw, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=sig)
    assert resp.status_code == 200


def test_webhook_works_while_sending_is_off(on):
    on.WHATSAPP_ENABLED = False
    ok = _api().get("/api/whatsapp/webhook/", {"hub.mode": "subscribe", "hub.verify_token": "unit-test-verify", "hub.challenge": "42"})
    assert ok.status_code == 200 and ok.content == b"42"
    user = _opted_in()
    raw, sig = _signed(_incoming(type="text", text={"body": "STOP"}))
    assert _api().post("/api/whatsapp/webhook/", raw, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=sig).status_code == 200
    assert WhatsAppOptIn.objects.get(user=user).opted_in is False
    assert service.notify(_opted_in("+919811111111"), "booking_confirmed", CONFIRMED_VALUES, event_key="k") is None


def test_webhook_refuses_unsigned_posts_and_a_missing_verify_token(on):
    raw, _sig = _signed({"entry": []})
    assert _api().post("/api/whatsapp/webhook/", raw, content_type="application/json").status_code == 403
    on.WHATSAPP_VERIFY_TOKEN = ""
    assert _api().get("/api/whatsapp/webhook/", {"hub.mode": "subscribe", "hub.verify_token": "", "hub.challenge": "1"}).status_code == 403


def test_webhook_logs_no_credentials_or_personal_details(on, caplog):
    import logging

    _opted_in()
    raw, sig = _signed(_incoming(type="text", text={"body": "my private words"}))
    with caplog.at_level(logging.DEBUG):
        _api().post("/api/whatsapp/webhook/", raw, content_type="application/json", HTTP_X_HUB_SIGNATURE_256=sig)
    for secret in (SECRET, "unit-test-token-not-real", "unit-test-verify", "my private words", "9876543210"):
        assert secret not in caplog.text


def test_messaging_is_off_and_closed_by_default(settings):
    from iic_booking.whatsapp import conf
    from config.settings import base

    assert base.WHATSAPP_ALLOW_ALL is False
    settings.WHATSAPP_ALLOW_ALL = False
    settings.WHATSAPP_PILOT_NUMBERS = []
    assert conf.may_send_to("+919876543210") is False


def test_an_empty_pilot_list_sends_to_nobody_without_the_explicit_switch(on):
    on.WHATSAPP_ALLOW_ALL = False
    assert service.notify(_opted_in(), "booking_confirmed", CONFIRMED_VALUES, event_key="k") is None


def test_a_pilot_list_wins_over_the_allow_all_switch(on):
    on.WHATSAPP_PILOT_NUMBERS = ["+919800000001"]
    assert service.notify(_opted_in(), "booking_confirmed", CONFIRMED_VALUES, event_key="k") is None


def test_a_queued_message_is_not_sent_once_the_number_leaves_the_pilot(on):
    on.WHATSAPP_PILOT_NUMBERS = ["+919876543210"]
    user = _opted_in()
    with patch("iic_booking.whatsapp.service._enqueue"):
        msg = service.notify(user, "booking_confirmed", CONFIRMED_VALUES, event_key="k")
    on.WHATSAPP_PILOT_NUMBERS = ["+919800000001"]
    session = FakeSession()
    assert service.deliver(msg.pk, session=session) == "skipped"
    assert session.calls == []


def test_consent_history_records_who_when_and_how(on):
    user = _opted_in()
    service.handle_webhook(_incoming(type="text", text={"body": "STOP"}))
    service.handle_webhook(_incoming(id="wamid.IN2", type="text", text={"body": "START"}))
    rows = list(WhatsAppConsentEvent.objects.filter(user=user).order_by("id").values_list("action", "source", "phone_e164"))
    assert rows == [("opt_in", "profile", "+919876543210"), ("opt_out", "reply_stop", "+919876543210"), ("opt_in", "reply_start", "+919876543210")]
    row = WhatsAppOptIn.objects.get(user=user)
    assert row.source == "reply_start" and row.opted_in_at is not None and row.opted_out_at is None


def test_stop_covers_every_account_on_the_number_and_anything_queued(on):
    first, second = _opted_in(), _opted_in()
    with patch("iic_booking.whatsapp.service._enqueue"):
        queued = service.notify(first, "booking_confirmed", CONFIRMED_VALUES, event_key="k")
    service.handle_webhook(_incoming(type="text", text={"body": "STOP"}))
    assert not WhatsAppOptIn.objects.filter(user__in=[first, second], opted_in=True).exists()
    queued.refresh_from_db()
    assert queued.status == "skipped"
    assert service.notify(second, "booking_confirmed", CONFIRMED_VALUES, event_key="k2") is None


def test_a_repeated_old_stop_does_not_undo_a_later_start(on):
    user = _opted_in()
    stop = _incoming(type="text", text={"body": "STOP"})
    service.handle_webhook(stop)
    service.handle_webhook(_incoming(id="wamid.IN2", type="text", text={"body": "START"}))
    result = service.handle_webhook(stop)  # Meta sends the old event again
    assert result["duplicates"] == 1
    assert WhatsAppOptIn.objects.get(user=user).opted_in is True
    assert WhatsAppConsentEvent.objects.filter(user=user, action="opt_out").count() == 1


def test_a_repeated_status_is_handled_once_and_each_new_status_counts(on):
    WhatsAppMessage.objects.create(to_e164="+919876543210", event="e", template="t", event_key="k3", status="sent", wamid="wamid.Z")
    delivered = {"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.Z", "status": "delivered"}]}}]}]}
    read = {"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.Z", "status": "read"}]}}]}]}
    assert service.handle_webhook(delivered)["statuses"] == 1
    assert service.handle_webhook(delivered)["duplicates"] == 1
    assert service.handle_webhook(read)["statuses"] == 1
    assert WhatsAppMessage.objects.get(wamid="wamid.Z").status == "read"
    assert WhatsAppWebhookEvent.objects.count() == 2


def test_a_failed_event_can_be_retried_by_meta(on):
    user = _opted_in()
    stop = _incoming(type="text", text={"body": "STOP"})
    with patch("iic_booking.whatsapp.service._apply_reply", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError):
            service.handle_webhook(stop)
    assert not WhatsAppWebhookEvent.objects.exists()  # not marked as handled
    service.handle_webhook(stop)
    assert WhatsAppOptIn.objects.get(user=user).opted_in is False


def test_webhook_records_hold_no_content_or_number(on):
    _opted_in()
    service.handle_webhook(_incoming(type="text", text={"body": "my private words"}))
    row = WhatsAppWebhookEvent.objects.get()
    assert "9876543210" not in f"{row.event_key}{row.kind}{row.detail}" and "private" not in f"{row.kind}{row.detail}"


def test_temporary_failures_are_retried_then_recorded(on):
    from celery.exceptions import Retry

    from iic_booking.whatsapp.tasks import deliver_whatsapp

    user = _opted_in()
    with patch("iic_booking.whatsapp.service._enqueue"):
        msg = service.notify(user, "booking_confirmed", CONFIRMED_VALUES, event_key="k")
    boom = WhatsAppError("Meta answered HTTP 503", retryable=True)
    with patch("iic_booking.whatsapp.client.send_template", side_effect=boom):
        with pytest.raises(Retry):
            deliver_whatsapp.apply(args=[msg.pk], throw=True).get()
    msg.refresh_from_db()
    assert msg.status == "failed" or msg.status == "queued"
    with patch("iic_booking.whatsapp.client.send_template", side_effect=boom):
        deliver_whatsapp.push_request(retries=deliver_whatsapp.max_retries)
        try:
            assert deliver_whatsapp.run(msg.pk) == "failed"
        finally:
            deliver_whatsapp.pop_request()
    msg.refresh_from_db()
    assert msg.status == "failed" and "503" in msg.error


def test_a_queue_that_is_down_does_not_reach_the_booking(on, django_capture_on_commit_callbacks):
    user = _opted_in()
    with patch("iic_booking.whatsapp.tasks.deliver_whatsapp.delay", side_effect=ConnectionError("broker down")):
        with django_capture_on_commit_callbacks(execute=True):
            assert service.notify(user, "booking_confirmed", CONFIRMED_VALUES, event_key="k") is not None


def test_a_whatsapp_database_error_does_not_break_the_booking_transaction(on):
    from types import SimpleNamespace

    from django.contrib.auth import get_user_model
    from django.db import DatabaseError, connection, transaction

    from iic_booking.equipment.models import BookingEventType
    from iic_booking.whatsapp.hooks import booking_event

    user = _opted_in()
    booking = SimpleNamespace(pk=1, virtual_booking_id="R1", equipment=SimpleNamespace(name="E"), user=user, status="BOOKED", daily_slots=None)

    def fails(*a, **k):
        with connection.cursor() as cur:
            cur.execute("SELECT * FROM no_such_table_whatsapp")
        raise DatabaseError("unreachable")

    with transaction.atomic():
        with patch("iic_booking.whatsapp.service.notify", fails), patch("iic_booking.whatsapp.hooks._first_slot", lambda b: None):
            booking_event(SimpleNamespace(booking=booking, event_type=BookingEventType.CREATED, event_id="e1", pk=1))
        assert get_user_model().objects.filter(pk=user.pk).exists()  # the outer transaction still works


def test_incoming_messages_are_for_the_main_admin_only(on):
    _opted_in()
    service.handle_webhook(_incoming(type="text", text={"body": "hello"}))
    assert _api().get("/api/whatsapp/inbound/").status_code in (401, 403)
    for user_type in (UserType.STUDENT, UserType.MANAGER, UserType.OPERATOR, UserType.FINANCE, UserType.DEPT_ADMIN):
        assert _api(UserFactory(user_type=user_type)).get("/api/whatsapp/inbound/").status_code == 403
    resp = _api(UserFactory(user_type=UserType.ADMIN, is_staff=True, is_superuser=True)).get("/api/whatsapp/inbound/")
    assert resp.status_code == 200 and resp.data["results"][0]["text"] == "hello"


VALID_META_TIME = "2023-11-14T22:13:20+00:00"  # 1700000000


@pytest.mark.parametrize("raw", ["1700000000", " 1700000000 ", 1700000000])
def test_meta_timestamp_is_stored_in_utc(on, raw):
    _opted_in()
    service.handle_webhook(_incoming(type="text", text={"body": "hello"}, timestamp=raw))
    row = WhatsAppInbound.objects.get()
    assert row.meta_timestamp.isoformat() == VALID_META_TIME
    assert row.meta_timestamp != row.received_at


def test_a_missing_meta_timestamp_does_not_lose_the_message(on):
    _opted_in()
    assert service.handle_webhook(_incoming(type="text", text={"body": "hello"}))["inbound"] == 1
    row = WhatsAppInbound.objects.get()
    assert row.text == "hello" and row.meta_timestamp is None and row.received_at is not None


@pytest.mark.parametrize("raw", [
    None, "", "   ", "soon", "-1", "0", 0, -5, "1700000000.5", 1700000000.5, "1e9", "٣٤٥", "99999999999999999999",
    "999999999999", True, [], {}, ["1700000000"],
])
def test_a_malformed_meta_timestamp_does_not_lose_the_message(on, raw):
    _opted_in()
    assert service.handle_webhook(_incoming(type="text", text={"body": "hello"}, timestamp=raw))["inbound"] == 1
    row = WhatsAppInbound.objects.get()
    assert row.text == "hello" and row.meta_timestamp is None and row.received_at is not None


def test_stop_still_works_with_a_malformed_meta_timestamp(on):
    user = _opted_in()
    assert service.handle_webhook(_incoming(type="text", text={"body": "STOP"}, timestamp="soon"))["replies"] == 1
    assert WhatsAppOptIn.objects.get(user=user).opted_in is False
    assert WhatsAppInbound.objects.get().action == "opt_out"


def test_the_main_admin_sees_both_times_separately(on):
    _opted_in()
    service.handle_webhook(_incoming(type="text", text={"body": "hello"}, timestamp="1700000000"))
    admin = UserFactory(user_type=UserType.ADMIN, is_staff=True, is_superuser=True)
    result = _api(admin).get("/api/whatsapp/inbound/").json()["results"][0]
    assert result["meta_timestamp"] == "2023-11-14T22:13:20Z"
    assert result["received_at"] and result["received_at"] != result["meta_timestamp"]


def test_older_rows_without_a_meta_timestamp_stay_readable_and_are_purged(on):
    from datetime import timedelta

    from django.utils import timezone

    user = _opted_in()
    old = WhatsAppInbound.objects.create(wamid="wamid.OLD", user=user, from_e164="+919876543210", text="older")
    assert old.meta_timestamp is None
    admin = UserFactory(user_type=UserType.ADMIN, is_staff=True, is_superuser=True)
    result = _api(admin).get("/api/whatsapp/inbound/").json()["results"][0]
    assert result["text"] == "older" and result["meta_timestamp"] is None
    WhatsAppInbound.objects.filter(pk=old.pk).update(received_at=timezone.now() - timedelta(days=400))
    assert service.purge_expired()["inbound"] == 1


def test_storing_a_meta_timestamp_grants_no_consent_and_sends_nothing(settings):
    settings.WHATSAPP_ENABLED = False
    settings.WHATSAPP_PILOT_NUMBERS = []
    settings.WHATSAPP_ALLOW_ALL = False
    user = UserFactory()
    WhatsAppOptIn.objects.create(user=user, phone_e164="+919876543210", opted_in=False)
    service.handle_webhook(_incoming(type="text", text={"body": "hello"}, timestamp="1700000000"))
    row = WhatsAppOptIn.objects.get(user=user)
    assert (row.opted_in, row.opted_in_at) == (False, None)
    assert WhatsAppInbound.objects.get().meta_timestamp is not None
    assert not WhatsAppConsentEvent.objects.exists() and not WhatsAppMessage.objects.exists()
    assert service.notify(user, "booking_confirmed", CONFIRMED_VALUES, event_key="k") is None


def test_retention_removes_old_messages_and_records_but_keeps_consent(on):
    from datetime import timedelta

    from django.utils import timezone

    on.WHATSAPP_RETENTION_DAYS = 30
    _opted_in()
    service.handle_webhook(_incoming(type="text", text={"body": "STOP"}))
    service.handle_webhook(_incoming(id="wamid.NEW", type="text", text={"body": "hello"}))
    old = timezone.now() - timedelta(days=31)
    WhatsAppInbound.objects.filter(wamid="wamid.IN1").update(received_at=old)
    WhatsAppWebhookEvent.objects.filter(pk=WhatsAppWebhookEvent.objects.order_by("id").first().pk).update(received_at=old)
    assert service.purge_expired() == {"inbound": 1, "webhook_events": 1}
    assert list(WhatsAppInbound.objects.values_list("wamid", flat=True)) == ["wamid.NEW"]
    assert WhatsAppConsentEvent.objects.filter(action="opt_out").count() == 1


def test_statuses_never_step_back(on):
    msg = WhatsAppMessage.objects.create(to_e164="+919876543210", event="e", template="t", event_key="k2", status="read",
                                         wamid="wamid.Y")
    service.handle_webhook({"entry": [{"changes": [{"value": {"statuses": [{"id": "wamid.Y", "status": "delivered"}]}}]}]})
    msg.refresh_from_db()
    assert msg.status == "read"


def test_status_is_for_the_main_admin_only(on):
    assert _api(UserFactory(user_type=UserType.STUDENT)).get("/api/whatsapp/status/").status_code == 403
    data = _api(UserFactory(user_type=UserType.ADMIN)).get("/api/whatsapp/status/").json()
    assert data["enabled"] is True and data["missing_settings"] == []


def test_booking_events_queue_the_matching_template(on):
    from iic_booking.equipment.models import BookingEventType
    from iic_booking.whatsapp.hooks import booking_event

    user = _opted_in()

    class Eq:
        name = "X-Ray Fluorescence (XRF)"

    class Slots:
        def order_by(self, *_):
            return []

    class Booking:
        pk, virtual_booking_id, equipment, daily_slots = 9, "IICXRF202600009", Eq(), Slots()
        status = "BOOKED"

    Booking.user = user

    class Event:
        event_id, event_type, booking = 77, BookingEventType.CREATED, Booking()

    with patch("iic_booking.whatsapp.tasks.deliver_whatsapp.delay"):
        booking_event(Event())
        Event.event_type = BookingEventType.COMMENT
        booking_event(Event())
    (msg,) = WhatsAppMessage.objects.all()
    assert msg.event == "booking_confirmed" and msg.params[1:3] == ["IICXRF202600009", "X-Ray Fluorescence (XRF)"] and len(msg.params) == 5
