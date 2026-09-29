"""Failed attempts that ended in an Equipment Group alternate booking do not count toward Type A rush relief."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from iic_booking.equipment import api_views
from iic_booking.equipment.models import BookingAttemptLog, BookingAttemptOutcome


def _failed(user, equipment, *, minutes_ago=0, reason="Selected slots are not available."):
    log = BookingAttemptLog.objects.create(
        user=user, equipment=equipment, outcome=BookingAttemptOutcome.FAILED, failure_reason=reason
    )
    if minutes_ago:
        BookingAttemptLog.objects.filter(pk=log.pk).update(requested_at=timezone.now() - timedelta(minutes=minutes_ago))
        log.refresh_from_db()
    return log


@pytest.mark.django_db
def test_attempt_resolved_by_alternate_booking_is_not_counted(egs_factory):
    user = egs_factory.student()
    source = egs_factory.equipment()
    _failed(user, source, minutes_ago=5)
    latest = _failed(user, source, minutes_ago=1)
    assert len(api_views._peak_qualified_failed_attempts(user, source)) == 2

    marked = api_views._mark_attempt_resolved_by_alternate(
        user=user, source_equipment_id=source.pk, booking=SimpleNamespace(booking_id=77)
    )

    assert marked.pk == latest.pk
    latest.refresh_from_db()
    assert latest.additional_info[api_views.ALTERNATE_BOOKING_RESOLVED_KEY] == 77
    remaining = api_views._peak_qualified_failed_attempts(user, source)
    assert [log.pk for log in remaining] != [latest.pk]
    assert len(remaining) == 1


@pytest.mark.django_db
def test_marking_keeps_existing_additional_info_and_ignores_old_attempts(egs_factory):
    user = egs_factory.student()
    source = egs_factory.equipment()
    old = _failed(user, source, minutes_ago=api_views.ALTERNATE_ATTEMPT_LINK_MINUTES + 10)

    assert api_views._mark_attempt_resolved_by_alternate(
        user=user, source_equipment_id=source.pk, booking=SimpleNamespace(booking_id=1)
    ) is None
    assert len(api_views._peak_qualified_failed_attempts(user, source)) == 1

    recent = _failed(user, source)
    BookingAttemptLog.objects.filter(pk=recent.pk).update(additional_info={"input_values": {"A": "1"}})
    api_views._mark_attempt_resolved_by_alternate(
        user=user, source_equipment_id=source.pk, booking=SimpleNamespace(booking_id=2)
    )
    recent.refresh_from_db()
    assert recent.additional_info["input_values"] == {"A": "1"}
    assert recent.additional_info[api_views.ALTERNATE_BOOKING_RESOLVED_KEY] == 2
    assert [log.pk for log in api_views._peak_qualified_failed_attempts(user, source)] == [old.pk]


@pytest.mark.django_db
def test_second_alternate_booking_does_not_remark_the_same_attempt(egs_factory):
    user = egs_factory.student()
    source = egs_factory.equipment()
    _failed(user, source)
    booking = SimpleNamespace(booking_id=5)

    assert api_views._mark_attempt_resolved_by_alternate(
        user=user, source_equipment_id=source.pk, booking=booking
    ) is not None
    assert api_views._mark_attempt_resolved_by_alternate(
        user=user, source_equipment_id=source.pk, booking=SimpleNamespace(booking_id=6)
    ) is None


@pytest.mark.django_db
def test_other_users_and_equipment_are_untouched(egs_factory):
    user = egs_factory.student()
    other = egs_factory.student()
    source = egs_factory.equipment()
    elsewhere = egs_factory.equipment()
    mine_elsewhere = _failed(user, elsewhere)
    theirs = _failed(other, source)

    assert api_views._mark_attempt_resolved_by_alternate(
        user=user, source_equipment_id=source.pk, booking=SimpleNamespace(booking_id=9)
    ) is None
    for log in (mine_elsewhere, theirs):
        log.refresh_from_db()
        assert not log.additional_info
