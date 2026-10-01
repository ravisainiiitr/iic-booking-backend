"""
Whitelisted Booking Assistant button actions (`ba_*`).

Card buttons post {type, payload} back to the conversation. Every type has a fixed payload schema;
unknown types, unknown keys or badly typed values are rejected with a 400 before anything runs, and
ids are only ever looked up again under the caller's own visibility/ownership.
"""

from __future__ import annotations

import re
from typing import Any

PICK_EQUIPMENT = "ba_pick_equipment"
AVAILABILITY = "ba_availability"
PICK_SLOT = "ba_pick_slot"
REVIEW = "ba_review"
INFO = "ba_info"
UPCOMING = "ba_upcoming"
FLOW = "ba_flow"

FLOW_STEPS = (
    "start", "department", "equipment", "inputs", "slots", "slot",
    "edit_inputs", "change_slot", "change_equipment", "change_department", "cancel",
)
MAX_SAMPLE_SETS = 20

INTENTS = ("availability", "book", "info", "overview", "location", "contacts", "charges", "instructions", "inputs", "rules")
TOPICS = ("overview", "location", "contacts", "charges", "instructions", "inputs", "rules")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")
_FIELD_KEY_RE = re.compile(r"^[A-Z](_elements)?$")


class InvalidAssistantAction(ValueError):
    pass


def is_assistant_action(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    t = raw.get("type") or raw.get("action_type")
    return isinstance(t, str) and t.strip().lower().startswith("ba_")


def _int(value: Any, *, lo: int = 1, hi: int = 2**31 - 1) -> int:
    if isinstance(value, bool):
        raise InvalidAssistantAction("invalid_integer")
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or not lo <= value <= hi:
        raise InvalidAssistantAction("invalid_integer")
    return value


def _when(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"start", "end", "after", "before", "at", "period"}:
        raise InvalidAssistantAction("invalid_when")
    out: dict[str, Any] = {}
    for key in ("start", "end"):
        v = value.get(key)
        if v is None:
            continue
        if not isinstance(v, str) or not _DATE_RE.match(v):
            raise InvalidAssistantAction("invalid_when")
        out[key] = v
    for key in ("after", "before", "at"):
        v = value.get(key)
        if v is None:
            continue
        if not isinstance(v, str) or not _TIME_RE.match(v):
            raise InvalidAssistantAction("invalid_when")
        out[key] = v
    period = value.get("period")
    if period is not None:
        if period not in ("morning", "afternoon", "evening"):
            raise InvalidAssistantAction("invalid_when")
        out["period"] = period
    if not out.get("start"):
        return None
    return out


def _slot_ids(value: Any) -> list[int]:
    if not isinstance(value, list) or not 1 <= len(value) <= 24:
        raise InvalidAssistantAction("invalid_slot_ids")
    ids = [_int(v) for v in value]
    if len(set(ids)) != len(ids):
        raise InvalidAssistantAction("invalid_slot_ids")
    return ids


def _inputs(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or len(value) > 26:
        raise InvalidAssistantAction("invalid_inputs")
    out: dict[str, str] = {}
    for k, v in value.items():
        if not isinstance(k, str) or not _FIELD_KEY_RE.match(k):
            raise InvalidAssistantAction("invalid_inputs")
        if isinstance(v, bool):
            v = "true" if v else "false"
        if v is None:
            continue
        if not isinstance(v, (str, int, float)):
            raise InvalidAssistantAction("invalid_inputs")
        text = str(v)
        if len(text) > 500:
            raise InvalidAssistantAction("invalid_inputs")
        out[k] = text
    return out


def _sample_sets(value: Any) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_SAMPLE_SETS:
        raise InvalidAssistantAction("invalid_sample_sets")
    return [s for s in (_inputs(v) for v in value) if s]


def _optional_int(value: Any) -> int | None:
    return None if value is None else _int(value)


def _step(value: Any) -> str:
    if value not in FLOW_STEPS:
        raise InvalidAssistantAction("invalid_step")
    return value


_SCHEMAS: dict[str, dict[str, Any]] = {
    PICK_EQUIPMENT: {"equipment_id": _int, "intent": "intent", "when": _when},
    AVAILABILITY: {"equipment_id": _int, "when": _when},
    PICK_SLOT: {"equipment_id": _int, "slot_ids": _slot_ids},
    REVIEW: {
        "equipment_id": _int, "slot_ids": _slot_ids, "number_of_samples": "samples",
        "input_values": _inputs, "sample_sets": _sample_sets,
    },
    INFO: {"equipment_id": _int, "topic": "topic"},
    UPCOMING: {},
    FLOW: {
        "step": _step,
        "department_id": _optional_int,
        "equipment_id": _optional_int,
        "number_of_samples": "samples",
        "input_values": _inputs,
        "sample_sets": _sample_sets,
        "slot_ids": _slot_ids,
        "when": _when,
    },
}
_REQUIRED = {
    PICK_EQUIPMENT: {"equipment_id"},
    AVAILABILITY: {"equipment_id"},
    PICK_SLOT: {"equipment_id", "slot_ids"},
    REVIEW: {"equipment_id", "slot_ids"},
    INFO: {"equipment_id"},
    UPCOMING: set(),
    FLOW: {"step"},
}
ACTION_TYPES = frozenset(_SCHEMAS)


def parse(raw: Any) -> dict[str, Any]:
    if not is_assistant_action(raw):
        raise InvalidAssistantAction("invalid_action")
    action_type = str(raw.get("type") or raw.get("action_type")).strip().lower()
    schema = _SCHEMAS.get(action_type)
    if schema is None:
        raise InvalidAssistantAction("unknown_action")
    payload = raw.get("payload") or {}
    if not isinstance(payload, dict) or set(payload) - set(schema):
        raise InvalidAssistantAction("invalid_payload")
    missing = _REQUIRED[action_type] - set(payload)
    if missing:
        raise InvalidAssistantAction("invalid_payload")
    clean: dict[str, Any] = {}
    for key, rule in schema.items():
        if key not in payload:
            continue
        value = payload[key]
        if rule == "intent":
            if value is None:
                continue
            if value not in INTENTS:
                raise InvalidAssistantAction("invalid_intent")
            clean[key] = value
        elif rule == "topic":
            if value is None:
                continue
            if value not in TOPICS:
                raise InvalidAssistantAction("invalid_topic")
            clean[key] = value
        elif rule == "samples":
            clean[key] = _int(value, lo=1, hi=500)
        else:
            parsed = rule(value)
            if parsed is not None:
                clean[key] = parsed
    return {"type": action_type, "payload": clean}
