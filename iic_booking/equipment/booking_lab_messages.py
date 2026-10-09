"""Message the lab: booking users write to the Lab Operator(s) and Officer In-Charge of the booked equipment.

Messages are stored as ``BookingEvent`` COMMENT rows tagged with ``metadata["lab_message"]``:
``"user"`` for a message from the booking user (or the supervisor whose wallet pays for it),
``"staff_reply"`` for a reply from the equipment's staff, and ``"staff_reminder"`` / ``"staff_question"``
for a reminder or question the staff send to the user (see ``booking_lab_outreach``). A user message
with ``metadata["in_reply_to"]`` answers a staff question. All appear in the booking's event history too.

Who may send a message: the booking user, or the faculty supervisor with an approved wallet link to
that student. Staff accounts (admin, OIC, Lab Operator, department administrator, accounts, external
relations) cannot use the user-side endpoint; OIC / Lab Operator / admin reply via the reply endpoint.

When a message may be sent (``lab_message_policy``):
- open bookings (pending, awaiting payment, booked, hold, disruption, processing): always;
- completed bookings: for 30 days after completion;
- cancelled, refunded, operator-unavailable and not-utilized bookings: for 7 days after closing;
- waitlist entries: never (there is no confirmed slot yet).
At most ``DAILY_LIMIT`` user messages per booking in any rolling 24 hours.
"""

from __future__ import annotations

import html
import logging
from datetime import timedelta
from typing import Any, Optional

from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from iic_booking.communication.email_branding import (
    absolute_http_url,
    format_email_datetime,
    format_email_end_datetime,
    user_display_name,
)
from iic_booking.communication.service import CommunicationService
from iic_booking.communication.utils import booking_display_id_for_email, get_frontend_absolute_url
from iic_booking.users.models.user_type import UserType

from .models import Booking, BookingEvent, BookingEventType, BookingStatus

logger = logging.getLogger(__name__)

LAB_MESSAGE_KEY = "lab_message"
KIND_USER = "user"
KIND_STAFF_REPLY = "staff_reply"
KIND_STAFF_REMINDER = "staff_reminder"
KIND_STAFF_QUESTION = "staff_question"
QUESTION_OPEN_KEY = "question_open"
IN_REPLY_TO_KEY = "in_reply_to"
REPLY_REASON_LABEL = "Reply to the lab's question"
EMAIL_TEMPLATE_CODE = "booking_lab_message_email"

MAX_LENGTH = 1000
DAILY_LIMIT = 10
COMPLETED_WINDOW_DAYS = 30
CLOSED_WINDOW_DAYS = 7

REASONS: list[tuple[str, str]] = [
    ("sample_delayed", "Sample submission delayed"),
    ("unable_to_attend", "Unable to attend / reach on time"),
    ("sample_details_changed", "Change in sample details"),
    ("results_query", "Query about results"),
    ("other", "Other"),
]
_REASON_LABELS = dict(REASONS)

_CLOSED_STATUSES = {
    BookingStatus.CANCELLED,
    BookingStatus.REFUNDED,
    BookingStatus.ABSENT,
    BookingStatus.BOOKING_NOT_UTILIZED,
}

_STAFF_ROLE_LABELS = {
    UserType.ADMIN: "Admin",
    UserType.MANAGER: "Officer In-Charge",
    UserType.OPERATOR: "Lab Operator",
    UserType.DEPT_ADMIN: "Department Administrator",
    UserType.FINANCE: "Accounts",
    UserType.EXTERNAL_RELATIONS: "External Relations",
}


def is_staff_account(user) -> bool:
    return getattr(user, "user_type", None) in UserType.get_admin_panel_codes()


def is_booking_supervisor(user, booking) -> bool:
    """Faculty whose wallet the booking user has joined (approved)."""
    if user is None or not getattr(user, "pk", None) or user.pk == booking.user_id:
        return False
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

    return WalletJoinRequest.objects.filter(
        faculty_id=user.pk, student_id=booking.user_id, status=WalletJoinRequestStatus.APPROVED
    ).exists()


