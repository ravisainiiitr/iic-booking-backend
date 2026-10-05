"""The analysis reservation reminder shows the start time in IST, not a raw UTC ISO timestamp."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.remote_analysis import tasks
from iic_booking.remote_analysis.collaboration_models import NotificationPreference
from iic_booking.remote_analysis.constants import ReservationStatus
from iic_booking.remote_analysis.notifications import NotificationEngine
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation


@pytest.mark.django_db
def test_reservation_reminder_body_uses_ist(ra_user, monkeypatch):
    bodies = []
    monkeypatch.setattr(
        NotificationEngine, "notify", lambda self, user, kind, title, body="", **kwargs: bodies.append(body)
    )
    prefs, _ = NotificationPreference.objects.get_or_create(user=ra_user)
    prefs.reminder_minutes_before = 30
    prefs.save()
    start = (timezone.now() + timedelta(minutes=29)).replace(microsecond=0)
    AnalysisReservation.objects.create(
        user=ra_user,
        status=ReservationStatus.RESERVED,
        requested_start=start,
        requested_end=start + timedelta(hours=1),
        priority=100,
    )

    assert tasks.send_reservation_reminders()["sent"] == 1

    local = timezone.localtime(start)
    assert local.utcoffset() == timedelta(hours=5, minutes=30)
    assert bodies == [f"Your analysis reservation starts at {local:%d %b %Y, %I:%M %p}"]
