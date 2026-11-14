"""I-STEM live activity (14 Nov 2026): every call to I-STEM with the time from the portal event to I-STEM's answer,
for the Main Administrator only."""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from iic_booking.istem import live_feed
from iic_booking.istem.choices import AttemptOutcome, JobState, Operation, SyncState
from iic_booking.istem.models import IstemApiAttempt, IstemBookingLink, IstemOutboxJob
from iic_booking.users.models.user_type import UserType
from iic_booking.users.tests.factories import UserFactory

from .support import apply_live, make_booking

pytestmark = pytest.mark.django_db


def _call(link, operation, *, trigger="automatic", queued_ago=8.0, took=3.0, outcome=AttemptOutcome.SUCCESS):
    now = timezone.now()
    job = IstemOutboxJob.objects.create(operation=operation, dedupe_key=f"{operation}:{link.pk}:{trigger}", trigger=trigger,
                                        booking_link=link, state=JobState.DONE)
    IstemOutboxJob.objects.filter(pk=job.pk).update(created_at=now - timedelta(seconds=queued_ago))
    return IstemApiAttempt.objects.create(job=job, booking_link=link, operation=operation, outcome=outcome,
                                          started_at=now - timedelta(seconds=took), finished_at=now, duration_ms=int(took * 1000))


def test_each_call_is_named_as_on_the_demo_screen_with_its_timings(settings):
    apply_live(settings, ISTEM_AUTOMATIC_FBR=True)
    booking = make_booking()
    link = IstemBookingLink.objects.create(booking=booking, sync_state=SyncState.CONFIRMED, request_id="95610",
                                           fbr_no="1976095610", confirmation_id="72020")
    _call(link, Operation.CREATE_BOOKING)
    _call(link, Operation.CONFIRM_BOOKING)
    _call(link, Operation.CANCEL_SUPERSEDED, trigger="replacement confirmed", outcome=AttemptOutcome.DEFINITE_FAILURE)
    IstemOutboxJob.objects.create(operation=Operation.EXECUTE_FBR, dedupe_key="EXECUTE_FBR:x", trigger="discovery",
                                  booking_link=link, state=JobState.PENDING, next_attempt_at=timezone.now())

    feed = live_feed.live_feed()
    labels = {e["label"]: e for e in feed["events"]}
    created = labels["Booking created on I-STEM (FBR)"]
    assert created["booking_ref"] == (booking.virtual_booking_id or str(booking.pk)) and created["fbr_no"] == "1976095610"
    assert created["status"] == "done" and created["total_seconds"] == pytest.approx(8.0, abs=1)
    assert created["wait_seconds"] == pytest.approx(5.0, abs=1) and created["call_seconds"] == 3.0
    assert "Slot allocated (booking confirmed)" in labels
    assert labels["Rescheduled: old slot cancelled on I-STEM"]["status"] == "refused"
    assert [w["label"] for w in feed["waiting"]] == ["FBR executed (booking completed)"]
    stats = {s["kind"]: s for s in feed["stats"]}
    assert stats["booking_created"]["calls"] == 1
    assert stats["booking_created"]["median_total_seconds"] == pytest.approx(8.0, abs=1)
    assert feed["peak"] == {"active": False, "ends_at": None}


def test_cancellations_moves_and_existing_users_read_as_such():
    assert live_feed.classify(Operation.EXECUTE_FBR, "portal cancellation")[0] == "booking_cancelled"
    assert live_feed.classify(Operation.CREATE_BOOKING, "slot moved")[0] == "booking_moved"
    assert live_feed.classify(Operation.ENSURE_USER, "", "existing I-STEM user linked automatically (no OTP)")[0] == "user_existing"
    assert live_feed.classify(Operation.ENSURE_USER, "", "I-STEM user created")[0] == "user_created"


def test_only_the_main_administrator_sees_the_live_feed():
    client = APIClient()
    client.force_authenticate(UserFactory(user_type=UserType.FACULTY))
    assert client.get("/api/istem/live/").status_code == 403
    client.force_authenticate(UserFactory(user_type=UserType.ADMIN))
    response = client.get("/api/istem/live/", {"since": (timezone.now() - timedelta(hours=1)).isoformat()})
    assert response.status_code == 200
    assert set(response.data) >= {"events", "waiting", "stats", "peak", "realtime"}
