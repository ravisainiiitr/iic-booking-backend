"""Consistent user display names (e.g. Prof. prefix for faculty)."""

from __future__ import annotations

import re
from typing import Any

# Titles that may already be part of a stored name ("Dr. Shriniwas Yadav", "Prof Dr X", "MRS. Y").
# Same list as stripHonorifics in the frontend src/lib/displayName.ts.
_NAME_HONORIFIC = "(?:professor|prof|dr|mrs|mr|ms|miss|shri|smt|er|श्रीमती|श्री|सुश्री|डॉ|डा|प्रोफेसर|प्रो)"
_LEADING_TITLES_RE = re.compile(rf"^(?:{_NAME_HONORIFIC}(?:\.\s*|,\s*|\s+|$))+", re.IGNORECASE)


def _is_faculty_user_type(user_type: Any) -> bool:
    if user_type is None:
        return False
    return str(user_type).strip().lower() == "faculty"


def apply_faculty_name_prefix(name: str, user_type: Any = None) -> str:
    """
    Prefix faculty names with "Prof." when missing.
    Idempotent for names that already start with Prof / Professor.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        return cleaned
    if not _is_faculty_user_type(user_type):
        return cleaned
    lower = cleaned.lower()
    if lower.startswith("prof.") or lower.startswith("professor"):
        return cleaned
    return f"Prof. {cleaned}"


def get_user_display_name(user: Any, *, fallback_to_email: bool = True) -> str:
    """Return the preferred display name for a User-like object."""
    if user is None:
        return ""
    raw = (getattr(user, "name", None) or "").strip()
    user_type = getattr(user, "user_type", None)
    display = apply_faculty_name_prefix(raw, user_type)
    if display:
        return display
    if fallback_to_email:
        return (getattr(user, "email", None) or "").strip()
    return ""


def strip_name_honorifics(name: str | None) -> str:
    """Name without leading titles (Mr, Mrs, Ms, Miss, Dr, Prof, Professor, Shri, Smt, Er; dots and case optional)."""
    cleaned = " ".join((name or "").split())
    return _LEADING_TITLES_RE.sub("", cleaned).strip()


def compose_honorific_name(name: str | None, honorific: str | None) -> str:
    """
    "<honorific> <name without its own titles>", e.g. ("Dr. Shriniwas Yadav", "Prof.") -> "Prof. Shriniwas Yadav".
    Returns "" when the honorific is blank or the name has nothing left after removing titles.
    """
    title = (honorific or "").strip()
    bare = strip_name_honorifics(name)
    if not title or not bare:
        return ""
    return f"{title} {bare}"


def name_with_honorific(user: Any, honorific: str | None, *, default: str = "") -> str:
    """
    Name of an equipment contact (OIC / Lab Operator) using the honorific chosen for that equipment.
    The explicit honorific replaces any title in the stored name and the automatic faculty "Prof.";
    a blank honorific (or a user without a real name) returns ``default`` unchanged.
    """
    if user is None:
        return default
    return compose_honorific_name(getattr(user, "name", None), honorific) or default
