"""
Phase D — multi-intent decomposition (deterministic, capped).

Decomposes compound user requests into ordered sub-intents without LLM.
"""

from __future__ import annotations

from dataclasses import dataclass

from iic_booking.research_copilot.services.v2.intent_resolver import ResolvedIntent, resolve_intent


@dataclass
class MultiIntentPlan:
    intents: list[ResolvedIntent]
    is_multi: bool


_SPLITTERS = (" and then ", " then ", " and ", ", then ", "; ")


def plan_intents(text: str, *, max_parts: int = 3) -> MultiIntentPlan:
    """
    Prefer a single resolve_intent match; if the utterance clearly chains
    multiple operational verbs, split and resolve each segment.
    """
    import re

    lower = (text or "").lower().strip()
    primary = resolve_intent(text)
    # Multi-intent signals — whole words only (avoid "book" matching "booking")
    verbs = ("find", "book", "estimate", "check", "recharge", "cancel", "reschedule", "compare", "show")
    chain_words = sum(1 for w in verbs if re.search(rf"\b{re.escape(w)}\b", lower))
    # "booking" alone is not a chain verb
    if chain_words < 2 and " and " not in lower and ";" not in lower:
        return MultiIntentPlan(intents=[primary], is_multi=False)

    parts: list[str] = [text]
    for sep in _SPLITTERS:
        if sep in lower:
            parts = [
                p.strip()
                for p in re.split(
                    r"\s+and then\s+|\s+then\s+|;\s*|,\s*(?=and\s)|(?:\s+and\s+)",
                    text,
                    flags=re.I,
                )
                if p and p.strip()
            ]
            break

    if len(parts) <= 1:
        return MultiIntentPlan(intents=[primary], is_multi=False)

    intents: list[ResolvedIntent] = []
    seen: set[str] = set()
    for part in parts[:max_parts]:
        ri = resolve_intent(part)
        if not ri.deterministic or ri.intent in {"empty", "general"}:
            continue
        if ri.intent in seen and ri.intent not in {"search_slots", "estimate_cost"}:
            continue
        seen.add(ri.intent)
        intents.append(ri)

    if len(intents) <= 1:
        return MultiIntentPlan(intents=[primary], is_multi=False)
    return MultiIntentPlan(intents=intents, is_multi=True)
