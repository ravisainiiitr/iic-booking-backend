"""Automation audit trail and the shared, capped AI model helper."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from django.core.cache import cache
from django.utils import timezone

logger = logging.getLogger(__name__)


def log_action(kind: str, action: str, *, target_type: str = "", target_id: Any = "", recipient=None, actor=None,
               summary: str = "", detail: dict | None = None):
    from .models import AutomationActionLog

    try:
        return AutomationActionLog.objects.create(
            kind=kind,
            action=action,
            target_type=target_type[:32],
            target_id=str(target_id or "")[:64],
            recipient=recipient if getattr(recipient, "pk", None) else None,
            actor=actor if getattr(actor, "pk", None) else None,
            summary=summary[:255],
            detail=detail or {},
        )
    except Exception:  # noqa: BLE001
        logger.exception("automation audit log write failed kind=%s action=%s", kind, action)
        return None


def settings_row():
    from .models import AutomationSettings

    try:
        # Read-only: the row is created when the admin first saves the settings, so reads never write.
        return AutomationSettings.objects.filter(pk=1).first() or AutomationSettings(pk=1)
    except Exception:  # noqa: BLE001
        logger.warning("automation settings unavailable; using defaults", exc_info=True)
        return AutomationSettings()


def _llm_counter_key() -> str:
    return f"automation:llm_calls:{timezone.localdate().isoformat()}"


def llm_calls_today() -> int:
    try:
        return int(cache.get(_llm_counter_key()) or 0)
    except Exception:  # noqa: BLE001
        return 0


def consume_llm_budget(limit: int) -> bool:
    """Count one AI model call against today's cap; False when the cap is reached (or 0)."""
    if limit <= 0:
        return False
    key = _llm_counter_key()
    try:
        if cache.add(key, 0, timeout=60 * 60 * 26):
            pass
        current = int(cache.get(key) or 0)
        if current >= limit:
            return False
        cache.incr(key)
    except Exception:  # noqa: BLE001
        return False
    return True


LLM_TIMEOUT_SECONDS = 8.0


def classify_with_llm(text: str, choices: dict[str, str], *, purpose: str, actor=None) -> list[str]:
    """Ask the configured model to pick up to three keys from ``choices`` for ``text``; [] on any problem.

    Only the user's request text (trimmed to 300 characters) is sent: no names, ids or account data. The result
    is whitelisted against ``choices``. Calls are capped per day and logged.
    """
    row = settings_row()
    if not consume_llm_budget(int(row.llm_daily_call_limit or 0)):
        log_action("instrument_finder_llm", "skipped", target_type="llm", summary="daily AI call cap reached", actor=actor)
        return []
    clean = re.sub(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", " ", text or "")
    clean = re.sub(r"\d{5,}", " ", clean)[:300]
    system = (
        "You map a researcher's plain-language request to laboratory characterisation techniques. Reply with JSON "
        '{"keys": [...]} using up to three keys from this list only: '
        + ", ".join(f"{k} ({v})" for k, v in choices.items())
        + ". No prose."
    )
    try:
        from iic_booking.research_copilot.services.llm_gateway import get_gateway

        gateway = get_gateway()
        if hasattr(gateway, "timeout_seconds"):
            try:
                gateway.timeout_seconds = min(float(gateway.timeout_seconds or LLM_TIMEOUT_SECONDS), LLM_TIMEOUT_SECONDS)
            except (TypeError, ValueError):
                pass
        result = gateway.generate([{"role": "system", "content": system}, {"role": "user", "content": clean}], max_tokens=60)
        raw = (getattr(result, "text", "") or "").strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        keys = json.loads(m.group(0)).get("keys", []) if m else []
        picked = [k for k in keys if isinstance(k, str) and k in choices][:3]
        log_action(
            "instrument_finder_llm", "called", target_type="llm", actor=actor, summary=purpose[:120],
            detail={"picked": picked, "provider": str(getattr(result, "provider", "") or ""),
                    "error": str(getattr(result, "error_category", "") or "")},
        )
        return picked
    except Exception as exc:  # noqa: BLE001
        log_action("instrument_finder_llm", "failed", target_type="llm", actor=actor, summary=type(exc).__name__)
        return []