def equipment_staff(booking) -> list:
    """Active Officer In-Charge (incl. temporary OIC) and Lab Operator users of the booked equipment."""
    from .reports import get_equipment_lab_incharge_users, get_equipment_oic_users

    seen: set[int] = set()
    out: list = []
    for role, users in (
        ("Lab Operator", get_equipment_lab_incharge_users(booking.equipment)),
        ("Officer In-Charge", get_equipment_oic_users(booking.equipment)),
    ):
        for staff in users:
            if staff.id in seen:
                continue
            seen.add(staff.id)
            out.append((staff, role))
    return out


def can_reply(user, booking) -> bool:
    if getattr(user, "user_type", None) == UserType.ADMIN:
        return True
    if getattr(user, "user_type", None) not in (UserType.MANAGER, UserType.OPERATOR):
        return False
    return any(staff.id == user.id for staff, _role in equipment_staff(booking))


def _can_view(user, booking) -> bool:
    if booking.user_id == user.pk or is_booking_supervisor(user, booking):
        return True
    if can_reply(user, booking):
        return True
    from .api_views import check_operator_permission

    return check_operator_permission(user) or getattr(user, "user_type", None) == UserType.FINANCE


def _closed_at(booking):
    if booking.status == BookingStatus.COMPLETED and booking.completed_at:
        return booking.completed_at
    event = (
        BookingEvent.objects.filter(booking=booking, new_status=booking.status)
        .order_by("-created_at")
        .only("created_at")
        .first()
    )
    if event is None and booking.status in BookingEventType.values:
        event = (
            BookingEvent.objects.filter(booking=booking, event_type=booking.status)
            .order_by("-created_at")
            .only("created_at")
            .first()
        )
    return event.created_at if event else booking.updated_at


def lab_message_policy(booking, now=None) -> tuple[bool, str]:
    """(allowed, reason shown to the user when not allowed)."""
    now = now or timezone.now()
    if booking.status == BookingStatus.WAITLISTED:
        return False, "Messages can be sent once your waitlist request is confirmed as a booking."
    if booking.status == BookingStatus.COMPLETED:
        window = COMPLETED_WINDOW_DAYS
    elif booking.status in _CLOSED_STATUSES:
        window = CLOSED_WINDOW_DAYS
    else:
        return True, ""
    closed_at = _closed_at(booking)
    if closed_at and now - closed_at > timedelta(days=window):
        return False, (
            f"Messages are closed for this booking ({booking.get_status_display().lower()} more than "
            f"{window} days ago). Please raise a support ticket if you still need help."
        )
    return True, ""


def _lab_messages(booking):
    return (
        BookingEvent.objects.filter(
            booking=booking,
            event_type=BookingEventType.COMMENT,
            metadata__has_key=LAB_MESSAGE_KEY,
        )
        .select_related("created_by")
        .order_by("created_at", "event_id")
    )


def _messages_sent_last_24h(booking, now=None) -> int:
    since = (now or timezone.now()) - timedelta(hours=24)
    return sum(
        1
        for md in BookingEvent.objects.filter(
            booking=booking,
            event_type=BookingEventType.COMMENT,
            created_at__gte=since,
            metadata__has_key=LAB_MESSAGE_KEY,
        ).values_list("metadata", flat=True)
        if (md or {}).get(LAB_MESSAGE_KEY) == KIND_USER
    )


def sender_role_label(sender, booking) -> str:
    if sender is None:
        return "Portal"
    if sender.pk == booking.user_id:
        return "Booking user"
    role = _STAFF_ROLE_LABELS.get(getattr(sender, "user_type", None))
    if role:
        return role
    return "Supervisor"


