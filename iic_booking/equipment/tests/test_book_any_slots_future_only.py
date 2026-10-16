"""The "Book other free slots this week" fallback (book_any_available_slots) only picks slots that have not started.

Past slots keep status AVAILABLE (the calendar shows them as "No booking"), so the alternative search has to
skip them; otherwise a mid-week fallback picks Monday's passed slots and the booking fails.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from django.utils import timezone

from iic_booking.equipment.api_views import _find_alternative_available_slots, _find_one_available_slot_in_window
from iic_booking.equipment.models import DailySlot
from iic_booking.users.models.user_type import UserType

from .test_booking_template_preferred_slot import _book, _student_with_wallet, no_portal_lock  # noqa: F401


@pytest.mark.django_db
def test_alternative_search_skips_slots_that_have_started(egs_factory):
    eq = egs_factory.equipment()
    now = timezone.now()
    past = egs_factory.slot(eq, now - timedelta(hours=3))
    started = egs_factory.slot(eq, now - timedelta(minutes=10))
    future = egs_factory.slot(eq, now + timedelta(hours=2))
    window = {"visible_week_start": (now - timedelta(days=1)).date(), "visible_week_end": (now + timedelta(days=1)).date()}

    found = _find_alternative_available_slots(eq, 60, UserType.STUDENT, True, **window)
    assert found is not None
    assert found[0] == [future.pk]

    one = _find_one_available_slot_in_window(eq, UserType.STUDENT, True, **window)
    assert one is not None and one[0] == future.pk
    assert past.pk not in found[0] and started.pk not in found[0]


@pytest.mark.django_db
def test_alternative_search_finds_nothing_when_only_past_slots_are_free(egs_factory):
    eq = egs_factory.equipment()
    now = timezone.now()
    egs_factory.slot(eq, now - timedelta(hours=3))
    egs_factory.slot(eq, now - timedelta(hours=2))
    assert _find_alternative_available_slots(eq, 60, UserType.STUDENT, True) is None
    assert _find_one_available_slot_in_window(eq, UserType.STUDENT, True) is None


@pytest.mark.django_db
def test_any_free_slots_books_a_future_slot_when_earlier_slots_this_week_have_passed(egs_factory, no_portal_lock):  # noqa: F811
    eq = egs_factory.equipment()
    start = egs_factory.future(days=3, hour=10)
    taken = egs_factory.slot(eq, start)
    free_later = egs_factory.slot(eq, start + timedelta(hours=2))
    passed = egs_factory.slot(eq, timezone.now() - timedelta(hours=3))
    winner, _ = _student_with_wallet(egs_factory)
    student, _ = _student_with_wallet(egs_factory)
    assert _book(egs_factory, winner, eq, [taken]).status_code == 201

    body = {
        "slot_ids": [taken.pk],
        "start_time": taken.start_datetime.isoformat(),
        "end_time": taken.end_datetime.isoformat(),
        "input_values": {},
        "waitlist_on_failure": False,
        "book_any_available_slots": True,
        "visible_week_start": timezone.localtime(passed.start_datetime).date().isoformat(),
        "visible_week_end": timezone.localtime(free_later.start_datetime).date().isoformat(),
    }
    resp = egs_factory.client_for(student).post(f"/api/equipments/{eq.pk}/book/", body, format="json")
    assert resp.status_code == 201, resp.data
    free_later.refresh_from_db()
    passed.refresh_from_db()
    assert free_later.status == "BOOKED"
    assert passed.status == "AVAILABLE"
    assert not DailySlot.objects.filter(pk=passed.pk, booking__isnull=False).exists()
