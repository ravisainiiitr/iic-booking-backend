"""One-click unsubscribe, daily digest, quiet hours and browser push."""

from __future__ import annotations

import base64
import json
from datetime import datetime, time, timedelta
from unittest import mock

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from django.core import mail
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.communication import notification_preferences as prefs
from iic_booking.communication import web_push
from iic_booking.communication.models import (
    CommunicationLog,
    CommunicationTemplate,
    NotificationDeliverySettings,
    NotificationDigestItem,
    NotificationPreference,
    WebPushSubscription,
)
from iic_booking.communication.service import CommunicationService
from iic_booking.communication.tasks import send_due_notification_digests
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _portal(settings):
    settings.FRONTEND_URL = "https://portal.example"
    settings.EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"


def _template(code):
    return CommunicationTemplate.objects.create(
        code=code, name=code, communication_type="email", subject=f"Subject {code}", body_text="Body", is_active=True
    )


def _student():
    return UserFactory(admin_approved=True, user_type=UserType.STUDENT)


def _local(hour, minute=0, days=0):
    today = timezone.localdate() + timedelta(days=days)
    return timezone.make_aware(datetime.combine(today, time(hour, minute)))


# One-click unsubscribe ---------------------------------------------------------------------------------------------


def test_optional_email_carries_one_click_unsubscribe_headers_and_service_email_does_not():
    user = _student()
    _template("booking_reminder_email")
    _template("booking_confirmed_email")

    CommunicationService.send_email(user, template="booking_reminder_email")
    CommunicationService.send_email(user, template="booking_confirmed_email")

    reminder, confirmed = mail.outbox
    assert reminder.extra_headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert reminder.extra_headers["List-Unsubscribe"].startswith("<https://portal.example/api/notifications/unsubscribe/")
    assert "List-Unsubscribe" not in (confirmed.extra_headers or {})


def test_unsubscribe_link_get_redirects_info_describes_and_post_switches_off():
    user = _student()
    token = prefs.unsubscribe_token(user, "reminders")
    client = APIClient()

    resp = client.get(f"/api/notifications/unsubscribe/{token}/")
    assert resp.status_code == 302 and resp["Location"] == f"https://portal.example/unsubscribe?t={token}"
    assert not NotificationPreference.objects.filter(user=user).exists()

    info = client.get(f"/api/notifications/unsubscribe/{token}/", {"info": "1"}).json()
    assert info["label"] == "Reminders" and info["email_enabled"] is True

    resp = client.post(f"/api/notifications/unsubscribe/{token}/", "List-Unsubscribe=One-Click",
                       content_type="application/x-www-form-urlencoded")
    assert resp.status_code == 200 and resp.json()["email_enabled"] is False
    assert NotificationPreference.objects.get(user=user, category="reminders").email_enabled is False


def test_unsubscribe_rejects_tampered_and_locked_tokens():
    user = _student()
    client = APIClient()
    bad = prefs.unsubscribe_token(user, "reminders")[:-2] + "xx"
    assert client.post(f"/api/notifications/unsubscribe/{bad}/").status_code == 400
    locked = prefs.unsubscribe_token(user, "bookings")
    assert client.post(f"/api/notifications/unsubscribe/{locked}/").status_code == 400


# Digest and quiet hours --------------------------------------------------------------------------------------------


def test_quiet_hours_span_midnight():
    row = NotificationDeliverySettings(quiet_hours_enabled=True, quiet_start=time(22), quiet_end=time(7))
    assert prefs.in_quiet_hours(row, _local(23, 30))
    assert prefs.in_quiet_hours(row, _local(6, 59))
    assert not prefs.in_quiet_hours(row, _local(7, 0))
    assert prefs.quiet_hours_end(row, _local(23, 30)) == _local(7, days=1)
    assert prefs.quiet_hours_end(row, _local(1, 0)) == _local(7)


def test_email_decision_digest_quiet_and_locked():
    user = _student()
    NotificationPreference.objects.create(user=user, category="announcements", email_digest=True)
    NotificationDeliverySettings.objects.create(user=user, quiet_hours_enabled=True, digest_hour=9)
    night = _local(23)

    digest = prefs.email_decision(user, "announcements", night)
    assert digest.deferred and digest.reason == "digest" and digest.deliver_after == _local(9, days=1)
    quiet = prefs.email_decision(user, "reminders", night)
    assert quiet.deferred and quiet.reason == "quiet" and quiet.deliver_after == _local(7, days=1)
    assert prefs.email_decision(user, "bookings", night).action == prefs.SEND
    assert prefs.email_decision(user, "reminders", _local(12)).action == prefs.SEND


