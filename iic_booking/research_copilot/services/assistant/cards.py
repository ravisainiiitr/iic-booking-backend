"""Response envelope, buttons and option cards for the Booking Assistant (`ba_*` card types)."""

from __future__ import annotations

from typing import Any

from iic_booking.research_copilot.services.v2.response_builder import build_response

SOURCE_LABEL = "Live portal data"


def reply(
    content: str,
    *,
    cards: list[dict[str, Any]] | None = None,
    actions: list[dict[str, Any]] | None = None,
    intent: str = "",
    kind: str = "LIVE_DATA",
    title_hint: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "booking_assistant": True,
        "deterministic": True,
        "llm_used": False,
        "intent": f"assistant:{intent}" if intent else "assistant",
        "source_label": SOURCE_LABEL,
    }
    if title_hint:
        meta["title_hint"] = title_hint[:80]
    if extra:
        meta.update(extra)
    return build_response(kind=kind, content=content, cards=cards, actions=actions, metadata=meta)


def link(label: str, href: str, *, primary: bool = False) -> dict[str, Any]:
    slug = "".join(ch if ch.isalnum() else "_" for ch in label.lower())[:40]
    return {"id": f"ba_link:{slug}:{href}"[:120], "label": label, "href": href, "enabled": True, "primary": primary}


def assistant_action(label: str, action_type: str, payload: dict[str, Any], *, primary: bool = False, utterance: str | None = None) -> dict[str, Any]:
    key = ":".join(str(v) for v in payload.values() if isinstance(v, (int, str)))[:60]
    return {
        "id": f"{action_type}:{key}:{label}"[:120],
        "label": label,
        "action_type": action_type,
        "payload": payload,
        "utterance": utterance or label,
        "enabled": True,
        "primary": primary,
        "style": "primary" if primary else "secondary",
    }


def equipment_options_card(
    rows: list[dict[str, Any]],
    *,
    title: str,
    intent: str,
    when=None,
    prompt: str = "",
    query: str = "",
) -> dict[str, Any]:
    return {
        "type": "ba_equipment_options",
        "title": title,
        "prompt": prompt,
        "query": query,
        "intent": intent,
        "when": when.to_payload() if when is not None else None,
        "items": rows,
    }
