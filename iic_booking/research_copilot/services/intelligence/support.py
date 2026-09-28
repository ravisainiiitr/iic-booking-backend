"""
Copilot -> support ticket escalation.

Uses the existing portal ticket system (TicketCreateSerializer, TicketComment, routing/events),
so tickets appear in the normal support queue with OIC auto-assignment. A ticket is only created
when the user explicitly asks for one; the same question in the same conversation within
DEDUPE_WINDOW returns the existing ticket instead of creating a duplicate.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

DEDUPE_WINDOW = timedelta(minutes=10)
_HISTORY_MESSAGES = 8
_HISTORY_CHARS = 600

_TYPE_BY_INTENT = {
    "BOOKING": "booking",
    "CANCELLATION": "booking",
    "PARTIAL_CANCELLATION": "booking",
    "RESCHEDULING": "booking",
    "MY_BOOKINGS": "booking",
    "NEXT_BOOKING": "booking",
    "EQUIPMENT_AVAILABILITY": "booking",
    "COST_ESTIMATE": "payment",
    "WALLET_BALANCE": "payment",
    "WALLET_TRANSACTIONS": "payment",
    "CREDIT": "payment",
    "EQUIPMENT_SEARCH": "equipment",
    "EQUIPMENT_INFORMATION": "equipment",
    "EQUIPMENT_COMPARISON": "equipment",
    "EQUIPMENT_RECOMMENDATION": "equipment",
    "RESULT_STATUS": "laboratory",
    "SAMPLE_STATUS": "laboratory",
    "REMOTE_ANALYSIS": "laboratory",
    "MY_RESEARCH": "account",
    "RESEARCH_GROUP": "account",
}


class EscalationError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def ticket_href(ticket_id) -> str:
    return f"/tickets?ticket={ticket_id}"


def ticket_type_for(intent: str) -> str:
    return _TYPE_BY_INTENT.get((intent or "").upper(), "general")


def _visible_equipment_id(user, equipment_id) -> int | None:
    if not equipment_id:
        return None
    from iic_booking.research_copilot.services.intelligence import equipment

    try:
        eq = equipment.get_visible(user, int(equipment_id))
    except (TypeError, ValueError):
        return None
    return int(eq.pk) if eq else None


def _owned_booking_id(user, booking_id) -> int | None:
    if not booking_id:
        return None
    from iic_booking.equipment.models import Booking

    try:
        return Booking.objects.filter(booking_id=int(booking_id), user=user).values_list("booking_id", flat=True).first()
    except (TypeError, ValueError):
        return None


def _history(conversation) -> str:
    if conversation is None:
        return ""
    rows = list(conversation.messages.order_by("-created_at").values("role", "content")[:_HISTORY_MESSAGES])
    lines = []
    for r in reversed(rows):
        text = " ".join(str(r["content"] or "").split())
        if len(text) > _HISTORY_CHARS:
            text = text[: _HISTORY_CHARS - 3] + "..."
        lines.append(f"{r['role']}: {text}")
    return "\n".join(lines)


def _description(*, question, copilot_response, intent, entities, reason, note, conversation) -> str:
    parts = ["Raised from IIC Booking Assistant.", "", f"Question: {question}"]
    if note:
        parts += ["", f"Additional details from user: {note}"]
    if copilot_response:
        parts += ["", f"Booking Assistant response: {copilot_response[:1500]}"]
    parts += ["", f"Reason: {reason}"]
    if intent:
        parts.append(f"Detected intent: {intent}")
    if entities:
        shown = {k: v for k, v in entities.items() if v not in (None, "", [], {}, False)}
        if shown:
            parts.append("Detected details: " + ", ".join(f"{k}={v}" for k, v in list(shown.items())[:12]))
    history = _history(conversation)
    if history:
        parts += ["", "Recent conversation:", history]
    return "\n".join(parts)[:8000]


def _existing(user, conversation, question):
    from iic_booking.research_copilot.models import CopilotEscalation

    since = timezone.now() - DEDUPE_WINDOW
    return (
        CopilotEscalation.objects.select_related("ticket")
        .filter(user=user, conversation=conversation, question=question, created_at__gte=since)
        .exclude(ticket=None)
        .order_by("-created_at")
        .first()
    )


def escalate(
    *,
    user,
    conversation,
    question: str,
    reason: str = "user_requested",
    intent: str = "",
    entities: dict[str, Any] | None = None,
    equipment_id: Any = None,
    booking_id: Any = None,
    copilot_response: str = "",
    message=None,
    knowledge_article=None,
    note: str = "",
) -> dict[str, Any]:
    """Create (or reuse) a support ticket for this Copilot question. Caller verifies conversation ownership."""
    from iic_booking.research_copilot.models import CopilotEscalation, EscalationReason
    from iic_booking.support.models import TicketComment
    from iic_booking.support.serializers import TicketCreateSerializer
    from iic_booking.support.ticket_service import apply_create_routing_and_events

    if user is None or not getattr(user, "is_authenticated", False):
        raise EscalationError("Sign in to raise a support ticket.", status=401)
    question = " ".join(str(question or "").split())[:2000]
    if not question:
        raise EscalationError("There is no question to escalate.")
    if reason not in EscalationReason.values:
        reason = EscalationReason.OTHER

    prior = _existing(user, conversation, question)
    if prior is not None:
        return {"ok": True, "ticket_id": prior.ticket_id, "duplicate": True, "href": ticket_href(prior.ticket_id)}

    eq_id = _visible_equipment_id(user, equipment_id)
    bk_id = _owned_booking_id(user, booking_id)
    entities = dict(entities or {})
    data = {
        "ticket_type": ticket_type_for(intent),
        "subject": ("Booking Assistant: " + question)[:255],
        "description": _description(
            question=question,
            copilot_response=str(copilot_response or ""),
            intent=intent,
            entities=entities,
            reason=reason,
            note=" ".join(str(note or "").split())[:1000],
            conversation=conversation,
        ),
        "priority": "medium",
    }
    if eq_id:
        data["related_equipment"] = eq_id
    if bk_id:
        data["related_booking"] = bk_id

    with transaction.atomic():
        serializer = TicketCreateSerializer(data=data)
        if not serializer.is_valid():
            logger.warning("copilot escalation ticket invalid: %s", serializer.errors)
            raise EscalationError("The support ticket could not be created. Please use the Support page.")
        ticket = serializer.save(user=user)
        raised_by = ticket.get_user_name() or "User"
        TicketComment.objects.create(
            ticket=ticket, user=user, comment=f"Ticket raised by {raised_by} from IIC Booking Assistant.", is_internal=False
        )
        ticket = apply_create_routing_and_events(ticket, actor=user)
        CopilotEscalation.objects.create(
            ticket=ticket,
            user=user,
            conversation=conversation,
            message=message,
            question=question,
            intent=(intent or "")[:64],
            entities=entities,
            equipment_id=eq_id,
            booking_id=bk_id,
            reason=reason,
            copilot_response=str(copilot_response or "")[:4000],
            knowledge_article=knowledge_article,
        )
    return {"ok": True, "ticket_id": ticket.ticket_id, "duplicate": False, "href": ticket_href(ticket.ticket_id)}
