"""Builds I-STEM request payloads from local data. Pure validation and formatting: no network, no writes.

Every mandatory value comes from real local data or an administrator-maintained mapping. When a value cannot be
established the field is reported in ``errors`` and nothing is substituted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation

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


def collect_public_user_profile(user) -> dict:
    """Local values for each Create User field; ``None`` where the local data model has no source.

    No local source exists for user_organisation, user_salutation, user_district or a separate personal address,
    so those stay ``None`` and Create User is blocked until a data source is agreed (no values are invented).
    """
    name = (user.name or "").strip()
    first, _, last = name.rpartition(" ")
    billing = getattr(user, "external_billing_profile", None)
    billing_address = None
    if billing is not None:
        parts = [billing.billing_address_line1, billing.billing_address_line2, billing.billing_city,
                 billing.billing_pincode, billing.billing_country]
        billing_address = ", ".join(p.strip() for p in parts if p and p.strip()) or None
    return {
        "user_first_name": first.strip() or None,
        "user_last_name": last.strip() or None,
        "user_email": (user.email or "").strip() or None,
        "user_contactno": _ten_digit_mobile(user.phone_number or ""),
        "user_organisation": None,
        "user_gender": LOCAL_GENDER_TO_ISTEM.get((user.gender or "").strip().lower()),
        "user_salutation": None,
        "user_address": None,
        "user_city": None,
        "user_district": None,
        "user_state": None,
        "user_country": None,
        "user_pin": None,
        "billing_user_name": (billing.billing_name or "").strip() or None if billing else None,
        "billing_address": billing_address,
    }


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
    user_type, error = resolve_code(CodeType.PUBLIC_USER_TYPE, user.user_type or "", allowed=contract.PUBLIC_USER_TYPES)
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
    if billing is None:
        for key in ("billing_user_name", "billing_address", "billing_state", "booking_gstn"):
            result.errors[key] = "the booking user has no billing profile"
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
        gstin = (billing.gstin or "").strip().upper()
        if not gstin:
            result.errors["booking_gstn"] = "GSTIN is empty (mandatory for Create Booking; I-STEM has not said empty is accepted)"
        elif not GSTIN.match(gstin):
            result.errors["booking_gstn"] = "GSTIN must be 15 letters/digits"
        else:
            p["booking_gstn"] = gstin

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
    if profile_type not in ("SAMPLE", "HOUR"):
        result.errors["eq_rate"] = f"no single rate for '{profile_type}' charge profiles"
    else:
        rate = _whole_number(getattr(profile, "primary_unit_charge", None))
        if rate is None:
            result.errors["eq_rate"] = "unit rate is not a whole rupee amount (I-STEM expects an integer)"
        else:
            p["eq_rate"] = rate

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

def _final_amount(booking) -> tuple[str | None, str | None]:
    """(amount, error). ``Booking.total_charge`` of the completed booking exactly as recorded locally (net of reward
    discount, including any return-shipping fee), never recalculated, estimated or rounded. Refused while a charge
    recalculation is pending. Whether this is the amount I-STEM expects is a business decision still to be confirmed."""
    pending = booking.charge_recalculation_pending_amount
    if pending is not None and Decimal(pending) != 0:
        return None, "a charge recalculation is still pending, so the final amount is not settled"
    try:
        amount = Decimal(str(booking.total_charge))
    except (InvalidOperation, TypeError):
        return None, "the booking has no total charge"
    if amount < 0 or amount != amount.quantize(Decimal("0.01")):
        return None, "the total charge is negative or has more than two decimal places"
    return format(amount.quantize(Decimal("0.01")), "f"), None


def build_execute_payload(booking, link) -> MappingResult:
    """FBR Execute: request_id + confirmation_id from the link, completion data from the completed local booking.

    Dates are IST calendar dates (YYYY-MM-DD). allocation_date is "the date the equipment/slot was actually allocated
    to the user" [SPEC]: the IST date of the first allocated local slot (the slot sent as allocated_timings_from).
    completion_date is the IST date of ``Booking.completed_at``. Neither may be in the future, and the service cannot
    have completed before its slot date.
    """
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
    updated_by = conf.execute_updated_by()
    if updated_by:
        if re.fullmatch(r"[A-Za-z0-9_ ]+", updated_by):
            p["updated_by"] = updated_by
        else:
            result.errors["updated_by"] = "ISTEM_EXECUTE_UPDATED_BY has unsupported characters"
    return result
