"""Phase D — unanswered query capture for continuous improvement."""

from __future__ import annotations

from typing import Any


def log_unanswered(
    *,
    user=None,
    conversation=None,
    query: str,
    intent: str = "",
    reason: str = "NO_AUTHORITATIVE_SOURCE",
    attempted_tools: list[str] | None = None,
    confidence: str = "LOW_CONFIDENCE",
) -> None:
    try:
        from iic_booking.research_copilot.models import KnowledgeGap

        KnowledgeGap.objects.create(
            conversation=conversation if getattr(conversation, "pk", None) else None,
            user=user if getattr(user, "is_authenticated", False) else None,
            query_summary=(query or "")[:500],
            reason=(reason or "")[:64],
            suggested_faq=(
                f"intent={intent}; confidence={confidence}; tools={','.join(attempted_tools or [])}"
            )[:2000],
        )
    except Exception:  # noqa: BLE001
        pass


def unanswered_response(*, query: str) -> dict[str, Any]:
    from iic_booking.research_copilot.services.v2.response_builder import build_response

    return build_response(
        kind="CLARIFICATION",
        content=(
            "I do not have an authoritative portal answer for that yet, so I will not invent one.\n\n"
            "Your question has been logged for the lab knowledge team. "
            "Try naming equipment (e.g. FESEM, PXRD) or open Equipments / Support Tickets."
        ),
        actions=[
            {"id": "equipments", "label": "Browse equipment", "href": "/equipments", "enabled": True},
            {"id": "tickets", "label": "Support tickets", "href": "/tickets", "enabled": True},
            {"id": "help", "label": "Research help", "prompt": "How do I prepare a sample for FESEM?", "enabled": True},
        ],
        metadata={"deterministic": True, "unanswered": True, "confidence": "NO_AUTHORITATIVE_SOURCE"},
        escalate=True,
    )
