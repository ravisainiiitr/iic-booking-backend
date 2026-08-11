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


def audit_tool_executed(*, user, name: str, ok: bool, arguments: dict | None = None, result: dict | None = None) -> None:
    """Record Copilot tool execution (read or confirmation-card prepare)."""
    safe_args = {
        k: v
        for k, v in (arguments or {}).items()
        if k.lower() not in {"password", "token", "secret", "authorization"}
    }
    write_audit(
        action=AuditAction.TOOL_EXECUTED if ok else AuditAction.TOOL_DENIED,
        message=f"Tool {name} {'ok' if ok else 'denied/failed'}"[:512],
        user=user,
        detail={
            "tool": name,
            "ok": ok,
            "arguments": safe_args,
            "error": (result or {}).get("error"),
            "message": (result or {}).get("message"),
        },
    )
