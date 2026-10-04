"""Mobile number rule shared by registration, profile edits and the post-login "Complete your profile" prompt.

The portal accepts a 10-digit Indian mobile number starting with 6, 7, 8 or 9 (optionally written with
+91, 91 or a leading 0, spaces, dashes or brackets). Anything else (empty, 0000000000, too short,
foreign numbers) counts as no mobile number.
"""

from __future__ import annotations

import re

from .models.user_type import UserType

INDIAN_MOBILE_ERROR = (
    "Enter a valid 10-digit Indian mobile number (e.g. 9876543210). It must start with 6, 7, 8, or 9."
)

# IIT Roorkee faculty accounts, including Officer In Charge accounts (held by faculty), are never prompted.
MOBILE_PROMPT_EXEMPT_USER_TYPES = frozenset({UserType.FACULTY, UserType.MANAGER})

_SEPARATORS = re.compile(r"[\s\-().]")
_COUNTRY_OR_TRUNK_PREFIX = re.compile(r"^(?:\+91|0091|91(?=\d{10}$)|0+)")
_INDIAN_MOBILE = re.compile(r"[6-9]\d{9}")


def normalize_indian_mobile(value: object) -> str | None:
    """Return the bare 10-digit mobile number, or None when the value is not a valid Indian mobile."""
    raw = str(value or "").strip()
    if not raw:
        return None
    digits = _COUNTRY_OR_TRUNK_PREFIX.sub("", _SEPARATORS.sub("", raw))
    return digits if _INDIAN_MOBILE.fullmatch(digits) else None


def is_valid_mobile_number(value: object) -> bool:
    return normalize_indian_mobile(value) is not None


def is_mobile_prompt_exempt(user) -> bool:
    user_type = str(getattr(user, "user_type", "") or "").strip().lower()
    return user_type in MOBILE_PROMPT_EXEMPT_USER_TYPES


def user_needs_mobile_number(user) -> bool:
    """True when the post-login prompt should ask this user for a mobile number."""
    if user is None or is_mobile_prompt_exempt(user):
        return False
    return not is_valid_mobile_number(getattr(user, "phone_number", None))
