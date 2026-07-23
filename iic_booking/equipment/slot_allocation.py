"""
Reusable slot-count allocation helpers, including Slot Tolerance.

NumberOfSlots = max(1, ceil((AnalysisTime − SlotTolerance) / SlotDuration))

When SlotTolerance is 0 this is identical to ceil(AnalysisTime / SlotDuration).
"""

from __future__ import annotations

import math
from typing import Optional, Union


def _as_non_negative_int(value: Union[int, float, None], default: int = 0) -> int:
    try:
        n = int(value) if value is not None else default
    except (TypeError, ValueError):
        n = default
    return max(0, n)


def slot_tolerance_minutes_for(equipment) -> int:
    """Read equipment.slot_tolerance_minutes safely (default 0)."""
    if equipment is None:
        return 0
    return _as_non_negative_int(getattr(equipment, "slot_tolerance_minutes", 0), 0)


def slots_needed_for_analysis_time(
    analysis_time_minutes: Union[int, float, None],
    slot_duration_minutes: Union[int, float, None],
    slot_tolerance_minutes: Union[int, float, None] = 0,
) -> int:
    """
    Minimum number of slots required to cover analysis time with optional overrun tolerance.

    (NumberOfSlots × SlotDuration) + SlotTolerance ≥ AnalysisTime
    NumberOfSlots = max(1, ceil((AnalysisTime − SlotTolerance) / SlotDuration))

    Returns 0 only when analysis_time_minutes ≤ 0 (nothing to schedule).
    """
    analysis = _as_non_negative_int(analysis_time_minutes, 0)
    duration = _as_non_negative_int(slot_duration_minutes, 0)
    tolerance = _as_non_negative_int(slot_tolerance_minutes, 0)

    if analysis <= 0:
        return 0
    if duration <= 0:
        # Degenerate config: treat as needing at least one slot.
        return 1

    # ceil((analysis - tolerance) / duration), floored at 1 when analysis > 0
    adjusted = analysis - tolerance
    if adjusted <= 0:
        return 1
    return max(1, int(math.ceil(adjusted / duration)))


def slots_needed_for_equipment(
    equipment,
    analysis_time_minutes: Union[int, float, None],
    *,
    slot_duration_minutes: Optional[Union[int, float]] = None,
) -> int:
    """Convenience wrapper using equipment.slot_duration_minutes and slot_tolerance_minutes."""
    duration = (
        slot_duration_minutes
        if slot_duration_minutes is not None
        else getattr(equipment, "slot_duration_minutes", None) or 0
    )
    return slots_needed_for_analysis_time(
        analysis_time_minutes,
        duration,
        slot_tolerance_minutes_for(equipment),
    )


def allocated_capacity_covers_analysis(
    allocated_slot_minutes: Union[int, float, None],
    analysis_time_minutes: Union[int, float, None],
    slot_tolerance_minutes: Union[int, float, None] = 0,
) -> bool:
    """
    True when allocated wall-clock slot minutes plus tolerance cover analysis time:

        allocated + tolerance ≥ analysis
    """
    allocated = _as_non_negative_int(allocated_slot_minutes, 0)
    analysis = _as_non_negative_int(analysis_time_minutes, 0)
    tolerance = _as_non_negative_int(slot_tolerance_minutes, 0)
    if analysis <= 0:
        return True
    return allocated + tolerance >= analysis


def minutes_covered_with_tolerance(
    allocated_slot_minutes: Union[int, float, None],
    slot_tolerance_minutes: Union[int, float, None] = 0,
) -> int:
    """Effective analysis minutes coverable by allocated slots under tolerance."""
    return _as_non_negative_int(allocated_slot_minutes, 0) + _as_non_negative_int(
        slot_tolerance_minutes, 0
    )