def test_held_emails_go_out_as_one_digest():
    user = _student()
    _template("booking_reminder_email")
    _template("notice_published_email")
    NotificationPreference.objects.create(user=user, category="reminders", email_digest=True)
    NotificationPreference.objects.create(user=user, category="announcements", email_digest=True)

    first = CommunicationService.send_email(user, template="booking_reminder_email", template_context={"link": "/my-bookings"})
    second = CommunicationService.send_email(user, template="notice_published_email")
    assert first.status == second.status == CommunicationLog.CommunicationStatus.QUEUED
    assert mail.outbox == []
    assert NotificationDigestItem.objects.filter(user=user, sent_at__isnull=True).count() == 2

    assert send_due_notification_digests() == 0
    NotificationDigestItem.objects.update(deliver_after=timezone.now() - timedelta(minutes=1))
    assert send_due_notification_digests() == 2

    assert len(mail.outbox) == 1
    digest = mail.outbox[0]
    assert digest.subject == "Your daily IIC portal digest: 2 updates"
    assert "Subject booking_reminder_email" in digest.body and "https://portal.example/my-bookings" in digest.body
    first.refresh_from_db()
    assert first.status == CommunicationLog.CommunicationStatus.SENT and first.metadata["delivered_in"]
    assert send_due_notification_digests() == 0


def test_preferences_api_saves_digest_and_quiet_hours_atomically():
    user = _student()
    client = APIClient()
    client.force_authenticate(user=user)

    resp = client.put(
        "/api/notifications/preferences/",
        {
            "categories": [{"key": "announcements", "digest": True}, {"key": "bookings", "digest": True}],
            "delivery": {"quiet_hours_enabled": True, "quiet_start": "21:30", "quiet_end": "06:45", "digest_hour": 18},
        },
        format="json",
    )
    assert resp.status_code == 200
    body = resp.json()
    rows = {c["key"]: c for c in body["categories"]}
    assert rows["announcements"]["digest"] is True and rows["bookings"]["digest"] is False
    assert body["delivery"] == {
        "quiet_hours_enabled": True, "quiet_start": "21:30", "quiet_end": "06:45", "digest_hour": 18,
    }

    bad = client.put(
        "/api/notifications/preferences/",
        {"categories": "nope", "delivery": {"digest_hour": 5}},
        format="json",
    )
    assert bad.status_code == 400
    assert NotificationDeliverySettings.objects.get(user=user).digest_hour == 18
    assert client.put("/api/notifications/preferences/", {"categories": [], "delivery": {"quiet_start": "25:00"}},
                      format="json").status_code == 400


# Web push ----------------------------------------------------------------------------------------------------------


def test_encrypt_payload_matches_rfc_8291_example():
    sender = ec.derive_private_key(
        int.from_bytes(web_push.b64url_decode("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"), ec.SECP256R1()
    )
    body = web_push.encrypt_payload(
        b"When I grow up, I want to be a watermelon",
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
        "BTBZMqHH6r4Tts7J_aSIgg",
        salt=web_push.b64url_decode("DGv6ra1nlYgDCS1FRnbzlw"),
        sender_key=sender,
    )
    assert web_push.b64url(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl"
        "7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
    )


def test_vapid_token_verifies_with_the_published_key():
    header = web_push.vapid_authorization("https://fcm.googleapis.com/fcm/send/abc", now=1_000_000)
    token = header.split("t=")[1].split(",")[0]
    head, claims, sig = token.split(".")
    assert json.loads(web_push.b64url_decode(claims))["aud"] == "https://fcm.googleapis.com"
    raw = web_push.b64url_decode(sig)
    public = ec.EllipticCurvePublicKey.from_encoded_point(
        ec.SECP256R1(), web_push.b64url_decode(web_push.vapid_public_key())
    )
    signature = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public.verify(signature, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
    assert header.endswith(f"k={web_push.vapid_public_key()}")


def _browser_keys():
    ua = ec.generate_private_key(ec.SECP256R1())
    from cryptography.hazmat.primitives import serialization

    p256dh = web_push.b64url(
        ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    )
    return p256dh, base64.urlsafe_b64encode(b"0123456789abcdef").decode().rstrip("=")


def test_subscribe_endpoint_and_push_delivery_drops_gone_browsers():
    user = _student()
    client = APIClient()
    client.force_authenticate(user=user)
    p256dh, auth = _browser_keys()
    sub = {"endpoint": "https://push.example/abc", "keys": {"p256dh": p256dh, "auth": auth}}
    resp = client.post("/api/notifications/push/", sub, format="json")
    assert resp.status_code == 200 and resp.json()["subscriptions"] == 1
    assert resp.json()["public_key"] == web_push.vapid_public_key()
    assert client.post("/api/notifications/push/", {"endpoint": "http://x"}, format="json").status_code == 400

    with mock.patch("requests.post") as post:
        post.return_value.status_code = 201
        log = CommunicationService.send_push_notification(user, title="Results ready", message="Download now")
        assert web_push.deliver_for_log(log) == 1
        kwargs = post.call_args.kwargs
        assert kwargs["headers"]["Content-Encoding"] == "aes128gcm"
        assert kwargs["headers"]["Authorization"].startswith("vapid t=")

        post.return_value.status_code = 410
        assert web_push.deliver_for_log(log) == 0
    assert not WebPushSubscription.objects.filter(user=user).exists()


def test_push_waits_out_quiet_hours_for_optional_categories_only():
    user = _student()
    NotificationDeliverySettings.objects.create(
        user=user, quiet_hours_enabled=True, quiet_start=time(0, 0), quiet_end=time(23, 59)
    )
    assert not prefs.push_allowed_now(user, "reminders", _local(12))
    assert prefs.push_allowed_now(user, "results", _local(12))
