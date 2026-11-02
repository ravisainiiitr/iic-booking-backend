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
from iic_booking.whatsapp.models import WhatsAppMessage, WhatsAppOptIn

pytestmark = pytest.mark.django_db

SECRET = "unit-test-app-secret-not-real"


@pytest.fixture
def on(settings):
    settings.WHATSAPP_ENABLED = True
    settings.WHATSAPP_PHONE_NUMBER_ID = "1234567890"
    settings.WHATSAPP_ACCESS_TOKEN = "unit-test-token-not-real"
    settings.WHATSAPP_APP_SECRET = SECRET
    settings.WHATSAPP_VERIFY_TOKEN = "unit-test-verify"
    settings.WHATSAPP_PILOT_NUMBERS = []
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
    assert service.notify(user, "booking_confirmed", ["B1", "XRD", "Mon"], event_key="k1") is None


def test_nothing_is_queued_without_opt_in(on):
    user = UserFactory(user_type=UserType.STUDENT)
    assert service.notify(user, "booking_confirmed", ["B1", "XRD", "Mon"], event_key="k1") is None


def test_pilot_numbers_limit_who_receives(on):
    on.WHATSAPP_PILOT_NUMBERS = ["+911111111111"]
    user = _opted_in()
    assert service.notify(user, "booking_confirmed", ["B1", "XRD", "Mon"], event_key="k1") is None


def test_a_message_is_sent_once_per_event(on):
    user = _opted_in()
    with patch("iic_booking.whatsapp.tasks.deliver_whatsapp.delay") as delay:
        first = service.notify(user, "booking_confirmed", ["B1", "XRD", "Mon"], event_key="k1")
        again = service.notify(user, "booking_confirmed", ["B1", "XRD", "Mon"], event_key="k1")
    assert first is not None and again is None
    assert first.template == "iic_booking_confirmed" and first.to_e164 == "+919876543210"


def test_delivery_sends_the_template_and_records_the_message_id(on):
    user = _opted_in()
    with patch("iic_booking.whatsapp.tasks.deliver_whatsapp.delay"):
        msg = service.notify(user, "booking_confirmed", ["B1", "XRD", "Mon 10:00"], event_key="k1")
    session = FakeSession(FakeResponse(200, {"messages": [{"id": "wamid.ABC"}]}))
    assert service.deliver(msg.pk, session=session) == "sent"
    msg.refresh_from_db()
    assert msg.wamid == "wamid.ABC"
    sent = session.calls[0]
    assert sent["url"].endswith("/1234567890/messages") and sent["headers"]["Authorization"].startswith("Bearer ")
    assert sent["json"]["to"] == "919876543210" and sent["json"]["template"]["name"] == "iic_booking_confirmed"
    assert [p["text"] for p in sent["json"]["template"]["components"][0]["parameters"]] == ["B1", "XRD", "Mon 10:00"]


def test_a_refused_message_is_marked_failed_and_a_server_error_is_retried(on):
    user = _opted_in()
    with patch("iic_booking.whatsapp.tasks.deliver_whatsapp.delay"):
        msg = service.notify(user, "booking_cancelled", ["B1", "XRD", "Mon"], event_key="k1")
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
        msg = service.notify(user, "booking_confirmed", ["B1", "XRD", "Mon"], event_key="k1")
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

    Booking.user = user

    class Event:
        event_id, event_type, booking = 77, BookingEventType.CREATED, Booking()

    with patch("iic_booking.whatsapp.tasks.deliver_whatsapp.delay"):
        booking_event(Event())
        Event.event_type = BookingEventType.COMMENT
        booking_event(Event())
    (msg,) = WhatsAppMessage.objects.all()
    assert msg.event == "booking_confirmed" and msg.params[:2] == ["IICXRF202600009", "X-Ray Fluorescence (XRF)"]
