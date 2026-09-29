"""Calendar sync: iCalendar (.ics) subscription feed and per-booking downloads.

Available to internal and external booking users only; staff roles (admin, department
admin, OIC, lab incharge, accounts, org admin, external relations) are excluded.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone as dt_timezone
from urllib.parse import quote

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.users.models.user_type import UserType

from .models import Booking, BookingStatus, CalendarFeedToken

CALENDAR_NAME = "IIC Bookings"
PRODID = "-//IIT Roorkee IIC//Equipment Bookings//EN"
UID_DOMAIN = "iic-booking.iitr"
FEED_PAST_DAYS = 90
FEED_MAX_BOOKINGS = 500
ACCESS_STAMP_INTERVAL = timedelta(minutes=10)
GOOGLE_ADD_BY_URL = "https://calendar.google.com/calendar/u/0/r/settings/addbyurl"

FEED_STATUSES = frozenset(
    s.value
    for s in (
        BookingStatus.PENDING,
        BookingStatus.PENDING_PAYMENT,
        BookingStatus.BOOKED,
        BookingStatus.HOLD,
        BookingStatus.DISRUPTION_PENDING,
        BookingStatus.PROCESSING,
        BookingStatus.COMPLETED,
    )
)
CONFIRMED_STATUSES = frozenset(
    s.value for s in (BookingStatus.BOOKED, BookingStatus.PROCESSING, BookingStatus.COMPLETED)
)
DEADLINE_EVENT_STATUSES = frozenset(
    s.value
    for s in (BookingStatus.PENDING, BookingStatus.PENDING_PAYMENT, BookingStatus.BOOKED, BookingStatus.HOLD)
)
STATUS_SUFFIX = {
    BookingStatus.PENDING.value: "pending approval",
    BookingStatus.PENDING_PAYMENT.value: "awaiting payment",
    BookingStatus.HOLD.value: "on hold",
    BookingStatus.DISRUPTION_PENDING.value: "action needed",
}

_ELIGIBLE_CODES = frozenset(
    code.lower() for code in (UserType.get_internal_user_codes() | UserType.get_external_user_codes())
)


def is_calendar_sync_eligible(user) -> bool:
    if user is None or not getattr(user, "is_authenticated", False) or not getattr(user, "is_active", False):
        return False
    return str(getattr(user, "user_type", "") or "").strip().lower() in _ELIGIBLE_CODES


def _new_token() -> str:
    return secrets.token_urlsafe(32)


def get_or_create_feed_token(user) -> CalendarFeedToken:
    existing = CalendarFeedToken.objects.filter(user=user).first()
    if existing:
        return existing
    try:
        with transaction.atomic():
            return CalendarFeedToken.objects.create(user=user, token=_new_token())
    except IntegrityError:
        return CalendarFeedToken.objects.get(user=user)


def regenerate_feed_token(user) -> CalendarFeedToken:
    obj = get_or_create_feed_token(user)
    obj.token = _new_token()
    obj.last_accessed_at = None
    obj.save(update_fields=["token", "last_accessed_at"])
    return obj


# --- iCalendar serialisation -------------------------------------------------


def _escape(value) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
        .replace("\r", "\\n")
    )


def _fold(line: str) -> str:
    """Fold a content line to at most 75 octets per physical line (RFC 5545 §3.1)."""
    if len(line.encode("utf-8")) <= 75:
        return line
    parts: list[str] = []
    current = ""
    current_len = 0
    limit = 75
    for ch in line:
        ch_len = len(ch.encode("utf-8"))
        if current_len + ch_len > limit:
            parts.append(current)
            current = ch
            current_len = ch_len
            limit = 74
        else:
            current += ch
            current_len += ch_len
    parts.append(current)
    return "\r\n ".join(parts)


def _fmt_utc(dt: datetime) -> str:
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt)
    return dt.astimezone(dt_timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _frontend_bookings_url() -> str:
    base = str(getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    return f"{base}/my-bookings" if base else ""


def _slot_segments(booking) -> list[tuple[datetime, datetime]]:
    """Merge the booking's daily slots into contiguous (start, end) runs."""
    slots = sorted(
        (s for s in booking.daily_slots.all() if s.start_datetime and s.end_datetime),
        key=lambda s: s.start_datetime,
    )
    segments: list[list[datetime]] = []
    for slot in slots:
        if segments and slot.start_datetime <= segments[-1][1]:
            if slot.end_datetime > segments[-1][1]:
                segments[-1][1] = slot.end_datetime
        else:
            segments.append([slot.start_datetime, slot.end_datetime])
    return [(start, end) for start, end in segments]


