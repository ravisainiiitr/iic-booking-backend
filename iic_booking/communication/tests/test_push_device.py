"""Lightweight tests for PushDevice registration helpers."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model

from iic_booking.communication.fcm import send_fcm_to_token
from iic_booking.communication.models import PushDevice


@pytest.mark.django_db
def test_push_device_register_unique():
    User = get_user_model()
    user = User.objects.create_user(email="push-user@example.com", password="x")
    d1, c1 = PushDevice.objects.update_or_create(
        user=user,
        token="tok-abc",
        defaults={"platform": PushDevice.Platform.ANDROID, "device_name": "Pixel"},
    )
    assert c1 is True
    d2, c2 = PushDevice.objects.update_or_create(
        user=user,
        token="tok-abc",
        defaults={"platform": PushDevice.Platform.ANDROID, "device_name": "Pixel 2"},
    )
    assert c2 is False
    assert d1.id == d2.id
    assert PushDevice.objects.filter(user=user).count() == 1


def test_fcm_skipped_without_server_key(settings):
    settings.FCM_SERVER_KEY = ""
    result = send_fcm_to_token(token="x", title="t", body="b")
    assert result["ok"] is False
    assert result.get("skipped") is True
