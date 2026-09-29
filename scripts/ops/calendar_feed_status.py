"""
Read-only: which users have created a calendar sync link and whether a calendar app has fetched it.
Prints user id / type, link creation time, last fetch time and the events currently in each feed.
Never prints tokens or email addresses. Makes no database changes.

Usage (inside the django container): python - < scripts/ops/calendar_feed_status.py
"""

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.production")
django.setup()

from django.utils import timezone  # noqa: E402

from iic_booking.equipment.calendar_sync import (  # noqa: E402
    _slot_segments,
    feed_bookings_for_user,
    is_calendar_sync_eligible,
)
from iic_booking.equipment.models import CalendarFeedToken  # noqa: E402

now = timezone.now()
rows = CalendarFeedToken.objects.select_related("user").order_by("created_at")
print("now", timezone.localtime(now).isoformat(timespec="minutes"), "| links", rows.count())
for t in rows:
    u = t.user
    bookings = list(feed_bookings_for_user(u))
    upcoming = sorted(
        (seg[0], b.booking_id, b.status)
        for b in bookings
        for seg in _slot_segments(b)
        if seg[1] > now
    )
    last = timezone.localtime(t.last_accessed_at).isoformat(timespec="minutes") if t.last_accessed_at else "NEVER"
    print(
        f"user={u.pk} type={u.user_type} eligible={is_calendar_sync_eligible(u)}"
        f" | link_created={timezone.localtime(t.created_at).isoformat(timespec='minutes')}"
        f" | last_fetched_by_calendar_app={last}"
        f" | bookings_in_feed={len(bookings)} upcoming_events={len(upcoming)}"
    )
    for start, booking_id, status in upcoming[:5]:
        print(f"    upcoming booking={booking_id} status={status} start={timezone.localtime(start).isoformat(timespec='minutes')}")