def serialize_message(event, booking, viewer, name_cache: Optional[dict] = None) -> dict[str, Any]:
    md = event.metadata or {}
    sender = event.created_by
    kind = md.get(LAB_MESSAGE_KEY)
    if sender and kind in (KIND_STAFF_REMINDER, KIND_STAFF_QUESTION, KIND_STAFF_REPLY):
        from .booking_lab_outreach import staff_sender_identity

        cache = name_cache if name_cache is not None else {}
        if sender.pk not in cache:
            cache[sender.pk] = staff_sender_identity(sender, booking)
        sender_name, sender_role = cache[sender.pk]
    else:
        sender_name = user_display_name(sender, fallback="Portal") if sender else "Portal"
        sender_role = sender_role_label(sender, booking)
    return {
        "id": event.event_id,
        "kind": kind,
        "reason": md.get("lab_message_reason") or "",
        "message": event.comment or "",
        "sender_name": sender_name,
        "sender_role": sender_role,
        "is_mine": bool(sender and viewer and sender.pk == viewer.pk),
        "created_at": event.created_at.isoformat() if event.created_at else None,
        "preset": md.get("lab_message_preset") or "",
        "reply_by": md.get("reply_by") or None,
        "question_open": bool(md.get(QUESTION_OPEN_KEY)) if kind == KIND_STAFF_QUESTION else False,
        "answered_at": md.get("answered_at") or None,
        "resolved_at": md.get("resolved_at") or None,
        "in_reply_to": md.get(IN_REPLY_TO_KEY) or None,
    }


def _get_booking(booking_id) -> Optional[Booking]:
    return Booking.objects.select_related("equipment", "user").filter(booking_id=booking_id).first()


def _thread_payload(booking, user) -> dict[str, Any]:
    if is_staff_account(user):
        viewer = "staff"
    elif booking.user_id == user.pk:
        viewer = "booking_user"
    elif is_booking_supervisor(user, booking):
        viewer = "supervisor"
    else:
        viewer = "other"
    allowed, closed_reason = lab_message_policy(booking)
    sent_today = _messages_sent_last_24h(booking)
    staff = can_reply(user, booking)
    events = list(_lab_messages(booking))
    open_count = sum(
        1
        for e in events
        if (e.metadata or {}).get(LAB_MESSAGE_KEY) == KIND_STAFF_QUESTION and (e.metadata or {}).get(QUESTION_OPEN_KEY)
    )
    is_user_side = viewer in ("booking_user", "supervisor")
    names: dict = {}
    outreach = None
    if staff:
        from .booking_lab_outreach import outreach_payload

        outreach = outreach_payload(booking, user)
    return {
        "booking_id": booking.booking_id,
        "viewer": viewer,
        "can_post": allowed and is_user_side,
        "can_answer": is_user_side and open_count > 0 and booking.status != BookingStatus.WAITLISTED,
        "can_reply": staff,
        "closed_reason": "" if allowed else closed_reason,
        "reasons": [{"code": code, "label": label} for code, label in REASONS],
        "max_length": MAX_LENGTH,
        "daily_limit": DAILY_LIMIT,
        "remaining_today": max(0, DAILY_LIMIT - sent_today),
        "has_lab_staff": bool(equipment_staff(booking)),
        "open_question_count": open_count,
        "outreach": outreach,
        "messages": [serialize_message(e, booking, user, names) for e in events],
    }


