"""
Plain-language explanations of booking failure messages.

Booking attempts store the technical message the booking API produced ("Quota check failed: Individual Weekly
quota exceeded: current usage 120 min + requested 90 min = 210 min; configured limit 270 min; ..."). ``explain``
turns it into a short title and a sentence an Officer in Charge or the user can act on; the technical message is
still shown under "Technical details".
"""

from __future__ import annotations

import re
from typing import Optional

_MIN_QUOTA_RE = re.compile(
    r"(?P<scope>[A-Za-z ]*?)\s*(?P<period>weekly|monthly)\s+quota exceeded:\s*current usage\s*(?P<used>\d+)\s*min"
    r"\s*\+\s*requested\s*(?P<req>\d+)\s*min"
    r"(?:.*?(?:configured limit|limit|>)\s*(?P<limit>\d+)\s*min)?",
    re.IGNORECASE | re.DOTALL,
)
_COUNT_QUOTA_RE = re.compile(
    r"(?P<scope>[A-Za-z ]*?)\s*(?P<period>weekly|monthly)\s+booking-count quota exceeded:\s*(?P<total>\d+)\s*bookings vs limit\s*(?P<limit>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_CHARGE_QUOTA_RE = re.compile(
    r"(?P<scope>[A-Za-z ]*?)\s*(?P<period>weekly|monthly)\s+charge quota exceeded:\s*₹?\s*(?P<total>\d[\d,]*(?:\.\d+)?)\s*vs limit\s*₹?\s*(?P<limit>\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE,
)
_CHARGE_PROFILE_RE = re.compile(r"No active charge profile found for equipment \S+ and user type (?P<type>[^.]+)\.?", re.I)
_SHARED_RE = re.compile(r"shared across (?P<n>\d+) user", re.I)


def _whose(scope: str) -> str:
    s = (scope or "").strip().lower()
    if "faculty" in s:
        return "the supervisor's wallet group"
    if "external" in s:
        return "external users"
    return "this user"


def _money(text: str) -> str:
    try:
        value = float(str(text).replace(",", ""))
    except ValueError:
        return str(text)
    return f"{value:,.0f}" if value.is_integer() else f"{value:,.2f}"


def _clean(message: str) -> str:
    return " ".join(str(message or "").split())


