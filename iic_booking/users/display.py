"""Consistent user display names (e.g. Prof. prefix for faculty)."""

from __future__ import annotations

import re
from typing import Any

# Titles that may already be part of a stored name ("Dr. Shriniwas Yadav", "Prof Dr X", "MRS. Y").
# Same list as stripHonorifics in the frontend src/lib/displayName.ts.
_NAME_HONORIFIC = "(?:professor|prof|dr|mrs|mr|ms|miss|shri|smt|er|श्रीमती|श्री|सुश्री|डॉ|डा|प्रोफेसर|प्रो)"
_LEADING_TITLES_RE = re.compile(rf"^(?:{_NAME_HONORIFIC}(?:\.\s*|,\s*|\s+|$))+", re.IGNORECASE)

# Same rules as applyFacultyNamePrefix / cleanPersonName in the frontend src/lib/displayName.ts.
_PREFIX_TITLE = r"(?:prof(?:essor)?|dr|mr|mrs|ms|shri|smt)\b\.?"
_BARE_TITLE_RE = re.compile(rf"^(?:{_PREFIX_TITLE}[\s.,]*)+$", re.IGNORECASE)
_ACADEMIC_TITLE_RE = re.compile(r"^(?:prof(?:essor)?|dr)\b", re.IGNORECASE)
_REDUNDANT_PROF_RE = re.compile(rf"^prof(?:essor)?\b\.?\s+(?={_PREFIX_TITLE}(?:\s|$))", re.IGNORECASE)


def _is_faculty_user_type(user_type: Any) -> bool:
    if user_type is None:
        return False
    return str(user_type).strip().lower() == "faculty"


def is_faculty_person(user: Any) -> bool:
    """IIT Roorkee faculty account (user type "faculty"), the only users shown with "Prof."."""
    return user is not None and _is_faculty_user_type(getattr(user, "user_type", None))


def clean_person_name(name: str | None) -> str:
    """
    Trim and collapse whitespace; drop a leading "Prof." that only duplicates another title
    ("Prof. Prof. X", "Prof. Dr. X"). Returns "" for empty input or a bare title such as "Prof.".
    """
    cleaned = " ".join((name or "").split())
    while _REDUNDANT_PROF_RE.match(cleaned):
        cleaned = _REDUNDANT_PROF_RE.sub("", cleaned, count=1)
    if not cleaned or _BARE_TITLE_RE.match(cleaned):
        return ""
    return cleaned


def apply_faculty_name_prefix(name: str | None, user_type: Any = None) -> str:
    """
    "Prof. <name>" for faculty. A name that already starts with Prof. / Professor / Dr. keeps its own
    title ("Dr. X" stays "Dr. X", never "Prof. Dr. X"); other users get the cleaned name unchanged.
    """
    cleaned = clean_person_name(name)
    if not cleaned or not _is_faculty_user_type(user_type):
        return cleaned
    if _ACADEMIC_TITLE_RE.match(cleaned):
        return cleaned
    return f"Prof. {cleaned}"


def format_named_person(name: str | None, user_type: Any = None, email: str | None = None) -> str:
    """Display name from separate values (e.g. a ``.values()`` row); falls back to the email without a prefix."""
    return apply_faculty_name_prefix(name, user_type) or (email or "").strip()


def get_user_display_name(user: Any, *, fallback_to_email: bool = True) -> str:
    """Return the preferred display name for a User-like object."""
    if user is None:
        return ""
    display = apply_faculty_name_prefix(getattr(user, "name", None), getattr(user, "user_type", None))
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
