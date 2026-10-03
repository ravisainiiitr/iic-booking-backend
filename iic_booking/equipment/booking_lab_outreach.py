"""Reminders and questions from the lab to the booking user.

The equipment's Officer In-Charge, temporary OIC or Lab Operator (or the Main Admin) can send the booking
user a reminder ("please submit your sample", "your slot is tomorrow at 10:00") or ask a question that
needs a reply. Both are lab messages (``booking_lab_messages``): COMMENT booking events tagged
``metadata["lab_message"]`` = ``"staff_reminder"`` / ``"staff_question"``, so they show in the booking's
Message the lab thread and event history. A question stays open (``metadata["question_open"]``) until the
user or their supervisor replies to it from the booking page, or the staff mark it resolved.

The booking user is emailed (``booking_lab_staff_message_email``) and notified in-app. The supervisor is
not copied, as for other booking comments. Limits per booking in any rolling 24 hours:
``REMINDER_DAILY_LIMIT`` reminders and ``QUESTION_DAILY_LIMIT`` questions (all staff together). The same
text from the same sender within ``DUPLICATE_WINDOW_SECONDS``, or a repeated ``client_request_id``,
returns the message already saved instead of sending again.
"""

from __future__ import annotations

import html
import logging
from datetime import date, timedelta
from typing import Any, Optional

from django.db import transaction
from django.db.models import Count
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.communication.email_branding import (
    absolute_http_url,
    format_email_datetime,
    user_display_name,
)
from iic_booking.communication.service import CommunicationService
from iic_booking.communication.utils import booking_display_id_for_email, get_frontend_absolute_url
from iic_booking.users.models.user_type import UserType

from .booking_lab_messages import (
    _CLOSED_STATUSES,
    CLOSED_WINDOW_DAYS,
    COMPLETED_WINDOW_DAYS,
    KIND_STAFF_QUESTION,
    KIND_STAFF_REMINDER,
    LAB_MESSAGE_KEY,
    MAX_LENGTH,
    QUESTION_OPEN_KEY,
    _clean_text,
    _closed_at,
    _get_booking,
    can_reply,
    ensure_email_template,
    sender_role_label,
    serialize_message,
)
from .models import BookingEvent, BookingEventType, BookingStatus

logger = logging.getLogger(__name__)

EMAIL_TEMPLATE_CODE = "booking_lab_staff_message_email"

REMINDER_DAILY_LIMIT = 3
QUESTION_DAILY_LIMIT = 5
DUPLICATE_WINDOW_SECONDS = 120
MAX_REPLY_BY_DAYS = 60

KINDS = {
    "reminder": KIND_STAFF_REMINDER,
    "question": KIND_STAFF_QUESTION,
}
DAILY_LIMITS = {KIND_STAFF_REMINDER: REMINDER_DAILY_LIMIT, KIND_STAFF_QUESTION: QUESTION_DAILY_LIMIT}
HEADINGS = {
    KIND_STAFF_REMINDER: "Reminder from the lab",
    KIND_STAFF_QUESTION: "Question from the lab \u2013 reply needed",
}
KIND_PHRASES = {KIND_STAFF_REMINDER: "a reminder", KIND_STAFF_QUESTION: "a question"}

QUESTION_PRESETS: list[dict[str, str]] = [
    {"code": "non_magnetic", "label": "Non-magnetic?", "text": "Please confirm that your sample is non-magnetic."},
    {
        "code": "mode",
        "label": "Detector / mode",
        "text": "Which detector or measurement mode would you like us to use for your sample?",
    },
    {
        "code": "safety",
        "label": "Composition / safety",
        "text": "Please share the composition of your sample and any safety information (for example, an MSDS).",
    },
    {
        "code": "sample_count",
        "label": "Number of samples",
        "text": "Please confirm how many samples you will submit for this booking.",
    },
]
CUSTOM_PRESET = {"code": "custom", "label": "Custom message", "text": ""}

_OPEN_STATUSES = {
    BookingStatus.PENDING,
    BookingStatus.PENDING_PAYMENT,
    BookingStatus.BOOKED,
    BookingStatus.HOLD,
    BookingStatus.DISRUPTION_PENDING,
    BookingStatus.UNDER_MAINTENANCE,
    BookingStatus.OTHER_DISRUPTION,
    BookingStatus.PROCESSING,
}


# --- who is sending ---------------------------------------------------------------------------------------------


