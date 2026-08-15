"""Query intelligence helpers for Research Copilot (AI.22).

Deterministic follow-up enrichment and clarification — not a second Copilot engine.
Server-side only; does not change authorization.
"""

from __future__ import annotations

import re
from typing import Any

_EQUIPMENT_HINTS = (
    "xrd",
    "pxrd",
    "gi-xrd",
    "fesem",
    "sem",
    "tem",
    "afm",
    "xps",
    "equipment",
    "instrument",
)

_FOLLOWUP_RE = re.compile(
    r"^\s*("
    r"how much(?: will it cost)?(?:\?)?|"
    r"what about (?:it|that|tomorrow|this)(?:\?)?|"
    r"can i (?:use|analyze|do) it(?: remotely)?(?:\?)?|"
    r"what should i prepare(?:\?)?|"
    r"and the (?:cost|price|fee)(?:\?)?|"
    r"is it available(?:\?)?|"
    r"which equipment(?: do we have)?(?: for that)?(?:\?)?|"
    r"what equipment(?: do we have)?(?: for that)?(?:\?)?"
    r")\s*$",
    re.I,
)


def extract_num_samples(text: str) -> int | None:
    lower = (text or "").lower()
    m = re.search(r"\b(\d+)\s*samples?\b", lower)
    if m:
        return int(m.group(1))
    m = re.search(r"\bfor\s+(\d+)\b", lower)
    if m:
        return int(m.group(1))
    return None


def _mentions_equipment(text: str) -> bool:
    lower = (text or "").lower()
    if re.search(r"\bequipment(?:_id)?\s*[:=]?\s*\d+\b", lower):
        return True
    return any(h in lower for h in _EQUIPMENT_HINTS)


def enrich_query_with_history(*, text: str, prior_user_texts: list[str] | None = None) -> dict[str, Any]:
    """Attach recent user context to short follow-ups without dumping tool payloads."""
    raw = (text or "").strip()
    prior = [p.strip() for p in (prior_user_texts or []) if (p or "").strip()]
    if not raw:
        return {"text": raw, "enriched": False, "prior_used": ""}

    if not _FOLLOWUP_RE.match(raw) and not (
        len(raw.split()) <= 6
        and re.search(r"\b(it|that|this|them|those)\b", raw, re.I)
        and not _mentions_equipment(raw)
    ):
        return {"text": raw, "enriched": False, "prior_used": ""}

    for prev in reversed(prior[-4:]):
        if _mentions_equipment(prev) or re.search(r"\bbooking\b", prev, re.I):
            enriched = f"{raw}\n[Prior user context: {prev[:180]}]"
            return {"text": enriched, "enriched": True, "prior_used": prev[:180]}
    return {"text": raw, "enriched": False, "prior_used": ""}


def clarification_question(*, text: str) -> str | None:
    """Return a concise clarification when required portal entities are missing.

    Correct clarification is a success — do not guess equipment/booking.
    """
    lower = (text or "").lower().strip()
    if not lower:
        return None

    # Already has enough grounding cues.
    if _mentions_equipment(lower) or re.search(r"\bbooking\s*#?\s*\d+\b", lower):
        return None
    if "[prior user context:" in lower:
        return None

    if re.search(r"\bcan i book( it)?\b", lower) or lower in {"book it", "book this", "book this?"}:
        return "Which equipment would you like to book (for example PXRD, FESEM, or an equipment id)?"

    if re.search(r"\bcan i (?:use|analyze|do) it\b", lower) and not _mentions_equipment(lower):
        return "Which equipment or prior context should I use for that request?"

    if re.search(r"\b(is it available|when (?:is|can) (?:it|this))\b", lower) or lower in {
        "is it available?",
        "available?",
    }:
        return "Which equipment's availability should I check?"

    if re.search(r"\bhow much (?:will it|does it) cost\b", lower) or lower in {
        "how much?",
        "how much will it cost?",
        "how much does it cost?",
    }:
        return "Which equipment should I estimate charges for, and how many samples (if applicable)?"

    if lower in {"cancel it", "change it", "modify it", "cancel it?", "change it?", "modify it?"}:
        return "Which booking should I use? You can say your next booking or a booking id."

    if re.search(r"\b(cancel|change|modify) (?:my )?booking\b", lower) and not re.search(
        r"\bbooking\s*#?\s*\d+\b", lower
    ):
        # List/search tools can still help; only force clarify when completely pronoun-vague.
        pass

    return None


def security_refusal(*, text: str) -> str | None:
    """Hard refuse secret / infrastructure disclosure attempts (AI.22.2).

    Correct refusal is safer than letting the small model invent URLs or prompts.
    """
    lower = (text or "").lower()
    if not lower:
        return None
    needles = (
        "system prompt",
        "api key",
        "api keys",
        "ollama url",
        "internal ollama",
        "reveal the system",
        "ignore previous instructions",
        "secret token",
        "access token",
        "private key",
        "another user's",
        "another user",
        "other user's",
        "other users'",
        "someone else's",
    )
    if any(n in lower for n in needles):
        return (
            "I can't help with that. I won't disclose system prompts, API keys, "
            "tokens, or internal service URLs. Portal booking and research help remain available."
        )
    return None