def _sample_deadline(booking):
    try:
        from .sample_submission_deadline_reminders import compute_sample_submission_deadline

        return compute_sample_submission_deadline(booking)
    except Exception:
        return None


def _booking_events(booking, now: datetime, dtstamp: str) -> list[list[str]]:
    segments = _slot_segments(booking)
    if not segments:
        return []
    equipment = booking.equipment
    eq_name = getattr(equipment, "name", "") or "Equipment"
    eq_code = getattr(equipment, "code", "") or ""
    eq_label = f"{eq_name} ({eq_code})" if eq_code else eq_name
    location = (getattr(equipment, "location", "") or "").strip()
    ref = booking.virtual_booking_id or str(booking.booking_id)
    status_value = str(getattr(booking.status, "value", booking.status))
    suffix = STATUS_SUFFIX.get(status_value)
    summary = f"{eq_label} - booking {ref}" + (f" ({suffix})" if suffix else "")
    manage_url = _frontend_bookings_url()
    deadline = _sample_deadline(booking)
    ics_status = "CONFIRMED" if status_value in CONFIRMED_STATUSES else "TENTATIVE"
    last_modified = _fmt_utc(booking.updated_at) if getattr(booking, "updated_at", None) else dtstamp

    description_lines = [
        f"Booking ID: {ref}",
        f"Status: {booking.get_status_display()}",
        f"Equipment: {eq_label}",
    ]
    if deadline:
        local_deadline = timezone.localtime(deadline)
        description_lines.append(f"Sample submission deadline: {local_deadline:%d %b %Y, %I:%M %p}")
    if booking.atmosphere_sensitive_sample:
        description_lines.append("Atmosphere-sensitive sample: bring the sample at slot start.")
    if manage_url:
        description_lines.append(f"Manage booking: {manage_url}")
    description = "\n".join(description_lines)

    events: list[list[str]] = []
    multi = len(segments) > 1
    for index, (start, end) in enumerate(segments, start=1):
        uid = f"booking-{booking.booking_id}-{index}@{UID_DOMAIN}"
        seg_summary = f"{summary} [part {index}/{len(segments)}]" if multi else summary
        lines = [
            "BEGIN:VEVENT",
            f"UID:{uid}",
            f"DTSTAMP:{dtstamp}",
            f"LAST-MODIFIED:{last_modified}",
            f"DTSTART:{_fmt_utc(start)}",
            f"DTEND:{_fmt_utc(end)}",
            f"SUMMARY:{_escape(seg_summary)}",
            f"DESCRIPTION:{_escape(description)}",
            f"STATUS:{ics_status}",
            "TRANSP:OPAQUE",
        ]
        if location:
            lines.append(f"LOCATION:{_escape(location)}")
        if manage_url:
            lines.append(f"URL:{manage_url}")
        if start > now:
            lines += [
                "BEGIN:VALARM",
                "ACTION:DISPLAY",
                f"DESCRIPTION:{_escape(seg_summary)}",
                "TRIGGER:-PT1H",
                "END:VALARM",
            ]
        lines.append("END:VEVENT")
        events.append(lines)

    first_start = segments[0][0]
    if deadline and status_value in DEADLINE_EVENT_STATUSES and now < deadline < first_start:
        lines = [
            "BEGIN:VEVENT",
            f"UID:sample-deadline-{booking.booking_id}@{UID_DOMAIN}",
            f"DTSTAMP:{dtstamp}",
            f"LAST-MODIFIED:{last_modified}",
            f"DTSTART:{_fmt_utc(deadline)}",
            f"DTEND:{_fmt_utc(deadline + timedelta(minutes=15))}",
            f"SUMMARY:{_escape(f'Submit sample: {eq_label} - booking {ref}')}",
            f"DESCRIPTION:{_escape(description)}",
            "STATUS:CONFIRMED",
            "TRANSP:TRANSPARENT",
        ]
        if location:
            lines.append(f"LOCATION:{_escape(location)}")
        if manage_url:
            lines.append(f"URL:{manage_url}")
        lines += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_escape(f'Sample submission deadline: {eq_label}')}",
            "TRIGGER:-PT24H",
            "END:VALARM",
            "END:VEVENT",
        ]
        events.append(lines)
    return events