def staff_sender_identity(sender, booking) -> tuple[str, str]:
    """(name with the equipment's honorific, role) for a staff sender, e.g. ("Prof. A Kumar", "Officer In-Charge")."""
    from iic_booking.users.display import name_with_honorific

    from .models import EquipmentManager, EquipmentOperator, EquipmentTemporaryOIC

    default = user_display_name(sender, fallback="The lab")
    eq_id = booking.equipment_id
    manager = EquipmentManager.objects.filter(equipment_id=eq_id, manager_id=sender.pk).only("honorific").first()
    if manager is not None:
        return name_with_honorific(sender, manager.honorific, default=default), "Officer In-Charge"
    operator = EquipmentOperator.objects.filter(equipment_id=eq_id, operator_id=sender.pk).only("honorific").first()
    if operator is not None:
        return name_with_honorific(sender, operator.honorific, default=default), "Lab Operator"
    if EquipmentTemporaryOIC.objects.filter(
        equipment_id=eq_id, temporary_oic_id=sender.pk, resume_at__gt=timezone.now()
    ).exists():
        return default, "Officer In-Charge (temporary)"
    return default, sender_role_label(sender, booking)


# --- when it may be sent ----------------------------------------------------------------------------------------


def staff_send_policy(booking, now=None) -> tuple[bool, str]:
    """Same windows as user messages: open bookings, completed for 30 days, otherwise closed for 7 days."""
    now = now or timezone.now()
    if booking.status == BookingStatus.WAITLISTED:
        return False, "Reminders and questions can be sent once this waitlist request becomes a booking."
    if booking.status == BookingStatus.COMPLETED:
        window = COMPLETED_WINDOW_DAYS
    elif booking.status in _CLOSED_STATUSES:
        window = CLOSED_WINDOW_DAYS
    else:
        return True, ""
    closed_at = _closed_at(booking)
    if closed_at and now - closed_at > timedelta(days=window):
        return False, (
            f"Messaging is closed for this booking ({booking.get_status_display().lower()} more than "
            f"{window} days ago)."
        )
    return True, ""


def _sends_last_24h(booking, now=None) -> dict[str, int]:
    since = (now or timezone.now()) - timedelta(hours=24)
    counts = {KIND_STAFF_REMINDER: 0, KIND_STAFF_QUESTION: 0}
    for md in BookingEvent.objects.filter(
        booking=booking,
        event_type=BookingEventType.COMMENT,
        created_at__gte=since,
        metadata__has_key=LAB_MESSAGE_KEY,
    ).values_list("metadata", flat=True):
        kind = (md or {}).get(LAB_MESSAGE_KEY)
        if kind in counts:
            counts[kind] += 1
    return counts


# --- presets ----------------------------------------------------------------------------------------------------


def _local(dt):
    return timezone.localtime(dt) if dt and timezone.is_aware(dt) else dt


def _date_text(dt) -> str:
    dt = _local(dt)
    return f"{dt.day} {dt.strftime('%b %Y')}" if dt else ""


def _time_text(dt) -> str:
    dt = _local(dt)
    return dt.strftime("%I:%M %p").lstrip("0") if dt else ""


def _uses_slot_ids(booking) -> bool:
    return getattr(booking.equipment, "weekly_view_display", None) == "SLOT_ID"


def _slot_phrase(booking, start) -> str:
    """"on 5 Oct 2026 at 10:00 AM" (date only for equipment that hides slot times from users)."""
    if not start:
        return ""
    if _uses_slot_ids(booking):
        return f"on {_date_text(start)}"
    return f"on {_date_text(start)} at {_time_text(start)}"