def _clean_text(raw) -> str:
    return str(raw or "").replace("\r\n", "\n").strip()


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def booking_lab_messages(request, booking_id):
    """GET the Message-the-lab thread; POST a new message as the booking user (or their supervisor).

    POST body: {"message": "...", "reason": "<optional reason code>"}
    """
    booking = _get_booking(booking_id)
    if booking is None:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    user = request.user

    if request.method == "GET":
        if not _can_view(user, booking):
            return Response(
                {"error": "You don't have permission to view messages for this booking."},
                status=status.HTTP_403_FORBIDDEN,
            )
        return Response(_thread_payload(booking, user))

    if is_staff_account(user):
        return Response(
            {"error": "Staff accounts cannot send booking-user messages. Use Reply instead."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if booking.user_id != user.pk and not is_booking_supervisor(user, booking):
        return Response(
            {"error": "Only the booking user or their supervisor can message the lab about this booking."},
            status=status.HTTP_403_FORBIDDEN,
        )
    question = None
    raw_reply_to = request.data.get(IN_REPLY_TO_KEY)
    if raw_reply_to not in (None, ""):
        try:
            question_id = int(raw_reply_to)
        except (TypeError, ValueError):
            question_id = None
        question = (
            BookingEvent.objects.filter(
                booking=booking, event_id=question_id, event_type=BookingEventType.COMMENT
            ).first()
            if question_id
            else None
        )
        if question is None or (question.metadata or {}).get(LAB_MESSAGE_KEY) != KIND_STAFF_QUESTION:
            return Response(
                {"error": "That question was not found on this booking."}, status=status.HTTP_400_BAD_REQUEST
            )
    allowed, closed_reason = lab_message_policy(booking)
    answering_open_question = bool(
        question is not None
        and (question.metadata or {}).get(QUESTION_OPEN_KEY)
        and booking.status != BookingStatus.WAITLISTED
    )
    if not allowed and not answering_open_question:
        return Response({"error": closed_reason}, status=status.HTTP_400_BAD_REQUEST)

    text = _clean_text(request.data.get("message"))
    if not text:
        return Response({"error": "Please write a message."}, status=status.HTTP_400_BAD_REQUEST)
    if len(text) > MAX_LENGTH:
        return Response(
            {"error": f"Please keep your message within {MAX_LENGTH} characters."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    reason_code = str(request.data.get("reason") or "").strip()
    if reason_code and reason_code not in _REASON_LABELS:
        return Response({"error": "Please choose a valid reason."}, status=status.HTTP_400_BAD_REQUEST)
    sent_today = _messages_sent_last_24h(booking)
    if sent_today >= DAILY_LIMIT:
        return Response(
            {
                "error": (
                    f"You can send up to {DAILY_LIMIT} messages for a booking in 24 hours. "
                    "Please try again later or contact the lab directly."
                )
            },
            status=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    from django.db import transaction

    from .booking_events import COMMENT_RECIPIENTS_METADATA_KEY, create_booking_event

    metadata = {
        LAB_MESSAGE_KEY: KIND_USER,
        "lab_message_reason": _REASON_LABELS.get(reason_code, ""),
        "lab_message_reason_code": reason_code,
        COMMENT_RECIPIENTS_METADATA_KEY: {"user": False, "oic": True, "lab_incharge": True},
    }
    if question is not None:
        metadata[IN_REPLY_TO_KEY] = question.event_id
        if not reason_code:
            metadata["lab_message_reason"] = REPLY_REASON_LABEL
    with transaction.atomic():
        event = create_booking_event(
            booking=booking,
            event_type=BookingEventType.COMMENT,
            created_by=user,
            comment=text,
            metadata=metadata,
            send_notification=True,
        )
        if question is not None:
            mark_question_answered(question.event_id, event, user)
    warnings = []
    if not equipment_staff(booking):
        warnings.append(
            "No Lab Operator or Officer In-Charge is assigned to this equipment right now. "
            "Your message is saved on the booking."
        )
    return Response(
        {
            "message": serialize_message(event, booking, user),
            "remaining_today": max(0, DAILY_LIMIT - sent_today - 1),
            "warnings": warnings,
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_lab_message_reply(request, booking_id):
    """Officer In-Charge / Lab Operator of the equipment (or admin) replies to the booking user.

    The reply goes through the regular booking comment notifications: the booking user gets the
    booking comment email, the other OIC / Lab Operator get a copy, the author gets nothing.
    """
    booking = _get_booking(booking_id)
    if booking is None:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    if not can_reply(request.user, booking):
        return Response(
            {"error": "Only the Officer In-Charge or Lab Operator of this equipment can reply."},
            status=status.HTTP_403_FORBIDDEN,
        )
    text = _clean_text(request.data.get("message"))
    if not text:
        return Response({"error": "Please write a reply."}, status=status.HTTP_400_BAD_REQUEST)
    if len(text) > MAX_LENGTH:
        return Response(
            {"error": f"Please keep your reply within {MAX_LENGTH} characters."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    from .booking_events import COMMENT_RECIPIENTS_METADATA_KEY, create_booking_event

    event = create_booking_event(
        booking=booking,
        event_type=BookingEventType.COMMENT,
        created_by=request.user,
        comment=text,
        metadata={
            LAB_MESSAGE_KEY: KIND_STAFF_REPLY,
            COMMENT_RECIPIENTS_METADATA_KEY: {"user": True, "oic": True, "lab_incharge": True},
        },
        send_notification=True,
    )
    return Response(
        {"message": serialize_message(event, booking, request.user), "warnings": []},
        status=status.HTTP_201_CREATED,
    )


def mark_question_answered(question_id: int, reply_event, user) -> None:
    """First reply closes the question; later replies keep the original answer details."""
    question = BookingEvent.objects.select_for_update().filter(event_id=question_id).first()
    if question is None:
        return
    md = dict(question.metadata or {})
    if not md.get(QUESTION_OPEN_KEY):
        return
    md.update(
        {
            QUESTION_OPEN_KEY: False,
            "answered_at": timezone.now().isoformat(),
            "answered_by_event_id": reply_event.event_id,
            "answered_by_user_id": user.pk,
        }
    )
    question.metadata = md
    question.save(update_fields=["metadata"])


def is_user_lab_message(event) -> bool:
    return (
        event is not None
        and event.event_type == BookingEventType.COMMENT
        and (event.metadata or {}).get(LAB_MESSAGE_KEY) == KIND_USER
    )


def ensure_email_template(code: str = EMAIL_TEMPLATE_CODE) -> None:
    """Create the email template row from the default catalog when it has not been synced yet.

    An existing row (possibly customised by an admin) is never changed.
    """
    from iic_booking.communication.models import CommunicationTemplate

    email_type = CommunicationTemplate.CommunicationType.EMAIL
    if CommunicationTemplate.objects.filter(code=code, communication_type=email_type).exists():
        return
    from iic_booking.communication.default_email_templates import get_default_email_templates

    spec = next(t for t in get_default_email_templates() if t["code"] == code)
    CommunicationTemplate.objects.get_or_create(
        code=code,
        communication_type=email_type,
        defaults={
            "name": spec["name"],
            "subject": spec["subject"],
            "body_text": spec["body_text"],
            "body_html": spec["body_html"],
            "description": spec.get("description") or "",
            "variable_help": spec.get("variable_help") or "",
            "is_active": True,
        },
    )


def _booking_time_rows(booking) -> dict[str, str]:
    slots = list(booking.daily_slots.order_by("start_datetime").only("start_datetime", "end_datetime"))
    if slots:
        return {
            "start_time": format_email_datetime(slots[0].start_datetime),
            "end_time": format_email_end_datetime(slots[-1].end_datetime),
        }
    from .models import BookingSlotRange

    released = BookingSlotRange.objects.filter(booking_id=booking.pk).first()
    if released:
        return {
            "start_time": format_email_datetime(released.start_datetime),
            "end_time": format_email_end_datetime(released.end_datetime),
        }
    return {"start_time": "", "end_time": ""}


def send_lab_message_notifications(event) -> None:
    """Email + in-app the message to the equipment's Lab Operator(s) and Officer In-Charge(s).

    Each recipient and channel is attempted independently; failures are logged, never raised.
    The sender gets no email.
    """
    from .booking_events import booking_party_context

    booking = event.booking
    equipment = booking.equipment
    sender = event.created_by
    md = event.metadata or {}
    question = None
    if md.get(IN_REPLY_TO_KEY):
        question = (
            BookingEvent.objects.select_related("created_by")
            .filter(booking=booking, event_id=md.get(IN_REPLY_TO_KEY))
            .first()
        )
    asker = getattr(question, "created_by", None)
    try:
        targets = [(s, role) for s, role in equipment_staff(booking) if not sender or s.id != sender.id]
        if (
            asker is not None
            and getattr(asker, "is_active", True)
            and (not sender or asker.id != sender.id)
            and all(s.id != asker.id for s, _role in targets)
        ):
            targets.append((asker, sender_role_label(asker, booking)))
    except Exception:
        logger.exception("Failed to resolve lab message recipients event_id=%s", event.event_id)
        return
    if not targets:
        return

    display_ref = booking_display_id_for_email(booking)
    path = f"/booking-management?expand={booking.booking_id}"
    link = absolute_http_url(get_frontend_absolute_url(path) or path)
    text = (event.comment or "").strip()
    reason = md.get("lab_message_reason") or ""
    if question is not None:
        asked = (question.comment or "").strip()
        asked = asked if len(asked) <= 160 else asked[:157] + "..."
        asker_name = user_display_name(asker, fallback="the lab") if asker else "the lab"
        reason = f"Reply to the question from {asker_name}: \u201c{asked}\u201d"
    sender_name = user_display_name(sender, fallback="The booking user") if sender else "The booking user"
    sender_role = sender_role_label(sender, booking)
    sender_email = (getattr(sender, "email", "") or "").strip()
    base_context = {
        "booking_id": display_ref,
        "virtual_booking_id": display_ref,
        "equipment_name": equipment.name if equipment else "",
        "equipment_code": equipment.code if equipment else "",
        "booking_status": booking.get_status_display(),
        "sender_name": sender_name,
        "sender_display": f"{sender_name} ({sender_email}), {sender_role}" if sender_email else f"{sender_name}, {sender_role}",
        "message_reason": reason,
        "lab_message": text,
        "lab_message_html": html.escape(text),
        "sent_at": format_email_datetime(event.created_at),
        "link": link,
        **_booking_time_rows(booking),
        **booking_party_context(booking.user, equipment),
    }
    meta = {
        "booking_id": display_ref,
        "real_booking_id": booking.booking_id,
        "event_id": event.event_id,
        "event_type": event.event_type,
        LAB_MESSAGE_KEY: KIND_USER,
        "link": link,
        "staff_recipient": True,
    }

    template_ready = True
    try:
        ensure_email_template()
    except Exception:
        template_ready = False
        logger.exception("Lab message email template unavailable event_id=%s", event.event_id)

    title = f"Message from booking user — {display_ref}"
    if question is not None:
        title = f"Reply to lab question — {display_ref}"
    elif reason:
        title = f"{reason} — {display_ref}"
    preview = text if len(text) <= 200 else text[:197] + "..."
    in_app_message = f"{equipment.name if equipment else ''}: {sender_name} ({sender_role}): {preview}"
    if question is not None:
        meta[IN_REPLY_TO_KEY] = question.event_id

    for staff, role in targets:
        staff_meta = {**meta, "recipient_role": role}
        staff_title = (
            f"Reply to your question — {display_ref}" if asker is not None and staff.id == asker.id else title
        )
        if template_ready and (staff.email or "").strip():
            try:
                CommunicationService.send_email(
                    recipient=staff,
                    template=EMAIL_TEMPLATE_CODE,
                    template_context={
                        **base_context,
                        "user_name": user_display_name(staff),
                        "user_email": staff.email or "",
                    },
                    metadata=staff_meta,
                    created_by=sender,
                )
            except Exception:
                logger.exception(
                    "Failed to email lab message to %s user_id=%s event_id=%s", role, staff.id, event.event_id
                )
        try:
            CommunicationService.send_push_notification(
                recipient=staff,
                title=staff_title,
                message=in_app_message,
                metadata={**staff_meta, "notification_type": "info", "event": "booking.lab_message"},
                created_by=sender,
            )
        except Exception:
            logger.exception(
                "Failed to send in-app lab message to %s user_id=%s event_id=%s", role, staff.id, event.event_id
            )
