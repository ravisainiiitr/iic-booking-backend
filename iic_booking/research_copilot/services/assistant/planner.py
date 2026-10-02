"""
Optional LLM planner for phrasings the deterministic parser does not recognise.

The model only *classifies*: it returns JSON naming one whitelisted intent plus the equipment words,
the date/time words and an info topic copied from the user's message. The result is fed back through
the same deterministic resolvers (visible-equipment matching, IST date parsing, portal slot lookup),
so the model never sees other users' data, never chooses ids and can never book: bookings still need
the summary card's Confirm button and a server-side proposal token.

BOOKING_ASSISTANT_LLM_PLANNER: "off", "auto"/"on" (default; active only when OPENAI_API_KEY is set,
because the small local model is too slow to add to every unrecognised message), or "local" to opt into
the configured local model anyway. With a key the planner calls OpenAI directly, whatever COPILOT_PROVIDER
the rest of the Copilot uses.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

PLANNER_INTENTS = ("availability", "info", "policy", "capability", "upcoming", "booking_status", "none")
_TOPICS = ("overview", "location", "contacts", "charges", "instructions", "inputs", "rules")
PLANNER_TIMEOUT_SECONDS = 8.0

_SYSTEM = (
    "You classify messages sent to a laboratory equipment booking assistant. Reply with one JSON object only, "
    'shaped {"intent": "...", "equipment": "...", "when": "...", "topic": "..."}. '
    f"intent is one of {', '.join(PLANNER_INTENTS)}. equipment is the instrument name or technique exactly as "
    "written by the user (empty if none). when is the date/time words copied from the message (empty if none). "
    f"topic is one of {', '.join(_TOPICS)} for info questions, else empty. Never add other keys or prose."
)


def _openai_key() -> str:
    return str(getattr(settings, "OPENAI_API_KEY", "") or "").strip()


def planner_enabled() -> bool:
    mode = str(getattr(settings, "BOOKING_ASSISTANT_LLM_PLANNER", "auto") or "auto").strip().lower()
    if mode in {"off", "false", "0", "no"}:
        return False
    if mode == "local":
        return True
    return bool(_openai_key())


def _planner_gateway():
    from iic_booking.research_copilot.services.llm_gateway import OpenAIGateway, get_gateway

    key = _openai_key()
    if key:
        model = str(getattr(settings, "OPENAI_CHAT_MODEL", "") or "gpt-4o-mini").strip()
        return OpenAIGateway(api_key=key, model=model, timeout_seconds=PLANNER_TIMEOUT_SECONDS)
    return get_gateway()


def plan(text: str) -> dict[str, Any] | None:
    if not planner_enabled() or not text or len(text) > 400:
        return None
    try:
        result = _planner_gateway().generate(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": text[:400]}], max_tokens=120
        )
    except Exception:  # noqa: BLE001
        logger.warning("booking assistant planner unavailable", exc_info=True)
        return None
    raw = (getattr(result, "text", "") or "").strip()
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict) or data.get("intent") not in PLANNER_INTENTS or data.get("intent") == "none":
        return None
    lower = text.lower()

    def _quoted(key: str) -> str:
        value = str(data.get(key) or "").strip()[:80]
        # Only words that actually occur in the user's message are trusted.
        return value if value and all(w in lower for w in value.lower().split()) else ""

    topic = data.get("topic") if data.get("topic") in _TOPICS else None
    return {"intent": data["intent"], "equipment": _quoted("equipment"), "when": _quoted("when"), "topic": topic}