def reminder_presets(booking, now=None) -> list[dict[str, str]]:
    """Reminder texts that fit the booking's current state, then a custom message."""
    from .models import BookingSampleTrace, SampleTraceStatus
    from .sample_lifecycle_policy import equipment_is_walk_in_sample
    from .sample_submission_deadline_reminders import compute_sample_submission_deadline
    from .serializers import compute_sample_collection_deadline

    now = now or timezone.now()
    ref = booking_display_id_for_email(booking)
    equipment_name = booking.equipment.name if booking.equipment else "the equipment"
    first_slot = booking.daily_slots.order_by("start_datetime").only("start_datetime").first()
    start = first_slot.start_datetime if first_slot else None
    trace = set(BookingSampleTrace.objects.filter(booking=booking).values_list("status", flat=True))
    walk_in = equipment_is_walk_in_sample(booking.equipment)
    analysed = booking.status == BookingStatus.COMPLETED or SampleTraceStatus.COMPLETED in trace
    sample_back = bool(trace & {SampleTraceStatus.RETURNED, SampleTraceStatus.DISPOSED, SampleTraceStatus.ARCHIVED})
    is_open = booking.status in _OPEN_STATUSES
    presets: list[dict[str, str]] = []

    if booking.status == BookingStatus.PENDING_PAYMENT:
        presets.append({
            "code": "payment_pending",
            "label": "Complete payment",
            "text": f"Please complete the payment for booking {ref} so that your slot on {equipment_name} is confirmed.",
        })
    if is_open and start and start > now:
        bring = " with your sample" if walk_in else ""
        presets.append({
            "code": "slot_upcoming",
            "label": "Upcoming slot",
            "text": (
                f"This is a reminder that your slot on {equipment_name} is {_slot_phrase(booking, start)}. "
                f"Please reach the lab on time{bring}."
            ),
        })
    if is_open and not walk_in and not analysed and not trace:
        deadline = compute_sample_submission_deadline(booking)
        if deadline and deadline > now:
            by = f" by {_date_text(deadline)}, {_time_text(deadline)}"
        elif start and start > now:
            by = f" before your slot {_slot_phrase(booking, start)}"
        else:
            by = ""
        presets.append({
            "code": "sample_pending",
            "label": "Submit sample",
            "text": (
                f"Please submit your sample for booking {ref} to the lab{by}. "
                "If you have already handed it over, please mark it as submitted on the booking page."
            ),
        })
    if (
        booking.status in (BookingStatus.PENDING, BookingStatus.BOOKED, BookingStatus.PROCESSING)
        and start
        and start <= now
        and not analysed
    ):
        presets.append({
            "code": "results_delayed",
            "label": "Results delayed",
            "text": (
                f"The results for booking {ref} on {equipment_name} are taking longer than expected. "
                "The lab is working on it and will update you as soon as they are ready. "
                "We apologise for the delay."
            ),
        })
    if analysed and not sample_back and booking.status not in _CLOSED_STATUSES:
        deadline, _hours = compute_sample_collection_deadline(booking)
        by = f" by {_date_text(deadline)}, {_time_text(deadline)}" if deadline and deadline > now else ""
        what = "your results" if walk_in else "your sample and results"
        presets.append({
            "code": "collect_results",
            "label": "Collect sample / results",
            "text": f"The analysis for booking {ref} is complete. Please collect {what} from the lab{by}.",
        })
    if not presets:
        presets.append({
            "code": "general",
            "label": "General reminder",
            "text": f"This is a reminder about your booking {ref} on {equipment_name}. Please check the booking details in the portal.",
        })
    return presets + [dict(CUSTOM_PRESET)]


def _preset_codes(kind: str, booking) -> set[str]:
    if kind == KIND_STAFF_QUESTION:
        return {p["code"] for p in QUESTION_PRESETS} | {CUSTOM_PRESET["code"]}
    return {p["code"] for p in reminder_presets(booking)}


# --- email context ----------------------------------------------------------------------------------------------


def _user_slot_context(booking) -> dict[str, str]:
    """Start/end (or date and slot names for equipment that hides slot times from users)."""
    slots = list(booking.daily_slots.select_related("slot_master").order_by("start_datetime"))
    empty = {"start_time": "", "end_time": "", "booking_date": "", "slot_id_display": ""}
    if not slots:
        from .models import BookingSlotRange

        released = BookingSlotRange.objects.filter(booking_id=booking.pk).first()
        if released is None or not released.start_datetime:
            return empty
        if _uses_slot_ids(booking):
            return {**empty, "booking_date": format_email_datetime(released.start_datetime).split(",")[0]}
        return {
            **empty,
            "start_time": format_email_datetime(released.start_datetime),
            "end_time": format_email_datetime(released.end_datetime),
        }
    if _uses_slot_ids(booking):
        names = [
            ((ds.slot_master.slot_name or "").strip() or f"Slot {ds.slot_master.slot_number}") if ds.slot_master else "\u2014"
            for ds in slots
        ]
        return {
            **empty,
            "booking_date": format_email_datetime(slots[0].start_datetime).split(",")[0],
            "slot_id_display": ", ".join(names),
        }
    return {
        **empty,
        "start_time": format_email_datetime(slots[0].start_datetime),
        "end_time": format_email_datetime(slots[-1].end_datetime),
    }