def build_calendar(bookings, *, name: str = CALENDAR_NAME) -> str:
    now = timezone.now()
    dtstamp = _fmt_utc(now)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        f"X-WR-CALNAME:{_escape(name)}",
        "X-WR-CALDESC:Your IIC equipment bookings",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
    ]
    for booking in bookings:
        for event in _booking_events(booking, now, dtstamp):
            lines.extend(event)
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def feed_bookings_for_user(user):
    since = timezone.now() - timedelta(days=FEED_PAST_DAYS)
    ids = list(
        Booking.objects.filter(
            user=user,
            status__in=list(FEED_STATUSES),
            daily_slots__end_datetime__gte=since,
        )
        .order_by("-booking_id")
        .values_list("booking_id", flat=True)
        .distinct()[:FEED_MAX_BOOKINGS]
    )
    return (
        Booking.objects.filter(booking_id__in=ids)
        .select_related("equipment")
        .prefetch_related("daily_slots")
        .order_by("booking_id")
    )


def _ics_response(body: str, filename: str, *, attachment: bool) -> HttpResponse:
    response = HttpResponse(body, content_type="text/calendar; charset=utf-8")
    disposition = "attachment" if attachment else "inline"
    response["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    response["Cache-Control"] = "private, max-age=300"
    response["X-Robots-Tag"] = "noindex"
    return response


# --- Views ---------------------------------------------------------------------


def _settings_payload(request, token_obj: CalendarFeedToken) -> dict:
    path = reverse("api:calendar-feed", kwargs={"token": token_obj.token})
    feed_url = request.build_absolute_uri(path)
    webcal_url = "webcal://" + feed_url.split("://", 1)[-1]
    return {
        "eligible": True,
        "feed_url": feed_url,
        "webcal_url": webcal_url,
        # Google fetches cid=webcal:// links over plain http, which is not reachable from outside the
        # campus network; its "From URL" page keeps the https feed URL the user pastes.
        "google_url": GOOGLE_ADD_BY_URL,
        "outlook_url": (
            "https://outlook.live.com/calendar/0/addfromweb?url="
            + quote(feed_url, safe="")
            + "&name="
            + quote(CALENDAR_NAME, safe="")
        ),
        "created_at": token_obj.created_at.isoformat() if token_obj.created_at else None,
        "last_accessed_at": token_obj.last_accessed_at.isoformat() if token_obj.last_accessed_at else None,
    }


_NOT_ELIGIBLE = {
    "eligible": False,
    "error": "Calendar sync is available to internal and external booking users only.",
}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def calendar_sync_settings(request):
    if not is_calendar_sync_eligible(request.user):
        return Response(_NOT_ELIGIBLE, status=status.HTTP_403_FORBIDDEN)
    return Response(_settings_payload(request, get_or_create_feed_token(request.user)))


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def calendar_sync_regenerate(request):
    if not is_calendar_sync_eligible(request.user):
        return Response(_NOT_ELIGIBLE, status=status.HTTP_403_FORBIDDEN)
    return Response(_settings_payload(request, regenerate_feed_token(request.user)))


@require_GET
def calendar_feed(request, token: str):
    """Public subscription feed; the secret token in the URL is the only credential."""
    token_obj = (
        CalendarFeedToken.objects.select_related("user").filter(token=token).first() if token else None
    )
    if token_obj is None or not is_calendar_sync_eligible(token_obj.user):
        raise Http404("Calendar feed not found")
    now = timezone.now()
    if token_obj.last_accessed_at is None or now - token_obj.last_accessed_at > ACCESS_STAMP_INTERVAL:
        CalendarFeedToken.objects.filter(pk=token_obj.pk).update(last_accessed_at=now)
    body = build_calendar(feed_bookings_for_user(token_obj.user))
    return _ics_response(body, "iic-bookings.ics", attachment=False)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_calendar_ics(request, booking_id: int):
    if not is_calendar_sync_eligible(request.user):
        return Response(_NOT_ELIGIBLE, status=status.HTTP_403_FORBIDDEN)
    booking = (
        Booking.objects.select_related("equipment")
        .prefetch_related("daily_slots")
        .filter(booking_id=booking_id, user=request.user)
        .first()
    )
    if booking is None:
        return Response({"error": "Booking not found"}, status=status.HTTP_404_NOT_FOUND)
    if str(getattr(booking.status, "value", booking.status)) not in FEED_STATUSES or not _slot_segments(booking):
        return Response(
            {"error": "This booking has no scheduled slots to add to a calendar."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    ref = booking.virtual_booking_id or str(booking.booking_id)
    safe_ref = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(ref))
    body = build_calendar([booking], name=f"IIC booking {ref}")
    return _ics_response(body, f"iic-booking-{safe_ref}.ics", attachment=True)
