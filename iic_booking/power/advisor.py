"""Plain-language UPS health summary for the Officer in Charge.

The portal's own language model (the IITR Assistant's gateway: a local Ollama model by default, so no
per-call cost) words the summary from the findings of ``analysis.assess`` only. It is told not to add facts, and its
reply is checked; if the model is unreachable, slow or replies badly, the summary is composed from the findings
instead. ``summary_source`` records which.
"""

from __future__ import annotations

import json
import logging

from .analysis import Assessment

logger = logging.getLogger(__name__)

LEVEL_WORDS = {"ok": "is healthy", "watch": "is working, with points to keep an eye on",
               "attention": "needs attention soon", "urgent": "needs attention now"}

SYSTEM = (
    "You write short UPS health notes for the Officer in Charge of laboratory instruments at IIT Roorkee. "
    "Use ONLY the facts in the JSON you are given; never add numbers, causes, dates or advice that are not in it. "
    "Write 2 to 5 plain sentences, at most 110 words, no headings, no lists, no markdown. "
    "Start with the overall state of the UPS and which instruments depend on it. Then say what to do, most urgent "
    "first, and why it matters for the instruments. If the readings are not yet verified, say so in one sentence."
)


def rules_summary(device, assessment: Assessment, equipment_names: list[str]) -> str:
    on = f" (powers {', '.join(equipment_names)})" if equipment_names else ""
    head = f"{device.name}{on} {LEVEL_WORDS[assessment.level]}."
    parts = [head]
    for f in [f for f in assessment.findings if f.code != "map_unverified"][:4]:
        parts.append(f"{f.title}: {f.detail} {f.action}")
    if any(f.code == "map_unverified" for f in assessment.findings):
        parts.append("These readings are not yet verified against the UPS's network card.")
    return " ".join(parts)


def _facts(device, assessment: Assessment, equipment_names: list[str]) -> str:
    return json.dumps({
        "ups": {"name": device.name, "make": device.manufacturer, "model": device.model,
                "powers_instruments": equipment_names},
        "overall": assessment.level,
        "findings": [{"level": f.level, "title": f.title, "detail": f.detail, "what_to_do": f.action}
                     for f in assessment.findings],
    }, ensure_ascii=False)


def _acceptable(text: str) -> bool:
    if not text or not (40 <= len(text) <= 1200):
        return False
    lowered = text.lower()
    return not any(bad in lowered for bad in ("as an ai", "i cannot", "i'm sorry", "```", "{\""))


def summarise(device, assessment: Assessment, equipment_names: list[str], *, gateway=None) -> tuple[str, str, str]:
    """(summary, source "ai"|"rules", model)."""
    fallback = rules_summary(device, assessment, equipment_names)
    if assessment.level == "ok" and not assessment.findings:
        return fallback, "rules", ""
    try:
        if gateway is None:
            from iic_booking.research_copilot.services.llm_gateway import get_gateway

            gateway = get_gateway()
        if getattr(gateway, "provider_name", "") == "fallback":
            return fallback, "rules", ""
        result = gateway.complete([
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": _facts(device, assessment, equipment_names)},
        ], max_tokens=260)
    except Exception:  # noqa: BLE001 - the model is optional; the rules summary always works
        logger.warning("UPS advisor: language model unavailable for UPS %s", device.pk, exc_info=True)
        return fallback, "rules", ""
    text = " ".join((getattr(result, "text", "") or "").split())
    if result is None or getattr(result, "error_category", "") or getattr(result, "provider", "") == "local" \
            or not _acceptable(text):
        return fallback, "rules", ""
    return text, "ai", str(getattr(result, "model", "") or "")[:80]
