"""AI.9: notification list exposes real_booking_id for Android deep links."""

from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from rest_framework.test import APIClient

from iic_booking.communication.models import CommunicationLog


@pytest.mark.django_db
def test_notifications_list_exposes_real_booking_id():
    User = get_user_model()
    user = User.objects.create_user(email="notif-deep@example.com", password="x")
    CommunicationLog.objects.create(
        recipient=user,
        communication_type=CommunicationLog.CommunicationType.PUSH_NOTIFICATION,
        subject="Booking Confirmed",
        message="Your booking is confirmed.",
        status=CommunicationLog.CommunicationStatus.SENT,
        metadata={
            "notification_type": "booking_confirmed",
            "link": "https://equip.iitr.ac.in/my-bookings?booking=GEN-SAMPLE-1",
            "booking_id": "GEN-SAMPLE-1",
            "booking_display_id": "GEN-SAMPLE-1",
            "real_booking_id": 4242,
        },
    )

    client = APIClient()
    client.force_authenticate(user=user)
    resp = client.get("/api/notifications/")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1
    row = data[0]
    assert row["real_booking_id"] == 4242
    assert row["virtual_booking_id"] == "GEN-SAMPLE-1"
    assert "booking=GEN-SAMPLE-1" in (row.get("link") or "")


@pytest.mark.django_db
def test_notifications_list_is_recipient_scoped():
    User = get_user_model()
    owner = User.objects.create_user(email="notif-owner@example.com", password="x")
    other = User.objects.create_user(email="notif-other@example.com", password="x")
    CommunicationLog.objects.create(
        recipient=owner,
        communication_type=CommunicationLog.CommunicationType.PUSH_NOTIFICATION,
        subject="Private",
        message="Owner only",
        status=CommunicationLog.CommunicationStatus.SENT,
        metadata={"real_booking_id": 99, "booking_id": "OWN-1"},
    )

    client = APIClient()
    client.force_authenticate(user=other)
    resp = client.get("/api/notifications/")
    assert resp.status_code == 200
    assert resp.json() == []