def _user_link(booking) -> str:
    path = f"/my-bookings?booking={booking_display_id_for_email(booking)}"
    return absolute_http_url(get_frontend_absolute_url(path) or path)


def _reply_by_text(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        d = date.fromisoformat(str(value))
    except ValueError:
        return ""
    return f"{d.day} {d.strftime('%b %Y')}"


def _email_context(booking, kind: str, sender, text: str, *, reply_by: Optional[str], sent_at) -> dict[str, Any]:
    from .booking_events import booking_party_context

    sender_name, sender_role = staff_sender_identity(sender, booking) if sender else ("The lab", "Lab")
    display_ref = booking_display_id_for_email(booking)
    equipment = booking.equipment
    return {
        "user_name": user_display_name(booking.user),
        "user_email": getattr(booking.user, "email", "") or "",
        "booking_id": display_ref,
        "virtual_booking_id": display_ref,
        "equipment_name": equipment.name if equipment else "",
        "equipment_code": equipment.code if equipment else "",
        "message_heading": HEADINGS[kind],
        "message_kind_phrase": KIND_PHRASES[kind],
        "reply_needed": "yes" if kind == KIND_STAFF_QUESTION else "",
        "sender_name": sender_name,
        "sender_display": f"{sender_name}, {sender_role}",
        "reply_by": _reply_by_text(reply_by) if kind == KIND_STAFF_QUESTION else "",
        "lab_message": text,
        "lab_message_html": html.escape(text),
        "sent_at": format_email_datetime(sent_at) if sent_at else "",
        "link": _user_link(booking),
        **_user_slot_context(booking),
        **booking_party_context(booking.user, equipment),
    }


def email_subject_preview(booking, kind: str) -> str:
    from iic_booking.communication.models import CommunicationTemplate

    template = CommunicationTemplate.objects.filter(
        code=EMAIL_TEMPLATE_CODE, communication_type=CommunicationTemplate.CommunicationType.EMAIL
    ).first()
    if template is None:
        from iic_booking.communication.default_email_templates import get_default_email_templates

        spec = next(t for t in get_default_email_templates() if t["code"] == EMAIL_TEMPLATE_CODE)
        template = CommunicationTemplate(
            code=EMAIL_TEMPLATE_CODE,
            communication_type=CommunicationTemplate.CommunicationType.EMAIL,
            subject=spec["subject"],
        )
    context = {
        "booking_id": booking_display_id_for_email(booking),
        "equipment_name": booking.equipment.name if booking.equipment else "",
        "equipment_code": booking.equipment.code if booking.equipment else "",
        "message_heading": HEADINGS[kind],
        "user_name": user_display_name(booking.user),
    }
    subject_only = CommunicationTemplate(
        code=template.code, communication_type=template.communication_type, subject=template.subject
    )
    return CommunicationService.render_template(subject_only, context=context).get("subject", "")


# --- thread payload ---------------------------------------------------------------------------------------------


def outreach_payload(booking, user, now=None) -> Optional[dict[str, Any]]:
    """What the staff need to send a reminder or question; None for anyone who may not send."""
    if not can_reply(user, booking):
        return None
    allowed, reason = staff_send_policy(booking, now)
    sent = _sends_last_24h(booking, now)
    recipient = booking.user
    return {
        "can_send": allowed,
        "closed_reason": "" if allowed else reason,
        "recipient_name": user_display_name(recipient),
        "recipient_has_email": bool((getattr(recipient, "email", "") or "").strip()),
        "max_length": MAX_LENGTH,
        "max_reply_by_days": MAX_REPLY_BY_DAYS,
        "reminder": {
            "presets": reminder_presets(booking, now),
            "daily_limit": REMINDER_DAILY_LIMIT,
            "remaining_today": max(0, REMINDER_DAILY_LIMIT - sent[KIND_STAFF_REMINDER]),
            "email_subject": email_subject_preview(booking, KIND_STAFF_REMINDER),
        },
        "question": {
            "presets": [dict(p) for p in QUESTION_PRESETS] + [dict(CUSTOM_PRESET)],
            "daily_limit": QUESTION_DAILY_LIMIT,
            "remaining_today": max(0, QUESTION_DAILY_LIMIT - sent[KIND_STAFF_QUESTION]),
            "email_subject": email_subject_preview(booking, KIND_STAFF_QUESTION),
        },
    }


# --- endpoints --------------------------------------------------------------------------------------------------


def _parse_reply_by(raw) -> tuple[Optional[str], Optional[str]]:
    """(iso date or None, error)."""
    text = str(raw or "").strip()
    if not text:
        return None, None
    try:
        value = date.fromisoformat(text[:10])
    except ValueError:
        return None, "Please choose a valid reply-by date."
    today = timezone.localdate()
    if value < today:
        return None, "The reply-by date cannot be in the past."
    if value > today + timedelta(days=MAX_REPLY_BY_DAYS):
        return None, f"Please choose a reply-by date within {MAX_REPLY_BY_DAYS} days."
    return value.isoformat(), None


def _find_duplicate(booking, user, kind: str, text: str, client_request_id: str, now):
    recent = BookingEvent.objects.filter(
        booking=booking,
        event_type=BookingEventType.COMMENT,
        created_by=user,
        metadata__has_key=LAB_MESSAGE_KEY,
    ).order_by("-created_at")
    since = now - timedelta(seconds=DUPLICATE_WINDOW_SECONDS)
    for event in recent[:50]:
        md = event.metadata or {}
        if md.get(LAB_MESSAGE_KEY) != kind:
            continue
        if client_request_id and md.get("client_request_id") == client_request_id:
            return event
        if event.created_at and event.created_at >= since and (event.comment or "").strip() == text:
            return event
    return None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_lab_outreach_send(request, booking_id, kind):
    """Send the booking user a reminder (kind="reminder") or a question (kind="question").

    POST body: {"message": "...", "preset": "<optional preset code>", "reply_by": "YYYY-MM-DD" (question only),
    "client_request_id": "<optional id, the same for retries of one send>"}
    """
    event_kind = KINDS.get(kind)
    if event_kind is None:
        return Response({"error": "Unknown message type."}, status=status.HTTP_404_NOT_FOUND)
    booking = _get_booking(booking_id)
    if booking is None:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    user = request.user
    if not can_reply(user, booking):
        return Response(
            {"error": "Only the Officer In-Charge or Lab Operator of this equipment can send reminders and questions."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if booking.user_id == user.pk:
        return Response({"error": "This is your own booking."}, status=status.HTTP_400_BAD_REQUEST)
    allowed, closed_reason = staff_send_policy(booking)
    if not allowed:
        return Response({"error": closed_reason}, status=status.HTTP_400_BAD_REQUEST)

    text = _clean_text(request.data.get("message"))
    noun = "reminder" if event_kind == KIND_STAFF_REMINDER else "question"
    if not text:
        return Response({"error": f"Please write the {noun}."}, status=status.HTTP_400_BAD_REQUEST)
    if len(text) > MAX_LENGTH:
        return Response(
            {"error": f"Please keep the {noun} within {MAX_LENGTH} characters."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    preset = str(request.data.get("preset") or "").strip()
    if preset and preset not in _preset_codes(event_kind, booking):
        preset = CUSTOM_PRESET["code"]
    reply_by = None
    if event_kind == KIND_STAFF_QUESTION:
        reply_by, error = _parse_reply_by(request.data.get("reply_by"))
        if error:
            return Response({"error": error}, status=status.HTTP_400_BAD_REQUEST)
    client_request_id = str(request.data.get("client_request_id") or "").strip()[:64]

    from .booking_events import create_booking_event
    from .models import Booking

    limit = DAILY_LIMITS[event_kind]
    with transaction.atomic():
        Booking.objects.select_for_update().filter(pk=booking.pk).first()
        now = timezone.now()
        duplicate = _find_duplicate(booking, user, event_kind, text, client_request_id, now)
        if duplicate is not None:
            sent = _sends_last_24h(booking, now)[event_kind]
            return Response(
                {
                    "message": serialize_message(duplicate, booking, user),
                    "duplicate": True,
                    "remaining_today": max(0, limit - sent),
                    "warnings": [f"This {noun} was already sent; it was not sent again."],
                },
                status=status.HTTP_200_OK,
            )
        sent = _sends_last_24h(booking, now)[event_kind]
        if sent >= limit:
            plural = "reminders" if event_kind == KIND_STAFF_REMINDER else "questions"
            return Response(
                {
                    "error": (
                        f"Up to {limit} {plural} can be sent for a booking in 24 hours. "
                        "Please try again later or contact the user directly."
                    )
                },
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        metadata: dict[str, Any] = {
            LAB_MESSAGE_KEY: event_kind,
            "lab_message_preset": preset,
            "lab_message_reason": HEADINGS[event_kind],
            "sender_user_id": user.pk,
            "comment_recipients": {"user": True, "oic": False, "lab_incharge": False},
        }
        if client_request_id:
            metadata["client_request_id"] = client_request_id
        if event_kind == KIND_STAFF_QUESTION:
            metadata[QUESTION_OPEN_KEY] = True
            if reply_by:
                metadata["reply_by"] = reply_by
        event = create_booking_event(
            booking=booking,
            event_type=BookingEventType.COMMENT,
            created_by=user,
            comment=text,
            metadata=metadata,
            send_notification=True,
        )
    logger.info(
        "Lab %s sent booking_id=%s event_id=%s sender_id=%s preset=%s",
        noun,
        booking.booking_id,
        event.event_id,
        user.pk,
        preset or "-",
    )
    warnings = []
    if not (getattr(booking.user, "email", "") or "").strip():
        warnings.append("The user has no email address; they will see it in the portal and as a notification.")
    return Response(
        {
            "message": serialize_message(event, booking, user),
            "duplicate": False,
            "remaining_today": max(0, limit - sent - 1),
            "warnings": warnings,
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_lab_question_resolve(request, booking_id, event_id):
    """Staff close an open question without a portal reply (e.g. the user answered by phone)."""
    booking = _get_booking(booking_id)
    if booking is None:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    user = request.user
    if not can_reply(user, booking):
        return Response(
            {"error": "Only the Officer In-Charge or Lab Operator of this equipment can resolve questions."},
            status=status.HTTP_403_FORBIDDEN,
        )
    with transaction.atomic():
        question = (
            BookingEvent.objects.select_for_update()
            .filter(booking=booking, event_id=event_id, event_type=BookingEventType.COMMENT)
            .first()
        )
        if question is None or (question.metadata or {}).get(LAB_MESSAGE_KEY) != KIND_STAFF_QUESTION:
            return Response({"error": "Question not found."}, status=status.HTTP_404_NOT_FOUND)
        md = dict(question.metadata or {})
        if md.get(QUESTION_OPEN_KEY):
            md.update({QUESTION_OPEN_KEY: False, "resolved_at": timezone.now().isoformat(), "resolved_by_user_id": user.pk})
            question.metadata = md
            question.save(update_fields=["metadata"])
            logger.info(
                "Lab question resolved booking_id=%s event_id=%s by user_id=%s", booking.booking_id, event_id, user.pk
            )
    question = BookingEvent.objects.select_related("created_by").get(event_id=question.event_id)
    return Response({"message": serialize_message(question, booking, user)})


def _staff_equipment_ids(user) -> Optional[set[int]]:
    """Equipment whose questions the user follows; None means all (Main Admin)."""
    user_type = getattr(user, "user_type", None)
    if user_type == UserType.ADMIN:
        return None
    ids: set[int] = set()
    if user_type == UserType.MANAGER:
        from .reports import get_equipment_ids_managed_by_oic

        ids |= set(get_equipment_ids_managed_by_oic(user.pk))
    if user_type == UserType.OPERATOR:
        from .models import EquipmentOperator

        ids |= set(EquipmentOperator.objects.filter(operator_id=user.pk).values_list("equipment_id", flat=True))
    return ids


def _open_question_queryset():
    return BookingEvent.objects.filter(
        event_type=BookingEventType.COMMENT,
        metadata__lab_message=KIND_STAFF_QUESTION,
        metadata__question_open=True,
    )


def open_question_counts(booking_ids) -> dict[int, int]:
    ids = [i for i in booking_ids if i]
    if not ids:
        return {}
    rows = (
        _open_question_queryset()
        .filter(booking_id__in=ids)
        .values("booking_id")
        .annotate(n=Count("event_id"))
    )
    return {row["booking_id"]: row["n"] for row in rows}


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def lab_questions_awaiting(request):
    """Open questions on bookings of the equipment the OIC / temporary OIC / Lab Operator looks after."""
    user = request.user
    if getattr(user, "user_type", None) not in (UserType.ADMIN, UserType.MANAGER, UserType.OPERATOR):
        return Response({"error": "Not available for your account."}, status=status.HTTP_403_FORBIDDEN)
    equipment_ids = _staff_equipment_ids(user)
    qs = _open_question_queryset()
    if equipment_ids is not None:
        if not equipment_ids:
            return Response({"count": 0, "overdue": 0, "items": []})
        qs = qs.filter(booking__equipment_id__in=equipment_ids)
    qs = qs.select_related("booking", "booking__equipment", "booking__user", "created_by").order_by("created_at")
    today = timezone.localdate().isoformat()
    total = qs.count()
    items = []
    overdue = 0
    for event in qs[:50]:
        md = event.metadata or {}
        booking = event.booking
        reply_by = md.get("reply_by") or None
        is_overdue = bool(reply_by and reply_by < today)
        overdue += 1 if is_overdue else 0
        question = (event.comment or "").strip()
        items.append(
            {
                "event_id": event.event_id,
                "booking_id": booking.booking_id,
                "booking_ref": booking_display_id_for_email(booking),
                "equipment_name": booking.equipment.name if booking.equipment else "",
                "user_name": user_display_name(booking.user),
                "question": question if len(question) <= 160 else question[:157] + "...",
                "asked_by": user_display_name(event.created_by, fallback="Lab") if event.created_by else "Lab",
                "asked_by_me": bool(event.created_by_id and event.created_by_id == user.pk),
                "asked_at": event.created_at.isoformat() if event.created_at else None,
                "reply_by": reply_by,
                "overdue": is_overdue,
            }
        )
    return Response({"count": total, "overdue": overdue, "items": items})


# --- notifications ----------------------------------------------------------------------------------------------


def is_staff_outreach(event) -> bool:
    return (
        event is not None
        and event.event_type == BookingEventType.COMMENT
        and (event.metadata or {}).get(LAB_MESSAGE_KEY) in (KIND_STAFF_REMINDER, KIND_STAFF_QUESTION)
    )


def send_staff_outreach_notifications(event) -> None:
    """Email + in-app the reminder or question to the booking user. Failures are logged, never raised."""
    booking = event.booking
    recipient = booking.user
    md = event.metadata or {}
    kind = md.get(LAB_MESSAGE_KEY)
    sender = event.created_by
    text = (event.comment or "").strip()
    display_ref = booking_display_id_for_email(booking)
    try:
        context = _email_context(booking, kind, sender, text, reply_by=md.get("reply_by"), sent_at=event.created_at)
    except Exception:
        logger.exception("Failed to build lab outreach context event_id=%s", event.event_id)
        return
    meta = {
        "booking_id": display_ref,
        "real_booking_id": booking.booking_id,
        "event_id": event.event_id,
        "event_type": event.event_type,
        LAB_MESSAGE_KEY: kind,
        "link": context["link"],
    }
    if (recipient.email or "").strip():
        try:
            ensure_email_template(EMAIL_TEMPLATE_CODE)
            CommunicationService.send_email(
                recipient=recipient,
                template=EMAIL_TEMPLATE_CODE,
                template_context=context,
                metadata=meta,
                created_by=sender,
            )
        except Exception:
            logger.exception("Failed to email lab %s to user_id=%s event_id=%s", kind, recipient.id, event.event_id)
    equipment_name = booking.equipment.name if booking.equipment else ""
    preview = text if len(text) <= 200 else text[:197] + "..."
    if kind == KIND_STAFF_QUESTION:
        title = f"Question from the lab \u2014 reply needed ({display_ref})"
        due = f" Please reply by {context['reply_by']}." if context.get("reply_by") else ""
        message = f"{equipment_name}: {context['sender_name']} asks: {preview}{due}"
    else:
        title = f"Reminder from the lab \u2014 {display_ref}"
        message = f"{equipment_name}: {context['sender_name']}: {preview}"
    try:
        CommunicationService.send_push_notification(
            recipient=recipient,
            title=title,
            message=message,
            metadata={
                **meta,
                "notification_type": "warning" if kind == KIND_STAFF_QUESTION else "info",
                "event": "booking.lab_question" if kind == KIND_STAFF_QUESTION else "booking.lab_reminder",
            },
            created_by=sender,
        )
    except Exception:
        logger.exception("Failed to send in-app lab %s to user_id=%s event_id=%s", kind, recipient.id, event.event_id)