def explain(message: Optional[str], *, outcome: str = "FAILED") -> dict:
    """
    {"code", "title", "message"} for a booking failure message. ``message`` is a full sentence; unknown messages
    that already read well are passed through, technical ones get a generic sentence.
    """
    raw = _clean(message)
    low = raw.lower()

    def out(code: str, title: str, text: str) -> dict:
        return {"code": code, "title": title, "message": text}

    if not raw:
        if str(outcome).upper() == "SUCCESS":
            return out("success", "Booking created", "The booking was created.")
        return out("unknown", "Booking failed", "No reason was recorded for this attempt.")

    m = _MIN_QUOTA_RE.search(raw)
    if m and "quota" in low:
        period = m.group("period").lower()
        used, req = int(m.group("used")), int(m.group("req"))
        whose = _whose(m.group("scope"))
        subject = "you had used" if whose == "this user" else f"{whose} had used"
        limit = m.group("limit")
        title = f"{period.capitalize()} booking limit reached"
        text = f"{title}: {subject} {used} min and requested {req} min"
        text += f"; the {period} limit is {int(limit)} min." if limit else "."
        shared = _SHARED_RE.search(raw)
        if shared and whose != "this user":
            text += f" The limit is shared by {shared.group('n')} users on the wallet."
        return out("quota_time", title, text)
    m = _COUNT_QUOTA_RE.search(raw)
    if m:
        period = m.group("period").lower()
        limit = m.group("limit").rstrip("0").rstrip(".") if "." in m.group("limit") else m.group("limit")
        title = f"{period.capitalize()} booking count limit reached"
        return out("quota_count", title,
                   f"{title}: this booking would make {m.group('total')} bookings; the {period} limit is {limit}.")
    m = _CHARGE_QUOTA_RE.search(raw)
    if m:
        period = m.group("period").lower()
        title = f"{period.capitalize()} spending limit reached"
        return out("quota_charge", title,
                   f"{title}: this booking would bring the total to ₹{_money(m.group('total'))}; "
                   f"the {period} limit is ₹{_money(m.group('limit'))}.")
    if "external weekly slot quota" in low:
        detail = raw.split("quota exceeded", 1)[-1].strip(" .:")
        text = "Weekly slot limit for external users reached on this equipment."
        if detail and detail.lower() != raw.lower():
            text += f" ({detail[:1].upper() + detail[1:]}.)"
        return out("quota_external_slots", "External weekly slot limit reached", text)
    if "quota" in low and ("exceed" in low or "limit" in low):
        period = "monthly" if "month" in low else "weekly"
        return out("quota", f"{period.capitalize()} booking limit reached",
                   f"The {period} booking limit for this equipment would be exceeded by this booking.")

    if "spending limit" in low or "spend limit" in low:
        return out("spending_limit", "Supervisor's spending limit reached",
                   f"The supervisor's spending limit for this student would be exceeded. {raw}".strip())
    if "insufficient" in low or "enough balance" in low or ("balance" in low and "wallet" in low):
        return out("wallet_balance", "Not enough wallet balance",
                   "The wallet this booking is charged to did not have enough balance."
                   + (f" {raw}" if raw.lower() not in ("insufficient wallet balance", "insufficient wallet balance.") else ""))
    if "wallet" in low and ("access" in low or "no wallet" in low or "not linked" in low or "joined" in low):
        return out("no_wallet", "No wallet linked",
                   "The user has no wallet the booking could be charged to (students need to be linked to their supervisor's wallet).")

    if "already been booked" in low or "already booked by another" in low or "all slots are occupied" in low:
        return out("slot_taken", "Slot already taken",
                   "The selected slot was booked by someone else just before this request was submitted.")
    if re.search(r"slots? \[[\d,\s]+\] (are|is) not available for booking", low) or "invalid or not available" in low:
        return out("slot_unavailable", "Slot no longer available",
                   "One or more of the selected slots were no longer free when the request was submitted.")
    if "no available slots found" in low:
        return out("slot_unavailable", "No free slots in the requested time",
                   "There were no free slots in the requested time range.")
    if "no alternative slots" in low:
        return out("no_alternative", "No alternative slot found",
                   "The selected slots were taken and no other free slots of the same length were available.")
    if "requested waitlist" in low:
        return out("waitlist_requested", "No free slots – joined the waitlist",
                   "There were no free slots this week, so the user asked to join the waitlist.")
    if "outside the allowed slot window" in low:
        return out("outside_window", "Outside booking hours",
                   "The selected slots fall outside the booking hours set for this equipment.")
    if "home / non-home" in low or "reserved for" in low or "department under" in low:
        text = raw if not re.search(r"slots? \[", low) else (
            "The selected slots are reserved for other departments under the slot reservation rules.")
        return out("department_reservation", "Slot reserved for other departments", text)
    if "another mode" in low:
        return out("mode_conflict", "Instrument busy in another mode",
                   "The instrument is already booked in another mode at an overlapping time.")
    if "mode is not scheduled" in low:
        return out("mode_unavailable", "Mode not available at that time",
                   "This mode of the instrument is not scheduled for booking at the selected time.")
    if "active exclusive mode" in low:
        return out("mode_unavailable", "Mode not available at that time",
                   "Another mode of the instrument is active at the selected time; only that mode can be booked then.")
    if "mode not available" in low or "mode is not available" in low:
        return out("mode_unavailable", "Mode not available on that date", raw)
    if "legacy_migration_slot_blocked" in low or "previous booking portal" in low:
        return out("legacy_block", "Slot held for a migrated booking",
                   "This slot is temporarily held for a booking carried forward from the previous booking portal.")
    if "peak" in low or ("external" in low and "pause" in low):
        return out("peak_window", "Booking paused at this time", raw)

    if "cannot be greater than" in low or "cannot be less than" in low:
        return out("input_limit", "An input is out of range", raw)
    if "sample set" in low and ("limit" in low or "maximum" in low or "formula" in low):
        return out("sample_set_limit", "Sample set limit", raw)
    if "invalid input values for calculation" in low or "invalid numeric value" in low or "error calculating" in low:
        return out("charge_error", "Charge could not be calculated",
                   "The time and charge could not be worked out from the inputs given (a value was missing or not a number).")
    m = _CHARGE_PROFILE_RE.search(raw)
    if m:
        return out("no_charge_profile", "No charges set for this user category",
                   f"No charges are set up on this equipment for the user's category ({m.group('type').strip()}), "
                   "so the booking could not be priced.")

    if "not operational" in low:
        return out("equipment_unavailable", "Equipment not operational", raw)
    if "on behalf" in low or "permission" in low or "not allowed to" in low or "only admin" in low:
        return out("permission", "Not permitted", raw)
    if "start_time and end_time" in low or "end_time must be after" in low or "invalid datetime" in low:
        return out("invalid_time", "Requested time missing or invalid",
                   "The requested start and end time were missing or invalid.")
    if "slot_ids must be" in low:
        return out("invalid_request", "Invalid request", "The selected slots were not sent correctly by the booking page.")
    if low.startswith("error creating booking"):
        return out("system_error", "System error",
                   "The booking could not be saved because of a system error. The IIC team can check the technical details.")

    return out("other", "Booking failed", raw if re.search(r"[a-z]{3,}", raw) else "The booking could not be completed.")
