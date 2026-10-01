"""
Entity extraction from a user message.

Everything extracted here is a *candidate*: equipment terms are resolved against the portal
database under the user's visibility, booking ids are checked for ownership, and slot ids are only
accepted when they were offered to this user in the current conversation state.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from iic_booking.research_copilot.services.intelligence import terminology

_DATE_CUES = re.compile(
    r"\b(today|tomorrow|tonight|week|weekend|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
    r"jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec|january|february|march|april|june|july|august|"
    r"september|october|november|december|\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.]\d{1,2})\b|\bmay\s+\d{1,2}\b|\b\d{1,2}\s+may\b|"
    r"\bafter\s+\d{1,2}\b"
)
_PERIODS = {
    "morning": ("morning", "forenoon", "before noon", " am slot"),
    "afternoon": ("afternoon", "after lunch", "post lunch"),
    "evening": ("evening", "late slot"),
}
_SAMPLE_RE = re.compile(r"\b(\d{1,3})\s*(?:samples?|specimens?|pieces?|pcs|nos?\.?)\b")
_SAMPLE_WORD_RE = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|single)\s+(?:samples?|specimens?)\b"
)
_WORD_NUMBERS = {
    "one": 1, "single": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
_BOOKING_RE = re.compile(r"(?:\bbooking\s*(?:id|no\.?|number)?\s*[#:]?\s*|#\s*)(\d{1,9})\b", re.IGNORECASE)
_EARLIEST_RE = re.compile(r"\b(earliest|first available|next available|soonest|as soon as possible|asap)\b")
_PARTIAL_RE = re.compile(r"\b(only|some|partial|partially|selected|few|one of|part of|specific)\b")
_ENTIRE_RE = re.compile(r"\b(entire|whole|full|complete|all slots|everything)\b")


@dataclass
class Entities:
    techniques: list[str] = field(default_factory=list)
    purpose_techniques: list[str] = field(default_factory=list)
    has_date: bool = False
    period: str | None = None
    sample_count: int | None = None
    booking_ref: int | None = None
    booking_vref: str | None = None
    earliest: bool = False
    partial: bool = False
    entire: bool = False
    next_booking: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def extract(text: str) -> Entities:
    lower = terminology.normalize(text)
    ents = Entities()
    ents.techniques = [t.key for t in terminology.find_techniques(lower)]
    ents.purpose_techniques = [t.key for t in terminology.techniques_for_purpose(lower)]
    ents.has_date = bool(_DATE_CUES.search(lower))
    for period, cues in _PERIODS.items():
        if any(c in f" {lower}" for c in cues):
            ents.period = period
            break
    m = _SAMPLE_RE.search(lower)
    if m:
        ents.sample_count = max(1, min(int(m.group(1)), 500))
    else:
        m = _SAMPLE_WORD_RE.search(lower)
        if m:
            ents.sample_count = _WORD_NUMBERS[m.group(1)]
    m = _BOOKING_RE.search(text or "")
    if m:
        ents.booking_ref = int(m.group(1))
    else:
        from iic_booking.research_copilot.services.booking_refs import find_virtual_ref

        ents.booking_vref = find_virtual_ref(text or "")
    ents.earliest = bool(_EARLIEST_RE.search(lower))
    ents.partial = bool(_PARTIAL_RE.search(lower))
    ents.entire = bool(_ENTIRE_RE.search(lower))
    ents.next_booking = bool(re.search(r"\b(next|upcoming)\s+booking\b", lower))
    return ents


def parse_count(text: str) -> int | None:
    """A reply that is just a number or "3 samples" (used when Copilot asked for a sample count)."""
    lower = terminology.normalize(text).strip(" .!?")
    if re.fullmatch(r"\d{1,3}", lower):
        return max(1, min(int(lower), 500))
    m = _SAMPLE_RE.search(lower)
    if m:
        return max(1, min(int(m.group(1)), 500))
    m = _SAMPLE_WORD_RE.search(lower)
    if m:
        return _WORD_NUMBERS[m.group(1)]
    if lower in _WORD_NUMBERS:
        return _WORD_NUMBERS[lower]
    return None
