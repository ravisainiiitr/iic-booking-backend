from __future__ import annotations

from iic_booking.research_copilot.models import AuditAction, CopilotAuditEvent


def write_audit(
    *,
    action: str,
    message: str = "",
    user=None,
    conversation=None,
    detail: dict | None = None,
) -> CopilotAuditEvent:
    return CopilotAuditEvent.objects.create(
        action=action,
        message=(message or "")[:512],
        user=user,
        conversation=conversation,
        detail=detail or {},
    )


def audit_conversation_created(*, user, conversation) -> None:
    write_audit(
        action=AuditAction.CONVERSATION_CREATED,
        message="Conversation created",
        user=user,
        conversation=conversation,
        detail={"conversation_id": str(conversation.id)},
    )


def audit_message_replied(*, user, conversation, confidence: float | None, escalate: bool) -> None:
    write_audit(
        action=AuditAction.MESSAGE_REPLIED if not escalate else AuditAction.ESCALATE_HINT,
        message="Assistant replied",
        user=user,
        conversation=conversation,
        detail={
            "conversation_id": str(conversation.id),
            "confidence": confidence,
            "escalate_hint": escalate,
        },
    )
