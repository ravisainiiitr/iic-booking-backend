"""Helpers for support ticket routing, events, and assignee notifications."""

from __future__ import annotations

import logging
from typing import Any, Optional

from django.conf import settings
from django.core.mail import send_mail

from .models import Ticket, TicketEvent

logger = logging.getLogger(__name__)

STAFF_ASSIGNEE_TYPES = ("admin", "manager", "operator", "finance")


def user_can_manage_tickets(user) -> bool:
    """True for Django staff or Main Administrator (and other assignee staff types)."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_staff", False):
        return True
    ut = (getattr(user, "user_type", None) or "").strip().lower()
    return ut in STAFF_ASSIGNEE_TYPES or ut == "admin"

def get_equipment_primary_oic(equipment) -> Optional[Any]:
    """Return the first active EquipmentManager (OIC) for the equipment, if any."""
    if not equipment:
        return None
    try:
        from iic_booking.equipment.models import EquipmentManager

        row = (
            EquipmentManager.objects.filter(equipment=equipment, manager__is_active=True)
            .select_related("manager")
            .order_by("equipment_manager_id")
            .first()
        )
        return row.manager if row else None
    except Exception:
        logger.exception("Failed to resolve equipment OIC for equipment=%s", getattr(equipment, "pk", None))
        return None


def record_ticket_event(
    ticket: Ticket,
    event_type: str,
    *,
    actor=None,
    message: str = "",
    from_value: str = "",
    to_value: str = "",
    metadata: Optional[dict] = None,
    is_internal: bool = False,
) -> TicketEvent:
    return TicketEvent.objects.create(
        ticket=ticket,
        event_type=event_type,
        actor=actor if getattr(actor, "is_authenticated", False) else None,
        message=message or "",
        from_value=from_value or "",
        to_value=to_value or "",
        metadata=metadata or {},
        is_internal=is_internal,
    )


def notify_ticket_assignee(ticket: Ticket, *, assigned_by=None, previous_assignee=None) -> None:
    """Email + in-app push to the newly assigned staff member."""
    assignee = ticket.assigned_to
    if not assignee:
        return

    by_name = ""
    if assigned_by and getattr(assigned_by, "is_authenticated", False):
        by_name = assigned_by.name or assigned_by.email or ""
    prev = ""
    if previous_assignee:
        prev = previous_assignee.name or previous_assignee.email or str(previous_assignee.pk)

    equip = ""
    if ticket.related_equipment_id:
        equip = f"{ticket.related_equipment.code} — {ticket.related_equipment.name}"

    base = (getattr(settings, "FRONTEND_URL", None) or "").rstrip("/")
    link = f"{base}/admin-settings/support" if base else ""

    subject = f"Support ticket #{ticket.ticket_id} assigned to you"
    lines = [
        f"Hello {assignee.get_display_name()},",
        "",
        f"Ticket #{ticket.ticket_id} has been assigned to you.",
        f"Subject: {ticket.subject}",
        f"Type: {ticket.get_ticket_type_display()}",
        f"Priority: {ticket.get_priority_display()}",
        f"Status: {ticket.get_status_display()}",
        f"Raised by: {ticket.get_user_name()} ({ticket.get_user_email()})",
    ]
    if equip:
        lines.append(f"Equipment: {equip}")
    if prev:
        lines.append(f"Previous assignee: {prev}")
    if by_name:
        lines.append(f"Assigned by: {by_name}")
    if link:
        lines.extend(["", f"Open Support: {link}"])
    lines.extend(["", "— IIT Roorkee IIC Booking"])

    try:
        send_mail(
            subject=subject,
            message="\n".join(lines),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[assignee.email],
            fail_silently=False,
        )
    except Exception:
        logger.exception("Failed assignee email for ticket #%s to %s", ticket.ticket_id, assignee.email)

    try:
        from iic_booking.communication.service import CommunicationService

        CommunicationService.send_push_notification(
            recipient=assignee,
            title=f"Ticket #{ticket.ticket_id} assigned to you",
            message=f"{ticket.subject} — {ticket.get_priority_display()} priority",
            metadata={
                "notification_type": "info",
                "kind": "ticket_assigned",
                "ticket_id": ticket.ticket_id,
                "link": "/admin-settings/support",
            },
        )
    except Exception:
        logger.exception("Failed assignee push for ticket #%s", ticket.ticket_id)


def apply_create_routing_and_events(ticket: Ticket, *, actor=None) -> Ticket:
    """
    On create: auto-assign equipment OIC when related_equipment is set and unassigned;
    record created (+ assigned) events; notify assignee.
    """
    raised_by = ticket.get_user_name() or "User"
    record_ticket_event(
        ticket,
        TicketEvent.EventType.CREATED,
        actor=actor,
        message=f"Ticket raised by {raised_by}.",
        metadata={"ticket_type": ticket.ticket_type, "priority": ticket.priority},
    )

    if ticket.related_equipment_id and not ticket.assigned_to_id:
        oic = get_equipment_primary_oic(ticket.related_equipment)
        if oic:
            ticket.assigned_to = oic
            ticket.save(update_fields=["assigned_to", "updated_at"])
            record_ticket_event(
                ticket,
                TicketEvent.EventType.ASSIGNED,
                actor=actor,
                message=f"Auto-assigned to equipment OIC {oic.name or oic.email}.",
                from_value="Unassigned",
                to_value=oic.name or oic.email or str(oic.pk),
                metadata={"auto": True, "reason": "equipment_oic"},
            )
            notify_ticket_assignee(ticket, assigned_by=actor, previous_assignee=None)

    return ticket
