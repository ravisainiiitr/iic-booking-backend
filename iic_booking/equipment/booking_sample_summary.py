"""Compact "3 sets · 12 samples" summary of a booking's sample details for list rows.

The number of samples is read from the equipment's sample-count input (a NUMERIC field labelled
like "No. of Samples", "Number of samples", "Sample count" or "Samples"), summed over sample set 1
and every additional sample set. Mirrors the frontend `src/lib/sampleCount.ts`.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from typing import Any, Iterable, Optional

from .calculators import split_sample_sets
from .sample_set_limits import booking_field_user_type

NUMERIC_FIELD_TYPES = frozenset({"NUMERIC", "NUMBER"})
_COUNT_WORDS = r"(?:nos\.?|no\.?|number|count|qty\.?|quantity|how\s+many)"
SAMPLE_COUNT_LABEL_RE = re.compile(
    rf"\b{_COUNT_WORDS}\s*(?:of\s+)?samples?\b"
    rf"|\bsamples?\s*\(?\s*{_COUNT_WORDS}"
    r"|^\s*samples?\s*$",
    re.IGNORECASE,
)
PER_SAMPLE_RE = re.compile(r"\b(?:per|each)\s+sample\b", re.IGNORECASE)


def is_sample_count_label(label: Any) -> bool:
    text = str(label or "")
    return bool(SAMPLE_COUNT_LABEL_RE.search(text)) and not PER_SAMPLE_RE.search(text)


def sample_count_field_key(fields: Iterable[dict]) -> Optional[str]:
    """Key of the first NUMERIC input (in field-key order) labelled as a sample count, else None."""
    for field in sorted(fields, key=lambda f: str(f.get("field_key") or "")):
        if str(field.get("field_type") or "").strip().upper() not in NUMERIC_FIELD_TYPES:
            continue
        if is_sample_count_label(field.get("field_label")):
            return str(field.get("field_key"))
    return None


def _positive_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        n = float(str(value).strip().replace(",", "."))
    except ValueError:
        return None
    return n if math.isfinite(n) and n > 0 else None


def sample_summary(input_values: Any, sample_key: Optional[str]) -> dict:
    """{"sets": sample sets in the booking (at least 1), "samples": total samples or None when unknown}."""
    primary, extra = split_sample_sets(input_values if isinstance(input_values, dict) else None)
    groups = [primary, *extra]
    samples = None
    if sample_key:
        counts = [n for n in (_positive_number(g.get(sample_key)) for g in groups) if n is not None]
        if counts:
            total = sum(counts)
            samples = int(total) if float(total).is_integer() else round(total, 2)
    return {"sets": len(groups), "samples": samples}


class SampleCountFieldIndex:
    """Sample-count field per (equipment, user type), loaded with one query per batch of equipment."""

    def __init__(self) -> None:
        self._fields: dict[int, dict[str, list[dict]]] = {}

    def preload(self, equipment_ids: Iterable[Any]) -> None:
        from .models import DynamicInputField

        wanted = set()
        for eid in equipment_ids:
            try:
                wanted.add(int(eid))
            except (TypeError, ValueError):
                continue
        missing = wanted - set(self._fields)
        if not missing:
            return
        for eid in missing:
            self._fields[eid] = defaultdict(list)
        rows = DynamicInputField.objects.filter(equipment_id__in=missing).values(
            "equipment_id", "user_type", "field_key", "field_label", "field_type"
        )
        for row in rows:
            self._fields[row["equipment_id"]][(row["user_type"] or "").strip()].append(row)

    def key_for(self, equipment_id: Any, user_type: str) -> Optional[str]:
        """Same field set BookingSerializer.input_fields uses: the user type's own fields, else the common ones."""
        if equipment_id is None:
            return None
        self.preload([equipment_id])
        by_type = self._fields.get(int(equipment_id)) or {}
        ut = (user_type or "").strip()
        fields = by_type.get(ut) if ut and by_type.get(ut) else by_type.get("", [])
        return sample_count_field_key(fields or [])


def booking_sample_summary(booking, index: SampleCountFieldIndex) -> dict:
    key = index.key_for(getattr(booking, "equipment_id", None), booking_field_user_type(booking))
    return sample_summary(getattr(booking, "input_values", None), key)
