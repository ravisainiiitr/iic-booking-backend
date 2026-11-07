"""A booking refund or cancellation running at the same moment as another credits the wallet once: the row is locked and read again."""

from __future__ import annotations

from decimal import Decimal

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext

from iic_booking.equipment.api_views import refund_booking_internal
from iic_booking.equipment.booking_cancellation import CancellationValidationError, perform_booking_cancellation
from iic_booking.equipment.models import Booking

from .test_lab_failure_refunds import _shipped
from .test_refund_policy import _wallet, policy, user  # noqa: F401

pytestmark = pytest.mark.django_db


def _two_copies(factory, user):
    booking = _shipped(factory, user)
    return Booking.objects.get(pk=booking.pk), Booking.objects.get(pk=booking.pk)


def test_refund_from_an_out_of_date_copy_is_refused_and_credits_once(policy, factory, user):
    first, stale = _two_copies(factory, user)
    refund_booking_internal(first, "first", None)
    credited = _wallet(user, factory)
    assert credited > Decimal("0.00")
    with pytest.raises(ValueError, match="already been refunded"):
        refund_booking_internal(stale, "second", None)
    assert _wallet(user, factory) == credited


def test_refund_takes_the_row_lock(policy, factory, user):
    first, _ = _two_copies(factory, user)
    with CaptureQueriesContext(connection) as queries:
        refund_booking_internal(first, "first", None)
    assert any("FOR UPDATE" in q["sql"] and "equipment_booking" in q["sql"] for q in queries)


def test_cancellation_after_a_refund_from_an_out_of_date_copy_credits_nothing_more(policy, factory, user):
    first, stale = _two_copies(factory, user)
    refund_booking_internal(first, "first", None)
    credited = _wallet(user, factory)
    with pytest.raises(CancellationValidationError):
        perform_booking_cancellation(stale, slot_ids=[], should_refund=True, cancel_notes="", actor=None, allow_started_slots=True)
    assert _wallet(user, factory) == credited
