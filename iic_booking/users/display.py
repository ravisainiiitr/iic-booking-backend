"""Consistent user display names (e.g. Prof. prefix for faculty)."""

from __future__ import annotations

from typing import Any


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
