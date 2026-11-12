"""GSTIN format and check-digit validation, and the GST state codes.

A GSTIN is 15 characters: 2-digit state code, 10-character PAN, entity number, "Z" by default, and a check character
computed with the GST mod-36 algorithm. Passing this check only means the number is well formed; whether it is a
registered, active taxpayer can only be confirmed by the GST system (Get GSTIN details in the sandbox).
"""

from __future__ import annotations

import re

STATE_CODES = {
    "01": "Jammu and Kashmir", "02": "Himachal Pradesh", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand",
    "06": "Haryana", "07": "Delhi", "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura", "17": "Meghalaya",
    "18": "Assam", "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "Madhya Pradesh",
    "24": "Gujarat", "26": "Dadra and Nagar Haveli and Daman and Diu", "27": "Maharashtra", "29": "Karnataka",
    "30": "Goa", "31": "Lakshadweep", "32": "Kerala", "33": "Tamil Nadu", "34": "Puducherry",
    "35": "Andaman and Nicobar Islands", "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory",
}
STATE_BY_NAME = {name.lower(): code for code, name in STATE_CODES.items()}

_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_PATTERN = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def check_character(first14: str) -> str:
    """The GST check character for the first 14 characters (mod-36 weighted sum)."""
    total = 0
    for i, ch in enumerate(first14):
        value = _CHARS.index(ch) * (2 if i % 2 else 1)
        total += value // 36 + value % 36
    return _CHARS[(36 - total % 36) % 36]


def validate(gstin: str) -> tuple[bool, str]:
    """(valid, reason). Upper-cases and strips; checks pattern, state code and check character."""
    value = (gstin or "").strip().upper()
    if not value:
        return False, "GSTIN is empty."
    if len(value) != 15:
        return False, "A GSTIN has 15 characters."
    if not _PATTERN.match(value):
        return False, "The GSTIN does not follow the format (state code, PAN, entity number, Z, check character)."
    if value[:2] not in STATE_CODES:
        return False, f"{value[:2]} is not a GST state code."
    if check_character(value[:14]) != value[14]:
        return False, "The GSTIN's check character is wrong (likely a typing mistake)."
    return True, ""


def state_code_of(gstin: str) -> str:
    value = (gstin or "").strip().upper()
    return value[:2] if len(value) >= 2 and value[:2] in STATE_CODES else ""


def state_code_for_name(name: str) -> str:
    return STATE_BY_NAME.get((name or "").strip().lower(), "")
