"""Lightweight per-request timing for book-equipment (diagnose slow confirmations)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


class BookingRequestTimer:
    """Monotonic marks from the start of the slot-ids booking path."""

    __slots__ = ("_t0", "_last", "marks")

    def __init__(self) -> None:
        self._t0 = time.perf_counter()
        self._last = self._t0
        self.marks: list[tuple[str, float, float]] = []

    def mark(self, name: str) -> None:
        now = time.perf_counter()
        total_ms = (now - self._t0) * 1000.0
        delta_ms = (now - self._last) * 1000.0
        self.marks.append((name, total_ms, delta_ms))
        self._last = now

    def total_ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000.0

    def to_header_json(self) -> str:
        payload = {
            "marks": [
                {"n": n, "total_ms": round(t, 2), "delta_ms": round(d, 2)}
                for n, t, d in self.marks
            ],
            "total_ms": round(self.total_ms(), 2),
        }
        return json.dumps(payload, separators=(",", ":"))

    def log_summary(self, *, equipment_id: Any = None, slow_ms: float = 2000.0) -> None:
        total = self.total_ms()
        if total < slow_ms:
            return
        parts = [f"{n} +{d:.0f}ms" for n, _, d in self.marks]
        logger.warning(
            "Slow book_equipment slot_ids path equipment_id=%s total_ms=%.0f | %s",
            equipment_id,
            total,
            " | ".join(parts),
        )


def attach_booking_performance_headers(
    response: Any,
    perf: BookingRequestTimer,
    *,
    equipment_id: Any = None,
) -> None:
    """Set X-Booking-Perf and log when BOOKING_PERFORMANCE_TIMINGS or DEBUG."""
    from django.conf import settings

    perf.log_summary(equipment_id=equipment_id)
    if getattr(settings, "BOOKING_PERFORMANCE_TIMINGS", False) or getattr(
        settings, "DEBUG", False
    ):
        response["X-Booking-Perf"] = perf.to_header_json()
    if getattr(settings, "BOOKING_PERFORMANCE_TIMINGS", False):
        logger.info(
            "book_equipment perf equipment_id=%s %s",
            equipment_id,
            perf.to_header_json(),
        )
