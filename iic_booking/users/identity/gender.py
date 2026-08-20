"""Normalize Channel-I biological sex → portal Gender (explicit map only)."""

from __future__ import annotations

from iic_booking.users.models.user import Gender

# Exact Channel-I values observed / allowed. Unknown → leave unset (do not invent).
CHANNEL_I_SEX_TO_GENDER: dict[str, str] = {
    "male": Gender.MALE,
    "female": Gender.FEMALE,
    "man": Gender.MALE,
    "woman": Gender.FEMALE,
    "m": Gender.MALE,
    "f": Gender.FEMALE,
}


def normalize_channel_i_sex(raw) -> str | None:
    """Return Gender choice or None when value is missing/unsupported."""
    if raw is None:
        return None
    key = str(raw).strip().casefold()
    if not key:
        return None
    return CHANNEL_I_SEX_TO_GENDER.get(key)


def extract_channel_i_sex(user_info: dict | None) -> str:
    """Raw Channel-I sex string from biologicalInformation (no invention)."""
    info = user_info if isinstance(user_info, dict) else {}
    bio = info.get("biologicalInformation") or info.get("biological_information") or {}
    if not isinstance(bio, dict):
        return ""
    value = bio.get("sex") or bio.get("Sex") or ""
    return str(value).strip() if value is not None else ""
