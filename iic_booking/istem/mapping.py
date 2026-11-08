"""Builds I-STEM request payloads from local data. Pure validation and formatting: no network, no writes.

Every mandatory value comes from real local data or an administrator-maintained mapping. When a value cannot be
established the field is reported in ``errors`` and nothing is substituted (exceptions: the GSTIN placeholder I-STEM
accepts where no GSTIN applies, and Create User's institute address for internal users and city as district).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.utils import timezone

from iic_booking.users.display import get_user_display_name

from . import conf, contract
from .choices import CodeType
from .models import IstemCodeMapping, IstemEquipmentMapping

ALPHA_SPACES = re.compile(r"^[A-Za-z][A-Za-z ]*$")
GSTIN = re.compile(r"^[0-9A-Z]{15}$")
ALL_KEYS = "*"


@dataclass
class MappingResult:
    payload: dict = field(default_factory=dict)
    errors: dict = field(default_factory=dict)  # field -> actionable reason

    @property
    def ok(self) -> bool:
        return not self.errors

    def missing_fields(self) -> list[str]:
        return sorted(self.errors)


def ist(value):
    """An aware datetime in IST (I-STEM's time zone), whatever the server's TIME_ZONE."""
    return timezone.localtime(value, contract.IST)


def ist_today():
    return ist(timezone.now()).date()


def format_booking_datetime(value) -> str:
    return ist(value).strftime(contract.BOOKING_DATETIME_FORMAT)


def istem_hour_window(start, end, shift_hours: int = 0):
    """The I-STEM slots for a booking: I-STEM reserves one-hour slots on a 24-hour grid from 00:00 IST, while IIC slots
    vary (30 minutes, 1.5 hours, ...). From the start of the hour the booking begins in to the end of the hour it ends
    in (10:30-12:00 -> 10:00-12:00; 09:00-09:30 -> 09:00-10:00), moved ``shift_hours`` later when I-STEM reported the
    first choice occupied."""
    step = timedelta(hours=contract.ISTEM_SLOT_HOURS)
    first = ist(start).replace(minute=0, second=0, microsecond=0)
    last = ist(end)
    last_hour = last.replace(minute=0, second=0, microsecond=0)
    if last_hour < last:
        last_hour += step
    return first + shift_hours * step, last_hour + shift_hours * step


def istem_window_fits_day(start, end, shift_hours: int) -> bool:
    """True while the shifted window still ends by midnight of the day the booking starts (I-STEM's day)."""
    first, last = istem_hour_window(start, end, shift_hours)
    midnight = ist(start).replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return last <= midnight


def format_execute_date(value) -> str:
    return ist(value).strftime(contract.FBR_EXECUTE_DATE_FORMAT)


def resolve_code(code_type: str, local_key: str, *, equipment=None, allowed=None, equipment_only: bool = False):
    """(value, error) for a code mapping: equipment override, then global key, then global ``*``."""
    qs = IstemCodeMapping.objects.filter(code_type=code_type, is_active=True)
    candidates = []
    if equipment is not None:
        candidates += [qs.filter(equipment=equipment, local_key=local_key), qs.filter(equipment=equipment, local_key=ALL_KEYS)]
    if not equipment_only:
        candidates += [qs.filter(equipment__isnull=True, local_key=local_key), qs.filter(equipment__isnull=True, local_key=ALL_KEYS)]
    for candidate in candidates:
        row = candidate.first()
        if row is not None:
            value = (row.istem_value or "").strip()
            if allowed is not None and value not in allowed:
                return None, f"mapping {code_type}/{row.local_key} has '{value}', which is not an accepted I-STEM value"
            return value, None
    scope = f" for equipment {getattr(equipment, 'code', equipment)}" if equipment is not None else ""
    return None, f"no active {code_type} mapping for '{local_key}'{scope}"


def resolve_equipment_mapping(equipment):
    """(mapping, error). The mapping must exist, be active, be verified and hold a numeric I-STEM equipment_id."""
    mapping = IstemEquipmentMapping.objects.filter(equipment=equipment).first()
    if mapping is None:
        return None, (f"equipment {equipment.code} has no I-STEM equipment mapping (add it in the admin, then check it with "
                      "'istem_recover verify-equipment')")
    if not mapping.is_active:
        return None, f"the I-STEM mapping for equipment {equipment.code} is inactive"
    if not mapping.verified_at:
        return None, f"the I-STEM mapping for equipment {equipment.code} has not been verified by an administrator"
    if not str(mapping.istem_equipment_id or "").strip().isdigit():
        return None, f"the I-STEM equipment_id for equipment {equipment.code} is not a valid numeric ID"
    return mapping, None


# --- Create User ----------------------------------------------------------------------------------------------------

LOCAL_GENDER_TO_ISTEM = {"male": "Male", "female": "Female", "other": "Other"}


def _ten_digit_mobile(raw: str) -> str | None:
    digits = re.sub(r"[\s-]", "", raw or "")
    if digits.startswith("+91") and len(digits) == 13:
        digits = digits[3:]
    return digits if re.fullmatch(r"[0-9]{10}", digits) else None


# I-STEM Create User accepts exactly these salutations (staging, 8 Nov 2026: "Invalid salutation. Must be Mr., Ms.,
# Dr., or Prof."). Mrs and Miss become Ms.; there is none for gender Other, so such a user needs a title in their name.
ISTEM_SALUTATIONS = ("Mr.", "Ms.", "Dr.", "Prof.")
_TITLES = {"dr": "Dr.", "prof": "Prof.", "professor": "Prof.", "mr": "Mr.", "mrs": "Ms.", "ms": "Ms.", "miss": "Ms."}
_GENDER_SALUTATION = {"Male": "Mr.", "Female": "Ms."}

# Create User ``user_type`` when no PUBLIC_USER_TYPE code mapping is set ([USER 2026-11-08] linking is automatic, so
# every portal user type needs a value). An administrator mapping always wins.
DEFAULT_PUBLIC_USER_TYPES = {
    "student": "Academic User", "individual_student": "Academic User", "faculty": "Academic User",
    "external": "Academic User", "RND": "National Research Lab User", "Industry": "Industry User",
    "startup_incubated_iitr": "Startup/MSME User", "external_startup_msme": "Startup/MSME User",
    "other": "Industry User",
}


def _split_name(raw: str) -> tuple[str | None, str | None, str | None]:
    """(salutation from a leading title, first name, last name). "Dr. Asha Verma" -> ("Dr.", "Asha", "Verma")."""
    words = [w for w in re.split(r"\s+", (raw or "").strip()) if w]
    title = None
    while words and words[0].rstrip(".").lower() in _TITLES:
        title = title or _TITLES[words.pop(0).rstrip(".").lower()]
    if len(words) < 2:
        return title, (words[0] if words else None), None
    return title, " ".join(words[:-1]), words[-1]


def _salutation(user, title: str | None, gender: str | None) -> str | None:
    if title:
        return title
    designation = (getattr(user, "designation", "") or "").lower()
    if "prof" in designation:
        return "Prof."
    if (user.user_type or "") == "faculty" or "scientist" in designation:
        return "Dr."
    return _GENDER_SALUTATION.get(gender or "")


def _is_internal(user) -> bool:
    from iic_booking.users.models.user_type import UserType

    return UserType.is_internal_user(user.user_type or "")


def _organisation(user, billing) -> str | None:
    if _is_internal(user):
        return conf.institute_address()["organisation"] or None
    department = getattr(user, "department", None)
    for candidate in (getattr(department, "name", ""), getattr(getattr(user, "organization_request", None), "name", ""),
                      getattr(billing, "billing_name", "") if billing else ""):
        if (candidate or "").strip():
            return candidate.strip()
    return None


def collect_public_user_profile(user) -> dict:
    """Local values for each Create User field; ``None`` where the profile has no value (the user is then told what to
    complete). [USER 2026-11-08] the portal creates or finds the I-STEM user itself, so every field has a source:
    external users' address from their billing profile (the city doubles as the district, which the profile does not
    hold); internal users the institute's address (``conf.institute_address``) and, without a billing profile of their
    own, the institute's billing identity as in Create Booking."""
    title, first, last = _split_name(user.name or "")
    gender = LOCAL_GENDER_TO_ISTEM.get((user.gender or "").strip().lower())
    billing = getattr(user, "external_billing_profile", None)
    profile = {
        "user_first_name": first, "user_last_name": last, "user_email": (user.email or "").strip() or None,
        "user_contactno": _ten_digit_mobile(user.phone_number or ""), "user_organisation": _organisation(user, billing),
        "user_gender": gender, "user_salutation": _salutation(user, title, gender),
        "user_address": None, "user_city": None, "user_district": None, "user_state": None, "user_country": None,
        "user_pin": None, "billing_user_name": None, "billing_address": None,
    }
    if billing is not None:
        line = ", ".join(p.strip() for p in (billing.billing_address_line1, billing.billing_address_line2) if p and p.strip())
        city = (billing.billing_city or "").strip()
        parts = [billing.billing_address_line1, billing.billing_address_line2, billing.billing_city,
                 billing.billing_pincode, billing.billing_country]
        profile.update({
            "user_address": line or None, "user_city": city or None, "user_district": city or None,
            "user_state": (billing.billing_state or "").strip() or None,
            "user_country": (billing.billing_country or "").strip() or "India",
            "user_pin": (billing.billing_pincode or "").strip() or None,
            "billing_user_name": (billing.billing_name or "").strip() or None,
            "billing_address": ", ".join(p.strip() for p in parts if p and p.strip()) or None,
        })
    elif _is_internal(user):
        institute = conf.institute_address()
        billing_identity = conf.internal_billing()
        profile.update({
            "user_address": institute["address"] or None, "user_city": institute["city"] or None,
            "user_district": institute["district"] or None, "user_state": institute["state"] or None,
            "user_country": institute["country"] or None, "user_pin": institute["pin"] or None,
            "billing_user_name": billing_identity["billing_user_name"] or None,
            "billing_address": billing_identity["billing_address"] or None,
        })
    return profile


def public_user_type(user) -> tuple[str | None, str | None]:
    """(Create User user_type, error): the administrator's PUBLIC_USER_TYPE mapping, else the built-in default."""
    value, error = resolve_code(CodeType.PUBLIC_USER_TYPE, user.user_type or "", allowed=contract.PUBLIC_USER_TYPES)
    if error and error.startswith("no active"):
        if (user.user_type or "") in DEFAULT_PUBLIC_USER_TYPES:
            return DEFAULT_PUBLIC_USER_TYPES[user.user_type], None
        if _is_internal(user):
            return "Academic User", None
    return value, error


def build_public_user_payload(user, institute_id: str | None) -> MappingResult:
    """``institute_id`` is the one returned with the integration's access token (``IstemClient.session_info``)."""
    result = MappingResult()
    profile = collect_public_user_profile(user)
    for key, value in profile.items():
        if value in (None, ""):
            result.errors[key] = "no local value (field required by Create User)"
        else:
            result.payload[key] = str(value)
    for key in ("user_first_name", "user_last_name", "billing_user_name"):
        if key in result.payload and not ALPHA_SPACES.match(result.payload[key]):
            result.errors[key] = "must contain alphabets and spaces only (SOP)"
    if "user_pin" in result.payload and not re.fullmatch(r"[0-9]{6}", result.payload["user_pin"]):
        result.errors["user_pin"] = "must be 6 digits (SOP)"
    if "user_gender" in result.payload and result.payload["user_gender"] not in contract.USER_GENDERS:
        result.errors["user_gender"] = "must be Male/Female/Other"
    user_type, error = public_user_type(user)
    if error:
        result.errors["user_type"] = error
    else:
        result.payload["user_type"] = user_type
    if not str(institute_id or "").isdigit():
        result.errors["institute_id"] = "the institute_id is not known (it comes from I-STEM's token response)"
    else:
        result.payload["institute_id"] = str(institute_id)
    result.payload["user_status"] = "1"
    return result


# --- Create Booking -------------------------------------------------------------------------------------------------

def _whole_number(value) -> str | None:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None
    if d < 0 or d != d.to_integral_value():
        return None
    return str(int(d))


def _add_eq_rate(p: dict, errors: dict, profile_type: str, profile) -> None:
    """eq_rate, the unit rate I-STEM asks for. Per-sample and per-hour profiles have one: their primary unit charge.
    Other profiles (formula, multi-parameter, sample + element) price a booking as a whole, so the rate is the booking
    total divided by the number of samples ([USER 2026-11-08] rule (a)), rounded to a whole rupee as I-STEM expects
    integers; the total sent stays the booking's exact total."""
    if profile_type in ("SAMPLE", "HOUR"):
        rate = _whole_number(getattr(profile, "primary_unit_charge", None))
        if rate is None:
            errors["eq_rate"] = "unit rate is not a whole rupee amount (I-STEM expects an integer)"
        else:
            p["eq_rate"] = rate
        return
    total, count = p.get("total_est_amt"), p.get("booking_no_sample")
    if total is None or count is None:
        errors["eq_rate"] = "needs the booking total and the sample count (booking total ÷ samples)"
        return
    rate = (Decimal(total) / Decimal(count)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    p["eq_rate"] = str(int(rate))


def booking_slot_window(booking):
    slots = list(booking.daily_slots.order_by("start_datetime").values_list("start_datetime", "end_datetime"))
    if not slots:
        return None, None
    return slots[0][0], max(end for _, end in slots)


def _add_descriptive_fields(booking, p: dict, minutes: int) -> None:
    """The staging spec's remaining Create Booking fields ([STAGING]: without them the later confirmation is recorded
    without an amount and never listed). Neutral values where the portal holds nothing more specific."""
    na = contract.NOT_APPLICABLE
    p["remarks"] = p.get("booking_title") or na
    name = get_user_display_name(booking.user) if booking.user_id else ""
    p["booking_by"] = name if name and ALPHA_SPACES.match(name) else na
    if minutes > 0:
        p["req_unit"] = f"{minutes // 60}:{minutes % 60:02d}"
    for key in ("bk_natsamp", "bk_tecinf", "bk_speinf", "background_of_work", "specific_information", "sample_name"):
        p[key] = na
    if p.get("booking_gstn"):
        p["gstin"] = p["booking_gstn"]
    p["addl_amt"] = "0"
    p["exclusive_use"] = "No"
    p["consumables_provided by"] = contract.CONSUMABLES_BY_FACILITY
    p["consumables_provided_by"] = contract.CONSUMABLES_BY_FACILITY
    p["sample_rows"] = "[]"


def is_internal_booking(booking) -> bool:
    """Booked under an internal (IIT Roorkee) user type, as recorded on the booking, else the user's current type."""
    from iic_booking.users.models.user_type import UserType

    user_type = booking.user_type_snapshot or getattr(booking.user, "user_type", "") or ""
    return UserType.is_internal_user(user_type)


def _add_gstin(p: dict, errors: dict, raw) -> None:
    """booking_gstn: a valid GSTIN, or the placeholder when none applies ([TEAM 2026-10-30]: only non-empty is
    required). A malformed GSTIN is still refused rather than replaced."""
    gstin = (raw or "").strip().upper()
    if not gstin:
        placeholder = conf.gstin_placeholder()
        if placeholder:
            p["booking_gstn"] = placeholder
        else:
            errors["booking_gstn"] = "GSTIN is empty and ISTEM_GSTIN_PLACEHOLDER is not set"
    elif not GSTIN.match(gstin):
        errors["booking_gstn"] = "GSTIN must be 15 letters/digits"
    else:
        p["booking_gstn"] = gstin


def _add_internal_billing(p: dict, errors: dict) -> None:
    """Internal bookings are billed to the institute ([TEAM 2026-10-30]); the user stays the public_user_id."""
    institute = conf.internal_billing()
    name = institute["billing_user_name"]
    if not name or not ALPHA_SPACES.match(name):
        errors["billing_user_name"] = "ISTEM_INTERNAL_BILLING_NAME must be set (alphabets and spaces only)"
    else:
        p["billing_user_name"] = name
    for key, setting in (("billing_address", "ISTEM_INTERNAL_BILLING_ADDRESS"), ("billing_state", "ISTEM_INTERNAL_BILLING_STATE")):
        if institute[key]:
            p[key] = institute[key]
        else:
            errors[key] = f"{setting} is not set"
    _add_gstin(p, errors, institute["gstin"])


def build_create_booking_payload(booking, user_link) -> MappingResult:
    """The 19 fields I-STEM confirmed as mandatory. Optional fields are omitted (permitted by I-STEM)."""
    result = MappingResult()
    p = result.payload
    equipment = booking.equipment
    profile = booking.charge_profile
    profile_type = getattr(profile, "profile_type", "") or ""

    mapping, error = resolve_equipment_mapping(equipment)
    if error:
        result.errors["equipment_id"] = error
    else:
        p["equipment_id"] = str(mapping.istem_equipment_id).strip()

    if user_link is None or user_link.state != "LINKED" or not str(user_link.public_user_id or "").isdigit():
        result.errors["public_user_id"] = "the booking user is not linked to an I-STEM public user"
    else:
        p["public_user_id"] = str(user_link.public_user_id)

    start, end = booking_slot_window(booking)
    if not start or not end or end <= start:
        result.errors["preferred_timings_from"] = "the booking has no allocated slot window"
    else:
        first, last = istem_hour_window(start, end)
        p["preferred_timings_from"] = format_booking_datetime(first)
        p["preferred_timings_to"] = format_booking_datetime(last)

    for code_type, field_name, key, allowed in (
        (CodeType.SERVICE_TYPE, "service_type", profile_type, contract.SERVICE_TYPES),
        (CodeType.EQUIPMENT_UOM, "equipment_uom", profile_type, contract.EQUIPMENT_UOMS),
        (CodeType.USAGE_TYPE, "usage_type", booking.user_type_snapshot or "", contract.BOOKING_USER_TYPES),
        (CodeType.USER_TYPE, "user_type", booking.user_type_snapshot or "", contract.BOOKING_USER_TYPES),
    ):
        value, error = resolve_code(code_type, key, equipment=equipment, allowed=allowed)
        if error:
            result.errors[field_name] = error
        else:
            p[field_name] = value
    # analysis_type values are defined per equipment by I-STEM: only an equipment-specific mapping is accepted.
    value, error = resolve_code(CodeType.ANALYSIS_TYPE, profile_type, equipment=equipment, equipment_only=True)
    if error:
        result.errors["analysis_type"] = error + " (analysis_type is equipment-specific)"
    elif not value:
        result.errors["analysis_type"] = "the equipment's analysis_type mapping is empty"
    else:
        p["analysis_type"] = value

    billing = getattr(booking.user, "external_billing_profile", None)
    if billing is None and is_internal_booking(booking):
        # An internal user with a billing profile of their own (e.g. an incubated startup) is billed to it instead.
        _add_internal_billing(p, result.errors)
    elif billing is None:
        for key in ("billing_user_name", "billing_address", "billing_state"):
            result.errors[key] = "the booking user has no billing profile"
        if conf.gstin_placeholder():
            p["booking_gstn"] = conf.gstin_placeholder()
        else:
            result.errors["booking_gstn"] = "the booking user has no billing profile"
    else:
        name = (billing.billing_name or "").strip()
        if not name:
            result.errors["billing_user_name"] = "billing name is empty"
        elif not ALPHA_SPACES.match(name):
            result.errors["billing_user_name"] = "billing name must contain alphabets and spaces only (SOP)"
        else:
            p["billing_user_name"] = name
        if not (billing.billing_address_line1 or "").strip() or not (billing.billing_city or "").strip() or not (billing.billing_pincode or "").strip():
            result.errors["billing_address"] = "billing address line 1, city and pincode are required"
        else:
            parts = [billing.billing_address_line1, billing.billing_address_line2, billing.billing_city,
                     billing.billing_pincode, billing.billing_country]
            p["billing_address"] = ", ".join(x.strip() for x in parts if x and x.strip())
        state = (billing.billing_state or "").strip()
        if not state:
            result.errors["billing_state"] = "billing state is empty"
        else:
            p["billing_state"] = state
        _add_gstin(p, result.errors, billing.gstin)

    p["request_date"] = format_booking_datetime(booking.created_at)
    vid = (booking.virtual_booking_id or "").strip()
    if not vid:
        result.errors["booking_title"] = "the booking has no booking ID to build a title from"
    else:
        p["booking_title"] = f"{vid} - {equipment.name}".strip()

    total = _whole_number(booking.total_charge)
    if total is None:
        result.errors["total_est_amt"] = "total charge is not a whole rupee amount (I-STEM expects an integer; no rounding rule agreed)"
    else:
        p["total_est_amt"] = total
    key = (mapping.sample_count_input_key or "").strip() if mapping else ""
    if not key:
        result.errors["booking_no_sample"] = "the equipment mapping does not name the input field holding the sample count"
    else:
        raw = (booking.input_values or {}).get(key)
        count = _whole_number(raw) if raw not in (None, "") else None
        if not count or int(count) < 1:
            result.errors["booking_no_sample"] = f"input field {key} does not hold a positive whole sample count"
        else:
            p["booking_no_sample"] = count

    _add_eq_rate(p, result.errors, profile_type, profile)

    minutes = booking.total_time_minutes or 0
    if minutes <= 0:
        result.errors["working_hrs"] = "the booking has no working time"
    else:
        hours = (Decimal(minutes) / Decimal(60)).quantize(Decimal("0.01"))
        p["working_hrs"] = format(hours.normalize(), "f")

    if vid:
        # [STAGING] Without a non-empty booking_remark I-STEM creates nothing (new_service_id 0).
        p["booking_remark"] = f"IIC booking {vid}"
    _add_descriptive_fields(booking, p, minutes)

    missing = [f for f in contract.CREATE_BOOKING_MANDATORY_FIELDS if f not in p and f not in result.errors]
    for f in missing:
        result.errors[f] = "not produced by the mapping layer"
    return result


def build_confirm_payload(booking, link, *, approved_on, booking_payload: dict | None = None) -> MappingResult:
    """Confirm Booking: the five fields the spec requires, on top of the booking's Create Booking payload and
    booking_status "Slot Allocated". [STAGING] Only a confirmation that re-sends the full booking is recorded with its
    amount and listed with its confirmation_id; [SPEC] ignores the extra fields.

    ``booking_payload`` is the current, unmasked Create Booking payload (``link.create_payload`` is stored redacted and
    must never be sent); without it only the five fields are built.

    The allocated window is the local booking's slot window, and it must equal the window requested by Create Booking
    (``link.create_payload``): a different window means the booking moved and must be reconciled first.
    ``approved_on`` is fixed by the caller (the confirmation request time) so a retry sends identical parameters.
    """
    result = MappingResult()
    p = result.payload
    request_id = str(link.request_id or "").strip()
    if not request_id.isdigit():
        result.errors["request_id"] = "no I-STEM request_id (new_service_id) on the link"
    else:
        p["request_id"] = request_id
    approver = conf.confirm_approved_by()
    if not approver:
        result.errors["approved_by"] = "ISTEM_CONFIRM_APPROVED_BY is not set"
    elif not re.fullmatch(r"[A-Za-z0-9_ ]+", approver):
        result.errors["approved_by"] = "approver name has unsupported characters"
    else:
        p["approved_by"] = approver
    if approved_on is None:
        result.errors["approved_on"] = "no confirmation request time"
    else:
        p["approved_on"] = format_booking_datetime(approved_on)
    start, end = booking_slot_window(booking)
    if not start or not end or end <= start:
        result.errors["allocated_timings_from"] = "the booking has no allocated slot window"
    else:
        shift = link.istem_slot_shift_hours or 0
        first, last = istem_hour_window(start, end, shift)
        p["allocated_timings_from"] = format_booking_datetime(first)
        p["allocated_timings_to"] = format_booking_datetime(last)
        requested = link.create_payload or {}
        unshifted = [format_booking_datetime(t) for t in istem_hour_window(start, end)]
        for key, sent in (("preferred_timings_from", unshifted[0]), ("preferred_timings_to", unshifted[1])):
            if requested.get(key) and requested[key] != sent:
                result.errors["allocated_timings_from"] = (
                    "the local slot window differs from the window sent with Create Booking; reconcile the booking first"
                )
    if result.errors:
        result.payload = {}
        return result
    if booking_payload:
        # The confirm-specific values win over anything with the same name in the Create Booking payload.
        created = {k: v for k, v in booking_payload.items() if k not in p}
        result.payload = {**created, **p, "booking_status": contract.CONFIRM_BOOKING_STATUS}
    return result


# --- FBR Execute ----------------------------------------------------------------------------------------------------

def _money(value) -> tuple[str | None, str | None]:
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None, "the amount is not a number"
    if amount < 0 or amount != amount.quantize(Decimal("0.01")):
        return None, "the amount is negative or has more than two decimal places"
    return format(amount.quantize(Decimal("0.01")), "f"), None


def _final_amount(booking) -> tuple[str | None, str | None]:
    """(amount, error). ``Booking.total_charge`` of the completed booking exactly as recorded locally (net of reward
    discount, including any return-shipping fee), never recalculated, estimated or rounded. Refused while a charge
    recalculation is pending. ₹0 is valid ([TEAM 2026-10-30]: fully grant-funded bookings)."""
    pending = booking.charge_recalculation_pending_amount
    if pending is not None and Decimal(pending) != 0:
        return None, "a charge recalculation is still pending, so the final amount is not settled"
    if booking.total_charge is None:
        return None, "the booking has no total charge"
    return _money(booking.total_charge)


def build_execute_payload(booking, link) -> MappingResult:
    """FBR Execute: request_id + confirmation_id from the link, completion data from the completed local booking.

    Dates are IST calendar dates (YYYY-MM-DD). allocation_date is "the date the equipment/slot was actually allocated
    to the user" [SPEC]: the IST date of the first allocated local slot (the slot sent as allocated_timings_from).
    completion_date is the IST date of ``Booking.completed_at``. Neither may be in the future, and the service cannot
    have completed before its slot date.

    A late cancellation that kept part of the fee (``link.retained_amount`` set) is closed instead of cancelled
    ([TEAM 2026-10-30]); see ``_build_retention_payload``.
    """
    if link.retained_amount is not None:
        return _build_retention_payload(booking, link)
    result = MappingResult()
    p = result.payload
    request_id = str(link.request_id or "").strip()
    if not request_id.isdigit():
        result.errors["request_id"] = "no I-STEM request_id (new_service_id) on the link"
    else:
        p["request_id"] = request_id
    confirmation_id = str(link.confirmation_id or "").strip()
    if not confirmation_id.isdigit():
        result.errors["confirmation_id"] = "no confirmation_id on the link (Confirm Booking / Get Bookings)"
    else:
        p["confirmation_id"] = confirmation_id
    today = ist_today()
    completed = ist(booking.completed_at).date() if booking.status == "COMPLETED" and booking.completed_at else None
    if completed is None:
        result.errors["completion_date"] = f"the booking is not completed (status {booking.status})"
    elif completed > today:
        result.errors["completion_date"] = "completion_date is in the future"
    else:
        p["completion_date"] = format_execute_date(booking.completed_at)
    start, _ = booking_slot_window(booking)
    allocated = ist(start).date() if start else None
    if allocated is None:
        result.errors["allocation_date"] = "the booking has no allocated slot window"
    elif allocated > today:
        result.errors["allocation_date"] = "allocation_date is in the future (I-STEM refuses future allocation dates)"
    elif completed is not None and completed < allocated:
        result.errors["allocation_date"] = "the booking was completed before its allocated slot date; check the booking"
    else:
        p["allocation_date"] = format_execute_date(start)
    amount, error = _final_amount(booking)
    if error:
        result.errors["final_amount"] = error
    else:
        p["final_amount"] = amount
    _add_updated_by(p, result.errors)
    return result


def _add_updated_by(p: dict, errors: dict) -> None:
    updated_by = conf.execute_updated_by()
    if updated_by:
        if re.fullmatch(r"[A-Za-z0-9_ ]+", updated_by):
            p["updated_by"] = updated_by
        else:
            errors["updated_by"] = "ISTEM_EXECUTE_UPDATED_BY has unsupported characters"


def _build_retention_payload(booking, link) -> MappingResult:
    """FBR Execute closing a cancelled booking for the fee it kept. final_amount is ``link.retained_amount`` as an
    administrator recorded it (the portal does not compute deductions yet); completion_date is the cancellation date
    (``link.retained_on``); allocation_date is the slot date, or the cancellation date when the slot was still ahead
    (I-STEM refuses future allocation dates and a completion before allocation)."""
    result = MappingResult()
    p = result.payload
    if booking.status != "CANCELLED":
        result.errors["final_amount"] = f"a retained amount is recorded but the booking is {booking.status}, not cancelled"
    for key in ("request_id", "confirmation_id"):
        value = str(getattr(link, key) or "").strip()
        if value.isdigit():
            p[key] = value
        else:
            result.errors[key] = f"no {key} on the link"
    cancelled = link.retained_on
    if cancelled is None:
        result.errors["completion_date"] = "the cancellation date is not recorded"
    elif cancelled > ist_today():
        result.errors["completion_date"] = "the cancellation date is in the future"
    else:
        p["completion_date"] = cancelled.strftime(contract.FBR_EXECUTE_DATE_FORMAT)
        start, _ = booking_slot_window(booking)
        allocated = min(ist(start).date(), cancelled) if start else cancelled
        p["allocation_date"] = allocated.strftime(contract.FBR_EXECUTE_DATE_FORMAT)
    amount, error = _money(link.retained_amount)
    if error:
        result.errors["final_amount"] = error
    elif Decimal(amount) == 0:
        result.errors["final_amount"] = "nothing was retained: a full refund is cancelled on I-STEM, not executed"
    else:
        p["final_amount"] = amount
    _add_updated_by(p, result.errors)
    return result
