"""Sample lifecycle rules derived from the equipment's sample timing settings.

Walk-in equipment has neither a sample submission lead time nor a sample collect /
discard deadline (both unset or 0): users bring their samples to the slot in person and
take them back, so the laboratory never holds the sample. No submission reminders,
collection notices, disposal notices or sample-based "Booking Not Utilized" marking apply.
"""

from __future__ import annotations

from django.db.models import Q


def _hours(value) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def sample_collect_deadline_hours(equipment) -> int:
    return _hours(getattr(equipment, "sample_collect_deadline_hours", 0)) if equipment is not None else 0


def equipment_has_sample_collect_deadline(equipment) -> bool:
    return sample_collect_deadline_hours(equipment) > 0


def equipment_is_walk_in_sample(equipment) -> bool:
    if equipment is None:
        return False
    return (
        _hours(getattr(equipment, "sample_submission_lead_hours", 0)) == 0
        and sample_collect_deadline_hours(equipment) == 0
    )


def walk_in_sample_equipment_q(prefix: str = "") -> Q:
    """Filter matching walk-in equipment; ``prefix`` is the lookup path to Equipment (e.g. ``"equipment__"``)."""
    return Q(**{f"{prefix}sample_submission_lead_hours": 0, f"{prefix}sample_collect_deadline_hours": 0})
