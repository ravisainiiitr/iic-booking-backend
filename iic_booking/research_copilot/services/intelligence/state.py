"""
Structured conversation state (separate from chat history).

Shape (all keys optional):
    workflow        booking | availability | estimate | cancel | reschedule | equipment | support
    step            workflow step, e.g. choose_equipment, ask_samples, choose_slot, confirm
    equipment_id / equipment_name
    samples, period, date_text, input_values, slot_ids, booking_id, cancel_mode
    pending_choice  {kind, prompt, options: [{value, label, ...}]}
    last_equipment_id / last_equipment_name   (kept across workflows for "book it" style follow-ups)
    last_intent, last_question, updated_at

Choice values are only accepted when they were offered to this user in `pending_choice`, so a
client can never inject an arbitrary equipment, slot or booking id through a button payload.
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from django.utils import timezone
from django.utils.dateparse import parse_datetime

STATE_TTL = timedelta(minutes=30)
STICKY_KEYS = ("last_equipment_id", "last_equipment_name", "title_auto")
_ORDINALS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "last": -1}


def load(conversation) -> dict[str, Any]:
    if conversation is None:
        return {}
    raw = getattr(conversation, "state", None)
    state = dict(raw) if isinstance(raw, dict) else {}
    updated = parse_datetime(str(state.get("updated_at") or ""))
    if updated is not None and timezone.now() - updated > STATE_TTL:
        return {k: state[k] for k in STICKY_KEYS if k in state}
    return state


def save(conversation, state: dict[str, Any]) -> None:
    if conversation is None:
        return
    state = dict(state)
    state["updated_at"] = timezone.now().isoformat()
    conversation.state = state
    conversation.save(update_fields=["state"])


def reset_workflow(state: dict[str, Any], workflow: str | None = None, **values) -> dict[str, Any]:
    fresh = {k: state[k] for k in STICKY_KEYS if k in state}
    if workflow:
        fresh["workflow"] = workflow
    fresh.update({k: v for k, v in values.items() if v is not None})
    return fresh


def restart(state: dict[str, Any], workflow: str | None = None, **values) -> dict[str, Any]:
    """Replace the workflow keys in place (sticky keys survive), so callers holding `state` see the reset."""
    fresh = reset_workflow(state, workflow, **values)
    state.clear()
    state.update(fresh)
    return state


def set_choice(state: dict[str, Any], *, kind: str, prompt: str, options: list[dict[str, Any]]) -> dict[str, Any]:
    state["pending_choice"] = {
        "kind": kind,
        "prompt": prompt,
        "options": [{k: v for k, v in o.items() if k in {"value", "label", "match"}} for o in options],
    }
    return state


def clear_choice(state: dict[str, Any]) -> dict[str, Any]:
    state.pop("pending_choice", None)
    return state


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def match_choice(state: dict[str, Any], *, value: Any = None, text: str = "", kind: str | None = None) -> dict[str, Any] | None:
    """Return the offered option picked by an explicit value, or by typed text ("Bruker", "2", "the second one")."""
    pending = state.get("pending_choice") or {}
    if not pending or (kind and pending.get("kind") != kind):
        return None
    options = list(pending.get("options") or [])
    if not options:
        return None
    if value is not None and str(value) != "":
        for opt in options:
            if str(opt.get("value")) == str(value):
                return opt
        return None
    typed = _norm(text)
    if not typed:
        return None
    m = re.fullmatch(r"(?:option\s+|number\s+|no\s+)?(\d{1,2})", typed)
    if m:
        idx = int(m.group(1))
        return options[idx - 1] if 1 <= idx <= len(options) else None
    for word, idx in _ORDINALS.items():
        if re.fullmatch(rf"(?:the\s+)?{word}(?:\s+one)?", typed):
            return options[idx - 1] if idx > 0 and idx <= len(options) else (options[-1] if idx == -1 else None)
    exact = [o for o in options if _norm(o.get("label", "")) == typed or _norm(str(o.get("match", ""))) == typed]
    if len(exact) == 1:
        return exact[0]
    tokens = [t for t in typed.split() if len(t) >= 2 and t not in {"the", "one", "please", "book", "use", "i", "want"}]
    if not tokens:
        return None
    partial = [
        o
        for o in options
        if all(t in _norm(o.get("label", "") + " " + str(o.get("match", ""))).split() or t in _norm(o.get("label", "")) for t in tokens)
    ]
    return partial[0] if len(partial) == 1 else None
