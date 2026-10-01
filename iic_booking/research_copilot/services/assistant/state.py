"""Per-conversation assistant context (current equipment and date window), kept in cache for follow-ups."""

from __future__ import annotations

from typing import Any

from django.core.cache import cache
from django.utils import timezone

TTL_SECONDS = 6 * 60 * 60


def _key(conversation) -> str | None:
    cid = getattr(conversation, "id", None)
    return f"booking_assistant:{cid}" if cid else None


def load(conversation) -> dict[str, Any]:
    key = _key(conversation)
    if not key:
        return {}
    data = cache.get(key)
    return dict(data) if isinstance(data, dict) else {}


def save(conversation, **values) -> None:
    key = _key(conversation)
    if not key:
        return
    data = load(conversation)
    data.update({k: v for k, v in values.items() if v is not None})
    data["updated_at"] = timezone.now().isoformat()
    cache.set(key, data, TTL_SECONDS)


def remember_equipment(conversation, eq, when=None, intent: str | None = None) -> None:
    save(
        conversation,
        equipment_id=int(eq.pk),
        equipment_name=eq.name,
        when=when.to_payload() if when is not None else None,
        intent=intent,
    )
    # Let the guided intelligence flow pick up the same instrument ("book it" after an availability answer).
    try:
        from iic_booking.research_copilot.services.intelligence import state as intel_state

        st = intel_state.load(conversation)
        if st.get("last_equipment_id") != int(eq.pk):
            st["last_equipment_id"] = int(eq.pk)
            st["last_equipment_name"] = eq.name
            intel_state.save(conversation, st)
    except Exception:  # noqa: BLE001
        pass
