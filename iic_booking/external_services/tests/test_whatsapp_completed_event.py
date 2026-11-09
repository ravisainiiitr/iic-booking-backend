"""A completed booking reaches WhatsApp even though its event is recorded without the generic email (9 Nov 2026):
both completion paths send their own completion email and record the event with send_notification=False."""

from datetime import timedelta
from unittest.mock import patch

import pytest
from django.utils import timezone

from iic_booking.equipment.booking_events import create_booking_event
from iic_booking.equipment.models import BookingEventType, BookingStatus

from .conftest import make_user

pytestmark = pytest.mark.django_db


def test_completed_event_goes_to_whatsapp_even_when_silent(factory):
    booking = factory.booking(make_user(), factory.equipment(), factory.at(timezone.localdate() + timedelta(days=3)),
                              payment_settled_at=timezone.now())
    with patch("iic_booking.whatsapp.hooks.booking_event") as hook:
        create_booking_event(booking=booking, event_type=BookingEventType.COMPLETED, previous_status=BookingStatus.BOOKED,
                             new_status=BookingStatus.COMPLETED, comment="done", send_notification=False)
        assert hook.call_count == 1
        create_booking_event(booking=booking, event_type=BookingEventType.CANCELLED, previous_status=BookingStatus.BOOKED,
                             new_status=BookingStatus.CANCELLED, comment="cancelled", send_notification=False)
        assert hook.call_count == 2  # every event reaches the hook; the hook picks what to send
