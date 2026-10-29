"""Normalisers for the two TrackCourier.io shapes.

``GET /v1/track`` answers in PascalCase (``ShipmentState``, ``Checkpoints``, ``CheckpointState``); webhook payloads are
snake_case throughout (``status``, ``checkpoints``, ``checkpoint_state``). They are parsed separately into the same
normalised form. Checkpoints are stored newest first; the provider does not guarantee an order, so we sort by the
courier's Date + Time where they parse and keep the provider's order otherwise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .constants import (
    POSTAL_FORMAT_SLUGS,
    POSTAL_TRACKING_RE,
    PROVIDER_STATE_MAP,
    TRACKING_NUMBER_RE,
    WEBHOOK_STATE_MAP,
    ShipmentStatus,
)

_TEXT_LIMIT = 500


@dataclass
class NormalizedTracking:
    status: str  # ShipmentStatus value ("" when the provider state is unknown to us)
    provider_state: str
    status_label: str
    checkpoints: list[dict] | None  # None: provider sent no list (keep what we have)
    courier_has_no_record: bool = False
    delivered_date: str = ""


def normalize_tracking_number(raw: str) -> str:
    """Remove whitespace and upper-case, as the provider does."""
    return re.sub(r"\s+", "", raw or "").upper()


def tracking_number_problem(courier_slug: str, normalized: str) -> str:
    """User-facing reason the number cannot be right, or "" (format rules only where the provider documents them)."""
    if not normalized:
        return "Enter the tracking (AWB) number."
    if courier_slug in POSTAL_FORMAT_SLUGS:
        if not POSTAL_TRACKING_RE.match(normalized):
            return (
                "India Post, Speed Post, Registered Post and India Post International numbers have 13 characters: "
                "2 letters, 9 digits, then 2 letters (for example EE123456789IN)."
            )
        return ""
    if not TRACKING_NUMBER_RE.match(normalized):
        return "Tracking numbers use 5 to 50 letters, digits, hyphens or slashes."
    return ""


def _text(value: Any) -> str:
    return str(value).strip()[:_TEXT_LIMIT] if value is not None else ""


def _when(date: str, time: str) -> datetime | None:
    for fmt in ("%d-%b-%Y %H:%M", "%d-%b-%Y %H:%M:%S", "%Y-%m-%d %H:%M", "%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M"):
        try:
            return datetime.strptime(f"{date} {time or '00:00'}".strip(), fmt)
        except ValueError:
            continue
    for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(date, fmt)
        except ValueError:
            continue
    return None


def _sorted_newest_first(points: list[dict]) -> list[dict]:
    keyed = [(i, _when(p["date"], p["time"])) for i, p in enumerate(points)]
    if all(when is not None for _, when in keyed) and keyed:
        order = sorted(keyed, key=lambda k: (k[1], -k[0]), reverse=True)
        return [points[i] for i, _ in order]
    return points


def _checkpoint(item: dict, *, keys: dict[str, str], state_map: dict) -> dict:
    raw_state = _text(item.get(keys["state"])).lower()
    return {
        "activity": _text(item.get(keys["activity"])),
        "state": raw_state,
        "status": str(state_map.get(raw_state, "")),
        "courier_name": _text(item.get(keys["courier_name"]))[:128],
        "date": _text(item.get(keys["date"]))[:40],
        "time": _text(item.get(keys["time"]))[:20],
        "location": _text(item.get(keys["location"]))[:255],
    }


_PASCAL = {"activity": "Activity", "state": "CheckpointState", "courier_name": "CourierName", "date": "Date",
           "time": "Time", "location": "Location"}
_SNAKE = {"activity": "activity", "state": "checkpoint_state", "courier_name": "courier_name", "date": "date",
          "time": "time", "location": "location"}


def _checkpoints(raw: Any, keys: dict[str, str], state_map: dict) -> list[dict] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        return None
    points = [_checkpoint(item, keys=keys, state_map=state_map) for item in raw if isinstance(item, dict)]
    return _sorted_newest_first(points)


def parse_track_response(data: dict) -> NormalizedTracking:
    """``data`` object of a successful ``GET /v1/track`` (PascalCase)."""
    state = _text(data.get("ShipmentState")).lower()
    no_record = bool(data.get("CourierHasNoRecordOfShipment"))
    status = str(PROVIDER_STATE_MAP.get(state, ""))
    return NormalizedTracking(
        status=status,
        provider_state=state,
        status_label=_text(data.get("MostRecentStatus"))[:255],
        checkpoints=_checkpoints(data.get("Checkpoints"), _PASCAL, PROVIDER_STATE_MAP),
        courier_has_no_record=no_record,
    )


@dataclass
class WebhookTracking:
    event: str
    courier_slug: str
    tracking_number: str
    enqueued_at: str
    tracking: NormalizedTracking


class WebhookPayloadError(ValueError):
    pass


def parse_webhook_payload(payload: Any) -> WebhookTracking:
    """A verified webhook body (snake_case). Raises WebhookPayloadError for shapes we cannot act on."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise WebhookPayloadError("payload has no data object")
    data = payload["data"]
    courier = _text(data.get("courier")).lower()[:64]
    number = normalize_tracking_number(_text(data.get("tracking_number")))[:64]
    if not courier or not number:
        raise WebhookPayloadError("payload has no courier or tracking number")
    state = _text(data.get("status")).lower()
    return WebhookTracking(
        event=_text(payload.get("event"))[:40],
        courier_slug=courier,
        tracking_number=number,
        enqueued_at=_text(payload.get("enqueued_at") or data.get("enqueued_at"))[:40],
        tracking=NormalizedTracking(
            status=str(WEBHOOK_STATE_MAP.get(state, "")),
            provider_state=state,
            status_label=_text(data.get("most_recent_status"))[:255],
            checkpoints=_checkpoints(data.get("checkpoints"), _SNAKE, WEBHOOK_STATE_MAP),
            delivered_date=_text(data.get("delivered_date"))[:40],
        ),
    )


__all__ = [
    "NormalizedTracking",
    "ShipmentStatus",
    "WebhookPayloadError",
    "WebhookTracking",
    "normalize_tracking_number",
    "parse_track_response",
    "parse_webhook_payload",
    "tracking_number_problem",
]
