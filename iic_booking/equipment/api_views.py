"""API views for Equipment models."""

import io
import html
import logging
import mimetypes
import os
import re
import threading
import time
import zipfile
from datetime import date, datetime, timedelta
from decimal import Decimal

from django.conf import settings
from django.db import connection, transaction, IntegrityError
from django.db.models.deletion import ProtectedError, RestrictedError
from django.db.transaction import TransactionManagementError
from django.http import HttpResponse, HttpResponseRedirect
from django.db.models import Q, Min, Max, Sum, Count, Subquery, OuterRef, DecimalField, Exists, Prefetch
from django.utils import timezone
from django.utils.dateparse import parse_date as parse_date_iso
from django.core.files.storage import default_storage

from .image_utils import get_equipment_image_storage_path, open_equipment_image_bytes
from rest_framework import status
from rest_framework import serializers as drf_serializers
from rest_framework.decorators import api_view, permission_classes, parser_classes, authentication_classes
from rest_framework.parsers import MultiPartParser, FormParser, JSONParser
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema, inline_serializer, OpenApiParameter

from .models import (
    BookingDisruptionKind,
    Equipment,
    EquipmentAccessory,
    EquipmentAdditionalAccessory,
    EquipmentCategory,
    EquipmentGroup,
    EquipmentProfileType,
    EquipmentStatus,
    ChargeProfile,
    ChargeProfilePricingProfile,
    DailySlot,
    Booking,
    BookingResultFile,
    BookingStatus,
    IstemFbrStatus,
    initial_istem_fbr_fields_for_charge_profile,
    get_equipment_istem_portal_url,
    charge_profile_requires_istem_fbr,
    RepeatSampleRequest,
    RepeatSampleRequestStatus,
    BookingSampleTrace,
    BookingSampleTraceReplyAttachment,
    SampleTraceStatus,
    SlotStatus,
    BookingEvent,
    BookingEventType,
    Holiday,
    DynamicInputField,
    DynamicInputFieldType,
    BookingCancellationRequest,
    BookingCancellationRequestStatus,
    SlotMaster,
    NoSlotAllocationLog,
    UrgentBookingRequest,
    UrgentBookingRequestStatus,
    UrgentBookingRequestType,
    BookingAttemptLog,
    BookingAttemptOutcome,
    WaitlistEntry,
    EquipmentManager,
    EquipmentOperator,
    EquipmentTemporaryOIC,
    OperatorLeaveRequest,
    EquipmentOperatorCoverage,
    CalendarColorSetting,
    InternalUserSlotWindowSetting,
    BookingChargeSetting,
    UrgentHoldExpiryConfig,
    ICPMSStandardSample,
    Semester,
    StudentEquipmentNomination,
    StudentEquipmentNominationStatus,
    EquipmentOperatingTACall,
    EquipmentOperatingTACallStatus,
    TAAssignment,
    TAAssignmentStatus,
    TARewardConfig,
    TADutyLog,
    TADutyLogStatus,
    TARewardLedger,
    TARewardLedgerEntryType,
    TARewardLedgerSourceType,
    BookingRewardRedemption,
    InventoryItem,
    EquipmentInventoryItem,
    EquipmentItemStock,
    InventoryRequest,
    InventoryRequestLine,
    InventoryRequestStatus,
    InventoryTransaction,
    InventoryTransactionType,
    IssuedAsset,
    ProcurementRequest,
    ProcurementRequestLine,
    ProcurementAttachment,
    ProcurementActionLog,
    ProcurementRequestStatus,
    ProcurementHeadApprovalMode,
    ProcurementAttachmentType,
    ProcurementActionType,
    EquipmentAMCContract,
    EquipmentExpense,
    EquipmentExpenseType,
    EquipmentWriteOffRequest,
    EquipmentWriteOffStatus,
    EquipmentWriteOffActionLog,
    EquipmentWriteOffActionType,
    PrintMaterial,
)
from .serializers import (
    EquipmentListSerializer,
    EquipmentListLiteSerializer,
    EquipmentDetailSerializer,
    EquipmentCategorySerializer,
    EquipmentGroupSerializer,
    DailySlotSerializer,
    BookingSerializer,
    BookingListSerializer,
    BookingEventSerializer,
    BookingSampleTraceSerializer,
    BookingCancellationRequestSerializer,
    RepeatSampleRequestSerializer,
    TARewardConfigSerializer,
    TAAssignmentSerializer,
    TADutyLogSerializer,
    TARewardLedgerSerializer,
    AdminRewardAdjustSerializer,
    InventoryItemSerializer,
    EquipmentInventoryItemSerializer,
    EquipmentItemStockSerializer,
    InventoryRequestSerializer,
    InventoryRequestLineSerializer,
    InventoryTransactionSerializer,
    IssuedAssetSerializer,
    ProcurementRequestSerializer,
    ProcurementRequestLineSerializer,
    EquipmentLifecycleFieldsSerializer,
    EquipmentAMCContractSerializer,
    EquipmentExpenseSerializer,
    EquipmentWriteOffRequestSerializer,
    EquipmentAccessorySerializer,
    EquipmentAdditionalAccessorySerializer,
    PrintMaterialSerializer,
)
from .calculators import (
    TimeCalculationEngine,
    ChargeCalculationEngine,
    build_safe_input_values_for_charge_calculation,
)
from .slot_utils import SlotGenerator, SlotAvailabilityChecker
from .slot_department_access import (
    filter_queryset_for_home_department,
    slot_allows_internal_user,
)
from .quota_utils import QuotaChecker, get_quota_breakdown, booking_quota_should_skip
from .booking_timing import BookingRequestTimer, attach_booking_performance_headers
from .booking_events import create_booking_event
from .booking_cancellation import (
    CancellationValidationError,
    parse_cancellation_request,
    perform_booking_cancellation,
    preview_partial_cancellation,
    preview_partial_cancellation_print_items,
    partial_cancel_reduction_key,
    partial_cancel_uses_input_reduction,
    user_may_cancel_started_slots,
)
from .maintenance_policy import (
    clear_disruption_policy_fields,
    effective_slot_status_when_freeing_disruption_booking,
    released_slot_status_after_booking_freed,
)
from .waitlist import (
    add_user_to_waitlist,
    notify_waitlist_slots_available,
    waitlist_virtual_booking_id,
)
from .reports import get_equipment_ids_managed_by_oic
from iic_booking.users.models.user import User
from iic_booking.users.models.user_type import UserType
from iic_booking.users.models.equipment_supply_chain_role import (
    UserEquipmentSupplyChainRole,
    EquipmentSupplyChainRole,
)
from iic_booking.communication.service import CommunicationService
from iic_booking.communication.utils import get_frontend_absolute_url, booking_display_id_for_email
from iic_booking.communication.styled_transactional_emails import send_return_shipping_tracking_email

logger = logging.getLogger(__name__)

# Cap "book any" alternative-slot scans — unbounded select_for_update iteration can take minutes.
_BOOK_ANY_ALT_SLOT_SCAN_LIMIT = 4000


def _is_admin_panel_user(user) -> bool:
    """True for admin / OIC / operator / finance; matches user_type case-insensitively."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    ut = getattr(user, "user_type", None)
    if ut is None or ut == "":
        return False
    codes = UserType.get_admin_panel_codes()
    if ut in codes:
        return True
    u_norm = str(ut).strip().lower()
    return any(str(c).strip().lower() == u_norm for c in codes)


# User-facing message when slot(s) were taken by another user (concurrent booking)
SLOTS_ALREADY_OCCUPIED_MESSAGE = (
    "The selected slot(s) have already been booked by another user. Please choose a different slot."
)

ISTEM_PORTAL_URL = "https://www.istem.gov.in/"


def _booking_requires_istem_fbr_results_block(booking) -> bool:
    """True when booking is in the I-STEM FBR workflow and FBR is not yet executed."""
    st = booking.istem_fbr_status
    if st is None:
        return False
    return st != IstemFbrStatus.EXECUTED


def _booking_istem_portal_url(booking) -> str:
    equipment = getattr(booking, "equipment", None)
    return get_equipment_istem_portal_url(equipment) if equipment else ISTEM_PORTAL_URL


def _ensure_istem_fbr_initialized(booking) -> None:
    """If charge profile requires I-STEM but booking predates the flag, initialize workflow status."""
    if getattr(booking, "istem_fbr_status", None):
        return
    cp = getattr(booking, "charge_profile", None)
    if not charge_profile_requires_istem_fbr(cp):
        return
    if booking.status not in (
        BookingStatus.BOOKED,
        BookingStatus.COMPLETED,
        BookingStatus.PENDING_PAYMENT,
    ):
        return
    booking.istem_fbr_status = IstemFbrStatus.PENDING_FBR
    booking.save(update_fields=["istem_fbr_status", "updated_at"])


def _user_can_act_as_oic_for_equipment(user, equipment) -> bool:
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "user_type", None) == UserType.ADMIN:
        return True
    if getattr(user, "user_type", None) != UserType.MANAGER:
        return False
    eq_id = getattr(equipment, "equipment_id", None) or getattr(equipment, "pk", None)
    if eq_id is None:
        return False
    if eq_id in get_equipment_ids_managed_by_oic(user.id):
        return True
    now_ts = timezone.now()
    return EquipmentTemporaryOIC.objects.filter(
        equipment_id=eq_id,
        temporary_oic=user,
        resume_at__gt=now_ts,
    ).exists()


def _notify_oic_istem_fbr_submitted(booking) -> None:
    """Email OIC(s) for the equipment when external user submits I-STEM FBR."""
    from django.conf import settings as django_settings
    from django.core.mail import send_mail
    from iic_booking.equipment.models import EquipmentManager

    equipment = booking.equipment
    emails: set[str] = set()
    for em in EquipmentManager.objects.filter(equipment=equipment).select_related("manager"):
        mgr = em.manager
        if mgr and mgr.email and mgr.is_active:
            emails.add(mgr.email.strip())
    if not emails:
        return
    vid = getattr(booking, "virtual_booking_id", None) or booking.booking_id
    subject = f"I-STEM FBR verification required — {vid}"
    body = (
        f"An external user submitted an I-STEM FBR number for booking {vid} "
        f"({equipment.name}).\n\n"
        f"FBR number: {booking.istem_fbr_number}\n"
        f"User: {booking.user.email}\n\n"
        f"Please verify on the I-STEM portal and mark verified in the booking portal."
    )
    try:
        send_mail(
            subject=subject[:250],
            message=body,
            from_email=django_settings.DEFAULT_FROM_EMAIL,
            recipient_list=sorted(emails),
            fail_silently=True,
        )
    except Exception:
        pass


def _to_float_or_none(value):
    """Best-effort numeric conversion for dynamic input field validation."""
    if value is None:
        return None
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        s = value.strip()
        if s == "":
            return None
        try:
            return float(s)
        except (TypeError, ValueError):
            return None
    return None


def _resolve_numeric_max_for_field_a(field, input_values, equipment):
    """
    Resolve max limit for numeric field A.

    Priority:
    1) options.max_formula (supports A..Z + SLOT_DURATION_MINUTES),
    2) options.max
    """
    raw_options = field.options
    opts = raw_options if isinstance(raw_options, dict) else {}
    formula = None

    if isinstance(opts, dict):
        formula = opts.get("max_formula")
    elif isinstance(raw_options, str):
        # Legacy/plain mode: options saved as formula string (e.g. "B*4")
        formula = raw_options
    elif isinstance(raw_options, list) and len(raw_options) == 1 and isinstance(raw_options[0], str):
        # Legacy list mode from older admin handling: ["B*4"]
        formula = raw_options[0]
    if isinstance(formula, str) and formula.strip():
        expr = formula.strip()
        for token in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
            v = _to_float_or_none(input_values.get(token))
            expr = re.sub(
                rf"(?<![a-zA-Z0-9_]){token}(?![a-zA-Z0-9_])",
                str(v if v is not None else 0),
                expr,
            )
        expr = re.sub(
            r"(?<![a-zA-Z0-9_])SLOT_DURATION_MINUTES(?![a-zA-Z0-9_])",
            str(getattr(equipment, "slot_duration_minutes", 0) or 0),
            expr,
        )
        if not re.fullmatch(r"[0-9\.\+\-\*\/\(\)\s]+", expr):
            return None
        try:
            return float(eval(expr, {"__builtins__": {}}, {}))
        except Exception:
            return None

    if isinstance(opts, dict):
        return _to_float_or_none(opts.get("max"))
    return None


def _validate_dynamic_numeric_input_limits(equipment, input_values, booking_user=None):
    """
    Validate NUMERIC dynamic fields against options / help_text limits.

    help_text convention (NUMERIC):
      line 1 = min, line 2 = max, line 3 = step (defaults 0 / 100 / 1).

    Field A may also use options.max_formula / options.max. External booking users
    skip formula/static options.max for A only; help_text / default range still apply.
    """
    from .numeric_field_limits import resolve_numeric_field_bounds

    is_external = booking_user is not None and UserType.is_external_user(
        getattr(booking_user, "user_type", None) or ""
    )

    fields = list(
        DynamicInputField.objects.filter(
            equipment=equipment,
            field_type=DynamicInputFieldType.NUMERIC,
        ).only("field_key", "field_label", "options", "help_text")
    )
    if not fields:
        return None

    for field in fields:
        key = field.field_key
        raw = input_values.get(key)
        if raw is None or raw == "":
            continue
        value = _to_float_or_none(raw)
        if value is None:
            continue

        formula_max = None
        if key == "A" and not is_external:
            formula_max = _resolve_numeric_max_for_field_a(field, input_values, equipment)

        min_v, max_v, _step = resolve_numeric_field_bounds(
            options=field.options,
            help_text=field.help_text,
            formula_max=formula_max,
        )
        label = field.field_label or key
        if value < min_v:
            pretty = int(min_v) if float(min_v).is_integer() else round(min_v, 6)
            return f"{label} cannot be less than {pretty}."
        if value > max_v:
            pretty = int(max_v) if float(max_v).is_integer() else round(max_v, 6)
            return f"{label} cannot be greater than {pretty}."
    return None

# Pricing profile selector for booking/charge calculation.
def _get_charge_profile_pricing_profile_for_user(user, equipment) -> str:
    """
    Return ChargeProfilePricingProfile code for a user+equipment.

    Backwards compatible behavior:
    - if user.use_discounted_charge_profile is False => STANDARD
    - if user.use_discounted_charge_profile is True and user has NO per-equipment overrides
      in `UserDiscountedChargeEquipment` => DISCOUNTED for ALL equipment
    - if per-equipment overrides exist => DISCOUNTED only for overridden equipment rows
    """
    if not user:
        return ChargeProfilePricingProfile.STANDARD
    if not bool(getattr(user, "use_discounted_charge_profile", False)):
        return ChargeProfilePricingProfile.STANDARD

    if equipment is None:
        # Treat as apply-to-all for defensive usage.
        return ChargeProfilePricingProfile.DISCOUNTED

    from .models import UserDiscountedChargeEquipment

    overrides_exist = UserDiscountedChargeEquipment.objects.filter(
        user=user, is_active=True
    ).exists()
    if not overrides_exist:
        return ChargeProfilePricingProfile.DISCOUNTED

    overridden = UserDiscountedChargeEquipment.objects.filter(
        user=user, equipment=equipment, is_active=True
    ).exists()
    return ChargeProfilePricingProfile.DISCOUNTED if overridden else ChargeProfilePricingProfile.STANDARD

# Default calendar colors (used when DB has no rows) – pronounced for clear weekly view
DEFAULT_CALENDAR_COLORS = {
    "slot_colors": {
        "AVAILABLE": "#22c55e",      # Strong green
        "BOOKED": "#ef4444",         # Strong red
        "HOLD": "#f59e0b",           # Amber for hold (pending approval)
        "BLOCKED": "#64748b",       # Slate gray
        "UNDER_MAINTENANCE": "#f97316",  # Orange
        "OPERATOR_ABSENT": "#eab308",     # Amber
        "BOOKING_NOT_UTILIZED": "#a855f7",  # Purple
        "RESERVED_FOR_EXTERNAL": "#94a3b8",  # Slate for "Reserved for External User" (internal view)
        "HOME_DEPARTMENT_ONLY": "#c4b5fd",  # Violet — home dept only (unmarked while policy active)
        "NON_HOME_RESERVED": "#06b6d4",  # Cyan — reserved for other departments
        "NOT_AVAILABLE": "#e2e8f0",  # Light slate for "Not Available" (external view)
        "COMPLETED": "#059669",  # Emerald for completed bookings (distinct from BOOKED red)
    },
    "holiday_default": "#f59e0b",   # Amber for holidays
    "saturday_color": "#c7d2fe",     # Indigo-200 for Saturday
    "sunday_color": "#fbcfe8",       # Pink-200 for Sunday
}


def get_calendar_colors():
    """Return admin-configurable colors for the weekly calendar. Used in equipment_daily_slots response."""
    try:
        with transaction.atomic():
            rows = CalendarColorSetting.objects.all().values_list("key", "value")
            slot_keys = {
                "AVAILABLE",
                "BOOKED",
                "COMPLETED",
                "HOLD",
                "BLOCKED",
                "UNDER_MAINTENANCE",
                "OPERATOR_ABSENT",
                "BOOKING_NOT_UTILIZED",
                "RESERVED_FOR_EXTERNAL",
                "HOME_DEPARTMENT_ONLY",
                "NON_HOME_RESERVED",
                "NOT_AVAILABLE",
            }
            slot_colors = dict(DEFAULT_CALENDAR_COLORS["slot_colors"])
            holiday_default = DEFAULT_CALENDAR_COLORS["holiday_default"]
            saturday_color = DEFAULT_CALENDAR_COLORS.get("saturday_color", "#c7d2fe")
            sunday_color = DEFAULT_CALENDAR_COLORS.get("sunday_color", "#fbcfe8")
            for key, value in rows:
                if key in slot_keys and value:
                    slot_colors[key] = value.strip()
                elif key == "HOLIDAY_DEFAULT" and value:
                    holiday_default = value.strip()
                elif key == "SATURDAY" and value:
                    saturday_color = value.strip()
                elif key == "SUNDAY" and value:
                    sunday_color = value.strip()
            return {
                "slot_colors": slot_colors,
                "holiday_default": holiday_default,
                "saturday_color": saturday_color,
                "sunday_color": sunday_color,
                "external_gst_percent": float(get_external_gst_percent()),
            }
    except Exception:
        out = dict(DEFAULT_CALENDAR_COLORS)
        out["external_gst_percent"] = 18
        return out


def get_external_gst_percent():
    """Return the GST percentage (0-100) for external users from BookingChargeSetting. Default 18."""
    try:
        obj = BookingChargeSetting.objects.filter(key="EXTERNAL_GST_PERCENT").first()
        if obj and obj.value:
            return Decimal(obj.value.strip())
    except Exception:
        pass
    return Decimal("18")


def get_external_return_shipping_fee_amount() -> Decimal:
    """
    Return the configured return-shipping fee (INR) for external sample return option.
    Stored in BookingChargeSetting as a string decimal. Default 0.00 when not configured.
    """
    try:
        obj = BookingChargeSetting.objects.filter(key="EXTERNAL_RETURN_SHIPPING_FEE_AMOUNT").first()
        if obj and obj.value is not None and str(obj.value).strip() != "":
            return Decimal(str(obj.value).strip())
    except Exception:
        pass
    return Decimal("0.00")


def get_internal_slot_window_setting():
    """Return the singleton InternalUserSlotWindowSetting if it exists and has both weekday and time set."""
    try:
        obj = InternalUserSlotWindowSetting.objects.first()
        if obj and obj.reference_weekday is not None and obj.reference_time is not None:
            return obj
    except Exception:
        pass
    return None


def get_slot_window_reference_datetime_for_local_week(containing_dt, ref_weekday, ref_time):
    """
    Slot window reference instant in the ISO week that contains containing_dt (project local date).

    Same construction as equipment_daily_slots (Monday of that week + ref_weekday + ref_time).
    Returns None if weekday/time not configured.
    """
    from datetime import datetime as dt_class, timedelta
    from django.utils import timezone as tz

    if ref_weekday is None or ref_time is None:
        return None
    local = tz.localtime(containing_dt)
    d = local.date()
    week_monday = d - timedelta(days=d.weekday())
    ref_date = week_monday + timedelta(days=int(ref_weekday))
    ref_naive = dt_class.combine(ref_date, ref_time)
    return tz.make_aware(ref_naive, tz.get_current_timezone())


PEAK_SLOT_WAITLIST_AFTER_REFERENCE_MINUTES = 30


def get_equipment_slot_window_reference_config(equipment):
    """Return (reference_weekday, reference_time) for internal slot-window rules.

    Per-equipment weekday/time wins when both are set; otherwise the global
    InternalUserSlotWindowSetting singleton is used.
    """
    rw = getattr(equipment, "slot_window_reference_weekday", None)
    rt = getattr(equipment, "slot_window_reference_time", None)
    if rw is not None and rt is not None:
        return rw, rt
    setting = get_internal_slot_window_setting()
    if setting is not None:
        return setting.reference_weekday, setting.reference_time
    return None, None


def get_internal_slot_window_date_bounds(equipment, at=None):
    """
    Bookable date range for internal users when slot-window reference is configured.

    Before the reference instant in the current ISO week: current Monday through Sunday
    (current week only). On/after that instant: current Monday through Sunday of the
    following ISO week (current + next week).

    Returns (min_date, max_date, before_reference). All None/False when not configured.
    """
    from datetime import datetime as dt_class, timedelta

    rw, rt = get_equipment_slot_window_reference_config(equipment)
    if rw is None or rt is None:
        return None, None, False
    at = timezone.now() if at is None else at
    if timezone.is_naive(at):
        at = timezone.make_aware(at, timezone.get_current_timezone())
    now = timezone.localtime(at)
    week_monday = now.date() - timedelta(days=now.weekday())
    ref_date = week_monday + timedelta(days=int(rw))
    ref_naive = dt_class.combine(ref_date, rt)
    ref_datetime = timezone.make_aware(ref_naive, timezone.get_current_timezone())
    if now < ref_datetime:
        return week_monday, week_monday + timedelta(days=6), True
    return week_monday, week_monday + timedelta(days=13), False


def is_slot_window_peak_waitlist_period(equipment, at=None):
    """
    True when local time is in [this week's slot-window reference datetime,
    reference datetime + PEAK_SLOT_WAITLIST_AFTER_REFERENCE_MINUTES).

    During this interval, failures due to slot non-availability are waitlisted even if
    the client sent waitlist_on_failure=False.
    """
    from datetime import timedelta

    at = timezone.now() if at is None else at
    if timezone.is_naive(at):
        at = timezone.make_aware(at, timezone.get_current_timezone())
    rw, rt = get_equipment_slot_window_reference_config(equipment)
    if rw is None or rt is None:
        return False
    ref_dt = get_slot_window_reference_datetime_for_local_week(at, rw, rt)
    if ref_dt is None:
        return False
    peak_end = ref_dt + timedelta(minutes=PEAK_SLOT_WAITLIST_AFTER_REFERENCE_MINUTES)
    local_at = timezone.localtime(at)
    return ref_dt <= local_at < peak_end


def user_can_access_booking_for_maintenance_slots_api(request_user, booking) -> bool:
    """
    True if this user may act on the booking for slots API (owner or wallet supervisor).
    Mirrors list_bookings scope so maintenance_extra_week_booking_id works for supervised students.
    """
    if booking.user_id == request_user.id:
        return True
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

    return WalletJoinRequest.objects.filter(
        faculty=request_user,
        student_id=booking.user_id,
        status=WalletJoinRequestStatus.APPROVED,
    ).exists()


def get_slot_window_deadline_for_request(requested_at, equipment):
    """
    Return the deadline (slot window datetime - 15 min) for the next occurrence of the internal slot window
    (reference_weekday + reference_time) on or after requested_at. Used to auto-expire undecided requests
    so holds are released before the next week opens. Returns None if no slot window is configured.
    """
    from django.utils import timezone
    from datetime import datetime as dt_class, timedelta
    ref_weekday, ref_time = get_equipment_slot_window_reference_config(equipment)
    if ref_weekday is None or ref_time is None:
        return None
    local = timezone.localtime(requested_at)
    days_ahead = (ref_weekday - local.weekday()) % 7
    if days_ahead == 0 and local.time() >= ref_time:
        days_ahead = 7
    next_date = local.date() + timedelta(days=days_ahead)
    next_dt_naive = dt_class.combine(next_date, ref_time)
    tz = timezone.get_current_timezone()
    next_dt = timezone.make_aware(next_dt_naive, tz)
    return next_dt - timedelta(minutes=15)


def get_disruption_choice_deadline_at(requested_at, equipment):
    """
    Next occurrence of the slot-window reference (weekday + time), plus 1 hour.
    Used when the user must choose refund (cancel) or reschedule after a disruption.
    Returns None if weekday/time are not configured (caller may fall back to legacy deadline logic).
    """
    from datetime import datetime as dt_class, timedelta
    from django.utils import timezone

    ref_weekday, ref_time = get_equipment_slot_window_reference_config(equipment)
    if ref_weekday is None or ref_time is None:
        return None
    local = timezone.localtime(requested_at)
    days_ahead = (ref_weekday - local.weekday()) % 7
    if days_ahead == 0 and local.time() >= ref_time:
        days_ahead = 7
    next_date = local.date() + timedelta(days=days_ahead)
    next_dt_naive = dt_class.combine(next_date, ref_time)
    tz = timezone.get_current_timezone()
    next_dt = timezone.make_aware(next_dt_naive, tz)
    return next_dt + timedelta(hours=1)


def get_effective_expiry_at(urgent_request, validity_days):
    """
    Return the effective expiry datetime for an urgent request: the earlier of
    (requested_at + validity_days) and (slot window deadline - 15 min), so that
    undecided requests expire before the next week opens.
    """
    from datetime import timedelta
    if not urgent_request.requested_at:
        return None
    validity_end = urgent_request.requested_at + timedelta(days=validity_days)
    slot_deadline = get_slot_window_deadline_for_request(urgent_request.requested_at, urgent_request.equipment)
    if slot_deadline is None:
        return validity_end
    return min(validity_end, slot_deadline)


def get_approved_urgent_requests_last_6_months(user_id, equipment_id):
    """
    Return list of approved urgent requests for the given user and equipment in the past 6 months.
    Each item has id, requested_at, decided_at. Used to show requester history to user, supervisor, and Admin/OIC.
    """
    from django.utils import timezone
    from datetime import timedelta
    cutoff = timezone.now() - timedelta(days=180)
    qs = UrgentBookingRequest.objects.filter(
        user_id=user_id,
        equipment_id=equipment_id,
        status=UrgentBookingRequestStatus.APPROVED,
        decided_at__isnull=False,
        decided_at__gte=cutoff,
    ).order_by("-decided_at").values("id", "requested_at", "decided_at")
    return list(qs)


def _format_approved_urgent_6m(rows):
    """Turn list of dicts with id, requested_at, decided_at into API-safe list with ISO dates."""
    out = []
    for r in rows:
        out.append({
            "id": r["id"],
            "requested_at": r["requested_at"].isoformat() if r.get("requested_at") else None,
            "decided_at": r["decided_at"].isoformat() if r.get("decided_at") else None,
        })
    return out


def _get_no_slot_log_for_urgent_display(user, equipment):
    """
    Return (log_count, log_recent) for no-slot allocation log on urgent request detail.
    Uses failed BookingAttemptLog entries, excluding cases where failure was due to
    weekly/monthly quota exhausted (individual or faculty).
    """
    from django.db.models import Q
    qs = (
        BookingAttemptLog.objects
        .filter(
            user=user,
            equipment=equipment,
            outcome=BookingAttemptOutcome.FAILED,
        )
        .exclude(
            Q(failure_reason__icontains="quota check failed")
            | Q(failure_reason__icontains="weekly quota")
            | Q(failure_reason__icontains="monthly quota")
        )
        .order_by("-requested_at")
    )
    log_count = qs.count()
    log_recent = list(
        qs[:10].values("requested_at", "number_of_samples", "slots_requested", "duration_minutes")
    )
    for e in log_recent:
        if e.get("requested_at"):
            e["requested_at"] = e["requested_at"].isoformat()
    return log_count, log_recent


def _ensure_booking_result_file_table():
    """Create equipment_bookingresultfile table if it does not exist (e.g. migration not applied)."""
    if connection.vendor != "postgresql":
        return
    with connection.cursor() as cursor:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS equipment_bookingresultfile (
                id BIGSERIAL PRIMARY KEY,
                booking_id INTEGER NOT NULL REFERENCES equipment_booking(booking_id) ON DELETE CASCADE,
                file VARCHAR(100) NOT NULL,
                original_name VARCHAR(255) NOT NULL DEFAULT '',
                created_at TIMESTAMP WITH TIME ZONE NOT NULL
            );
        """)


def user_can_see_equipment(user, equipment):
    """
    Return True if the user is allowed to see this equipment.
    - Equipment without visibility_group is public (everyone can see).
    - Equipment with visibility_group is visible only to group members.
    - Admins and operators can see all equipment.
    - Managers (OIC) can see only equipment for which they are OIC (primary or temporary until resume_at).
    - Multi-mode catalog rules hide inactive child modes / exclusive-hidden parents for non-staff.
    """
    if user and user.is_authenticated and user.user_type == UserType.MANAGER:
        allowed_ids = get_equipment_ids_managed_by_oic(user.id)
        return equipment.equipment_id in allowed_ids
    if user and user.is_authenticated and user.user_type == UserType.OPERATOR:
        allowed_ids = _get_equipment_ids_for_log_access(user) or []
        return equipment.equipment_id in allowed_ids
    if equipment.visibility_group_id is None:
        visible = True
    elif user and user.is_authenticated and user.user_type in [
        UserType.OPERATOR, UserType.MANAGER, UserType.ADMIN
    ]:
        visible = True
    elif user and user.is_authenticated:
        from iic_booking.users.models.user_group import UserGroupMember
        visible = UserGroupMember.objects.filter(
            user_group_id=equipment.visibility_group_id,
            user=user,
        ).exists()
    else:
        visible = False
    if not visible:
        return False
    from .mode_utils import is_equipment_visible_on_date, is_staff_bypass_user
    if is_staff_bypass_user(user):
        return True
    return is_equipment_visible_on_date(equipment, timezone.localdate())


def user_can_see_equipment_image(user, equipment):
    """
    Equipment photos are public (no login required).
    Booking, slots, and detail APIs still enforce visibility via user_can_see_equipment.
    """
    return True


def get_visible_equipment_queryset(user):
    """
    Return Equipment queryset filtered by visibility for the given user.
    - Anonymous: only equipment with visibility_group=None (public).
    - Authenticated: public OR equipment whose visibility_group has user as member.
    - Operator/Admin: all equipment.
    - Manager (OIC): only equipment for which they are OIC (primary or temporary until resume_at).
    - Department Administrator: only equipment in their assigned internal department.
    """
    queryset = Equipment.objects.all()
    if not user or not user.is_authenticated:
        queryset = queryset.filter(visibility_group__isnull=True)
    elif user.user_type == UserType.MANAGER:
        allowed_ids = get_equipment_ids_managed_by_oic(user.id)
        if not allowed_ids:
            return queryset.none()
        queryset = queryset.filter(equipment_id__in=allowed_ids)
    elif user.user_type == UserType.OPERATOR:
        allowed_ids = _get_equipment_ids_for_log_access(user) or []
        if not allowed_ids:
            return queryset.none()
        queryset = queryset.filter(equipment_id__in=allowed_ids)
    elif user.user_type == UserType.ADMIN:
        pass
    elif user.user_type == UserType.DEPT_ADMIN:
        dept_id = getattr(user, "department_id", None)
        if not dept_id:
            return queryset.none()
        queryset = queryset.filter(internal_department_id=dept_id)
    else:
        queryset = queryset.filter(
            Q(visibility_group__isnull=True) |
            Q(visibility_group__members__user=user)
        ).distinct()

    from .mode_utils import filter_queryset_for_mode_catalog
    return filter_queryset_for_mode_catalog(queryset, user)


@api_view(["GET"])
@permission_classes([AllowAny])
def equipment_category_list(request):
    """List equipment categories (for filters/dropdowns)."""
    categories = EquipmentCategory.objects.all().order_by('name')
    serializer = EquipmentCategorySerializer(categories, many=True)
    return Response({"categories": serializer.data}, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def equipment_form_choices(request):
    """Return dropdown choices for the admin equipment add/edit form. Admin-panel users only."""
    if request.user.user_type not in UserType.get_admin_panel_codes():
        return Response(
            {"error": "Only admin-panel users can access equipment form choices."},
            status=status.HTTP_403_FORBIDDEN,
        )
    from iic_booking.users.models import Department, UserGroup
    from iic_booking.users.models.user import User
    from iic_booking.users.models.department import DepartmentType

    categories = EquipmentCategory.objects.all().order_by('name')
    equipment_groups = EquipmentGroup.objects.all().order_by('name')
    # Multi-mode bases only (enable_multi_mode + no parent) for parent linking
    parent_equipment_choices = (
        Equipment.objects.filter(parent_equipment__isnull=True, enable_multi_mode=True)
        .order_by("code")
        .values("equipment_id", "code", "name")
    )
    internal_departments = Department.objects.filter(
        department_type=DepartmentType.INTERNAL
    ).order_by('name')
    user_groups = UserGroup.objects.all().order_by('name')
    # Officers / Lab Incharge for equipment assignment: only users whose department is Internal.
    managers = (
        User.objects.filter(
            user_type=UserType.MANAGER,
            is_active=True,
            department__department_type=DepartmentType.INTERNAL,
        )
        .select_related("department")
        .order_by("name", "email")
    )
    operators = (
        User.objects.filter(
            user_type=UserType.OPERATOR,
            is_active=True,
            department__department_type=DepartmentType.INTERNAL,
        )
        .select_related("department")
        .order_by("name", "email")
    )

    dept_id_raw = (request.query_params.get("internal_department_id") or "").strip()
    if dept_id_raw and dept_id_raw.lower() not in ("none", "null", "all"):
        try:
            dept_id = int(dept_id_raw)
            if internal_departments.filter(pk=dept_id).exists():
                managers = managers.filter(department_id=dept_id)
                operators = operators.filter(department_id=dept_id)
        except (TypeError, ValueError):
            pass

    return Response({
        "categories": EquipmentCategorySerializer(categories, many=True).data,
        "equipment_groups": EquipmentGroupSerializer(equipment_groups, many=True).data,
        "parent_equipment_choices": list(parent_equipment_choices),
        "internal_departments": [
            {"id": d.id, "name": d.name, "code": d.code or "", "department_type": DepartmentType.INTERNAL}
            for d in internal_departments
        ],
        "user_groups": [
            {"id": g.id, "name": g.name, "code": g.code}
            for g in user_groups
        ],
        "managers": [
            {
                "id": u.id,
                "name": u.name or u.email or "",
                "email": u.email or "",
                "department_id": u.department_id,
                "department_name": (u.department.name if u.department_id else None),
                "department_code": (u.department.code if u.department_id else None),
            }
            for u in managers
        ],
        "operators": [
            {
                "id": u.id,
                "name": u.name or u.email or "",
                "email": u.email or "",
                "department_id": u.department_id,
                "department_name": (u.department.name if u.department_id else None),
                "department_code": (u.department.code if u.department_id else None),
            }
            for u in operators
        ],
        "profile_type_choices": [
            {"value": c[0], "label": str(c[1])}
            for c in EquipmentProfileType.choices
        ],
        "status_choices": [
            {"value": c[0], "label": str(c[1])}
            for c in EquipmentStatus.choices
        ],
        "dynamic_input_field_type_choices": [
            {"value": c[0], "label": str(c[1])}
            for c in DynamicInputFieldType.choices
        ],
        "user_type_choices": [
            {"value": c[0], "label": str(c[1])}
            for c in UserType.get_choices()
        ],
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def temporary_oic_list_oic_users(request):
    """List OIC (manager) users for temporary OIC dropdown. Excludes current user. Manager only.
    Optional query param: search — filter by name or email (case-insensitive)."""
    if request.user.user_type != UserType.MANAGER:
        return Response(
            {"error": "Only Officer in Charge (OIC) can access this."},
            status=status.HTTP_403_FORBIDDEN,
        )
    from iic_booking.users.models.user import User
    queryset = User.objects.filter(
        user_type=UserType.MANAGER,
        is_active=True,
    ).exclude(pk=request.user.pk).order_by("name", "email")
    search = (request.GET.get("search") or "").strip()
    if search:
        queryset = queryset.filter(
            Q(name__icontains=search) | Q(email__icontains=search)
        )
    return Response({
        "oic_users": [
            {"id": u.id, "name": u.name or u.email or "", "email": u.email or ""}
            for u in queryset
        ],
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def temporary_oic_my_equipments(request):
    """List equipments for which the current user is primary OIC (for temporary OIC form). Manager only."""
    if request.user.user_type != UserType.MANAGER:
        return Response(
            {"error": "Only Officer in Charge (OIC) can access this."},
            status=status.HTTP_403_FORBIDDEN,
        )
    equipment_ids = list(
        EquipmentManager.objects.filter(manager=request.user).values_list("equipment_id", flat=True)
    )
    if not equipment_ids:
        return Response({"equipments": []}, status=status.HTTP_200_OK)
    equipments = Equipment.objects.filter(pk__in=equipment_ids).order_by("code")
    return Response({
        "equipments": [
            {"id": e.equipment_id, "code": e.code, "name": e.name}
            for e in equipments
        ],
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def temporary_oic_create(request):
    """Create a temporary OIC delegation. Current user must be primary OIC for the equipment. Manager only."""
    if request.user.user_type != UserType.MANAGER:
        return Response(
            {"error": "Only Officer in Charge (OIC) can create temporary delegation."},
            status=status.HTTP_403_FORBIDDEN,
        )
    from django.utils import timezone
    from iic_booking.users.models.user import User

    equipment_id = request.data.get("equipment_id")
    temporary_oic_id = request.data.get("temporary_oic_id")
    resume_at_str = request.data.get("resume_at")

    if not equipment_id or not temporary_oic_id or not resume_at_str:
        return Response(
            {"error": "equipment_id, temporary_oic_id, and resume_at are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        equipment = Equipment.objects.get(pk=int(equipment_id))
    except (Equipment.DoesNotExist, ValueError, TypeError):
        return Response({"error": "Invalid equipment."}, status=status.HTTP_404_NOT_FOUND)
    if not EquipmentManager.objects.filter(equipment=equipment, manager=request.user).exists():
        return Response(
            {"error": "You are not the Officer in Charge for this equipment."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        temp_oic = User.objects.get(pk=int(temporary_oic_id), user_type=UserType.MANAGER, is_active=True)
    except (User.DoesNotExist, ValueError, TypeError):
        return Response({"error": "Invalid temporary OIC user."}, status=status.HTTP_400_BAD_REQUEST)
    if temp_oic.pk == request.user.pk:
        return Response(
            {"error": "Temporary OIC cannot be yourself."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        from django.utils.dateparse import parse_datetime
        resume_at = parse_datetime(resume_at_str)
        if resume_at is None:
            return Response(
                {"error": "resume_at must be a valid ISO datetime (e.g. 2025-03-10T18:00:00 or 2025-03-10T18:00:00+05:30)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if timezone.is_naive(resume_at):
            resume_at = timezone.make_aware(resume_at)
    except Exception:
        return Response(
            {"error": "resume_at must be a valid ISO datetime."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if resume_at <= timezone.now():
        return Response(
            {"error": "Resume date and time must be in the future."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    delegation = EquipmentTemporaryOIC.objects.create(
        equipment=equipment,
        primary_oic=request.user,
        temporary_oic=temp_oic,
        resume_at=resume_at,
    )
    return Response({
        "id": delegation.id,
        "equipment_id": equipment.equipment_id,
        "equipment_code": equipment.code,
        "temporary_oic_id": temp_oic.id,
        "temporary_oic_name": temp_oic.name or temp_oic.email,
        "resume_at": resume_at.isoformat(),
        "message": "Temporary OIC assigned. They can manage this equipment until the resume date and time.",
    }, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def temporary_oic_list_mine(request):
    """List active temporary OIC delegations where current user is primary OIC (resume_at > now). Manager only."""
    if request.user.user_type != UserType.MANAGER:
        return Response(
            {"error": "Only Officer in Charge (OIC) can access this."},
            status=status.HTTP_403_FORBIDDEN,
        )
    from django.utils import timezone
    now = timezone.now()
    delegations = (
        EquipmentTemporaryOIC.objects
        .filter(primary_oic=request.user, resume_at__gt=now)
        .select_related("equipment", "temporary_oic")
        .order_by("resume_at")
    )
    return Response({
        "delegations": [
            {
                "id": d.id,
                "equipment_id": d.equipment_id,
                "equipment_code": d.equipment.code,
                "equipment_name": d.equipment.name,
                "temporary_oic_id": d.temporary_oic_id,
                "temporary_oic_name": d.temporary_oic.name or d.temporary_oic.email,
                "temporary_oic_email": d.temporary_oic.email,
                "resume_at": d.resume_at.isoformat(),
                "created_at": d.created_at.isoformat(),
            }
            for d in delegations
        ],
    }, status=status.HTTP_200_OK)


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def temporary_oic_cancel(request, delegation_id):
    """Cancel a temporary OIC delegation. Only the primary OIC can cancel. Manager only."""
    if request.user.user_type != UserType.MANAGER:
        return Response(
            {"error": "Only Officer in Charge (OIC) can cancel delegation."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        delegation = EquipmentTemporaryOIC.objects.get(pk=delegation_id, primary_oic=request.user)
    except EquipmentTemporaryOIC.DoesNotExist:
        return Response({"error": "Delegation not found or you are not the primary OIC."}, status=status.HTTP_404_NOT_FOUND)
    delegation.delete()
    return Response({"message": "Temporary OIC delegation cancelled."}, status=status.HTTP_200_OK)


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def temporary_oic_update(request, delegation_id):
    """Update a temporary OIC delegation's resume_at. Only the primary OIC can update. Manager only."""
    if request.user.user_type != UserType.MANAGER:
        return Response(
            {"error": "Only Officer in Charge (OIC) can update delegation."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        delegation = EquipmentTemporaryOIC.objects.select_related("equipment", "temporary_oic").get(
            pk=delegation_id, primary_oic=request.user
        )
    except EquipmentTemporaryOIC.DoesNotExist:
        return Response(
            {"error": "Delegation not found or you are not the primary OIC."},
            status=status.HTTP_404_NOT_FOUND,
        )
    resume_at_str = request.data.get("resume_at")
    if not resume_at_str:
        return Response(
            {"error": "resume_at is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    from django.utils import timezone
    from django.utils.dateparse import parse_datetime
    new_resume_at = parse_datetime(resume_at_str)
    if not new_resume_at:
        return Response(
            {"error": "Invalid resume_at format. Use ISO 8601 datetime."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if new_resume_at <= timezone.now():
        return Response(
            {"error": "Resume date and time must be in the future."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    delegation.resume_at = new_resume_at
    delegation.save(update_fields=["resume_at"])
    return Response({
        "message": "Delegation updated.",
        "id": delegation.id,
        "resume_at": delegation.resume_at.isoformat(),
        "equipment_code": delegation.equipment.code,
        "temporary_oic_name": delegation.temporary_oic.name or delegation.temporary_oic.email,
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([AllowAny])
def equipment_catalog_departments(request):
    """List internal departments that have visible, non-disposed equipment (for catalog filters)."""
    queryset = get_visible_equipment_queryset(request.user).exclude(status=EquipmentStatus.DISPOSED)

    dept_rows = (
        queryset.filter(internal_department__isnull=False)
        .values("internal_department_id", "internal_department__name", "internal_department__code")
        .annotate(equipment_count=Count("equipment_id"))
        .order_by("internal_department__name")
    )
    departments = [
        {
            "id": row["internal_department_id"],
            "name": row["internal_department__name"],
            "code": row["internal_department__code"] or "",
            "equipment_count": row["equipment_count"],
        }
        for row in dept_rows
    ]
    unassigned_count = queryset.filter(internal_department__isnull=True).count()

    return Response(
        {
            "departments": departments,
            "unassigned_count": unassigned_count,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def equipment_list(request):
    """Get list of equipment visible to the current user.

    Anonymous users see only public equipment (no visibility group).
    Authenticated users see public equipment plus equipment from groups they belong to.
    Operators, managers, and admins see all equipment.

    Query Parameters:
        profile_type: Filter by profile type (SAMPLE, HOUR, SAMPLE_ELEMENT, MULTI_PARAM)
        status: Filter by status (ACTIVE, INACTIVE, etc.)
        search: Search by code or name
        internal_department_id: Filter by internal department id (omit or 'all' for every department)

    Returns:
        Response: List of equipment with count
    """
    from django.db.models import Avg, Count

    queryset = get_visible_equipment_queryset(request.user).select_related(
        "category", "internal_department"
    )

    include_ratings_raw = (request.query_params.get("include_ratings") or "").strip().lower()
    include_ratings = include_ratings_raw in {"1", "true", "yes", "y"}
    if include_ratings:
        rating_filter = Q(bookings__rating__isnull=False, bookings__rating_removed=False)
        queryset = queryset.annotate(
            avg_rating=Avg("bookings__rating", filter=rating_filter),
            rating_count=Count("bookings__rating", filter=rating_filter),
            rating_1_count=Count("bookings", filter=rating_filter & Q(bookings__rating=1)),
            rating_2_count=Count("bookings", filter=rating_filter & Q(bookings__rating=2)),
            rating_3_count=Count("bookings", filter=rating_filter & Q(bookings__rating=3)),
            rating_4_count=Count("bookings", filter=rating_filter & Q(bookings__rating=4)),
            rating_5_count=Count("bookings", filter=rating_filter & Q(bookings__rating=5)),
        )

    # Filter by profile_type if provided
    profile_type = request.query_params.get('profile_type')
    if profile_type:
        queryset = queryset.filter(profile_type=profile_type)

    # Filter by status if provided
    status_filter = request.query_params.get('status')
    if status_filter:
        queryset = queryset.filter(status=status_filter)

    # Search by code or name if provided
    search = request.query_params.get('search')
    if search:
        queryset = queryset.filter(
            Q(code__icontains=search) | Q(name__icontains=search)
        )

    internal_department_id = (request.query_params.get("internal_department_id") or "").strip()
    # Department Administrators are always scoped to their own department (ignore client overrides).
    if getattr(request.user, "is_authenticated", False) and getattr(request.user, "user_type", None) == UserType.DEPT_ADMIN:
        dept_id = getattr(request.user, "department_id", None)
        if not dept_id:
            queryset = queryset.none()
        else:
            queryset = queryset.filter(internal_department_id=dept_id)
    elif internal_department_id and internal_department_id.lower() != "all":
        try:
            dept_id = int(internal_department_id)
        except (TypeError, ValueError):
            return Response(
                {"error": "Invalid internal_department_id."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        queryset = queryset.filter(internal_department_id=dept_id)

    # Order by name
    queryset = queryset.order_by('name')

    # scope=booking_attempt_log is deprecated: OIC visibility is now enforced in get_visible_equipment_queryset
    serializer_class = EquipmentListSerializer if include_ratings else EquipmentListLiteSerializer
    serializer = serializer_class(queryset, many=True, context={"request": request})
    return Response(
        {
            "equipments": serializer.data,
            "count": len(serializer.data),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def equipment_ratings(request, equipment_id: int):
    """
    Ratings summary + distribution + review list for an equipment.

    Query params:
      - offset (default 0)
      - limit (default 20, max 100)
    """
    from django.db.models import Avg, Count

    try:
        equipment = Equipment.objects.select_related("visibility_group").get(pk=equipment_id)
    except Equipment.DoesNotExist:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)

    if not user_can_see_equipment(request.user, equipment):
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)

    offset_raw = request.query_params.get("offset", "0")
    limit_raw = request.query_params.get("limit", "20")
    try:
        offset = max(0, int(offset_raw))
    except Exception:
        offset = 0
    try:
        limit = int(limit_raw)
    except Exception:
        limit = 20
    limit = min(100, max(1, limit))

    is_admin_panel_user = bool(
        request.user
        and getattr(request.user, "is_authenticated", False)
        and getattr(request.user, "user_type", None) in UserType.get_admin_panel_codes()
    )

    qs = Booking.objects.filter(equipment_id=equipment_id, rating__isnull=False).select_related("user")
    if not is_admin_panel_user:
        qs = qs.filter(rating_removed=False)

    summary = qs.aggregate(avg_rating=Avg("rating"), rating_count=Count("rating"))

    dist_rows = qs.values("rating").annotate(count=Count("booking_id")).order_by("rating")
    dist = {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0}
    for row in dist_rows:
        r = row.get("rating")
        if r is None:
            continue
        key = str(int(r))
        if key in dist:
            dist[key] = int(row.get("count") or 0)

    total_count = qs.count()
    page = list(
        qs.order_by("-rated_at", "-booking_id")[offset : offset + limit].values(
            "booking_id",
            "rating",
            "rating_feedback",
            "rated_at",
            "rating_removed",
            "rating_removed_at",
            "rating_removed_reason",
            "user_id",
            "user__name",
            "user__email",
        )
    )
    reviews = []
    for item in page:
        user_name = item.get("user__name") or item.get("user__email")
        reviews.append(
            {
                "booking_id": item.get("booking_id"),
                "rating": item.get("rating"),
                "feedback": item.get("rating_feedback") or "",
                "rated_at": item.get("rated_at").isoformat() if item.get("rated_at") else None,
                "user_id": item.get("user_id"),
                "user_name": user_name,
                "rating_removed": bool(item.get("rating_removed")),
                "rating_removed_at": item.get("rating_removed_at").isoformat() if item.get("rating_removed_at") else None,
                "rating_removed_reason": item.get("rating_removed_reason"),
            }
        )

    return Response(
        {
            "equipment_id": equipment_id,
            "avg_rating": float(summary.get("avg_rating")) if summary.get("avg_rating") is not None else None,
            "rating_count": int(summary.get("rating_count") or 0),
            "distribution": dist,
            "reviews": reviews,
            "total_reviews": int(total_count),
            "offset": offset,
            "limit": limit,
            "is_admin_panel_user": is_admin_panel_user,
        },
        status=status.HTTP_200_OK,
    )


def _parse_elements_csv(raw: str) -> set[str]:
    if not raw:
        return set()
    # Normalize: split by comma, strip spaces, drop empties, uppercase symbols
    parts = [p.strip() for p in str(raw).split(",")]
    return {p.upper() for p in parts if p}


@api_view(["POST"])
@permission_classes([AllowAny])
def icpms_min_standards_cover(request):
    """
    Given a list of element symbols, return the minimum set of ICPMS standards
    that covers all requested elements (exact solution via brute force; small dataset).

    Body: { "elements": ["Ag","As", ...] }
    """
    elements = request.data.get("elements")
    if not isinstance(elements, list) or not elements:
        return Response({"error": "elements must be a non-empty list of element symbols."}, status=status.HTTP_400_BAD_REQUEST)

    target = {str(e).strip().upper() for e in elements if str(e).strip()}
    if not target:
        return Response({"error": "No valid element symbols provided."}, status=status.HTTP_400_BAD_REQUEST)

    standards = list(
        ICPMSStandardSample.objects.all().values("id", "s_no", "name_of_std", "list_of_elements", "status")
    )
    # Consider only active standards (status=1). If none match, fall back to all.
    active = [s for s in standards if int(s.get("status") or 0) == 1]
    pool = active if active else standards

    std_sets = []
    for s in pool:
        covered = _parse_elements_csv(s.get("list_of_elements") or "")
        # Ignore standards that cover nothing
        if covered:
            std_sets.append((s, covered))

    if not std_sets:
        return Response(
            {"count": 0, "standards": [], "uncovered": sorted(target)},
            status=status.HTTP_200_OK,
        )

    # Quick check: if impossible, report uncovered.
    union_all = set().union(*[cov for _, cov in std_sets])
    if not target.issubset(union_all):
        uncovered = sorted(target - union_all)
        return Response(
            {"error": "Some elements cannot be covered by available standards.", "uncovered": uncovered, "count": 0, "standards": []},
            status=status.HTTP_200_OK,
        )

    # Exact minimal set cover via brute force over subsets (n is small ~13).
    best = None  # (k, totalCovered, idsSet, pickedStandards)
    n = len(std_sets)
    # Try subset sizes from 1..n and stop at first feasible size.
    for k in range(1, n + 1):
        from itertools import combinations

        for idxs in combinations(range(n), k):
            covered = set()
            picked = []
            ids = set()
            for i in idxs:
                s, cov = std_sets[i]
                covered |= cov
                picked.append(s)
                ids.add(s["id"])
            if target.issubset(covered):
                # tie-breaker: prefer higher coverage (irrelevant since all cover target), then stable by ids
                best = (k, len(covered), ids, picked)
                break
        if best is not None:
            break

    picked = best[3] if best else []
    # Sort by s_no for readability
    picked_sorted = sorted(picked, key=lambda x: str(x.get("s_no") or ""))
    return Response(
        {
            "count": len(picked_sorted),
            "standards": [
                {
                    "s_no": s["s_no"],
                    "name_of_std": s["name_of_std"],
                    "id": s["id"],
                    # Show only the elements from the user's selection that this standard covers.
                    # (Frontend table expects this filtered value.)
                    "list_of_elements": ", ".join(
                        sorted(target.intersection(_parse_elements_csv(s.get("list_of_elements") or "")))
                    ),
                }
                for s in picked_sorted
            ],
            "elements": sorted(target),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([AllowAny])
def icpms_available_standards(request):
    """
    Return all available ICPMS standards (status=1).

    If `elements` is provided (list of element symbols), we also filter each standard's
    `list_of_elements` to only show intersection with the user selection.
    """
    elements = request.data.get("elements")
    target = None
    if isinstance(elements, list):
        target = {str(e).strip().upper() for e in elements if str(e).strip()}

    qs = ICPMSStandardSample.objects.filter(status=1).values(
        "id",
        "s_no",
        "name_of_std",
        "list_of_elements",
    )

    standards_out = []
    for s in qs:
        std_set = _parse_elements_csv(s.get("list_of_elements") or "")
        if target is not None:
            std_set = std_set.intersection(target)
        standards_out.append(
            {
                "id": s["id"],
                "s_no": s["s_no"],
                "name_of_std": s["name_of_std"],
                "list_of_elements": ", ".join(sorted(std_set)) if std_set else "",
            }
        )

    return Response(
        {
            "count": len(standards_out),
            "standards": standards_out,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def icpms_standards_full_list(request):
    """
    Full ICPMS standard sample database for booking UI (all rows, all columns).
    Frontend maps `status` to Available / Not Available.
    """
    qs = ICPMSStandardSample.objects.all().order_by("s_no", "id").values(
        "id",
        "s_no",
        "part_no",
        "name_of_std",
        "list_of_elements",
        "concentration",
        "status",
        "created_at",
        "updated_at",
    )
    out = []
    for row in qs:
        ca = row.get("created_at")
        ua = row.get("updated_at")
        out.append(
            {
                **row,
                "created_at": ca.isoformat() if ca else None,
                "updated_at": ua.isoformat() if ua else None,
            }
        )
    return Response({"count": len(out), "standards": out}, status=status.HTTP_200_OK)


@api_view(["GET", "PATCH"])
@permission_classes([AllowAny])
def equipment_detail(request, pk):
    """Get or update a specific equipment.

    GET: Returns full equipment details. Anonymous users see only public
    equipment (no visibility group). Authenticated users see public equipment
    plus equipment from groups they belong to. Equipment bound to a visibility
    group is only visible to group members.

    PATCH: Requires authentication. Allowed only for admin-panel users (admin,
    manager, operator, finance). Allowed fields: name, description, status, location.

    Args:
        pk: Primary key (equipment_id) of the equipment

    Returns:
        Response: Detailed equipment information (GET) or updated equipment (PATCH)
    """
    try:
        equipment = Equipment.objects.select_related('equipment_group').get(pk=pk)
    except Equipment.DoesNotExist:
        return Response(
            {"error": "Equipment not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if not user_can_see_equipment(request.user, equipment):
        return Response(
            {"error": "Equipment not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if request.method == "PATCH":
        if not request.user or not request.user.is_authenticated:
            return Response(
                {"detail": "Authentication credentials were not provided."},
                status=status.HTTP_401_UNAUTHORIZED,
            )
        if request.user.user_type not in UserType.get_admin_panel_codes():
            return Response(
                {"error": "Only admin-panel users can update equipment."},
                status=status.HTTP_403_FORBIDDEN,
            )
        allowed = {"name", "description", "status", "location"}
        for key in allowed:
            if key in request.data:
                setattr(equipment, key, request.data[key])
        equipment.save()
        serializer = EquipmentDetailSerializer(equipment, context={'request': request})
        return Response(serializer.data, status=status.HTTP_200_OK)

    serializer = EquipmentDetailSerializer(equipment, context={'request': request})
    return Response(serializer.data, status=status.HTTP_200_OK)


@api_view(["GET"])
@authentication_classes([])  # Ignore invalid Authorization headers (stale tokens) for anonymous img loads
@permission_classes([AllowAny])
def equipment_image_proxy(request, pk):
    """
    Stream the equipment image from storage through the API so clients use a stable URL (no expiring signed URLs).
    Public endpoint: no login required. Accepts ?token= for restricted equipment when needed.
    """
    from iic_booking.users.api.token_auth import resolve_request_user

    try:
        equipment = Equipment.objects.get(pk=pk)
    except Equipment.DoesNotExist:
        return Response(
            {"error": "Equipment not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    if not user_can_see_equipment_image(resolve_request_user(request), equipment):
        return Response(
            {"error": "Equipment not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    stored_path = get_equipment_image_storage_path(equipment)
    if not stored_path:
        return Response(
            {"error": "No image for this equipment."},
            status=status.HTTP_404_NOT_FOUND,
        )
    content, resolved_path, content_type = open_equipment_image_bytes(equipment)
    if content is None or not resolved_path:
        logger.warning(
            "Equipment image not found in storage. Path: %s (equipment_id=%s)",
            stored_path,
            equipment.equipment_id,
        )
        return Response(
            {"error": "Image not available."},
            status=status.HTTP_404_NOT_FOUND,
        )

    response = HttpResponse(content, content_type=content_type or "image/jpeg")
    response["Cache-Control"] = "public, max-age=86400"
    response["Content-Length"] = len(content)
    return response


@api_view(["GET"])
@permission_classes([AllowAny])
def equipment_calculate(request, pk):
    """Calculate time and charge for equipment booking.
    
    This endpoint calculates the time and charge for a booking based on
    equipment configuration and input parameters. User type is automatically
    detected from the authentication token.
    
    Args:
        pk: Equipment ID from URL path
    
    Query Parameters:
        A, B, C, D, E, F, G: Optional. Input field values for calculations
        user_type: Optional. Estimate charges for this user type (standard charge profile).
        user_id: Optional. Admin only — calculate for another user's type and discount rules.
    
    Returns:
        Response: Calculation results with time and charge breakdown
    """
    
    # Get equipment
    try:
        equipment = Equipment.objects.select_related('equipment_group').get(pk=pk)
    except Equipment.DoesNotExist:
        return Response(
            {"error": "Equipment not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if not user_can_see_equipment(request.user, equipment):
        return Response(
            {"error": "Equipment not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Get user_type: optional user_type query param = estimate for that profile (standard pricing);
    # for admin, optional user_id = calculate for that user's type and discount rules.
    user_is_authenticated = bool(request.user and request.user.is_authenticated)
    booking_user = request.user if user_is_authenticated else None
    user_type = getattr(request.user, "user_type", None) if user_is_authenticated else None
    if not user_type:
        user_type = UserType.STUDENT
    pricing_profile = (
        _get_charge_profile_pricing_profile_for_user(booking_user, equipment)
        if booking_user
        else ChargeProfilePricingProfile.STANDARD
    )

    user_type_param = (request.query_params.get("user_type") or "").strip().lower()
    if user_type_param:
        if not ChargeProfile.objects.filter(
            equipment=equipment,
            user_type=user_type_param,
            pricing_profile=ChargeProfilePricingProfile.STANDARD,
            is_active=True,
        ).exists():
            return Response(
                {"error": f"No active charge profile found for equipment {pk} and user type {user_type_param}."},
                status=status.HTTP_404_NOT_FOUND,
            )
        user_type = user_type_param
        pricing_profile = ChargeProfilePricingProfile.STANDARD

    user_id_param = request.query_params.get("user_id")
    actor_type = str(request.user.user_type or "").lower() if user_is_authenticated else ""
    if user_id_param and user_is_authenticated and actor_type in (UserType.ADMIN, UserType.MANAGER):
        from iic_booking.users.models import User
        try:
            target_user = User.objects.get(pk=int(user_id_param))
            booking_user = target_user
            if target_user.user_type and not user_type_param:
                user_type = target_user.user_type
                pricing_profile = _get_charge_profile_pricing_profile_for_user(booking_user, equipment)
        except (ValueError, User.DoesNotExist):
            pass
    
    # Get charge profile
    try:
        charge_profile = ChargeProfile.objects.get(
            equipment=equipment,
            user_type=user_type,
            pricing_profile=pricing_profile,
            is_active=True
        )
    except ChargeProfile.DoesNotExist:
        return Response(
            {"error": f"No active charge profile found for equipment {pk} and user type {user_type}."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Extract input values from query params (A-Z and optional *_elements for periodic table)
    input_values = {}
    for key in ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H', 'I', 'J', 'K', 'L', 'M', 'N', 'O', 'P', 'Q', 'R', 'S', 'T', 'U', 'V', 'W', 'X', 'Y', 'Z']:
        value = request.query_params.get(key)
        if value is not None:
            try:
                # Try to convert to float for numeric values
                input_values[key] = float(value)
            except (ValueError, TypeError):
                # Keep as string if not numeric
                input_values[key] = value
        elements_key = f"{key}_elements"
        elements_val = request.query_params.get(elements_key)
        if elements_val is not None:
            input_values[elements_key] = elements_val

    from .calculators import normalize_periodic_table_billable_counts
    input_values = normalize_periodic_table_billable_counts(equipment, input_values)

    numeric_limit_error = _validate_dynamic_numeric_input_limits(
        equipment, input_values, booking_user=booking_user
    )
    if numeric_limit_error:
        return Response({"error": numeric_limit_error}, status=status.HTTP_400_BAD_REQUEST)

    if getattr(equipment, "profile_type", None) == EquipmentProfileType.PRINT_3D:
        from .print_3d_views import (
            get_charge_estimate_guest_user,
            merge_print_booking_into_input_values,
        )

        print_analysis_id = request.query_params.get("print_analysis_id")
        print_analysis_batch_id = request.query_params.get("print_analysis_batch_id")
        print_user = booking_user if user_is_authenticated else get_charge_estimate_guest_user()
        input_values, pa_err = merge_print_booking_into_input_values(
            equipment,
            input_values,
            print_user,
            print_analysis_id=print_analysis_id,
            print_analysis_batch_id=print_analysis_batch_id,
        )
        if pa_err:
            return Response({"error": pa_err}, status=status.HTTP_400_BAD_REQUEST)
    
    # Calculate time
    try:
        # Create a temporary charge profile object with profile_type from equipment
        # This is a workaround since calculators expect charge_profile.profile_type
        # but it's actually on equipment
        class ChargeProfileWithType:
            def __init__(self, charge_profile, equipment):
                self.equipment = charge_profile.equipment
                self.user_type = charge_profile.user_type
                self.is_active = charge_profile.is_active
                self.primary_unit_charge = charge_profile.primary_unit_charge
                self.secondary_unit_charge = charge_profile.secondary_unit_charge
                self.breakpoint = charge_profile.breakpoint
                self.time_formula = charge_profile.time_formula
                self.pricing_profile = getattr(charge_profile, "pricing_profile", ChargeProfilePricingProfile.STANDARD)
                self.profile_type = equipment.profile_type
                # secondary_flat_charge is referenced in calculators but doesn't exist in model
                # Set to None (calculators check if it exists before using)
                # self.secondary_flat_charge = getattr(charge_profile, 'secondary_flat_charge', None)
        
        charge_profile_with_type = ChargeProfileWithType(charge_profile, equipment)
        
        total_time_minutes = TimeCalculationEngine.calculate_time(
            charge_profile_with_type,
            input_values,
            slot_duration_minutes=equipment.slot_duration_minutes
        )
    except Exception as e:
        return Response(
            {"error": f"Error calculating time: {str(e)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Calculate charge
    try:
        total_charge, charge_breakdown = ChargeCalculationEngine.calculate_charge(
            charge_profile_with_type,
            input_values,
            total_time_minutes,
            selected_parameters=None
        )
    except Exception as e:
        return Response(
            {"error": f"Error calculating charge: {str(e)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    base_charge = total_charge
    gst_percent = Decimal("0")
    gst_amount = Decimal("0")
    if UserType.is_external_user(user_type):
        # External option: return samples after analysis => add return shipping fee BEFORE GST.
        sample_return_raw = request.query_params.get("sample_return_after_analysis")
        sample_return = str(sample_return_raw).strip().lower() in ("1", "true", "yes", "y") if sample_return_raw is not None else False
        return_shipping_fee = Decimal("0.00")
        if sample_return:
            try:
                return_shipping_fee = get_external_return_shipping_fee_amount().quantize(Decimal("0.01"))
            except Exception:
                return_shipping_fee = Decimal("0.00")
            if return_shipping_fee > 0:
                base_charge = (Decimal(str(base_charge)) + return_shipping_fee).quantize(Decimal("0.01"))
                charge_breakdown = list(charge_breakdown) + [
                    {"description": "Return shipping charges", "amount": float(return_shipping_fee)},
                ]
        gst_percent = get_external_gst_percent()
        if gst_percent > 0:
            gst_amount = (base_charge * gst_percent / Decimal("100")).quantize(Decimal("0.01"))
            total_charge = (base_charge + gst_amount).quantize(Decimal("0.01"))
            charge_breakdown = list(charge_breakdown) + [
                {"description": f"GST ({gst_percent}%)", "amount": float(gst_amount)},
            ]

    reward_points_requested = request.query_params.get("reward_points_to_redeem")
    reward_points_applied = Decimal("0.00")
    reward_discount_amount = Decimal("0.00")
    reward_message = None
    if reward_points_requested not in (None, "") and user_is_authenticated:
        try:
            reward_req = Decimal(str(reward_points_requested))
            reward_points_applied, reward_discount_amount, reward_message = _calculate_reward_redemption(
                total_charge, reward_req, booking_user, equipment=equipment
            )
        except Exception:
            reward_message = "Invalid reward_points_to_redeem value."
            reward_points_applied = Decimal("0.00")
            reward_discount_amount = Decimal("0.00")

    total_after_reward = max(Decimal("0.00"), Decimal(total_charge) - reward_discount_amount).quantize(Decimal("0.01"))
    # Return results
    response_data = {
        "equipment_id": equipment.equipment_id,
        "equipment_code": equipment.code,
        "equipment_name": equipment.name,
        "user_type": user_type,
        "profile_type": equipment.profile_type,
        "input_values": input_values,
        "total_time_minutes": total_time_minutes,
        "base_charge": str(base_charge),
        "gst_percent": float(gst_percent),
        "gst_amount": str(gst_amount),
        "total_charge": str(total_charge),
        "charge_breakdown": charge_breakdown if getattr(charge_profile, "show_charge_breakdown", True) else [],
        "show_charge_breakdown": bool(getattr(charge_profile, "show_charge_breakdown", True)),
        "reward": {
            "points_balance": str(_get_points_balance(booking_user)),
            "requested_points": str(reward_points_requested or "0"),
            "points_applied": str(reward_points_applied),
            "discount_amount": str(reward_discount_amount),
            "final_payable": str(total_after_reward),
            "message": reward_message,
        },
    }
    
    # Add debug information if time_formula exists
    if charge_profile.time_formula:
        response_data["time_formula"] = charge_profile.time_formula
        response_data["slot_duration_minutes"] = equipment.slot_duration_minutes
    
    return Response(response_data, status=status.HTTP_200_OK)


def _calculate_one_proforma_line(request_user, equipment, input_values):
    """
    Calculate time and charge for one equipment with given input_values, for the given user's type.
    Returns (line_data_dict, error_string). If error_string is set, line_data_dict is None.
    """
    user_type = getattr(request_user, "user_type", None) or UserType.STUDENT
    try:
        charge_profile = ChargeProfile.objects.get(
            equipment=equipment,
            user_type=user_type,
            pricing_profile=_get_charge_profile_pricing_profile_for_user(request_user, equipment),
            is_active=True,
        )
    except ChargeProfile.DoesNotExist:
        return None, f"No charge profile for equipment {equipment.equipment_id} and user type {user_type}."

    class ChargeProfileWithType:
        def __init__(self, cp, eq):
            self.equipment = cp.equipment
            self.user_type = cp.user_type
            self.is_active = cp.is_active
            self.primary_unit_charge = cp.primary_unit_charge
            self.secondary_unit_charge = cp.secondary_unit_charge
            self.breakpoint = cp.breakpoint
            self.time_formula = cp.time_formula
            self.pricing_profile = getattr(cp, "pricing_profile", ChargeProfilePricingProfile.STANDARD)
            self.profile_type = eq.profile_type

    cp_with_type = ChargeProfileWithType(charge_profile, equipment)
    try:
        total_time_minutes = TimeCalculationEngine.calculate_time(
            cp_with_type,
            input_values,
            slot_duration_minutes=equipment.slot_duration_minutes,
        )
    except Exception as e:
        return None, f"Time calculation failed for {equipment.code}: {e}"
    try:
        base_charge, charge_breakdown = ChargeCalculationEngine.calculate_charge(
            cp_with_type,
            input_values,
            total_time_minutes,
            selected_parameters=None,
        )
    except Exception as e:
        return None, f"Charge calculation failed for {equipment.code}: {e}"

    gst_percent = Decimal("0")
    gst_amount = Decimal("0")
    if UserType.is_external_user(user_type):
        gst_percent = get_external_gst_percent()
        if gst_percent > 0:
            gst_amount = (base_charge * gst_percent / Decimal("100")).quantize(Decimal("0.01"))
    total_charge = (base_charge + gst_amount).quantize(Decimal("0.01"))
    if gst_percent > 0:
        charge_breakdown = list(charge_breakdown) + [
            {"description": f"GST ({gst_percent}%)", "amount": float(gst_amount)},
        ]

    return {
        "equipment_id": equipment.equipment_id,
        "equipment_code": equipment.code or "",
        "equipment_name": equipment.name or "",
        "profile_type": equipment.profile_type or "",
        "input_values": input_values,
        "total_time_minutes": total_time_minutes,
        "charge_breakdown": charge_breakdown if getattr(charge_profile, "show_charge_breakdown", True) else [],
        "show_charge_breakdown": bool(getattr(charge_profile, "show_charge_breakdown", True)),
        "base_charge": str(base_charge),
        "gst_percent": float(gst_percent),
        "gst_amount": str(gst_amount),
        "total_charge": str(total_charge),
    }, None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def proforma_invoice_calculate(request):
    """
    Calculate proforma invoice for multiple equipments with samples/slots/elements inputs.
    Charges are computed for the logged-in user's type. Returns a table of line items and totals.

    Body: { "items": [ { "equipment_id": 1, "input_values": { "A": 5, "B": 2, ... } }, ... ] }
    """
    data = request.data or {}
    items = data.get("items")
    if not isinstance(items, list) or len(items) == 0:
        return Response(
            {"error": "Provide a non-empty list of items with equipment_id and input_values."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user_type = getattr(request.user, "user_type", None) or UserType.STUDENT
    line_items = []
    subtotal = Decimal("0")
    total_gst = Decimal("0")

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            return Response(
                {"error": f"Item at index {idx} must be an object with equipment_id and input_values."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        equipment_id = item.get("equipment_id")
        if equipment_id is None:
            return Response(
                {"error": f"Item at index {idx}: equipment_id is required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            equipment = Equipment.objects.select_related("equipment_group").get(pk=equipment_id)
        except Equipment.DoesNotExist:
            return Response(
                {"error": f"Equipment with id {equipment_id} not found."},
                status=status.HTTP_404_NOT_FOUND,
            )
        if not user_can_see_equipment(request.user, equipment):
            return Response(
                {"error": f"You do not have access to equipment {equipment.code or equipment_id}."},
                status=status.HTTP_403_FORBIDDEN,
            )
        raw_input = item.get("input_values") or {}
        input_values = {}
        for k, v in raw_input.items():
            if v is None:
                continue
            if isinstance(v, (int, float)):
                input_values[k] = v
            else:
                try:
                    input_values[k] = float(v)
                except (ValueError, TypeError):
                    input_values[k] = v

        line_data, err = _calculate_one_proforma_line(request.user, equipment, input_values)
        if err:
            return Response({"error": err}, status=status.HTTP_400_BAD_REQUEST)
        line_items.append(line_data)
        subtotal += Decimal(line_data["base_charge"])
        total_gst += Decimal(line_data["gst_amount"])

    total_amount = subtotal + total_gst

    from .document_exports import _amount_in_words
    try:
        amount_in_words = _amount_in_words(total_amount)
    except Exception:
        amount_in_words = "Rupees Only"

    return Response(
        {
            "user_type": user_type,
            "line_items": line_items,
            "subtotal": str(subtotal.quantize(Decimal("0.01"))),
            "total_gst": str(total_gst.quantize(Decimal("0.01"))),
            "total_amount": str(total_amount.quantize(Decimal("0.01"))),
            "amount_in_words": amount_in_words,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([AllowAny])
def equipment_daily_slots(request, pk):
    """Get daily slots for a specific equipment.
    
    This endpoint is publicly accessible (no authentication required).
    Returns daily slots for the current week (Monday to Sunday).
    Automatically generates slots if they don't exist.
    
    Query Parameters:
        start_date: Optional. Start date (YYYY-MM-DD). Defaults to Monday of current week.
        end_date: Optional. End date (YYYY-MM-DD). Defaults to Sunday of current week.
        week: Optional. If 'current', returns current week slots. Default behavior.
    
    Args:
        pk: Primary key (equipment_id) of the equipment
    
    Returns:
        Response: List of daily slots with count
    """
    from datetime import date, timedelta
    
    try:
        equipment = Equipment.objects.select_related('equipment_group').get(pk=pk)
    except Equipment.DoesNotExist:
        return Response(
            {"error": "Equipment not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Check visibility - unauthenticated users can see public equipment
    # Pass None for unauthenticated users (user_can_see_equipment handles this)
    user = request.user if request.user.is_authenticated else None
    # Admin-panel users (admin, manager, operator, finance) can see and use slots on weekends/holidays
    user_type = getattr(user, 'user_type', None) if user else None
    is_admin = _is_admin_panel_user(user)
    if not user_can_see_equipment(user, equipment):
        return Response(
            {"error": "Equipment not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Get date range from query params or use current week
    start_date_param = request.query_params.get('start_date')
    end_date_param = request.query_params.get('end_date')
    
    urgent_week_extension = str(request.query_params.get("urgent_week_extension", "")).lower() in ("1", "true", "yes")
    if start_date_param and end_date_param:
        try:
            week_start = date.fromisoformat(start_date_param)
            week_end = date.fromisoformat(end_date_param)
        except ValueError:
            return Response(
                {"error": "Invalid date format. Use YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    else:
        # Calculate current week (Monday to Sunday)
        today = timezone.localdate()
        days_since_monday = today.weekday()
        week_start = today - timedelta(days=days_since_monday)
        week_end = week_start + timedelta(days=6)

    # Calendar week from the request (Mon–Sun). External booking uses a 7-day bookable window from today+15 that often
    # starts mid-week; the UI still shows the full ISO week, so we must query/generate slots for this range — not a clamped
    # sub-range — otherwise early-week columns have no rows and the grid shows "—".
    calendar_week_start = week_start
    calendar_week_end = week_end

    # For non-admin internal users: slot window = [min_date, max_date]. Before reference time: current week only;
    # on/after: current week + next week.
    # Per-equipment slot window when set; else global InternalUserSlotWindowSetting.
    slot_window_min_date = None
    slot_window_max_date = None
    before_slot_window_reference = False
    effective_ref_weekday, effective_ref_time = get_equipment_slot_window_reference_config(equipment)
    ref_weekday = effective_ref_weekday
    ref_time = effective_ref_time
    if not is_admin:
        # External users: the booking UI uses a 1-week window starting 15 days from today.
        # Do NOT apply the internal slot-window reference weekday/time logic for them.
        for_external_user = user_type and UserType.is_external_user(user_type)
        if for_external_user:
            from datetime import timedelta as _td
            today = timezone.localdate()
            slot_window_min_date = today + _td(days=15)
            slot_window_max_date = slot_window_min_date + _td(days=6)
            # Do not shrink week_start/week_end to the bookable window; keep the requested Mon–Sun range for the grid.
            week_start, week_end = calendar_week_start, calendar_week_end
        elif ref_weekday is not None and ref_time is not None:
            from django.utils import timezone as tz
            now = tz.localtime(tz.now())
            slot_window_min_date, slot_window_max_date, before_slot_window_reference = (
                get_internal_slot_window_date_bounds(equipment, now)
            )
            # Keep the requested Mon–Sun range for the grid; clamp to the slot window max so next week is hidden
            # until the reference instant.
            week_start, week_end = calendar_week_start, calendar_week_end
            if slot_window_min_date is not None and week_start < slot_window_min_date:
                week_start = slot_window_min_date
            if slot_window_max_date is not None:
                if urgent_week_extension:
                    slot_window_max_date = slot_window_max_date + timedelta(days=7)
                meb_raw = request.query_params.get("maintenance_extra_week_booking_id")
                if meb_raw and user and getattr(user, "is_authenticated", False):
                    try:
                        meb_id = int(meb_raw)
                        meb_booking = Booking.objects.filter(
                            booking_id=meb_id,
                            equipment_id=equipment.equipment_id,
                        ).filter(
                            Q(maintenance_reschedule_extra_week=True) | Q(status=BookingStatus.DISRUPTION_PENDING)
                        ).first()
                        if meb_booking and user_can_access_booking_for_maintenance_slots_api(user, meb_booking):
                            # After maintenance: extend visible range. If equipment became operational *after* the slot
                            # window reference instant that week, allow two extra weeks; otherwise one extra week only.
                            op_at = getattr(meb_booking, "maintenance_operational_marked_at", None) or timezone.now()
                            ref_in_week = get_slot_window_reference_datetime_for_local_week(
                                op_at, effective_ref_weekday, effective_ref_time
                            )
                            if ref_in_week is not None and op_at > ref_in_week:
                                slot_window_max_date = slot_window_max_date + timedelta(days=14)
                            else:
                                slot_window_max_date = slot_window_max_date + timedelta(days=7)
                    except (ValueError, TypeError):
                        pass
                if week_end > slot_window_max_date:
                    week_end = slot_window_max_date
                if week_start > slot_window_max_date:
                    week_start = slot_window_max_date
        else:
            # No global/equipment slot-window reference: still apply default two-week horizon and
            # maintenance reschedule extension (otherwise extra week never appears).
            from django.utils import timezone as tz

            now = tz.localtime(tz.now())
            week_monday = now.date() - timedelta(days=now.weekday())
            slot_window_min_date = week_monday
            slot_window_max_date = week_monday + timedelta(days=13)
            if slot_window_min_date is not None and week_start < slot_window_min_date:
                week_start = slot_window_min_date
            if slot_window_max_date is not None:
                if urgent_week_extension:
                    slot_window_max_date = slot_window_max_date + timedelta(days=7)
                meb_raw = request.query_params.get("maintenance_extra_week_booking_id")
                if meb_raw and user and getattr(user, "is_authenticated", False):
                    try:
                        meb_id = int(meb_raw)
                        meb_booking = Booking.objects.filter(
                            booking_id=meb_id,
                            equipment_id=equipment.equipment_id,
                        ).filter(
                            Q(maintenance_reschedule_extra_week=True) | Q(status=BookingStatus.DISRUPTION_PENDING)
                        ).first()
                        if meb_booking and user_can_access_booking_for_maintenance_slots_api(user, meb_booking):
                            # No reference weekday/time: one additional week after maintenance.
                            slot_window_max_date = slot_window_max_date + timedelta(days=7)
                    except (ValueError, TypeError):
                        pass
                if week_end > slot_window_max_date:
                    week_end = slot_window_max_date
                if week_start > slot_window_max_date:
                    week_start = slot_window_max_date

    # Fetch holidays once for the range (single query + Saturdays and Sundays in memory)
    holidays_in_range = Holiday.get_holidays_in_range(week_start, week_end)
    calendar_colors = get_calendar_colors()
    saturday_color = calendar_colors.get("saturday_color") or "#c7d2fe"
    sunday_color = calendar_colors.get("sunday_color") or "#fbcfe8"
    holidays = {}
    for k, v in holidays_in_range.items():
        color = v.get("color") or "#fef3c7"
        weekday = k.weekday()
        if weekday == 5:
            color = saturday_color
        elif weekday == 6:
            color = sunday_color
        holidays[k.isoformat()] = {"label": v["reason"], "color": color}

    # Ensure slot masters exist (generate on-demand if needed)
    SlotGenerator.ensure_slot_masters_exist(equipment)

    # Generate all missing slots for the week in one batch (faster than per-day generation when navigating weeks)
    # Sat/Sun and institute holidays are in Holiday.get_holidays_in_range; without allow_holiday, no DailySlot rows
    # exist and the "Change slot status" week grid shows non-clickable placeholder cells.
    # Authenticated requests (staff or users with a token) therefore generate those days too; new rows use
    # NOT_AVAILABLE on closed days when the equipment default status is AVAILABLE (see SlotGenerator).
    for_external_user = user_type and UserType.is_external_user(user_type)
    authenticated_slots_viewer = bool(user is not None and getattr(user, "is_authenticated", False))
    SlotGenerator.generate_slots_for_week(
        equipment,
        week_start,
        week_end,
        allow_holiday=authenticated_slots_viewer,
    )

    # Get all daily slots with select_related to avoid N+1 in serializer
    daily_slots = DailySlot.objects.filter(
        slot_master__equipment=equipment,
        date__gte=week_start,
        date__lte=week_end
    ).select_related('slot_master', 'slot_master__equipment', 'booking', 'booking__user', 'booking__user__department').order_by('date', 'start_datetime')

    # External booking weekly grid: include *all* DailySlot rows in range; DailySlotSerializer maps display/bookability.
    # Internal users: include *all* DailySlot rows in range as well. Historically, AVAILABLE Sat/Sun/holiday slots were
    # bulk_updated to BLOCKED on every GET and other statuses on weekends were omitted from the payload — that hid
    # admin/OIC status changes and reverted AVAILABLE after refresh. Booking still enforces slot status and
    # reserved_for_external in create_booking.
    if for_external_user:
        filtered_slots = list(daily_slots)
    else:
        filtered_slots = list(daily_slots)

    # Apply equipment slot window time filter: only slots fully within [time_from, time_to] (inclusive) are shown.
    time_from = getattr(equipment, 'weekly_view_time_from', None)
    time_to = getattr(equipment, 'weekly_view_time_to', None)
    force_time_filter = str(request.query_params.get("apply_weekly_view_time_filter", "")).lower() in ("1", "true", "yes")
    should_time_filter = (force_time_filter or (not is_admin)) and (not for_external_user) and (time_from is not None or time_to is not None)
    if should_time_filter:
        from django.utils import timezone as tz
        def slot_within_window(slot):
            if not slot.start_datetime or not slot.end_datetime:
                return False
            start_local = tz.localtime(slot.start_datetime) if tz.is_aware(slot.start_datetime) else slot.start_datetime
            end_local = tz.localtime(slot.end_datetime) if tz.is_aware(slot.end_datetime) else slot.end_datetime
            st, et = start_local.time(), end_local.time()
            if time_from is not None and st < time_from:
                return False
            if time_to is not None and et > time_to:
                return False
            return True
        filtered_slots = [s for s in filtered_slots if slot_within_window(s)]

    # Multi-mode: keep all slots (no blank cells). Overlay label/color for end users.
    from .mode_utils import (
        apply_mode_overlays_to_slot_payloads,
        is_staff_bypass_user,
        bypasses_multimode_restrictions,
    )

    # Slot Master times for calendar time axis. For regular users with time filter, only include masters that have slots in filtered_slots.
    slot_masters_qs = SlotMaster.objects.filter(equipment=equipment, is_active=True).order_by('slot_number')
    if should_time_filter and filtered_slots:
        slot_master_ids_in_range = list({s.slot_master_id for s in filtered_slots})
        slot_masters_qs = slot_masters_qs.filter(pk__in=slot_master_ids_in_range).order_by('slot_number')
    slot_masters = list(slot_masters_qs)
    slot_master_times = [sm.open_time.strftime("%H:%M:%S") for sm in slot_masters]

    # For weekly window display: show COMPLETED bookings with future slot date as BOOKED (internal + external users). Admin sees real status.
    for_weekly_display = not is_admin
    for_external_user_ctx = bool(for_external_user)
    serializer = DailySlotSerializer(
        filtered_slots,
        many=True,
        context={
            'for_weekly_display': for_weekly_display,
            'for_external_user': for_external_user_ctx,
            'holidays_in_range': set(holidays_in_range.keys()) if holidays_in_range else set(),
            'external_bookable_min_date': slot_window_min_date if for_external_user_ctx else None,
            'external_bookable_max_date': slot_window_max_date if for_external_user_ctx else None,
            # Email/phone only for admin-panel staff (Admin / OIC / Operator / Finance).
            'include_booking_user_contact': bool(is_admin),
        },
    )
    slots_payload = list(serializer.data)
    if not (is_staff_bypass_user(user) or bypasses_multimode_restrictions(user)):
        slots_payload = apply_mode_overlays_to_slot_payloads(equipment, filtered_slots, slots_payload)

    # Min/max times for backward compatibility (from the slot_masters we already have)
    agg = (
        {"min_open": min(sm.open_time for sm in slot_masters), "max_close": max(sm.close_time for sm in slot_masters)}
        if slot_masters
        else {}
    )
    slot_start = agg.get('min_open')
    slot_end = agg.get('max_close')
    # Waitlist queue info for frontend CTA when no slots are visible in this week.
    waitlist_depth = getattr(equipment, "waitlist_queue_depth", None) or 0
    try:
        from .models import WaitlistEntry
        waitlist_current_count = WaitlistEntry.objects.filter(equipment=equipment).count()
    except Exception:
        waitlist_current_count = 0
    waitlist_has_room = waitlist_depth > 0 and waitlist_current_count < waitlist_depth
    # Admin-configurable colors for weekly window (slot statuses + holiday + weekend)
    # calendar_colors already computed above for holidays weekend colors
    return Response(
        {
            "equipment_id": equipment.equipment_id,
            "equipment_code": equipment.code,
            "start_date": week_start.isoformat(),
            "end_date": week_end.isoformat(),
            "slots": slots_payload,
            "count": len(slots_payload),
            "holidays": holidays,
            "slot_start_time": slot_start.strftime("%H:%M:%S") if slot_start else None,
            "slot_end_time": slot_end.strftime("%H:%M:%S") if slot_end else None,
            "slot_duration_minutes": equipment.slot_duration_minutes or 60,
            "slot_master_times": slot_master_times,  # Actual Slot Master open_time values for calendar
            "calendar_colors": calendar_colors,
            "weekly_view_time_from": equipment.weekly_view_time_from.strftime("%H:%M") if equipment.weekly_view_time_from else None,
            "weekly_view_time_to": equipment.weekly_view_time_to.strftime("%H:%M") if equipment.weekly_view_time_to else None,
            "weekly_view_max_rows": equipment.weekly_view_max_rows,
            "weekly_view_default_days": equipment.weekly_view_default_days,
            "slot_window_min_date": slot_window_min_date.isoformat() if slot_window_min_date else None,
            "slot_window_max_date": slot_window_max_date.isoformat() if slot_window_max_date else None,
            "slot_window_reference_weekday": effective_ref_weekday if (slot_window_min_date is not None or slot_window_max_date is not None) else None,
            "slot_window_reference_time": (effective_ref_time.strftime("%H:%M") if effective_ref_time else None) if (slot_window_min_date is not None or slot_window_max_date is not None) else None,
            "urgent_peak_window_minutes": getattr(equipment, "urgent_peak_window_minutes", None),
            "max_urgent_requests": getattr(equipment, "max_urgent_requests", None),
            "waitlist_queue_depth": waitlist_depth,
            "waitlist_current_count": waitlist_current_count,
            "waitlist_has_room": waitlist_has_room,
        },
        status=status.HTTP_200_OK,
    )


@transaction.non_atomic_requests
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def book_equipment(request, pk):
    """Book equipment with selected time range.
    
    This endpoint requires authentication.
    Creates a booking for the authenticated user and links it to daily slots matching the time range.
    
    Request Body (JSON):
        {
            "equipment_id": "5",  # Optional, also in URL path
            "start_time": "2026-01-09T04:30:00.000Z",  # ISO datetime string
            "end_time": "2026-01-09T05:30:00.000Z",   # ISO datetime string
            "input_values": {                         # Input field values (A-G)
                "A": "1",
                "B": "2",
                "C": "",
                "D": "",
                "E": "",
                "F": [],
                "G": false
            },
            "status": "pending",  # Optional, defaults to "pending"
            "total_cost": 50,     # Optional, will be calculated if not provided
            "total_hours": 1      # Optional, will be calculated if not provided
        }
    
    Args:
        pk: Primary key (equipment_id) of the equipment
    
    Returns:
        Response: Created booking information with linked slots
    """
    from django.db import transaction
    from django.utils.dateparse import parse_datetime
    from datetime import datetime

    try:
        return _book_equipment_impl(request, pk)
    except Exception as exc:
        # Never re-raise: with ATOMIC_REQUESTS + ASGI, a bare raise becomes a plain-text
        # uvicorn "Internal Server Error" instead of a useful JSON body for the UI.
        if connection.in_atomic_block:
            try:
                transaction.set_rollback(True)
            except Exception:
                pass
        logger.exception("book_equipment failed equipment_id=%s", pk)
        return Response(
            {"error": f"Error creating booking: {exc}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


def _book_equipment_impl(request, pk):
    from django.db import transaction
    from django.utils.dateparse import parse_datetime
    from datetime import datetime
    
    # Get equipment
    try:
        equipment = Equipment.objects.select_related('equipment_group').get(pk=pk)
    except Equipment.DoesNotExist:
        return Response(
            {"error": "Equipment not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if not user_can_see_equipment(request.user, equipment):
        return Response(
            {"error": "Equipment not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Booking is allowed only when equipment is Operational.
    # This is enforced server-side so maintenance statuses are always visible but not bookable.
    if (equipment.status or "").strip() != EquipmentStatus.ACTIVE:
        status_label = equipment.get_status_display() if hasattr(equipment, "get_status_display") else (equipment.status or "")
        _create_booking_attempt_log(
            request,
            equipment,
            BookingAttemptOutcome.FAILED,
            failure_reason=f"Equipment is not operational (current status: {status_label}).",
            number_of_samples=request.data.get("number_of_samples") or 1,
            additional_info=_get_additional_info_from_request(request, equipment),
        )
        return Response(
            {
                "error": f"Booking is not allowed while equipment is {status_label}.",
                "equipment_status": equipment.status,
                "equipment_status_display": status_label,
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Multi-mode: reject booking when this mode is not active for the requested slot date(s).
    # (Detailed per-slot checks run after slots are resolved below.)
    
    # Booking user: admin/OIC may pass user_id to book on behalf of another user
    booking_user = request.user
    user_id_body = request.data.get("user_id")
    actor_type = str(request.user.user_type or "").lower()
    if user_id_body is not None and actor_type in (UserType.ADMIN, UserType.MANAGER):
        from iic_booking.users.models import User
        try:
            booking_user = User.objects.get(pk=int(user_id_body))
        except (ValueError, User.DoesNotExist):
            _create_booking_attempt_log(
                request, equipment, BookingAttemptOutcome.FAILED,
                failure_reason="Invalid user_id for booking on behalf.",
                number_of_samples=request.data.get("number_of_samples") or 1,
                additional_info=_get_additional_info_from_request(request, equipment),
            )
            return Response(
                {"error": "Invalid user_id for booking on behalf."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    user_type = booking_user.user_type
    if not user_type:
        user_type = UserType.STUDENT
    if UserType.is_external_user(user_type) and not getattr(booking_user, "istem_portal_acknowledged", False):
        pass  # External users confirm I-STEM after payment on the next-steps page (no pre-book gate).
    # Admin and OIC (and other admin-panel users) see all slots and are not restricted by slot window; only regular users are.
    is_admin = getattr(request.user, "user_type", None) in UserType.get_admin_panel_codes()

    # Get request data
    start_time_str = request.data.get('start_time')
    end_time_str = request.data.get('end_time')
    input_values_raw = request.data.get('input_values', {})
    booking_status = request.data.get('status', 'pending')
    notes = request.data.get('notes', '')
    create_as_hold = request.data.get('create_as_hold') in (True, 'true', 'True', '1')
    
    # Clean input_values: convert by type. Numeric strings must stay numeric (e.g. "1" -> 1, not True).
    input_values = {}
    for key, value in input_values_raw.items():
        # Skip empty values
        if value == "" or value == [] or value is None:
            continue
        
        if isinstance(value, str):
            value_stripped = value.strip()
            value_lower = value_stripped.lower()
            if value_lower == '':
                continue
            # Try numeric conversion first so "1" and "0" are stored as numbers, not boolean
            try:
                if '.' in value_lower:
                    input_values[key] = float(value_lower)
                else:
                    input_values[key] = int(value_lower)
            except (ValueError, TypeError):
                # Not numeric - only explicit boolean strings become boolean (do not treat "1"/"0" as bool)
                if value_lower in ['true', 'yes']:
                    input_values[key] = True
                elif value_lower in ['false', 'no']:
                    input_values[key] = False
                else:
                    input_values[key] = value
        # Handle lists - skip empty ones
        elif isinstance(value, list):
            if len(value) == 0:
                continue
            # For non-empty lists, keep as-is (calculators might handle them)
            input_values[key] = value
        # Handle boolean values directly
        elif isinstance(value, bool):
            if value:
                input_values[key] = value
            # Skip False values for numeric fields
        # Handle numeric values
        elif isinstance(value, (int, float)):
            input_values[key] = value
        else:
            # For other types, try to convert to string and then parse
            try:
                str_value = str(value).strip()
                if str_value == '':
                    continue
                # Try numeric conversion
                if '.' in str_value:
                    input_values[key] = float(str_value)
                else:
                    input_values[key] = int(str_value)
            except (ValueError, TypeError):
                # Skip if conversion fails (likely invalid for calculations)
                continue

    from .calculators import normalize_periodic_table_billable_counts
    input_values = normalize_periodic_table_billable_counts(equipment, input_values)

    # MULTI_PARAM: ensure B (param_code / slot option) is set from selected_parameters so charge is calculated and wallet debited
    if getattr(equipment, "profile_type", None) == "MULTI_PARAM" and "B" not in input_values:
        sel = request.data.get("selected_parameters")
        if sel is not None:
            if isinstance(sel, list) and len(sel) > 0:
                input_values["B"] = sel[0] if isinstance(sel[0], str) else str(sel[0])
            elif isinstance(sel, str) and sel.strip():
                input_values["B"] = sel.strip()

    print_analysis_obj = None
    print_analysis_batch_obj = None
    if getattr(equipment, "profile_type", None) == EquipmentProfileType.PRINT_3D:
        from .models import PrintAnalysis, PrintAnalysisBatch
        from .print_3d_views import merge_print_booking_into_input_values, link_print_analyses_to_booking

        print_analysis_id = request.data.get("print_analysis_id")
        print_analysis_batch_id = request.data.get("print_analysis_batch_id")
        input_values, pa_err = merge_print_booking_into_input_values(
            equipment,
            input_values,
            booking_user,
            print_analysis_id=print_analysis_id,
            print_analysis_batch_id=print_analysis_batch_id,
        )
        if pa_err:
            _create_booking_attempt_log(
                request, equipment, BookingAttemptOutcome.FAILED,
                failure_reason=pa_err,
                number_of_samples=request.data.get("number_of_samples") or 1,
                additional_info=_get_additional_info_from_request(request, equipment),
            )
            return Response({"error": pa_err}, status=status.HTTP_400_BAD_REQUEST)
        if print_analysis_batch_id:
            print_analysis_batch_obj = PrintAnalysisBatch.objects.filter(
                pk=print_analysis_batch_id
            ).first()
            if print_analysis_batch_obj:
                print_analysis_obj = (
                    print_analysis_batch_obj.items.filter(cancelled_at__isnull=True)
                    .order_by("sequence", "created_at")
                    .first()
                )
        elif print_analysis_id:
            print_analysis_obj = PrintAnalysis.objects.filter(pk=print_analysis_id).first()

    numeric_limit_error = _validate_dynamic_numeric_input_limits(
        equipment, input_values, booking_user=booking_user
    )
    if numeric_limit_error:
        _create_booking_attempt_log(
            request, equipment, BookingAttemptOutcome.FAILED,
            failure_reason=numeric_limit_error,
            number_of_samples=request.data.get("number_of_samples") or 1,
            additional_info=_get_additional_info_from_request(request, equipment),
        )
        return Response({"error": numeric_limit_error}, status=status.HTTP_400_BAD_REQUEST)

    # Whether to push failed booking attempts into waitlist queue.
    # Default: True (backwards compatible; existing frontend flows push to waitlist).
    waitlist_on_failure_raw = request.data.get("waitlist_on_failure", True)
    if waitlist_on_failure_raw is None:
        waitlist_on_failure = True
    elif isinstance(waitlist_on_failure_raw, bool):
        waitlist_on_failure = waitlist_on_failure_raw
    elif isinstance(waitlist_on_failure_raw, str):
        waitlist_on_failure = waitlist_on_failure_raw.strip().lower() in ("true", "1", "yes", "y")
    else:
        waitlist_on_failure = bool(waitlist_on_failure_raw)

    # Explicit waitlist booking request when there are no visible slots selected.
    # Used by frontend "Confirm Waitlisted Booking" flow.
    request_waitlist_without_slot_selection = request.data.get("request_waitlist_without_slot_selection") is True
    if request_waitlist_without_slot_selection:
        _create_booking_attempt_log(
            request, equipment, BookingAttemptOutcome.FAILED,
            failure_reason="No slots available in current week; user requested waitlist booking.",
            number_of_samples=request.data.get("number_of_samples") or 1,
            additional_info=_get_additional_info_from_request(request, equipment),
        )
        return Response(
            _enrich_failed_booking_response(
                equipment,
                booking_user,
                "Booking unsuccessful. All slots are occupied.",
                waitlist_on_failure=waitlist_on_failure,
                slot_unavailable_failure=True,
            ),
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Optional: book by exact slot IDs (one booking for all selected slots).
    # Slots may be non-consecutive (staggered); total_time_minutes is the sum of each slot's duration.
    slot_ids_raw = request.data.get('slot_ids')
    if slot_ids_raw and isinstance(slot_ids_raw, list) and len(slot_ids_raw) > 0:
        try:
            slot_ids = [int(x) for x in slot_ids_raw]
        except (ValueError, TypeError):
            _create_booking_attempt_log(
                request, equipment, BookingAttemptOutcome.FAILED,
                failure_reason="slot_ids must be a list of integers.",
                number_of_samples=request.data.get("number_of_samples") or 1,
                additional_info=_get_additional_info_from_request(request, equipment),
            )
            return Response(
                {"error": "slot_ids must be a list of integers."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        perf = BookingRequestTimer()
        perf.mark("slot_ids_parsed")
        if is_admin:
            daily_slots = DailySlot.objects.filter(
                id__in=slot_ids,
                slot_master__equipment=equipment,
            ).exclude(status=SlotStatus.BOOKED).order_by('start_datetime')
        else:
            base_filter = {
                "id__in": slot_ids,
                "slot_master__equipment": equipment,
                "status": SlotStatus.AVAILABLE,
            }
            if UserType.is_external_user(user_type):
                base_filter["reserved_for_external"] = True
            else:
                base_filter["reserved_for_external"] = False
            daily_slots = DailySlot.objects.filter(**base_filter).order_by('start_datetime')
            daily_slots = filter_queryset_for_home_department(
                daily_slots,
                user=booking_user,
                equipment=equipment,
                is_admin=False,
                is_external=UserType.is_external_user(user_type),
            )
        perf.mark("candidate_slots_query")
        book_any_available_slots = request.data.get("book_any_available_slots") is True
        book_even_if_single_slot_available = request.data.get("book_even_if_single_slot_available") is True
        input_values_adjusted_for_single_slot = [False]  # use list so inner transaction can set [0]=True
        visible_week_start = None
        visible_week_end = None
        if book_any_available_slots:
            from django.utils.dateparse import parse_date
            ws = request.data.get("visible_week_start")
            we = request.data.get("visible_week_end")
            if ws and we:
                visible_week_start = parse_date(ws) if isinstance(ws, str) else ws
                visible_week_end = parse_date(we) if isinstance(we, str) else we
        if daily_slots.count() != len(slot_ids):
            if book_any_available_slots:
                required_minutes = _get_slots_duration_minutes(slot_ids)
                if required_minutes > 0:
                    alt = _find_alternative_available_slots(
                        equipment, required_minutes, user_type, is_admin,
                        visible_week_start=visible_week_start, visible_week_end=visible_week_end,
                        booking_user=booking_user,
                    )
                    if alt:
                        slot_ids, daily_slots = alt
                    else:
                        # Try reduce to 1 slot and book one if "Book even if single slot available" is checked
                        book_even_if_single = request.data.get("book_even_if_single_slot_available") is True
                        single_slot_fallback_done = False
                        if book_even_if_single:
                            try:
                                charge_profile_early = ChargeProfile.objects.get(
                                    equipment=equipment,
                                    user_type=user_type,
                                    pricing_profile=_get_charge_profile_pricing_profile_for_user(booking_user, equipment),
                                    is_active=True
                                )
                            except ChargeProfile.DoesNotExist:
                                charge_profile_early = None
                            if charge_profile_early:
                                class ChargeProfileWithType:
                                    def __init__(self, cp, eq):
                                        self.equipment = cp.equipment
                                        self.user_type = cp.user_type
                                        self.is_active = cp.is_active
                                        self.primary_unit_charge = cp.primary_unit_charge
                                        self.secondary_unit_charge = cp.secondary_unit_charge
                                        self.breakpoint = cp.breakpoint
                                        self.time_formula = cp.time_formula
                                        self.pricing_profile = getattr(cp, "pricing_profile", ChargeProfilePricingProfile.STANDARD)
                                        self.profile_type = getattr(eq, "profile_type", None)
                                cp_with_type = ChargeProfileWithType(charge_profile_early, equipment)
                                slot_dur = getattr(equipment, "slot_duration_minutes", None) or 60
                                red = _try_reduce_to_single_slot(
                                    equipment, cp_with_type, input_values, slot_dur
                                )
                                if red:
                                    reduced_input_values, _single_slot_time = red
                                    one_slot = _find_one_available_slot_in_window(
                                        equipment, user_type, is_admin,
                                        visible_week_start=visible_week_start,
                                        visible_week_end=visible_week_end,
                                        booking_user=booking_user,
                                    )
                                    if one_slot:
                                        slot_id, _slot_obj = one_slot
                                        slot_ids = [slot_id]
                                        daily_slots = DailySlot.objects.filter(id=slot_id).order_by("start_datetime")
                                        input_values = dict(reduced_input_values)
                                        single_slot_fallback_done = True
                                        input_values_adjusted_for_single_slot[0] = True
                                        # fall through; charge/quota/wallet will use new slot_ids, daily_slots, input_values
                            if not single_slot_fallback_done:
                                _create_booking_attempt_log(
                                    request, equipment, BookingAttemptOutcome.FAILED,
                                    failure_reason="Booking unsuccessful. All slots are occupied.",
                                    slots_requested=len(slot_ids),
                                    number_of_samples=request.data.get("number_of_samples") or 1,
                                    additional_info=_get_additional_info_from_request(request, equipment),
                                )
                                return Response(
                                    _enrich_failed_booking_response(
                                        equipment, booking_user,
                                        "Booking unsuccessful. All slots are occupied.",
                                        waitlist_on_failure=waitlist_on_failure,
                                        slot_unavailable_failure=True,
                                    ),
                                    status=status.HTTP_400_BAD_REQUEST,
                                )
                        if not single_slot_fallback_done:
                            _create_booking_attempt_log(
                                request, equipment, BookingAttemptOutcome.FAILED,
                                failure_reason="No alternative slots available for the requested duration.",
                                slots_requested=len(slot_ids),
                                number_of_samples=request.data.get("number_of_samples") or 1,
                                additional_info=_get_additional_info_from_request(request, equipment),
                            )
                            return Response(
                                _enrich_failed_booking_response(
                                    equipment,
                                    booking_user,
                                    "Selected slots are no longer available and no alternative slots could be found for the requested duration.",
                                    waitlist_on_failure=waitlist_on_failure,
                                    slot_unavailable_failure=True,
                                ),
                                status=status.HTTP_400_BAD_REQUEST,
                            )
                else:
                    _create_booking_attempt_log(
                        request, equipment, BookingAttemptOutcome.FAILED,
                        failure_reason="One or more slots are invalid or not available.",
                        slots_requested=len(slot_ids),
                        number_of_samples=request.data.get("number_of_samples") or 1,
                        additional_info=_get_additional_info_from_request(request, equipment),
                    )
                    return Response(
                        _enrich_failed_booking_response(
                            equipment,
                            booking_user,
                            "One or more slots are invalid or not available.",
                            waitlist_on_failure=waitlist_on_failure,
                            slot_unavailable_failure=True,
                        ),
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            else:
                _create_booking_attempt_log(
                    request, equipment, BookingAttemptOutcome.FAILED,
                    failure_reason="One or more slots are invalid or not available.",
                    slots_requested=len(slot_ids),
                    number_of_samples=request.data.get("number_of_samples") or 1,
                    additional_info=_get_additional_info_from_request(request, equipment),
                )
                return Response(
                    _enrich_failed_booking_response(
                        equipment,
                        booking_user,
                        "One or more slots are invalid or not available.",
                        waitlist_on_failure=waitlist_on_failure,
                        slot_unavailable_failure=True,
                    ),
                    status=status.HTTP_400_BAD_REQUEST,
                )
        perf.mark("slot_count_alternatives_done")
        if not is_admin:
            checker = (
                SlotAvailabilityChecker.is_slot_available_for_external
                if UserType.is_external_user(user_type)
                else SlotAvailabilityChecker.is_slot_available
            )
            unavailable_slots = [s.id for s in daily_slots if not checker(s)]
            if unavailable_slots:
                _create_booking_attempt_log(
                    request, equipment, BookingAttemptOutcome.FAILED,
                    failure_reason=f"Slots {unavailable_slots} are not available for booking.",
                    slots_requested=len(slot_ids),
                    number_of_samples=request.data.get("number_of_samples") or 1,
                    additional_info=_get_additional_info_from_request(request, equipment),
                )
                return Response(
                    _enrich_failed_booking_response(
                        equipment,
                        booking_user,
                        f"Slots {unavailable_slots} are not available for booking.",
                        waitlist_on_failure=waitlist_on_failure,
                        slot_unavailable_failure=True,
                    ),
                    status=status.HTTP_400_BAD_REQUEST,
                )
            if not UserType.is_external_user(user_type):
                home_denied = [
                    s.id for s in daily_slots
                    if not slot_allows_internal_user(s, booking_user, equipment)
                ]
                if home_denied:
                    from .slot_department_access import department_access_denial_message

                    denied_slots = [s for s in daily_slots if s.id in home_denied]
                    msg = department_access_denial_message(
                        denied_slots[0], booking_user, equipment
                    ) if denied_slots else (
                        f"Slots {home_denied} are not available for your department under "
                        "home / non-home reservation rules."
                    )
                    if len(home_denied) > 1:
                        msg = (
                            f"Slots {home_denied} are not available for your department under "
                            "home / non-home reservation rules."
                        )
                    _create_booking_attempt_log(
                        request, equipment, BookingAttemptOutcome.FAILED,
                        failure_reason=msg,
                        slots_requested=len(slot_ids),
                        number_of_samples=request.data.get("number_of_samples") or 1,
                        additional_info=_get_additional_info_from_request(request, equipment),
                    )
                    return Response(
                        _enrich_failed_booking_response(
                            equipment,
                            booking_user,
                            msg,
                            waitlist_on_failure=waitlist_on_failure,
                            slot_unavailable_failure=True,
                        ),
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            perf.mark("slot_availability_checked")
            # Enforce slot window (from/to): only slots fully within the window are bookable
            time_from = getattr(equipment, 'weekly_view_time_from', None)
            time_to = getattr(equipment, 'weekly_view_time_to', None)
            if time_from is not None or time_to is not None:
                from django.utils import timezone as tz
                out_of_window = []
                for s in daily_slots:
                    if not s.start_datetime or not s.end_datetime:
                        out_of_window.append(s.id)
                        continue
                    start_local = tz.localtime(s.start_datetime) if tz.is_aware(s.start_datetime) else s.start_datetime
                    end_local = tz.localtime(s.end_datetime) if tz.is_aware(s.end_datetime) else s.end_datetime
                    st, et = start_local.time(), end_local.time()
                    if time_from is not None and st < time_from:
                        out_of_window.append(s.id)
                    elif time_to is not None and et > time_to:
                        out_of_window.append(s.id)
                if out_of_window:
                    _create_booking_attempt_log(
                        request, equipment, BookingAttemptOutcome.FAILED,
                        failure_reason=f"Slots {out_of_window} are outside the allowed slot window (time from/to).",
                        slots_requested=len(slot_ids),
                        number_of_samples=request.data.get("number_of_samples") or 1,
                        additional_info=_get_additional_info_from_request(request, equipment),
                    )
                    return Response(
                        _enrich_failed_booking_response(
                            equipment, booking_user,
                            "One or more selected slots are outside the allowed booking window. Please choose slots within the configured time range.",
                            waitlist_on_failure=waitlist_on_failure,
                        ),
                        status=status.HTTP_400_BAD_REQUEST,
                    )
            # Slot window reference controls week visibility in the slots API; booking enforces only slot status
            # and time window (weekly_view_time_from/to). Current week remains bookable before reference.
        perf.mark("availability_window_checks_done")

        # Multi-mode: mode must be active on each slot date; exclusive days share physical instrument.
        # Admin and OIC bypass all multi-mode schedule restrictions.
        from .mode_utils import (
            bypasses_multimode_restrictions,
            equipment_bookable_on_date,
            family_slots_overlap_conflict,
        )
        if not bypasses_multimode_restrictions(request.user):
            for s in daily_slots:
                at_time = None
                if s.start_datetime:
                    _dt = timezone.localtime(s.start_datetime) if timezone.is_aware(s.start_datetime) else s.start_datetime
                    at_time = _dt.time()
                ok, mode_err = equipment_bookable_on_date(equipment, s.date, at_time)
                if not ok:
                    _create_booking_attempt_log(
                        request, equipment, BookingAttemptOutcome.FAILED,
                        failure_reason=mode_err or "Mode not available on selected date.",
                        slots_requested=len(slot_ids),
                        number_of_samples=request.data.get("number_of_samples") or 1,
                        additional_info=_get_additional_info_from_request(request, equipment),
                    )
                    return Response(
                        _enrich_failed_booking_response(
                            equipment,
                            booking_user,
                            mode_err or "This equipment mode is not available on the selected date.",
                            waitlist_on_failure=waitlist_on_failure,
                            slot_unavailable_failure=True,
                        ),
                        status=status.HTTP_400_BAD_REQUEST,
                    )
                if s.start_datetime and s.end_datetime:
                    conflict = family_slots_overlap_conflict(
                        equipment,
                        s.start_datetime,
                        s.end_datetime,
                        exclude_slot_ids=slot_ids,
                    )
                    if conflict is not None:
                        msg = (
                            "This instrument is already booked in another mode for an overlapping time. "
                            "Existing bookings are respected while modes share the physical instrument."
                        )
                        _create_booking_attempt_log(
                            request, equipment, BookingAttemptOutcome.FAILED,
                            failure_reason=msg,
                            slots_requested=len(slot_ids),
                            number_of_samples=request.data.get("number_of_samples") or 1,
                            additional_info=_get_additional_info_from_request(request, equipment),
                        )
                        return Response(
                            _enrich_failed_booking_response(
                                equipment,
                                booking_user,
                                msg,
                                waitlist_on_failure=waitlist_on_failure,
                                slot_unavailable_failure=True,
                            ),
                            status=status.HTTP_400_BAD_REQUEST,
                        )

        # Sum each slot's duration (staggered slots OK; no requirement that slots be consecutive)
        total_time_minutes = sum(
            int((s.end_datetime - s.start_datetime).total_seconds() / 60)
            for s in daily_slots
        )
        start_time = daily_slots.first().start_datetime
        end_time = daily_slots.last().end_datetime
        booking_date = start_time
        try:
            charge_profile = ChargeProfile.objects.get(
                equipment=equipment,
                user_type=user_type,
                pricing_profile=_get_charge_profile_pricing_profile_for_user(booking_user, equipment),
                is_active=True,
            )
        except ChargeProfile.DoesNotExist:
            _create_booking_attempt_log(
                request, equipment, BookingAttemptOutcome.FAILED,
                failure_reason=f"No active charge profile found for equipment {pk} and user type {user_type}.",
                slots_requested=len(slot_ids),
                number_of_samples=request.data.get("number_of_samples") or 1,
                duration_minutes=total_time_minutes,
                additional_info=_get_additional_info_from_request(request, equipment),
            )
            return Response(
                _enrich_failed_booking_response(
                    equipment,
                    booking_user,
                    f"No active charge profile found for equipment {pk} and user type {user_type}.",
                    waitlist_on_failure=waitlist_on_failure,
                ),
                status=status.HTTP_404_NOT_FOUND,
            )
        perf.mark("charge_profile_loaded")
        class ChargeProfileWithType:
            def __init__(self, charge_profile, equipment):
                self.equipment = charge_profile.equipment
                self.user_type = charge_profile.user_type
                self.is_active = charge_profile.is_active
                self.primary_unit_charge = charge_profile.primary_unit_charge
                self.secondary_unit_charge = charge_profile.secondary_unit_charge
                self.breakpoint = charge_profile.breakpoint
                self.time_formula = charge_profile.time_formula
                self.pricing_profile = getattr(charge_profile, "pricing_profile", ChargeProfilePricingProfile.STANDARD)
                self.profile_type = equipment.profile_type
        charge_profile_with_type = ChargeProfileWithType(charge_profile, equipment)
        from .calculators import build_safe_input_values_for_charge_calculation
        safe_input_values = build_safe_input_values_for_charge_calculation(input_values, equipment=equipment)
        try:
            calculated_charge, charge_breakdown = ChargeCalculationEngine.calculate_charge(
                charge_profile_with_type,
                safe_input_values,
                total_time_minutes,
                selected_parameters=None,
            )
        except Exception as e:
            return Response(
                {"error": f"Error calculating charge: {str(e)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        perf.mark("charge_calculated")
        total_charge = calculated_charge
        sample_return_after_analysis = False
        return_shipping_fee_amount = Decimal("0.00")
        atmosphere_raw = request.data.get("atmosphere_sensitive_sample")
        if atmosphere_raw is None:
            atmosphere_sensitive_sample = False
        elif isinstance(atmosphere_raw, bool):
            atmosphere_sensitive_sample = atmosphere_raw
        elif isinstance(atmosphere_raw, (int, float)):
            atmosphere_sensitive_sample = bool(atmosphere_raw)
        else:
            atmosphere_sensitive_sample = str(atmosphere_raw).strip().lower() in ("1", "true", "yes", "y")
        if UserType.is_external_user(user_type):
            # External option: return samples after analysis => add return shipping fee BEFORE GST.
            raw = request.data.get("sample_return_after_analysis")
            if raw is None:
                sample_return_after_analysis = False
            elif isinstance(raw, bool):
                sample_return_after_analysis = raw
            elif isinstance(raw, (int, float)):
                sample_return_after_analysis = bool(raw)
            else:
                sample_return_after_analysis = str(raw).strip().lower() in ("1", "true", "yes", "y")
            if sample_return_after_analysis:
                try:
                    return_shipping_fee_amount = get_external_return_shipping_fee_amount().quantize(Decimal("0.01"))
                except Exception:
                    return_shipping_fee_amount = Decimal("0.00")
                if return_shipping_fee_amount > 0:
                    total_charge = (Decimal(total_charge) + return_shipping_fee_amount).quantize(Decimal("0.01"))
                    charge_breakdown = list(charge_breakdown) + [
                        {"description": "Return shipping charges", "amount": float(return_shipping_fee_amount)},
                    ]
            gst_percent = get_external_gst_percent()
            if gst_percent > 0:
                gst_amount = (total_charge * gst_percent / Decimal("100")).quantize(Decimal("0.01"))
                total_charge = (total_charge + gst_amount).quantize(Decimal("0.01"))
                charge_breakdown = list(charge_breakdown) + [
                    {"description": f"GST ({gst_percent}%)", "amount": float(gst_amount)},
                ]
        reward_points_requested = Decimal(str(request.data.get("reward_points_to_redeem") or "0"))
        reward_points_applied = Decimal("0.00")
        reward_discount_amount = Decimal("0.00")
        if reward_points_requested > 0:
            reward_points_applied, reward_discount_amount, reward_err = _calculate_reward_redemption(
                total_charge, reward_points_requested, booking_user, equipment=equipment, lock_ledger=True
            )
            if reward_err:
                return Response({"error": reward_err}, status=status.HTTP_400_BAD_REQUEST)
            if reward_discount_amount > 0:
                total_charge = max(Decimal("0.00"), total_charge - reward_discount_amount).quantize(Decimal("0.01"))
                charge_breakdown = list(charge_breakdown) + [
                    {"description": "TA Reward Points", "amount": -float(reward_discount_amount)},
                ]
        if not is_admin and not booking_quota_should_skip(equipment):
            QUOTA_TYPE_WEEKLY = 'WEEKLY'
            QUOTA_TYPE_MONTHLY = 'MONTHLY'
            quota_allowed, quota_error = QuotaChecker.check_user_quota(
                user=booking_user,
                equipment=equipment,
                quota_type=QUOTA_TYPE_WEEKLY,
                additional_time_minutes=total_time_minutes,
                additional_bookings=1,
                additional_charge=total_charge,
                booking_date=booking_date,
            )
            if not quota_allowed:
                _create_booking_attempt_log(
                    request, equipment, BookingAttemptOutcome.FAILED,
                    failure_reason=f"Weekly quota check failed: {quota_error}",
                slots_requested=len(slot_ids),
                number_of_samples=request.data.get("number_of_samples") or 1,
                duration_minutes=total_time_minutes,
                additional_info=_get_additional_info_from_request(request, equipment),
            )
                return Response({"error": f"Weekly quota check failed: {quota_error}"}, status=status.HTTP_400_BAD_REQUEST)
            quota_allowed, quota_error = QuotaChecker.check_user_quota(
                user=booking_user,
                equipment=equipment,
                quota_type=QUOTA_TYPE_MONTHLY,
                additional_time_minutes=total_time_minutes,
                additional_bookings=1,
                additional_charge=total_charge,
                booking_date=booking_date,
            )
            if not quota_allowed:
                _create_booking_attempt_log(
                    request, equipment, BookingAttemptOutcome.FAILED,
                    failure_reason=f"Monthly quota check failed: {quota_error}",
                slots_requested=len(slot_ids),
                number_of_samples=request.data.get("number_of_samples") or 1,
                duration_minutes=total_time_minutes,
                additional_info=_get_additional_info_from_request(request, equipment),
            )
                return Response({"error": f"Monthly quota check failed: {quota_error}"}, status=status.HTTP_400_BAD_REQUEST)
        perf.mark("quota_checks_done")
        status_map = {'booked': BookingStatus.BOOKED, 'hold': BookingStatus.HOLD}
        booking_status_enum = BookingStatus.HOLD if create_as_hold else status_map.get(booking_status.lower(), BookingStatus.BOOKED)
        from iic_booking.users.repositories.wallet_repository import WalletRepository
        booking_target, _ = WalletRepository.get_booking_wallet_target(
            booking_user, getattr(equipment, "internal_department", None)
        )
        if not booking_target:
            _create_booking_attempt_log(
                request, equipment, BookingAttemptOutcome.FAILED,
                failure_reason="You don't have access to any wallet.",
                slots_requested=len(slot_ids),
                number_of_samples=request.data.get("number_of_samples") or 1,
                duration_minutes=total_time_minutes,
                additional_info=_get_additional_info_from_request(request, equipment),
            )
            return Response(
                {"error": "You don't have access to any wallet."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        booking_target.refresh_from_db()
        from iic_booking.equipment.booking_payment_service import compute_booking_payment_split

        wallet_applied_precheck, amount_due_precheck, bal_err = compute_booking_payment_split(
            booking_target, total_charge, user_type=user_type, create_as_hold=create_as_hold
        )
        if bal_err:
            _create_booking_attempt_log(
                request, equipment, BookingAttemptOutcome.FAILED,
                failure_reason=bal_err or "Insufficient wallet balance",
                slots_requested=len(slot_ids),
                number_of_samples=request.data.get("number_of_samples") or 1,
                duration_minutes=total_time_minutes,
                additional_info=_get_additional_info_from_request(request, equipment),
            )
            return Response(
                {"error": bal_err or "Insufficient wallet balance"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        perf.mark("wallet_precheck_done")
        try:
            debit_transaction = None
            with transaction.atomic():
                # Lock slots at DB level to prevent concurrent booking of the same slot (millisecond-level serialization)
                lock_filter = {"id__in": slot_ids, "slot_master__equipment": equipment}
                if is_admin:
                    locked_slots_qs = DailySlot.objects.select_for_update().filter(**lock_filter).exclude(status=SlotStatus.BOOKED).order_by('start_datetime')
                else:
                    lock_filter["status"] = SlotStatus.AVAILABLE
                    lock_filter["reserved_for_external"] = UserType.is_external_user(user_type)
                    locked_slots_qs = DailySlot.objects.select_for_update().filter(**lock_filter).order_by('start_datetime')
                    locked_slots_qs = filter_queryset_for_home_department(
                        locked_slots_qs,
                        user=booking_user,
                        equipment=equipment,
                        is_admin=False,
                        is_external=UserType.is_external_user(user_type),
                    )
                locked_slots = list(locked_slots_qs)
                perf.mark("select_for_update_slots")
                if len(locked_slots) != len(slot_ids):
                    if book_any_available_slots:
                        # Try to allocate any other available slots (distributed OK) from the visible week only
                        alt_filter = {"slot_master__equipment": equipment, "status": SlotStatus.AVAILABLE}
                        if not is_admin:
                            alt_filter["reserved_for_external"] = UserType.is_external_user(user_type)
                        if visible_week_start is not None:
                            alt_filter["date__gte"] = visible_week_start
                        if visible_week_end is not None:
                            alt_filter["date__lte"] = visible_week_end
                        alt_qs = DailySlot.objects.select_for_update(skip_locked=True).filter(**alt_filter).order_by(
                            "start_datetime"
                        )
                        if not is_admin:
                            alt_qs = filter_queryset_for_home_department(
                                alt_qs,
                                user=booking_user,
                                equipment=equipment,
                                is_admin=False,
                                is_external=UserType.is_external_user(user_type),
                            )
                        alt_qs = alt_qs[:_BOOK_ANY_ALT_SLOT_SCAN_LIMIT]
                        time_from = getattr(equipment, "weekly_view_time_from", None)
                        time_to = getattr(equipment, "weekly_view_time_to", None)
                        collected = []
                        total_minutes_alt = 0
                        for slot in alt_qs:
                            if (time_from is not None or time_to is not None) and slot.start_datetime and slot.end_datetime:
                                from django.utils import timezone as tz
                                start_local = tz.localtime(slot.start_datetime) if tz.is_aware(slot.start_datetime) else slot.start_datetime
                                end_local = tz.localtime(slot.end_datetime) if tz.is_aware(slot.end_datetime) else slot.end_datetime
                                st, et = start_local.time(), end_local.time()
                                if time_from is not None and st < time_from:
                                    continue
                                if time_to is not None and et > time_to:
                                    continue
                            delta = (slot.end_datetime - slot.start_datetime) if slot.start_datetime and slot.end_datetime else None
                            if not delta:
                                continue
                            mins = int(delta.total_seconds() / 60)
                            collected.append(slot)
                            total_minutes_alt += mins
                            if total_minutes_alt >= total_time_minutes:
                                break
                        if collected and total_minutes_alt >= total_time_minutes:
                            slot_ids = [s.id for s in collected]
                            locked_slots = collected
                            total_time_minutes = total_minutes_alt
                            start_time = locked_slots[0].start_datetime
                            end_time = locked_slots[-1].end_datetime
                            calculated_charge, charge_breakdown = ChargeCalculationEngine.calculate_charge(
                                charge_profile_with_type, safe_input_values, total_time_minutes, selected_parameters=None
                            )
                            total_charge = calculated_charge
                            if UserType.is_external_user(user_type):
                                gst_percent = get_external_gst_percent()
                                if gst_percent > 0:
                                    gst_amount = (total_charge * gst_percent / Decimal("100")).quantize(Decimal("0.01"))
                                    total_charge = (total_charge + gst_amount).quantize(Decimal("0.01"))
                                    charge_breakdown = list(charge_breakdown) + [
                                        {"description": f"GST ({gst_percent}%)", "amount": float(gst_amount)},
                                    ]
                            if reward_points_requested > 0:
                                reward_points_applied, reward_discount_amount, reward_err = _calculate_reward_redemption(
                                    total_charge, reward_points_requested, booking_user, equipment=equipment, lock_ledger=True
                                )
                                if reward_err:
                                    raise ValueError(reward_err)
                                if reward_discount_amount > 0:
                                    total_charge = max(Decimal("0.00"), total_charge - reward_discount_amount).quantize(Decimal("0.01"))
                                    charge_breakdown = list(charge_breakdown) + [
                                        {"description": "TA Reward Points", "amount": -float(reward_discount_amount)},
                                    ]
                            booking_target.refresh_from_db()
                            from iic_booking.users.wallet_credit_facility import subwallet_booking_balance_ok

                            ok_alt, alt_err = subwallet_booking_balance_ok(
                                booking_target, total_charge, create_as_hold
                            )
                            if not ok_alt:
                                raise ValueError(alt_err or "Insufficient wallet balance for alternative slots")
                        else:
                            # Try reduce to 1 slot and book one if "Book even if single slot available" is checked
                            if book_even_if_single_slot_available:
                                slot_dur = getattr(equipment, "slot_duration_minutes", None) or 60
                                red = _try_reduce_to_single_slot(
                                    equipment, charge_profile_with_type, input_values, slot_dur
                                )
                                if red:
                                    reduced_input_values, single_slot_time = red
                                    one_slot_qs = DailySlot.objects.select_for_update(skip_locked=True).filter(
                                        **alt_filter
                                    ).order_by("start_datetime")[:_BOOK_ANY_ALT_SLOT_SCAN_LIMIT]
                                    time_from_inner = getattr(equipment, "weekly_view_time_from", None)
                                    time_to_inner = getattr(equipment, "weekly_view_time_to", None)
                                    one_slot = None
                                    for slot in one_slot_qs:
                                        if (time_from_inner is not None or time_to_inner is not None) and slot.start_datetime and slot.end_datetime:
                                            from django.utils import timezone as tz
                                            start_local = tz.localtime(slot.start_datetime) if tz.is_aware(slot.start_datetime) else slot.start_datetime
                                            end_local = tz.localtime(slot.end_datetime) if tz.is_aware(slot.end_datetime) else slot.end_datetime
                                            st, et = start_local.time(), end_local.time()
                                            if time_from_inner is not None and st < time_from_inner:
                                                continue
                                            if time_to_inner is not None and et > time_to_inner:
                                                continue
                                        one_slot = slot
                                        break
                                    if one_slot:
                                        slot_ids = [one_slot.id]
                                        locked_slots = [one_slot]
                                        total_time_minutes = single_slot_time
                                        start_time = one_slot.start_datetime
                                        end_time = one_slot.end_datetime
                                        input_values = dict(reduced_input_values)
                                        input_values_adjusted_for_single_slot[0] = True
                                        calculated_charge, charge_breakdown = ChargeCalculationEngine.calculate_charge(
                                            charge_profile_with_type, reduced_input_values, single_slot_time, selected_parameters=None
                                        )
                                        total_charge = calculated_charge
                                        if UserType.is_external_user(user_type):
                                            gst_percent = get_external_gst_percent()
                                            if gst_percent > 0:
                                                gst_amount = (total_charge * gst_percent / Decimal("100")).quantize(Decimal("0.01"))
                                                total_charge = (total_charge + gst_amount).quantize(Decimal("0.01"))
                                                charge_breakdown = list(charge_breakdown) + [
                                                    {"description": f"GST ({gst_percent}%)", "amount": float(gst_amount)},
                                                ]
                                        if reward_points_requested > 0:
                                            reward_points_applied, reward_discount_amount, reward_err = _calculate_reward_redemption(
                                                total_charge, reward_points_requested, booking_user, equipment=equipment, lock_ledger=True
                                            )
                                            if reward_err:
                                                raise ValueError(reward_err)
                                            if reward_discount_amount > 0:
                                                total_charge = max(Decimal("0.00"), total_charge - reward_discount_amount).quantize(Decimal("0.01"))
                                                charge_breakdown = list(charge_breakdown) + [
                                                    {"description": "TA Reward Points", "amount": -float(reward_discount_amount)},
                                                ]
                                        booking_target.refresh_from_db()
                                        from iic_booking.users.wallet_credit_facility import subwallet_booking_balance_ok

                                        ok_ss, ss_err = subwallet_booking_balance_ok(
                                            booking_target, total_charge, create_as_hold
                                        )
                                        if not ok_ss:
                                            raise ValueError(ss_err or "Insufficient wallet balance for single slot")
                                    else:
                                        raise ValueError("Booking unsuccessful. All slots are occupied.")
                                else:
                                    raise ValueError("Booking unsuccessful. All slots are occupied.")
                            else:
                                raise ValueError(SLOTS_ALREADY_OCCUPIED_MESSAGE)
                    else:
                        raise ValueError(SLOTS_ALREADY_OCCUPIED_MESSAGE)
                perf.mark("slot_lock_path_resolved")
                debit_transaction = None
                wallet_applied = wallet_applied_precheck
                amount_due = amount_due_precheck
                settlement_dept = getattr(equipment, "internal_department", None)
                if wallet_applied > 0 and not create_as_hold:
                    from iic_booking.users.wallet_credit_facility import subwallet_minimum_balance_after_debit

                    transaction_description = f"Booking #{equipment.code} - {equipment.name} ({total_time_minutes} minutes)"
                    transaction_description += _student_booking_description_suffix(booking_target, booking_user)
                    if amount_due > 0:
                        transaction_description += f" (partial ₹{wallet_applied:.2f}; ₹{amount_due:.2f} due)"
                    debit_transaction = booking_target.debit(
                        amount=wallet_applied,
                        description=transaction_description,
                        related_user=booking_user,
                        minimum_balance_after=subwallet_minimum_balance_after_debit(booking_target),
                    )
                perf.mark("after_wallet_debit_phase")
                if not create_as_hold and amount_due > 0 and UserType.is_external_user(user_type):
                    booking_status_enum = BookingStatus.PENDING_PAYMENT
                istem_fbr_defaults = (
                    initial_istem_fbr_fields_for_charge_profile(charge_profile)
                    if booking_status_enum == BookingStatus.BOOKED
                    else {}
                )
                booking = Booking.objects.create(
                    user=booking_user,
                    equipment=equipment,
                    charge_profile=charge_profile,
                    user_type_snapshot=user_type,
                    total_time_minutes=total_time_minutes,
                    total_charge=total_charge,
                    input_values=input_values,
                    selected_parameters=None,
                    charge_breakdown=charge_breakdown,
                    status=booking_status_enum,
                    notes=notes,
                    reward_points_used=reward_points_applied,
                    reward_discount_amount=reward_discount_amount,
                    sample_return_after_analysis=sample_return_after_analysis if UserType.is_external_user(user_type) else False,
                    return_shipping_fee_amount=return_shipping_fee_amount if (UserType.is_external_user(user_type) and sample_return_after_analysis) else Decimal("0.00"),
                    atmosphere_sensitive_sample=atmosphere_sensitive_sample,
                    wallet_amount_applied=wallet_applied,
                    amount_due=amount_due,
                    settlement_department=settlement_dept,
                    created_by=request.user,
                    print_analysis=print_analysis_obj,
                    print_analysis_batch=print_analysis_batch_obj,
                    **istem_fbr_defaults,
                )
                if getattr(equipment, "profile_type", None) == EquipmentProfileType.PRINT_3D:
                    from .print_3d_views import link_print_analyses_to_booking

                    link_print_analyses_to_booking(
                        booking,
                        print_analysis_obj=print_analysis_obj,
                        print_analysis_batch_obj=print_analysis_batch_obj,
                    )
                perf.mark("booking_row_inserted")
                _schedule_equipment_booking_requester_group_upsert(equipment.pk, booking_user.pk)
                if reward_points_applied > 0 and reward_discount_amount > 0:
                    redemption_entry = TARewardLedger.objects.create(
                        student=booking_user,
                        entry_type=TARewardLedgerEntryType.REDEEM,
                        points=-reward_points_applied,
                        currency_value=reward_discount_amount,
                        source_type=TARewardLedgerSourceType.BOOKING,
                        source_id=booking.booking_id,
                        description=f"Redeemed on booking {booking_display_id_for_email(booking)}",
                        created_by=request.user if request.user.is_authenticated else None,
                    )
                    BookingRewardRedemption.objects.create(
                        booking=booking,
                        student=booking_user,
                        points_used=reward_points_applied,
                        discount_amount=reward_discount_amount,
                        ledger_entry=redemption_entry,
                    )
                DailySlot.objects.filter(id__in=slot_ids).update(booking=booking, status=SlotStatus.BOOKED)
                perf.mark("daily_slots_marked_booked")
                create_booking_event(
                    booking=booking,
                    event_type=BookingEventType.CREATED,
                    created_by=request.user,
                    comment=(
                        f"{'Hold created' if create_as_hold else 'Booking created'} for {equipment.name} "
                        f"({total_time_minutes} minutes, ₹{total_charge:.2f})"
                        + (f" on behalf of user {booking_user.id}" if booking_user != request.user else "")
                        + (f"; ₹{amount_due:.2f} payment pending" if amount_due > 0 else "")
                        + (
                            "; Atmosphere-sensitive sample — submit at slot start; do not mark Not Utilized before slot start"
                            if atmosphere_sensitive_sample
                            else ""
                        )
                    ),
                    new_status=booking.status,
                    metadata={"atmosphere_sensitive_sample": True} if atmosphere_sensitive_sample else None,
                    # HOLD / pending payment: do not send booking-confirmed notifications yet.
                    send_notification=not create_as_hold and amount_due <= 0,
                )
                if atmosphere_sensitive_sample and not create_as_hold:
                    try:
                        from iic_booking.equipment.reports import get_equipment_staff_notify_users
                        from iic_booking.communication.service import CommunicationService
                        from iic_booking.communication.utils import booking_display_id_for_email as _bdisp

                        ref = _bdisp(booking)
                        for staff in get_equipment_staff_notify_users(equipment):
                            if not staff or not getattr(staff, "email", None):
                                continue
                            CommunicationService.send_push_notification(
                                recipient=staff,
                                title="Atmosphere-sensitive sample",
                                message=(
                                    f"{ref} — {equipment.name}: sample will be brought at slot start. "
                                    "Do not mark Booking Not Utilized before the slot begins."
                                ),
                                metadata={
                                    "booking_id": booking.booking_id,
                                    "atmosphere_sensitive_sample": True,
                                    "notification_type": "warning",
                                },
                            )
                    except Exception:
                        logger.exception(
                            "Failed atmosphere-sensitive staff notify for booking %s",
                            getattr(booking, "booking_id", None),
                        )
                perf.mark("booking_event_created")
                if debit_transaction and getattr(booking, "virtual_booking_id", None):
                    vid = (booking.virtual_booking_id or "").strip()
                    if vid and vid not in (debit_transaction.description or ""):
                        debit_transaction.description = f"{debit_transaction.description.rstrip()} | Ref: {vid}"
                        debit_transaction.save(update_fields=["description"])
                # Wallet deduction email is skipped; booking confirmation email includes final wallet balance.
            perf.mark("inner_atomic_exited")
            # Log successful booking attempt (server-side)
            num_samples = int(request.data.get("number_of_samples") or 1)
            _create_booking_attempt_log(
                request,
                equipment,
                BookingAttemptOutcome.SUCCESS,
                booking_id=booking.booking_id,
                slots_requested=len(slot_ids),
                number_of_samples=max(1, num_samples),
                duration_minutes=total_time_minutes,
                additional_info=_get_additional_info_from_request(request, equipment),
            )
            perf.mark("booking_attempt_log_written")
            daily_slots = (
                DailySlot.objects.filter(id__in=slot_ids)
                .order_by("start_datetime")
                .select_related("slot_master", "slot_master__equipment", "booking")
            )
            perf.mark("before_daily_slot_serializer_data")
            daily_slots_response = _serialize_booked_daily_slots_for_booking_response(daily_slots)
            perf.mark("after_daily_slot_serializer_data")
            display_booking_id = booking_display_id_for_email(booking)
            resp_data = {
                "booking_id": display_booking_id,
                "virtual_booking_id": booking.virtual_booking_id,
                "real_booking_id": booking.booking_id,
                "equipment_id": equipment.equipment_id,
                "user": booking_user.id,
                "start_time": start_time.isoformat() if hasattr(start_time, 'isoformat') else str(start_time),
                "end_time": end_time.isoformat() if hasattr(end_time, 'isoformat') else str(end_time),
                "total_time_minutes": total_time_minutes,
                "total_charge": str(total_charge),
                "charge_breakdown": charge_breakdown,
                "status": booking.status.lower(),
                "daily_slots": daily_slots_response,
                "created_at": booking.created_at.isoformat() if hasattr(booking.created_at, 'isoformat') else str(booking.created_at),
                "updated_at": booking.updated_at.isoformat() if hasattr(booking.updated_at, 'isoformat') else str(booking.updated_at),
                "reward": {
                    "points_used": str(booking.reward_points_used or "0.00"),
                    "discount_amount": str(booking.reward_discount_amount or "0.00"),
                },
                "wallet_amount_applied": str(wallet_applied),
                "amount_due": str(amount_due),
                "payment_required": amount_due > 0,
                "settlement_department_id": settlement_dept.id if settlement_dept else None,
                "require_istem_fbr": charge_profile_requires_istem_fbr(charge_profile),
                "istem_portal_url": get_equipment_istem_portal_url(equipment),
                "istem_fbr_status": booking.istem_fbr_status,
            }
            if input_values_adjusted_for_single_slot[0]:
                resp_data["input_values_adjusted"] = True
                resp_data["input_values"] = booking.input_values
            perf.mark("response_payload_ready")
            ok_response = Response(resp_data, status=status.HTTP_201_CREATED)
            attach_booking_performance_headers(ok_response, perf, equipment_id=equipment.equipment_id)
            return ok_response
        except ValueError as e:
            err_msg = str(e)
            try:
                _create_booking_attempt_log(
                    request, equipment, BookingAttemptOutcome.FAILED,
                    failure_reason=err_msg,
                    slots_requested=len(slot_ids),
                    number_of_samples=request.data.get("number_of_samples") or 1,
                    duration_minutes=total_time_minutes,
                    additional_info=_get_additional_info_from_request(request, equipment),
                )
            except TransactionManagementError:
                pass  # Request is inside ATOMIC_REQUESTS and inner atomic failed; skip logging, still return 400

            # For slot-occupancy/all-slots-occupied errors, route through waitlist enrichment
            # so frontend can show WL1/WL2/... when queue has space.
            err_lower = err_msg.lower()
            slot_occupied_error = ("occupied" in err_lower) or ("all slots" in err_lower)
            if slot_occupied_error:
                return Response(
                    _enrich_failed_booking_response(
                        equipment,
                        booking_user,
                        err_msg,
                        waitlist_on_failure=waitlist_on_failure,
                        slot_unavailable_failure=True,
                    ),
                    status=status.HTTP_400_BAD_REQUEST,
                )

            return Response({"error": err_msg}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:
            err_msg = str(e).lower()
            # Treat slot/lock/concurrent-booking errors as 400 with clear message (avoid 500 for "late" user)
            if any(
                x in err_msg
                for x in ("slot", "lock", "available", "booked", "no longer", "already", "occupied")
            ):
                try:
                    _create_booking_attempt_log(
                        request, equipment, BookingAttemptOutcome.FAILED,
                        failure_reason=SLOTS_ALREADY_OCCUPIED_MESSAGE,
                        slots_requested=len(slot_ids),
                        number_of_samples=request.data.get("number_of_samples") or 1,
                        duration_minutes=total_time_minutes,
                        additional_info=_get_additional_info_from_request(request, equipment),
                    )
                except TransactionManagementError:
                    pass
                return Response(
                    _enrich_failed_booking_response(
                        equipment,
                        booking_user,
                        SLOTS_ALREADY_OCCUPIED_MESSAGE,
                        waitlist_on_failure=waitlist_on_failure,
                        slot_unavailable_failure=True,
                    ),
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                _create_booking_attempt_log(
                    request, equipment, BookingAttemptOutcome.FAILED,
                    failure_reason=f"Error creating booking: {str(e)}",
                    slots_requested=len(slot_ids),
                    number_of_samples=request.data.get("number_of_samples") or 1,
                    duration_minutes=total_time_minutes,
                    additional_info=_get_additional_info_from_request(request, equipment),
                )
            except TransactionManagementError:
                pass
            return Response(
                {"error": f"Error creating booking: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    # Validate required fields (when not using slot_ids)
    if not start_time_str or not end_time_str:
        _create_booking_attempt_log(
            request, equipment, BookingAttemptOutcome.FAILED,
            failure_reason="start_time and end_time are required.",
            number_of_samples=request.data.get("number_of_samples") or 1,
            additional_info=_get_additional_info_from_request(request, equipment),
        )
        return Response(
            {"error": "start_time and end_time are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Parse datetime strings
    try:
        start_time = parse_datetime(start_time_str)
        end_time = parse_datetime(end_time_str)
        
        if not start_time or not end_time:
            raise ValueError("Invalid datetime format")
        
        # Convert to timezone-aware if needed (module-level django.utils.timezone)
        if timezone.is_naive(start_time):
            start_time = timezone.make_aware(start_time)
        if timezone.is_naive(end_time):
            end_time = timezone.make_aware(end_time)
            
    except (ValueError, TypeError) as e:
        _create_booking_attempt_log(
            request, equipment, BookingAttemptOutcome.FAILED,
            failure_reason=f"Invalid datetime format. Error: {str(e)}",
            number_of_samples=request.data.get("number_of_samples") or 1,
            additional_info=_get_additional_info_from_request(request, equipment),
        )
        return Response(
            {"error": f"Invalid datetime format. Use ISO 8601 format (e.g., 2026-01-09T04:30:00.000Z). Error: {str(e)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Validate time range
    if end_time <= start_time:
        _create_booking_attempt_log(
            request, equipment, BookingAttemptOutcome.FAILED,
            failure_reason="end_time must be after start_time.",
            number_of_samples=request.data.get("number_of_samples") or 1,
            additional_info=_get_additional_info_from_request(request, equipment),
        )
        return Response(
            {"error": "end_time must be after start_time."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    from .mode_utils import (
        bypasses_multimode_restrictions,
        equipment_bookable_on_date,
        family_slots_overlap_conflict,
    )
    if not bypasses_multimode_restrictions(request.user):
        slot_date = timezone.localtime(start_time).date()
        at_time = timezone.localtime(start_time).time()
        ok, mode_err = equipment_bookable_on_date(equipment, slot_date, at_time)
        if not ok:
            _create_booking_attempt_log(
                request, equipment, BookingAttemptOutcome.FAILED,
                failure_reason=mode_err or "Mode not available on selected date.",
                number_of_samples=request.data.get("number_of_samples") or 1,
                additional_info=_get_additional_info_from_request(request, equipment),
            )
            return Response(
                {
                    "error": mode_err
                    or "This equipment mode is not available on the selected date.",
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        conflict = family_slots_overlap_conflict(equipment, start_time, end_time)
        if conflict is not None:
            msg = (
                "This instrument is already booked in another mode for an overlapping time. "
                "Existing bookings are respected while modes share the physical instrument."
            )
            _create_booking_attempt_log(
                request, equipment, BookingAttemptOutcome.FAILED,
                failure_reason=msg,
                number_of_samples=request.data.get("number_of_samples") or 1,
                additional_info=_get_additional_info_from_request(request, equipment),
            )
            return Response({"error": msg}, status=status.HTTP_400_BAD_REQUEST)
    
    # Get charge profile
    try:
        charge_profile = ChargeProfile.objects.get(
            equipment=equipment,
            user_type=user_type,
                pricing_profile=_get_charge_profile_pricing_profile_for_user(booking_user, equipment),
            is_active=True
        )
    except ChargeProfile.DoesNotExist:
        _create_booking_attempt_log(
            request, equipment, BookingAttemptOutcome.FAILED,
            failure_reason=f"No active charge profile found for equipment {pk} and user type {user_type}.",
            number_of_samples=request.data.get("number_of_samples") or 1,
            additional_info=_get_additional_info_from_request(request, equipment),
        )
        return Response(
            {"error": f"No active charge profile found for equipment {pk} and user type {user_type}."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Find daily slots that overlap with the requested time range
    # Slots that start before end_time and end after start_time
    slots_filter = {
        "slot_master__equipment": equipment,
        "start_datetime__lt": end_time,
        "end_datetime__gt": start_time,
        "status": SlotStatus.AVAILABLE,
    }
    if UserType.is_external_user(user_type):
        slots_filter["reserved_for_external"] = True
    else:
        slots_filter["reserved_for_external"] = False
    daily_slots = DailySlot.objects.filter(**slots_filter).order_by('start_datetime')
    daily_slots = filter_queryset_for_home_department(
        daily_slots,
        user=booking_user,
        equipment=equipment,
        is_admin=is_admin,
        is_external=UserType.is_external_user(user_type),
    )
    
    if not daily_slots.exists():
        _create_booking_attempt_log(
            request, equipment, BookingAttemptOutcome.FAILED,
            failure_reason="No available slots found for the requested time range.",
            number_of_samples=request.data.get("number_of_samples") or 1,
            additional_info=_get_additional_info_from_request(request, equipment),
        )
        return Response(
            {"error": "No available slots found for the requested time range."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Check if all slots are available
    slot_checker = (
        SlotAvailabilityChecker.is_slot_available_for_external
        if UserType.is_external_user(user_type)
        else SlotAvailabilityChecker.is_slot_available
    )
    unavailable_slots = []
    for slot in daily_slots:
        if not slot_checker(slot):
            unavailable_slots.append(slot.id)
    
    if unavailable_slots:
        _create_booking_attempt_log(
            request, equipment, BookingAttemptOutcome.FAILED,
            failure_reason=f"Slots {unavailable_slots} are not available for booking.",
            slots_requested=daily_slots.count(),
            number_of_samples=request.data.get("number_of_samples") or 1,
            additional_info=_get_additional_info_from_request(request, equipment),
        )
        return Response(
            {"error": f"Slots {unavailable_slots} are not available for booking."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Calculate time and charge
    # Always calculate to ensure accuracy, but allow override if provided
    try:
        # Create a temporary charge profile object with profile_type from equipment
        class ChargeProfileWithType:
            def __init__(self, charge_profile, equipment):
                self.equipment = charge_profile.equipment
                self.user_type = charge_profile.user_type
                self.is_active = charge_profile.is_active
                self.primary_unit_charge = charge_profile.primary_unit_charge
                self.secondary_unit_charge = charge_profile.secondary_unit_charge
                self.breakpoint = charge_profile.breakpoint
                self.time_formula = charge_profile.time_formula
                self.pricing_profile = getattr(charge_profile, "pricing_profile", ChargeProfilePricingProfile.STANDARD)
                self.profile_type = equipment.profile_type
        
        charge_profile_with_type = ChargeProfileWithType(charge_profile, equipment)
        
        # Ensure input_values are safe for calculations (no lists, dicts, etc. that can't be converted)
        safe_input_values = {}
        for key, value in input_values.items():
            # Only include values that can be used in calculations
            if isinstance(value, (int, float, bool, str)):
                # For strings, ensure they're numeric or boolean
                if isinstance(value, str):
                    value_lower = value.lower().strip()
                    if value_lower in ['true', 'yes']:
                        safe_input_values[key] = True
                    elif value_lower in ['false', 'no']:
                        safe_input_values[key] = False
                    else:
                        # Try to parse as number; keep as string if not numeric (e.g. MULTI_PARAM param_code in B)
                        try:
                            if '.' in value_lower:
                                safe_input_values[key] = float(value_lower)
                            else:
                                safe_input_values[key] = int(value_lower)
                        except (ValueError, TypeError):
                            safe_input_values[key] = value
                else:
                    safe_input_values[key] = value
            # Skip lists, dicts, and other complex types
            else:
                continue

        # Calculate time in minutes
        calculated_time_minutes = TimeCalculationEngine.calculate_time(
            charge_profile_with_type,
            safe_input_values,
            slot_duration_minutes=equipment.slot_duration_minutes
        )
        # Calculate charge
        calculated_charge, charge_breakdown = ChargeCalculationEngine.calculate_charge(
            charge_profile_with_type,
            safe_input_values,
            calculated_time_minutes,
            selected_parameters=None
        )
                
        # Always use server-calculated charge and time for deduction and booking (Total Cost = Total Charge)
        total_charge = calculated_charge
        if UserType.is_external_user(user_type):
            gst_percent = get_external_gst_percent()
            if gst_percent > 0:
                gst_amount = (total_charge * gst_percent / Decimal("100")).quantize(Decimal("0.01"))
                total_charge = (total_charge + gst_amount).quantize(Decimal("0.01"))
                charge_breakdown = list(charge_breakdown) + [
                    {"description": f"GST ({gst_percent}%)", "amount": float(gst_amount)},
                ]
        total_cost = float(total_charge)
        total_time_minutes = calculated_time_minutes
        total_hours = round(calculated_time_minutes / 60, 2)
            
    except ValueError as e:
        err_msg = str(e)
        transaction.set_rollback(True)
        _create_booking_attempt_log(
            request, equipment, BookingAttemptOutcome.FAILED,
            failure_reason=f"Invalid input values for calculation: {str(e)}",
            slots_requested=daily_slots.count(),
            number_of_samples=request.data.get("number_of_samples") or 1,
            additional_info=_get_additional_info_from_request(request, equipment),
        )
        return Response(
            {"error": f"Invalid input values for calculation: {str(e)}. Please check your input_values."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        import traceback
        transaction.set_rollback(True)
        error_details = str(e)
        # Provide more specific error message for Decimal conversion errors
        if 'ConversionSyntax' in str(type(e)) or 'invalid literal' in error_details.lower():
            _create_booking_attempt_log(
                request, equipment, BookingAttemptOutcome.FAILED,
                failure_reason=f"Invalid numeric value in input_values. Error: {error_details}",
                slots_requested=daily_slots.count(),
                number_of_samples=request.data.get("number_of_samples") or 1,
                additional_info=_get_additional_info_from_request(request, equipment),
            )
            return Response(
                {"error": f"Invalid numeric value in input_values. Please ensure all numeric fields contain valid numbers. Error: {error_details}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        _create_booking_attempt_log(
            request, equipment, BookingAttemptOutcome.FAILED,
            failure_reason=f"Error calculating time/charge: {error_details}",
            slots_requested=daily_slots.count(),
            number_of_samples=request.data.get("number_of_samples") or 1,
            additional_info=_get_additional_info_from_request(request, equipment),
        )
        return Response(
            {"error": f"Error calculating time/charge: {error_details}"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Check quota limits before creating booking (skip for admin)
    booking_date = start_time
    if not is_admin and not booking_quota_should_skip(equipment):
        QUOTA_TYPE_WEEKLY = 'WEEKLY'
        QUOTA_TYPE_MONTHLY = 'MONTHLY'
        quota_allowed, quota_error = QuotaChecker.check_user_quota(
            user=booking_user,
            equipment=equipment,
            quota_type=QUOTA_TYPE_WEEKLY,
            additional_time_minutes=total_time_minutes,
            additional_bookings=1,
            additional_charge=total_charge,
            booking_date=booking_date
        )
        if not quota_allowed:
            _create_booking_attempt_log(
                request, equipment, BookingAttemptOutcome.FAILED,
                failure_reason=f"Weekly quota check failed: {quota_error}",
                slots_requested=daily_slots.count(),
                number_of_samples=request.data.get("number_of_samples") or 1,
                duration_minutes=total_time_minutes,
                additional_info=_get_additional_info_from_request(request, equipment),
            )
            return Response(
                {"error": f"Weekly quota check failed: {quota_error}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        quota_allowed, quota_error = QuotaChecker.check_user_quota(
            user=booking_user,
            equipment=equipment,
            quota_type=QUOTA_TYPE_MONTHLY,
            additional_time_minutes=total_time_minutes,
            additional_bookings=1,
            additional_charge=total_charge,
            booking_date=booking_date
        )
        if not quota_allowed:
            _create_booking_attempt_log(
                request, equipment, BookingAttemptOutcome.FAILED,
                failure_reason=f"Monthly quota check failed: {quota_error}",
                slots_requested=daily_slots.count(),
                number_of_samples=request.data.get("number_of_samples") or 1,
                duration_minutes=total_time_minutes,
                additional_info=_get_additional_info_from_request(request, equipment),
            )
            return Response(
                {"error": f"Monthly quota check failed: {quota_error}"},
                status=status.HTTP_400_BAD_REQUEST,
            )

    # Map status string to BookingStatus enum
    status_map = {
        'booked': BookingStatus.BOOKED,
        'hold': BookingStatus.HOLD,
    }
    booking_status_enum = BookingStatus.HOLD if create_as_hold else status_map.get(booking_status.lower(), BookingStatus.BOOKED)
    
    # Resolve wallet or sub-wallet for this booking (sub-wallet when equipment has internal_department)
    from iic_booking.users.repositories.wallet_repository import WalletRepository
    
    wallet = booking_user.get_accessible_wallet()
    if not wallet and booking_user.can_have_wallet():
        wallet, _ = WalletRepository.get_or_create(booking_user)
    
    booking_target, _is_sub = WalletRepository.get_booking_wallet_target(
        booking_user, getattr(equipment, "internal_department", None)
    )
    
    if not booking_target:
        _create_booking_attempt_log(
            request, equipment, BookingAttemptOutcome.FAILED,
            failure_reason="You don't have access to any wallet. Please ensure you have a wallet or are joined to a faculty wallet (for students).",
            slots_requested=daily_slots.count(),
            number_of_samples=request.data.get("number_of_samples") or 1,
            duration_minutes=total_time_minutes,
        )
        return Response(
            {"error": "You don't have access to any wallet. Please ensure you have a wallet or are joined to a faculty wallet (for students)."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    booking_target.refresh_from_db()
    from iic_booking.users.wallet_credit_facility import subwallet_booking_balance_ok

    ok_bal2, bal_err2 = subwallet_booking_balance_ok(booking_target, total_charge, create_as_hold)
    if not ok_bal2:
        _create_booking_attempt_log(
            request, equipment, BookingAttemptOutcome.FAILED,
            failure_reason=bal_err2 or "Insufficient wallet balance",
            slots_requested=daily_slots.count(),
            number_of_samples=request.data.get("number_of_samples") or 1,
            duration_minutes=total_time_minutes,
        )
        return Response(
            {"error": bal_err2 or "Insufficient wallet balance"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    try:
        debit_transaction = None
        with transaction.atomic():
            # Lock slots at DB level to prevent concurrent booking of the same slot (millisecond-level serialization)
            locked_slots_qs = DailySlot.objects.select_for_update().filter(**slots_filter).order_by('start_datetime')
            locked_slots_list = list(locked_slots_qs)
            if not locked_slots_list:
                raise ValueError(SLOTS_ALREADY_OCCUPIED_MESSAGE)
            # Re-verify availability (e.g. slot window, checker) on locked rows
            slot_checker = (
                SlotAvailabilityChecker.is_slot_available_for_external
                if UserType.is_external_user(user_type)
                else SlotAvailabilityChecker.is_slot_available
            )
            try:
                unavailable_locked = [s.id for s in locked_slots_list if not slot_checker(s)]
            except Exception:
                raise ValueError(SLOTS_ALREADY_OCCUPIED_MESSAGE)
            if unavailable_locked:
                raise ValueError(SLOTS_ALREADY_OCCUPIED_MESSAGE)
            slot_ids = [s.id for s in locked_slots_list]
            # Only debit wallet when there is a positive charge (skip for free bookings and for hold bookings)
            if total_charge > 0 and not create_as_hold:
                from iic_booking.users.wallet_credit_facility import subwallet_minimum_balance_after_debit

                transaction_description = f"Booking #{equipment.code} - {equipment.name} ({total_time_minutes} minutes)"
                transaction_description += _student_booking_description_suffix(booking_target, booking_user)
                debit_transaction = booking_target.debit(
                    amount=total_charge,
                    description=transaction_description,
                    related_user=booking_user,
                    minimum_balance_after=subwallet_minimum_balance_after_debit(booking_target),
                )
            
            # Create booking
            booking = Booking.objects.create(
                user=booking_user,
                equipment=equipment,
                charge_profile=charge_profile,
                user_type_snapshot=user_type,
                total_time_minutes=total_time_minutes,
                total_charge=total_charge,
                input_values=input_values,
                selected_parameters=None,
                charge_breakdown=charge_breakdown,
                status=booking_status_enum,
                notes=notes,
                created_by=request.user,
                print_analysis=print_analysis_obj,
                print_analysis_batch=print_analysis_batch_obj,
                **initial_istem_fbr_fields_for_charge_profile(charge_profile),
            )
            from .print_3d_views import link_print_analyses_to_booking

            link_print_analyses_to_booking(
                booking,
                print_analysis_obj=print_analysis_obj,
                print_analysis_batch_obj=print_analysis_batch_obj,
            )
            
            # Update daily slots to link them to the booking and mark as booked (use locked slot IDs)
            DailySlot.objects.filter(id__in=slot_ids).update(booking=booking, status=SlotStatus.BOOKED)
            
            # Refresh slots from database for response
            daily_slots = (
                DailySlot.objects.filter(id__in=slot_ids)
                .order_by("start_datetime")
                .select_related("slot_master", "slot_master__equipment", "booking")
            )
            
            # Create booking event for successful booking creation
            create_booking_event(
                booking=booking,
                event_type=BookingEventType.CREATED,
                created_by=request.user,
                comment=(
                    f"{'Hold created' if create_as_hold else 'Booking created'} for {equipment.name} "
                    f"({total_time_minutes} minutes, ₹{total_charge:.2f})"
                    + (f" on behalf of user {booking_user.id}" if booking_user != request.user else "")
                ),
                new_status=booking.status,
                # HOLD is part of urgent-review queueing; do not send booking-confirmed notifications yet.
                send_notification=not create_as_hold
            )
            if debit_transaction and getattr(booking, "virtual_booking_id", None):
                vid = (booking.virtual_booking_id or "").strip()
                if vid and vid not in (debit_transaction.description or ""):
                    debit_transaction.description = f"{debit_transaction.description.rstrip()} | Ref: {vid}"
                    debit_transaction.save(update_fields=["description"])
            # Wallet deduction email is skipped; booking confirmation email includes final wallet balance.
            
            # Log successful booking attempt (time-range path)
            num_samples = int(request.data.get("number_of_samples") or 1)
            _create_booking_attempt_log(
                request,
                equipment,
                BookingAttemptOutcome.SUCCESS,
                booking_id=booking.booking_id,
                slots_requested=daily_slots.count(),
                number_of_samples=max(1, num_samples),
                duration_minutes=total_time_minutes,
            )
            
    except ValueError as e:
        # Handle wallet-related errors (e.g., insufficient balance) and slot-unavailable (e.g. concurrent booking)
        err_msg = str(e)
        try:
            slot_count = daily_slots.count() if daily_slots else 1
            _create_booking_attempt_log(
                request, equipment, BookingAttemptOutcome.FAILED,
                failure_reason=err_msg,
                slots_requested=slot_count,
                number_of_samples=request.data.get("number_of_samples") or 1,
                duration_minutes=total_time_minutes,
            )
        except TransactionManagementError:
            pass  # Request is inside ATOMIC_REQUESTS and inner atomic failed; skip logging, still return 400
        # For slot-occupancy/all-slots-occupied errors, route through waitlist enrichment
        # so frontend can show WL1/WL2/... when queue has space.
        err_lower = err_msg.lower()
        slot_occupied_error = ("occupied" in err_lower) or ("all slots" in err_lower)
        if slot_occupied_error:
            return Response(
                _enrich_failed_booking_response(
                    equipment,
                    booking_user,
                    err_msg,
                    waitlist_on_failure=waitlist_on_failure,
                    slot_unavailable_failure=True,
                ),
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(
            {"error": err_msg},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        err_str = str(e).lower()
        # Treat slot/lock/concurrent-booking errors as 400 with clear message (avoid 500 for "late" user)
        if any(
            x in err_str
            for x in ("slot", "lock", "available", "booked", "no longer", "already", "occupied")
        ):
            try:
                slot_count = daily_slots.count() if daily_slots else 1
                _create_booking_attempt_log(
                    request, equipment, BookingAttemptOutcome.FAILED,
                    failure_reason=SLOTS_ALREADY_OCCUPIED_MESSAGE,
                    slots_requested=slot_count,
                    number_of_samples=request.data.get("number_of_samples") or 1,
                    duration_minutes=total_time_minutes,
                )
            except TransactionManagementError:
                pass
            return Response(
                _enrich_failed_booking_response(
                    equipment,
                    booking_user,
                    SLOTS_ALREADY_OCCUPIED_MESSAGE,
                    waitlist_on_failure=waitlist_on_failure,
                    slot_unavailable_failure=True,
                ),
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            slot_count = daily_slots.count() if daily_slots else 1
            _create_booking_attempt_log(
                request, equipment, BookingAttemptOutcome.FAILED,
                failure_reason=f"Error creating booking: {str(e)}",
                slots_requested=slot_count,
                number_of_samples=request.data.get("number_of_samples") or 1,
                duration_minutes=total_time_minutes,
            )
        except TransactionManagementError:
            pass
        return Response(
            {"error": f"Error creating booking: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
    
    # Return booking information matching the expected format
    display_booking_id = booking_display_id_for_email(booking)
    return Response(
        {
            "booking_id": display_booking_id,
            "virtual_booking_id": booking.virtual_booking_id,
            "real_booking_id": booking.booking_id,
            "equipment_id": equipment.equipment_id,
            "equipment_code": equipment.code,
            "equipment_name": equipment.name,
            "start_time": start_time_str,
            "end_time": end_time_str,
            "input_values": input_values,
            "status": booking_status.lower(),
            "total_cost": total_cost,
            "total_hours": total_hours,
            "charge_breakdown": charge_breakdown,
            "daily_slots": _serialize_booked_daily_slots_for_booking_response(daily_slots),
            "created_at": booking.created_at.isoformat() if hasattr(booking.created_at, 'isoformat') else str(booking.created_at),
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def approaching_sample_submission_deadlines(request):
    """
    Bookings for the current user whose sample submission deadline is within
    the 12-hour advance window (informational alerts on login / dashboard).
    """
    from .sample_submission_deadline_reminders import list_approaching_sample_submission_for_user

    items = list_approaching_sample_submission_for_user(request.user)
    return Response({"items": items, "count": len(items)}, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_bookings(request):
    """List bookings with optional filtering.
    
    This endpoint requires authentication.
    Returns bookings for the authenticated user by default, or all bookings if user has admin privileges.
    
    Query Parameters:
        user_id: Optional. Filter by user ID (admin only)
        equipment_id: Optional. Filter by equipment ID
        booking_id: Optional. Filter by booking ID (admin/OIC only; use with limit=1 to fetch one booking).
        status: Optional. Filter by booking status (PENDING, BOOKED, COMPLETED, CANCELLED, ABSENT, REFUNDED)
        start_date: Optional. Filter bookings from this date (YYYY-MM-DD)
        end_date: Optional. Filter bookings until this date (YYYY-MM-DD)
        ordering: Optional. Order by field (default: -created_at). Use '-' prefix for descending.
        limit: Optional. Max number of results per page (default: 50).
        offset: Optional. Number of results to skip (default: 0).
        search: Optional. Search in booking ID, equipment name, user name, user email, user mobile.
        user_name: Optional. Filter by booking user's name (partial match).
        supervisor_name: Optional. Filter by Supervisor name (partial match).
        user_type_filter: Optional. "internal" (students/faculty) or "external" – filter by booking user_type_snapshot.
        rating: Optional. Filter by rating (admin/OIC/lab only). Use "unrated", "2_and_below", "3_and_below", "4_and_below", or "5".
        istem_fbr: Optional. Filter I-STEM FBR seal status (admin/OIC only). Use "verified" or "unverified".
    
    Returns:
        Response: List of bookings with count, total_count, limit, offset
    """
    from datetime import date
    from django.utils.dateparse import parse_date
    
    # Start with base queryset
    # Regular users see only their own bookings, operators/managers/admins can see all
    queryset = Booking.objects.all()
    
    # Check if user is operator, manager, or admin
    is_operator_or_manager = check_operator_permission(request.user)
    
    # Filter by user_id if provided
    user_id_filter = request.query_params.get('user_id')
    if user_id_filter:
        if _user_is_accounts_finance_user(request.user):
            return Response(
                {"error": "Accounts In Charge cannot filter bookings by user_id."},
                status=status.HTTP_403_FORBIDDEN,
            )
        # Only operators/managers/admins can filter by any user_id
        if not is_operator_or_manager:
            return Response(
                {"error": "You don't have permission to filter by user_id. Only operators, managers, and admins can filter bookings by user."},
                status=status.HTTP_403_FORBIDDEN,
            )
        try:
            queryset = queryset.filter(user_id=int(user_id_filter))
        except (ValueError, TypeError):
            return Response(
                {"error": "Invalid user_id parameter."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    elif _user_is_accounts_finance_user(request.user):
        queryset = queryset.filter(
            user_type_snapshot__in=list(UserType.get_external_user_codes()),
            status__in=[BookingStatus.BOOKED, BookingStatus.COMPLETED],
        )
    elif not is_operator_or_manager:
        # Regular users see their own bookings.
        # Wallet owners (Faculty/Supervisor) also see bookings made by internal students who use their wallet.
        from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus
        student_ids_using_my_wallet = list(
            WalletJoinRequest.objects.filter(
                faculty=request.user,
                status=WalletJoinRequestStatus.APPROVED,
            ).values_list("student_id", flat=True)
        )
        if student_ids_using_my_wallet:
            queryset = queryset.filter(
                Q(user=request.user) | Q(user_id__in=student_ids_using_my_wallet)
            ).distinct()
        else:
            queryset = queryset.filter(user=request.user)
    else:
        # Operator/manager/admin scope:
        # - manager (OIC): managed equipment only
        # - operator (Lab Incharge): mapped equipment only
        if request.user.user_type == UserType.MANAGER:
            oic_equipment_ids = get_equipment_ids_managed_by_oic(request.user.id)
            if not oic_equipment_ids:
                queryset = queryset.none()
            else:
                queryset = queryset.filter(equipment_id__in=oic_equipment_ids)
        elif request.user.user_type == UserType.OPERATOR:
            operator_equipment_ids = _get_equipment_ids_for_log_access(request.user) or []
            if not operator_equipment_ids:
                queryset = queryset.none()
            else:
                queryset = queryset.filter(equipment_id__in=operator_equipment_ids)

    # Filter by equipment_id if provided
    equipment_id = request.query_params.get('equipment_id')
    if equipment_id:
        try:
            queryset = queryset.filter(equipment_id=int(equipment_id))
        except (ValueError, TypeError):
            return Response(
                {"error": "Invalid equipment_id parameter."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    # Filter by booking_id if provided (any user can filter by booking_id to fetch one booking – for non-admin, queryset is already restricted to own bookings)
    booking_id_filter = request.query_params.get('booking_id')
    if booking_id_filter:
        try:
            queryset = queryset.filter(booking_id=int(booking_id_filter))
        except (ValueError, TypeError):
            pass
    
    # Filter by status if provided
    status_filter = request.query_params.get('status')
    if status_filter:
        # Validate status
        valid_statuses = [choice[0] for choice in BookingStatus.choices]
        if status_filter.upper() not in valid_statuses:
            return Response(
                {"error": f"Invalid status. Must be one of: {', '.join(valid_statuses)}"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        queryset = queryset.filter(status=status_filter.upper())

    # Search across booking ID, equipment name, user name/email/phone (single query param)
    search = request.query_params.get('search', '').strip()
    if search:
        search_q = (
            Q(virtual_booking_id__icontains=search)
            | Q(equipment__name__icontains=search)
            | Q(user__name__icontains=search)
            | Q(user__email__icontains=search)
            | Q(user__phone_number__icontains=search)
        )
        # Match numeric primary key (display id can be CODE-pk when no virtual_booking_id)
        if search.isdigit():
            try:
                search_q |= Q(booking_id=int(search))
            except (ValueError, TypeError):
                pass
        # Match "{equipment_code}-{booking_id}" style display references
        if "-" in search:
            head, _, tail = search.rpartition("-")
            if head and tail.isdigit():
                try:
                    pk = int(tail)
                    search_q |= Q(equipment__code__iexact=head) & Q(booking_id=pk)
                except (ValueError, TypeError):
                    pass
        queryset = queryset.filter(search_q).distinct()

    # Filter by user name (booking user's name) – if specified
    user_name_filter = request.query_params.get('user_name', '').strip()
    if user_name_filter:
        queryset = queryset.filter(user__name__icontains=user_name_filter)

    # Filter by Supervisor name – if specified
    supervisor_name_filter = request.query_params.get('supervisor_name', '').strip()
    if supervisor_name_filter:
        from django.contrib.auth import get_user_model
        from iic_booking.users.models.wallet import WalletJoinRequestStatus
        User = get_user_model()
        # Supervisors (users who have a wallet) whose name/email contains the filter
        supervisor_owner_ids = list(
            User.objects.filter(wallet__isnull=False).filter(
                Q(name__icontains=supervisor_name_filter) | Q(email__icontains=supervisor_name_filter)
            ).values_list('pk', flat=True)
        )
        if supervisor_owner_ids:
            from iic_booking.users.models.wallet import WalletJoinRequest
            queryset = queryset.filter(
                Q(user_id__in=supervisor_owner_ids)
                | Q(
                    user__wallet_join_requests__status=WalletJoinRequestStatus.APPROVED,
                    user__wallet_join_requests__faculty_id__in=supervisor_owner_ids,
                )
            ).distinct()
        else:
            queryset = queryset.none()

    # Filter by user type (internal vs external) – based on booking user_type_snapshot
    user_type_filter = (request.query_params.get('user_type_filter') or '').strip().lower()
    if user_type_filter in ('internal', 'external'):
        if user_type_filter == 'internal':
            internal_codes = list(UserType.get_internal_user_codes())
            queryset = queryset.filter(user_type_snapshot__in=internal_codes)
        else:
            external_codes = list(UserType.get_external_user_codes())
            queryset = queryset.filter(user_type_snapshot__in=external_codes)

    # Filter by rating (admin/OIC/lab only) – unrated, or 2/3/4 stars and below, or 5 stars
    rating_filter = (request.query_params.get('rating') or '').strip().lower()
    if rating_filter and is_operator_or_manager:
        if rating_filter == 'unrated':
            queryset = queryset.filter(rating__isnull=True)
        elif rating_filter == '2_and_below':
            queryset = queryset.filter(rating__isnull=False, rating__lte=2)
        elif rating_filter == '3_and_below':
            queryset = queryset.filter(rating__isnull=False, rating__lte=3)
        elif rating_filter == '4_and_below':
            queryset = queryset.filter(rating__isnull=False, rating__lte=4)
        elif rating_filter == '5':
            queryset = queryset.filter(rating=5)

    # Filter by I-STEM FBR verification (admin/OIC only)
    istem_fbr_filter = (request.query_params.get('istem_fbr') or '').strip().lower()
    if istem_fbr_filter in ('verified', 'unverified') and is_operator_or_manager:
        istem_fbr_applicable = Q(charge_profile__require_istem_fbr=True) | Q(istem_fbr_status__isnull=False)
        if istem_fbr_filter == 'verified':
            queryset = queryset.filter(istem_fbr_status=IstemFbrStatus.EXECUTED)
        else:
            queryset = queryset.filter(istem_fbr_applicable).exclude(
                istem_fbr_status=IstemFbrStatus.EXECUTED
            )

    # Filter by date range if provided
    start_date_param = request.query_params.get('start_date')
    end_date_param = request.query_params.get('end_date')
    
    if start_date_param:
        try:
            start_date = parse_date(start_date_param)
            if start_date:
                # Filter by bookings that have slots starting on or after this date
                queryset = queryset.filter(
                    daily_slots__date__gte=start_date
                ).distinct()
        except (ValueError, TypeError):
            return Response(
                {"error": "Invalid start_date format. Use YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    
    if end_date_param:
        try:
            end_date = parse_date(end_date_param)
            if end_date:
                # Filter by bookings that have slots ending on or before this date
                queryset = queryset.filter(
                    daily_slots__date__lte=end_date
                ).distinct()
        except (ValueError, TypeError):
            return Response(
                {"error": "Invalid end_date format. Use YYYY-MM-DD."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    
    # Ordering
    ordering = request.query_params.get('ordering', '-created_at')
    # Validate ordering field to prevent SQL injection
    valid_ordering_fields = [
        'booking_id', '-booking_id',
        'created_at', '-created_at',
        'updated_at', '-updated_at',
        'status', '-status',
        'total_charge', '-total_charge',
        'total_time_minutes', '-total_time_minutes',
    ]
    if ordering in valid_ordering_fields:
        queryset = queryset.order_by(ordering)
    else:
        queryset = queryset.order_by('-created_at')

    list_view = request.query_params.get('list_view', '').strip().lower() in ('1', 'true', 'yes')
    # Fast flag for UI: show Results action only when at least one result file exists.
    queryset = queryset.annotate(
        has_results=Exists(
            BookingResultFile.objects.filter(booking_id=OuterRef("pk"))
        )
    )
    queryset = queryset.select_related(
        'user', 'user__department', 'created_by', 'equipment', 'equipment__internal_department', 'charge_profile',
    )
    daily_slots_prefetch = Prefetch(
        "daily_slots",
        queryset=DailySlot.objects.select_related(
            "slot_master",
            "slot_master__equipment",
            "booking",
            "booking__equipment",
        ),
    )
    sample_trace_prefetch = Prefetch(
        "sample_trace_events",
        queryset=BookingSampleTrace.objects.select_related("created_by"),
    )
    if list_view:
        queryset = queryset.prefetch_related(
            daily_slots_prefetch,
            'repeat_sample_requests',
            'equipment__equipment_operators__operator',
            'equipment__equipment_managers__manager',
        )
    else:
        queryset = queryset.prefetch_related(
            sample_trace_prefetch,
            'sample_trace_events__reply_attachments',
            'repeat_sample_requests',
            daily_slots_prefetch,
            'equipment__equipment_operators__operator',
            'equipment__equipment_managers__manager',
        )

    # Pagination: limit (default 50) and offset (default 0)
    try:
        limit = int(request.query_params.get('limit', 50))
        limit = max(1, min(limit, 100))
    except (ValueError, TypeError):
        limit = 50
    try:
        offset = int(request.query_params.get('offset', 0))
        offset = max(0, offset)
    except (ValueError, TypeError):
        offset = 0

    total_count = queryset.count()
    queryset = queryset[offset:offset + limit]

    if not list_view:
        for booking in queryset:
            _ensure_istem_fbr_initialized(booking)

    if list_view:
        serializer = BookingListSerializer(queryset, many=True, context={"request": request})
    else:
        serializer = BookingSerializer(queryset, many=True, context={"request": request})

    return Response(
        {
            "bookings": serializer.data,
            "count": len(serializer.data),
            "total_count": total_count,
            "limit": limit,
            "offset": offset,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_stats(request):
    """Return lightweight booking aggregates for reports page."""
    from iic_booking.users.test_accounts import exclude_test_bookings

    queryset = exclude_test_bookings(Booking.objects.all())
    is_operator_or_manager = check_operator_permission(request.user)

    if not is_operator_or_manager:
        from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus

        student_ids_using_my_wallet = list(
            WalletJoinRequest.objects.filter(
                faculty=request.user,
                status=WalletJoinRequestStatus.APPROVED,
            ).values_list("student_id", flat=True)
        )
        if student_ids_using_my_wallet:
            queryset = queryset.filter(
                Q(user=request.user) | Q(user_id__in=student_ids_using_my_wallet)
            ).distinct()
        else:
            queryset = queryset.filter(user=request.user)
    elif request.user.user_type == UserType.MANAGER:
        oic_equipment_ids = get_equipment_ids_managed_by_oic(request.user.id)
        if not oic_equipment_ids:
            queryset = queryset.none()
        else:
            queryset = queryset.filter(equipment_id__in=oic_equipment_ids)
    elif request.user.user_type == UserType.OPERATOR:
        operator_equipment_ids = _get_equipment_ids_for_log_access(request.user) or []
        if not operator_equipment_ids:
            queryset = queryset.none()
        else:
            queryset = queryset.filter(equipment_id__in=operator_equipment_ids)

    aggregates = queryset.aggregate(
        total_bookings=Count("booking_id"),
        total_spent=Sum("total_charge"),
        total_hours=Sum("total_time_minutes"),
    )
    status_counts_qs = queryset.values("status").annotate(count=Count("booking_id"))
    status_counts = {
        row["status"]: row["count"]
        for row in status_counts_qs
    }

    total_hours = (
        float(aggregates["total_hours"] or 0) / 60.0
    )
    total_spent = float(aggregates["total_spent"] or 0)

    return Response(
        {
            "total_bookings": aggregates["total_bookings"] or 0,
            "total_spent": total_spent,
            "total_hours": total_hours,
            "status_counts": status_counts,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def dashboard_summary(request):
    """Return dashboard data in one response: upcoming bookings, equipment stats, pending rating bookings.
    Reduces round-trips for dashboard load. Same permission scope as list_bookings (own user or operator/OIC scope).
    """
    from datetime import date
    from django.utils.dateparse import parse_date

    today = timezone.localdate()
    today_str = today.isoformat()

    from iic_booking.users.test_accounts import exclude_test_bookings

    # Reuse list_bookings scope: own user, Accounts In Charge external list, or operator/manager scope
    is_operator_or_manager = check_operator_permission(request.user)
    base_qs = exclude_test_bookings(Booking.objects.all())
    if _user_is_accounts_finance_user(request.user):
        base_qs = base_qs.filter(
            user_type_snapshot__in=list(UserType.get_external_user_codes()),
            status__in=[BookingStatus.BOOKED, BookingStatus.COMPLETED],
        )
    elif not is_operator_or_manager:
        base_qs = base_qs.filter(user=request.user)
    else:
        if request.user.user_type == UserType.MANAGER:
            oic_equipment_ids = get_equipment_ids_managed_by_oic(request.user.id)
            if not oic_equipment_ids:
                base_qs = base_qs.none()
            else:
                base_qs = base_qs.filter(equipment_id__in=oic_equipment_ids)

    base_qs = base_qs.select_related(
        "user", "user__department", "created_by", "equipment", "equipment__internal_department"
    ).prefetch_related("sample_trace_events", "sample_trace_events__reply_attachments", "repeat_sample_requests")

    # 1) Upcoming: start_date >= today, limit 10, order by created_at (backend doesn't support start_time ordering; frontend sorts)
    upcoming_qs = (
        base_qs.filter(daily_slots__date__gte=today)
        .filter(status__in=[BookingStatus.PENDING, BookingStatus.BOOKED, BookingStatus.DISRUPTION_PENDING])
        .distinct()
        .order_by("created_at")[:10]
    )
    upcoming_serializer = BookingSerializer(upcoming_qs, many=True, context={"request": request})

    # 2) Equipment stats: completed bookings aggregated by equipment, top 5 by count
    from django.db.models import F

    stats_qs = (
        base_qs.filter(status=BookingStatus.COMPLETED)
        .values("equipment_id", "equipment__name", "equipment__code")
        .annotate(
            booking_count=Count("booking_id"),
            total_time_minutes=Sum("total_time_minutes"),
            total_charge=Sum("total_charge"),
        )
        .order_by("-booking_count")[:5]
    )
    equipment_stats = [
        {
            "equipment_id": s["equipment_id"],
            "equipment_code": s["equipment__code"] or "",
            "equipment_name": s["equipment__name"] or "",
            "booking_count": s["booking_count"],
            "total_hours": round((s["total_time_minutes"] or 0) / 60.0, 2),
            "total_spent": float(s["total_charge"] or 0),
        }
        for s in stats_qs
    ]

    # 3) Pending rating: completed, unrated, equipment has user_rating_enabled, limit 20
    pending_rating_qs = (
        base_qs.filter(status=BookingStatus.COMPLETED, rating__isnull=True)
        .filter(equipment__user_rating_enabled=True)
        .distinct()
        .order_by("-completed_at")[:20]
    )
    pending_serializer = BookingSerializer(pending_rating_qs, many=True, context={"request": request})

    return Response(
        {
            "upcoming_bookings": upcoming_serializer.data,
            "equipment_stats": equipment_stats,
            "pending_rating_bookings": pending_serializer.data,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def lab_operator_dashboard(request):
    """Lab Incharge (operator) and OIC (manager): calendar week + filterable metrics (today/week/month/year/custom date range)."""
    from datetime import date as date_cls
    from collections import defaultdict

    if getattr(request.user, "user_type", None) not in (UserType.OPERATOR, UserType.MANAGER):
        return Response(
            {"error": "Only lab operators and officers in charge can access this dashboard."},
            status=status.HTTP_403_FORBIDDEN,
        )

    equipment_ids = _get_equipment_ids_for_log_access(request.user) or []
    today = timezone.localdate()
    week_start_param = (request.query_params.get("week_start") or "").strip()
    if week_start_param:
        try:
            ws = date_cls.fromisoformat(week_start_param)
            week_start = ws - timedelta(days=ws.weekday())
        except ValueError:
            week_start = today - timedelta(days=today.weekday())
    else:
        week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=6)

    from calendar import monthrange

    period_raw = (request.query_params.get("period") or "today").strip().lower()
    if period_raw not in ("today", "week", "month", "year", "custom"):
        period_raw = "today"

    def _filter_date_range():
        if period_raw == "custom":
            df_s = (request.query_params.get("date_from") or "").strip()
            dt_s = (request.query_params.get("date_to") or "").strip()
            try:
                rs = date_cls.fromisoformat(df_s[:10])
                re_end = date_cls.fromisoformat(dt_s[:10])
            except ValueError:
                return today, today
            if rs > re_end:
                rs, re_end = re_end, rs
            return rs, re_end
        if period_raw == "week":
            return week_start, week_end
        if period_raw == "today":
            return today, today
        if period_raw == "month":
            rs = today.replace(day=1)
            ld = monthrange(today.year, today.month)[1]
            return rs, date_cls(today.year, today.month, ld)
        if period_raw == "year":
            return date_cls(today.year, 1, 1), date_cls(today.year, 12, 31)
        return today, today

    filter_range_start, filter_range_end = _filter_date_range()

    def _empty_payload():
        return Response(
            {
                "today": today.isoformat(),
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "filter_period": period_raw,
                "filter_date_start": filter_range_start.isoformat(),
                "filter_date_end": filter_range_end.isoformat(),
                "overall_booking_total": 0,
                "overall_booking_booked_total": 0,
                "overall_booking_completed": 0,
                "overall_booking_rows": [],
                "external_booking_total": 0,
                "external_booking_booked_total": 0,
                "external_booking_completed": 0,
                "external_booking_rows": [],
                "not_utilized_marked_total": 0,
                "not_utilized_available_total": 0,
                "not_utilized_focus_booking_id": None,
                "sample_returned_done_total": 0,
                "sample_available_to_return_total": 0,
                "sample_return_focus_booking_id": None,
                "sample_disposed_done_total": 0,
                "sample_available_to_dispose_total": 0,
                "sample_dispose_focus_booking_id": None,
                "days": [
                    {
                        "date": (week_start + timedelta(days=i)).isoformat(),
                        "is_today": (week_start + timedelta(days=i)) == today,
                        "bookings": [],
                    }
                    for i in range(7)
                ],
                "pending_sample_returned_count": 0,
                "pending_sample_returned_bookings": [],
                "pending_dispose_count": 0,
                "pending_dispose_bookings": [],
                "pending_not_utilized_count": 0,
                "pending_not_utilized_bookings": [],
                "not_utilized_marked_bookings": [],
                "sample_returned_done_bookings": [],
                "sample_disposed_done_bookings": [],
                "equipment_ids": [],
                "equipment_summaries": [],
            },
            status=status.HTTP_200_OK,
        )

    if not equipment_ids:
        return _empty_payload()

    # Full assignment list for UI (dropdown); optional query narrows metrics + week data only.
    full_operator_equipment_ids = list(equipment_ids)
    equipment_id_param = (request.query_params.get("equipment_id") or "").strip()
    if equipment_id_param:
        try:
            eq_single = int(equipment_id_param)
            if eq_single in full_operator_equipment_ids:
                equipment_ids = [eq_single]
        except ValueError:
            pass

    base = Booking.objects.filter(equipment_id__in=equipment_ids)

    excluded_calendar = [
        BookingStatus.CANCELLED,
        BookingStatus.REFUNDED,
        BookingStatus.WAITLISTED,
    ]

    slot_range_q = Q(
        daily_slots__date__gte=filter_range_start,
        daily_slots__date__lte=filter_range_end,
    )

    def _first_booking_id(qs):
        row = (
            qs.annotate(_slot_start=Min("daily_slots__start_datetime"))
            .order_by("_slot_start", "booking_id")
            .first()
        )
        return row.booking_id if row else None

    overall_qs = (
        base.filter(status__in=[BookingStatus.BOOKED, BookingStatus.COMPLETED])
        .filter(slot_range_q)
        .distinct()
        .select_related("user", "equipment")
        .prefetch_related(
            Prefetch("daily_slots", queryset=DailySlot.objects.order_by("start_datetime")),
        )
    )
    overall_booking_total = overall_qs.count()
    overall_booking_booked_total = overall_qs.filter(status=BookingStatus.BOOKED).count()
    overall_booking_completed = overall_qs.filter(status=BookingStatus.COMPLETED).count()

    Trace = BookingSampleTrace
    post_life = Q(status__in=[BookingStatus.BOOKED, BookingStatus.COMPLETED])
    exclude_life = ~Q(
        status__in=[
            BookingStatus.HOLD,
            BookingStatus.REFUNDED,
            BookingStatus.ABSENT,
            BookingStatus.BOOKING_NOT_UTILIZED,
        ]
    )
    no_return = ~Exists(Trace.objects.filter(booking_id=OuterRef("pk"), status=SampleTraceStatus.RETURNED))
    no_arch = ~Exists(Trace.objects.filter(booking_id=OuterRef("pk"), status=SampleTraceStatus.ARCHIVED))
    no_disp = ~Exists(Trace.objects.filter(booking_id=OuterRef("pk"), status=SampleTraceStatus.DISPOSED))
    has_arch = Exists(Trace.objects.filter(booking_id=OuterRef("pk"), status=SampleTraceStatus.ARCHIVED))
    has_not_utilized_trace = Exists(
        Trace.objects.filter(booking_id=OuterRef("pk"), status=SampleTraceStatus.NOT_UTILIZED)
    )

    week_qs = (
        base.exclude(status__in=excluded_calendar)
        .filter(daily_slots__date__gte=week_start, daily_slots__date__lte=week_end)
        .distinct()
        .select_related("user", "equipment")
        .prefetch_related(
            Prefetch("daily_slots", queryset=DailySlot.objects.order_by("date", "start_datetime")),
        )
    )
    week_bookings = list(week_qs[:500])

    # Sample pickup / return queue: **completed** bookings only; exclude any trace marked NOT_UTILIZED
    # (timeline label "Booking Not Utilized" even if booking row stayed COMPLETED).
    sample_return_qs = (
        base.filter(status=BookingStatus.COMPLETED)
        .filter(exclude_life & no_return & no_arch & no_disp & ~has_not_utilized_trace)
        .distinct()
        .select_related("user", "equipment")
        .prefetch_related(
            Prefetch("daily_slots", queryset=DailySlot.objects.order_by("start_datetime")),
        )
    )
    dispose_qs = (
        base.filter(post_life & exclude_life & has_arch & no_return & no_disp)
        .distinct()
        .select_related("user", "equipment")
        .prefetch_related(
            Prefetch("daily_slots", queryset=DailySlot.objects.order_by("start_datetime")),
        )
    )

    now_dt = timezone.now()
    trace_beyond_sample_sent = Exists(
        Trace.objects.filter(booking_id=OuterRef("pk")).exclude(status=SampleTraceStatus.SAMPLE_SENT)
    )
    has_booked_daily_slot = Exists(
        DailySlot.objects.filter(booking_id=OuterRef("pk"), status=SlotStatus.BOOKED)
    )
    not_util_ready_qs = (
        base.filter(status=BookingStatus.BOOKED)
        .annotate(last_end=Max("daily_slots__end_datetime"))
        .filter(last_end__isnull=False, last_end__lte=now_dt)
        .filter(~trace_beyond_sample_sent)
        .filter(has_booked_daily_slot)
        .distinct()
        .select_related("user", "equipment")
        .prefetch_related(
            Prefetch("daily_slots", queryset=DailySlot.objects.order_by("start_datetime")),
        )
        .order_by("-last_end")
    )

    not_util_marked_qs = base.filter(status=BookingStatus.BOOKING_NOT_UTILIZED).filter(slot_range_q).distinct()
    not_util_available_qs = not_util_ready_qs.filter(slot_range_q).distinct()
    not_utilized_marked_total = not_util_marked_qs.count()
    not_utilized_available_total = not_util_available_qs.count()
    not_utilized_focus_booking_id = _first_booking_id(not_util_available_qs) or _first_booking_id(
        not_util_marked_qs
    )

    has_returned_trace = Exists(
        Trace.objects.filter(booking_id=OuterRef("pk"), status=SampleTraceStatus.RETURNED)
    )
    sample_returned_done_qs = (
        base.filter(has_returned_trace).exclude(status__in=excluded_calendar).filter(slot_range_q).distinct()
    )
    sample_available_to_return_qs = sample_return_qs.filter(slot_range_q).distinct()
    sample_returned_done_total = sample_returned_done_qs.count()
    sample_available_to_return_total = sample_available_to_return_qs.count()
    sample_return_focus_booking_id = _first_booking_id(sample_available_to_return_qs) or _first_booking_id(
        sample_returned_done_qs
    )

    has_disposed_trace = Exists(
        Trace.objects.filter(booking_id=OuterRef("pk"), status=SampleTraceStatus.DISPOSED)
    )
    sample_disposed_done_qs = (
        base.filter(has_disposed_trace).exclude(status__in=excluded_calendar).filter(slot_range_q).distinct()
    )
    sample_available_to_dispose_qs = dispose_qs.filter(slot_range_q).distinct()
    sample_disposed_done_total = sample_disposed_done_qs.count()
    sample_available_to_dispose_total = sample_available_to_dispose_qs.count()
    sample_dispose_focus_booking_id = _first_booking_id(sample_available_to_dispose_qs) or _first_booking_id(
        sample_disposed_done_qs
    )

    external_type_codes = list(UserType.get_external_user_codes())
    external_qs = (
        base.filter(user_type_snapshot__in=external_type_codes)
        .filter(status__in=[BookingStatus.BOOKED, BookingStatus.COMPLETED])
        .filter(slot_range_q)
        .distinct()
        .select_related("user", "equipment")
        .prefetch_related(
            Prefetch("daily_slots", queryset=DailySlot.objects.order_by("start_datetime")),
        )
    )
    external_booking_total = external_qs.count()
    external_booking_booked_total = external_qs.filter(status=BookingStatus.BOOKED).count()
    external_booking_completed = external_qs.filter(status=BookingStatus.COMPLETED).count()

    def _serialize_lab_row(b):
        slots = list(b.daily_slots.all())
        first = slots[0] if slots else None
        last = slots[-1] if slots else None
        ref = (b.virtual_booking_id or "").strip() or f"{b.equipment.code}-#{b.booking_id}"
        return {
            "booking_id": b.booking_id,
            "booking_ref": ref,
            "virtual_booking_id": ref,
            "equipment_code": b.equipment.code,
            "equipment_name": b.equipment.name or "",
            "user_name": (b.user.name if b.user else "") or "",
            "status": b.status,
            "status_display": b.get_status_display(),
            "start_time": first.start_datetime.isoformat() if first and first.start_datetime else None,
            "end_time": last.end_datetime.isoformat() if last and last.end_datetime else None,
        }

    by_date = defaultdict(set)
    for b in week_bookings:
        for sl in b.daily_slots.all():
            if sl.date and week_start <= sl.date <= week_end:
                by_date[sl.date].add(b.booking_id)

    booking_by_id = {b.booking_id: b for b in week_bookings}
    days_out = []
    for i in range(7):
        d = week_start + timedelta(days=i)
        ids = by_date.get(d, set())
        day_rows = [_serialize_lab_row(booking_by_id[bid]) for bid in sorted(ids) if bid in booking_by_id]
        days_out.append(
            {
                "date": d.isoformat(),
                "is_today": d == today,
                "bookings": day_rows,
            }
        )

    eq_meta = (
        Equipment.objects.filter(equipment_id__in=full_operator_equipment_ids)
        .values("equipment_id", "code", "name", "status")
        .order_by("code")
    )
    _eq_status_labels = dict(EquipmentStatus.choices)
    equipment_summaries = []
    for r in eq_meta:
        raw_st = (r.get("status") or "").strip()
        equipment_summaries.append(
            {
                "equipment_id": int(r["equipment_id"]),
                "equipment_code": r["code"] or "",
                "equipment_name": r["name"] or "",
                "equipment_status": raw_st,
                "equipment_status_display": _eq_status_labels.get(raw_st, raw_st or "Unknown"),
            }
        )

    current_oic = None
    try:
        first_eq_id = int(equipment_summaries[0]["equipment_id"]) if equipment_summaries else None
        if first_eq_id:
            mgr = (
                EquipmentManager.objects.filter(equipment_id=first_eq_id)
                .select_related("manager")
                .order_by("equipment_manager_id")
                .first()
            )
            m = getattr(mgr, "manager", None) if mgr else None
            if m and getattr(m, "email", None):
                current_oic = {
                    "id": m.id,
                    "name": getattr(m, "name", None) or m.email,
                    "email": m.email,
                }
    except Exception:
        current_oic = None

    overall_for_rows = (
        overall_qs.annotate(_slot_start=Min("daily_slots__start_datetime"))
        .order_by("_slot_start", "booking_id")
    )
    overall_booking_rows = [_serialize_lab_row(b) for b in overall_for_rows[:400]]
    external_for_rows = (
        external_qs.annotate(_slot_start=Min("daily_slots__start_datetime"))
        .order_by("_slot_start", "booking_id")
    )
    external_booking_rows = [_serialize_lab_row(b) for b in external_for_rows[:400]]

    not_util_marked_for_rows = (
        not_util_marked_qs.select_related("user", "equipment")
        .prefetch_related(
            Prefetch("daily_slots", queryset=DailySlot.objects.order_by("start_datetime")),
        )
        .annotate(_slot_start=Min("daily_slots__start_datetime"))
        .order_by("-_slot_start", "booking_id")
    )
    not_utilized_marked_bookings = [_serialize_lab_row(b) for b in not_util_marked_for_rows[:100]]

    sample_returned_done_for_rows = (
        sample_returned_done_qs.select_related("user", "equipment")
        .prefetch_related(
            Prefetch("daily_slots", queryset=DailySlot.objects.order_by("start_datetime")),
        )
        .annotate(_slot_start=Min("daily_slots__start_datetime"))
        .order_by("-_slot_start", "booking_id")
    )
    sample_returned_done_bookings = [_serialize_lab_row(b) for b in sample_returned_done_for_rows[:100]]

    sample_disposed_done_for_rows = (
        sample_disposed_done_qs.select_related("user", "equipment")
        .prefetch_related(
            Prefetch("daily_slots", queryset=DailySlot.objects.order_by("start_datetime")),
        )
        .annotate(_slot_start=Min("daily_slots__start_datetime"))
        .order_by("-_slot_start", "booking_id")
    )
    sample_disposed_done_bookings = [_serialize_lab_row(b) for b in sample_disposed_done_for_rows[:100]]

    return Response(
        {
            "today": today.isoformat(),
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "filter_period": period_raw,
            "filter_date_start": filter_range_start.isoformat(),
            "filter_date_end": filter_range_end.isoformat(),
            "overall_booking_total": overall_booking_total,
            "overall_booking_booked_total": overall_booking_booked_total,
            "overall_booking_completed": overall_booking_completed,
            "overall_booking_rows": overall_booking_rows,
            "external_booking_total": external_booking_total,
            "external_booking_booked_total": external_booking_booked_total,
            "external_booking_completed": external_booking_completed,
            "external_booking_rows": external_booking_rows,
            "not_utilized_marked_total": not_utilized_marked_total,
            "not_utilized_available_total": not_utilized_available_total,
            "not_utilized_focus_booking_id": not_utilized_focus_booking_id,
            "sample_returned_done_total": sample_returned_done_total,
            "sample_available_to_return_total": sample_available_to_return_total,
            "sample_return_focus_booking_id": sample_return_focus_booking_id,
            "sample_disposed_done_total": sample_disposed_done_total,
            "sample_available_to_dispose_total": sample_available_to_dispose_total,
            "sample_dispose_focus_booking_id": sample_dispose_focus_booking_id,
            "days": days_out,
            "pending_sample_returned_count": sample_available_to_return_total,
            "pending_sample_returned_bookings": [
                _serialize_lab_row(b) for b in sample_available_to_return_qs[:100]
            ],
            "pending_dispose_count": sample_available_to_dispose_total,
            "pending_dispose_bookings": [_serialize_lab_row(b) for b in sample_available_to_dispose_qs[:100]],
            "pending_not_utilized_count": not_utilized_available_total,
            "pending_not_utilized_bookings": [_serialize_lab_row(b) for b in not_util_available_qs[:100]],
            "not_utilized_marked_bookings": not_utilized_marked_bookings,
            "sample_returned_done_bookings": sample_returned_done_bookings,
            "sample_disposed_done_bookings": sample_disposed_done_bookings,
            "equipment_ids": sorted(int(x) for x in full_operator_equipment_ids),
            "equipment_summaries": equipment_summaries,
            "current_oic": current_oic,
        },
        status=status.HTTP_200_OK,
    )


# ============================================================================
# Operator leave management (Lab operator dashboard)
# ============================================================================


def _leave_request_email_context(req: OperatorLeaveRequest, *, reviewer=None) -> dict:
    app_name = getattr(settings, "APP_NAME", None) or getattr(settings, "SITE_NAME", None) or "IIC Booking"
    return {
        "app_name": app_name,
        "leave_id": req.id,
        "operator_name": getattr(req.operator, "name", "") or getattr(req.operator, "email", "") or "Operator",
        "start_date": req.start_date.isoformat(),
        "start_session": req.start_session,
        "end_date": req.end_date.isoformat(),
        "end_session": req.end_session,
        "reason": req.reason or "",
        "reviewer_name": (getattr(reviewer, "name", "") or getattr(reviewer, "email", "") or "").strip(),
        "rejection_reason": (req.rejection_reason or "").strip(),
        "leave_management_url": get_frontend_absolute_url("/leave-management"),
        "team_calendar_url": get_frontend_absolute_url("/team-calendar"),
    }


def _leave_request_oic_recipients_for_operator(operator_user: User):
    """
    Resolve OIC recipients for an operator based on equipment assignments.
    We dedupe by user id/email.
    """
    if not operator_user:
        return []

    equipment_ids = list(
        EquipmentOperator.objects.filter(operator=operator_user).values_list("equipment_id", flat=True)
    )
    if not equipment_ids:
        return []

    recipients: dict[int, User] = {}

    for m in EquipmentManager.objects.filter(equipment_id__in=equipment_ids).select_related("manager"):
        u = getattr(m, "manager", None)
        if not u or not getattr(u, "is_active", False) or not getattr(u, "email", ""):
            continue
        recipients[u.id] = u

    now_ts = timezone.now()
    for t in (
        EquipmentTemporaryOIC.objects.filter(equipment_id__in=equipment_ids, resume_at__gt=now_ts)
        .select_related("temporary_oic")
    ):
        u = getattr(t, "temporary_oic", None)
        if not u or not getattr(u, "is_active", False) or not getattr(u, "email", ""):
            continue
        recipients[u.id] = u

    return list(recipients.values())


def _send_leave_submission_emails(req: OperatorLeaveRequest):
    """
    Fire-and-forget emails:
    - To operator: confirmation
    - To OIC(s): approval needed
    """
    try:
        operator = req.operator
        if operator and getattr(operator, "email", ""):
            CommunicationService.send_email(
                operator,
                template="operator_leave_submitted_operator_email",
                template_context=_leave_request_email_context(req),
                metadata={"leave_request_id": req.id, "event": "leave_submitted"},
                created_by=operator,
            )
    except Exception:
        logger.exception("Failed to send leave submission email to operator (leave_id=%s).", req.id)

    try:
        oics = _leave_request_oic_recipients_for_operator(req.operator)
        for oic in oics:
            CommunicationService.send_email(
                oic,
                template="operator_leave_submitted_oic_email",
                template_context={
                    **_leave_request_email_context(req),
                    "oic_name": getattr(oic, "name", "") or getattr(oic, "email", "") or "OIC",
                },
                metadata={"leave_request_id": req.id, "event": "leave_submitted"},
                created_by=req.operator,
            )
    except Exception:
        logger.exception("Failed to send leave submission email(s) to OIC(s) (leave_id=%s).", req.id)


def _send_leave_decision_email(req: OperatorLeaveRequest, *, reviewer: User):
    """Send decision (approved/rejected) to operator."""
    operator = req.operator
    if not operator or not getattr(operator, "email", ""):
        return
    if req.status == OperatorLeaveRequest.Status.APPROVED:
        template_code = "operator_leave_approved_operator_email"
        event = "leave_approved"
    elif req.status == OperatorLeaveRequest.Status.REJECTED:
        template_code = "operator_leave_rejected_operator_email"
        event = "leave_rejected"
    else:
        return

    CommunicationService.send_email(
        operator,
        template=template_code,
        template_context=_leave_request_email_context(req, reviewer=reviewer),
        metadata={"leave_request_id": req.id, "event": event, "reviewer_id": getattr(reviewer, "id", None)},
        created_by=reviewer,
    )


def _parse_oic_leave_intimation_extra_emails() -> list[str]:
    """Comma/semicolon/newline-separated addresses from OIC_LEAVE_INTIMATION_EXTRA_EMAILS."""
    raw = getattr(settings, "OIC_LEAVE_INTIMATION_EXTRA_EMAILS", "") or ""
    parts = re.split(r"[,;\n]+", str(raw))
    seen: set[str] = set()
    out: list[str] = []
    for p in parts:
        e = p.strip()
        if not e:
            continue
        key = e.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _add_department_head_intimation_recipient(oic_user: User, recipients: dict[int, User]) -> None:
    from iic_booking.users.models import Department

    dept_id = getattr(oic_user, "department_id", None)
    if not dept_id:
        return
    dept = Department.objects.filter(pk=dept_id).select_related("head").first()
    if not dept or not dept.head_id or dept.head_id == oic_user.id:
        return
    h = dept.head
    if h and getattr(h, "is_active", False) and getattr(h, "email", ""):
        recipients[h.id] = h


def _merge_extra_env_emails_into_intimation_targets(oic_user: User, recipients: dict[int, User]) -> list[str]:
    """
    Env-listed addresses: resolve to User when possible; otherwise return for raw SMTP send
    (CommunicationService.send_email requires a User for the primary recipient).
    """
    self_email = (getattr(oic_user, "email", None) or "").strip().lower()
    raw_out: list[str] = []
    for addr in _parse_oic_leave_intimation_extra_emails():
        al = addr.strip().lower()
        if not al or al == self_email:
            continue
        u = User.objects.filter(email__iexact=addr.strip()).first()
        if u and getattr(u, "is_active", False) and getattr(u, "email", ""):
            if u.id != oic_user.id:
                recipients[u.id] = u
        else:
            raw_out.append(addr.strip())
    return raw_out


def _collect_oic_leave_intimation_recipients(oic_user: User) -> tuple[dict[int, User], list[str]]:
    """
    FYI recipients when an OIC records own leave: co-OICs / temp OIC contacts on managed equipment,
    optional department head, plus env OIC_LEAVE_INTIMATION_EXTRA_EMAILS (User or raw).
    """
    recipients: dict[int, User] = {}
    if not oic_user:
        return recipients, []

    eq_ids = get_equipment_ids_managed_by_oic(oic_user.id)
    if eq_ids:
        now_ts = timezone.now()
        for m in EquipmentManager.objects.filter(equipment_id__in=eq_ids).select_related("manager"):
            u = getattr(m, "manager", None)
            if u and u.id != oic_user.id and getattr(u, "is_active", False) and getattr(u, "email", ""):
                recipients[u.id] = u
        for t in EquipmentTemporaryOIC.objects.filter(equipment_id__in=eq_ids, resume_at__gt=now_ts).select_related(
            "temporary_oic", "primary_oic"
        ):
            for u in (getattr(t, "primary_oic", None), getattr(t, "temporary_oic", None)):
                if u and u.id != oic_user.id and getattr(u, "is_active", False) and getattr(u, "email", ""):
                    recipients[u.id] = u

    _add_department_head_intimation_recipient(oic_user, recipients)
    raw_extra = _merge_extra_env_emails_into_intimation_targets(oic_user, recipients)
    return recipients, raw_extra


def _send_operator_leave_oic_intimation_raw(email_addr: str, req: OperatorLeaveRequest) -> None:
    """Send OIC leave FYI template to an address that is not a registered user (no CommunicationLog user FK)."""
    from django.core.mail import EmailMultiAlternatives

    from iic_booking.communication.models import CommunicationTemplate
    from iic_booking.communication.service import CommunicationService

    template_code = "operator_leave_submitted_oic_email"
    template_obj = CommunicationService.get_template(
        template=template_code,
        communication_type=CommunicationTemplate.CommunicationType.EMAIL,
    )
    if not template_obj:
        logger.warning("Template %s not found; skipping raw intimation to %s.", template_code, email_addr)
        return
    ctx = {
        **_leave_request_email_context(req),
        "oic_name": email_addr.split("@", 1)[0] if "@" in email_addr else email_addr,
    }
    rendered = CommunicationService.render_template(template_obj, context=ctx)
    subject = rendered.get("subject", "") or "Leave intimation"
    message = rendered.get("message", "") or ""
    html_message = (rendered.get("html_message") or "").strip()
    msg = EmailMultiAlternatives(
        subject=subject,
        body=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[email_addr.strip()],
    )
    if html_message:
        msg.attach_alternative(html_message, "text/html")
    msg.send(fail_silently=False)


def _send_leave_oic_self_leave_intimations(req: OperatorLeaveRequest):
    """Auto-approved OIC leave: confirmation to submitter + FYI to co-OICs, department head, env list."""
    try:
        _send_leave_decision_email(req, reviewer=req.operator)
    except Exception:
        logger.exception("Failed to send OIC self-leave confirmation email (leave_id=%s).", req.id)

    user_map, raw_emails = _collect_oic_leave_intimation_recipients(req.operator)
    for oic in user_map.values():
        try:
            CommunicationService.send_email(
                oic,
                template="operator_leave_submitted_oic_email",
                template_context={
                    **_leave_request_email_context(req),
                    "oic_name": getattr(oic, "name", "") or getattr(oic, "email", "") or "OIC",
                },
                metadata={"leave_request_id": req.id, "event": "leave_oic_intimation"},
                created_by=req.operator,
            )
        except Exception:
            logger.exception(
                "Failed to send OIC self-leave intimation to user %s (leave_id=%s).", oic.id, req.id
            )
    for addr in raw_emails:
        try:
            _send_operator_leave_oic_intimation_raw(addr, req)
        except Exception:
            logger.exception(
                "Failed to send OIC self-leave intimation to raw address %s (leave_id=%s).", addr, req.id
            )


def _operator_leave_span_valid(req: OperatorLeaveRequest) -> bool:
    if req.start_date > req.end_date:
        return False
    if req.start_date == req.end_date:
        if (
            req.start_session == OperatorLeaveRequest.Session.AN
            and req.end_session == OperatorLeaveRequest.Session.FN
        ):
            return False
    return True


def _apply_resume_duty_leave_adjustment(
    req: OperatorLeaveRequest, acting_user: User
) -> tuple[dict | None, tuple[int, dict] | None]:
    """
    Shorten or cancel approved leave based on local time-of-day (10:00 cutoff).
    Before 10:00: today is excluded from leave (effective end = yesterday, full day AN).
    From 10:00 onward: today counts as forenoon (FN) only; future dates are dropped.
    Special case: leave that starts today afternoon only keeps end_session=AN when truncating to today.
    """
    from datetime import time as time_cls, timedelta

    if req.status != OperatorLeaveRequest.Status.APPROVED:
        return None, (status.HTTP_400_BAD_REQUEST, {"error": "Only approved leave can be adjusted on resume."})

    local_now = timezone.localtime()
    today = local_now.date()
    if today < req.start_date or today > req.end_date:
        return None, (
            status.HTTP_400_BAD_REQUEST,
            {"error": "Resume duty is only available on a day within your approved leave period."},
        )

    ten_am = time_cls(10, 0)
    before_10 = local_now.time() < ten_am
    now_ts = timezone.now()

    if before_10:
        new_end = today - timedelta(days=1)
        if new_end < req.start_date:
            req.status = OperatorLeaveRequest.Status.CANCELLED
            req.save(update_fields=["status", "updated_at"])
            n = EquipmentOperatorCoverage.objects.filter(source_leave_request=req, ended_early_at__isnull=True).update(
                ended_early_at=now_ts, ended_early_by=acting_user
            )
            return {"message": "Leave vacated.", "status": req.status, "ended_coverages": n}, None
        req.end_date = new_end
        req.end_session = OperatorLeaveRequest.Session.AN
    else:
        req.end_date = today
        if req.start_date == today and req.start_session == OperatorLeaveRequest.Session.AN:
            req.end_session = OperatorLeaveRequest.Session.AN
        else:
            req.end_session = OperatorLeaveRequest.Session.FN

    if not _operator_leave_span_valid(req):
        return None, (status.HTTP_400_BAD_REQUEST, {"error": "Leave period could not be adjusted for resume."})

    req.save(update_fields=["end_date", "end_session", "updated_at"])
    _starts_at, ends_at = _leave_session_window_to_datetimes(req)
    n = EquipmentOperatorCoverage.objects.filter(source_leave_request=req, ended_early_at__isnull=True).update(
        ends_at=ends_at
    )
    return {
        "message": "Leave shortened; duty resumed.",
        "end_date": req.end_date.isoformat(),
        "end_session": req.end_session,
        "updated_coverages": n,
    }, None


def _leave_weight_in_range(req: OperatorLeaveRequest, range_start, range_end) -> float:
    """Return leave days in [range_start, range_end], accounting for FN/AN half-days."""
    from datetime import date as date_cls

    if not isinstance(range_start, date_cls) or not isinstance(range_end, date_cls):
        return 0.0
    s = max(req.start_date, range_start)
    e = min(req.end_date, range_end)
    if s > e:
        return 0.0

    days = float((e - s).days + 1)
    start_sess = req.start_session if s == req.start_date else OperatorLeaveRequest.Session.FN
    end_sess = req.end_session if e == req.end_date else OperatorLeaveRequest.Session.AN
    if start_sess == OperatorLeaveRequest.Session.AN:
        days -= 0.5
    if end_sess == OperatorLeaveRequest.Session.FN:
        days -= 0.5
    return max(days, 0.0)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def operator_leave_summary(request):
    """Lab operator: approved leave days in a given year (half day counts as 0.5)."""
    if getattr(request.user, "user_type", None) not in (UserType.OPERATOR, UserType.MANAGER):
        return Response(
            {"error": "Only lab operators and OIC users can access leave summary."},
            status=status.HTTP_403_FORBIDDEN,
        )

    year_raw = (request.query_params.get("year") or "").strip()
    try:
        year = int(year_raw) if year_raw else timezone.localdate().year
    except ValueError:
        year = timezone.localdate().year

    from datetime import date as date_cls

    year_start = date_cls(year, 1, 1)
    year_end = date_cls(year, 12, 31)

    qs = OperatorLeaveRequest.objects.filter(
        operator=request.user,
        status=OperatorLeaveRequest.Status.APPROVED,
        start_date__lte=year_end,
        end_date__gte=year_start,
    ).select_related("equipment")

    total = 0.0
    for req in qs:
        total += _leave_weight_in_range(req, year_start, year_end)

    return Response({"approved_days_this_year": total}, status=status.HTTP_200_OK)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def operator_leave_requests(request):
    """
    Lab operator: list leave requests (year filter) and apply for leave (multipart supported).
    """
    if getattr(request.user, "user_type", None) not in (UserType.OPERATOR, UserType.MANAGER):
        return Response(
            {"error": "Only lab operators and OIC users can access leave requests."},
            status=status.HTTP_403_FORBIDDEN,
        )

    if request.method == "GET":
        year_raw = (request.query_params.get("year") or "").strip()
        try:
            year = int(year_raw) if year_raw else timezone.localdate().year
        except ValueError:
            year = timezone.localdate().year
        from datetime import date as date_cls

        year_start = date_cls(year, 1, 1)
        year_end = date_cls(year, 12, 31)

        qs = (
            OperatorLeaveRequest.objects.filter(
                operator=request.user,
                start_date__lte=year_end,
                end_date__gte=year_start,
            )
            .select_related("equipment")
            .order_by("-created_at")
        )

        out = []
        for r in qs:
            eq = r.equipment
            out.append(
                {
                    "id": r.id,
                    "equipment_id": eq.equipment_id if eq else None,
                    "equipment_code": getattr(eq, "code", None),
                    "equipment_name": getattr(eq, "name", None),
                    "start_date": r.start_date.isoformat(),
                    "start_session": r.start_session,
                    "end_date": r.end_date.isoformat(),
                    "end_session": r.end_session,
                    "reason": r.reason,
                    "status": r.status,
                    "rejection_reason": r.rejection_reason,
                    "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
                }
            )
        return Response({"leaves": out}, status=status.HTTP_200_OK)

    # POST: create request (global by default; equipment_id optional)
    data = request.data or {}
    equipment = None
    equipment_id_raw = data.get("equipment_id")
    if equipment_id_raw not in (None, "", "__none__"):
        try:
            equipment_id = int(equipment_id_raw)
        except (TypeError, ValueError):
            return Response({"error": "Invalid equipment_id."}, status=status.HTTP_400_BAD_REQUEST)
        equipment = Equipment.objects.filter(equipment_id=equipment_id).first()
        if not equipment:
            return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
        # Operator must be assigned to this equipment (primary or secondary).
        if not EquipmentOperator.objects.filter(equipment=equipment, operator=request.user).exists():
            return Response(
                {"error": "You are not assigned as operator for this instrument."},
                status=status.HTTP_403_FORBIDDEN,
            )

    start_date = parse_date_iso(str(data.get("start_date") or "").strip()[:10])
    end_date = parse_date_iso(str(data.get("end_date") or "").strip()[:10])
    if not start_date or not end_date:
        return Response({"error": "Invalid start_date or end_date. Use YYYY-MM-DD."}, status=status.HTTP_400_BAD_REQUEST)
    if start_date > end_date:
        start_date, end_date = end_date, start_date

    start_session = (str(data.get("start_session") or OperatorLeaveRequest.Session.FN).strip().upper() or "FN")
    end_session = (str(data.get("end_session") or OperatorLeaveRequest.Session.AN).strip().upper() or "AN")
    if start_session not in (OperatorLeaveRequest.Session.FN, OperatorLeaveRequest.Session.AN):
        start_session = OperatorLeaveRequest.Session.FN
    if end_session not in (OperatorLeaveRequest.Session.FN, OperatorLeaveRequest.Session.AN):
        end_session = OperatorLeaveRequest.Session.AN

    if start_date == end_date and start_session == OperatorLeaveRequest.Session.AN and end_session == OperatorLeaveRequest.Session.FN:
        return Response({"error": "Invalid session range for the same day."}, status=status.HTTP_400_BAD_REQUEST)

    reason = (str(data.get("reason") or "").strip())
    if len(reason) < 3:
        return Response({"error": "Reason is required."}, status=status.HTTP_400_BAD_REQUEST)

    # Prevent overlaps with pending/approved (global by default; if equipment provided, still global overlap)
    overlap = OperatorLeaveRequest.objects.filter(
        operator=request.user,
        status__in=[OperatorLeaveRequest.Status.PENDING, OperatorLeaveRequest.Status.APPROVED],
        start_date__lte=end_date,
        end_date__gte=start_date,
    ).exists()
    if overlap:
        return Response({"error": "You already have a leave request in this period."}, status=status.HTTP_400_BAD_REQUEST)

    attachment = request.FILES.get("attachment") or request.FILES.get("file")
    is_oic = getattr(request.user, "user_type", None) == UserType.MANAGER
    now = timezone.now()
    if is_oic:
        req = OperatorLeaveRequest.objects.create(
            equipment=equipment,
            operator=request.user,
            start_date=start_date,
            start_session=start_session,
            end_date=end_date,
            end_session=end_session,
            reason=reason,
            attachment=attachment if attachment else None,
            status=OperatorLeaveRequest.Status.APPROVED,
            reviewed_by=request.user,
            reviewed_at=now,
        )
        threading.Thread(target=_send_leave_oic_self_leave_intimations, args=(req,), daemon=True).start()
    else:
        req = OperatorLeaveRequest.objects.create(
            equipment=equipment,
            operator=request.user,
            start_date=start_date,
            start_session=start_session,
            end_date=end_date,
            end_session=end_session,
            reason=reason,
            attachment=attachment if attachment else None,
            status=OperatorLeaveRequest.Status.PENDING,
        )
        threading.Thread(target=_send_leave_submission_emails, args=(req,), daemon=True).start()
    return Response({"id": req.id, "status": req.status}, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def operator_leave_request_resume(request, leave_id: int):
    """Lab operator/OIC: shorten leave and coverage per 10:00 local rule, or vacate leave if before 10:00 on first day."""
    if getattr(request.user, "user_type", None) not in (UserType.OPERATOR, UserType.MANAGER):
        return Response({"error": "Only lab operators and OIC users can resume duty."}, status=status.HTTP_403_FORBIDDEN)

    req = OperatorLeaveRequest.objects.select_related("operator").filter(id=leave_id).first()
    if not req:
        return Response({"error": "Leave request not found."}, status=status.HTTP_404_NOT_FOUND)
    if req.operator_id != request.user.id:
        return Response({"error": "You can only resume duty for your own leave request."}, status=status.HTTP_403_FORBIDDEN)

    payload, err = _apply_resume_duty_leave_adjustment(req, request.user)
    if err:
        return Response(err[1], status=err[0])
    return Response(payload, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def oic_leave_requests_pending(request):
    """OIC/Admin: list pending leave requests for department members."""
    utype = getattr(request.user, "user_type", None)
    if utype not in (UserType.MANAGER, UserType.ADMIN):
        return Response({"error": "Only OIC and Admin can access pending leave requests."}, status=status.HTTP_403_FORBIDDEN)

    dept_id = getattr(getattr(request.user, "department", None), "id", None) or getattr(request.user, "department_id", None)
    if utype == UserType.ADMIN:
        dept_raw = (request.query_params.get("department_id") or "").strip()
        if dept_raw:
            try:
                dept_id = int(dept_raw)
            except ValueError:
                pass
    if not dept_id:
        return Response({"leaves": []}, status=status.HTTP_200_OK)

    qs = (
        OperatorLeaveRequest.objects.filter(
            status=OperatorLeaveRequest.Status.PENDING,
            operator__department_id=dept_id,
        )
        .select_related("operator", "equipment")
        .order_by("start_date", "start_session", "created_at")
    )

    out = []
    for r in qs:
        out.append(
            {
                "id": r.id,
                "operator": {
                    "id": r.operator_id,
                    "name": getattr(r.operator, "name", None),
                    "email": getattr(r.operator, "email", None),
                },
                "start_date": r.start_date.isoformat(),
                "start_session": r.start_session,
                "end_date": r.end_date.isoformat(),
                "end_session": r.end_session,
                "reason": r.reason,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
        )
    return Response({"leaves": out}, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def oic_leave_requests_approved(request):
    """OIC/Admin: list approved leave requests for department members."""
    utype = getattr(request.user, "user_type", None)
    if utype not in (UserType.MANAGER, UserType.ADMIN):
        return Response({"error": "Only OIC and Admin can access approved leave requests."}, status=status.HTTP_403_FORBIDDEN)

    dept_id = getattr(getattr(request.user, "department", None), "id", None) or getattr(request.user, "department_id", None)
    if utype == UserType.ADMIN:
        dept_raw = (request.query_params.get("department_id") or "").strip()
        if dept_raw:
            try:
                dept_id = int(dept_raw)
            except ValueError:
                pass
    if not dept_id:
        return Response({"leaves": []}, status=status.HTTP_200_OK)

    qs = (
        OperatorLeaveRequest.objects.filter(
            status=OperatorLeaveRequest.Status.APPROVED,
            operator__department_id=dept_id,
        )
        .select_related("operator", "equipment")
        .order_by("-reviewed_at", "-start_date", "-created_at")
    )

    out = []
    for r in qs:
        out.append(
            {
                "id": r.id,
                "operator": {
                    "id": r.operator_id,
                    "name": getattr(r.operator, "name", None),
                    "email": getattr(r.operator, "email", None),
                },
                "start_date": r.start_date.isoformat(),
                "start_session": r.start_session,
                "end_date": r.end_date.isoformat(),
                "end_session": r.end_session,
                "reason": r.reason,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "reviewed_at": r.reviewed_at.isoformat() if r.reviewed_at else None,
            }
        )
    return Response({"leaves": out}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def oic_leave_request_approve(request, leave_id: int):
    """OIC/Admin: approve a pending leave request and notify operator."""
    utype = getattr(request.user, "user_type", None)
    if utype not in (UserType.MANAGER, UserType.ADMIN):
        return Response({"error": "Only OIC and Admin can approve leave requests."}, status=status.HTTP_403_FORBIDDEN)

    req = OperatorLeaveRequest.objects.select_related("operator").filter(id=leave_id).first()
    if not req:
        return Response({"error": "Leave request not found."}, status=status.HTTP_404_NOT_FOUND)

    if req.status != OperatorLeaveRequest.Status.PENDING:
        return Response({"error": f"Cannot approve a request in status '{req.status}'."}, status=status.HTTP_400_BAD_REQUEST)

    if utype == UserType.MANAGER:
        my_dept = getattr(getattr(request.user, "department", None), "id", None) or getattr(request.user, "department_id", None)
        if my_dept and getattr(req.operator, "department_id", None) != my_dept:
            return Response({"error": "You can only approve leave requests within your department."}, status=status.HTTP_403_FORBIDDEN)

    req.status = OperatorLeaveRequest.Status.APPROVED
    req.reviewed_by = request.user
    req.reviewed_at = timezone.now()
    req.rejection_reason = ""
    req.save(update_fields=["status", "reviewed_by", "reviewed_at", "rejection_reason"])

    threading.Thread(target=_send_leave_decision_email, kwargs={"req": req, "reviewer": request.user}, daemon=True).start()
    return Response({"id": req.id, "status": req.status}, status=status.HTTP_200_OK)


def _leave_session_window_to_datetimes(req: OperatorLeaveRequest):
    """
    Convert OperatorLeaveRequest date+session range to timezone-aware datetime window.

    FN => start-of-day to 13:00 local. AN => 13:00 local to end-of-day (23:59:59).
    """
    from datetime import datetime as dt_cls, time as time_cls

    tz = timezone.get_current_timezone()
    start_time = time_cls(0, 0, 0) if req.start_session == OperatorLeaveRequest.Session.FN else time_cls(13, 0, 0)
    end_time = time_cls(13, 0, 0) if req.end_session == OperatorLeaveRequest.Session.FN else time_cls(23, 59, 59)
    starts_at = timezone.make_aware(dt_cls.combine(req.start_date, start_time), tz)
    ends_at = timezone.make_aware(dt_cls.combine(req.end_date, end_time), tz)
    if ends_at < starts_at:
        starts_at, ends_at = ends_at, starts_at
    return starts_at, ends_at


def _acting_operator_has_overlapping_leave(operator_user: User, starts_at, ends_at) -> bool:
    """True if operator has any PENDING/APPROVED leave overlapping the datetime window."""
    if not operator_user:
        return False
    from datetime import date as date_cls

    start_date = timezone.localdate(starts_at) if starts_at else None
    end_date = timezone.localdate(ends_at) if ends_at else None
    if not start_date or not end_date:
        return False
    if start_date > end_date:
        start_date, end_date = end_date, start_date
    return OperatorLeaveRequest.objects.filter(
        operator=operator_user,
        status__in=[OperatorLeaveRequest.Status.PENDING, OperatorLeaveRequest.Status.APPROVED],
        start_date__lte=end_date,
        end_date__gte=start_date,
    ).exists()


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def oic_leave_request_coverage_options(request, leave_id: int):
    """OIC/Admin: fetch per-equipment coverage options for an operator leave request."""
    utype = getattr(request.user, "user_type", None)
    if utype not in (UserType.MANAGER, UserType.ADMIN):
        return Response({"error": "Only OIC and Admin can access coverage options."}, status=status.HTTP_403_FORBIDDEN)

    req = OperatorLeaveRequest.objects.select_related("operator").filter(id=leave_id).first()
    if not req:
        return Response({"error": "Leave request not found."}, status=status.HTTP_404_NOT_FOUND)

    # Dept scope check for manager
    if utype == UserType.MANAGER:
        my_dept = getattr(getattr(request.user, "department", None), "id", None) or getattr(request.user, "department_id", None)
        if my_dept and getattr(req.operator, "department_id", None) != my_dept:
            return Response({"error": "You can only access requests within your department."}, status=status.HTTP_403_FORBIDDEN)

    # Equipments where this operator is PRIMARY.
    eq_rows = (
        EquipmentOperator.objects.filter(operator=req.operator, role=EquipmentOperator.Role.PRIMARY)
        .select_related("equipment")
        .order_by("equipment__code")
    )
    equipments = [
        {"equipment_id": r.equipment_id, "equipment_code": r.equipment.code, "equipment_name": r.equipment.name}
        for r in eq_rows
    ]

    # Eligible operators: same department as requester/operator; for Admin default to operator's department.
    dept_id = getattr(req.operator, "department_id", None)
    if utype == UserType.ADMIN:
        dept_raw = (request.query_params.get("department_id") or "").strip()
        if dept_raw:
            try:
                dept_id = int(dept_raw)
            except ValueError:
                pass
    eligible_ops = (
        User.objects.filter(user_type=UserType.OPERATOR, is_active=True, department_id=dept_id)
        .exclude(id=req.operator_id)
        .values("id", "name", "email")
        .order_by("name", "email")
    )

    return Response(
        {
            "leave": {
                "id": req.id,
                "operator_id": req.operator_id,
                "operator_name": getattr(req.operator, "name", None),
                "operator_email": getattr(req.operator, "email", None),
                "start_date": req.start_date.isoformat(),
                "start_session": req.start_session,
                "end_date": req.end_date.isoformat(),
                "end_session": req.end_session,
            },
            "equipments": equipments,
            "eligible_operators": list(eligible_ops),
            "modes": [m for m, _ in EquipmentOperatorCoverage.Mode.choices],
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def oic_leave_request_approve_with_coverage(request, leave_id: int):
    """OIC/Admin: approve operator leave and create per-equipment coverage rows."""
    utype = getattr(request.user, "user_type", None)
    if utype not in (UserType.MANAGER, UserType.ADMIN):
        return Response({"error": "Only OIC and Admin can approve leave requests."}, status=status.HTTP_403_FORBIDDEN)

    req = OperatorLeaveRequest.objects.select_related("operator").filter(id=leave_id).first()
    if not req:
        return Response({"error": "Leave request not found."}, status=status.HTTP_404_NOT_FOUND)

    if req.status != OperatorLeaveRequest.Status.PENDING:
        return Response({"error": f"Cannot approve a request in status '{req.status}'."}, status=status.HTTP_400_BAD_REQUEST)

    if utype == UserType.MANAGER:
        my_dept = getattr(getattr(request.user, "department", None), "id", None) or getattr(request.user, "department_id", None)
        if my_dept and getattr(req.operator, "department_id", None) != my_dept:
            return Response({"error": "You can only approve leave requests within your department."}, status=status.HTTP_403_FORBIDDEN)

    data = request.data or {}
    coverages = data.get("coverages") or []
    if not isinstance(coverages, list) or not coverages:
        return Response({"error": "coverages is required."}, status=status.HTTP_400_BAD_REQUEST)

    starts_at, ends_at = _leave_session_window_to_datetimes(req)

    primary_eq_ids = set(
        EquipmentOperator.objects.filter(operator=req.operator, role=EquipmentOperator.Role.PRIMARY).values_list("equipment_id", flat=True)
    )
    if not primary_eq_ids:
        return Response({"error": "No primary equipments found for this operator."}, status=status.HTTP_400_BAD_REQUEST)

    created_rows = []
    with transaction.atomic():
        # Approve request
        req.status = OperatorLeaveRequest.Status.APPROVED
        req.reviewed_by = request.user
        req.reviewed_at = timezone.now()
        req.rejection_reason = ""
        req.save(update_fields=["status", "reviewed_by", "reviewed_at", "rejection_reason"])

        for c in coverages:
            if not isinstance(c, dict):
                continue
            try:
                equipment_id = int(c.get("equipment_id"))
            except Exception:
                return Response({"error": "Invalid equipment_id in coverages."}, status=status.HTTP_400_BAD_REQUEST)
            if equipment_id not in primary_eq_ids:
                return Response({"error": f"Invalid equipment_id {equipment_id} for this operator."}, status=status.HTTP_400_BAD_REQUEST)

            mode = str(c.get("mode") or "").strip()
            if mode not in dict(EquipmentOperatorCoverage.Mode.choices):
                return Response({"error": f"Invalid coverage mode '{mode}'."}, status=status.HTTP_400_BAD_REQUEST)

            acting_operator = None
            if mode == EquipmentOperatorCoverage.Mode.SECONDARY_OPERATOR:
                acting_id = c.get("acting_operator_id")
                try:
                    acting_id = int(acting_id)
                except Exception:
                    return Response({"error": "acting_operator_id is required for SECONDARY_OPERATOR mode."}, status=status.HTTP_400_BAD_REQUEST)
                acting_operator = User.objects.filter(id=acting_id, user_type=UserType.OPERATOR, is_active=True).first()
                if not acting_operator:
                    return Response({"error": "Invalid acting_operator_id."}, status=status.HTTP_400_BAD_REQUEST)
                if getattr(acting_operator, "department_id", None) != getattr(req.operator, "department_id", None):
                    return Response({"error": "Acting operator must be in the same department."}, status=status.HTTP_400_BAD_REQUEST)
                if _acting_operator_has_overlapping_leave(acting_operator, starts_at, ends_at):
                    return Response(
                        {"error": f"Selected secondary operator ({acting_operator.email}) is on leave during this period. Please choose another operator."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            row = EquipmentOperatorCoverage.objects.create(
                equipment_id=equipment_id,
                primary_operator=req.operator,
                acting_operator=acting_operator,
                mode=mode,
                starts_at=starts_at,
                ends_at=ends_at,
                source_leave_request=req,
                created_by=request.user,
            )
            created_rows.append(row.id)

    threading.Thread(target=_send_leave_decision_email, kwargs={"req": req, "reviewer": request.user}, daemon=True).start()
    return Response({"id": req.id, "status": req.status, "coverage_ids": created_rows}, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def oic_leave_request_coverages(request, leave_id: int):
    """OIC/Admin: list existing coverages for a leave request (for editing)."""
    utype = getattr(request.user, "user_type", None)
    if utype not in (UserType.MANAGER, UserType.ADMIN):
        return Response({"error": "Only OIC and Admin can access coverages."}, status=status.HTTP_403_FORBIDDEN)

    req = OperatorLeaveRequest.objects.select_related("operator").filter(id=leave_id).first()
    if not req:
        return Response({"error": "Leave request not found."}, status=status.HTTP_404_NOT_FOUND)

    if utype == UserType.MANAGER:
        my_dept = getattr(getattr(request.user, "department", None), "id", None) or getattr(request.user, "department_id", None)
        if my_dept and getattr(req.operator, "department_id", None) != my_dept:
            return Response({"error": "You can only act within your department."}, status=status.HTTP_403_FORBIDDEN)

    qs = EquipmentOperatorCoverage.objects.filter(source_leave_request=req).select_related("acting_operator", "equipment")
    out = []
    for c in qs:
        out.append(
            {
                "id": c.id,
                "equipment_id": c.equipment_id,
                "mode": c.mode,
                "acting_operator_id": c.acting_operator_id,
                "acting_operator_email": getattr(getattr(c, "acting_operator", None), "email", None),
                "ended_early_at": c.ended_early_at.isoformat() if c.ended_early_at else None,
            }
        )
    return Response({"coverages": out}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def oic_leave_request_set_coverages(request, leave_id: int):
    """
    OIC/Admin: set/replace coverages for an already-approved leave request.
    Used for approved list 'map coverage' action.
    """
    utype = getattr(request.user, "user_type", None)
    if utype not in (UserType.MANAGER, UserType.ADMIN):
        return Response({"error": "Only OIC and Admin can set coverages."}, status=status.HTTP_403_FORBIDDEN)

    req = OperatorLeaveRequest.objects.select_related("operator").filter(id=leave_id).first()
    if not req:
        return Response({"error": "Leave request not found."}, status=status.HTTP_404_NOT_FOUND)
    if req.status != OperatorLeaveRequest.Status.APPROVED:
        return Response({"error": "Coverages can only be set for approved leave requests."}, status=status.HTTP_400_BAD_REQUEST)

    if utype == UserType.MANAGER:
        my_dept = getattr(getattr(request.user, "department", None), "id", None) or getattr(request.user, "department_id", None)
        if my_dept and getattr(req.operator, "department_id", None) != my_dept:
            return Response({"error": "You can only act within your department."}, status=status.HTTP_403_FORBIDDEN)

    data = request.data or {}
    coverages = data.get("coverages") or []
    if not isinstance(coverages, list) or not coverages:
        return Response({"error": "coverages is required."}, status=status.HTTP_400_BAD_REQUEST)

    starts_at, ends_at = _leave_session_window_to_datetimes(req)
    primary_eq_ids = set(
        EquipmentOperator.objects.filter(operator=req.operator, role=EquipmentOperator.Role.PRIMARY).values_list("equipment_id", flat=True)
    )
    if not primary_eq_ids:
        return Response({"error": "No primary equipments found for this operator."}, status=status.HTTP_400_BAD_REQUEST)

    created_rows = []
    with transaction.atomic():
        # End existing (active or historical) coverages for this leave and recreate fresh ones.
        EquipmentOperatorCoverage.objects.filter(source_leave_request=req, ended_early_at__isnull=True).update(
            ended_early_at=timezone.now(), ended_early_by=request.user
        )
        for c in coverages:
            if not isinstance(c, dict):
                continue
            try:
                equipment_id = int(c.get("equipment_id"))
            except Exception:
                return Response({"error": "Invalid equipment_id in coverages."}, status=status.HTTP_400_BAD_REQUEST)
            if equipment_id not in primary_eq_ids:
                return Response({"error": f"Invalid equipment_id {equipment_id} for this operator."}, status=status.HTTP_400_BAD_REQUEST)

            mode = str(c.get("mode") or "").strip()
            if mode not in dict(EquipmentOperatorCoverage.Mode.choices):
                return Response({"error": f"Invalid coverage mode '{mode}'."}, status=status.HTTP_400_BAD_REQUEST)

            acting_operator = None
            if mode == EquipmentOperatorCoverage.Mode.SECONDARY_OPERATOR:
                acting_id = c.get("acting_operator_id")
                try:
                    acting_id = int(acting_id)
                except Exception:
                    return Response({"error": "acting_operator_id is required for SECONDARY_OPERATOR mode."}, status=status.HTTP_400_BAD_REQUEST)
                acting_operator = User.objects.filter(id=acting_id, user_type=UserType.OPERATOR, is_active=True).first()
                if not acting_operator:
                    return Response({"error": "Invalid acting_operator_id."}, status=status.HTTP_400_BAD_REQUEST)
                if getattr(acting_operator, "department_id", None) != getattr(req.operator, "department_id", None):
                    return Response({"error": "Acting operator must be in the same department."}, status=status.HTTP_400_BAD_REQUEST)
                if _acting_operator_has_overlapping_leave(acting_operator, starts_at, ends_at):
                    return Response(
                        {"error": f"Selected secondary operator ({acting_operator.email}) is on leave during this period. Please choose another operator."},
                        status=status.HTTP_400_BAD_REQUEST,
                    )

            row = EquipmentOperatorCoverage.objects.create(
                equipment_id=equipment_id,
                primary_operator=req.operator,
                acting_operator=acting_operator,
                mode=mode,
                starts_at=starts_at,
                ends_at=ends_at,
                source_leave_request=req,
                created_by=request.user,
            )
            created_rows.append(row.id)

    return Response({"coverage_ids": created_rows}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def oic_leave_request_reject(request, leave_id: int):
    """OIC/Admin: reject a pending leave request (requires rejection_reason) and notify operator."""
    utype = getattr(request.user, "user_type", None)
    if utype not in (UserType.MANAGER, UserType.ADMIN):
        return Response({"error": "Only OIC and Admin can reject leave requests."}, status=status.HTTP_403_FORBIDDEN)

    req = OperatorLeaveRequest.objects.select_related("operator").filter(id=leave_id).first()
    if not req:
        return Response({"error": "Leave request not found."}, status=status.HTTP_404_NOT_FOUND)

    if req.status != OperatorLeaveRequest.Status.PENDING:
        return Response({"error": f"Cannot reject a request in status '{req.status}'."}, status=status.HTTP_400_BAD_REQUEST)

    if utype == UserType.MANAGER:
        my_dept = getattr(getattr(request.user, "department", None), "id", None) or getattr(request.user, "department_id", None)
        if my_dept and getattr(req.operator, "department_id", None) != my_dept:
            return Response({"error": "You can only reject leave requests within your department."}, status=status.HTTP_403_FORBIDDEN)

    data = request.data or {}
    rejection_reason = (str(data.get("rejection_reason") or "").strip())
    if len(rejection_reason) < 3:
        return Response({"error": "rejection_reason is required."}, status=status.HTTP_400_BAD_REQUEST)

    req.status = OperatorLeaveRequest.Status.REJECTED
    req.rejection_reason = rejection_reason
    req.reviewed_by = request.user
    req.reviewed_at = timezone.now()
    req.save(update_fields=["status", "rejection_reason", "reviewed_by", "reviewed_at"])

    threading.Thread(target=_send_leave_decision_email, kwargs={"req": req, "reviewer": request.user}, daemon=True).start()
    return Response({"id": req.id, "status": req.status}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def oic_leave_request_resume(request, leave_id: int):
    """OIC/Admin: shorten operator leave and coverage (same 10:00 local rule as operator self-resume)."""
    utype = getattr(request.user, "user_type", None)
    if utype not in (UserType.MANAGER, UserType.ADMIN):
        return Response({"error": "Only OIC and Admin can resume duty for a leave request."}, status=status.HTTP_403_FORBIDDEN)

    req = OperatorLeaveRequest.objects.select_related("operator").filter(id=leave_id).first()
    if not req:
        return Response({"error": "Leave request not found."}, status=status.HTTP_404_NOT_FOUND)

    if utype == UserType.MANAGER:
        my_dept = getattr(getattr(request.user, "department", None), "id", None) or getattr(request.user, "department_id", None)
        if my_dept and getattr(req.operator, "department_id", None) != my_dept:
            return Response({"error": "You can only act within your department."}, status=status.HTTP_403_FORBIDDEN)

    payload, err = _apply_resume_duty_leave_adjustment(req, request.user)
    if err:
        return Response(err[1], status=err[0])
    return Response(payload, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def team_calendar_department_leaves(request):
    """
    OIC/Admin: Team calendar data for lab operators in the same Department as the requester.

    Query params:
      - month: "YYYY-MM" (optional; default = current local month)
      - department_id: int (optional; admin only; else ignored)
    """
    utype = getattr(request.user, "user_type", None)
    if utype not in (UserType.MANAGER, UserType.ADMIN, UserType.OPERATOR):
        return Response({"error": "Only OIC, Admin, and Lab operators can access team calendar."}, status=status.HTTP_403_FORBIDDEN)

    month_raw = (request.query_params.get("month") or "").strip()
    today = timezone.localdate()
    year = today.year
    month = today.month
    if month_raw:
        try:
            parts = month_raw.split("-")
            year = int(parts[0])
            month = int(parts[1])
            if month < 1 or month > 12:
                raise ValueError
        except Exception:
            year = today.year
            month = today.month

    from datetime import date as date_cls
    from calendar import monthrange

    start = date_cls(year, month, 1)
    end = date_cls(year, month, monthrange(year, month)[1])

    dept_id = None
    if utype == UserType.ADMIN:
        dept_raw = (request.query_params.get("department_id") or "").strip()
        if dept_raw:
            try:
                dept_id = int(dept_raw)
            except ValueError:
                dept_id = None
    if dept_id is None:
        dept_id = getattr(getattr(request.user, "department", None), "id", None)
    if not dept_id:
        return Response(
            {"month": f"{year:04d}-{month:02d}", "operators": [], "leaves": []},
            status=status.HTTP_200_OK,
        )

    UserModel = User
    # Include all department members except Student/Faculty/Admin (e.g. Operator, OIC/Manager, Finance, etc.)
    excluded = {
        UserType.ADMIN,
        UserType.FACULTY,
        UserType.STUDENT,
        UserType.INDIVIDUAL_STUDENT,
    }
    members = list(
        UserModel.objects.filter(
            is_active=True,
            department_id=dept_id,
        )
        .exclude(user_type__in=list(excluded))
        .order_by("name", "email")
        .values("id", "name", "email", "user_type")
    )
    member_ids = [m["id"] for m in members]
    if not member_ids:
        return Response(
            {"month": f"{year:04d}-{month:02d}", "members": [], "leaves": []},
            status=status.HTTP_200_OK,
        )

    qs = (
        OperatorLeaveRequest.objects.filter(
            operator_id__in=member_ids,
            start_date__lte=end,
            end_date__gte=start,
        )
        .select_related("operator")
        .order_by("start_date", "id")
    )
    leaves = []
    for r in qs:
        leaves.append(
            {
                "id": r.id,
                "operator_id": r.operator_id,
                "operator_name": getattr(r.operator, "name", None) or getattr(r.operator, "email", ""),
                "start_date": r.start_date.isoformat(),
                "start_session": r.start_session,
                "end_date": r.end_date.isoformat(),
                "end_session": r.end_session,
                "status": r.status,
                "reason": r.reason,
                "rejection_reason": r.rejection_reason,
            }
        )

    # Weekend + institute holiday shading (date -> {reason, color})
    # Align weekend colors with weekly calendar (equipment_daily_slots) via get_calendar_colors().
    holiday_map = {}
    try:
        cal = get_calendar_colors() or {}
        saturday_color = (cal.get("saturday_color") or "#c7d2fe").strip() or "#c7d2fe"
        sunday_color = (cal.get("sunday_color") or "#fbcfe8").strip() or "#fbcfe8"
        holiday_default = (cal.get("holiday_default") or "#f59e0b").strip() or "#f59e0b"

        # Seed with explicit holidays (with per-holiday color)
        for row in Holiday.objects.filter(date__gte=start, date__lte=end, is_active=True).values_list(
            "date", "reason", "color"
        ):
            d, reason, color = row[0], row[1], row[2]
            holiday_map[d] = {
                "reason": reason or "Holiday",
                "color": (color or holiday_default).strip() or holiday_default,
            }

        # Add Saturdays/Sundays for the remaining dates
        from datetime import timedelta

        cur = start
        while cur <= end:
            wd = cur.weekday()
            if wd == 5:
                holiday_map.setdefault(cur, {"reason": "Saturday", "color": saturday_color})
            elif wd == 6:
                holiday_map.setdefault(cur, {"reason": "Sunday", "color": sunday_color})
            cur += timedelta(days=1)
    except Exception:
        holiday_map = {}

    return Response(
        {
            "month": f"{year:04d}-{month:02d}",
            "date_start": start.isoformat(),
            "date_end": end.isoformat(),
            "members": members,
            "leaves": leaves,
            "holidays": {
                d.isoformat(): {"reason": v.get("reason") or "Holiday", "color": v.get("color") or "#e5e7eb"}
                for d, v in holiday_map.items()
            },
        },
        status=status.HTTP_200_OK,
    )


def _is_internal_user(user):
    """True if user is student or faculty (internal)."""
    if not user or not getattr(user, "user_type", None):
        return False
    return user.user_type in UserType.get_internal_user_codes()


def _serialize_waitlist_entry_for_history(entry: WaitlistEntry, position: int) -> dict:
    """Shape waitlist entry like booking-list row for My Bookings history."""
    equipment = getattr(entry, "equipment", None)
    user = getattr(entry, "user", None)
    eq_code = getattr(equipment, "code", "") or ""
    dept_code = Booking.department_code_for_virtual_id(equipment)
    wl_vid = waitlist_virtual_booking_id(
        eq_code,
        position,
        department_code=dept_code,
        created_at=getattr(entry, "created_at", None),
    )

    # Latest booking attempt for this queue entry: prefer FAILED (join reason), else any (legacy / logging gaps).
    attempt = None
    try:
        eq_ref = getattr(entry, "equipment", None)
        user_ref = getattr(entry, "user", None)
        if eq_ref and user_ref:
            attempt = (
                BookingAttemptLog.objects.filter(
                    equipment=eq_ref,
                    user=user_ref,
                    outcome=BookingAttemptOutcome.FAILED,
                )
                .order_by("-requested_at")
                .first()
            )
            if not attempt:
                attempt = (
                    BookingAttemptLog.objects.filter(equipment=eq_ref, user=user_ref)
                    .order_by("-requested_at")
                    .first()
                )
    except Exception:
        attempt = None
    input_values = {}
    selected_parameters = []
    total_time_minutes = 0
    user_phone = None
    user_department = None
    wallet_owner_name = None
    accounts_in_charge = None
    lab_in_charge = None
    oic_contacts = []
    try:
        if attempt and isinstance(getattr(attempt, "additional_info", None), dict):
            info = attempt.additional_info or {}
            iv = info.get("input_values")
            if isinstance(iv, dict):
                # This already uses field labels (see _get_additional_info_from_request).
                input_values = iv
            sp = info.get("selected_parameters")
            if isinstance(sp, list):
                selected_parameters = sp
            elif isinstance(sp, str) and sp.strip():
                selected_parameters = [sp.strip()]
        # Prefer calculating duration from inputs (more reliable than logged duration).
        try:
            from .calculators import TimeCalculationEngine, build_safe_input_values_for_charge_calculation
            from .models import ChargeProfile, ChargeProfilePricingProfile
            from iic_booking.users.models.user_discounted_charge import UserDiscountedChargeEquipment

            slot_duration = int(getattr(equipment, "slot_duration_minutes", None) or 60) or 60
            # Map labeled input_values back to keys (A/B/...) for time calculation.
            raw_inputs = {}
            if attempt and isinstance(getattr(attempt, "additional_info", None), dict):
                info = attempt.additional_info or {}
                iv2 = info.get("input_values")
                raw_inputs = iv2 if isinstance(iv2, dict) else {}

            label_to_key = {}
            try:
                rows = DynamicInputField.objects.filter(equipment=equipment).values_list("field_key", "field_label")
                for k, lbl in rows:
                    if k:
                        label_to_key[str(k)] = str(k)
                    if lbl:
                        label_to_key[str(lbl)] = str(k)
            except Exception:
                label_to_key = {}

            normalized = {}
            for k, v in (raw_inputs or {}).items():
                nk = label_to_key.get(str(k), str(k))
                normalized[nk] = v
            safe_inputs = build_safe_input_values_for_charge_calculation(normalized, equipment=equipment)

            user_type = getattr(user, "user_type", None) if user else None
            pricing_profile = ChargeProfilePricingProfile.STANDARD
            if user and bool(getattr(user, "use_discounted_charge_profile", False)):
                overrides_exist = UserDiscountedChargeEquipment.objects.filter(user=user, is_active=True).exists()
                if not overrides_exist:
                    pricing_profile = ChargeProfilePricingProfile.DISCOUNTED
                else:
                    overridden = UserDiscountedChargeEquipment.objects.filter(user=user, equipment=equipment, is_active=True).exists()
                    pricing_profile = ChargeProfilePricingProfile.DISCOUNTED if overridden else ChargeProfilePricingProfile.STANDARD

            cp = None
            if user_type:
                cp = ChargeProfile.objects.filter(
                    equipment=equipment,
                    user_type=user_type,
                    pricing_profile=pricing_profile,
                    is_active=True,
                ).first()
                if not cp:
                    cp = ChargeProfile.objects.filter(
                        equipment=equipment,
                        user_type=user_type,
                        is_active=True,
                    ).first()
            if not cp:
                cp = ChargeProfile.objects.filter(equipment=equipment, is_active=True).order_by("id").first()
            if cp:
                class _ChargeProfileProxy:
                    def __init__(self, profile, eq):
                        self.equipment = profile.equipment
                        self.user_type = profile.user_type
                        self.is_active = profile.is_active
                        self.primary_unit_charge = profile.primary_unit_charge
                        self.secondary_unit_charge = profile.secondary_unit_charge
                        self.breakpoint = profile.breakpoint
                        self.time_formula = profile.time_formula
                        self.pricing_profile = getattr(profile, "pricing_profile", ChargeProfilePricingProfile.STANDARD)
                        self.profile_type = getattr(eq, "profile_type", None)

                total_time_minutes = int(
                    TimeCalculationEngine.calculate_time(
                        _ChargeProfileProxy(cp, equipment),
                        safe_inputs,
                        slot_duration_minutes=slot_duration,
                    )
                ) or 0
            elif attempt and getattr(attempt, "duration_minutes", None):
                total_time_minutes = int(attempt.duration_minutes or 0) or 0
            # If engine returned 0 (e.g. MULTI_PARAM lookup mismatch) but the attempt logged duration/slots, use that.
            if total_time_minutes <= 0 and attempt:
                dm = getattr(attempt, "duration_minutes", None)
                if dm is not None:
                    try:
                        total_time_minutes = max(0, int(dm))
                    except Exception:
                        total_time_minutes = 0
                if total_time_minutes <= 0:
                    try:
                        slots = max(1, int(getattr(attempt, "slots_requested", None) or 1))
                        total_time_minutes = slots * slot_duration
                    except Exception:
                        pass
        except Exception:
            if attempt and getattr(attempt, "duration_minutes", None):
                total_time_minutes = int(attempt.duration_minutes or 0) or 0
            if total_time_minutes <= 0 and attempt:
                try:
                    slot_duration_fb = int(getattr(equipment, "slot_duration_minutes", None) or 60) or 60
                    slots = max(1, int(getattr(attempt, "slots_requested", None) or 1))
                    total_time_minutes = slots * slot_duration_fb
                except Exception:
                    pass

        # User contact details (for BookingDetailCard header).
        if user:
            user_phone = getattr(user, "phone_number", None)
            dept = getattr(user, "department", None)
            user_department = getattr(dept, "name", None) if dept else None

        # Supervisor label (wallet owner) — reuse booking serializer helper when available.
        try:
            from .serializers import _get_wallet_owner_display_name  # type: ignore

            wallet_owner_name = _get_wallet_owner_display_name(user, {}) if user else None
        except Exception:
            wallet_owner_name = None

        # Accounts in charge (finance) + lab in charge + OIC contacts for "undersigned" sections.
        try:
            from iic_booking.users.models.user import User as UserModel
            from iic_booking.users.models.user_type import UserType as UserTypeEnum
            from .models import EquipmentManager, EquipmentOperator

            dept_id = getattr(getattr(equipment, "internal_department", None), "id", None)
            qs = UserModel.objects.filter(user_type=UserTypeEnum.FINANCE)
            if dept_id:
                qs = qs.filter(department_id=dept_id)
            u_fin = qs.order_by("id").first() or UserModel.objects.filter(user_type=UserTypeEnum.FINANCE).order_by("id").first()
            if u_fin:
                accounts_in_charge = {
                    "user_id": u_fin.id,
                    "name": u_fin.name or u_fin.email,
                    "email": u_fin.email,
                    "phone": u_fin.phone_number,
                    "user_type": "finance",
                }

            op_link = (
                EquipmentOperator.objects.filter(equipment_id=getattr(equipment, "equipment_id", None))
                .select_related("operator")
                .order_by("equipment_operator_id")
                .first()
            )
            op_user = getattr(op_link, "operator", None) if op_link else None
            if op_user:
                lab_in_charge = {
                    "user_id": op_user.id,
                    "name": op_user.name or op_user.email,
                    "email": op_user.email,
                    "phone": op_user.phone_number,
                    "user_type": "operator",
                }

            mgr_links = (
                EquipmentManager.objects.filter(equipment_id=getattr(equipment, "equipment_id", None))
                .select_related("manager")
                .order_by("equipment_manager_id")
            )
            for link in mgr_links:
                m = getattr(link, "manager", None)
                if not m:
                    continue
                oic_contacts.append(
                    {
                        "user_id": m.id,
                        "name": m.name or m.email,
                        "email": m.email,
                        "phone": m.phone_number,
                        "user_type": "manager",
                    }
                )
        except Exception:
            accounts_in_charge = None
            lab_in_charge = None
            oic_contacts = []
    except Exception:
        # Do not clear duration/inputs if they were computed; only contact fields may fail downstream.
        user_phone = None
        user_department = None
        wallet_owner_name = None
        accounts_in_charge = None
        lab_in_charge = None
        oic_contacts = []
    return {
        "booking_id": -int(entry.id),  # synthetic id for table rendering
        "virtual_booking_id": wl_vid,
        "user": getattr(user, "id", None),
        "user_email": getattr(user, "email", "") or "",
        "user_name": getattr(user, "name", "") or getattr(user, "email", ""),
        "user_phone": user_phone,
        "user_department": user_department,
        "wallet_owner_name": wallet_owner_name,
        "equipment": getattr(equipment, "equipment_id", None),
        "equipment_code": eq_code,
        "equipment_name": getattr(equipment, "name", "") or "",
        "equipment_slot_duration_minutes": int(getattr(equipment, "slot_duration_minutes", None) or 60) or 60,
        "total_time_minutes": total_time_minutes,
        "total_hours": round(total_time_minutes / 60.0, 2) if total_time_minutes else 0,
        "total_charge": "0.00",
        "input_values": input_values,
        "selected_parameters": selected_parameters,
        "charge_breakdown": [],
        "status": "WAITLISTED",
        "status_display": "Waitlisted",
        "notes": f"Current position in queue: WL{int(position)}",
        "start_time": "",
        "end_time": "",
        "daily_slots": [],
        "created_at": entry.created_at.isoformat() if getattr(entry, "created_at", None) else None,
        "updated_at": entry.created_at.isoformat() if getattr(entry, "created_at", None) else None,
        "waitlist_entry_id": int(entry.id),
        "waitlist_position": int(position),
        "waitlist_code": f"WL{int(position)}",
        "is_waitlist_entry": True,
        "accounts_in_charge": accounts_in_charge,
        "lab_in_charge": lab_in_charge,
        "oic_contacts": oic_contacts,
        "booking_attempt_requested_at": attempt.requested_at.isoformat() if attempt and attempt.requested_at else None,
        "booking_attempt_failure_reason": (attempt.failure_reason or "") if attempt else "",
        "booking_attempt_number_of_samples": getattr(attempt, "number_of_samples", None) if attempt else None,
        "booking_attempt_slots_requested": getattr(attempt, "slots_requested", None) if attempt else None,
        "booking_attempt_duration_minutes": getattr(attempt, "duration_minutes", None) if attempt else None,
        "booking_attempt_additional_info": getattr(attempt, "additional_info", None) if attempt else None,
    }


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def log_no_slot_allocation(request):
    """
    Log that the current user attempted to book but no slot was allocated.
    Body: equipment_id, number_of_samples (optional, default 1), slots_requested (optional, default 1), duration_minutes (optional).
    Used to maintain a log for admin/OIC when considering urgent booking requests.
    """
    if not _is_internal_user(request.user):
        return Response(
            {"error": "Only internal users (students/faculty) can use this endpoint."},
            status=status.HTTP_403_FORBIDDEN,
        )
    equipment_id = request.data.get("equipment_id")
    if not equipment_id:
        return Response(
            {"error": "equipment_id is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        equip = Equipment.objects.get(pk=int(equipment_id))
    except (ValueError, Equipment.DoesNotExist):
        return Response(
            {"error": "Invalid equipment_id."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    number_of_samples = int(request.data.get("number_of_samples") or 1)
    slots_requested = int(request.data.get("slots_requested") or 1)
    duration_minutes = request.data.get("duration_minutes")
    if duration_minutes is not None:
        try:
            duration_minutes = int(duration_minutes)
        except (ValueError, TypeError):
            duration_minutes = None
    NoSlotAllocationLog.objects.create(
        user=request.user,
        equipment=equip,
        number_of_samples=max(1, number_of_samples),
        slots_requested=max(1, slots_requested),
        duration_minutes=duration_minutes,
    )
    return Response(
        {"message": "Logged."},
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def log_booking_attempt(request):
    """
    Log every booking submit attempt (success or failure). Called by frontend on Submit booking.
    Body: equipment_id, outcome (SUCCESS|FAILED), failure_reason (optional), booking_id (optional when SUCCESS),
          number_of_samples (optional), slots_requested (optional), duration_minutes (optional).
    """
    equipment_id = request.data.get("equipment_id")
    if not equipment_id:
        return Response(
            {"error": "equipment_id is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    outcome = (request.data.get("outcome") or "").strip().upper()
    if outcome not in (BookingAttemptOutcome.SUCCESS, BookingAttemptOutcome.FAILED):
        return Response(
            {"error": "outcome must be SUCCESS or FAILED."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        equip = Equipment.objects.get(pk=int(equipment_id))
    except (ValueError, Equipment.DoesNotExist):
        return Response(
            {"error": "Invalid equipment_id."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    failure_reason = (request.data.get("failure_reason") or "").strip() if outcome == BookingAttemptOutcome.FAILED else ""
    booking_id = request.data.get("booking_id")
    if booking_id is not None:
        try:
            booking_id = int(booking_id)
        except (ValueError, TypeError):
            booking_id = None
    number_of_samples = int(request.data.get("number_of_samples") or 1)
    slots_requested = int(request.data.get("slots_requested") or 1)
    duration_minutes = request.data.get("duration_minutes")
    if duration_minutes is not None:
        try:
            duration_minutes = int(duration_minutes)
        except (ValueError, TypeError):
            duration_minutes = None
    additional_info = _get_additional_info_from_request(request, equip)
    BookingAttemptLog.objects.create(
        user=request.user,
        equipment=equip,
        outcome=outcome,
        failure_reason=failure_reason[:2000] if failure_reason else "",  # cap length
        number_of_samples=max(1, number_of_samples),
        slots_requested=max(1, slots_requested),
        duration_minutes=duration_minutes,
        booking_id=booking_id,
        additional_info=additional_info,
    )
    return Response(
        {"message": "Logged."},
        status=status.HTTP_201_CREATED,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def create_urgent_booking_request(request):
    """
    Create an urgent booking request.
    Two types: NO_SLOT (unable to get slot despite repeated trials), REVIEWER_URGENT (urgent comment from reviewer).
    For REVIEWER_URGENT: documentary evidence file is required (multipart: evidence_file).
    User must have at least one NoSlotAllocationLog (or failed attempt) in last 90 days for NO_SLOT.
    Body: equipment_id, request_type (NO_SLOT | REVIEWER_URGENT), disclaimer_accepted (true),
          number_of_samples, slots_requested, duration_minutes (optional).
    For REVIEWER_URGENT also send: evidence_file (file upload), evidence_original_name (optional),
    reviewer_comment (required text, 10–8000 characters).
    """
    if not _is_internal_user(request.user):
        return Response(
            {"error": "Only internal users (students/faculty) can request urgent booking."},
            status=status.HTTP_403_FORBIDDEN,
        )
    equipment_id = request.data.get("equipment_id")
    if not equipment_id:
        return Response(
            {"error": "equipment_id is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    request_type_val = (request.data.get("request_type") or "NO_SLOT").strip().upper()
    if request_type_val not in (UrgentBookingRequestType.NO_SLOT, UrgentBookingRequestType.REVIEWER_URGENT):
        return Response(
            {"error": "request_type must be NO_SLOT or REVIEWER_URGENT."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if (
        request_type_val == UrgentBookingRequestType.REVIEWER_URGENT
        and request.user.user_type != UserType.FACULTY
    ):
        return Response(
            {"error": "Only faculty users can submit 'Urgent comment from reviewer' requests."},
            status=status.HTTP_403_FORBIDDEN,
        )
    disclaimer_accepted = request.data.get("disclaimer_accepted")
    if disclaimer_accepted not in (True, "true", "True", "1"):
        return Response(
            {"error": "You must accept the disclaimer to submit an urgent booking request."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        equip = Equipment.objects.get(pk=int(equipment_id))
    except (ValueError, Equipment.DoesNotExist):
        return Response(
            {"error": "Invalid equipment_id."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    # Block repeated urgent requests: same user + same equipment until previous is EXPIRED
    has_active = UrgentBookingRequest.objects.filter(
        user=request.user,
        equipment=equip,
    ).exclude(status=UrgentBookingRequestStatus.EXPIRED).exists()
    if has_active:
        return Response(
            {"error": "You already have an active or decided request for this equipment. You can raise a new request only after the current one expires."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    # NO_SLOT only: block if regular slots exist — user should book normally. REVIEWER_URGENT is independent of slot availability.
    if request_type_val == UrgentBookingRequestType.NO_SLOT:
        from datetime import date, timedelta
        from django.conf import settings as django_settings
        from django.utils import timezone as tz
        now_local = tz.localtime(tz.now()) if getattr(django_settings, "USE_TZ", True) else tz.now()
        now_date = tz.localdate()
        days_since_monday = now_date.weekday()
        current_week_start = now_date - timedelta(days=days_since_monday)
        search_end_date = current_week_start + timedelta(days=6)
        effective_ref_weekday, effective_ref_time = get_equipment_slot_window_reference_config(equip)
        if effective_ref_weekday is not None and effective_ref_time is not None:
            current_weekday = now_local.weekday()
            current_time = now_local.time()
            ref_weekday = int(effective_ref_weekday)
            ref_time = effective_ref_time
            before_reference = (current_weekday < ref_weekday) or (
                current_weekday == ref_weekday and current_time < ref_time
            )
            if not before_reference:
                search_end_date = current_week_start + timedelta(days=13)
        available_slot_filter = {
            "slot_master__equipment": equip,
            "date__gte": now_date,
            "date__lte": search_end_date,
            "status": SlotStatus.AVAILABLE,
            "start_datetime__gte": now_utc,
        }
        if UserType.is_external_user(getattr(request.user, "user_type", None)):
            available_slot_filter["reserved_for_external"] = True
        else:
            available_slot_filter["reserved_for_external"] = False
        available_this_week_qs = DailySlot.objects.filter(**available_slot_filter)
        time_from = getattr(equip, "weekly_view_time_from", None)
        time_to = getattr(equip, "weekly_view_time_to", None)
        if time_from is None and time_to is None:
            has_available_this_week = available_this_week_qs.exists()
        else:
            has_available_this_week = False
            for slot in available_this_week_qs.only("id", "start_datetime", "end_datetime"):
                if not slot.start_datetime or not slot.end_datetime:
                    continue
                start_local = tz.localtime(slot.start_datetime) if tz.is_aware(slot.start_datetime) else slot.start_datetime
                end_local = tz.localtime(slot.end_datetime) if tz.is_aware(slot.end_datetime) else slot.end_datetime
                st, et = start_local.time(), end_local.time()
                if time_from is not None and st < time_from:
                    continue
                if time_to is not None and et > time_to:
                    continue
                has_available_this_week = True
                break
        if has_available_this_week:
            return Response(
                {"error": "Slots are available in the current week for this equipment. Please book normally instead of raising an urgent request."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    # Enforce max urgent requests per equipment (Admin/OIC configurable cap)
    if getattr(equip, "max_urgent_requests", None) is not None and equip.max_urgent_requests is not None:
        pending_count = UrgentBookingRequest.objects.filter(
            equipment=equip,
            status=UrgentBookingRequestStatus.PENDING,
        ).count()
        if pending_count >= equip.max_urgent_requests:
            return Response(
                {"error": "Maximum number of urgent requests for this equipment has been reached. Please try again later."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    from django.utils import timezone
    from datetime import timedelta
    if request_type_val == UrgentBookingRequestType.NO_SLOT:
        cutoff = timezone.now() - timedelta(days=90)
        has_log = NoSlotAllocationLog.objects.filter(
            user=request.user,
            requested_at__gte=cutoff,
        ).exists() or BookingAttemptLog.objects.filter(
            user=request.user,
            outcome=BookingAttemptOutcome.FAILED,
            requested_at__gte=cutoff,
        ).exists()
        if not has_log:
            return Response(
                {"error": "No record found for your booking request. Your request cannot be entertained. Please try to book slots first; if no slots are available, try again and then submit an urgent request."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    else:
        evidence_file = request.FILES.get("evidence_file")
        if not evidence_file:
            return Response(
                {"error": "Documentary evidence file is required for 'Urgent comment from reviewer'. Please upload a document."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        reviewer_comment = (request.data.get("reviewer_comment") or "").strip()
        if len(reviewer_comment) < 10:
            return Response(
                {"error": "Reviewer comment is required (at least 10 characters). Summarize the reviewer feedback or urgency."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if len(reviewer_comment) > 8000:
            return Response(
                {"error": "Reviewer comment is too long (maximum 8000 characters)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    number_of_samples = int(request.data.get("number_of_samples") or 1)
    slots_requested = int(request.data.get("slots_requested") or 1)
    duration_minutes = request.data.get("duration_minutes")
    if duration_minutes is not None:
        try:
            duration_minutes = int(duration_minutes)
        except (ValueError, TypeError):
            duration_minutes = None
    hold_booking_id = request.data.get("hold_booking_id")
    req = UrgentBookingRequest(
        user=request.user,
        equipment=equip,
        request_type=request_type_val,
        disclaimer_accepted=True,
        number_of_samples=max(1, number_of_samples),
        slots_requested=max(1, slots_requested),
        duration_minutes=duration_minutes,
    )
    if hold_booking_id is not None:
        try:
            hold_booking = Booking.objects.get(
                pk=int(hold_booking_id),
                user=request.user,
                equipment=equip,
                status=BookingStatus.HOLD,
            )
            req.hold_booking = hold_booking
        except (ValueError, Booking.DoesNotExist):
            return Response(
                {"error": "Invalid hold_booking_id. The booking must be yours, for this equipment, and in Hold status."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    if request_type_val == UrgentBookingRequestType.REVIEWER_URGENT:
        req.evidence_file = request.FILES.get("evidence_file")
        req.evidence_original_name = (request.data.get("evidence_original_name") or (req.evidence_file.name if req.evidence_file else ""))[:255]
        req.reviewer_comment = (request.data.get("reviewer_comment") or "").strip()[:8000]
    req.save()
    # Acknowledgement to the requester (consistent subject/body with portal wording)
    try:
        user_link = get_frontend_absolute_url("/my-urgent-requests")
        if request_type_val == UrgentBookingRequestType.NO_SLOT:
            rt_label = "No slot available despite repeated attempts"
            next_steps = (
                "An administrator will review your request. You can track its status using the link below."
            )
        else:
            rt_label = "Urgent comment from reviewer (with documentary evidence)"
            next_steps = (
                "An administrator/Officer in charge will review your request. "
                "You can track progress using the link below."
            )
        CommunicationService.send_email(
            recipient=request.user,
            template="urgent_booking_request_submitted_user_email",
            template_context={
                "user_name": request.user.name or request.user.email,
                "request_id": req.id,
                "equipment_name": equip.name,
                "equipment_code": equip.code,
                "request_type_label": rt_label,
                "next_steps": next_steps,
                "link": user_link,
            },
            metadata={"urgent_booking_request_id": req.id},
        )
    except Exception as e:
        logger.warning(
            "Failed to send urgent request submitted email to %s: %s",
            getattr(request.user, "email", ""),
            e,
            exc_info=True,
        )
    message = "Urgent booking request submitted. Admin/Officer in charge will review it."
    return Response(
        {
            "message": message,
            "id": req.id,
        },
        status=status.HTTP_201_CREATED,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_approved_urgent_history(request):
    """
    For the current user (internal only): return their approved urgent requests for a given equipment
    in the past 6 months. Used when raising a new request to show "Your approved urgent (this equipment, 6m): N".
    Query param: equipment_id (required).
    """
    if not _is_internal_user(request.user):
        return Response(
            {"error": "Only internal users can view their approved urgent history."},
            status=status.HTTP_403_FORBIDDEN,
        )
    equipment_id = request.query_params.get("equipment_id")
    if not equipment_id:
        return Response(
            {"error": "equipment_id is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        equip = Equipment.objects.get(pk=int(equipment_id))
    except (ValueError, Equipment.DoesNotExist):
        return Response(
            {"error": "Invalid equipment_id."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    rows = get_approved_urgent_requests_last_6_months(request.user.id, equip.equipment_id)
    return Response(
        {
            "approved_requests": _format_approved_urgent_6m(rows),
            "count": len(rows),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_my_urgent_booking_requests(request):
    """
    List the current user's own urgent booking requests (for IIT Students / internal users).
    Returns a simplified list with id, equipment, status, requested_at for dashboard and status view.
    """
    if not _is_internal_user(request.user):
        return Response(
            {"error": "Only internal users (students/faculty) can view their urgent requests."},
            status=status.HTTP_403_FORBIDDEN,
        )
    requests_qs = (
        UrgentBookingRequest.objects
        .filter(user=request.user)
        .select_related("equipment")
        .order_by("-requested_at")
    )
    status_filter = request.query_params.get("status", "").strip().upper()
    if status_filter and status_filter in UrgentBookingRequestStatus.values:
        requests_qs = requests_qs.filter(status=status_filter)
    limit = min(int(request.query_params.get("limit", 50) or 50), 100)
    offset = int(request.query_params.get("offset", 0) or 0)
    total_count = requests_qs.count()
    requests_qs = requests_qs[offset : offset + limit]
    results = []
    config = UrgentHoldExpiryConfig.objects.first()
    validity_days = 1
    if config and getattr(config, "urgent_booking_validity_days", None) is not None:
        validity_days = config.urgent_booking_validity_days or 1
    for req in requests_qs:
        expiry_at = get_effective_expiry_at(req, validity_days)
        results.append({
            "id": req.id,
            "equipment_id": req.equipment_id,
            "equipment_code": req.equipment.code,
            "equipment_name": req.equipment.name,
            "request_type": req.request_type,
            "status": req.status,
            "requested_at": req.requested_at.isoformat() if req.requested_at else None,
            "decided_at": req.decided_at.isoformat() if req.decided_at else None,
            "expiry_at": expiry_at.isoformat() if expiry_at else None,
            "pending_wallet_approval": False,
        })
    return Response(
        {"urgent_requests": results, "total_count": total_count, "limit": limit, "offset": offset},
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_urgent_booking_requests(request):
    """
    List urgent booking requests. Admin and Officer in charge only.
    Returns list with user, equipment, requested_at, status, and no-slot log summary for each user/equipment.
    """
    if not check_operator_permission(request.user):
        return Response(
            {"error": "Only admin and Officer in charge can view urgent requests."},
            status=status.HTTP_403_FORBIDDEN,
        )
    requests_qs = (
        UrgentBookingRequest.objects
        .select_related("user", "equipment", "decided_by", "wallet_approved_by", "hold_booking")
        .order_by("-requested_at")
    )
    # Restrict OIC/manager to their managed equipment; operator to their mapped equipment; admin sees all
    equipment_ids = _get_equipment_ids_for_log_access(request.user)
    if equipment_ids is not None:
        if len(equipment_ids) == 0:
            requests_qs = requests_qs.none()
        else:
            requests_qs = requests_qs.filter(equipment_id__in=equipment_ids)
    status_filter = request.query_params.get("status", "").strip().upper()
    if status_filter and status_filter in UrgentBookingRequestStatus.values:
        requests_qs = requests_qs.filter(status=status_filter)
    limit = min(int(request.query_params.get("limit", 50) or 50), 100)
    offset = int(request.query_params.get("offset", 0) or 0)
    total_count = requests_qs.count()
    requests_qs = requests_qs[offset : offset + limit]
    results = []
    config_for_expiry = UrgentHoldExpiryConfig.objects.first()
    validity_days_list = 1
    if config_for_expiry and getattr(config_for_expiry, "urgent_booking_validity_days", None) is not None:
        validity_days_list = config_for_expiry.urgent_booking_validity_days or 1
    key_to_label_cache = {}
    urgent_log_cache = {}
    approved_6m_cache = {}
    for req in requests_qs:
        log_cache_key = (req.user_id, req.equipment_id)
        if log_cache_key in urgent_log_cache:
            log_count, log_recent = urgent_log_cache[log_cache_key]
        else:
            log_count, log_recent = _get_no_slot_log_for_urgent_display(req.user, req.equipment)
            urgent_log_cache[log_cache_key] = (log_count, log_recent)
        evidence_url = None
        if req.evidence_file:
            try:
                evidence_url = default_storage.url(req.evidence_file.name)
            except Exception:
                pass
        pending_wallet = False
        expiry_at = get_effective_expiry_at(req, validity_days_list)
        if log_cache_key in approved_6m_cache:
            approved_6m = approved_6m_cache[log_cache_key]
        else:
            approved_6m = get_approved_urgent_requests_last_6_months(req.user_id, req.equipment_id)
            approved_6m_cache[log_cache_key] = approved_6m
        hold_booking_summary = None
        if req.hold_booking_id and req.hold_booking:
            hb = req.hold_booking
            daily_slots_list = list(
                hb.daily_slots.select_related("slot_master").order_by("start_datetime")
            )
            slot_times = []
            for ds in daily_slots_list:
                label = None
                if getattr(ds, "slot_master", None):
                    sm = ds.slot_master
                    label = sm.slot_name or (f"Slot {sm.slot_number}" if getattr(sm, "slot_number", None) is not None else None)
                slot_times.append({
                    "start": ds.start_datetime.isoformat() if ds.start_datetime else None,
                    "end": ds.end_datetime.isoformat() if ds.end_datetime else None,
                    "label": label,
                })
            input_vals = hb.input_values or {}
            if req.equipment_id in key_to_label_cache:
                key_to_label = key_to_label_cache[req.equipment_id]
            else:
                key_to_label = _get_input_field_key_to_label_map(req.equipment)
                key_to_label_cache[req.equipment_id] = key_to_label
            input_values_with_labels = {}
            for key, value in input_vals.items():
                label = key_to_label.get(key, key)
                input_values_with_labels[label] = value
            hold_booking_summary = {
                "booking_id": booking_display_id_for_email(hb),
                "real_booking_id": hb.booking_id,
                "total_charge": str(hb.total_charge) if hb.total_charge is not None else None,
                "total_time_minutes": hb.total_time_minutes,
                "slot_times": slot_times,
                "input_values": input_values_with_labels,
                "charge_breakdown": hb.charge_breakdown if getattr(hb, "charge_breakdown", None) else None,
            }
        results.append({
            "id": req.id,
            "request_type": req.request_type,
            "user_id": req.user_id,
            "user_name": req.user.name or req.user.email,
            "user_email": req.user.email,
            "equipment_id": req.equipment_id,
            "equipment_code": req.equipment.code,
            "equipment_name": req.equipment.name,
            "requested_at": req.requested_at.isoformat() if req.requested_at else None,
            "disclaimer_accepted": req.disclaimer_accepted,
            "number_of_samples": req.number_of_samples,
            "slots_requested": req.slots_requested,
            "duration_minutes": req.duration_minutes,
            "evidence_file_url": evidence_url,
            "evidence_original_name": req.evidence_original_name or "",
            "reviewer_comment": req.reviewer_comment or "",
            "wallet_approved_at": req.wallet_approved_at.isoformat() if req.wallet_approved_at else None,
            "wallet_approved_by_name": (req.wallet_approved_by.name or req.wallet_approved_by.email) if req.wallet_approved_by else None,
            "wallet_notes": req.wallet_notes or "",
            "pending_wallet_approval": pending_wallet,
            "status": req.status,
            "admin_notes": req.admin_notes or "",
            "decided_at": req.decided_at.isoformat() if req.decided_at else None,
            "decided_by_name": (req.decided_by.name or req.decided_by.email) if req.decided_by else None,
            "expiry_at": expiry_at.isoformat() if expiry_at else None,
            "requester_approved_urgent_last_6_months": _format_approved_urgent_6m(approved_6m),
            "no_slot_log_count": log_count,
            "no_slot_log_entries": log_recent,
            "hold_booking_id": req.hold_booking_id,
            "hold_booking_summary": hold_booking_summary,
        })
    return Response(
        {"urgent_requests": results, "total_count": total_count, "limit": limit, "offset": offset},
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_urgent_request_detail(request, request_id):
    """
    Get a single urgent request detail (same structure as one item from list_urgent_booking_requests).
    Allowed: admin/OIC only.
    """
    try:
        urg = UrgentBookingRequest.objects.select_related(
            "user", "equipment", "decided_by", "wallet_approved_by", "hold_booking"
        ).get(pk=int(request_id))
    except (ValueError, UrgentBookingRequest.DoesNotExist):
        return Response({"error": "Urgent request not found."}, status=status.HTTP_404_NOT_FOUND)
    if not check_operator_permission(request.user):
        return Response(
            {"error": "You are not allowed to view this request."},
            status=status.HTTP_403_FORBIDDEN,
        )
    # OIC/operator may only view requests for their managed equipment
    if check_operator_permission(request.user):
        allowed_ids = _get_equipment_ids_for_log_access(request.user)
        if allowed_ids is not None and urg.equipment_id not in allowed_ids:
            return Response(
                {"error": "You do not have permission to view this request (equipment not under your charge)."},
                status=status.HTTP_403_FORBIDDEN,
            )
    log_count, log_recent = _get_no_slot_log_for_urgent_display(urg.user, urg.equipment)
    evidence_url = None
    if urg.evidence_file:
        try:
            evidence_url = default_storage.url(urg.evidence_file.name)
        except Exception:
            pass
    pending_wallet = False
    hold_booking_summary = None
    if urg.hold_booking_id and urg.hold_booking:
        hb = urg.hold_booking
        daily_slots_list = list(
            hb.daily_slots.select_related("slot_master").order_by("start_datetime")
        )
        slot_times = []
        for ds in daily_slots_list:
            label = None
            if getattr(ds, "slot_master", None):
                sm = ds.slot_master
                label = sm.slot_name or (f"Slot {sm.slot_number}" if getattr(sm, "slot_number", None) is not None else None)
            slot_times.append({
                "start": ds.start_datetime.isoformat() if ds.start_datetime else None,
                "end": ds.end_datetime.isoformat() if ds.end_datetime else None,
                "label": label,
            })
        input_vals = hb.input_values or {}
        key_to_label = _get_input_field_key_to_label_map(urg.equipment)
        input_values_with_labels = {}
        for key, value in input_vals.items():
            label = key_to_label.get(key, key)
            input_values_with_labels[label] = value
        hold_booking_summary = {
            "booking_id": booking_display_id_for_email(hb),
            "real_booking_id": hb.booking_id,
            "total_charge": str(hb.total_charge) if hb.total_charge is not None else None,
            "total_time_minutes": hb.total_time_minutes,
            "slot_times": slot_times,
            "input_values": input_values_with_labels,
            "charge_breakdown": hb.charge_breakdown if getattr(hb, "charge_breakdown", None) else None,
        }
    config_detail = UrgentHoldExpiryConfig.objects.first()
    validity_days_detail = 1
    if config_detail and getattr(config_detail, "urgent_booking_validity_days", None) is not None:
        validity_days_detail = config_detail.urgent_booking_validity_days or 1
    expiry_at_detail = get_effective_expiry_at(urg, validity_days_detail)
    approved_6m_detail = get_approved_urgent_requests_last_6_months(urg.user_id, urg.equipment_id)
    result = {
        "id": urg.id,
        "request_type": urg.request_type,
        "user_id": urg.user_id,
        "user_name": urg.user.name or urg.user.email,
        "user_email": urg.user.email,
        "equipment_id": urg.equipment_id,
        "equipment_code": urg.equipment.code,
        "equipment_name": urg.equipment.name,
        "requested_at": urg.requested_at.isoformat() if urg.requested_at else None,
        "disclaimer_accepted": urg.disclaimer_accepted,
        "number_of_samples": urg.number_of_samples,
        "slots_requested": urg.slots_requested,
        "duration_minutes": urg.duration_minutes,
        "evidence_file_url": evidence_url,
        "evidence_original_name": urg.evidence_original_name or "",
        "reviewer_comment": urg.reviewer_comment or "",
        "wallet_approved_at": urg.wallet_approved_at.isoformat() if urg.wallet_approved_at else None,
        "wallet_approved_by_name": (urg.wallet_approved_by.name or urg.wallet_approved_by.email) if urg.wallet_approved_by else None,
        "wallet_notes": urg.wallet_notes or "",
        "pending_wallet_approval": pending_wallet,
        "status": urg.status,
        "admin_notes": urg.admin_notes or "",
        "decided_at": urg.decided_at.isoformat() if urg.decided_at else None,
        "decided_by_name": (urg.decided_by.name or urg.decided_by.email) if urg.decided_by else None,
        "expiry_at": expiry_at_detail.isoformat() if expiry_at_detail else None,
        "requester_approved_urgent_last_6_months": _format_approved_urgent_6m(approved_6m_detail),
        "no_slot_log_count": log_count,
        "no_slot_log_entries": log_recent,
        "hold_booking_id": urg.hold_booking_id,
        "hold_booking_summary": hold_booking_summary,
    }
    return Response(result, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_no_slot_log_for_user(request, user_id):
    """Get no-slot allocation log for a user (admin/OIC only). Used when reviewing urgent request."""
    if not check_operator_permission(request.user):
        return Response(
            {"error": "Only admin and Officer in charge can view this."},
            status=status.HTTP_403_FORBIDDEN,
        )
    from django.contrib.auth import get_user_model
    User = get_user_model()
    try:
        target_user = User.objects.get(pk=int(user_id))
    except (ValueError, User.DoesNotExist):
        return Response({"error": "User not found."}, status=status.HTTP_404_NOT_FOUND)
    equipment_id = request.query_params.get("equipment_id")
    qs = NoSlotAllocationLog.objects.filter(user=target_user).select_related("equipment").order_by("-requested_at")
    if equipment_id:
        try:
            qs = qs.filter(equipment_id=int(equipment_id))
        except ValueError:
            pass
    limit = min(int(request.query_params.get("limit", 100) or 100), 200)
    entries = list(qs[:limit].values("id", "requested_at", "equipment_id", "number_of_samples", "slots_requested", "duration_minutes"))
    for e in entries:
        if e.get("requested_at"):
            e["requested_at"] = e["requested_at"].isoformat()
    return Response(
        {"user_id": target_user.id, "user_name": target_user.name or target_user.email, "entries": entries},
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_booking_attempt_logs(request):
    """
    List comprehensive booking attempt log (success and failure). Admin and Officer in charge only.
    Admin: no equipment restriction. OIC/Manager: only equipments for which they are officer in charge.
    Query params: equipment_id, user_id, outcome (SUCCESS|FAILED), date_from (YYYY-MM-DD), date_to (YYYY-MM-DD),
                  failure_reason_contains (search in failure_reason), department_id (filter by user's department),
                  limit, offset.
    """
    if not check_operator_permission(request.user):
        return Response(
            {"error": "Only admin and Officer in charge can view the booking attempt log."},
            status=status.HTTP_403_FORBIDDEN,
        )
    equipment_ids = _get_equipment_ids_for_log_access(request.user)
    if equipment_ids is not None and len(equipment_ids) == 0:
        # Manager/operator with no assigned equipments
        return Response(
            {"results": [], "total_count": 0, "limit": 0, "offset": 0},
            status=status.HTTP_200_OK,
        )
    qs = (
        BookingAttemptLog.objects.select_related("user", "equipment")
        .order_by("-requested_at")
    )
    if equipment_ids is not None:
        qs = qs.filter(equipment_id__in=equipment_ids)
    equipment_id = request.query_params.get("equipment_id")
    if equipment_id:
        try:
            qs = qs.filter(equipment_id=int(equipment_id))
        except ValueError:
            pass
    user_id = request.query_params.get("user_id")
    if user_id:
        try:
            qs = qs.filter(user_id=int(user_id))
        except ValueError:
            pass
    department_id = request.query_params.get("department_id")
    if department_id:
        try:
            qs = qs.filter(user__department_id=int(department_id))
        except ValueError:
            pass
    outcome = request.query_params.get("outcome", "").strip().upper()
    if outcome in (BookingAttemptOutcome.SUCCESS, BookingAttemptOutcome.FAILED):
        qs = qs.filter(outcome=outcome)
    date_from = request.query_params.get("date_from", "").strip()
    if date_from:
        try:
            start_date = datetime.strptime(date_from, "%Y-%m-%d").date()
            start_dt = timezone.make_aware(datetime.combine(start_date, datetime.min.time()))
            qs = qs.filter(requested_at__gte=start_dt)
        except ValueError:
            pass
    date_to = request.query_params.get("date_to", "").strip()
    if date_to:
        try:
            end_date = datetime.strptime(date_to, "%Y-%m-%d").date()
            end_dt = timezone.make_aware(datetime.combine(end_date + timedelta(days=1), datetime.min.time()))
            qs = qs.filter(requested_at__lt=end_dt)
        except ValueError:
            pass
    failure_reason_contains = request.query_params.get("failure_reason_contains", "").strip()
    if failure_reason_contains:
        qs = qs.filter(failure_reason__icontains=failure_reason_contains)
    total_count = qs.count()
    limit = min(int(request.query_params.get("limit", 50) or 50), 200)
    offset = int(request.query_params.get("offset", 0) or 0)
    page = list(qs[offset : offset + limit])
    # Build display_booking_id (virtual_booking_id or equipment_code-booking_id) for successful logs
    booking_ids = [log.booking_id for log in page if log.booking_id is not None]
    display_booking_id_map = {}
    if booking_ids:
        for bid, vbid in Booking.objects.filter(
            booking_id__in=booking_ids
        ).values_list("booking_id", "virtual_booking_id"):
            display_booking_id_map[bid] = (vbid or "").strip() or None
    results = []
    for log in page:
        display_booking_id = None
        if log.booking_id is not None:
            display_booking_id = display_booking_id_map.get(log.booking_id)
            if not display_booking_id and log.equipment:
                display_booking_id = f"{log.equipment.code}-{log.booking_id}"
        results.append({
            "id": log.id,
            "user_id": log.user_id,
            "user_name": log.user.name or log.user.email,
            "user_email": log.user.email,
            "equipment_id": log.equipment_id,
            "equipment_code": log.equipment.code,
            "equipment_name": log.equipment.name,
            "requested_at": log.requested_at.isoformat() if log.requested_at else None,
            "outcome": log.outcome,
            "failure_reason": log.failure_reason or "",
            "number_of_samples": log.number_of_samples,
            "slots_requested": log.slots_requested,
            "duration_minutes": log.duration_minutes,
            "booking_id": display_booking_id,
            "real_booking_id": log.booking_id,
            "display_booking_id": display_booking_id,
            "additional_info": log.additional_info,
        })
    return Response(
        {"results": results, "total_count": total_count, "limit": limit, "offset": offset},
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_my_unsuccessful_booking_attempts(request):
    """
    List current user's unsuccessful (FAILED) booking attempts for an equipment in the past 2 weeks.
    Used on the Request urgent booking popup when reason is "Unable to get slot despite repeated trials".
    Excludes failures due to weekly/monthly quota (individual or faculty).
    When equipment has slot window (internal users) and urgent_peak_window_minutes set, only returns
    entries whose requested_at falls within [slot_window_ref_time, slot_window_ref_time + peak_minutes]
    on the slot window reference weekday.
    Query params: equipment_id (required).
    """
    equipment_id = request.query_params.get("equipment_id")
    if not equipment_id:
        return Response(
            {"error": "equipment_id is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        equipment_id = int(equipment_id)
    except ValueError:
        return Response(
            {"error": "Invalid equipment_id."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    from datetime import timedelta
    from django.utils import timezone

    try:
        equipment = Equipment.objects.get(pk=equipment_id)
    except Equipment.DoesNotExist:
        return Response(
            {"error": "Equipment not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    two_weeks_ago = timezone.now() - timedelta(days=14)
    qs = (
        BookingAttemptLog.objects
        .filter(
            user=request.user,
            equipment_id=equipment_id,
            outcome=BookingAttemptOutcome.FAILED,
            requested_at__gte=two_weeks_ago,
        )
        .select_related("equipment")
        .order_by("-requested_at")[:100]
    )

    # Effective slot window (internal users): per-equipment when set, else global
    effective_ref_weekday, effective_ref_time = get_equipment_slot_window_reference_config(equipment)

    peak_minutes = getattr(equipment, "urgent_peak_window_minutes", None)
    apply_time_window = (
        effective_ref_weekday is not None
        and effective_ref_time is not None
        and peak_minutes is not None
        and peak_minutes > 0
    )

    def is_quota_failure(failure_reason):
        if not failure_reason:
            return False
        r = failure_reason.strip().lower()
        if "quota check failed" in r:
            return True
        if "weekly quota" in r or "monthly quota" in r:
            return True
        return False

    def in_peak_window(requested_at):
        if not apply_time_window or requested_at is None:
            return True
        local_dt = timezone.localtime(requested_at)
        if local_dt.weekday() != effective_ref_weekday:
            return False
        ref_time = effective_ref_time
        if hasattr(ref_time, "hour"):
            start_minutes = ref_time.hour * 60 + ref_time.minute
        else:
            parts = str(ref_time).split(":")
            start_minutes = int(parts[0]) * 60 + (int(parts[1]) if len(parts) > 1 else 0)
        end_minutes = start_minutes + peak_minutes
        log_minutes = local_dt.hour * 60 + local_dt.minute
        return start_minutes <= log_minutes <= end_minutes

    results = []
    for log in qs:
        if is_quota_failure(log.failure_reason):
            continue
        if not in_peak_window(log.requested_at):
            continue
        results.append({
            "id": log.id,
            "requested_at": log.requested_at.isoformat() if log.requested_at else None,
            "outcome": log.outcome,
            "failure_reason": log.failure_reason or "",
            "number_of_samples": log.number_of_samples,
            "slots_requested": log.slots_requested,
            "duration_minutes": log.duration_minutes,
        })
    return Response(
        {"entries": results, "equipment_id": equipment_id},
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_my_waitlist_entries(request):
    """List active waitlist entries for the logged-in user."""
    entries = (
        WaitlistEntry.objects.filter(user=request.user)
        .select_related("equipment", "user")
        .order_by("-created_at")
    )
    results = []
    entries_by_equipment = {}
    for entry in entries:
        entries_by_equipment.setdefault(entry.equipment_id, []).append(entry)
    position_by_entry_id = {}
    for _, eq_entries in entries_by_equipment.items():
        eq_entries_sorted = sorted(eq_entries, key=lambda e: (e.created_at, e.id))
        for idx, eq_entry in enumerate(eq_entries_sorted, start=1):
            position_by_entry_id[eq_entry.id] = idx
    for entry in entries:
        position = position_by_entry_id.get(entry.id, 0)
        results.append(_serialize_waitlist_entry_for_history(entry, position))
    return Response({"entries": results, "count": len(results)}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cancel_my_waitlist_entry(request, entry_id):
    """Cancel own waitlist entry (allowed at any time)."""
    try:
        entry = WaitlistEntry.objects.select_related("equipment").get(pk=entry_id, user=request.user)
    except WaitlistEntry.DoesNotExist:
        return Response({"error": "Waitlist entry not found."}, status=status.HTTP_404_NOT_FOUND)

    equipment_name = getattr(entry.equipment, "name", None) or getattr(entry.equipment, "code", "Equipment")
    entry.delete()
    return Response(
        {"message": f"Waitlist booking for {equipment_name} cancelled successfully."},
        status=status.HTTP_200_OK,
    )


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def delete_booking_attempt_log(request, log_id):
    """
    Permanently delete a single booking attempt log entry. Admin only.
    No other user type can delete log entries.
    """
    if request.user.user_type != UserType.ADMIN:
        return Response(
            {"error": "Only admin can delete log entries."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        log = BookingAttemptLog.objects.get(pk=log_id)
    except BookingAttemptLog.DoesNotExist:
        return Response({"error": "Log entry not found."}, status=status.HTTP_404_NOT_FOUND)
    log.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_booking_attempt_log_quota_breakdown(request, log_id):
    """
    Get date-wise quota calculation details for a failed booking attempt (quota check failed).
    Admin and Officer in charge only. OIC can only view breakdown for logs of equipments they manage.
    """
    if not check_operator_permission(request.user):
        return Response(
            {"error": "Only admin and Officer in charge can view quota calculation details."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        log = BookingAttemptLog.objects.select_related("user", "equipment").get(pk=log_id)
    except BookingAttemptLog.DoesNotExist:
        return Response({"error": "Log entry not found."}, status=status.HTTP_404_NOT_FOUND)
    if log.outcome != BookingAttemptOutcome.FAILED:
        return Response(
            {"error": "Quota breakdown is only available for failed attempts."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    failure_reason = (log.failure_reason or "").strip()
    if "quota check failed" not in failure_reason.lower():
        return Response(
            {"error": "Quota breakdown is only available when failure reason is quota check failed."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if "weekly" in failure_reason.lower():
        quota_type = "WEEKLY"
    elif "monthly" in failure_reason.lower():
        quota_type = "MONTHLY"
    else:
        return Response(
            {"error": "Could not determine quota type (weekly/monthly) from failure reason."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    equipment_ids = _get_equipment_ids_for_log_access(request.user)
    if equipment_ids is not None and log.equipment_id not in equipment_ids:
        return Response(
            {"error": "You do not have permission to view quota details for this equipment."},
            status=status.HTTP_403_FORBIDDEN,
        )
    from django.utils import timezone
    reference_date = log.requested_at or timezone.now()
    breakdown = get_quota_breakdown(
        log.user, log.equipment, quota_type, reference_date, failure_reason
    )
    return Response(breakdown, status=status.HTTP_200_OK)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def update_urgent_booking_request(request, request_id):
    """Admin/OIC: PATCH to set status (APPROVED/REJECTED) and admin_notes; DELETE to remove the request.
    For EXPIRED requests only DELETE is allowed. When REJECTED and hold_booking is set, the hold is released (slots freed)."""
    from django.utils import timezone
    if not check_operator_permission(request.user):
        return Response(
            {"error": "Only admin and Officer in charge can update urgent requests."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        urg = UrgentBookingRequest.objects.select_related("hold_booking", "hold_booking__equipment", "equipment").get(pk=int(request_id))
    except (ValueError, UrgentBookingRequest.DoesNotExist):
        return Response({"error": "Urgent request not found."}, status=status.HTTP_404_NOT_FOUND)
    # OIC/operator may only update requests for their managed equipment
    allowed_ids = _get_equipment_ids_for_log_access(request.user)
    if allowed_ids is not None and urg.equipment_id not in allowed_ids:
        return Response(
            {"error": "You do not have permission to update this request (equipment not under your charge)."},
            status=status.HTTP_403_FORBIDDEN,
        )

    if request.method == "DELETE":
        if urg.hold_booking_id and urg.hold_booking and urg.hold_booking.status == BookingStatus.HOLD:
            _release_hold_booking(urg.hold_booking)
        urg.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    if urg.status == UrgentBookingRequestStatus.EXPIRED:
        return Response(
            {"error": "Expired requests cannot be updated. You may delete the request."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    new_status = request.data.get("status", "").strip().upper()
    hold_converted = False
    hold_released = False
    if new_status in (UrgentBookingRequestStatus.APPROVED, UrgentBookingRequestStatus.REJECTED):
        urg.status = new_status
        urg.decided_by = request.user
        urg.decided_at = timezone.now()
    if new_status == UrgentBookingRequestStatus.REJECTED and urg.hold_booking_id:
        _release_hold_booking(urg.hold_booking)
        hold_released = True
    if "admin_notes" in request.data:
        urg.admin_notes = request.data.get("admin_notes") or ""
    # On APPROVED with a hold booking: debit wallet and convert HOLD -> BOOKED
    if new_status == UrgentBookingRequestStatus.APPROVED and urg.hold_booking_id:
        hold_booking = urg.hold_booking
        if hold_booking and hold_booking.status == BookingStatus.HOLD and hold_booking.total_charge and hold_booking.total_charge > 0:
            from iic_booking.users.repositories.wallet_repository import WalletRepository
            booking_target, _ = WalletRepository.get_booking_wallet_target(
                urg.user, getattr(urg.equipment, "internal_department", None)
            )
            if not booking_target:
                return Response(
                    {"error": "User has no wallet for debiting. Cannot approve urgent request with hold booking."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            booking_target.refresh_from_db()
            from iic_booking.users.wallet_credit_facility import subwallet_booking_balance_ok

            ok_hold, hold_err = subwallet_booking_balance_ok(
                booking_target, hold_booking.total_charge, False
            )
            if not ok_hold:
                return Response(
                    {"error": hold_err or "Insufficient wallet balance to confirm hold."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            try:
                with transaction.atomic():
                    from iic_booking.users.wallet_credit_facility import subwallet_minimum_balance_after_debit

                    transaction_description = f"Urgent approval: Booking #{hold_booking.equipment.code} - {hold_booking.equipment.name} (Hold converted)"
                    _vid = (getattr(hold_booking, "virtual_booking_id", None) or "").strip()
                    if _vid:
                        transaction_description += f" | Ref: {_vid}"
                    transaction_description += _student_booking_description_suffix(booking_target, hold_booking.user)
                    booking_target.debit(
                        amount=hold_booking.total_charge,
                        description=transaction_description,
                        related_user=hold_booking.user,
                        minimum_balance_after=subwallet_minimum_balance_after_debit(booking_target),
                    )
                    hold_booking.status = BookingStatus.BOOKED
                    hold_booking.save(update_fields=["status"])
                    create_booking_event(
                        booking=hold_booking,
                        event_type=BookingEventType.STATUS_CHANGED,
                        created_by=request.user,
                        comment="Hold converted to Booked after urgent request approved.",
                        previous_status=BookingStatus.HOLD,
                        new_status=BookingStatus.BOOKED,
                        metadata={"urgent_hold_converted": True},
                        send_notification=True,
                    )
                    hold_converted = True
            except ValueError as e:
                return Response({"error": f"Wallet error: {str(e)}"}, status=status.HTTP_400_BAD_REQUEST)
        elif hold_booking and hold_booking.status == BookingStatus.HOLD and (not hold_booking.total_charge or hold_booking.total_charge == 0):
            with transaction.atomic():
                hold_booking.status = BookingStatus.BOOKED
                hold_booking.save(update_fields=["status"])
                create_booking_event(
                    booking=hold_booking,
                    event_type=BookingEventType.STATUS_CHANGED,
                    created_by=request.user,
                    comment="Hold converted to Booked after urgent request approved (no charge).",
                    previous_status=BookingStatus.HOLD,
                    new_status=BookingStatus.BOOKED,
                    metadata={"urgent_hold_converted": True},
                    send_notification=True,
                )
                hold_converted = True
    urg.save()
    # Email requester when Admin/OIC approves/rejects without a separate hold email (no hold conversion / release).
    try:
        if new_status == UrgentBookingRequestStatus.APPROVED and not hold_converted:
            _link = get_frontend_absolute_url("/my-urgent-requests")
            CommunicationService.send_email(
                recipient=urg.user,
                template="urgent_booking_admin_decision_user_email",
                template_context={
                    "user_name": urg.user.name or urg.user.email,
                    "decision_headline": "Urgent Booking Request Approved by Admin",
                    "decision_body": (
                        "Your urgent booking request has been approved by the administrator. "
                        "Please follow any further instructions shown in the portal."
                    ),
                    "request_id": urg.id,
                    "equipment_name": urg.equipment.name,
                    "equipment_code": urg.equipment.code,
                    "admin_notes": (urg.admin_notes or "").strip() or "—",
                    "link": _link,
                },
                metadata={"urgent_booking_request_id": urg.id},
            )
        elif new_status == UrgentBookingRequestStatus.REJECTED and not hold_released:
            _link = get_frontend_absolute_url("/my-urgent-requests")
            CommunicationService.send_email(
                recipient=urg.user,
                template="urgent_booking_admin_decision_user_email",
                template_context={
                    "user_name": urg.user.name or urg.user.email,
                    "decision_headline": "Urgent Booking Request Rejected by Admin",
                    "decision_body": "Your urgent booking request has been rejected by the administrator.",
                    "request_id": urg.id,
                    "equipment_name": urg.equipment.name,
                    "equipment_code": urg.equipment.code,
                    "admin_notes": (urg.admin_notes or "").strip() or "—",
                    "link": _link,
                },
                metadata={"urgent_booking_request_id": urg.id},
            )
    except Exception as e:
        logger.warning(
            "Failed to send urgent admin decision email for request id=%s: %s",
            urg.id,
            e,
            exc_info=True,
        )
    return Response(
        {"message": "Updated.", "id": urg.id, "status": urg.status},
        status=status.HTTP_200_OK,
    )


def _get_wallet_supervisor_user(request_user):
    """
    Faculty (wallet owner) who should receive urgent REVIEWER_URGENT notifications and may approve
    wallet_approve_urgent_booking_request. Prefer explicit WalletJoinRequest.faculty; then wallet owner.
    """
    from iic_booking.users.models import User
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus
    from iic_booking.users.models.user_type import UserType

    ut = getattr(request_user, "user_type", None) or ""
    if ut in (UserType.STUDENT, UserType.OTHER):
        jr = (
            WalletJoinRequest.objects.filter(
                student=request_user,
                status=WalletJoinRequestStatus.APPROVED,
            )
            .select_related("faculty", "wallet", "wallet__user")
            .order_by("-id")
            .first()
        )
        if jr:
            if jr.faculty_id and jr.faculty_id != request_user.id:
                return jr.faculty
            if jr.wallet and jr.wallet.user_id and jr.wallet.user_id != request_user.id:
                return jr.wallet.user
    wallet = getattr(request_user, "get_accessible_wallet", None) and request_user.get_accessible_wallet()
    if wallet and wallet.user_id and wallet.user_id != request_user.id:
        return User.objects.filter(pk=wallet.user_id).first()
    return None


def _is_wallet_owner_for_urgent_request(user, urg):
    """True if user is the wallet faculty supervisor for the requester (same logic as list + email)."""
    sup = _get_wallet_supervisor_user(urg.user)
    return sup is not None and sup.id == user.id


def _release_hold_booking(hold_booking):
    """Release a HOLD booking: free its slots (AVAILABLE) and set booking status to CANCELLED. No refund (no debit was made)."""
    if not hold_booking or hold_booking.status != BookingStatus.HOLD:
        return
    released_slot_ids = list(hold_booking.daily_slots.values_list("id", flat=True))
    with transaction.atomic():
        hold_booking.daily_slots.update(
            booking=None,
            status=released_slot_status_after_booking_freed(hold_booking.equipment),
        )
        hold_booking.status = BookingStatus.CANCELLED
        hold_booking.save(update_fields=["status"])
        create_booking_event(
            booking=hold_booking,
            event_type=BookingEventType.STATUS_CHANGED,
            created_by=None,
            comment="Hold released (urgent request rejected or expired).",
            previous_status=BookingStatus.HOLD,
            new_status=BookingStatus.CANCELLED,
            metadata={"urgent_hold_released": True},
            send_notification=True,
        )
    try:
        notify_waitlist_slots_available(hold_booking.equipment, preferred_slot_ids=released_slot_ids)
    except Exception as e:
        logger.warning("Failed to notify waitlist after releasing hold for equipment %s: %s", hold_booking.equipment.code, e)


def expire_urgent_hold_requests():
    """Find PENDING urgent requests past the effective expiry (validity_days or slot window - 15 min); release hold and set status to EXPIRED."""
    from django.utils import timezone
    config = UrgentHoldExpiryConfig.objects.first()
    validity_days = 1
    if config and getattr(config, "urgent_booking_validity_days", None) is not None:
        validity_days = config.urgent_booking_validity_days or 1
    if validity_days <= 0:
        return 0
    now = timezone.now()
    to_expire = list(
        UrgentBookingRequest.objects.filter(
            status=UrgentBookingRequestStatus.PENDING,
        ).select_related("hold_booking", "equipment")
    )
    count = 0
    for urg in to_expire:
        effective_expiry = get_effective_expiry_at(urg, validity_days)
        if effective_expiry is None or now < effective_expiry:
            continue
        if urg.hold_booking_id and urg.hold_booking and urg.hold_booking.status == BookingStatus.HOLD:
            _release_hold_booking(urg.hold_booking)
        urg.status = UrgentBookingRequestStatus.EXPIRED
        urg.save(update_fields=["status"])
        count += 1
    return count


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def urgent_hold_expiry_config(request):
    """Admin/OIC: GET/PATCH urgent booking validity period in days (single setting). After this period, PENDING requests expire and hold is released."""
    if not check_operator_permission(request.user):
        return Response(
            {"error": "Only admin and Officer in charge can view or update this config."},
            status=status.HTTP_403_FORBIDDEN,
        )
    config = UrgentHoldExpiryConfig.objects.first()
    if not config:
        config = UrgentHoldExpiryConfig.objects.create(hold_expiry_hours=24, urgent_booking_validity_days=1)

    if request.method == "GET":
        validity_days = getattr(config, "urgent_booking_validity_days", None)
        return Response({
            "urgent_booking_validity_days": validity_days if validity_days is not None and validity_days > 0 else 1,
        }, status=status.HTTP_200_OK)

    validity_days = request.data.get("urgent_booking_validity_days")
    if validity_days is not None:
        try:
            vd = int(validity_days)
            if vd < 1:
                vd = 1
        except (ValueError, TypeError):
            return Response(
                {"error": "urgent_booking_validity_days must be a positive integer (days)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        config.urgent_booking_validity_days = vd
        config.save(update_fields=["urgent_booking_validity_days", "updated_at"])

    validity_days = getattr(config, "urgent_booking_validity_days", None)
    return Response({
        "urgent_booking_validity_days": validity_days if validity_days is not None and validity_days > 0 else 1,
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def wallet_approve_urgent_booking_request(request, request_id):
    """
    Deprecated: supervisor approval is no longer part of urgent request workflow.
    """
    return Response(
        {"error": "Supervisor approval is no longer required. Requests are reviewed directly by Admin/OIC."},
        status=status.HTTP_410_GONE,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_urgent_requests_pending_wallet_approval(request):
    """
    Deprecated: supervisor approval queue is no longer used.
    """
    return Response(
        {"urgent_requests": [], "total_count": 0, "limit": 0, "offset": 0},
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_urgent_requests_wallet(request):
    """
    Deprecated: supervisor urgent request list is no longer used.
    """
    return Response(
        {"urgent_requests": [], "total_count": 0, "limit": 0, "offset": 0},
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_urgent_request_evidence(request, request_id):
    """
    Download/view documentary evidence for an urgent request (REVIEWER_URGENT).
    Allowed: admin/OIC only.
    Streams the file through Django to avoid CORS when frontend fetches from S3.
    """
    try:
        urg = UrgentBookingRequest.objects.select_related("user").get(pk=int(request_id))
    except (ValueError, UrgentBookingRequest.DoesNotExist):
        return Response({"error": "Urgent request not found."}, status=status.HTTP_404_NOT_FOUND)
    if not urg.evidence_file or not urg.evidence_file.name:
        return Response({"error": "No evidence file for this request."}, status=status.HTTP_404_NOT_FOUND)
    if not check_operator_permission(request.user):
        return Response(
            {"error": "You are not allowed to view this attachment."},
            status=status.HTTP_403_FORBIDDEN,
        )
    file_name = urg.evidence_file.name
    if not default_storage.exists(file_name):
        logging.warning("Urgent request evidence file not found in storage: %s", file_name)
        return Response(
            {"error": "File not found in storage."},
            status=status.HTTP_404_NOT_FOUND,
        )
    try:
        with default_storage.open(file_name, "rb") as fh:
            content = fh.read()
    except Exception as e:
        logging.exception("Error opening urgent request evidence file %s: %s", file_name, e)
        return Response(
            {"error": "File not available."},
            status=status.HTTP_404_NOT_FOUND,
        )
    content_type, _ = mimetypes.guess_type(urg.evidence_original_name or file_name)
    if not content_type:
        content_type = "application/octet-stream"
    response = HttpResponse(content, content_type=content_type)
    # attachment: browsers reliably save PDFs; inline + blob URLs often fail for supervisors opening from SPA
    if urg.evidence_original_name:
        safe_name = urg.evidence_original_name.replace('"', "").replace("\r", "").replace("\n", "")
        response["Content-Disposition"] = f'attachment; filename="{safe_name}"'
    else:
        ext = ".pdf" if content_type == "application/pdf" else ""
        response["Content-Disposition"] = f'attachment; filename="urgent-evidence-{urg.pk}{ext}"'
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


def check_operator_permission(user):
    """Check if user can manage departmental bookings (admin / staff with bookings.manage).

    Args:
        user: User instance

    Returns:
        bool: True if user may list/filter others' bookings as staff, False otherwise
    """
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if user.user_type == UserType.ADMIN:
        return True
    if user.user_type in [UserType.OPERATOR, UserType.MANAGER, UserType.DEPT_ADMIN]:
        from iic_booking.users.rbac import user_has_permission

        return user_has_permission(user, "bookings.manage")
    return False


def _user_is_accounts_finance_user(user) -> bool:
    return bool(user and getattr(user, "user_type", None) == UserType.FINANCE)


def _booking_in_accounts_finance_list_scope(booking) -> bool:
    """External bookings only, status BOOKED or COMPLETED (Accounts In Charge my-bookings scope)."""
    if not booking:
        return False
    snap = (getattr(booking, "user_type_snapshot", None) or "").strip()
    if snap not in UserType.get_external_user_codes():
        return False
    return booking.status in (BookingStatus.BOOKED, BookingStatus.COMPLETED)


def _finance_user_can_access_booking_for_workflow(user, booking) -> bool:
    """Finance may view/act on sample trace and return shipping for in-scope external bookings."""
    if not _user_is_accounts_finance_user(user) or not booking:
        return False
    return _booking_in_accounts_finance_list_scope(booking)


def _get_equipment_ids_for_log_access(user):
    """
    For list_booking_attempt_logs: admin sees all; officer in charge (manager) sees only
    equipments for which they are marked as manager or temporary OIC (until resume_at).
    Returns None for no filter (admin), else list of equipment_id.
    """
    if user.user_type == UserType.ADMIN:
        return None
    if user.user_type == UserType.MANAGER:
        return get_equipment_ids_managed_by_oic(user.id)
    from .models import EquipmentOperator
    if user.user_type == UserType.OPERATOR:
        base_ids = set(EquipmentOperator.objects.filter(operator=user).values_list("equipment_id", flat=True))
        now_ts = timezone.now()
        # Exclude equipment where the user is the PRIMARY operator and there is an active coverage window.
        primary_ids = set(
            EquipmentOperator.objects.filter(operator=user, role=EquipmentOperator.Role.PRIMARY).values_list("equipment_id", flat=True)
        )
        covered_primary_ids = set(
            EquipmentOperatorCoverage.objects.filter(
                primary_operator=user,
                equipment_id__in=primary_ids,
                starts_at__lte=now_ts,
                ends_at__gte=now_ts,
            )
            .filter(Q(ended_early_at__isnull=True) | Q(ended_early_at__gt=now_ts))
            .values_list("equipment_id", flat=True)
        )
        # Include equipment where the user is acting operator during an active coverage window.
        acting_ids = set(
            EquipmentOperatorCoverage.objects.filter(
                acting_operator=user,
                starts_at__lte=now_ts,
                ends_at__gte=now_ts,
            )
            .filter(Q(ended_early_at__isnull=True) | Q(ended_early_at__gt=now_ts))
            .values_list("equipment_id", flat=True)
        )
        return list((base_ids - covered_primary_ids) | acting_ids)
    return []


def _get_input_field_key_to_label_map(equipment):
    """Return a dict mapping field_key -> field_label for the equipment's input fields."""
    from .models import DynamicInputField
    if not equipment:
        return {}
    return dict(
        DynamicInputField.objects.filter(equipment=equipment).values_list("field_key", "field_label")
    )


def _get_additional_info_from_request(request, equipment=None):
    """Build additional_info dict from request.data for booking attempt log.
    When equipment is provided, input_values keys are converted to field labels (FIELD_LABEL) for display.
    """
    data = getattr(request, "data", None)
    if not data:
        return None
    info = {}
    if "input_values" in data:
        val = data.get("input_values")
        input_values = val if isinstance(val, dict) else {}
        if equipment and input_values:
            key_to_label = _get_input_field_key_to_label_map(equipment)
            input_values_with_labels = {}
            for key, value in input_values.items():
                # Store using field label for display; fallback to key if not in map
                label = key_to_label.get(key, key)
                input_values_with_labels[label] = value
            info["input_values"] = input_values_with_labels
        else:
            info["input_values"] = input_values
    if "selected_parameters" in data:
        info["selected_parameters"] = data.get("selected_parameters")
    return info if info else None


def _student_booking_description_suffix(wallet_target, booking_user):
    """When the transaction is on a Supervisor's wallet but the booking was made by a student, return a suffix for the description so the Supervisor sees it in their account."""
    if not wallet_target or not booking_user:
        return ""
    wallet_owner = getattr(getattr(wallet_target, "wallet", None), "user", None)
    if not wallet_owner or wallet_owner.id == booking_user.id:
        return ""
    student_label = (booking_user.name or booking_user.email or "").strip() or f"User #{booking_user.id}"
    return f" - Student: {student_label}"


def _get_slots_duration_minutes(slot_ids):
    """Return total duration in minutes for the given slot IDs (from DailySlot start/end). Used to know required duration when finding alternatives."""
    if not slot_ids:
        return 0
    slots = DailySlot.objects.filter(id__in=slot_ids).values_list("start_datetime", "end_datetime")
    total_seconds = 0
    for start, end in slots:
        if start and end:
            total_seconds += (end - start).total_seconds()
    return int(total_seconds / 60)


def _get_reduce_key_for_single_slot(profile_type):
    """
    Return the input field key to reduce to 1 when falling back to single-slot booking.
    Hour-based: B (number of slots). Sample-based, Sample+Element, Multi-Parameter: A.
    """
    if profile_type == EquipmentProfileType.HOUR:
        return "B"
    if profile_type in (
        EquipmentProfileType.SAMPLE,
        EquipmentProfileType.SAMPLE_ELEMENT,
        EquipmentProfileType.MULTI_PARAM,
    ):
        return "A"
    return None


def _try_reduce_to_single_slot(equipment, charge_profile_with_type, input_values, slot_duration_minutes):
    """
    Try to reduce the requirement to one slot by setting the profile-specific key (A or B) to 1.
    Returns (reduced_input_values, single_slot_time_minutes) if new time <= slot_duration_minutes, else None.
    """
    profile_type = getattr(equipment, "profile_type", None)
    key = _get_reduce_key_for_single_slot(profile_type)
    if not key or slot_duration_minutes <= 0:
        return None
    reduced = dict(input_values)
    reduced[key] = 1
    try:
        new_time = TimeCalculationEngine.calculate_time(
            charge_profile_with_type,
            reduced,
            slot_duration_minutes=slot_duration_minutes,
        )
    except Exception:
        return None
    if new_time <= 0 or new_time > slot_duration_minutes:
        return None
    return (reduced, new_time)


def _find_one_available_slot_in_window(
    equipment, user_type, is_admin, visible_week_start=None, visible_week_end=None, booking_user=None
):
    """
    Find any one available slot for the equipment within the visible week.
    Returns (slot_id, DailySlot) or None.
    """
    base_filter = {"slot_master__equipment": equipment, "status": SlotStatus.AVAILABLE}
    if not is_admin:
        base_filter["reserved_for_external"] = UserType.is_external_user(user_type)
    if visible_week_start is not None:
        base_filter["date__gte"] = visible_week_start
    if visible_week_end is not None:
        base_filter["date__lte"] = visible_week_end
    qs = DailySlot.objects.filter(**base_filter).order_by("start_datetime")
    if booking_user is not None and not is_admin:
        qs = filter_queryset_for_home_department(
            qs,
            user=booking_user,
            equipment=equipment,
            is_admin=False,
            is_external=UserType.is_external_user(user_type),
        )
    candidates = list(qs[:40])
    time_from = getattr(equipment, "weekly_view_time_from", None)
    time_to = getattr(equipment, "weekly_view_time_to", None)
    for slot in candidates:
        if (time_from is not None or time_to is not None) and slot.start_datetime and slot.end_datetime:
            from django.utils import timezone as tz
            start_local = tz.localtime(slot.start_datetime) if tz.is_aware(slot.start_datetime) else slot.start_datetime
            end_local = tz.localtime(slot.end_datetime) if tz.is_aware(slot.end_datetime) else slot.end_datetime
            st, et = start_local.time(), end_local.time()
            if time_from is not None and st < time_from:
                continue
            if time_to is not None and et > time_to:
                continue
        return (slot.id, slot)
    return None


def _find_alternative_available_slots(
    equipment, required_minutes, user_type, is_admin, visible_week_start=None, visible_week_end=None, booking_user=None
):
    """
    Find any available slots for the equipment that together cover at least required_minutes.
    Uses same rules as slot_ids path: AVAILABLE, reserved_for_external for external, slot window.
    When visible_week_start/visible_week_end (date) are provided, only slots within that week are considered.
    Returns (slot_ids, daily_slots_queryset) or None if not enough slots.
    """
    base_filter = {"slot_master__equipment": equipment, "status": SlotStatus.AVAILABLE}
    if not is_admin:
        base_filter["reserved_for_external"] = UserType.is_external_user(user_type)
    if visible_week_start is not None:
        base_filter["date__gte"] = visible_week_start
    if visible_week_end is not None:
        base_filter["date__lte"] = visible_week_end
    qs = DailySlot.objects.filter(**base_filter).order_by("start_datetime")
    if booking_user is not None and not is_admin:
        qs = filter_queryset_for_home_department(
            qs,
            user=booking_user,
            equipment=equipment,
            is_admin=False,
            is_external=UserType.is_external_user(user_type),
        )
    time_from = getattr(equipment, "weekly_view_time_from", None)
    time_to = getattr(equipment, "weekly_view_time_to", None)
    collected = []
    total_minutes = 0
    for slot in qs:
        if time_from is not None or time_to is not None:
            if not slot.start_datetime or not slot.end_datetime:
                continue
            from django.utils import timezone as tz
            start_local = tz.localtime(slot.start_datetime) if tz.is_aware(slot.start_datetime) else slot.start_datetime
            end_local = tz.localtime(slot.end_datetime) if tz.is_aware(slot.end_datetime) else slot.end_datetime
            st, et = start_local.time(), end_local.time()
            if time_from is not None and st < time_from:
                continue
            if time_to is not None and et > time_to:
                continue
        delta = (slot.end_datetime - slot.start_datetime) if slot.start_datetime and slot.end_datetime else None
        if not delta:
            continue
        mins = int(delta.total_seconds() / 60)
        collected.append(slot)
        total_minutes += mins
        if total_minutes >= required_minutes:
            break
    if not collected or total_minutes < required_minutes:
        return None
    slot_ids = [s.id for s in collected]
    return (slot_ids, DailySlot.objects.filter(id__in=slot_ids).order_by("start_datetime"))


def _serialize_booked_daily_slots_for_booking_response(slots):
    """
    Minimal JSON for successful book-equipment responses. Avoids full DailySlotSerializer
    (many SerializerMethodFields). Callers must use select_related("slot_master", "slot_master__equipment", "booking").
    """
    rows = []
    for s in slots:
        sm = s.slot_master
        b = s.booking
        eq = sm.equipment if sm is not None else None
        slot_open = None
        if sm is not None and getattr(sm, "open_time", None) is not None:
            slot_open = sm.open_time.strftime("%H:%M:%S")
        disp_booking = booking_display_id_for_email(b) if b else None
        rows.append(
            {
                "id": s.id,
                "slot_master": sm.id if sm else None,
                "slot_number": sm.slot_number if sm else None,
                "slot_name": sm.slot_name if sm else None,
                "equipment_code": eq.code if eq else None,
                "date": s.date.isoformat() if hasattr(s.date, "isoformat") else str(s.date),
                "slot_open_time": slot_open,
                "start_datetime": s.start_datetime.isoformat() if s.start_datetime else None,
                "end_datetime": s.end_datetime.isoformat() if s.end_datetime else None,
                "status": s.status,
                "status_display": s.get_status_display(),
                "blocked_label": s.blocked_label,
                "reserved_for_external": bool(getattr(s, "reserved_for_external", False)),
                "booking": b.booking_id if b else None,
                "booking_id": disp_booking,
                "real_booking_id": b.booking_id if b else None,
                "booking_status": b.status if b else None,
                "booking_status_display": b.get_status_display() if b else None,
                "available_for_external": bool(
                    getattr(s, "reserved_for_external", False) and s.status == SlotStatus.AVAILABLE
                ),
                "created_at": s.created_at.isoformat() if s.created_at else None,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None,
            }
        )
    return rows


def _create_booking_attempt_log(
    request,
    equipment,
    outcome,
    failure_reason=None,
    booking_id=None,
    slots_requested=1,
    number_of_samples=1,
    duration_minutes=None,
    additional_info=None,
):
    """Create a BookingAttemptLog entry (success or failure). Uses request.user as the attempt user."""
    try:
        BookingAttemptLog.objects.create(
            user=request.user,
            equipment=equipment,
            outcome=outcome,
            failure_reason=(failure_reason or "")[:2000],
            number_of_samples=max(1, int(number_of_samples)),
            slots_requested=max(1, int(slots_requested)),
            duration_minutes=int(duration_minutes) if duration_minutes is not None else None,
            booking_id=int(booking_id) if booking_id is not None else None,
            additional_info=additional_info,
        )
    except Exception as e:
        logger.warning("Failed to create booking attempt log: %s", e)


def _schedule_unsuccessful_booking_waitlist_email(
    booking_user, equipment, position: int, failure_reason: str = ""
) -> None:
    """Queue waitlist email without blocking the book API (Celery/Redis retries must not stall HTTP)."""
    uid = int(booking_user.pk)
    eid = int(equipment.pk)
    pos = int(position)
    reason = failure_reason or ""

    def _run() -> None:
        from django.contrib.auth import get_user_model
        from django.db import close_old_connections

        from .models import Equipment
        from .waitlist import send_unsuccessful_booking_waitlist_email

        close_old_connections()
        try:
            try:
                from iic_booking.equipment.tasks import send_unsuccessful_booking_waitlist_email_task

                send_unsuccessful_booking_waitlist_email_task.delay(uid, eid, pos, reason)
            except Exception:
                logger.warning(
                    "Failed to queue waitlist email for user=%s equipment=%s; sending in thread",
                    uid,
                    eid,
                    exc_info=True,
                )
                User = get_user_model()
                user = User.objects.get(pk=uid)
                eq = Equipment.objects.get(pk=eid)
                send_unsuccessful_booking_waitlist_email(user, eq, pos, failure_reason=reason)
        except Exception:
            logger.exception("Waitlist unsuccessful email failed user_id=%s", uid)
        finally:
            close_old_connections()

    threading.Thread(
        target=_run,
        daemon=True,
        name=f"waitlist-booking-email-{uid}-{eid}",
    ).start()


def _enrich_failed_booking_response(
    equipment,
    booking_user,
    error_message: str,
    waitlist_on_failure: bool = True,
    *,
    slot_unavailable_failure: bool = False,
) -> dict:
    """
    For failed booking: add user to waitlist if enabled, include waitlist_position in response,
    and schedule unsuccessful+waitlist email when added (email does not block the API response).

    If slot_unavailable_failure is True and current time is within the slot-window reference instant
    plus PEAK_SLOT_WAITLIST_AFTER_REFERENCE_MINUTES, waitlist is applied regardless of waitlist_on_failure.
    """
    if slot_unavailable_failure and is_slot_window_peak_waitlist_period(equipment):
        waitlist_on_failure = True
    if not waitlist_on_failure:
        return {"error": error_message}

    payload = {"error": error_message}
    try:
        added, position = add_user_to_waitlist(equipment, booking_user)
        if position is not None:
            payload["waitlist_position"] = position
            payload["waitlist_code"] = f"WL{position}"
            from .models import WaitlistEntry

            wl_entry = (
                WaitlistEntry.objects.filter(equipment=equipment, user=booking_user)
                .only("created_at")
                .first()
            )
            payload["virtual_booking_id"] = waitlist_virtual_booking_id(
                getattr(equipment, "code", "") or "",
                position,
                department_code=Booking.department_code_for_virtual_id(equipment),
                created_at=wl_entry.created_at if wl_entry else None,
            )
            payload["error"] = f"Booking Waitlisted. Current position in queue: WL{position}."
            if added:
                _schedule_unsuccessful_booking_waitlist_email(
                    booking_user, equipment, position, failure_reason=error_message
                )
        else:
            # Waitlist enabled but queue full -> return explicit "No More Rooms" hint
            depth = getattr(equipment, "waitlist_queue_depth", None) or 0
            if depth > 0:
                from .models import WaitlistEntry
                current_count = WaitlistEntry.objects.filter(equipment=equipment).count()
                if current_count >= depth:
                    # Keep error message stable; frontend uses waitlist_full / absence of waitlist_position
                    # to show either "waitlisted" message or "all slots occupied" message.
                    payload["error"] = error_message
                    payload["waitlist_full"] = True
    except Exception as e:
        logger.warning("Waitlist add/enrich failed: %s", e)
    return payload


def _require_admin_panel(request):
    """Return True if request.user is admin-panel user (admin, manager, operator, finance) or staff."""
    if not request.user or not request.user.is_authenticated:
        return False
    if getattr(request.user, "is_staff", False):
        return True
    return getattr(request.user, "user_type", None) in UserType.get_admin_panel_codes()


def _supply_chain_role_permitted(user, role: str) -> bool:
    """When roles are assigned in DB, restrict step to those users (Admin always allowed)."""
    if not user or not user.is_authenticated:
        return False
    if getattr(user, "user_type", None) == UserType.ADMIN:
        return True
    if not UserEquipmentSupplyChainRole.objects.filter(role=role).exists():
        return True
    return UserEquipmentSupplyChainRole.objects.filter(user=user, role=role).exists()


def _user_can_view_equipment_lifecycle(request, equipment: Equipment) -> bool:
    if not _require_admin_panel(request):
        return False
    if request.user.user_type in (UserType.ADMIN, UserType.FINANCE):
        return True
    if InventoryRequest.is_user_authorized_for_equipment(request.user, equipment):
        return True
    if EquipmentOperator.objects.filter(equipment=equipment, operator=request.user).exists():
        return True
    return False


def _user_can_edit_equipment_lifecycle(request, equipment: Equipment) -> bool:
    if not _require_admin_panel(request):
        return False
    if request.user.user_type == UserType.ADMIN:
        return True
    return InventoryRequest.is_user_authorized_for_equipment(request.user, equipment)


def _user_can_record_equipment_expense(request, equipment: Equipment) -> bool:
    if not _require_admin_panel(request):
        return False
    if request.user.user_type == UserType.ADMIN:
        return True
    if request.user.user_type == UserType.FINANCE:
        return True
    if InventoryRequest.is_user_authorized_for_equipment(request.user, equipment):
        return True
    if EquipmentOperator.objects.filter(equipment=equipment, operator=request.user).exists():
        return True
    return False


def _user_can_initiate_write_off(request, equipment: Equipment) -> bool:
    if not _require_admin_panel(request):
        return False
    if request.user.user_type == UserType.ADMIN:
        return True
    return InventoryRequest.is_user_authorized_for_equipment(request.user, equipment)


def _user_can_procurement_oic_act(request, equipment: Equipment) -> bool:
    if not _require_admin_panel(request):
        return False
    if request.user.user_type == UserType.ADMIN:
        return True
    return InventoryRequest.is_user_authorized_for_equipment(request.user, equipment)


def _ensure_procurement_office_seen_expense(procurement: ProcurementRequest, created_by: User) -> None:
    """Create one PROCUREMENT_LINKED expense when procurement closes; no-op if already linked."""
    if EquipmentExpense.objects.filter(procurement_request=procurement).exists():
        return
    lines = list(procurement.lines.all())
    classifications = {ln.classification for ln in lines if getattr(ln, "classification", None)}
    classification = ""
    if len(classifications) == 1:
        classification = next(iter(classifications))
    parts = [f"Auto-logged when procurement {procurement.request_no} reached office seen (completed)."]
    for ln in lines[:20]:
        label = (ln.office_corrected_name or ln.manual_item_name or "").strip() or "Item"
        parts.append(f"- {label}: qty {ln.quantity}, est. {ln.tentative_total_cost} [{ln.classification}]")
    expense_date = (procurement.purchase_completed_at or timezone.now()).date()
    EquipmentExpense.objects.create(
        equipment=procurement.equipment,
        expense_type=EquipmentExpenseType.PROCUREMENT_LINKED,
        classification=classification,
        amount=procurement.total_estimated_cost or Decimal("0"),
        expense_date=expense_date,
        description="\n".join(parts)[:4000],
        procurement_request=procurement,
        created_by=created_by,
    )


def _lifecycle_accessible_equipment_ids(user) -> set[int]:
    if not user or not user.is_authenticated:
        return set()
    ut = getattr(user, "user_type", None)
    if ut in (UserType.ADMIN, UserType.FINANCE):
        return set(Equipment.objects.values_list("equipment_id", flat=True))
    ids = set(get_equipment_ids_managed_by_oic(user.id))
    now_ts = timezone.now()
    delegated_ids = EquipmentTemporaryOIC.objects.filter(
        temporary_oic=user,
        resume_at__gt=now_ts,
    ).values_list("equipment_id", flat=True)
    ids.update(delegated_ids)
    ids.update(EquipmentOperator.objects.filter(operator=user).values_list("equipment_id", flat=True))
    return ids


def _upsert_equipment_booking_requester_group_member(equipment: Equipment, booking_user) -> None:
    """
    Ensure the per-equipment "booking requesters" group exists and add booking_user to it.
    This is idempotent.
    """
    try:
        from iic_booking.equipment.models import EquipmentUserGroup, EquipmentUserGroupPurpose
        from iic_booking.users.models.user_group import UserGroup, UserGroupMember
    except Exception:
        return

    if not equipment or not getattr(equipment, "equipment_id", None) or not booking_user:
        return
    email = (getattr(booking_user, "email", "") or "").strip()
    if not email:
        return

    # Keep code <= 50 chars (UserGroup.code max_length=50)
    group_code = f"EQ{equipment.equipment_id}_BOOKREQ"
    group_name = f"{getattr(equipment, 'code', '') or 'Equipment'} - Booking Users"

    user_group, _ = UserGroup.objects.get_or_create(
        code=group_code,
        defaults={
            "name": group_name[:255],
            "description": f"Auto-managed: users who requested booking for {getattr(equipment, 'code', '') or 'equipment'}",
        },
    )
    EquipmentUserGroup.objects.get_or_create(
        equipment=equipment,
        purpose=EquipmentUserGroupPurpose.BOOKING_REQUESTERS,
        defaults={"user_group": user_group},
    )
    # If an old link exists but points to a different UserGroup, keep existing (do not rewrite).
    UserGroupMember.objects.get_or_create(user_group=user_group, user=booking_user)


def _schedule_equipment_booking_requester_group_upsert(equipment_pk: int, user_pk: int) -> None:
    """Defer UserGroup membership work until after commit — keeps row-lock window shorter."""
    import threading

    def _run():
        from django.db import close_old_connections

        close_old_connections()
        try:
            from django.contrib.auth import get_user_model

            eq = Equipment.objects.get(pk=equipment_pk)
            user = get_user_model().objects.get(pk=user_pk)
            _upsert_equipment_booking_requester_group_member(eq, user)
        except Exception:
            logger.debug(
                "equipment booking requester group upsert skipped (eq=%s user=%s)",
                equipment_pk,
                user_pk,
                exc_info=True,
            )
        finally:
            close_old_connections()

    def _start():
        try:
            threading.Thread(
                target=_run,
                name=f"booking-requester-group-{equipment_pk}-{user_pk}",
                daemon=True,
            ).start()
        except Exception:
            logger.exception(
                "Failed to start requester group upsert thread eq=%s user=%s",
                equipment_pk,
                user_pk,
            )

    transaction.on_commit(_start)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def bulk_email_recipients(request):
    """
    Admin only. Accept slot_ids; return unique recipients (email, name) for those slots that are BOOKED
    (excluding COMPLETED bookings). Used for bulk email to slot users.
    """
    if not _require_admin_panel(request):
        return Response(
            {"error": "Only admin-panel users can access this."},
            status=status.HTTP_403_FORBIDDEN,
        )
    slot_ids = request.data.get("slot_ids") or []
    if not isinstance(slot_ids, list):
        return Response(
            {"error": "slot_ids must be a list of slot IDs."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        slot_ids = [int(x) for x in slot_ids]
    except (ValueError, TypeError):
        return Response(
            {"error": "Each slot_id must be an integer."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    slots = (
        DailySlot.objects.filter(
            id__in=slot_ids,
            status=SlotStatus.BOOKED,
            booking__isnull=False,
        )
        .exclude(booking__status=BookingStatus.COMPLETED)
        .select_related("booking", "booking__user")
    )
    seen = set()
    recipients = []
    for slot in slots:
        if not slot.booking or not slot.booking.user:
            continue
        email = (slot.booking.user.email or "").strip()
        if not email or email in seen:
            continue
        seen.add(email)
        recipients.append({
            "email": email,
            "name": slot.booking.user.name or slot.booking.user.email or email,
        })
    return Response({"recipients": recipients}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def send_bulk_email(request):
    """
    Admin only. Send the same email to multiple recipients. Body: recipient_emails (list), subject, body (plain text).
    Sends one email per recipient (recipients are not visible to each other).
    """
    if not _require_admin_panel(request):
        return Response(
            {"error": "Only admin-panel users can access this."},
            status=status.HTTP_403_FORBIDDEN,
        )
    from django.core.mail import send_mail
    from django.conf import settings

    recipient_emails = request.data.get("recipient_emails") or []
    subject = (request.data.get("subject") or "").strip()
    body = (request.data.get("body") or "").strip()

    if not isinstance(recipient_emails, list):
        return Response(
            {"error": "recipient_emails must be a list."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    recipient_emails = [str(e).strip() for e in recipient_emails if str(e).strip()]
    if not recipient_emails:
        return Response(
            {"error": "At least one recipient email is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not subject:
        return Response(
            {"error": "Subject is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not body:
        return Response(
            {"error": "Body is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    failed = []
    sent = 0
    from iic_booking.users.test_accounts import redirect_email_address

    for email in recipient_emails:
        try:
            delivery_email, per_subject = redirect_email_address(email, subject=subject)
            send_mail(
                subject=per_subject or subject,
                message=body,
                from_email=settings.DEFAULT_FROM_EMAIL,
                recipient_list=[delivery_email],
                fail_silently=False,
            )
            sent += 1
        except Exception as e:
            logger.exception("Bulk email failed for %s", email)
            failed.append({"email": email, "error": str(e)})

    return Response(
        {
            "message": f"Email sent to {sent} recipient(s).",
            "sent_count": sent,
            "failed_count": len(failed),
            "failed": failed if failed else None,
        },
        status=status.HTTP_200_OK,
    )


def _send_completion_email_with_attachments(booking, result_files, context_extra=None):
    """Send booking completed email to the booking user with result files as attachments."""
    from django.core.mail import EmailMultiAlternatives
    from django.conf import settings
    from iic_booking.communication.service import CommunicationService
    from iic_booking.communication.models import CommunicationTemplate

    user = booking.user
    equipment = booking.equipment
    template_code = "booking_completed_email"
    template_obj = CommunicationService.get_template(
        template=template_code,
        communication_type=CommunicationTemplate.CommunicationType.EMAIL,
    )
    if not template_obj:
        logger.warning(f"Template {template_code} not found; sending completion email without template.")
        return

    sample_notice_ctx = _build_sample_notice_context(booking)
    context = {
        "user_name": user.name or user.email,
        "user_email": user.email,
        "booking_id": booking_display_id_for_email(booking),
        "equipment_name": equipment.name,
        "equipment_code": equipment.code,
        "new_status": "Completed",
        "total_charge": str(booking.total_charge),
        "total_time_minutes": str(booking.total_time_minutes),
        "sample_collection_notice": sample_notice_ctx["sample_collection_notice"],
        "sample_collection_deadline_hours": sample_notice_ctx["sample_collection_deadline_hours"],
        "is_external_user": sample_notice_ctx["is_external_user"],
        "sample_preserve_yes_url": sample_notice_ctx["sample_preserve_yes_url"],
        "sample_preserve_no_url": sample_notice_ctx["sample_preserve_no_url"],
    }
    daily_slots = booking.daily_slots.all().order_by('start_datetime')
    if daily_slots.exists():
        context["start_time"] = daily_slots.first().start_datetime.strftime("%Y-%m-%d %H:%M:%S")
        context["end_time"] = daily_slots.last().end_datetime.strftime("%Y-%m-%d %H:%M:%S")
    else:
        context["start_time"] = ""
        context["end_time"] = ""
    if context_extra:
        context.update(context_extra)

    from .booking_events import (
        apply_equipment_completion_email_extra_to_context,
        append_completion_email_extra_html,
        append_completion_email_extra_plaintext,
    )

    apply_equipment_completion_email_extra_to_context(context, equipment)

    rendered = CommunicationService.render_template(template_obj, context=context)
    subject = rendered.get("subject", "Booking completed")
    message = rendered.get("message", "Your booking has been completed.")
    html_message = rendered.get("html_message", "")
    message = _append_sample_notice_plaintext(message, sample_notice_ctx)
    html_message = _append_sample_notice_html(html_message, sample_notice_ctx)
    message = append_completion_email_extra_plaintext(message, equipment)
    html_message = append_completion_email_extra_html(html_message, equipment)

    from iic_booking.users.test_accounts import redirect_email_for_user

    delivery_email, subject = redirect_email_for_user(
        user, original_email=user.email, subject=subject
    )

    email = EmailMultiAlternatives(
        subject=subject,
        body=message,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[delivery_email],
    )
    if html_message:
        email.attach_alternative(html_message, "text/html")
    for bf in result_files:
        if bf.file:
            try:
                bf.file.open("rb")
                content = bf.file.read()
                bf.file.close()
                email.attach(bf.original_name or bf.file.name, content)
            except Exception as e:
                logger.warning(f"Could not attach result file {bf.pk}: {e}")
    email.send(fail_silently=False)


def _try_complete_booking_on_sample_analyzed(booking, acting_user) -> bool:
    """
    When staff sets Sample Lifecycle to Analyzed (COMPLETED trace), mirror Actions → Complete for PENDING/BOOKED:
    set booking COMPLETED, completed_at, booking event, and send the standard completion email (no result attachments).
    """
    with transaction.atomic():
        locked = Booking.objects.select_for_update().get(booking_id=booking.booking_id)
        if locked.status not in (BookingStatus.PENDING, BookingStatus.BOOKED):
            return False
        previous_status = locked.status
        locked.status = BookingStatus.COMPLETED
        locked.completed_at = timezone.now()
        locked.save()
        create_booking_event(
            booking=locked,
            event_type=BookingEventType.COMPLETED,
            previous_status=previous_status,
            new_status=BookingStatus.COMPLETED,
            comment="Booking marked as completed (sample lifecycle: Analyzed).",
            metadata={"via_sample_trace_analyzed": True},
            created_by=acting_user,
            send_notification=False,
        )
    booking.refresh_from_db()
    try:
        _send_completion_email_with_attachments(booking, [])
    except Exception:
        logger.exception(
            "Completion email failed after auto-complete from sample Analyzed for booking %s",
            booking.booking_id,
        )
    return True


def _build_sample_notice_context(booking):
    """Build common sample notice context for completion/results emails."""
    user = booking.user
    is_external = UserType.is_external_user(getattr(user, "user_type", None))
    booking_display_id = booking_display_id_for_email(booking) or str(booking.booking_id)
    yes_url = get_frontend_absolute_url(
        f"/my-bookings?booking={booking_display_id}&sample_preservation=YES"
    )
    no_url = get_frontend_absolute_url(
        f"/my-bookings?booking={booking_display_id}&sample_preservation=NO"
    )
    notice = (
        "Please collect the analyzed sample within 24 hours from this email; "
        "otherwise it will be discarded."
    )
    return {
        "sample_collection_deadline_hours": 24,
        "sample_collection_notice": notice,
        "is_external_user": is_external,
        "sample_preserve_yes_url": yes_url,
        "sample_preserve_no_url": no_url,
    }


def _append_sample_notice_plaintext(message, sample_notice_ctx):
    """Append sample collection notice and external-user action links to plain text email."""
    if not message:
        message = ""
    lines = ["", sample_notice_ctx["sample_collection_notice"]]
    if sample_notice_ctx["is_external_user"]:
        lines.extend(
            [
                "",
                "External users: confirm if the sample should be preserved and sent back",
                "(courier charges apply) by clicking:",
                f"Yes: {sample_notice_ctx['sample_preserve_yes_url']}",
                f"No: {sample_notice_ctx['sample_preserve_no_url']}",
            ]
        )
    return message + "\n" + "\n".join(lines)


def _append_sample_notice_html(html_message, sample_notice_ctx):
    """Append sample collection notice and external-user action buttons to HTML email."""
    notice_html = (
        "<hr style='margin:16px 0;border:none;border-top:1px solid #ddd;'/>"
        f"<p><strong>{html.escape(sample_notice_ctx['sample_collection_notice'])}</strong></p>"
    )
    if sample_notice_ctx["is_external_user"]:
        yes_url = html.escape(sample_notice_ctx["sample_preserve_yes_url"], quote=True)
        no_url = html.escape(sample_notice_ctx["sample_preserve_no_url"], quote=True)
        notice_html += (
            "<p>External users: please confirm whether the sample should be preserved and sent back "
            "(courier charges apply):</p>"
            f"<p><a href='{yes_url}' style='display:inline-block;padding:8px 14px;background:#198754;color:#fff;text-decoration:none;border-radius:4px;margin-right:8px;'>Yes</a>"
            f"<a href='{no_url}' style='display:inline-block;padding:8px 14px;background:#dc3545;color:#fff;text-decoration:none;border-radius:4px;'>No</a></p>"
        )
    if html_message:
        return html_message + notice_html
    return f"<html><body>{notice_html}</body></html>"


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def complete_booking(request, booking_id):
    """Mark a booking as completed. Optionally accept result files (multipart); they are saved and sent to the user email with the completion message.

    Request: multipart/form-data with optional "results" or "results[]" file fields.
    Returns: message, booking, uploaded_files (list of original filenames).
    """
    if not check_operator_permission(request.user):
        return Response(
            {"error": "You don't have permission to perform this action. Only operators, managers, and admins can complete bookings."},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        booking = Booking.objects.get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response(
            {"error": "Booking not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if booking.status not in [BookingStatus.PENDING, BookingStatus.BOOKED]:
        return Response(
            {"error": f"Cannot complete booking with status '{booking.status}'. Only PENDING or BOOKED bookings can be completed."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    previous_status = booking.status

    try:
        from django.utils import timezone
        # Update booking status first so it always persists even if file/email logic fails
        booking.status = BookingStatus.COMPLETED
        booking.completed_at = timezone.now()
        booking.save()

        # Collect uploaded result files (multiple); do not fail the request if this fails
        result_file_list = []
        try:
            if request.FILES:
                _ensure_booking_result_file_table()
                files_list = request.FILES.getlist("results") or request.FILES.getlist("results[]")
                for f in files_list:
                    if f and f.name:
                        bf = BookingResultFile(
                            booking=booking,
                            file=f,
                            original_name=f.name,
                        )
                        bf.save()
                        result_file_list.append(bf)
        except Exception as e:
            logger.exception("Saving result files failed; booking already marked completed")

        uploaded_names = [bf.original_name or bf.file.name for bf in result_file_list]
        file_count = len(uploaded_names)
        completion_comment = "Booking marked as completed."
        if file_count:
            completion_comment += f" {file_count} file(s) sent to user email: " + ", ".join(uploaded_names)
        completion_metadata = {}
        if file_count:
            completion_metadata = {"uploaded_files": uploaded_names, "uploaded_files_count": file_count}

        # Create booking event (no automatic email; we send one email with attachments below)
        create_booking_event(
            booking=booking,
            event_type=BookingEventType.COMPLETED,
            previous_status=previous_status,
            new_status=BookingStatus.COMPLETED,
            comment=completion_comment,
            metadata=completion_metadata or None,
            created_by=request.user,
            send_notification=False,
        )

        # Send completion email to user (with result files as attachments if any)
        try:
            _send_completion_email_with_attachments(booking, result_file_list)
        except Exception as e:
            logger.exception("Failed to send completion email with attachments")
            return Response(
                {
                    "message": "Booking marked as completed. Result files saved, but sending email failed: " + str(e),
                    "booking": BookingSerializer(booking).data,
                    "uploaded_files": uploaded_names,
                },
                status=status.HTTP_200_OK,
            )

        return Response(
            {
                "message": "Booking marked as completed." + (f" {len(uploaded_names)} file(s) sent to user email." if uploaded_names else ""),
                "booking": BookingSerializer(booking).data,
                "uploaded_files": uploaded_names,
            },
            status=status.HTTP_200_OK,
        )
    except Exception as e:
        logger.exception("Complete booking failed")
        return Response(
            {"error": "Complete booking failed: " + str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def refund_booking(request, booking_id):
    """Refund a booking and credit the wallet.
    
    This endpoint requires operator, manager, or admin permissions.
    Changes booking status to REFUNDED, frees up the slots (marks them as AVAILABLE),
    and credits the booking amount back to the user's wallet.
    
    Args:
        booking_id: Primary key of the booking
        
    Request Body (optional):
        {
            "notes": "Optional refund notes"
        }
        
    Returns:
        Response: Updated booking information and refund transaction details
    """
    if not check_operator_permission(request.user):
        return Response(
            {"error": "You don't have permission to perform this action. Only operators, managers, and admins can refund bookings."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    try:
        booking = Booking.objects.get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response(
            {"error": "Booking not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Don't allow refunding already refunded or not-utilized bookings
    if booking.status in (BookingStatus.REFUNDED, BookingStatus.BOOKING_NOT_UTILIZED):
        return Response(
            {"error": "This booking cannot be refunded."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    refund_notes = request.data.get('notes', '')
    try:
        refund_booking_internal(booking, refund_notes, request.user)
    except ValueError as e:
        return Response(
            {"error": str(e)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    except Exception as e:
        return Response(
            {"error": f"Error processing refund: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    from iic_booking.users.repositories.wallet_repository import WalletRepository
    refund_target, _ = WalletRepository.get_booking_wallet_target(
        booking.user, getattr(booking.equipment, "internal_department", None)
    )
    serializer = BookingSerializer(booking)
    return Response(
        {
            "message": f"Booking refunded successfully. ₹{booking.total_charge} credited to wallet.",
            "booking": serializer.data,
            "refund_amount": str(booking.total_charge),
            "wallet_balance": str(refund_target.balance),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def mark_booking_not_utilized(request, booking_id):
    """Mark a booking as Not Utilized (no refund).

    Allowed only when: booking is BOOKED; current time is after the latest **BOOKED** slot's
    **end_datetime**; sample lifecycle has no events or only SAMPLE_SENT. User did not attend or samples were not
    submitted in time.

    All booked slots are set to BOOKING_NOT_UTILIZED. Email is sent to the user and the
    faculty wallet owner (if different).

    Requires operator, manager, or admin permissions.
    """
    if not check_operator_permission(request.user):
        return Response(
            {"error": "You don't have permission to perform this action. Only operators, managers, and admins can mark bookings as not utilized."},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        booking = (
            Booking.objects.select_related("user", "equipment")
            .prefetch_related("daily_slots")
            .get(booking_id=booking_id)
        )
    except Booking.DoesNotExist:
        return Response(
            {"error": "Booking not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if booking.status != BookingStatus.BOOKED:
        return Response(
            {"error": "Only BOOKED bookings can be marked as Not Utilized."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    equipment = booking.equipment

    from .booking_not_utilized_service import apply_booking_not_utilized, latest_booked_slot_end_datetime

    booked_slots = list(
        booking.daily_slots.filter(status=SlotStatus.BOOKED).select_related("booking", "booking__user", "booking__equipment")
    )
    if not booked_slots:
        return Response(
            {"error": "No booked slots found for this booking. It may already be marked or cancelled."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if booking.daily_slots.exclude(status=SlotStatus.BOOKED).exists():
        return Response(
            {"error": "All slots for this booking must still be in BOOKED state."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Same anchor as the 8 PM job: latest DailySlot.end_datetime among BOOKED slots (not slot.date alone).
    booking_end = latest_booked_slot_end_datetime(booking)
    if booking_end is None:
        return Response(
            {"error": "BOOKED slots are missing end date/time; cannot evaluate slot end."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if getattr(booking, "atmosphere_sensitive_sample", False):
        starts = [
            s.start_datetime
            for s in booked_slots
            if getattr(s, "start_datetime", None) is not None
        ]
        if starts:
            slot_start = min(starts)
            if timezone.is_naive(slot_start):
                slot_start = timezone.make_aware(slot_start)
            if timezone.now() < slot_start:
                return Response(
                    {
                        "error": (
                            "This booking is marked atmosphere-sensitive: the sample may arrive at slot start. "
                            "Do not mark Booking Not Utilized before the slot begins."
                        ),
                    },
                    status=status.HTTP_400_BAD_REQUEST,
                )
    if timezone.now() < booking_end:
        return Response(
            {
                "error": (
                    "Booking Not Utilized can only be recorded after the last booked slot end time."
                ),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    from .sample_trace_policy import booking_not_utilized_blocked_detail

    bu_block = booking_not_utilized_blocked_detail(booking.booking_id)
    if bu_block:
        return Response({"error": bu_block}, status=status.HTTP_400_BAD_REQUEST)

    if not apply_booking_not_utilized(
        booking, actor=request.user, automated=False, hours_after_last_slot_end=0
    ):
        return Response(
            {"error": "Could not mark booking as Not Utilized (state may have changed)."},
            status=status.HTTP_409_CONFLICT,
        )

    booking.refresh_from_db()
    serializer = BookingSerializer(booking)
    return Response(
        {
            "message": "Booking marked as Not Utilized. No refund issued. User has been notified by email.",
            "booking": serializer.data,
        },
        status=status.HTTP_200_OK,
    )


def refund_booking_internal(booking, refund_notes, performed_by):
    """
    Refund a booking (free slots, credit wallet, set REFUNDED, create event, send notifications).
    Used by refund_booking API and by admin slot status change when marking booked slots as Blocked/Maintenance/Operator absent.
    Raises Exception on failure.
    """
    if booking.status == BookingStatus.REFUNDED:
        raise ValueError("Booking has already been refunded.")
    if booking.status == BookingStatus.BOOKING_NOT_UTILIZED:
        raise ValueError("Booking is marked as Not Utilized and cannot be refunded.")
    from iic_booking.users.repositories.wallet_repository import WalletRepository
    from django.db import transaction

    refund_target, _ = WalletRepository.get_booking_wallet_target(
        booking.user, getattr(booking.equipment, "internal_department", None)
    )
    if not refund_target:
        raise ValueError("User does not have a wallet. Cannot process refund.")

    if refund_notes:
        booking.notes = f"{booking.notes or ''}\n[Refund Notes]: {refund_notes}".strip()
        booking.save(update_fields=["notes"])

    released_slot_ids = list(booking.daily_slots.values_list("id", flat=True))
    with transaction.atomic():
        previous_status = booking.status
        booking.daily_slots.update(
            booking=None,
            status=released_slot_status_after_booking_freed(booking.equipment),
        )
        refund_description = f"Refund for Booking #{booking.booking_id} - {booking.equipment.code}"
        if refund_notes:
            refund_description += f" - {refund_notes}"
        refund_description += _student_booking_description_suffix(refund_target, booking.user)
        refund_description += f" | Ref: {booking_display_id_for_email(booking)}"
        refund_transaction = refund_target.credit(
            amount=booking.total_charge,
            description=refund_description,
            related_user=booking.user,
        )
        booking.status = BookingStatus.REFUNDED
        booking.save(update_fields=["status"])

        create_booking_event(
            booking=booking,
            event_type=BookingEventType.REFUNDED,
            previous_status=previous_status,
            new_status=BookingStatus.REFUNDED,
            comment=refund_notes or "Booking refunded.",
            created_by=performed_by,
            send_notification=True,
        )
        _reverse_reward_points_for_booking(booking, performed_by, "Reward points reversed for refunded booking")

        if refund_transaction:
            from iic_booking.communication.wallet_notifications import send_sub_wallet_transaction_notifications
            try:
                send_sub_wallet_transaction_notifications(
                    transaction=refund_transaction,
                    booking=booking,
                )
            except Exception as e:
                logger.error(f"Failed to send wallet transaction notification: {str(e)}", exc_info=True)

    # After slots are freed: notify waitlist so users can book (first come first serve)
    try:
        notify_waitlist_slots_available(
            booking.equipment,
            preferred_slot_ids=released_slot_ids,
            respect_reschedule_threshold=True,
        )
    except Exception as e:
        logger.warning("Failed to notify waitlist after refund for equipment %s: %s", booking.equipment.code, e)


def _reverse_reward_points_for_booking(booking, actor, note_prefix):
    points_used = Decimal(str(getattr(booking, "reward_points_used", 0) or "0"))
    if points_used <= 0:
        return
    already_reversed = TARewardLedger.objects.filter(
        student=booking.user,
        entry_type=TARewardLedgerEntryType.REVERSE,
        source_type=TARewardLedgerSourceType.BOOKING,
        source_id=booking.booking_id,
    ).exists()
    if already_reversed:
        return
    cfg = _get_reward_config(booking.equipment)
    TARewardLedger.objects.create(
        student=booking.user,
        entry_type=TARewardLedgerEntryType.REVERSE,
        points=points_used,
        currency_value=(points_used * Decimal(cfg.currency_per_point)).quantize(Decimal("0.01")),
        source_type=TARewardLedgerSourceType.BOOKING,
        source_id=booking.booking_id,
        description=f"{note_prefix} {booking_display_id_for_email(booking)}",
        created_by=actor,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def absent_booking(request, booking_id):
    """Mark a booking as Operator Unavailable.
    
    This endpoint requires operator, manager, or admin permissions.
    Frees the slots, issues a full refund to the user's wallet, sets booking status to
    Operator Unavailable (ABSENT), and sends an email to the user.
    
    Args:
        booking_id: Primary key of the booking
        
    Request Body (optional):
        {
            "notes": "Optional notes (e.g. reason operator was unavailable)"
        }
        
    Returns:
        Response: Updated booking information and refund details
    """
    if not check_operator_permission(request.user):
        return Response(
            {"error": "You don't have permission to perform this action. Only operators, managers, and admins can mark bookings as operator unavailable."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    try:
        booking = Booking.objects.select_related("user", "equipment").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response(
            {"error": "Booking not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    absent_notes = request.data.get("notes", "") or ""

    from iic_booking.equipment.maintenance_policy import apply_operator_disruption_pending_from_staff

    try:
        apply_operator_disruption_pending_from_staff(booking, notes=absent_notes)
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    booking.refresh_from_db()
    serializer = BookingSerializer(booking)
    return Response(
        {
            "message": (
                "Booking marked as Operator Unavailable (awaiting user choice). "
                "No refund yet — the user was emailed to choose cancel (refund) or reschedule."
            ),
            "booking": serializer.data,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def extend_booking_operator_absent_hold(request, booking_id):
    """
    Admin / Officer in Charge: extend the grace window used by automatic Operator Absent
    (and Operator Unavailable) jobs without changing slots or rescheduling the booking.

    Body:
      { "hold_until": "<ISO datetime>" }  — required; must be after last slot end
      { "clear": true }                   — optional; clears any existing hold
    """
    if request.user.user_type not in (UserType.ADMIN, UserType.MANAGER):
        return Response(
            {"error": "Only Admin and Officer in charge can extend the operator-absent hold."},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        booking = Booking.objects.select_related("user", "equipment").prefetch_related("daily_slots").get(
            booking_id=booking_id
        )
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.user.user_type == UserType.MANAGER:
        if not _user_can_act_as_oic_for_equipment(request.user, booking.equipment):
            return Response(
                {"error": "You do not manage this equipment."},
                status=status.HTTP_403_FORBIDDEN,
            )

    status_u = (booking.status or "").upper()
    if status_u not in (BookingStatus.BOOKED, BookingStatus.PENDING):
        return Response(
            {"error": "Operator-absent hold can only be set for Booked or Pending bookings."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    clear = bool(request.data.get("clear")) if request.data else False
    if clear:
        previous = booking.operator_absent_hold_until
        booking.operator_absent_hold_until = None
        booking.save(update_fields=["operator_absent_hold_until", "updated_at"])
        from iic_booking.equipment.booking_events import create_booking_event

        create_booking_event(
            booking,
            BookingEventType.COMMENT,
            created_by=request.user,
            comment="Cleared operator-absent hold extension.",
            metadata={
                "operator_absent_hold_cleared": True,
                "previous_hold_until": previous.isoformat() if previous else None,
            },
            send_notification=False,
        )
        serializer = BookingSerializer(booking)
        return Response(
            {"message": "Operator-absent hold cleared.", "booking": serializer.data},
            status=status.HTTP_200_OK,
        )

    hold_raw = (request.data.get("hold_until") if request.data else None) or None
    if not hold_raw:
        return Response(
            {"error": "hold_until (ISO datetime) is required, or set clear=true."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    from django.utils.dateparse import parse_datetime

    hold_until = parse_datetime(str(hold_raw).strip())
    if hold_until is None:
        return Response({"error": "Invalid hold_until datetime."}, status=status.HTTP_400_BAD_REQUEST)
    if timezone.is_naive(hold_until):
        hold_until = timezone.make_aware(hold_until)

    slots = list(booking.daily_slots.all())
    end_times = [s.end_datetime for s in slots if getattr(s, "end_datetime", None) is not None]
    if end_times:
        end_dt = max(end_times)
        if timezone.is_naive(end_dt):
            end_dt = timezone.make_aware(end_dt)
        if hold_until <= end_dt:
            return Response(
                {
                    "error": "hold_until must be after the booking's last slot end time.",
                    "last_slot_end": end_dt.isoformat(),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

    previous = booking.operator_absent_hold_until
    booking.operator_absent_hold_until = hold_until
    booking.save(update_fields=["operator_absent_hold_until", "updated_at"])

    from iic_booking.equipment.booking_events import create_booking_event

    create_booking_event(
        booking,
        BookingEventType.COMMENT,
        created_by=request.user,
        comment=f"Extended operator-absent hold until {timezone.localtime(hold_until).strftime('%Y-%m-%d %H:%M')}.",
        metadata={
            "operator_absent_hold_until": hold_until.isoformat(),
            "previous_hold_until": previous.isoformat() if previous else None,
        },
        send_notification=False,
    )

    serializer = BookingSerializer(booking)
    return Response(
        {
            "message": "Operator-absent hold extended. Slots were not changed.",
            "booking": serializer.data,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_maintenance_disruption(request, booking_id):
    """
    Admin or Officer in charge only: flag a booking for under-maintenance disruption policy
    (same as equipment-wide maintenance for that booking): user receives deadline email, may cancel
    (refund) or reschedule; extra week in slot picker applies after equipment is operational.
    No immediate refund; booking stays BOOKED/PENDING.
    """
    if request.user.user_type not in (UserType.ADMIN, UserType.MANAGER):
        return Response(
            {
                "error": "Only Admin and Officer in charge can flag a booking as under maintenance disruption.",
            },
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        booking = Booking.objects.select_related("user", "equipment").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.user.user_type == UserType.MANAGER:
        allowed_ids = set(get_equipment_ids_managed_by_oic(request.user.id))
        if booking.equipment_id not in allowed_ids:
            return Response(
                {"error": "You do not manage this equipment."},
                status=status.HTTP_403_FORBIDDEN,
            )

    notes = (request.data.get("notes") or "") if request.data else ""

    from iic_booking.equipment.maintenance_policy import apply_maintenance_disruption_for_booking_manually

    try:
        apply_maintenance_disruption_for_booking_manually(booking, notes=str(notes).strip())
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    booking.refresh_from_db()
    ev_comment = "Booking flagged for under-maintenance disruption (Admin/OIC). User notified by email."
    if str(notes).strip():
        ev_comment += f" Notes: {str(notes).strip()}"
    create_booking_event(
        booking=booking,
        event_type=BookingEventType.COMMENT,
        comment=ev_comment,
        created_by=request.user,
        send_notification=False,
    )

    serializer = BookingSerializer(booking)
    return Response(
        {
            "message": "Booking flagged for under-maintenance disruption. The user has been emailed with options and the decision deadline.",
            "booking": serializer.data,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def booking_other_disruption(request, booking_id):
    """
    Admin or Officer in charge only: flag a booking for "Other Disruption" policy.
    Staff must provide a reason; the same reason is emailed to the user. All other behavior
    matches existing disruption policy (awaiting user choice: cancel/refund or reschedule).
    """
    if request.user.user_type not in (UserType.ADMIN, UserType.MANAGER):
        return Response(
            {"error": "Only Admin and Officer in charge can flag a booking as Other Disruption."},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        booking = Booking.objects.select_related("user", "equipment").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

    if request.user.user_type == UserType.MANAGER:
        allowed_ids = set(get_equipment_ids_managed_by_oic(request.user.id))
        if booking.equipment_id not in allowed_ids:
            return Response({"error": "You do not manage this equipment."}, status=status.HTTP_403_FORBIDDEN)

    reason = (request.data.get("reason") or "") if request.data else ""

    from iic_booking.equipment.maintenance_policy import apply_other_disruption_for_booking_manually

    try:
        apply_other_disruption_for_booking_manually(booking, reason=str(reason).strip())
    except ValueError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    booking.refresh_from_db()
    ev_comment = "Booking flagged for Other Disruption (Admin/OIC). User notified by email."
    if str(reason).strip():
        ev_comment += f" Reason: {str(reason).strip()}"
    create_booking_event(
        booking=booking,
        event_type=BookingEventType.COMMENT,
        comment=ev_comment,
        created_by=request.user,
        send_notification=False,
    )

    serializer = BookingSerializer(booking)
    return Response(
        {
            "message": "Booking flagged for Other Disruption. The user has been emailed with options and the decision deadline.",
            "booking": serializer.data,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def reschedule_booking(request, booking_id):
    """Reschedule a booking to new time slots.
    
    This endpoint requires operator, manager, or admin permissions.
    Updates the booking's daily slots to new time range.
    
    Args:
        booking_id: Primary key of the booking
        
    Request Body:
        {
            "start_time": "2026-01-20T09:00:00Z",  # ISO datetime string
            "end_time": "2026-01-20T10:00:00Z"     # ISO datetime string
        }
        
    Returns:
        Response: Updated booking information with new slots
    """
    if not check_operator_permission(request.user):
        return Response(
            {"error": "You don't have permission to perform this action. Only operators, managers, and admins can reschedule bookings."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    try:
        booking = Booking.objects.get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response(
            {"error": "Booking not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    # Only allow rescheduling PENDING, BOOKED, or disruption-pending
    if booking.status not in [BookingStatus.PENDING, BookingStatus.BOOKED, BookingStatus.DISRUPTION_PENDING]:
        return Response(
            {
                "error": (
                    f"Cannot reschedule booking with status '{booking.status}'. "
                    "Only PENDING, BOOKED, or bookings awaiting your choice (disruption) can be rescheduled."
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Get new time range from request
    start_time_str = request.data.get('start_time')
    end_time_str = request.data.get('end_time')
    
    if not start_time_str or not end_time_str:
        return Response(
            {"error": "Both start_time and end_time are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    from django.utils.dateparse import parse_datetime
    
    try:
        start_time = parse_datetime(start_time_str)
        end_time = parse_datetime(end_time_str)
        
        if not start_time or not end_time:
            raise ValueError("Invalid datetime format")
        
        # Convert to timezone-aware if needed
        if timezone.is_naive(start_time):
            start_time = timezone.make_aware(start_time)
        if timezone.is_naive(end_time):
            end_time = timezone.make_aware(end_time)
        
        if end_time <= start_time:
            return Response(
                {"error": "End time must be after start time."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    except (ValueError, TypeError) as e:
        return Response(
            {"error": f"Invalid datetime format. Use ISO format (e.g., 2026-01-20T09:00:00Z). Error: {str(e)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Get equipment
    equipment = booking.equipment

    # Slots in the requested time range that are not already booked
    slots_in_range = DailySlot.objects.filter(
        slot_master__equipment=equipment,
        start_datetime__lt=end_time,
        end_datetime__gt=start_time,
        booking__isnull=True,
    ).order_by('start_datetime')

    # Operators/managers/admins may reschedule onto BLOCKED slots that are on holidays (weekends/calendar)
    if check_operator_permission(request.user):
        from django.db.models import Q
        slot_dates = list(slots_in_range.values_list('date', flat=True).distinct())
        holiday_dates = [d for d in slot_dates if Holiday.is_holiday(d)[0]]
        available_slots = slots_in_range.filter(
            Q(status=SlotStatus.AVAILABLE) |
            (Q(status=SlotStatus.BLOCKED) & Q(date__in=holiday_dates))
        )
    else:
        available_slots = slots_in_range.filter(status=SlotStatus.AVAILABLE)
        if UserType.is_external_user(getattr(booking.user, "user_type", None)):
            available_slots = available_slots.filter(reserved_for_external=True)
        else:
            available_slots = available_slots.filter(reserved_for_external=False)

    if not available_slots.exists():
        return Response(
            {"error": "No available slots found for the requested time range."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    released_slot_ids = list(booking.daily_slots.values_list("id", flat=True))
    # Process reschedule in a transaction
    try:
        from django.db import transaction

        with transaction.atomic():
            # Free up old slots
            old_slots = booking.daily_slots.all()
            free_status = (
                effective_slot_status_when_freeing_disruption_booking(booking)
                if (
                    booking.status == BookingStatus.DISRUPTION_PENDING
                    or getattr(booking, "maintenance_disruption_flag", False)
                )
                else released_slot_status_after_booking_freed(equipment)
            )
            old_slots.update(
                booking=None,
                status=free_status,
            )

            # Book new slots
            slot_ids = [slot.id for slot in available_slots]
            new_slots = DailySlot.objects.filter(id__in=slot_ids)
            new_slots.update(
                booking=booking,
                status=SlotStatus.BOOKED
            )
            
            previous_status = booking.status
            booking.status = BookingStatus.BOOKED
            if getattr(booking, "maintenance_disruption_flag", False) or previous_status == BookingStatus.DISRUPTION_PENDING:
                clear_disruption_policy_fields(booking)
            booking.save()
            
            # Create booking event for reschedule
            create_booking_event(
                booking=booking,
                event_type=BookingEventType.RESCHEDULED,
                previous_status=previous_status,
                new_status=booking.status,
                comment=f"Booking rescheduled to {start_time.strftime('%Y-%m-%d %H:%M')} - {end_time.strftime('%Y-%m-%d %H:%M')}",
                created_by=request.user,
                send_notification=True,
            )
            
    except Exception as e:
        return Response(
            {"error": f"Error rescheduling booking: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if released_slot_ids:
        try:
            notify_waitlist_slots_available(
                equipment,
                preferred_slot_ids=released_slot_ids,
                respect_reschedule_threshold=True,
            )
        except Exception as e:
            logger.warning(
                "Failed waitlist FCFS after reschedule for equipment %s: %s",
                getattr(equipment, "code", equipment.pk),
                e,
                exc_info=True,
            )

    serializer = BookingSerializer(booking)
    return Response(
        {
            "message": "Booking rescheduled successfully.",
            "booking": serializer.data,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cancel_booking(request, booking_id):
    """Cancel a booking.
    
    This endpoint requires operator, manager, or admin permissions.
    Changes booking status to CANCELLED and frees up the slots.
    Optionally refunds the booking amount.
    
    Args:
        booking_id: Primary key of the booking
        
    Request Body (optional):
        {
            "refund": true,  # Whether to refund the booking amount
            "notes": "Optional cancellation notes"
        }
        
    Returns:
        Response: Updated booking information
    """
    if not check_operator_permission(request.user):
        return Response(
            {"error": "You don't have permission to perform this action. Only operators, managers, and admins can cancel bookings."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    try:
        booking = Booking.objects.get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response(
            {"error": "Booking not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Don't allow cancelling already cancelled or refunded bookings
    if booking.status in [BookingStatus.CANCELLED, BookingStatus.REFUNDED, BookingStatus.BOOKING_NOT_UTILIZED]:
        return Response(
            {"error": f"This booking is already {booking.status.lower().replace('_', ' ')}."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    should_refund = request.data.get("refund", False)
    cancel_notes = request.data.get("notes", "")

    try:
        cancel_req = parse_cancellation_request(request.data, booking)
    except CancellationValidationError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    try:
        with transaction.atomic():
            cancel_result = perform_booking_cancellation(
                booking,
                slot_ids=cancel_req["slot_ids"],
                should_refund=should_refund,
                cancel_notes=cancel_notes,
                actor=request.user,
                allow_started_slots=True,
                reverse_reward_points_fn=_reverse_reward_points_for_booking,
                student_booking_description_suffix_fn=_student_booking_description_suffix,
                cancelled_by_label="admin",
                partial_plan=cancel_req.get("plan"),
            )
    except CancellationValidationError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        return Response(
            {"error": f"Error cancelling booking: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    released_slot_ids = cancel_result["released_slot_ids"]
    refund_transaction = cancel_result["refund_transaction"]
    refund_amount = cancel_result["refund_amount"]

    booking.refresh_from_db()
    serializer = BookingSerializer(booking)
    if cancel_result["is_full_cancel"]:
        message = "Booking cancelled successfully."
        if should_refund and refund_transaction:
            message += f" ₹{refund_amount} refunded to wallet."
    else:
        message = f"{len(released_slot_ids)} slot(s) cancelled." if released_slot_ids else "Booking partially updated."
        if should_refund and refund_transaction:
            message += f" ₹{refund_amount} refunded to wallet."

    response_data = {
        "message": message,
        "booking": serializer.data,
        "partial_cancellation": not cancel_result["is_full_cancel"],
    }
    if should_refund and refund_transaction:
        response_data["refund_amount"] = str(refund_amount)

    return Response(response_data, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def partial_cancel_preview(request, booking_id):
    """Preview partial cancellation refund and revised user inputs."""
    try:
        booking = Booking.objects.select_related("equipment", "charge_profile").get(
            booking_id=booking_id
        )
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

    is_owner = booking.user == request.user
    is_staff = check_operator_permission(request.user)
    if not is_owner and not is_staff:
        return Response(
            {"error": "You don't have permission to preview cancellation for this booking."},
            status=status.HTTP_403_FORBIDDEN,
        )

    reduced = request.data.get("reduced_input_values")
    raw_slot_ids = request.data.get("slot_ids")
    print_analysis_ids = request.data.get("print_analysis_ids")
    try:
        if print_analysis_ids is not None:
            if not isinstance(print_analysis_ids, list):
                raise CancellationValidationError("print_analysis_ids must be a list of UUID strings.")
            preview = preview_partial_cancellation_print_items(
                booking, print_analysis_ids=print_analysis_ids
            )
        elif reduced is not None:
            preview = preview_partial_cancellation(booking, reduced_input_values=reduced)
        elif raw_slot_ids is not None:
            if not isinstance(raw_slot_ids, list):
                raise CancellationValidationError("slot_ids must be a list of integers.")
            slot_ids = [int(x) for x in raw_slot_ids]
            preview = preview_partial_cancellation(booking, slot_ids_to_cancel=slot_ids)
        else:
            raise CancellationValidationError(
                "Provide reduced_input_values, slot_ids, or print_analysis_ids to preview partial cancellation."
            )
    except CancellationValidationError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    equipment = booking.equipment
    profile_type = getattr(equipment, "profile_type", None)
    reduction_key = partial_cancel_reduction_key(profile_type)
    preview["partial_cancel_mode"] = (
        "print_items"
        if (profile_type or "").strip().upper() == EquipmentProfileType.PRINT_3D
        else "input_reduction"
        if partial_cancel_uses_input_reduction(profile_type)
        else "slot_selection"
    )
    preview["reduction_field_key"] = reduction_key
    if reduction_key:
        field = equipment.input_fields.filter(field_key=reduction_key).first()
        preview["reduction_field_label"] = (
            field.field_label if field else reduction_key
        )
    else:
        preview["reduction_field_label"] = None
    preview["current_input_values"] = booking.input_values or {}
    preview["equipment_profile_type"] = profile_type

    return Response(preview, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def user_cancel_booking(request, booking_id):
    """Cancel a booking by the user who made it.
    
    This endpoint allows users to cancel their own bookings.
    Changes booking status to CANCELLED and frees up the slots.
    Optionally refunds the booking amount.
    
    Args:
        booking_id: Primary key of the booking
        
    Request Body (optional):
        {
            "refund": true,  # Whether to refund the booking amount (default: false)
            "notes": "Optional cancellation notes"
        }
        
    Returns:
        Response: Updated booking information
    """
    try:
        booking = Booking.objects.get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response(
            {"error": "Booking not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check if the booking belongs to the authenticated user
    if booking.user != request.user:
        return Response(
            {"error": "You don't have permission to cancel this booking. You can only cancel your own bookings."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Repeat-sample bookings (source_booking_id set) cannot be cancelled by users
    if getattr(booking, "source_booking_id", None) is not None:
        return Response(
            {"error": "Repeat sample bookings cannot be cancelled by users. Please contact admin if you need to cancel."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Don't allow cancelling already cancelled, completed, or refunded bookings
    if booking.status in [BookingStatus.CANCELLED, BookingStatus.COMPLETED, BookingStatus.REFUNDED, BookingStatus.BOOKING_NOT_UTILIZED]:
        return Response(
            {"error": f"This booking is already {booking.status.lower().replace('_', ' ')} and cannot be cancelled."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Only allow canceling PENDING, BOOKED, or disruption-pending (awaiting refund vs reschedule)
    if booking.status not in [BookingStatus.PENDING, BookingStatus.BOOKED, BookingStatus.DISRUPTION_PENDING]:
        return Response(
            {
                "error": (
                    f"Cannot cancel booking with status '{booking.status}'. "
                    "Only PENDING, BOOKED, or bookings awaiting your choice (disruption) can be cancelled."
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Enforce reschedule-hours threshold for user actions (normal case).
    # If now is strictly inside the threshold window before the allocated slot start time,
    # disallow cancel. (Maintenance disruption policy keeps cancel available.)
    if not getattr(booking, "maintenance_disruption_flag", False) and booking.status != BookingStatus.DISRUPTION_PENDING:
        equipment = booking.equipment
        threshold_hours = getattr(equipment, "reschedule_hours_threshold", None) or 48
        earliest_start = (
            booking.daily_slots.order_by("start_datetime")
            .values_list("start_datetime", flat=True)
            .first()
        )
        if earliest_start is None:
            return Response(
                {"error": "Cannot cancel this booking. Start time is not available."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cutoff_time = earliest_start - timedelta(hours=int(threshold_hours))
        if timezone.now() > cutoff_time:
            cutoff_str = timezone.localtime(cutoff_time).strftime("%Y-%m-%d %H:%M")
            return Response(
                {
                    "error": (
                        f"Cancellation is only available until {cutoff_str}. "
                        "Kindly contact the admin to cancel the booking."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
    
    # Get options from request (disruption-pending: default refund when using Cancel)
    should_refund = request.data.get("refund", booking.status == BookingStatus.DISRUPTION_PENDING)
    cancel_notes = request.data.get("notes", "")

    try:
        cancel_req = parse_cancellation_request(request.data, booking)
    except CancellationValidationError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    allow_started_slots = user_may_cancel_started_slots(request.user)

    try:
        with transaction.atomic():
            cancel_result = perform_booking_cancellation(
                booking,
                slot_ids=cancel_req["slot_ids"],
                should_refund=should_refund,
                cancel_notes=cancel_notes,
                actor=request.user,
                allow_started_slots=allow_started_slots,
                reverse_reward_points_fn=_reverse_reward_points_for_booking,
                student_booking_description_suffix_fn=_student_booking_description_suffix,
                cancelled_by_label="user",
                partial_plan=cancel_req.get("plan"),
            )
    except CancellationValidationError as e:
        return Response({"error": str(e)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception as e:
        return Response(
            {"error": f"Error cancelling booking: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    released_slot_ids = cancel_result["released_slot_ids"]
    refund_transaction = cancel_result["refund_transaction"]
    refund_amount = cancel_result["refund_amount"]

    booking.refresh_from_db()
    serializer = BookingSerializer(booking)
    if cancel_result["is_full_cancel"]:
        message = "Booking cancelled successfully."
        if should_refund and refund_transaction:
            message += f" ₹{refund_amount} refunded to wallet."
    else:
        message = f"{len(released_slot_ids)} slot(s) cancelled." if released_slot_ids else "Booking partially updated."
        if should_refund and refund_transaction:
            message += f" ₹{refund_amount} refunded to wallet."

    response_data = {
        "message": message,
        "booking": serializer.data,
        "partial_cancellation": not cancel_result["is_full_cancel"],
    }
    if should_refund and refund_transaction:
        response_data["refund_amount"] = str(refund_amount)

    return Response(response_data, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def request_booking_cancellation(request, booking_id):
    """Request cancellation of a booking with refund.
    
    This endpoint allows users to request cancellation of their own bookings with refund.
    Creates a cancellation request that needs admin approval.
    
    Args:
        booking_id: Primary key of the booking
        
    Request Body (optional):
        {
            "notes": "Optional cancellation notes"
        }
        
    Returns:
        Response: Cancellation request information
    """
    try:
        booking = Booking.objects.get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response(
            {"error": "Booking not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check if the booking belongs to the authenticated user
    if booking.user != request.user:
        return Response(
            {"error": "You don't have permission to cancel this booking. You can only cancel your own bookings."},
            status=status.HTTP_403_FORBIDDEN,
        )

    # Repeat-sample bookings cannot have cancellation requested by users
    if getattr(booking, "source_booking_id", None) is not None:
        return Response(
            {"error": "Repeat sample bookings cannot be cancelled by users. Please contact admin if you need to cancel."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Don't allow requesting cancellation for already cancelled, completed, or refunded bookings
    if booking.status in [BookingStatus.CANCELLED, BookingStatus.COMPLETED, BookingStatus.REFUNDED, BookingStatus.BOOKING_NOT_UTILIZED]:
        return Response(
            {"error": f"This booking is already {booking.status.lower().replace('_', ' ')} and cannot be cancelled."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Only allow requesting cancellation for PENDING, BOOKED, or disruption-pending
    if booking.status not in [BookingStatus.PENDING, BookingStatus.BOOKED, BookingStatus.DISRUPTION_PENDING]:
        return Response(
            {
                "error": (
                    f"Cannot request cancellation for booking with status '{booking.status}'. "
                    "Only PENDING, BOOKED, or bookings awaiting your choice (disruption) can use this flow."
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Enforce reschedule-hours threshold for user actions (normal case).
    # In the normal case, block cancellation requests when now is strictly inside the threshold window.
    if not getattr(booking, "maintenance_disruption_flag", False) and booking.status != BookingStatus.DISRUPTION_PENDING:
        equipment = booking.equipment
        threshold_hours = getattr(equipment, "reschedule_hours_threshold", None) or 48
        earliest_start = (
            booking.daily_slots.order_by("start_datetime")
            .values_list("start_datetime", flat=True)
            .first()
        )
        if earliest_start is None:
            return Response(
                {"error": "Cannot request cancellation. Start time is not available."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cutoff_time = earliest_start - timedelta(hours=int(threshold_hours))
        if timezone.now() > cutoff_time:
            cutoff_str = timezone.localtime(cutoff_time).strftime("%Y-%m-%d %H:%M")
            return Response(
                {
                    "error": (
                        f"Cancellation is only available until {cutoff_str}. "
                        "Kindly contact the admin to cancel the booking."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
    
    # Check if there's already a pending cancellation request
    existing_request = BookingCancellationRequest.objects.filter(
        booking=booking,
        status=BookingCancellationRequestStatus.PENDING
    ).first()
    
    if existing_request:
        return Response(
            {"error": "A pending cancellation request already exists for this booking."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Get notes from request
    cancel_notes = request.data.get('notes', '')
    
    # Create cancellation request
    try:
        cancellation_request = BookingCancellationRequest.objects.create(
            booking=booking,
            user=request.user,
            notes=cancel_notes,
            status=BookingCancellationRequestStatus.PENDING
        )
        
        # TODO: Send notification to admin/accounts team
        
        serializer = BookingCancellationRequestSerializer(cancellation_request)
        
        return Response(
            {
                "message": "Cancellation request submitted successfully. It will be reviewed by the admin.",
                "cancellation_request": serializer.data,
            },
            status=status.HTTP_201_CREATED,
        )
    except Exception as e:
        return Response(
            {"error": f"Error creating cancellation request: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def user_reschedule_booking(request, booking_id):
    """Reschedule a booking to new time slots by the user who made it.
    
    This endpoint allows users to reschedule their own bookings.
    Updates the booking's daily slots to new time range.
    
    Args:
        booking_id: Primary key of the booking
        
    Request Body:
        {
            "start_time": "2026-01-20T09:00:00Z",  # ISO datetime string
            "end_time": "2026-01-20T10:00:00Z"     # ISO datetime string
        }
        
    Returns:
        Response: Updated booking information with new slots
    """
    try:
        booking = Booking.objects.get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response(
            {"error": "Booking not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Own bookings for normal users; Admin / OIC / Lab Operator may reschedule any booking.
    is_staff_rescheduler = check_operator_permission(request.user)
    if booking.user != request.user and not is_staff_rescheduler:
        return Response(
            {"error": "You don't have permission to reschedule this booking. You can only reschedule your own bookings."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Only allow rescheduling PENDING, BOOKED, or disruption-pending (awaiting choice)
    if booking.status not in [BookingStatus.PENDING, BookingStatus.BOOKED, BookingStatus.DISRUPTION_PENDING]:
        return Response(
            {
                "error": (
                    f"Cannot reschedule booking with status '{booking.status}'. "
                    "Only PENDING, BOOKED, or bookings awaiting your choice (disruption) can be rescheduled."
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Enforce reschedule-hours threshold for user actions (normal case).
    # If now is strictly inside the threshold window before the allocated slot start time,
    # disallow reschedule. (Maintenance disruption policy bypasses this threshold check.)
    # Staff (Admin / OIC / Operator) are not bound by the threshold.
    if (
        not is_staff_rescheduler
        and not getattr(booking, "maintenance_disruption_flag", False)
        and booking.status != BookingStatus.DISRUPTION_PENDING
    ):
        equipment = booking.equipment
        threshold_hours = getattr(equipment, "reschedule_hours_threshold", None) or 48
        earliest_start = (
            booking.daily_slots.order_by("start_datetime")
            .values_list("start_datetime", flat=True)
            .first()
        )
        if earliest_start is None:
            return Response(
                {"error": "Cannot reschedule this booking. Start time is not available."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        cutoff_time = earliest_start - timedelta(hours=int(threshold_hours))
        if timezone.now() > cutoff_time:
            cutoff_str = timezone.localtime(cutoff_time).strftime("%Y-%m-%d %H:%M")
            return Response(
                {
                    "error": (
                        f"Rescheduling is only available until {cutoff_str}. "
                        "Kindly contact the admin to reschedule the booking."
                    )
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

    equipment = booking.equipment
    disruption_kind = getattr(booking, "disruption_kind", None)
    needs_equipment_operational = disruption_kind not in (
        BookingDisruptionKind.OPERATOR_ABSENT,
        BookingDisruptionKind.OTHER_DISRUPTION,
    )
    if getattr(booking, "maintenance_disruption_flag", False) and needs_equipment_operational:
        if (equipment.status or "").strip() != EquipmentStatus.ACTIVE:
            return Response(
                {
                    "error": (
                        "This booking is under the maintenance disruption policy. "
                        "Reschedule will be available once the equipment is operational. "
                        "You can cancel now for a full refund from My Bookings, or wait for our email when the equipment is back."
                    ),
                    "maintenance_disruption": True,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

    # Get new time range from request
    start_time_str = request.data.get('start_time')
    end_time_str = request.data.get('end_time')
    
    if not start_time_str or not end_time_str:
        return Response(
            {"error": "Both start_time and end_time are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    from django.utils.dateparse import parse_datetime
    
    try:
        start_time = parse_datetime(start_time_str)
        end_time = parse_datetime(end_time_str)
        
        if not start_time or not end_time:
            raise ValueError("Invalid datetime format")
        
        # Convert to timezone-aware if needed
        if timezone.is_naive(start_time):
            start_time = timezone.make_aware(start_time)
        if timezone.is_naive(end_time):
            end_time = timezone.make_aware(end_time)
        
        if end_time <= start_time:
            return Response(
                {"error": "End time must be after start time."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    except (ValueError, TypeError) as e:
        return Response(
            {"error": f"Invalid datetime format. Use ISO format (e.g., 2026-01-20T09:00:00Z). Error: {str(e)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Find available slots for the new time range
    # Exclude slots that are currently booked by this booking
    slots_filter = {
        "slot_master__equipment": equipment,
        "start_datetime__lt": end_time,
        "end_datetime__gt": start_time,
        "status": SlotStatus.AVAILABLE,
    }
    if UserType.is_external_user(getattr(booking.user, "user_type", None)):
        slots_filter["reserved_for_external"] = True
    else:
        slots_filter["reserved_for_external"] = False
    available_slots = DailySlot.objects.filter(**slots_filter).order_by('start_datetime')
    available_slots = filter_queryset_for_home_department(
        available_slots,
        user=booking.user,
        equipment=equipment,
        is_admin=False,
        is_external=UserType.is_external_user(getattr(booking.user, "user_type", None)),
    )
    
    if not available_slots.exists():
        return Response(
            {"error": "No available slots found for the requested time range."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Check if all slots are available
    slot_checker = (
        SlotAvailabilityChecker.is_slot_available_for_external
        if UserType.is_external_user(getattr(booking.user, "user_type", None))
        else SlotAvailabilityChecker.is_slot_available
    )
    unavailable_slots = []
    for slot in available_slots:
        if not slot_checker(slot):
            unavailable_slots.append(slot.id)
    
    if unavailable_slots:
        return Response(
            {"error": f"Slots {unavailable_slots} are not available for booking."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # Weekly/monthly quota on reschedule: treat as MOVE, not ADD.
    # Exclude this booking from existing-bookings aggregation so moving across weeks/months
    # correctly shifts usage to the new period.
    try:
        additional_minutes = int(getattr(booking, "total_time_minutes", 0) or 0)
        additional_charge = Decimal(str(getattr(booking, "total_charge", 0) or "0"))
        quota_date = start_time
        # Special case: reschedule from disruption-pending should remain counted in the ORIGINAL period.
        if booking.status == BookingStatus.DISRUPTION_PENDING and getattr(booking, "quota_period_anchor_at", None) is not None:
            quota_date = booking.quota_period_anchor_at
        if not booking_quota_should_skip(equipment):
            quota_allowed, quota_error = QuotaChecker.check_user_quota(
                booking.user,
                equipment,
                quota_type=QUOTA_TYPE_WEEKLY,
                additional_time_minutes=additional_minutes,
                additional_bookings=1,
                additional_charge=additional_charge,
                booking_date=quota_date,
                exclude_booking_id=booking.booking_id,
            )
            if not quota_allowed:
                return Response({"error": f"Weekly quota check failed: {quota_error}"}, status=status.HTTP_400_BAD_REQUEST)
            quota_allowed, quota_error = QuotaChecker.check_user_quota(
                booking.user,
                equipment,
                quota_type=QUOTA_TYPE_MONTHLY,
                additional_time_minutes=additional_minutes,
                additional_bookings=1,
                additional_charge=additional_charge,
                booking_date=quota_date,
                exclude_booking_id=booking.booking_id,
            )
            if not quota_allowed:
                return Response({"error": f"Monthly quota check failed: {quota_error}"}, status=status.HTTP_400_BAD_REQUEST)
    except Exception:
        logger.exception("Quota check failed during user reschedule for booking %s", booking.booking_id)
        return Response({"error": "Quota check failed. Please try again or contact admin."}, status=status.HTTP_400_BAD_REQUEST)

    released_slot_ids = list(booking.daily_slots.values_list("id", flat=True))
    # Process reschedule in a transaction
    try:
        from django.db import transaction

        with transaction.atomic():
            # Free up old slots
            old_slots = booking.daily_slots.all()
            free_status = (
                effective_slot_status_when_freeing_disruption_booking(booking)
                if (
                    booking.status == BookingStatus.DISRUPTION_PENDING
                    or getattr(booking, "maintenance_disruption_flag", False)
                )
                else released_slot_status_after_booking_freed(equipment)
            )
            old_slots.update(
                booking=None,
                status=free_status,
            )

            # Book new slots
            slot_ids = list(available_slots.values_list("id", flat=True))
            new_slots = DailySlot.objects.filter(id__in=slot_ids)
            new_slots.update(
                booking=booking,
                status=SlotStatus.BOOKED,
            )

            previous_status = booking.status
            booking.status = BookingStatus.BOOKED
            if getattr(booking, "maintenance_disruption_flag", False) or previous_status == BookingStatus.DISRUPTION_PENDING:
                clear_disruption_policy_fields(booking)
            booking.save()

            # Create booking event for reschedule
            create_booking_event(
                booking=booking,
                event_type=BookingEventType.RESCHEDULED,
                previous_status=previous_status,
                new_status=booking.status,
                comment=f"Booking rescheduled to {start_time.strftime('%Y-%m-%d %H:%M')} - {end_time.strftime('%Y-%m-%d %H:%M')}",
                created_by=request.user,
                send_notification=True,
            )

    except Exception as e:
        return Response(
            {"error": f"Error rescheduling booking: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if released_slot_ids:
        try:
            notify_waitlist_slots_available(
                equipment,
                preferred_slot_ids=released_slot_ids,
                respect_reschedule_threshold=True,
            )
        except Exception as e:
            logger.warning(
                "Failed waitlist FCFS after user reschedule for equipment %s: %s",
                getattr(equipment, "code", equipment.pk),
                e,
                exc_info=True,
            )

    serializer = BookingSerializer(booking)
    return Response(
        {
            "message": "Booking rescheduled successfully.",
            "booking": serializer.data,
        },
        status=status.HTTP_200_OK,
    )


# S3 prefix under which booking result folders live; search all subfolders for {virtual_booking_id}/
S3_RESULTS_PREFIX = "Results"


def _list_booking_result_files_from_s3(virtual_booking_id, expires_in=3600):
    """List objects in S3 under Results/ (any folder/subfolder) where a path segment equals virtual_booking_id.
    E.g. Results/{id}/, Results/2026/Jan/{id}/, Results/lab1/run1/{id}/ are all included.
    Returns presigned download URLs for every matching object.
    """
    from django.conf import settings
    import boto3
    from botocore.exceptions import ClientError

    bucket = getattr(settings, "AWS_STORAGE_BUCKET_NAME", None)
    if not bucket:
        return None, []
    # Search under entire Results/ tree; we filter by /{virtual_booking_id}/ in the key
    prefix = f"{S3_RESULTS_PREFIX}/"
    folder_segment = f"/{virtual_booking_id}/"
    try:
        client = boto3.client(
            "s3",
            region_name=getattr(settings, "AWS_S3_REGION_NAME", "ap-south-1"),
            aws_access_key_id=getattr(settings, "AWS_ACCESS_KEY_ID", None),
            aws_secret_access_key=getattr(settings, "AWS_SECRET_ACCESS_KEY", None),
        )
        paginator = client.get_paginator("list_objects_v2")
        files = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents") or []:
                key = obj.get("Key")
                if not key or key.endswith("/"):
                    continue
                # Include only keys that contain a path segment exactly equal to virtual_booking_id
                if folder_segment not in key:
                    continue
                # Display name: path relative to the booking-id folder (keeps subfolder structure)
                if folder_segment in key:
                    name = key.split(folder_segment, 1)[-1]
                else:
                    name = key.split("/")[-1]
                try:
                    url = client.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": bucket, "Key": key},
                        ExpiresIn=expires_in,
                    )
                    files.append({"key": key, "name": name, "download_url": url})
                except ClientError:
                    continue
        return True, files
    except Exception as e:
        logger.warning("S3 list Results for %s failed: %s", virtual_booking_id, e)
        return None, []


def _get_s3_client_and_results_keys(virtual_booking_id):
    """Return (client, bucket, list of (key, relative_name)) for streaming; (None, None, []) on error."""
    from django.conf import settings
    import boto3

    bucket = getattr(settings, "AWS_STORAGE_BUCKET_NAME", None)
    if not bucket:
        return None, None, []
    prefix = f"{S3_RESULTS_PREFIX}/"
    folder_segment = f"/{virtual_booking_id}/"
    try:
        client = boto3.client(
            "s3",
            region_name=getattr(settings, "AWS_S3_REGION_NAME", "ap-south-1"),
            aws_access_key_id=getattr(settings, "AWS_ACCESS_KEY_ID", None),
            aws_secret_access_key=getattr(settings, "AWS_SECRET_ACCESS_KEY", None),
        )
        paginator = client.get_paginator("list_objects_v2")
        keys_with_names = []
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents") or []:
                key = obj.get("Key")
                if not key or key.endswith("/"):
                    continue
                if folder_segment not in key:
                    continue
                name = key.split(folder_segment, 1)[-1]
                keys_with_names.append((key, name))
        return client, bucket, keys_with_names
    except Exception as e:
        logger.warning("S3 list keys for %s failed: %s", virtual_booking_id, e)
        return None, None, []


def _apply_results_available_event_and_completed_status(booking):
    """DB-only: booking event + status COMPLETED. Caller must hold row lock and verified notified_at is unset."""
    detected_at = timezone.now()
    detected_str = detected_at.strftime("%d %b %Y, %I:%M %p")
    previous_status = booking.status
    comment = f"Results are now available for this booking. Detected on {detected_str}. Booking status set to Complete."
    create_booking_event(
        booking=booking,
        event_type=BookingEventType.STATUS_CHANGED,
        previous_status=previous_status,
        new_status=BookingStatus.COMPLETED,
        comment=comment,
        created_by=booking.user,
        send_notification=False,
    )
    booking.status = BookingStatus.COMPLETED
    booking.save(update_fields=["status"])


def _send_results_available_push_and_email(booking):
    """Push + email only (slow I/O). Run after DB commit so list/download APIs return immediately."""
    from django.conf import settings
    from django.core.mail import send_mail

    from iic_booking.communication.service import CommunicationService
    from iic_booking.communication.utils import get_frontend_absolute_url

    user = booking.user
    sample_notice_ctx = _build_sample_notice_context(booking)
    virtual_id = booking_display_id_for_email(booking) or f"#{booking.booking_id}"
    my_bookings_link = get_frontend_absolute_url(f"/my-bookings?booking={virtual_id}")

    try:
        CommunicationService.send_push_notification(
            recipient=user,
            title="Results available for your booking",
            message=f"Result files are available for Booking ID- {virtual_id}. Open My Bookings to download.",
            metadata={
                "notification_type": "info",
                "link": my_bookings_link,
                "booking_id": virtual_id,
                "real_booking_id": booking.booking_id,
                "booking_display_id": virtual_id,
            },
        )
    except Exception as e:
        logger.warning("Results-available push notification failed for booking %s: %s", booking.booking_id, e)

    subject = f"Results available for Booking ID- {virtual_id}"
    body_lines = [
        f"Hello {user.name or user.email},",
        "",
        f"Result files for your booking (Booking ID- {virtual_id}) are now available.",
        "",
        "You can download them from your account:",
        "",
        my_bookings_link or "(Log in to the booking portal and go to My Bookings)",
        "",
        "Click the link above to open My Bookings. Find this booking and click the green 'Results' button to download the folder as a ZIP or open individual files.",
        "",
        sample_notice_ctx["sample_collection_notice"],
        "",
        "— IIC Booking",
    ]
    if sample_notice_ctx["is_external_user"]:
        body_lines = body_lines[:-1] + [
            "External users: confirm if sample is to be preserved and sent back",
            "(courier charges to be paid extra):",
            f"Yes: {sample_notice_ctx['sample_preserve_yes_url']}",
            f"No: {sample_notice_ctx['sample_preserve_no_url']}",
            "",
            body_lines[-1],
        ]
    try:
        from iic_booking.users.test_accounts import redirect_email_for_user

        delivery_email, subject = redirect_email_for_user(
            user, original_email=user.email, subject=subject
        )
        send_mail(
            subject=subject,
            message="\n".join(body_lines),
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[delivery_email],
            fail_silently=True,
        )
    except Exception as e:
        logger.warning("Results-available email failed for booking %s: %s", booking.booking_id, e)


def _notify_user_results_available_by_id(booking_id: int):
    """Idempotent: notify once per booking (results_available_notified_at). Safe for concurrent tasks."""
    from django.utils import timezone as dj_tz

    with transaction.atomic():
        try:
            locked = Booking.objects.select_for_update().select_related("user", "equipment").get(
                booking_id=booking_id
            )
        except Booking.DoesNotExist:
            return
        if locked.results_available_notified_at is not None:
            return
        virtual_booking_id = (locked.virtual_booking_id or "").strip()
        if not virtual_booking_id:
            return

    ok, file_list = _list_booking_result_files_from_s3(virtual_booking_id)
    if ok is None or len(file_list) == 0:
        return

    with transaction.atomic():
        locked = Booking.objects.select_for_update().select_related("user", "equipment").get(
            booking_id=booking_id
        )
        if locked.results_available_notified_at is not None:
            return
        _apply_results_available_event_and_completed_status(locked)
        locked.results_available_notified_at = dj_tz.now()
        locked.save(update_fields=["results_available_notified_at"])

    fresh = Booking.objects.select_related("user", "equipment").get(booking_id=booking_id)
    _send_results_available_push_and_email(fresh)


def _schedule_notify_user_results_available(booking_id: int):
    """Queue notify after HTTP response; never block the client on SMTP/push."""
    try:
        from iic_booking.equipment.tasks import notify_user_results_available_task

        notify_user_results_available_task.delay(booking_id)
    except Exception:
        logger.warning(
            "Failed to queue results-available notification for booking %s; using background thread",
            booking_id,
            exc_info=True,
        )

        def _run():
            from django.db import close_old_connections

            close_old_connections()
            try:
                _notify_user_results_available_by_id(booking_id)
            except Exception:
                logger.exception(
                    "Background results-available notification failed for booking %s",
                    booking_id,
                )
            finally:
                close_old_connections()

        threading.Thread(target=_run, daemon=True).start()


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def update_booking_istem_fbr(request, booking_id):
    """External booking user submits or updates the I-STEM FBR number after booking on https://www.istem.gov.in/ ."""
    try:
        booking = Booking.objects.select_related("equipment", "charge_profile").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    if booking.user_id != request.user.id:
        return Response({"error": "You can only update your own bookings."}, status=status.HTTP_403_FORBIDDEN)
    if booking.istem_fbr_status is None and not charge_profile_requires_istem_fbr(booking.charge_profile):
        return Response(
            {"error": "This booking is not in the I-STEM FBR workflow."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    from iic_booking.equipment.booking_payment_service import booking_payment_fully_settled

    if not booking_payment_fully_settled(booking):
        return Response(
            {"error": "Complete payment for this booking before submitting your I-STEM FBR number."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if booking.status not in (BookingStatus.BOOKED, BookingStatus.COMPLETED):
        return Response(
            {"error": "Booking must be confirmed before submitting FBR."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    st = booking.istem_fbr_status
    if st not in (IstemFbrStatus.PENDING_FBR, IstemFbrStatus.INVALID):
        if st is None:
            return Response(
                {"error": "This booking is not in the I-STEM FBR workflow."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if st == IstemFbrStatus.PENDING_OIC:
            return Response(
                {"error": "Your FBR is awaiting Officer in Charge verification. You cannot change it now."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response(
            {"error": "This FBR has already been marked as verified."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    raw = (request.data.get("istem_fbr_number") if isinstance(request.data, dict) else None) or ""
    raw = str(raw).strip()
    if not raw:
        return Response({"error": "I-STEM FBR number is required."}, status=status.HTTP_400_BAD_REQUEST)
    booking.istem_fbr_number = raw[:128]
    booking.istem_fbr_status = IstemFbrStatus.PENDING_OIC
    booking.istem_fbr_invalid_reason = None
    booking.save(update_fields=["istem_fbr_number", "istem_fbr_status", "istem_fbr_invalid_reason", "updated_at"])
    create_booking_event(
        booking=booking,
        event_type=BookingEventType.COMMENT,
        created_by=request.user,
        comment=f"I-STEM FBR number submitted for verification: {booking.istem_fbr_number}",
        send_notification=True,
    )
    _notify_oic_istem_fbr_submitted(booking)
    return Response({"message": "FBR submitted. An Officer in Charge will verify it on I-STEM.", "booking": BookingSerializer(booking).data})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def review_booking_istem_fbr(request, booking_id):
    """OIC (or admin): mark external user's FBR as executed on I-STEM or invalid."""
    try:
        booking = Booking.objects.select_related("equipment", "charge_profile").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    if booking.istem_fbr_status is None and not charge_profile_requires_istem_fbr(booking.charge_profile):
        return Response(
            {"error": "I-STEM FBR review does not apply to this booking."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    equipment = booking.equipment
    if not _user_can_act_as_oic_for_equipment(request.user, equipment):
        return Response(
            {"error": "Only the Officer in Charge for this equipment (or an administrator) can verify FBR."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if booking.istem_fbr_status != IstemFbrStatus.PENDING_OIC:
        return Response(
            {"error": "No FBR is pending verification for this booking."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    action = (request.data.get("action") if isinstance(request.data, dict) else None) or ""
    action = str(action).strip().lower()
    if action == "execute":
        booking.istem_fbr_status = IstemFbrStatus.EXECUTED
        booking.istem_fbr_executed_at = timezone.now()
        booking.istem_fbr_verified_by = request.user
        booking.istem_fbr_invalid_reason = None
        booking.save(
            update_fields=[
                "istem_fbr_status",
                "istem_fbr_executed_at",
                "istem_fbr_verified_by_id",
                "istem_fbr_invalid_reason",
                "updated_at",
            ]
        )
        create_booking_event(
            booking=booking,
            event_type=BookingEventType.COMMENT,
            created_by=request.user,
            comment="I-STEM FBR marked as verified. User may download results after completing other requirements (e.g. rating).",
            send_notification=True,
        )
        return Response({"message": "FBR marked as verified.", "booking": BookingSerializer(booking).data})
    if action == "invalidate":
        reason = (request.data.get("reason") if isinstance(request.data, dict) else None) or ""
        reason = str(reason).strip()
        if not reason:
            return Response(
                {"error": "A reason is required when marking the FBR as invalid."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        booking.istem_fbr_status = IstemFbrStatus.INVALID
        booking.istem_fbr_invalid_reason = reason[:4000]
        booking.istem_fbr_executed_at = None
        booking.istem_fbr_verified_by = request.user
        booking.save(
            update_fields=[
                "istem_fbr_status",
                "istem_fbr_invalid_reason",
                "istem_fbr_executed_at",
                "istem_fbr_verified_by_id",
                "updated_at",
            ]
        )
        create_booking_event(
            booking=booking,
            event_type=BookingEventType.COMMENT,
            created_by=request.user,
            comment=f"I-STEM FBR marked invalid. Reason: {reason}",
            send_notification=True,
        )
        return Response({"message": "User has been notified to correct the FBR.", "booking": BookingSerializer(booking).data})
    return Response(
        {"error": 'Invalid action. Use "execute" or "invalidate".'},
        status=status.HTTP_400_BAD_REQUEST,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_results_download(request, booking_id):
    """Download all result files for a booking as a single ZIP. ZIP has one root folder named
    with the booking ID; all files and subfolders are inside it.
    """
    try:
        booking = Booking.objects.select_related("equipment", "charge_profile").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response(
            {"error": "Booking not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if booking.user != request.user and not check_operator_permission(request.user):
        return Response(
            {"error": "You don't have permission to download this booking's results."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Enforce rating before results download for booking user (operators/admin can bypass).
    if booking.user == request.user:
        if booking.status != BookingStatus.COMPLETED:
            return Response(
                {"error": "Results can be downloaded only after the booking is completed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if getattr(booking.equipment, "user_rating_enabled", True) and (booking.rating is None or booking.rating_removed):
            return Response(
                {"error": "Rating required. Please submit your rating first, then download results."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if _booking_requires_istem_fbr_results_block(booking):
            return Response(
                {
                    "error": (
                        "Your I-STEM Facility Booking Record (FBR) is not verified yet. "
                        "Complete your booking on https://www.istem.gov.in/, enter the FBR number here, "
                        "and wait for the Officer in Charge to verify it before downloading results."
                    ),
                    "code": "istem_fbr_not_executed",
                    "istem_portal_url": _booking_istem_portal_url(booking),
                },
                status=status.HTTP_403_FORBIDDEN,
            )

    virtual_booking_id = (booking.virtual_booking_id or "").strip()
    if not virtual_booking_id:
        return Response(
            {"error": "Booking has no virtual ID."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    client, bucket, keys_with_names = _get_s3_client_and_results_keys(virtual_booking_id)
    if not client or not keys_with_names:
        return Response(
            {"error": "No result files found for this booking."},
            status=status.HTTP_404_NOT_FOUND,
        )

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for key, relative_name in keys_with_names:
            arcname = f"{virtual_booking_id}/{relative_name}"
            try:
                resp = client.get_object(Bucket=bucket, Key=key)
                body = resp.get("Body")
                if body:
                    zf.writestr(arcname, body.read())
            except Exception as e:
                logger.warning("S3 get_object %s failed: %s", key, e)
                continue

    zip_buffer.seek(0)
    filename = f"Results_{virtual_booking_id}.zip"
    response = HttpResponse(zip_buffer.getvalue(), content_type="application/zip")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_results(request, booking_id):
    """List result files for a booking from S3 path Results/ (all folders and subfolders).
    Searches for any path segment equal to virtual_booking_id, e.g. Results/{id}/,
    Results/2026/Jan/{id}/, Results/lab1/run1/{id}/. Returns exists=True and presigned
    download URLs when any matching objects are found; otherwise exists=False.
    """
    try:
        booking = Booking.objects.select_related("equipment", "charge_profile").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response(
            {"error": "Booking not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if booking.user != request.user and not check_operator_permission(request.user):
        return Response(
            {"error": "You don't have permission to view this booking's results."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    # Enforce rating before results listing for booking user (operators/admin can bypass).
    if booking.user == request.user:
        if booking.status != BookingStatus.COMPLETED:
            return Response(
                {"exists": False, "virtual_booking_id": None, "files": [], "error": "Booking is not completed yet."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if getattr(booking.equipment, "user_rating_enabled", True) and (booking.rating is None or booking.rating_removed):
            return Response(
                {"exists": False, "virtual_booking_id": (booking.virtual_booking_id or "").strip() or None, "files": [], "error": "Rating required. Please submit your rating first, then view results."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if _booking_requires_istem_fbr_results_block(booking):
            return Response(
                {
                    "exists": False,
                    "virtual_booking_id": (booking.virtual_booking_id or "").strip() or None,
                    "files": [],
                    "error": (
                        "I-STEM FBR is not executed yet. Enter your FBR from https://www.istem.gov.in/ "
                        "and wait for the Officer in Charge to mark it executed before results are available."
                    ),
                    "code": "istem_fbr_not_executed",
                    "istem_portal_url": _booking_istem_portal_url(booking),
                },
                status=status.HTTP_403_FORBIDDEN,
            )

    virtual_booking_id = (booking.virtual_booking_id or "").strip()
    if not virtual_booking_id:
        return Response(
            {"exists": False, "virtual_booking_id": None, "files": []},
            status=status.HTTP_200_OK,
        )

    success, files = _list_booking_result_files_from_s3(virtual_booking_id)
    if success is None:
        return Response(
            {"exists": False, "virtual_booking_id": virtual_booking_id, "files": []},
            status=status.HTTP_200_OK,
        )
    exists = len(files) > 0
    # First time we see results: schedule notify after this response returns (push + email must not delay JSON).
    if exists and booking.results_available_notified_at is None:
        bid = int(booking.booking_id)
        transaction.on_commit(lambda b=bid: _schedule_notify_user_results_available(b))
    return Response(
        {"exists": exists, "virtual_booking_id": virtual_booking_id, "files": files},
        status=status.HTTP_200_OK,
    )


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_booking_events(request, booking_id):
    """List all events for a specific booking.
    
    Args:
        booking_id: Primary key of the booking
        
    Returns:
        Response: List of booking events
    """
    try:
        booking = Booking.objects.get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response(
            {"error": "Booking not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check if user has permission to view this booking
    if booking.user != request.user and not check_operator_permission(request.user):
        return Response(
            {"error": "You don't have permission to view this booking's events."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    events = BookingEvent.objects.filter(booking=booking).select_related("created_by", "booking").order_by('-created_at')
    serializer = BookingEventSerializer(events, many=True)
    
    return Response(
        {
            "booking_id": booking_display_id_for_email(booking),
            "real_booking_id": booking_id,
            "events": serializer.data,
            "count": len(serializer.data),
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_booking_event_comment(request, booking_id):
    """Add a comment/event to a booking.
    
    Args:
        booking_id: Primary key of the booking
        
    Request Body:
        {
            "comment": "Optional comment text",
            "event_type": "COMMENT" (optional, defaults to COMMENT),
            "send_notification": true (optional, defaults to true)
        }
        
    Returns:
        Response: Created booking event
    """
    try:
        booking = Booking.objects.get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response(
            {"error": "Booking not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    
    # Check if user has permission to add events to this booking
    if booking.user != request.user and not check_operator_permission(request.user):
        return Response(
            {"error": "You don't have permission to add events to this booking."},
            status=status.HTTP_403_FORBIDDEN,
        )
    
    comment = request.data.get('comment', '')
    event_type = request.data.get('event_type', BookingEventType.COMMENT)
    send_notification = request.data.get('send_notification', True)
    
    if not comment and event_type == BookingEventType.COMMENT:
        return Response(
            {"error": "Comment is required for COMMENT event type."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    
    # Create the event
    from .booking_events import create_booking_event
    event = create_booking_event(
        booking=booking,
        event_type=event_type,
        created_by=request.user,
        comment=comment,
        send_notification=send_notification,
    )
    
    serializer = BookingEventSerializer(event)
    return Response(
        {
            "message": "Booking event created successfully.",
            "event": serializer.data,
        },
        status=status.HTTP_201_CREATED,
    )


# User types that can set "Sample Sent" (student, faculty, all external) — not Admin/OIC/Lab/Finance
def _can_set_sample_sent(user):
    """True if user is student, faculty, or external (can mark Sample Sent)."""
    # helper for sample sent role check
    ut = (getattr(user, 'user_type', None) or '').strip()
    return ut in UserType.get_internal_user_codes() or ut in UserType.get_external_user_codes()


def _safe_results_path_component(value: str, fallback: str) -> str:
    """Sanitize a folder-name segment for cross-platform filesystem safety."""
    raw = (value or "").strip()
    if not raw:
        raw = fallback
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or fallback


def _resolve_in_analysis_results_folder_spec(booking) -> dict:
    """
    Resolve the intended In Analysis results folder path for the operator's local PC.

    Structure (under equipment results_base_location):
    Base -> Equipment Code -> Internal/External -> Year -> Department -> User -> Virtual Booking ID

    Folder creation happens in the browser on the lab PC (File System Access API),
    not on the Django server.
    """
    from pathlib import Path

    from django.contrib.auth import get_user_model

    from iic_booking.users.models import Department

    equipment = getattr(booking, "equipment", None)
    if equipment is None and getattr(booking, "equipment_id", None):
        from iic_booking.equipment.models import Equipment

        equipment = (
            Equipment.objects.filter(pk=booking.equipment_id)
            .only("equipment_id", "code", "results_base_location")
            .first()
        )
    user = getattr(booking, "user", None)
    if user is None and getattr(booking, "user_id", None):
        user = (
            get_user_model()
            .objects.filter(pk=booking.user_id)
            .only("id", "name", "email", "user_type", "department_id")
            .first()
        )

    base_location = (getattr(equipment, "results_base_location", None) or r"D:\Results").strip()
    # Normalize mixed separators / escaped backslashes from UI saves.
    base_location = base_location.replace("\\\\", "\\").strip().strip('"').strip("'")
    if not base_location:
        base_location = r"D:\Results"

    equipment_code = _safe_results_path_component(getattr(equipment, "code", "") or "Equipment", "Equipment")
    user_type_label = "External" if UserType.is_external_user(getattr(user, "user_type", None)) else "Internal"
    year_label = str(timezone.now().year)

    # Fetch only the department name so folder creation does not depend on every Department column.
    department_name = ""
    department_id = getattr(user, "department_id", None) if user else None
    if department_id:
        department_name = (
            Department.objects.filter(pk=department_id).values_list("name", flat=True).first() or ""
        )
    department_label = _safe_results_path_component(department_name or "Unknown Department", "Unknown Department")
    user_label = _safe_results_path_component(
        getattr(user, "name", "") or getattr(user, "email", "") or "Unknown User",
        "Unknown User",
    )
    booking_label = _safe_results_path_component(
        getattr(booking, "virtual_booking_id", "") or f"BOOKING-{getattr(booking, 'booking_id', 'NA')}",
        "Booking",
    )

    segments = [
        equipment_code,
        user_type_label,
        year_label,
        department_label,
        user_label,
        booking_label,
    ]
    full_path = str(Path(base_location).joinpath(*segments))
    # Prefer Windows-style display for lab PCs even when API runs on Linux.
    display_path = full_path.replace("/", "\\")
    relative_path = "\\".join(segments)
    logger.info(
        "In Analysis results folder spec booking_id=%s path=%s",
        getattr(booking, "booking_id", None),
        display_path,
    )
    return {
        "results_base_location": base_location.replace("/", "\\"),
        "segments": segments,
        "relative_path": relative_path,
        "suggested_full_path": display_path,
        "virtual_booking_id": getattr(booking, "virtual_booking_id", "") or "",
    }


def _build_in_analysis_results_folder(booking) -> str:
    """
    Backward-compatible helper: return the suggested local path string only.
    Does not create folders on the server.
    """
    return _resolve_in_analysis_results_folder_spec(booking)["suggested_full_path"]


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def booking_sample_trace(request, booking_id):
    """Get sample/slot tracing timeline for a booking (arrow-style display)."""
    try:
        booking = Booking.objects.get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    if booking.user != request.user and not check_operator_permission(request.user):
        if not _finance_user_can_access_booking_for_workflow(request.user, booking):
            return Response({"error": "You don't have permission to view this booking."}, status=status.HTTP_403_FORBIDDEN)
    events = booking.sample_trace_events.select_related("created_by").prefetch_related('reply_attachments').order_by('created_at')
    return Response(
        {"sample_trace": BookingSampleTraceSerializer(events, many=True, context={"request": request}).data},
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def set_booking_sample_status(request, booking_id):
    """Set a sample-trace status for a booking.
    - Sample Sent: only booking user if they are student/faculty/external; optional sample_identifiers.
    - Held at Office, Forwarded to Lab, Sample Accepted, Sample Rejected, Processing, Completed, Returned, Archived, Disposed: only Admin, Officer In Charge, Lab Incharge.
    - When status is Completed (Analyzed), a PENDING or BOOKED booking is automatically marked COMPLETED (same as Actions → Complete), with completion email to the user.
    Body: { "status": "...", "sample_identifiers": "", "tracking_id": "", "reason": "" }
    Reason is mandatory for SAMPLE_REJECTED and HELD_AT_OFFICE.
    """
    try:
        booking = Booking.objects.select_related("equipment", "user").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

    status_value = (request.data.get('status') or '').strip().upper()
    disallowed_manual = {SampleTraceStatus.NOT_UTILIZED, SampleTraceStatus.OP_UNAVAILABLE}
    valid = [s[0] for s in SampleTraceStatus.choices if s[0] not in disallowed_manual]
    if status_value not in valid:
        return Response(
            {"error": f"Invalid status. Must be one of: {', '.join(valid)}"},
            status=status.HTTP_400_BAD_REQUEST,
        )

    reason_required_statuses = (SampleTraceStatus.SAMPLE_REJECTED, SampleTraceStatus.HELD_AT_OFFICE)
    if status_value in reason_required_statuses:
        reason = (request.data.get('reason') or '').strip()
        if not reason:
            return Response(
                {"error": "A reason is required for this status."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    else:
        reason = ''

    if status_value == SampleTraceStatus.SAMPLE_SENT:
        if request.user != booking.user:
            return Response(
                {"error": "Only the booking user can mark Sample Sent."},
                status=status.HTTP_403_FORBIDDEN,
            )
        if not _can_set_sample_sent(request.user):
            return Response(
                {"error": "Only Student, Faculty, or External users can mark Sample Sent."},
                status=status.HTTP_403_FORBIDDEN,
            )
    else:
        if _user_is_accounts_finance_user(request.user):
            if not _finance_user_can_access_booking_for_workflow(request.user, booking):
                return Response({"error": "You don't have permission to update this booking."}, status=status.HTTP_403_FORBIDDEN)
            if status_value not in (SampleTraceStatus.HELD_AT_OFFICE, SampleTraceStatus.FORWARDED_TO_LAB):
                return Response(
                    {"error": "Accounts In Charge can only set Held at Office or Forwarded to Lab."},
                    status=status.HTTP_403_FORBIDDEN,
                )
            if booking.sample_trace_events.filter(status=SampleTraceStatus.RETURNED).exists():
                return Response(
                    {"error": "Sample lifecycle is complete; return shipment has been recorded."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        elif not check_operator_permission(request.user):
            return Response(
                {"error": "Only Admin, Officer In Charge, or Lab Incharge can set this status."},
                status=status.HTTP_403_FORBIDDEN,
            )

    sample_identifiers = (request.data.get('sample_identifiers') or '').strip() if status_value == SampleTraceStatus.SAMPLE_SENT else ''
    tracking_id = (request.data.get('tracking_id') or '').strip() if status_value == SampleTraceStatus.SAMPLE_SENT else ''
    in_analysis_folder_spec = None
    if status_value == SampleTraceStatus.PROCESSING:
        try:
            in_analysis_folder_spec = _resolve_in_analysis_results_folder_spec(booking)
        except Exception as e:
            logger.exception("Failed resolving In Analysis folder path for booking %s: %s", booking.booking_id, e)
            return Response(
                {"error": f'Unable to resolve the "In Analysis" folder path: {str(e)}'},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
    event = BookingSampleTrace.objects.create(
        booking=booking,
        status=status_value,
        sample_identifiers=sample_identifiers,
        tracking_id=tracking_id,
        reason=reason,
        results_folder_path=(in_analysis_folder_spec or {}).get("suggested_full_path") or "",
        created_by=request.user,
    )
    if status_value == SampleTraceStatus.SAMPLE_REJECTED:
        rejection_reason = reason or "Sample rejected."
        auto_refund_notes = f"Auto-refund issued: sample rejected. Reason: {rejection_reason}"
        try:
            refund_booking_internal(booking, auto_refund_notes, request.user)
        except Exception as e:
            logger.warning(
                "Auto-refund on SAMPLE_REJECTED failed for booking %s: %s",
                booking.booking_id,
                e,
            )
            return Response(
                {
                    "error": "Sample status saved but auto-refund failed.",
                    "detail": str(e),
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
    booking_marked_completed_via_analyzed = False
    if status_value == SampleTraceStatus.COMPLETED:
        try:
            booking_marked_completed_via_analyzed = _try_complete_booking_on_sample_analyzed(booking, request.user)
        except Exception as e:
            logger.exception(
                "Auto-complete booking on sample Analyzed failed for booking %s",
                booking.booking_id,
            )
            events = booking.sample_trace_events.all().order_by("created_at")
            return Response(
                {
                    "error": "Sample status was saved but the booking could not be marked completed.",
                    "detail": str(e),
                    "sample_trace": BookingSampleTraceSerializer(
                        events, many=True, context={"request": request}
                    ).data,
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )
    events = booking.sample_trace_events.all().order_by('created_at')
    success_message = "Sample status updated."
    if status_value == SampleTraceStatus.SAMPLE_REJECTED:
        success_message = "Sample status updated. Full refund has been issued to the user's wallet and email notification has been sent."
    elif booking_marked_completed_via_analyzed:
        success_message = "Sample status updated. Booking marked as completed; the user was notified by email."
    if status_value == SampleTraceStatus.DISPOSED:
        # Notify the booking user by email when a sample is disposed.
        try:
            from iic_booking.communication.service import CommunicationService
            from iic_booking.communication.utils import booking_display_id_for_email
            equipment = getattr(booking, "equipment", None)
            ctx = {
                "user_name": getattr(booking.user, "name", None) or getattr(booking.user, "email", None) or "User",
                "user_email": getattr(booking.user, "email", "") or "",
                "equipment_name": (getattr(equipment, "name", None) or getattr(equipment, "code", None) or "Equipment") if equipment else "Equipment",
                "booking_id": booking_display_id_for_email(booking),
                "disposed_at": timezone.localtime(event.created_at).strftime("%d %b %Y, %I:%M %p"),
                "remarks": reason or "",
            }
            CommunicationService.send_email(
                recipient=booking.user,
                template="sample_disposed_email",
                template_context=ctx,
                created_by=request.user,
                metadata={"booking_id": int(getattr(booking, "booking_id", 0) or 0), "sample_trace_event_id": int(event.id)},
            )
        except Exception as e:
            logger.warning("Failed to send disposed email for booking %s: %s", booking.booking_id, e)
        success_message = "Sample status updated. Disposal email has been sent to the user."
    payload = {
        "message": success_message,
        "sample_trace": BookingSampleTraceSerializer(events, many=True, context={"request": request}).data,
    }
    if booking_marked_completed_via_analyzed:
        payload["booking"] = BookingSerializer(booking, context={"request": request}).data
    if status_value == SampleTraceStatus.PROCESSING and in_analysis_folder_spec:
        payload["in_analysis_folder_path"] = in_analysis_folder_spec["suggested_full_path"]
        payload["in_analysis_folder_opened"] = False
        payload["create_on_client"] = True
        payload["results_base_location"] = in_analysis_folder_spec["results_base_location"]
        payload["results_folder_segments"] = in_analysis_folder_spec["segments"]
        payload["results_folder_relative_path"] = in_analysis_folder_spec["relative_path"]
        payload["virtual_booking_id"] = in_analysis_folder_spec.get("virtual_booking_id") or getattr(
            booking, "virtual_booking_id", ""
        )
    return Response(payload, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def ensure_booking_results_folder(request, booking_id):
    """
    Return the In Analysis results folder path recipe for local (lab PC) creation.
    Staff only (admin / OIC / lab incharge). Creation is done in the browser, not on the server.
    """
    if not check_operator_permission(request.user):
        return Response(
            {"error": "Only Admin, Officer In Charge, or Lab Incharge can create results folders."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        booking = Booking.objects.select_related("equipment", "user").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

    try:
        folder_spec = _resolve_in_analysis_results_folder_spec(booking)
    except Exception as e:
        logger.exception("ensure_booking_results_folder failed for booking %s", booking_id)
        return Response(
            {"error": f"Unable to resolve results folder path: {str(e)}"},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    folder_path = folder_spec["suggested_full_path"]
    processing_event = (
        BookingSampleTrace.objects.filter(booking=booking, status=SampleTraceStatus.PROCESSING)
        .order_by("-created_at")
        .first()
    )
    if processing_event and folder_path and processing_event.results_folder_path != folder_path:
        processing_event.results_folder_path = folder_path
        processing_event.save(update_fields=["results_folder_path"])

    return Response(
        {
            "message": "Results folder path ready for local creation.",
            "in_analysis_folder_path": folder_path,
            "in_analysis_folder_opened": False,
            "create_on_client": True,
            "exists": False,
            "virtual_booking_id": folder_spec.get("virtual_booking_id") or booking.virtual_booking_id,
            "results_base_location": folder_spec["results_base_location"],
            "results_folder_segments": folder_spec["segments"],
            "results_folder_relative_path": folder_spec["relative_path"],
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def set_booking_return_shipping_tracking(request, booking_id: int):
    """
    Accounts In Charge (finance) or admin: set return-shipping carrier + tracking for external sample returns,
    email the booking user, and add Sample Returned to the lifecycle (final for this workflow).

    Body (Accounts): shipping_company: DTDC | BLUE_DART | INDIAN_SPEED_POST | OTHER,
    other_company_name (required if OTHER), tracking_number.

    Body (admin, legacy): tracking_id — single line; still creates Sample Returned if not already present.
    """
    try:
        booking = Booking.objects.select_related("user", "equipment").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

    ut = str(getattr(request.user, "user_type", "") or "").strip().lower()
    if ut not in ("finance", "admin"):
        return Response({"error": "Not allowed."}, status=status.HTTP_403_FORBIDDEN)

    if ut == "finance" and not _finance_user_can_access_booking_for_workflow(request.user, booking):
        return Response({"error": "Not allowed."}, status=status.HTTP_403_FORBIDDEN)

    if not getattr(booking, "sample_return_after_analysis", False):
        return Response({"error": "This booking does not require return shipment."}, status=status.HTTP_400_BAD_REQUEST)

    analyzed = booking.status == BookingStatus.COMPLETED or booking.sample_trace_events.filter(status=SampleTraceStatus.COMPLETED).exists()
    if not analyzed:
        return Response({"error": "Return shipment can be set only after analysis is completed."}, status=status.HTTP_400_BAD_REQUEST)

    if booking.sample_trace_events.filter(status=SampleTraceStatus.RETURNED).exists():
        return Response(
            {"error": "Return shipment has already been finalized; tracking cannot be changed."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    shipping_company_raw = (request.data.get("shipping_company") or "").strip().upper().replace(" ", "_")
    other_company_name = (request.data.get("other_company_name") or "").strip()
    tracking_number = (request.data.get("tracking_number") or "").strip()
    legacy_tracking = str(request.data.get("tracking_id") or "").strip()

    carrier_map = {
        "DTDC": "DTDC",
        "BLUE_DART": "Blue Dart",
        "INDIAN_SPEED_POST": "Indian Speed Post",
        "OTHER": None,
    }

    carrier_label = ""
    tracking_stored = ""

    if shipping_company_raw:
        if shipping_company_raw not in carrier_map:
            return Response(
                {"error": "Invalid shipping_company. Use DTDC, BLUE_DART, INDIAN_SPEED_POST, or OTHER."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if shipping_company_raw == "OTHER":
            if not other_company_name:
                return Response(
                    {"error": "other_company_name is required when shipping_company is OTHER."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            carrier_label = other_company_name[:128]
        else:
            carrier_label = carrier_map[shipping_company_raw] or ""
        if not tracking_number:
            return Response({"error": "tracking_number is required."}, status=status.HTTP_400_BAD_REQUEST)
        tracking_stored = tracking_number
    elif legacy_tracking:
        if ut == "finance":
            return Response(
                {"error": "shipping_company and tracking_number are required for Accounts In Charge."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        tracking_stored = legacy_tracking
    else:
        return Response(
            {"error": "shipping_company and tracking_number are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    trace_line = f"{carrier_label}: {tracking_stored}" if carrier_label else tracking_stored

    with transaction.atomic():
        booking.return_shipping_company = carrier_label
        booking.return_shipping_tracking_id = tracking_stored
        booking.return_shipping_tracking_updated_at = timezone.now()
        booking.save(
            update_fields=[
                "return_shipping_company",
                "return_shipping_tracking_id",
                "return_shipping_tracking_updated_at",
            ]
        )
        BookingSampleTrace.objects.create(
            booking=booking,
            status=SampleTraceStatus.RETURNED,
            sample_identifiers="",
            tracking_id=trace_line[:1024] if len(trace_line) > 1024 else trace_line,
            reason="",
            created_by=request.user,
        )

    try:
        send_return_shipping_tracking_email(
            booking=booking,
            carrier=carrier_label or "—",
            tracking_number=tracking_stored,
        )
    except Exception:
        logger.exception("Failed to send return-shipping tracking email (booking_id=%s)", booking.booking_id)

    return Response(
        {"message": "Return shipping updated. Sample lifecycle marked as returned."},
        status=status.HTTP_200_OK,
    )


@api_view(["PATCH", "PUT", "POST"])
@permission_classes([IsAuthenticated])
def set_sample_trace_reply(request, booking_id, event_id):
    """Set the booking user's reply (text and optional files) to a Sample Rejected or Held at Office reason. Only the booking user can set. Accepts JSON (reply only) or multipart/form-data (reply + files)."""
    try:
        booking = Booking.objects.get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    if request.user != booking.user:
        return Response(
            {"error": "Only the booking user can add a reply."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        event = BookingSampleTrace.objects.get(id=event_id, booking=booking)
    except BookingSampleTrace.DoesNotExist:
        return Response({"error": "Sample trace event not found."}, status=status.HTTP_404_NOT_FOUND)
    if event.status not in (SampleTraceStatus.SAMPLE_REJECTED, SampleTraceStatus.HELD_AT_OFFICE):
        return Response(
            {"error": "Reply is only allowed for Sample Rejected or Held at Office status."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if request.content_type and "multipart/form-data" in request.content_type:
        reply = (request.POST.get("reply") or "").strip()
        files = request.FILES.getlist("files") or request.FILES.getlist("files[]")
    else:
        reply = (request.data.get("reply") or "").strip()
        files = []
    event.user_reply = reply
    event.save(update_fields=["user_reply"])
    for f in files:
        att = BookingSampleTraceReplyAttachment(
            sample_trace=event,
            file=f,
            original_name=getattr(f, "name", "") or "",
            uploaded_by=request.user,
        )
        att.save()
    events = booking.sample_trace_events.prefetch_related('reply_attachments').order_by("created_at")
    return Response(
        {"message": "Reply saved.", "sample_trace": BookingSampleTraceSerializer(events, many=True, context={"request": request}).data},
        status=status.HTTP_200_OK,
    )


def _clean_single_input_value(value):
    """Convert a single submitted value to a type safe for JSON storage (int, float, bool, str)."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, list):
        return value  # e.g. MULTI_SELECT
    if isinstance(value, str):
        value_stripped = value.strip()
        value_lower = value_stripped.lower()
        if value_lower == '':
            return None
        try:
            if '.' in value_lower:
                return float(value_lower)
            return int(value_lower)
        except (ValueError, TypeError):
            if value_lower in ('true', 'yes'):
                return True
            if value_lower in ('false', 'no'):
                return False
            return value
    return value


def _recalculate_booking_charge_and_adjust_wallet(request, booking):
    """Recalculate charge for a BOOKED booking after input_values update. Saves old charge, updates
    booking with new charge and sets charge_recalculation_pending_amount (negative=refund, positive=extra to pay).
    Does NOT debit/credit wallet here; user/admin must click Refund or Pay Now. Sends email to user and
    Supervisor with summary and breakup. Returns dict with charge_recalculation_summary for API response.
    """
    from iic_booking.users.repositories.wallet_repository import WalletRepository

    equipment = booking.equipment
    charge_profile = booking.charge_profile
    if not charge_profile or not equipment:
        return {}

    class ChargeProfileWithType:
        def __init__(self, cp, eq):
            self.equipment = cp.equipment
            self.user_type = cp.user_type
            self.is_active = cp.is_active
            self.primary_unit_charge = cp.primary_unit_charge
            self.secondary_unit_charge = cp.secondary_unit_charge
            self.breakpoint = cp.breakpoint
            self.time_formula = cp.time_formula
            self.pricing_profile = getattr(cp, "pricing_profile", ChargeProfilePricingProfile.STANDARD)
            self.profile_type = getattr(eq, "profile_type", None)

    safe_input_values = build_safe_input_values_for_charge_calculation(booking.input_values, equipment=equipment)
    from .print_3d_views import apply_print_analysis_to_input_values
    safe_input_values = apply_print_analysis_to_input_values(booking, safe_input_values)
    charge_profile_with_type = ChargeProfileWithType(charge_profile, equipment)

    calculated_time_minutes = TimeCalculationEngine.calculate_time(
        charge_profile_with_type,
        safe_input_values,
        slot_duration_minutes=equipment.slot_duration_minutes,
    )
    new_charge, charge_breakdown = ChargeCalculationEngine.calculate_charge(
        charge_profile_with_type,
        safe_input_values,
        calculated_time_minutes,
        selected_parameters=booking.selected_parameters,
    )
    new_charge = new_charge.quantize(Decimal("0.01"))
    if UserType.is_external_user(getattr(booking.user, "user_type", None)):
        gst_percent = get_external_gst_percent()
        if gst_percent > 0:
            gst_amount = (new_charge * gst_percent / Decimal("100")).quantize(Decimal("0.01"))
            new_charge = (new_charge + gst_amount).quantize(Decimal("0.01"))
            charge_breakdown = list(charge_breakdown) + [
                {"description": f"GST ({gst_percent}%)", "amount": float(gst_amount)},
            ]
    # Keep a stable "previous charge" baseline while a recalculation is still pending.
    # If there is already a pending delta from an earlier edit, baseline is:
    #   baseline = current_total_charge - current_pending_delta
    # This prevents "Previous charge" from shifting on every subsequent edit
    # until Refund/Pay Now clears the pending amount.
    current_total_charge = booking.total_charge
    current_pending_delta = booking.charge_recalculation_pending_amount
    if current_pending_delta is not None:
        previous_charge = (current_total_charge - current_pending_delta).quantize(Decimal("0.01"))
    else:
        previous_charge = current_total_charge

    diff = new_charge - previous_charge
    pending_amount = diff if diff != 0 else None  # negative = refund, positive = extra to pay

    with transaction.atomic():
        booking.total_time_minutes = calculated_time_minutes
        booking.total_charge = new_charge
        booking.charge_breakdown = charge_breakdown
        booking.charge_recalculation_pending_amount = pending_amount
        booking.save(update_fields=[
            "total_time_minutes", "total_charge", "charge_breakdown", "charge_recalculation_pending_amount"
        ])

        wallet_target, has_wallet = WalletRepository.get_booking_wallet_target(
            booking.user, getattr(equipment, "internal_department", None)
        )
        # No automatic debit/credit; user must click Refund or Pay Now

        metadata = {
            "previous_charge": str(previous_charge),
            "new_charge": str(new_charge),
            "total_time_minutes": calculated_time_minutes,
            "charge_breakdown": charge_breakdown,
        }
        if pending_amount is not None:
            if pending_amount < 0:
                metadata["amount_credited"] = str(abs(pending_amount))
                metadata["refund_amount"] = str(abs(pending_amount))
            else:
                metadata["amount_debited"] = str(pending_amount)
                metadata["extra_amount"] = str(pending_amount)

        comment = (
            f"Charges recalculated: previous ₹{previous_charge:.2f}, new ₹{new_charge:.2f}."
        )
        if pending_amount is not None:
            if pending_amount < 0:
                comment += f" Refund of ₹{abs(pending_amount):.2f} pending — click Refund to credit wallet."
            else:
                comment += f" Extra ₹{pending_amount:.2f} to pay — click Pay Now to debit wallet."
        else:
            comment += " No change in amount."

        create_booking_event(
            booking=booking,
            event_type=BookingEventType.CHARGE_RECALCULATED,
            created_by=request.user,
            comment=comment,
            metadata=metadata,
            send_notification=True,
        )

    summary = {
        "previous_charge": str(previous_charge),
        "new_charge": str(new_charge),
        "refund_amount": str(abs(pending_amount)) if pending_amount is not None and pending_amount < 0 else None,
        "extra_amount": str(pending_amount) if pending_amount is not None and pending_amount > 0 else None,
    }
    return summary


@api_view(["PATCH", "PUT"])
@permission_classes([IsAuthenticated])
def update_booking_input_values(request, booking_id):
    """Update input_values for a booking. When equipment has 'enable_charge_recalculation' and booking
    is BOOKED, only fields marked as 'editing_required' can be
    updated (plus universal 'comments'). Only until the booking status is Complete. On save with charge recalculation enabled,
    charges are recalculated and wallet is debited/credited for the difference; user is notified by email.
    Request body: { "input_values": { "A": 1, "B": "text", ... } }
    """
    try:
        booking = Booking.objects.select_related("equipment", "charge_profile").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response(
            {"error": "Booking not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if booking.user != request.user and not check_operator_permission(request.user):
        return Response(
            {"error": "You don't have permission to update this booking."},
            status=status.HTTP_403_FORBIDDEN,
        )

    if booking.source_booking_id is not None:
        return Response(
            {"error": "Parameters cannot be modified for repeat sample bookings."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # For internal/external users: allow editing only when booking is BOOKED and not yet processed
    if not check_operator_permission(request.user):
        if booking.status != BookingStatus.BOOKED:
            return Response(
                {"error": "User input editing is only available for bookings in Booked status."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        latest_trace = (
            BookingSampleTrace.objects.filter(booking=booking).order_by("-created_at").first()
        )
        if latest_trace and latest_trace.status in (
            SampleTraceStatus.PROCESSING,
            SampleTraceStatus.COMPLETED,
        ):
            return Response(
                {"error": "User input editing is not allowed once the sample is marked as processed (Processing or Completed)."},
                status=status.HTTP_400_BAD_REQUEST,
            )

    if booking.status == BookingStatus.COMPLETED:
        return Response(
            {"error": "Cannot edit user inputs after the booking is completed."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if booking.status in (
        BookingStatus.REFUNDED,
        BookingStatus.ABSENT,
        BookingStatus.BOOKING_NOT_UTILIZED,
    ):
        return Response(
            {"error": "User inputs cannot be edited for refunded, operator unavailable, or booking not utilized bookings."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    equipment = booking.equipment
    enable_recalc = getattr(equipment, "enable_charge_recalculation", False) and booking.status == BookingStatus.BOOKED

    editable_keys = set(
        DynamicInputField.objects.filter(
            equipment_id=booking.equipment_id,
            editing_required=True,
        ).values_list("field_key", flat=True)
    )

    # Allow field_key and field_key_elements (e.g. for PERIODIC_TABLE)
    allowed_keys = set(editable_keys)
    # Universal comments field for all equipments.
    allowed_keys.add("comments")
    for k in list(editable_keys):
        allowed_keys.add(f"{k}_elements")

    raw = request.data.get("input_values") or {}
    if not isinstance(raw, dict):
        return Response(
            {"error": "input_values must be an object."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    current = dict(booking.input_values) if booking.input_values else {}

    # Clients often send the full input_values object; only block changes to non-editable keys.
    disallowed_keys = []
    for key, value in raw.items():
        if key in allowed_keys:
            continue
        new_val = _clean_single_input_value(value)
        old_val = current.get(key)
        if new_val != old_val:
            disallowed_keys.append(key)

    if disallowed_keys:
        return Response(
            {
                "error": "Some input fields are not editable after booking.",
                "non_editable_fields": sorted(disallowed_keys),
                "allowed_fields": sorted(allowed_keys),
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    for key, value in raw.items():
        if key not in allowed_keys:
            continue
        cleaned = _clean_single_input_value(value)
        if cleaned is None and key in current:
            del current[key]
        elif cleaned is not None:
            current[key] = cleaned

    from .calculators import normalize_periodic_table_billable_counts
    current = normalize_periodic_table_billable_counts(equipment, current)

    booking.input_values = current
    booking.save(update_fields=["input_values"])

    # Charge recalculation: when enabled and booking is BOOKED, recalc and set pending (no auto debit/credit)
    if enable_recalc and booking.status == BookingStatus.BOOKED:
        try:
            summary = _recalculate_booking_charge_and_adjust_wallet(request, booking)
            booking.refresh_from_db()
            return Response(
                {
                    "message": "User inputs updated. Charges recalculated.",
                    "booking": BookingSerializer(booking).data,
                    "charge_recalculation_summary": summary,
                },
                status=status.HTTP_200_OK,
            )
        except Exception as e:
            logger.exception("Charge recalculation failed")
            return Response(
                {"error": "Charge recalculation failed: " + str(e), "booking": BookingSerializer(booking).data},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR,
            )

    return Response(
        {"message": "User inputs updated.", "booking": BookingSerializer(booking).data},
        status=status.HTTP_200_OK,
    )


@api_view(["PATCH", "POST"])
@permission_classes([IsAuthenticated])
def update_booking_atmosphere_sensitive_sample(request, booking_id):
    """
    Set or clear atmosphere-sensitive sample on an existing booking.

    Body: { "atmosphere_sensitive_sample": true|false }

    Allowed for the booking owner or lab staff while the booking is BOOKED
    and Sample Accepted has not been recorded yet.
    """
    try:
        booking = Booking.objects.select_related("equipment", "user").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

    if booking.user_id != request.user.id and not check_operator_permission(request.user):
        return Response(
            {"error": "You don't have permission to update this booking."},
            status=status.HTTP_403_FORBIDDEN,
        )

    status_u = (booking.status or "").upper()
    if status_u != BookingStatus.BOOKED:
        return Response(
            {"error": "Atmosphere-sensitive sample can only be changed while the booking is Booked."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if BookingSampleTrace.objects.filter(
        booking=booking, status=SampleTraceStatus.SAMPLE_ACCEPTED
    ).exists():
        return Response(
            {
                "error": "Atmosphere-sensitive sample cannot be changed after Sample Accepted."
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    raw = request.data.get("atmosphere_sensitive_sample") if request.data is not None else None
    if raw is None:
        return Response(
            {"error": "atmosphere_sensitive_sample (true/false) is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if isinstance(raw, bool):
        new_value = raw
    elif isinstance(raw, (int, float)):
        new_value = bool(raw)
    else:
        new_value = str(raw).strip().lower() in ("1", "true", "yes", "y")

    previous = bool(getattr(booking, "atmosphere_sensitive_sample", False))
    if previous == new_value:
        return Response(
            {
                "message": "Atmosphere-sensitive sample unchanged.",
                "booking": BookingSerializer(booking).data,
            },
            status=status.HTTP_200_OK,
        )

    booking.atmosphere_sensitive_sample = new_value
    booking.save(update_fields=["atmosphere_sensitive_sample", "updated_at"])

    from iic_booking.equipment.booking_events import create_booking_event

    create_booking_event(
        booking=booking,
        event_type=BookingEventType.COMMENT,
        created_by=request.user,
        comment=(
            "Marked as atmosphere-sensitive sample (submit at slot start)."
            if new_value
            else "Cleared atmosphere-sensitive sample flag."
        ),
        metadata={
            "atmosphere_sensitive_sample": new_value,
            "previous_atmosphere_sensitive_sample": previous,
        },
        send_notification=False,
    )

    if new_value:
        try:
            from iic_booking.equipment.reports import get_equipment_staff_notify_users
            from iic_booking.communication.service import CommunicationService
            from iic_booking.communication.utils import booking_display_id_for_email as _bdisp

            equipment = booking.equipment
            ref = _bdisp(booking)
            for staff in get_equipment_staff_notify_users(equipment):
                if not staff or not getattr(staff, "email", None):
                    continue
                CommunicationService.send_push_notification(
                    recipient=staff,
                    title="Atmosphere-sensitive sample",
                    message=(
                        f"{ref} — {equipment.name}: sample will be brought at slot start. "
                        "Do not mark Booking Not Utilized before the slot begins."
                    ),
                    metadata={
                        "booking_id": booking.booking_id,
                        "atmosphere_sensitive_sample": True,
                        "notification_type": "warning",
                    },
                )
        except Exception:
            logger.exception(
                "Failed atmosphere-sensitive staff notify for booking %s",
                getattr(booking, "booking_id", None),
            )

    return Response(
        {
            "message": (
                "Atmosphere-sensitive sample enabled."
                if new_value
                else "Atmosphere-sensitive sample disabled."
            ),
            "booking": BookingSerializer(booking).data,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def rate_booking(request, booking_id):
    """Submit or update a rating (1-5 stars) and optional feedback for a booking.
    Only the booking user (student, faculty, external) can rate their own booking.
    Body (new): {
        "on_time_operator_availability": true/false,
        "laboratory_cleanliness_organization": true/false,
        "sample_handling_care": true/false,
        "operator_behaviour_professionalism": true/false,
        "compliance_booking_request_parameters": true/false,
        "feedback": "optional text"
    }
    Overall rating is computed as Yes count (0-5).
    Legacy body still supported: { "rating": 1-5, "feedback": "optional text" }
    """
    from django.utils import timezone
    
    def _parse_bool(v):
        if v is None:
            return None
        if isinstance(v, bool):
            return v
        if isinstance(v, int):
            if v in (0, 1):
                return bool(v)
        if isinstance(v, str):
            s = v.strip().lower()
            if s in ("true", "t", "1", "yes", "y"):
                return True
            if s in ("false", "f", "0", "no", "n"):
                return False
        return "INVALID"

    try:
        booking = Booking.objects.select_related("equipment", "charge_profile").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response(
            {"error": "Booking not found."},
            status=status.HTTP_404_NOT_FOUND,
        )

    if booking.user_id != request.user.id:
        return Response(
            {"error": "Only the booking user can rate this booking."},
            status=status.HTTP_403_FORBIDDEN,
        )

    if not getattr(booking.equipment, "user_rating_enabled", True):
        return Response(
            {"error": "Rating is disabled for this equipment."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if booking.status != BookingStatus.COMPLETED:
        return Response(
            {"error": "You can rate only after the booking is completed."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    # New multi-criteria yes/no rating (preferred)
    criteria_payload = {
        "rating_on_time_operator_availability": _parse_bool(request.data.get("on_time_operator_availability")),
        "rating_laboratory_cleanliness_organization": _parse_bool(request.data.get("laboratory_cleanliness_organization")),
        "rating_sample_handling_care": _parse_bool(request.data.get("sample_handling_care")),
        "rating_operator_behaviour_professionalism": _parse_bool(request.data.get("operator_behaviour_professionalism")),
        "rating_compliance_booking_request_parameters": _parse_bool(request.data.get("compliance_booking_request_parameters")),
    }
    has_any_criteria = any(request.data.get(k) is not None for k in [
        "on_time_operator_availability",
        "laboratory_cleanliness_organization",
        "sample_handling_care",
        "operator_behaviour_professionalism",
        "compliance_booking_request_parameters",
    ])
    if has_any_criteria:
        invalid = [k for k, v in criteria_payload.items() if v == "INVALID"]
        if invalid:
            return Response(
                {"error": f"Invalid yes/no value(s) for: {', '.join(invalid)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        missing = [k for k, v in criteria_payload.items() if v is None]
        if missing:
            return Response(
                {"error": f"Missing required yes/no field(s): {', '.join(missing)}."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        overall = int(sum(1 for v in criteria_payload.values() if v))
        booking.rating = overall
        for field_name, field_val in criteria_payload.items():
            setattr(booking, field_name, field_val)
    else:
        # Legacy star rating fallback (backward compatible)
        rating_val = request.data.get("rating")
        if rating_val is None:
            return Response(
                {"error": "Provide either the 5 yes/no rating fields or legacy rating (1-5)."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            rating_int = int(rating_val)
        except (TypeError, ValueError):
            return Response(
                {"error": "rating must be an integer between 1 and 5."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if rating_int < 1 or rating_int > 5:
            return Response(
                {"error": "rating must be between 1 and 5."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        booking.rating = rating_int

    feedback = request.data.get("feedback")
    if feedback is not None and not isinstance(feedback, str):
        feedback = str(feedback)
    elif feedback is not None:
        feedback = feedback.strip() or None

    booking.rating_feedback = feedback
    booking.rated_at = timezone.now()
    # If rating was removed earlier by admin/OIC, a new submission reinstates it.
    booking.rating_removed = False
    booking.rating_removed_at = None
    booking.rating_removed_by = None
    booking.rating_removed_reason = None
    booking.save(
        update_fields=[
            "rating",
            "rating_on_time_operator_availability",
            "rating_laboratory_cleanliness_organization",
            "rating_sample_handling_care",
            "rating_operator_behaviour_professionalism",
            "rating_compliance_booking_request_parameters",
            "rating_feedback",
            "rated_at",
            "rating_removed",
            "rating_removed_at",
            "rating_removed_by",
            "rating_removed_reason",
        ]
    )

    return Response(
        {
            "message": "Rating submitted.",
            "booking": BookingSerializer(booking).data,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def remove_booking_rating(request, booking_id):
    """Soft-remove a booking rating (admin/OIC/operator-manager only). Keeps an audit trail."""
    from django.utils import timezone

    if not check_operator_permission(request.user):
        return Response(
            {"error": "You don't have permission to remove ratings."},
            status=status.HTTP_403_FORBIDDEN,
        )

    try:
        booking = Booking.objects.select_related("equipment", "user").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)

    if booking.rating is None:
        return Response({"error": "This booking has no rating to remove."}, status=status.HTTP_400_BAD_REQUEST)

    reason = request.data.get("reason")
    if reason is not None and not isinstance(reason, str):
        reason = str(reason)
    if isinstance(reason, str):
        reason = reason.strip() or None

    booking.rating_removed = True
    booking.rating_removed_at = timezone.now()
    booking.rating_removed_by = request.user
    booking.rating_removed_reason = reason
    booking.save(
        update_fields=[
            "rating_removed",
            "rating_removed_at",
            "rating_removed_by",
            "rating_removed_reason",
        ]
    )

    return Response(
        {
            "message": "Rating removed.",
            "booking_id": booking_display_id_for_email(booking),
            "real_booking_id": booking.booking_id,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def process_charge_recalculation_refund(request, booking_id):
    """Process pending refund after charge recalculation: credit the associated wallet with a clear
    description (Refund for equipment name and booking id). Sends email to user and Supervisor.
    """
    from iic_booking.users.repositories.wallet_repository import WalletRepository
    from iic_booking.communication.wallet_notifications import send_sub_wallet_transaction_notifications

    try:
        booking = Booking.objects.select_related("equipment", "user").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response(
            {"error": "Booking not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    if booking.user != request.user and not check_operator_permission(request.user):
        return Response(
            {"error": "You don't have permission to process this refund."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if booking.source_booking_id is not None:
        return Response(
            {"error": "Charge adjustments do not apply to repeat sample bookings."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    pending = booking.charge_recalculation_pending_amount
    if pending is None or pending >= 0:
        return Response(
            {"error": "No pending refund for this booking."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    amount = abs(pending)
    equipment = booking.equipment
    wallet_target, has_wallet = WalletRepository.get_booking_wallet_target(
        booking.user, getattr(equipment, "internal_department", None)
    )
    if not has_wallet or not wallet_target:
        return Response(
            {"error": "No wallet associated with this booking for refund."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    description = f"Refund for {equipment.name} and Booking {booking_display_id_for_email(booking)}"
    description += _student_booking_description_suffix(wallet_target, booking.user)
    description += f" | Ref: {booking_display_id_for_email(booking)}"
    with transaction.atomic():
        txn = wallet_target.credit(amount=amount, description=description, related_user=booking.user)
        booking.charge_recalculation_pending_amount = None
        booking.save(update_fields=["charge_recalculation_pending_amount"])
        send_sub_wallet_transaction_notifications(transaction=txn, booking=booking)
    return Response(
        {"message": "Refund processed. Amount credited to wallet.", "booking": BookingSerializer(booking).data},
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def process_charge_recalculation_pay_now(request, booking_id):
    """Process extra amount to pay after charge recalculation: debit the associated wallet with a
    clear description. Sends email to user and Supervisor.
    """
    from iic_booking.users.repositories.wallet_repository import WalletRepository
    from iic_booking.communication.wallet_notifications import send_sub_wallet_transaction_notifications

    try:
        booking = Booking.objects.select_related("equipment", "user").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response(
            {"error": "Booking not found."},
            status=status.HTTP_404_NOT_FOUND,
        )
    if booking.user != request.user and not check_operator_permission(request.user):
        return Response(
            {"error": "You don't have permission to process this payment."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if booking.source_booking_id is not None:
        return Response(
            {"error": "Charge adjustments do not apply to repeat sample bookings."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    pending = booking.charge_recalculation_pending_amount
    if pending is None or pending <= 0:
        return Response(
            {"error": "No extra amount to pay for this booking."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    amount = pending
    equipment = booking.equipment
    wallet_target, has_wallet = WalletRepository.get_booking_wallet_target(
        booking.user, getattr(equipment, "internal_department", None)
    )
    if not has_wallet or not wallet_target:
        return Response(
            {"error": "No wallet associated with this booking."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    description = f"Additional charge for {equipment.name}, Booking {booking_display_id_for_email(booking)}"
    description += _student_booking_description_suffix(wallet_target, booking.user)
    wallet_target.refresh_from_db()
    from iic_booking.users.wallet_credit_facility import subwallet_booking_balance_ok

    ok_pay, pay_err = subwallet_booking_balance_ok(wallet_target, amount, False)
    if not ok_pay:
        return Response({"error": pay_err or "Insufficient wallet balance."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        with transaction.atomic():
            from iic_booking.users.wallet_credit_facility import subwallet_minimum_balance_after_debit

            txn = wallet_target.debit(
                amount=amount,
                description=description,
                related_user=booking.user,
                minimum_balance_after=subwallet_minimum_balance_after_debit(wallet_target),
            )
            booking.charge_recalculation_pending_amount = None
            booking.save(update_fields=["charge_recalculation_pending_amount"])
            send_sub_wallet_transaction_notifications(transaction=txn, booking=booking)
    except ValueError as e:
        return Response(
            {"error": str(e)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    return Response(
        {"message": "Payment processed. Amount debited from wallet.", "booking": BookingSerializer(booking).data},
        status=status.HTTP_200_OK,
    )


# ============== Repeat sample (redesign) ==============

@api_view(["POST"])
@permission_classes([IsAuthenticated])
def enable_repeat_sample(request, booking_id):
    """Enable repeat sample for a completed booking. Admin/OIC only."""
    if not check_operator_permission(request.user):
        return Response(
            {"error": "Only operators, managers, and admins can enable repeat sample."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        booking = Booking.objects.select_related("equipment", "user").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    if booking.status != BookingStatus.COMPLETED:
        return Response(
            {"error": "Only completed bookings can have repeat sample enabled."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if Booking.objects.filter(source_booking_id=booking_id).exists():
        return Response(
            {"error": "A repeat booking has already been created for this booking. Enable repeat sample cannot be used again."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if booking.repeat_sample_enabled:
        return Response(
            {"message": "Repeat sample is already enabled.", "repeat_sample_enabled": True},
            status=status.HTTP_200_OK,
        )
    booking.repeat_sample_enabled = True
    booking.save(update_fields=["repeat_sample_enabled"])
    create_booking_event(
        booking=booking,
        event_type=BookingEventType.REPEAT_SAMPLE_OFFERED,
        created_by=request.user,
        comment=f"Repeat sample option enabled. User can create one replica booking (same parameters, no charge) for equipment {booking.equipment.code}.",
        send_notification=True,
    )
    return Response({
        "message": "Repeat sample enabled. The booking user can now create a replica booking.",
        "repeat_sample_enabled": True,
    }, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_repeat_sample_eligibility(request, booking_id):
    """Return whether the booking user can create a repeat booking."""
    try:
        booking = Booking.objects.select_related("equipment", "charge_profile").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    if booking.user_id != request.user.id:
        return Response(
            {"error": "Only the booking user can check repeat sample eligibility."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if booking.status != BookingStatus.COMPLETED:
        return Response({"can_create_repeat": False, "reason": "Booking is not completed."}, status=status.HTTP_200_OK)
    if not getattr(booking, "repeat_sample_enabled", False):
        return Response({
            "can_create_repeat": False,
            "reason": "Repeat sample has not been enabled for this booking by admin/OIC.",
        }, status=status.HTTP_200_OK)
    if Booking.objects.filter(source_booking_id=booking_id).exists():
        return Response({
            "can_create_repeat": False,
            "reason": "A repeat booking has already been created for this booking.",
        }, status=status.HTTP_200_OK)
    return Response({"can_create_repeat": True, "reason": None}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_repeat_booking(request, booking_id):
    """Create a replica booking (repeat sample). Only the booking user. Excluded from quota."""
    try:
        orig_booking = Booking.objects.select_related("user", "equipment", "charge_profile").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    if orig_booking.user_id != request.user.id:
        return Response({"error": "Only the booking user can create a repeat booking."}, status=status.HTTP_403_FORBIDDEN)
    if orig_booking.status != BookingStatus.COMPLETED:
        return Response({"error": "Only completed bookings can have a repeat sample."}, status=status.HTTP_400_BAD_REQUEST)
    if not getattr(orig_booking, "repeat_sample_enabled", False):
        return Response({"error": "Repeat sample has not been enabled for this booking."}, status=status.HTTP_400_BAD_REQUEST)
    if Booking.objects.filter(source_booking_id=booking_id).exists():
        return Response({"error": "A repeat booking has already been created for this booking."}, status=status.HTTP_400_BAD_REQUEST)

    equipment = orig_booking.equipment
    total_time_minutes = orig_booking.total_time_minutes or (equipment.slot_duration_minutes or 60)

    slot_ids = None
    if request.data and isinstance(request.data.get("slot_ids"), list):
        raw = request.data.get("slot_ids")
        try:
            slot_ids = [int(x) for x in raw if x is not None]
        except (TypeError, ValueError):
            slot_ids = None
    if slot_ids:
        slot_filter = {
            "id__in": slot_ids,
            "slot_master__equipment": equipment,
            "status": SlotStatus.AVAILABLE,
        }
        if UserType.is_external_user(getattr(orig_booking.user, "user_type", None)):
            slot_filter["reserved_for_external"] = True
        else:
            slot_filter["reserved_for_external"] = False
        daily_slots = DailySlot.objects.filter(**slot_filter).order_by("start_datetime")
        daily_slots = filter_queryset_for_home_department(
            daily_slots,
            user=orig_booking.user,
            equipment=equipment,
            is_admin=False,
            is_external=UserType.is_external_user(getattr(orig_booking.user, "user_type", None)),
        )
        if daily_slots.count() != len(slot_ids):
            return Response(
                {"error": "One or more slots are invalid or not available. Please select only available slots for this equipment."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        total_slot_minutes = sum(
            (s.end_datetime - s.start_datetime).total_seconds() / 60
            for s in daily_slots
        )
        if total_slot_minutes < total_time_minutes:
            return Response(
                {"error": f"Selected slots total {int(total_slot_minutes)} minutes; at least {total_time_minutes} minutes required."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    else:
        from datetime import date, timedelta

        SlotGenerator.ensure_slot_masters_exist(equipment)
        start_date = timezone.localdate()
        end_date = start_date + timedelta(days=60)
        slots_needed = []
        is_external = UserType.is_external_user(getattr(orig_booking.user, "user_type", None))
        if is_external:
            available_list = SlotAvailabilityChecker.get_available_slots_for_external(
                equipment, start_date, end_date
            )
            for ds in available_list:
                slots_needed.append(ds)
                if sum((s.end_datetime - s.start_datetime).total_seconds() / 60 for s in slots_needed) >= total_time_minutes:
                    break
        else:
            # Ensure slots exist for the range using batch week generation
            current = start_date
            while current <= end_date:
                week_end = min(current + timedelta(days=6), end_date)
                SlotGenerator.generate_slots_for_week(equipment, current, week_end, allow_holiday=False)
                current = week_end + timedelta(days=1)
            # Collect available slots until we have enough minutes
            available_qs = DailySlot.objects.filter(
                slot_master__equipment=equipment,
                date__gte=start_date,
                date__lte=end_date,
                status=SlotStatus.AVAILABLE,
            ).select_related("slot_master").order_by("date", "start_datetime")
            for ds in available_qs:
                slots_needed.append(ds)
                if sum((s.end_datetime - s.start_datetime).total_seconds() / 60 for s in slots_needed) >= total_time_minutes:
                    break
        if not slots_needed or sum((s.end_datetime - s.start_datetime).total_seconds() / 60 for s in slots_needed) < total_time_minutes:
            return Response(
                {"error": "No available slots found for the requested duration. Select slots on the booking page or try again later."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        slot_ids = [s.id for s in slots_needed]
        daily_slots = DailySlot.objects.filter(id__in=slot_ids).order_by("start_datetime")

    for s in daily_slots:
        if s.status != SlotStatus.AVAILABLE:
            return Response({"error": "One or more slots became unavailable."}, status=status.HTTP_400_BAD_REQUEST)
        if UserType.is_external_user(getattr(orig_booking.user, "user_type", None)) and not getattr(s, "reserved_for_external", False):
            return Response({"error": "One or more slots are not available for external booking."}, status=status.HTTP_400_BAD_REQUEST)

    orig_vid = orig_booking.virtual_booking_id or str(orig_booking.booking_id)
    equipment_id_display = getattr(equipment, "code", None) or str(equipment.equipment_id)
    discount_remark = f"Repeat of {orig_vid} for {equipment_id_display}"
    # Complimentary repeat: no charges; store a single zero line (no original tariff + discount rows).
    charge_breakdown = [{"description": "Repeat sample (complimentary — no charge)", "amount": 0.0}]
    notes = discount_remark

    with transaction.atomic():
        new_booking = Booking.objects.create(
            user=orig_booking.user,
            equipment=equipment,
            charge_profile=orig_booking.charge_profile,
            user_type_snapshot=orig_booking.user_type_snapshot or "",
            total_time_minutes=total_time_minutes,
            total_charge=Decimal("0"),
            input_values=orig_booking.input_values or {},
            selected_parameters=orig_booking.selected_parameters,
            charge_breakdown=charge_breakdown,
            status=BookingStatus.BOOKED,
            notes=notes,
            created_by=request.user,
            source_booking=orig_booking,
            **{},
        )
        daily_slots.update(booking=new_booking, status=SlotStatus.BOOKED)
        create_booking_event(
            booking=new_booking,
            event_type=BookingEventType.REPEAT_SAMPLE_CREATED,
            created_by=request.user,
            comment=f"Repeat sample booking created for {equipment.name} ({total_time_minutes} min). Original booking: {orig_vid}. No charge; excluded from weekly/monthly limits.",
            new_status=BookingStatus.BOOKED,
            send_notification=True,
        )
        orig_booking.repeat_sample_enabled = False
        orig_booking.save(update_fields=["repeat_sample_enabled"])

    return Response({
        "message": "Repeat booking created successfully.",
        "booking": BookingSerializer(new_booking).data,
        "virtual_booking_id": new_booking.virtual_booking_id,
    }, status=status.HTTP_201_CREATED)


# ============== Repeat sample request (user + admin/OIC) – legacy ==============

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_repeat_sample_info(request, booking_id):
    """Return whether the user can request a repeat sample for this completed booking, disclaimer text, and days left."""
    try:
        booking = Booking.objects.select_related("equipment", "charge_profile").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    if booking.user_id != request.user.id:
        return Response({"error": "Only the booking user can view repeat sample info."}, status=status.HTTP_403_FORBIDDEN)
    if booking.status != BookingStatus.COMPLETED:
        return Response({
            "can_request": False,
            "disclaimer": "",
            "days_left": None,
            "reason": "Booking is not completed.",
        }, status=status.HTTP_200_OK)
    equipment = booking.equipment
    days_allowed = getattr(equipment, "repeat_sample_request_days", None) or 0
    if days_allowed <= 0:
        return Response({
            "can_request": False,
            "disclaimer": getattr(equipment, "repeat_sample_disclaimer", "") or "",
            "days_left": None,
            "reason": "Repeat sample request is not enabled for this equipment.",
        }, status=status.HTTP_200_OK)
    from django.utils import timezone
    completed_at = booking.completed_at or booking.updated_at
    if not completed_at:
        return Response({
            "can_request": False,
            "disclaimer": getattr(equipment, "repeat_sample_disclaimer", "") or "",
            "days_left": None,
            "reason": "Completion date unknown.",
        }, status=status.HTTP_200_OK)
    if timezone.is_naive(completed_at):
        completed_at = timezone.make_aware(completed_at)
    now = timezone.now()
    elapsed_days = (now - completed_at).days
    days_left = max(0, days_allowed - elapsed_days)
    if days_left <= 0:
        return Response({
            "can_request": False,
            "disclaimer": getattr(equipment, "repeat_sample_disclaimer", "") or "",
            "days_left": 0,
            "reason": "Time limit for requesting a repeat sample has passed.",
        }, status=status.HTTP_200_OK)
    has_pending_or_approved = RepeatSampleRequest.objects.filter(
        booking=booking,
        status__in=[RepeatSampleRequestStatus.PENDING, RepeatSampleRequestStatus.APPROVED],
    ).exists()
    if has_pending_or_approved:
        return Response({
            "can_request": False,
            "disclaimer": getattr(equipment, "repeat_sample_disclaimer", "") or "",
            "days_left": days_left,
            "reason": "A repeat sample request already exists for this booking.",
        }, status=status.HTTP_200_OK)
    return Response({
        "can_request": True,
        "disclaimer": getattr(equipment, "repeat_sample_disclaimer", "") or "",
        "days_left": days_left,
        "reason": None,
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def request_repeat_sample(request, booking_id):
    """Create a repeat sample request for a completed booking. Only the booking user."""
    try:
        booking = Booking.objects.select_related("equipment", "charge_profile").get(booking_id=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    if booking.user_id != request.user.id:
        return Response({"error": "Only the booking user can request a repeat sample."}, status=status.HTTP_403_FORBIDDEN)
    if booking.status != BookingStatus.COMPLETED:
        return Response({"error": "Only completed bookings can have a repeat sample request."}, status=status.HTTP_400_BAD_REQUEST)
    equipment = booking.equipment
    days_allowed = getattr(equipment, "repeat_sample_request_days", None) or 0
    if days_allowed <= 0:
        return Response({"error": "Repeat sample request is not enabled for this equipment."}, status=status.HTTP_400_BAD_REQUEST)
    from django.utils import timezone
    completed_at = booking.completed_at or booking.updated_at
    if completed_at and timezone.is_naive(completed_at):
        completed_at = timezone.make_aware(completed_at)
    if completed_at:
        elapsed_days = (timezone.now() - completed_at).days
        if elapsed_days > days_allowed:
            return Response({"error": "Time limit for requesting a repeat sample has passed."}, status=status.HTTP_400_BAD_REQUEST)
    if RepeatSampleRequest.objects.filter(booking=booking, status__in=[RepeatSampleRequestStatus.PENDING, RepeatSampleRequestStatus.APPROVED]).exists():
        return Response({"error": "A repeat sample request already exists for this booking."}, status=status.HTTP_400_BAD_REQUEST)
    user_notes = (request.data.get("user_notes") or "").strip()
    repeat_request = RepeatSampleRequest.objects.create(
        booking=booking,
        status=RepeatSampleRequestStatus.PENDING,
        user_notes=user_notes,
    )
    return Response({
        "message": "Repeat sample request submitted.",
        "repeat_sample_request": RepeatSampleRequestSerializer(repeat_request).data,
    }, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_repeat_sample_requests(request):
    """List repeat sample requests. Admin/OIC only. Query params: status (PENDING, APPROVED, REJECTED)."""
    if not check_operator_permission(request.user):
        return Response(
            {"error": "Only operators, managers, and admins can list repeat sample requests."},
            status=status.HTTP_403_FORBIDDEN,
        )
    status_filter = (request.query_params.get("status") or "").strip().upper()
    qs = RepeatSampleRequest.objects.select_related("booking", "booking__user", "booking__equipment", "new_booking", "responded_by").order_by("-requested_at")
    if status_filter in ("PENDING", "APPROVED", "REJECTED"):
        qs = qs.filter(status=status_filter)
    serializer = RepeatSampleRequestSerializer(qs, many=True)
    return Response({"repeat_sample_requests": serializer.data}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def reject_repeat_sample_request(request, request_id):
    """Reject a repeat sample request. Admin/OIC only. Body: { admin_notes?: string }."""
    if not check_operator_permission(request.user):
        return Response(
            {"error": "Only operators, managers, and admins can reject repeat sample requests."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        repeat_req = RepeatSampleRequest.objects.select_related("booking").get(id=request_id)
    except RepeatSampleRequest.DoesNotExist:
        return Response({"error": "Repeat sample request not found."}, status=status.HTTP_404_NOT_FOUND)
    if repeat_req.status != RepeatSampleRequestStatus.PENDING:
        return Response({"error": f"Request is already {repeat_req.status}."}, status=status.HTTP_400_BAD_REQUEST)
    from django.utils import timezone
    repeat_req.status = RepeatSampleRequestStatus.REJECTED
    repeat_req.responded_at = timezone.now()
    repeat_req.responded_by = request.user
    repeat_req.admin_notes = (request.data.get("admin_notes") or "").strip()
    repeat_req.save(update_fields=["status", "responded_at", "responded_by_id", "admin_notes"])
    return Response({
        "message": "Repeat sample request rejected.",
        "repeat_sample_request": RepeatSampleRequestSerializer(repeat_req).data,
    }, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def approve_repeat_sample_request(request, request_id):
    """Approve repeat sample request: create free booking with first available slots and send confirmation. Admin/OIC only."""
    if not check_operator_permission(request.user):
        return Response(
            {"error": "Only operators, managers, and admins can approve repeat sample requests."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        repeat_req = RepeatSampleRequest.objects.select_related("booking", "booking__user", "booking__equipment", "booking__charge_profile").get(id=request_id)
    except RepeatSampleRequest.DoesNotExist:
        return Response({"error": "Repeat sample request not found."}, status=status.HTTP_404_NOT_FOUND)
    if repeat_req.status != RepeatSampleRequestStatus.PENDING:
        return Response({"error": f"Request is already {repeat_req.status}."}, status=status.HTTP_400_BAD_REQUEST)
    orig_booking = repeat_req.booking
    equipment = orig_booking.equipment
    total_time_minutes = orig_booking.total_time_minutes or (equipment.slot_duration_minutes or 60)
    from datetime import date, timedelta
    from django.utils import timezone
    from django.db import transaction
    SlotGenerator.ensure_slot_masters_exist(equipment)
    start_date = timezone.localdate()
    end_date = start_date + timedelta(days=60)
    slots_needed = []
    # Ensure slots exist using batch week generation
    current = start_date
    while current <= end_date:
        week_end = min(current + timedelta(days=6), end_date)
        SlotGenerator.generate_slots_for_week(equipment, current, week_end, allow_holiday=False)
        current = week_end + timedelta(days=1)
    # Collect available slots until we have enough minutes
    available_qs = DailySlot.objects.filter(
        slot_master__equipment=equipment,
        date__gte=start_date,
        date__lte=end_date,
        status=SlotStatus.AVAILABLE,
    ).select_related("slot_master").order_by("date", "start_datetime")
    for ds in available_qs:
        slots_needed.append(ds)
        if sum((s.end_datetime - s.start_datetime).total_seconds() / 60 for s in slots_needed) >= total_time_minutes:
            break
    if not slots_needed or sum((s.end_datetime - s.start_datetime).total_seconds() / 60 for s in slots_needed) < total_time_minutes:
        return Response(
            {"error": "No available slots found for the requested duration. Try again later or book manually."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    slot_ids = [s.id for s in slots_needed]
    daily_slots = DailySlot.objects.filter(id__in=slot_ids).order_by("start_datetime")
    for s in daily_slots:
        if s.status != SlotStatus.AVAILABLE:
            return Response({"error": "One or more slots became unavailable."}, status=status.HTTP_400_BAD_REQUEST)
    charge_profile = orig_booking.charge_profile
    user_type = orig_booking.user_type_snapshot or ""
    input_values = orig_booking.input_values or {}
    total_charge = Decimal("0")
    charge_breakdown = [{"description": "Repeat sample (complimentary — no charge)", "amount": 0.0}]
    notes = f"Repeat sample (approved from request #{repeat_req.id}). Original booking: {orig_booking.virtual_booking_id or orig_booking.booking_id}."
    with transaction.atomic():
        new_booking = Booking.objects.create(
            user=orig_booking.user,
            equipment=equipment,
            charge_profile=charge_profile,
            user_type_snapshot=user_type,
            total_time_minutes=total_time_minutes,
            total_charge=total_charge,
            input_values=input_values,
            selected_parameters=orig_booking.selected_parameters,
            charge_breakdown=charge_breakdown,
            status=BookingStatus.BOOKED,
            notes=notes,
            created_by=request.user,
            source_booking=orig_booking,
            **initial_istem_fbr_fields_for_charge_profile(charge_profile),
        )
        daily_slots.update(booking=new_booking, status=SlotStatus.BOOKED)
        create_booking_event(
            booking=new_booking,
            event_type=BookingEventType.CREATED,
            created_by=request.user,
            comment=f"Repeat sample booking created (free) for {equipment.name} – {total_time_minutes} min. Original: {orig_booking.virtual_booking_id or orig_booking.booking_id}.",
            new_status=BookingStatus.BOOKED,
            metadata={
                "repeat_sample_from_request": True,
                "original_booking_id": booking_display_id_for_email(orig_booking),
            },
            send_notification=True,
        )
        repeat_req.status = RepeatSampleRequestStatus.APPROVED
        repeat_req.responded_at = timezone.now()
        repeat_req.responded_by = request.user
        repeat_req.new_booking = new_booking
        repeat_req.save(update_fields=["status", "responded_at", "responded_by_id", "new_booking_id"])
    # create_booking_event with send_notification=True already sends booking_created_email to the user
    return Response({
        "message": "Repeat sample request approved. A free booking has been created and the user will be notified.",
        "repeat_sample_request": RepeatSampleRequestSerializer(RepeatSampleRequest.objects.get(id=request_id)).data,
        "new_booking": BookingSerializer(new_booking).data,
    }, status=status.HTTP_200_OK)


# ----- TA Reward Points (duty earn + booking redeem) -----

def _is_admin_or_oic_or_operator(user):
    if not user or not user.is_authenticated:
        return False
    return str(getattr(user, "user_type", "")).lower() in {
        str(UserType.ADMIN).lower(),
        str(UserType.MANAGER).lower(),
        str(UserType.OPERATOR).lower(),
    }


def _is_admin_user(user):
    return str(getattr(user, "user_type", "")).lower() == str(UserType.ADMIN).lower()


def _can_manage_reward_config_for_equipment(user, equipment_id):
    if not _is_admin_or_oic_or_operator(user):
        return False
    if _is_admin_user(user):
        return True
    allowed_ids = set(get_equipment_ids_managed_by_oic(user.id))
    return int(equipment_id) in allowed_ids


def _build_default_reward_config(equipment=None):
    return TARewardConfig(
        equipment=equipment,
        is_enabled=False,
        points_per_duty_hour=Decimal("10.00"),
        points_per_sample=Decimal("0.00"),
        currency_per_point=Decimal("1.0000"),
        max_redeem_percent_per_booking=Decimal("30.00"),
        max_redeem_points_per_booking=300,
        min_booking_amount_for_redeem=Decimal("100.00"),
        expiry_days=180,
        allow_stack_with_other_discounts=True,
    )


def _get_reward_config(equipment=None):
    if equipment is not None:
        cfg = TARewardConfig.objects.filter(equipment=equipment).first()
        if cfg:
            return cfg
    fallback = TARewardConfig.objects.filter(equipment__isnull=True).order_by("-id").first()
    if fallback:
        return fallback
    return _build_default_reward_config(equipment=equipment)


def _get_points_balance(student):
    if not student:
        return Decimal("0.00")
    total = TARewardLedger.objects.filter(student=student).aggregate(v=Sum("points"))["v"] or Decimal("0.00")
    return Decimal(total).quantize(Decimal("0.01"))


def _compute_reward_from_duty(duty_log, cfg):
    points = (
        Decimal(duty_log.hours_spent or 0) * Decimal(cfg.points_per_duty_hour)
        + Decimal(duty_log.samples_processed or 0) * Decimal(cfg.points_per_sample)
    )
    return points.quantize(Decimal("0.01"))


def _create_earn_ledger_for_duty(duty_log, actor):
    cfg = _get_reward_config(duty_log.equipment)
    if not cfg.is_enabled:
        return None
    existing = TARewardLedger.objects.filter(
        source_type=TARewardLedgerSourceType.DUTY_LOG,
        source_id=duty_log.id,
        entry_type=TARewardLedgerEntryType.EARN,
        student=duty_log.student,
    ).first()
    if existing:
        return existing
    points = _compute_reward_from_duty(duty_log, cfg)
    if points <= 0:
        return None
    expires_at = None
    if cfg.expiry_days:
        expires_at = timezone.now() + timedelta(days=int(cfg.expiry_days))
    return TARewardLedger.objects.create(
        student=duty_log.student,
        entry_type=TARewardLedgerEntryType.EARN,
        points=points,
        currency_value=(points * Decimal(cfg.currency_per_point)).quantize(Decimal("0.01")),
        source_type=TARewardLedgerSourceType.DUTY_LOG,
        source_id=duty_log.id,
        description=f"TA duty verified: {duty_log.hours_spent}h, {duty_log.samples_processed} samples",
        expires_at=expires_at,
        created_by=actor,
    )


def _calculate_reward_redemption(total_charge, requested_points, booking_user, equipment=None, lock_ledger=False):
    """
    Return (points_applied, discount_amount, error_message_or_none) for reward redemption.
    """
    cfg = _get_reward_config(equipment)
    if lock_ledger:
        User.objects.select_for_update().filter(pk=booking_user.pk).exists()
    req_points = Decimal(requested_points or 0).quantize(Decimal("0.01"))
    if req_points <= 0:
        return Decimal("0.00"), Decimal("0.00"), None
    if not cfg.is_enabled:
        return Decimal("0.00"), Decimal("0.00"), "Reward redemption is disabled."
    if Decimal(total_charge) < Decimal(cfg.min_booking_amount_for_redeem):
        return Decimal("0.00"), Decimal("0.00"), f"Minimum booking amount for reward redemption is ₹{cfg.min_booking_amount_for_redeem}."
    points_balance = _get_points_balance(booking_user)
    max_by_percent_currency = (Decimal(total_charge) * Decimal(cfg.max_redeem_percent_per_booking) / Decimal("100")).quantize(Decimal("0.01"))
    max_by_percent_points = (max_by_percent_currency / Decimal(cfg.currency_per_point)).quantize(Decimal("0.01")) if Decimal(cfg.currency_per_point) > 0 else Decimal("0.00")
    max_points = min(points_balance, Decimal(cfg.max_redeem_points_per_booking), max_by_percent_points)
    points_applied = min(req_points, max_points).quantize(Decimal("0.01"))
    if points_applied <= 0:
        return Decimal("0.00"), Decimal("0.00"), "Insufficient reward points."
    discount_amount = (points_applied * Decimal(cfg.currency_per_point)).quantize(Decimal("0.01"))
    discount_amount = min(discount_amount, Decimal(total_charge))
    return points_applied, discount_amount, None


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_reward_summary(request):
    equipment_id = request.query_params.get("equipment_id")
    equipment = None
    if equipment_id:
        try:
            equipment = Equipment.objects.get(pk=int(equipment_id))
        except (ValueError, Equipment.DoesNotExist):
            return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    cfg = _get_reward_config(equipment)
    balance = _get_points_balance(request.user)
    earned = TARewardLedger.objects.filter(student=request.user, entry_type=TARewardLedgerEntryType.EARN).aggregate(v=Sum("points"))["v"] or Decimal("0.00")
    redeemed_raw = TARewardLedger.objects.filter(student=request.user, entry_type=TARewardLedgerEntryType.REDEEM).aggregate(v=Sum("points"))["v"] or Decimal("0.00")
    redeemed = abs(Decimal(redeemed_raw))
    return Response({
        "student_id": request.user.id,
        "points_balance": str(balance),
        "currency_per_point": str(cfg.currency_per_point),
        "currency_value_balance": str((balance * Decimal(cfg.currency_per_point)).quantize(Decimal("0.01"))),
        "lifetime_earned_points": str(Decimal(earned).quantize(Decimal("0.01"))),
        "lifetime_redeemed_points": str(redeemed.quantize(Decimal("0.01"))),
        "config": {
            "equipment_id": equipment.equipment_id if equipment else None,
            "is_enabled": cfg.is_enabled,
            "max_redeem_percent_per_booking": str(cfg.max_redeem_percent_per_booking),
            "max_redeem_points_per_booking": cfg.max_redeem_points_per_booking,
            "min_booking_amount_for_redeem": str(cfg.min_booking_amount_for_redeem),
        },
    })


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def my_reward_ledger(request):
    qs = TARewardLedger.objects.filter(student=request.user).order_by("-created_at")
    entry_type = request.query_params.get("entry_type")
    source_type = request.query_params.get("source_type")
    if entry_type:
        qs = qs.filter(entry_type=entry_type)
    if source_type:
        qs = qs.filter(source_type=source_type)
    serializer = TARewardLedgerSerializer(qs[:200], many=True)
    return Response({"count": qs.count(), "entries": serializer.data})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def allocate_ta_assignment(request):
    if str(getattr(request.user, "user_type", "")).lower() not in {
        str(UserType.ADMIN).lower(),
        str(UserType.MANAGER).lower(),
    }:
        return Response({"error": "Only admin or OIC can allocate TA duties."}, status=status.HTTP_403_FORBIDDEN)

    nomination_id = request.data.get("nomination_id")
    booking_id = request.data.get("booking_id")
    expected_hours = request.data.get("expected_hours")
    allocation_notes = (request.data.get("allocation_notes") or "").strip() or None
    if not nomination_id or not booking_id:
        return Response({"error": "nomination_id and booking_id are required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        nomination = StudentEquipmentNomination.objects.select_related("student", "equipment", "semester").get(pk=nomination_id)
    except StudentEquipmentNomination.DoesNotExist:
        return Response({"error": "Nomination not found."}, status=status.HTTP_404_NOT_FOUND)
    if nomination.status != StudentEquipmentNominationStatus.APPROVED:
        return Response({"error": "Only APPROVED nominations can be allocated."}, status=status.HTTP_400_BAD_REQUEST)

    if request.user.user_type != UserType.ADMIN:
        allowed_ids = set(get_equipment_ids_managed_by_oic(request.user.id))
        if nomination.equipment_id not in allowed_ids:
            return Response({"error": "You can only allocate duties for equipment you manage."}, status=status.HTTP_403_FORBIDDEN)

    try:
        booking = Booking.objects.get(pk=booking_id)
    except Booking.DoesNotExist:
        return Response({"error": "Booking not found."}, status=status.HTTP_404_NOT_FOUND)
    if booking.equipment_id != nomination.equipment_id:
        return Response({"error": "Booking equipment must match nominated equipment."}, status=status.HTTP_400_BAD_REQUEST)
    if booking.status in {BookingStatus.CANCELLED, BookingStatus.REFUNDED}:
        return Response({"error": "Cannot allocate TA for cancelled/refunded bookings."}, status=status.HTTP_400_BAD_REQUEST)

    if TAAssignment.objects.filter(
        booking=booking,
        status__in=[TAAssignmentStatus.ALLOCATED, TAAssignmentStatus.ACCEPTED],
    ).exists():
        return Response(
            {"error": "This booking already has an active TA duty assignment."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        assignment = TAAssignment.objects.create(
            nomination=nomination,
            booking=booking,
            ta_student=nomination.student,
            equipment=nomination.equipment,
            semester=nomination.semester,
            allocation_notes=allocation_notes,
            expected_hours=expected_hours or None,
            allocated_by=request.user,
            status=TAAssignmentStatus.ALLOCATED,
        )

    # Notify TA student by email (non-blocking)
    try:
        from django.utils import timezone as django_tz

        portal_url = get_frontend_absolute_url("/ta-assignments") or get_frontend_absolute_url("/dashboard") or "IIC Booking Portal"
        academic_year_name = _academic_year_label_from_semester(nomination.semester)
        daily_slots = DailySlot.objects.filter(booking=booking).select_related("slot_master").order_by("start_datetime")
        first_ds = daily_slots.first()
        last_ds = daily_slots.last()

        def _fmt_slot_dt(dt):
            if not dt:
                return ""
            try:
                local = django_tz.localtime(dt) if django_tz.is_aware(dt) else dt
            except Exception:
                local = dt
            return local.strftime("%Y-%m-%d %H:%M")

        booking_date_str = first_ds.date.isoformat() if first_ds else ""
        booking_start_str = _fmt_slot_dt(first_ds.start_datetime) if first_ds else ""
        booking_end_str = _fmt_slot_dt(last_ds.end_datetime) if last_ds else ""
        exp_h = expected_hours
        if exp_h is not None and str(exp_h).strip() != "":
            expected_hours_display = str(exp_h).strip()
        else:
            expected_hours_display = "—"

        context = {
            "student_name": nomination.student.name or nomination.student.email,
            "student_email": nomination.student.email,
            "instrument_name": nomination.equipment.name or nomination.equipment.code,
            "instrument_code": nomination.equipment.code,
            "academic_year_name": academic_year_name,
            "semester_name": academic_year_name,  # Backward-compatible placeholder in existing templates.
            "assignment_id": str(assignment.id),
            "booking_id": booking_display_id_for_email(booking) or str(booking.booking_id),
            "booking_date": booking_date_str,
            "booking_start": booking_start_str,
            "booking_end": booking_end_str,
            "expected_hours": expected_hours_display,
            "allocation_notes": allocation_notes or "No additional notes.",
            "portal_url": portal_url,
            "allocated_by_name": request.user.name or request.user.email,
        }
        CommunicationService.send_email(
            recipient=nomination.student,
            template="ta_duty_allocation_email",
            template_context=context,
            created_by=request.user,
            metadata={
                "assignment_id": assignment.id,
                "real_booking_id": booking.booking_id,
                "nomination_id": nomination.id,
                "notification_type": "ta_duty_allocation",
            },
        )
    except Exception as e:
        logger.warning("Failed to send TA duty allocation email to %s: %s", nomination.student.email, e)

    return Response({"assignment": TAAssignmentSerializer(assignment).data}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_ta_assignments(request):
    from django.db.models import Prefetch

    qs = TAAssignment.objects.select_related(
        "ta_student",
        "equipment",
        "semester",
        "allocated_by",
        "booking",
        "booking__equipment",
        "nomination",
    ).prefetch_related(
        Prefetch(
            "booking__daily_slots",
            queryset=DailySlot.objects.order_by("start_datetime"),
        ),
    )
    user_type = str(getattr(request.user, "user_type", "")).lower()
    if user_type == str(UserType.ADMIN).lower():
        pass
    elif user_type == str(UserType.MANAGER).lower():
        allowed_ids = get_equipment_ids_managed_by_oic(request.user.id)
        qs = qs.filter(equipment_id__in=allowed_ids)
    elif user_type in {str(UserType.STUDENT).lower(), str(UserType.INDIVIDUAL_STUDENT).lower()}:
        qs = qs.filter(ta_student=request.user)
    else:
        return Response({"error": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)

    status_filter = request.query_params.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)
    equipment_id = request.query_params.get("equipment_id")
    if equipment_id:
        qs = qs.filter(equipment_id=equipment_id)
    booking_id = request.query_params.get("booking_id")
    if booking_id:
        qs = qs.filter(booking_id=booking_id)

    qs = qs.order_by("-allocated_at")
    return Response({"count": qs.count(), "assignments": TAAssignmentSerializer(qs[:300], many=True).data})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def respond_ta_assignment(request, assignment_id):
    if str(getattr(request.user, "user_type", "")).lower() not in {
        str(UserType.STUDENT).lower(),
        str(UserType.INDIVIDUAL_STUDENT).lower(),
    }:
        return Response({"error": "Only TA students can respond to duty assignments."}, status=status.HTTP_403_FORBIDDEN)
    action = str(request.data.get("action") or "").strip().upper()
    if action not in {"ACCEPT", "DECLINE"}:
        return Response({"error": "action must be ACCEPT or DECLINE."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        assignment = TAAssignment.objects.get(pk=assignment_id, ta_student=request.user)
    except TAAssignment.DoesNotExist:
        return Response({"error": "Assignment not found."}, status=status.HTTP_404_NOT_FOUND)
    if assignment.status != TAAssignmentStatus.ALLOCATED:
        return Response({"error": "Only ALLOCATED assignments can be responded to."}, status=status.HTTP_400_BAD_REQUEST)

    from django.utils import timezone as tz
    assignment.status = TAAssignmentStatus.ACCEPTED if action == "ACCEPT" else TAAssignmentStatus.DECLINED
    assignment.responded_at = tz.now()
    response_note = (request.data.get("remarks") or "").strip()
    if response_note:
        assignment.allocation_notes = ((assignment.allocation_notes or "").strip() + "\n" + response_note).strip()
        assignment.save(update_fields=["status", "responded_at", "allocation_notes", "updated_at"])
    else:
        assignment.save(update_fields=["status", "responded_at", "updated_at"])
    return Response({"assignment": TAAssignmentSerializer(assignment).data})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def cancel_ta_assignment(request, assignment_id):
    """Admin or OIC: cancel an ALLOCATED or ACCEPTED assignment (e.g. before duty log is verified)."""
    if str(getattr(request.user, "user_type", "")).lower() not in {
        str(UserType.ADMIN).lower(),
        str(UserType.MANAGER).lower(),
    }:
        return Response(
            {"error": "Only admin or OIC can cancel TA duty assignments."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        assignment = TAAssignment.objects.select_related("equipment").get(pk=assignment_id)
    except TAAssignment.DoesNotExist:
        return Response({"error": "Assignment not found."}, status=status.HTTP_404_NOT_FOUND)
    if assignment.status not in (TAAssignmentStatus.ALLOCATED, TAAssignmentStatus.ACCEPTED):
        return Response(
            {"error": "Only ALLOCATED or ACCEPTED assignments can be cancelled."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if request.user.user_type != UserType.ADMIN:
        allowed_ids = set(get_equipment_ids_managed_by_oic(request.user.id))
        if assignment.equipment_id not in allowed_ids:
            return Response(
                {"error": "You can only cancel assignments for equipment you manage."},
                status=status.HTTP_403_FORBIDDEN,
            )
    if TADutyLog.objects.filter(assignment=assignment, status=TADutyLogStatus.VERIFIED).exists():
        return Response(
            {"error": "Cannot cancel: a duty log has already been verified for this assignment."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    assignment.status = TAAssignmentStatus.CANCELLED
    assignment.save(update_fields=["status", "updated_at"])
    return Response({"assignment": TAAssignmentSerializer(assignment).data})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_ta_duty_log(request):
    if not (
        _is_admin_or_oic_or_operator(request.user)
        or str(getattr(request.user, "user_type", "")).lower() == str(UserType.FACULTY).lower()
        or str(getattr(request.user, "user_type", "")).lower() in {
            str(UserType.STUDENT).lower(),
            str(UserType.INDIVIDUAL_STUDENT).lower(),
        }
    ):
        return Response({"error": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    assignment_id = request.data.get("assignment_id")
    if not assignment_id:
        return Response({"error": "assignment_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        assignment = TAAssignment.objects.select_related("nomination", "ta_student", "equipment", "booking").get(pk=assignment_id)
    except TAAssignment.DoesNotExist:
        return Response({"error": "TA assignment not found."}, status=status.HTTP_404_NOT_FOUND)
    if assignment.status != TAAssignmentStatus.ACCEPTED:
        return Response({"error": "Duty log can be created only for ACCEPTED TA assignments."}, status=status.HTTP_400_BAD_REQUEST)
    if str(getattr(request.user, "user_type", "")).lower() in {
        str(UserType.STUDENT).lower(),
        str(UserType.INDIVIDUAL_STUDENT).lower(),
    } and assignment.ta_student_id != request.user.id:
        return Response({"error": "You can only submit duty logs for your own assignment."}, status=status.HTTP_403_FORBIDDEN)
    if _is_admin_or_oic_or_operator(request.user) and request.user.user_type != UserType.ADMIN:
        allowed_ids = set(get_equipment_ids_managed_by_oic(request.user.id))
        if assignment.equipment_id not in allowed_ids:
            return Response({"error": "You can only manage duty logs for equipment you manage."}, status=status.HTTP_403_FORBIDDEN)
    if TADutyLog.objects.filter(assignment=assignment).exists():
        return Response({"error": "A duty log already exists for this assignment."}, status=status.HTTP_400_BAD_REQUEST)

    nomination = assignment.nomination
    try:
        nomination = StudentEquipmentNomination.objects.select_related("student", "equipment").get(pk=nomination.id)
    except StudentEquipmentNomination.DoesNotExist:
        return Response({"error": "Nomination not found for assignment."}, status=status.HTTP_404_NOT_FOUND)
    if nomination.status != StudentEquipmentNominationStatus.APPROVED:
        return Response({"error": "Duty log can be created only for APPROVED nominations."}, status=status.HTTP_400_BAD_REQUEST)

    serializer = TADutyLogSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    duty_log = serializer.save(
        assignment=assignment,
        nomination=nomination,
        student=nomination.student,
        equipment=nomination.equipment,
        booking=assignment.booking,
        created_by=request.user,
        status=TADutyLogStatus.PENDING,
    )
    return Response({"duty_log": TADutyLogSerializer(duty_log).data}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_ta_duty_logs(request):
    if _is_admin_or_oic_or_operator(request.user):
        qs = TADutyLog.objects.all()
    elif str(getattr(request.user, "user_type", "")).lower() == str(UserType.FACULTY).lower():
        qs = TADutyLog.objects.filter(nomination__supervisor=request.user)
    else:
        qs = TADutyLog.objects.filter(student=request.user)

    status_filter = request.query_params.get("status")
    if status_filter:
        qs = qs.filter(status=status_filter)
    student_id = request.query_params.get("student_id")
    if student_id and _is_admin_or_oic_or_operator(request.user):
        qs = qs.filter(student_id=student_id)
    equipment_id = request.query_params.get("equipment_id")
    if equipment_id:
        qs = qs.filter(equipment_id=equipment_id)
    assignment_id = request.query_params.get("assignment_id")
    if assignment_id:
        qs = qs.filter(assignment_id=assignment_id)
    earn_subq = TARewardLedger.objects.filter(
        source_type=TARewardLedgerSourceType.DUTY_LOG,
        entry_type=TARewardLedgerEntryType.EARN,
        source_id=OuterRef("pk"),
    ).values("points")[:1]
    qs = qs.annotate(
        _reward_points_earned=Subquery(earn_subq, output_field=DecimalField(max_digits=10, decimal_places=2))
    )
    qs = qs.select_related("student", "equipment", "verified_by", "assignment").order_by("-created_at")
    serializer = TADutyLogSerializer(qs[:300], many=True)
    return Response({"count": qs.count(), "duty_logs": serializer.data})


def _apply_pending_duty_log_amendments_from_request(request, duty_log):
    """
    Optional edits from verify request body (admin/OIC may correct TA-submitted values before approving).
    Keys: duty_date (YYYY-MM-DD), hours_spent, samples_processed, remarks.
    """
    data = getattr(request, "data", {}) or {}
    if "duty_date" in data and data["duty_date"] not in (None, ""):
        raw = data["duty_date"]
        dd = parse_date_iso(str(raw)[:10]) if isinstance(raw, str) else None
        if dd:
            duty_log.duty_date = dd
    if "hours_spent" in data and data["hours_spent"] not in (None, ""):
        try:
            duty_log.hours_spent = Decimal(str(data["hours_spent"]))
        except Exception:
            pass
    if "samples_processed" in data and data["samples_processed"] not in (None, ""):
        try:
            duty_log.samples_processed = int(data["samples_processed"])
        except (TypeError, ValueError):
            pass
    if "remarks" in data:
        duty_log.remarks = (data.get("remarks") or "").strip() or None


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def verify_ta_duty_log(request, duty_log_id):
    if not _is_admin_or_oic_or_operator(request.user):
        return Response({"error": "Only admin/OIC/operator can verify duty logs."}, status=status.HTTP_403_FORBIDDEN)
    try:
        duty_log = TADutyLog.objects.select_related("student", "assignment").get(pk=duty_log_id)
    except TADutyLog.DoesNotExist:
        return Response({"error": "Duty log not found."}, status=status.HTTP_404_NOT_FOUND)
    if duty_log.status != TADutyLogStatus.PENDING:
        return Response({"error": "Only PENDING duty logs can be verified."}, status=status.HTTP_400_BAD_REQUEST)
    if duty_log.assignment_id and duty_log.assignment and duty_log.assignment.status != TAAssignmentStatus.ACCEPTED:
        return Response({"error": "Only duty logs from ACCEPTED assignments can be verified."}, status=status.HTTP_400_BAD_REQUEST)
    if request.user.user_type != UserType.ADMIN and duty_log.equipment_id not in set(get_equipment_ids_managed_by_oic(request.user.id)):
        return Response({"error": "You can only verify duty logs for equipment you manage."}, status=status.HTTP_403_FORBIDDEN)

    _apply_pending_duty_log_amendments_from_request(request, duty_log)
    hours_f = float(duty_log.hours_spent or 0)
    samples_n = int(duty_log.samples_processed or 0)
    if hours_f <= 0 and samples_n <= 0:
        return Response(
            {"error": "Hours spent or samples processed must be greater than zero before verification."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    with transaction.atomic():
        duty_log.status = TADutyLogStatus.VERIFIED
        duty_log.verified_by = request.user
        duty_log.verified_at = timezone.now()
        duty_log.save()
        ledger = _create_earn_ledger_for_duty(duty_log, request.user)

    return Response({
        "duty_log": TADutyLogSerializer(duty_log).data,
        "reward_credit": TARewardLedgerSerializer(ledger).data if ledger else None,
        "reward_summary": {"points_balance": str(_get_points_balance(duty_log.student))},
    })


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def reject_ta_duty_log(request, duty_log_id):
    if not _is_admin_or_oic_or_operator(request.user):
        return Response({"error": "Only admin/OIC/operator can reject duty logs."}, status=status.HTTP_403_FORBIDDEN)
    try:
        duty_log = TADutyLog.objects.select_related("assignment").get(pk=duty_log_id)
    except TADutyLog.DoesNotExist:
        return Response({"error": "Duty log not found."}, status=status.HTTP_404_NOT_FOUND)
    if duty_log.status != TADutyLogStatus.PENDING:
        return Response({"error": "Only PENDING duty logs can be rejected."}, status=status.HTTP_400_BAD_REQUEST)

    if request.user.user_type != UserType.ADMIN and duty_log.equipment_id not in set(get_equipment_ids_managed_by_oic(request.user.id)):
        return Response({"error": "You can only reject duty logs for equipment you manage."}, status=status.HTTP_403_FORBIDDEN)
    duty_log.status = TADutyLogStatus.REJECTED
    duty_log.verified_by = request.user
    duty_log.verified_at = timezone.now()
    if request.data.get("remarks"):
        duty_log.remarks = request.data.get("remarks")
    duty_log.save()
    return Response({"duty_log": TADutyLogSerializer(duty_log).data})


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def reward_config_view(request):
    if not _is_admin_or_oic_or_operator(request.user):
        return Response({"error": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    if request.method == "GET":
        equipment_id = request.query_params.get("equipment_id")
        if equipment_id:
            try:
                equipment = Equipment.objects.get(pk=int(equipment_id))
            except (ValueError, Equipment.DoesNotExist):
                return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
            if not _can_manage_reward_config_for_equipment(request.user, equipment.equipment_id):
                return Response({"error": "Permission denied for this equipment."}, status=status.HTTP_403_FORBIDDEN)
            cfg = TARewardConfig.objects.filter(equipment=equipment).first() or _build_default_reward_config(equipment=equipment)
            return Response({"config": TARewardConfigSerializer(cfg).data})

        if _is_admin_user(request.user):
            allowed_eq_qs = Equipment.objects.all().order_by("code")
        else:
            allowed_ids = get_equipment_ids_managed_by_oic(request.user.id)
            allowed_eq_qs = Equipment.objects.filter(id__in=allowed_ids).order_by("code")
        allowed_ids = list(allowed_eq_qs.values_list("equipment_id", flat=True))
        qs = TARewardConfig.objects.select_related("equipment").filter(equipment_id__in=allowed_ids).order_by("equipment__code")
        configured_ids = set(qs.values_list("equipment_id", flat=True))
        manageable = [
            {
                "equipment_id": eq.equipment_id,
                "equipment_code": eq.code,
                "equipment_name": eq.name,
                "config_exists": eq.equipment_id in configured_ids,
            }
            for eq in allowed_eq_qs
        ]
        return Response({"configs": TARewardConfigSerializer(qs, many=True).data, "manageable_equipments": manageable})

    equipment_id = request.data.get("equipment_id") or request.query_params.get("equipment_id")
    if not equipment_id:
        return Response({"error": "equipment_id is required for update."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        equipment = Equipment.objects.get(pk=int(equipment_id))
    except (ValueError, Equipment.DoesNotExist):
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    if not _can_manage_reward_config_for_equipment(request.user, equipment.equipment_id):
        return Response({"error": "Permission denied for this equipment."}, status=status.HTTP_403_FORBIDDEN)
    cfg = TARewardConfig.objects.filter(equipment=equipment).first()
    if cfg is None:
        cfg = TARewardConfig(equipment=equipment)
    serializer = TARewardConfigSerializer(cfg, data=request.data, partial=True)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    serializer.save(equipment=equipment)
    return Response({"config": serializer.data})


def _oic_manageable_equipment_qs(user):
    if _is_admin_user(user):
        return Equipment.objects.all().order_by("code", "name")
    allowed_ids = get_equipment_ids_managed_by_oic(user.id)
    return Equipment.objects.filter(equipment_id__in=allowed_ids).order_by("code", "name")


def _user_can_manage_oic_equipment(user, equipment_id: int) -> bool:
    if _is_admin_user(user):
        return True
    if getattr(user, "user_type", None) != UserType.MANAGER:
        return False
    return int(equipment_id) in set(get_equipment_ids_managed_by_oic(user.id))


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def oic_equipment_accessories_list(request):
    """
    List accessories / additional accessories for equipment managed by the OIC (or all for Admin).
    Optional query: equipment_id
    """
    if not _is_admin_user(request.user) and getattr(request.user, "user_type", None) != UserType.MANAGER:
        return Response({"error": "Only Admin or Officer In Charge can manage accessories."}, status=status.HTTP_403_FORBIDDEN)

    qs = _oic_manageable_equipment_qs(request.user).prefetch_related(
        "equipment_accessories",
        "equipment_additional_accessories",
    )
    equipment_id = request.query_params.get("equipment_id")
    if equipment_id:
        try:
            eq_id = int(equipment_id)
        except (TypeError, ValueError):
            return Response({"error": "Invalid equipment_id."}, status=status.HTTP_400_BAD_REQUEST)
        if not _user_can_manage_oic_equipment(request.user, eq_id):
            return Response({"error": "Permission denied for this equipment."}, status=status.HTTP_403_FORBIDDEN)
        qs = qs.filter(equipment_id=eq_id)

    equipment_rows = []
    for eq in qs:
        equipment_rows.append(
            {
                "equipment_id": eq.equipment_id,
                "equipment_code": eq.code,
                "equipment_name": eq.name,
                "accessories": EquipmentAccessorySerializer(eq.equipment_accessories.all(), many=True).data,
                "additional_accessories": EquipmentAdditionalAccessorySerializer(
                    eq.equipment_additional_accessories.all(), many=True
                ).data,
            }
        )
    return Response({"equipments": equipment_rows})


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def oic_toggle_equipment_accessory(request, accessory_id):
    """Toggle or set is_enabled on an EquipmentAccessory."""
    if not _is_admin_user(request.user) and getattr(request.user, "user_type", None) != UserType.MANAGER:
        return Response({"error": "Only Admin or Officer In Charge can manage accessories."}, status=status.HTTP_403_FORBIDDEN)
    try:
        accessory = EquipmentAccessory.objects.select_related("equipment").get(pk=accessory_id)
    except EquipmentAccessory.DoesNotExist:
        return Response({"error": "Accessory not found."}, status=status.HTTP_404_NOT_FOUND)
    if not _user_can_manage_oic_equipment(request.user, accessory.equipment_id):
        return Response({"error": "Permission denied for this equipment."}, status=status.HTTP_403_FORBIDDEN)

    if "is_enabled" in (request.data or {}):
        raw = request.data.get("is_enabled")
        if isinstance(raw, bool):
            accessory.is_enabled = raw
        else:
            accessory.is_enabled = str(raw).strip().lower() in ("1", "true", "yes", "y")
    else:
        accessory.is_enabled = not accessory.is_enabled
    accessory.save(update_fields=["is_enabled"])
    return Response({"accessory": EquipmentAccessorySerializer(accessory).data})


@api_view(["PATCH"])
@permission_classes([IsAuthenticated])
def oic_toggle_equipment_additional_accessory(request, accessory_id):
    """Toggle or set is_enabled on an EquipmentAdditionalAccessory."""
    if not _is_admin_user(request.user) and getattr(request.user, "user_type", None) != UserType.MANAGER:
        return Response({"error": "Only Admin or Officer In Charge can manage accessories."}, status=status.HTTP_403_FORBIDDEN)
    try:
        accessory = EquipmentAdditionalAccessory.objects.select_related("equipment").get(pk=accessory_id)
    except EquipmentAdditionalAccessory.DoesNotExist:
        return Response({"error": "Additional accessory not found."}, status=status.HTTP_404_NOT_FOUND)
    if not _user_can_manage_oic_equipment(request.user, accessory.equipment_id):
        return Response({"error": "Permission denied for this equipment."}, status=status.HTTP_403_FORBIDDEN)

    if "is_enabled" in (request.data or {}):
        raw = request.data.get("is_enabled")
        if isinstance(raw, bool):
            accessory.is_enabled = raw
        else:
            accessory.is_enabled = str(raw).strip().lower() in ("1", "true", "yes", "y")
    else:
        accessory.is_enabled = not accessory.is_enabled
    accessory.save(update_fields=["is_enabled"])
    return Response({"additional_accessory": EquipmentAdditionalAccessorySerializer(accessory).data})


def _oic_can_manage_print_materials(user) -> bool:
    return _is_admin_user(user) or getattr(user, "user_type", None) == UserType.MANAGER


def _oic_print_3d_equipment_qs(user):
    return _oic_manageable_equipment_qs(user).filter(profile_type=EquipmentProfileType.PRINT_3D)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def oic_print_materials(request):
    """
    GET: list PRINT_3D equipment managed by OIC/Admin with all materials (active + inactive).
    POST: create a material on a managed PRINT_3D equipment.
    """
    if not _oic_can_manage_print_materials(request.user):
        return Response(
            {"error": "Only Admin or Officer In Charge can manage 3D print materials."},
            status=status.HTTP_403_FORBIDDEN,
        )

    if request.method == "GET":
        qs = _oic_print_3d_equipment_qs(request.user).prefetch_related(
            "print_materials",
            "charge_profiles",
        )
        equipment_id = request.query_params.get("equipment_id")
        if equipment_id:
            try:
                eq_id = int(equipment_id)
            except (TypeError, ValueError):
                return Response({"error": "Invalid equipment_id."}, status=status.HTTP_400_BAD_REQUEST)
            if not _user_can_manage_oic_equipment(request.user, eq_id):
                return Response({"error": "Permission denied for this equipment."}, status=status.HTTP_403_FORBIDDEN)
            qs = qs.filter(equipment_id=eq_id)

        choices_dict = dict(UserType.get_choices())
        equipment_rows = []
        for eq in qs:
            materials = eq.print_materials.all().order_by("display_order", "name")
            profiles = eq.charge_profiles.filter(
                pricing_profile=ChargeProfilePricingProfile.STANDARD,
            ).order_by("user_type")
            charge_rows = []
            for cp in profiles:
                charge_rows.append(
                    {
                        "user_type": cp.user_type,
                        "user_type_display": str(choices_dict.get(cp.user_type, cp.user_type)),
                        "primary_unit_charge": str(cp.primary_unit_charge),
                        "is_active": bool(cp.is_active),
                    }
                )
            equipment_rows.append(
                {
                    "equipment_id": eq.equipment_id,
                    "equipment_code": eq.code,
                    "equipment_name": eq.name,
                    "materials": PrintMaterialSerializer(materials, many=True).data,
                    "charge_profiles": charge_rows,
                }
            )
        return Response({"equipments": equipment_rows})

    # POST
    data = request.data or {}
    try:
        eq_id = int(data.get("equipment_id"))
    except (TypeError, ValueError):
        return Response({"error": "equipment_id is required."}, status=status.HTTP_400_BAD_REQUEST)
    if not _user_can_manage_oic_equipment(request.user, eq_id):
        return Response({"error": "Permission denied for this equipment."}, status=status.HTTP_403_FORBIDDEN)
    try:
        equipment = Equipment.objects.get(pk=eq_id)
    except Equipment.DoesNotExist:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    if equipment.profile_type != EquipmentProfileType.PRINT_3D:
        return Response(
            {"error": "Materials can only be managed for PRINT_3D equipment."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    code = str(data.get("code") or "").strip()
    name = str(data.get("name") or "").strip()
    if not code or not name:
        return Response({"error": "code and name are required."}, status=status.HTTP_400_BAD_REQUEST)
    if PrintMaterial.objects.filter(equipment=equipment, code=code).exists():
        return Response(
            {"error": f"A material with code '{code}' already exists for this equipment."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        density = Decimal(str(data.get("density_g_per_cm3") if data.get("density_g_per_cm3") not in (None, "") else "1.240"))
        price = Decimal(str(data.get("price_per_gram")))
    except Exception:
        return Response(
            {"error": "density_g_per_cm3 and price_per_gram must be valid numbers."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if price < 0 or density <= 0:
        return Response(
            {"error": "density must be > 0 and price_per_gram must be >= 0."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    user_type_raw = data.get("user_type")
    user_type = None
    if user_type_raw is not None and str(user_type_raw).strip() != "":
        user_type = str(user_type_raw).strip()

    try:
        display_order = int(data.get("display_order") if data.get("display_order") not in (None, "") else 0)
    except (TypeError, ValueError):
        display_order = 0

    is_active = True
    if "is_active" in data:
        raw = data.get("is_active")
        if isinstance(raw, bool):
            is_active = raw
        else:
            is_active = str(raw).strip().lower() in ("1", "true", "yes", "y")

    material = PrintMaterial.objects.create(
        equipment=equipment,
        code=code,
        name=name,
        density_g_per_cm3=density,
        price_per_gram=price,
        user_type=user_type,
        is_active=is_active,
        display_order=max(0, display_order),
    )
    return Response({"material": PrintMaterialSerializer(material).data}, status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def oic_print_material_detail(request, material_id):
    """Update or delete a single PrintMaterial on managed PRINT_3D equipment."""
    if not _oic_can_manage_print_materials(request.user):
        return Response(
            {"error": "Only Admin or Officer In Charge can manage 3D print materials."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        material = PrintMaterial.objects.select_related("equipment").get(pk=material_id)
    except PrintMaterial.DoesNotExist:
        return Response({"error": "Material not found."}, status=status.HTTP_404_NOT_FOUND)
    if not _user_can_manage_oic_equipment(request.user, material.equipment_id):
        return Response({"error": "Permission denied for this equipment."}, status=status.HTTP_403_FORBIDDEN)
    if material.equipment.profile_type != EquipmentProfileType.PRINT_3D:
        return Response(
            {"error": "Materials can only be managed for PRINT_3D equipment."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if request.method == "DELETE":
        try:
            material.delete()
        except (IntegrityError, ProtectedError, RestrictedError):
            return Response(
                {
                    "error": "Cannot delete this material because it is referenced by existing analyses or bookings. Disable it instead."
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"ok": True})

    data = request.data or {}
    update_fields = []

    if "code" in data:
        code = str(data.get("code") or "").strip()
        if not code:
            return Response({"error": "code cannot be empty."}, status=status.HTTP_400_BAD_REQUEST)
        if (
            PrintMaterial.objects.filter(equipment_id=material.equipment_id, code=code)
            .exclude(pk=material.pk)
            .exists()
        ):
            return Response(
                {"error": f"A material with code '{code}' already exists for this equipment."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        material.code = code
        update_fields.append("code")

    if "name" in data:
        name = str(data.get("name") or "").strip()
        if not name:
            return Response({"error": "name cannot be empty."}, status=status.HTTP_400_BAD_REQUEST)
        material.name = name
        update_fields.append("name")

    if "density_g_per_cm3" in data and data.get("density_g_per_cm3") not in (None, ""):
        try:
            density = Decimal(str(data.get("density_g_per_cm3")))
        except Exception:
            return Response({"error": "Invalid density_g_per_cm3."}, status=status.HTTP_400_BAD_REQUEST)
        if density <= 0:
            return Response({"error": "density must be > 0."}, status=status.HTTP_400_BAD_REQUEST)
        material.density_g_per_cm3 = density
        update_fields.append("density_g_per_cm3")

    if "price_per_gram" in data and data.get("price_per_gram") not in (None, ""):
        try:
            price = Decimal(str(data.get("price_per_gram")))
        except Exception:
            return Response({"error": "Invalid price_per_gram."}, status=status.HTTP_400_BAD_REQUEST)
        if price < 0:
            return Response({"error": "price_per_gram must be >= 0."}, status=status.HTTP_400_BAD_REQUEST)
        material.price_per_gram = price
        update_fields.append("price_per_gram")

    if "user_type" in data:
        raw = data.get("user_type")
        material.user_type = None if raw is None or str(raw).strip() == "" else str(raw).strip()
        update_fields.append("user_type")

    if "display_order" in data and data.get("display_order") not in (None, ""):
        try:
            material.display_order = max(0, int(data.get("display_order")))
        except (TypeError, ValueError):
            return Response({"error": "Invalid display_order."}, status=status.HTTP_400_BAD_REQUEST)
        update_fields.append("display_order")

    if "is_active" in data:
        raw = data.get("is_active")
        if isinstance(raw, bool):
            material.is_active = raw
        else:
            material.is_active = str(raw).strip().lower() in ("1", "true", "yes", "y")
        update_fields.append("is_active")

    if update_fields:
        material.save(update_fields=update_fields)
    return Response({"material": PrintMaterialSerializer(material).data})


@api_view(["GET", "PUT", "PATCH"])
@permission_classes([IsAuthenticated])
def oic_equipment_group_quotas(request, group_id=None):
    """
    GET (no group_id): list equipment groups that contain OIC-managed equipment, with quotas.
    GET/PUT/PATCH with group_id: retrieve or update quota rows for that group.
    Body for update: { "quotas": [ { quota_type, internal_*, external_*, is_enforced }, ... ] }
    """
    if not _is_admin_user(request.user) and getattr(request.user, "user_type", None) != UserType.MANAGER:
        return Response(
            {"error": "Only Admin or Officer In Charge can manage quota configurations."},
            status=status.HTTP_403_FORBIDDEN,
        )

    from .models import EquipmentGroupQuota, QuotaType
    from .serializers import EquipmentGroupDetailSerializer, EquipmentGroupQuotaSerializer

    if _is_admin_user(request.user):
        group_qs = EquipmentGroup.objects.all()
    else:
        allowed_ids = get_equipment_ids_managed_by_oic(request.user.id)
        group_qs = EquipmentGroup.objects.filter(equipment__equipment_id__in=allowed_ids).distinct()

    if group_id is None and request.method == "GET":
        groups = group_qs.prefetch_related("equipment", "quotas").order_by("name")
        return Response(
            {
                "groups": EquipmentGroupDetailSerializer(groups, many=True).data,
            }
        )

    if group_id is None:
        return Response({"error": "group_id is required."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        group = group_qs.prefetch_related("equipment", "quotas").get(equipment_group_id=group_id)
    except EquipmentGroup.DoesNotExist:
        return Response({"error": "Equipment group not found or not managed by you."}, status=status.HTTP_404_NOT_FOUND)

    if request.method == "GET":
        return Response({"group": EquipmentGroupDetailSerializer(group).data})

    quotas_data = (request.data or {}).get("quotas")
    if quotas_data is None or not isinstance(quotas_data, list):
        return Response({"error": "quotas must be a list."}, status=status.HTTP_400_BAD_REQUEST)

    valid_types = {QuotaType.WEEKLY, QuotaType.MONTHLY}
    seen = set()
    for q in quotas_data:
        if not isinstance(q, dict):
            continue
        qtype = str(q.get("quota_type") or "").upper()
        if qtype not in valid_types:
            continue
        seen.add(qtype)
        obj, _ = EquipmentGroupQuota.objects.get_or_create(
            equipment_group=group,
            quota_type=qtype,
            defaults={
                "internal_individual_quota_minutes": 0,
                "internal_faculty_quota_minutes": 0,
                "external_individual_quota_minutes": 0,
                "external_faculty_quota_minutes": 0,
                "is_enforced": True,
            },
        )
        for field in (
            "internal_individual_quota_minutes",
            "internal_faculty_quota_minutes",
            "external_individual_quota_minutes",
            "external_faculty_quota_minutes",
        ):
            if field in q:
                try:
                    setattr(obj, field, max(0, int(q.get(field) or 0)))
                except (TypeError, ValueError):
                    setattr(obj, field, 0)
        if "is_enforced" in q:
            raw = q.get("is_enforced")
            if isinstance(raw, bool):
                obj.is_enforced = raw
            else:
                obj.is_enforced = str(raw).strip().lower() in ("1", "true", "yes", "y")
        obj.save()

    # Keep only weekly/monthly rows that were submitted (or leave existing others)
    if seen:
        group.quotas.exclude(quota_type__in=seen).filter(quota_type__in=valid_types).delete()

    group.refresh_from_db()
    return Response(
        {
            "message": "Quota configurations updated.",
            "group": EquipmentGroupDetailSerializer(group).data,
            "quotas": EquipmentGroupQuotaSerializer(group.quotas.all(), many=True).data,
        }
    )


def _serialize_mode_schedule(sched):
    return {
        "id": sched.id,
        "parent_equipment_id": sched.parent_equipment_id,
        "mode_equipment_id": sched.mode_equipment_id,
        "mode_equipment_code": getattr(sched.mode_equipment, "code", None),
        "mode_equipment_name": getattr(sched.mode_equipment, "name", None),
        "start_date": sched.start_date.isoformat() if sched.start_date else None,
        "end_date": sched.end_date.isoformat() if sched.end_date else None,
        "start_time": sched.start_time.strftime("%H:%M") if sched.start_time else None,
        "end_time": sched.end_time.strftime("%H:%M") if sched.end_time else None,
        "behavior": sched.behavior,
        "behavior_display": sched.get_behavior_display() if hasattr(sched, "get_behavior_display") else sched.behavior,
        "unavailable_label": sched.unavailable_label or "Mode not scheduled",
        "unavailable_color": sched.unavailable_color or "#9ca3af",
        "exclusive_blocked_label": sched.exclusive_blocked_label or "Alternate mode active",
        "exclusive_blocked_color": sched.exclusive_blocked_color or "#9ca3af",
        "created_at": sched.created_at.isoformat() if sched.created_at else None,
        "updated_at": sched.updated_at.isoformat() if sched.updated_at else None,
    }


def _parse_optional_time(raw):
    if raw is None or raw == "":
        return None
    s = str(raw).strip()
    if not s:
        return None
    from datetime import time as time_cls
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(s[:len(fmt) + 2], fmt).time()
        except ValueError:
            continue
    try:
        parts = s.split(":")
        return time_cls(int(parts[0]), int(parts[1]) if len(parts) > 1 else 0)
    except (TypeError, ValueError, IndexError):
        return None


def _apply_schedule_display_fields(sched, data):
    if "unavailable_label" in data:
        sched.unavailable_label = (str(data.get("unavailable_label") or "").strip() or "Mode not scheduled")[:120]
    if "unavailable_color" in data:
        color = str(data.get("unavailable_color") or "").strip() or "#9ca3af"
        sched.unavailable_color = color[:20]
    if "exclusive_blocked_label" in data:
        sched.exclusive_blocked_label = (
            str(data.get("exclusive_blocked_label") or "").strip() or "Alternate mode active"
        )[:120]
    if "exclusive_blocked_color" in data:
        color = str(data.get("exclusive_blocked_color") or "").strip() or "#9ca3af"
        sched.exclusive_blocked_color = color[:20]
    if "start_time" in data:
        sched.start_time = _parse_optional_time(data.get("start_time"))
    if "end_time" in data:
        sched.end_time = _parse_optional_time(data.get("end_time"))


def _user_can_access_multimode_config(user) -> bool:
    """Admin and Officer In Charge (manager) can open Multi-Mode config."""
    if _is_admin_user(user):
        return True
    return getattr(user, "user_type", None) == UserType.MANAGER


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def oic_multi_mode_list(request):
    """
    List multi-mode-enabled parents managed by the OIC (or all for Admin),
    with children and schedules. Only equipment with enable_multi_mode=True
    appear for configuration.
    """
    if not _user_can_access_multimode_config(request.user):
        return Response(
            {"error": "Only Admin or Officer In Charge can configure Multi-Mode Equipment."},
            status=status.HTTP_403_FORBIDDEN,
        )

    from .models import ModeScheduleBehavior

    managed_qs = _oic_manageable_equipment_qs(request.user)

    # Only base instruments with Multi-Mode enabled are configurable parents
    parents = (
        managed_qs.filter(enable_multi_mode=True, parent_equipment__isnull=True)
        .prefetch_related("mode_children", "mode_schedules", "mode_schedules__mode_equipment")
        .order_by("code", "name")
    )

    # Mode candidates: managed standalone equipment that are not multi-mode bases
    linkable = list(
        managed_qs.filter(parent_equipment__isnull=True, enable_multi_mode=False)
        .order_by("code")
        .values("equipment_id", "code", "name")
    )

    families = []
    for parent in parents:
        children = [
            {
                "equipment_id": c.equipment_id,
                "code": c.code,
                "name": c.name,
                "status": c.status,
            }
            for c in parent.mode_children.all().order_by("code")
        ]
        schedules = [
            _serialize_mode_schedule(s)
            for s in parent.mode_schedules.all().order_by("-start_date", "-end_date")
        ]
        families.append(
            {
                "parent_equipment_id": parent.equipment_id,
                "parent_code": parent.code,
                "parent_name": parent.name,
                "parent_status": parent.status,
                "children": children,
                "schedules": schedules,
            }
        )

    return Response(
        {
            "multi_mode_enabled": True,
            "families": families,
            "linkable_equipment": linkable,
            "behaviors": [
                {"value": ModeScheduleBehavior.PARALLEL, "label": "Parallel"},
                {"value": ModeScheduleBehavior.EXCLUSIVE, "label": "Mutually Exclusive"},
            ],
        }
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def oic_multi_mode_schedule_create(request):
    """
    Create a mode schedule.
    Body: parent_equipment_id, mode_equipment_id, start_date, end_date, behavior,
    optional start_time/end_time, unavailable_*, exclusive_blocked_*
    """
    if not _user_can_access_multimode_config(request.user):
        return Response(
            {"error": "Only Admin or Officer In Charge can configure Multi-Mode Equipment."},
            status=status.HTTP_403_FORBIDDEN,
        )

    from django.core.exceptions import ValidationError as DjangoValidationError
    from .models import EquipmentModeSchedule, ModeScheduleBehavior

    data = request.data or {}
    try:
        parent_id = int(data.get("parent_equipment_id"))
        mode_id = int(data.get("mode_equipment_id"))
    except (TypeError, ValueError):
        return Response(
            {"error": "parent_equipment_id and mode_equipment_id are required integers."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    if not _user_can_manage_oic_equipment(request.user, parent_id):
        return Response({"error": "Permission denied for parent equipment."}, status=status.HTTP_403_FORBIDDEN)
    if not _user_can_manage_oic_equipment(request.user, mode_id):
        return Response({"error": "Permission denied for mode equipment."}, status=status.HTTP_403_FORBIDDEN)

    try:
        parent = Equipment.objects.get(pk=parent_id)
        mode = Equipment.objects.get(pk=mode_id)
    except Equipment.DoesNotExist:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)

    if not parent.enable_multi_mode:
        return Response(
            {
                "error": (
                    "Multi-Mode Equipment is not enabled for this instrument. "
                    "Enable it on the equipment create/edit form first."
                )
            },
            status=status.HTTP_400_BAD_REQUEST,
        )
    if parent.parent_equipment_id:
        return Response(
            {"error": "Parent must be a base instrument (not itself a child mode)."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if mode_id == parent_id:
        return Response({"error": "Mode cannot be the same as the parent."}, status=status.HTTP_400_BAD_REQUEST)

    if mode.parent_equipment_id and mode.parent_equipment_id != parent_id:
        return Response(
            {"error": "Mode equipment is already linked to a different parent."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not mode.parent_equipment_id:
        if mode.mode_children.exists():
            return Response(
                {"error": "Cannot link an equipment that already has child modes as a mode."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        mode.parent_equipment = parent
        mode.save(update_fields=["parent_equipment", "updated_at"])

    start_raw = (data.get("start_date") or "").strip()[:10]
    end_raw = (data.get("end_date") or "").strip()[:10]
    try:
        start_d = date.fromisoformat(start_raw)
        end_d = date.fromisoformat(end_raw)
    except ValueError:
        return Response(
            {"error": "start_date and end_date must be YYYY-MM-DD."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    behavior = (data.get("behavior") or ModeScheduleBehavior.PARALLEL).strip().upper()
    if behavior not in (ModeScheduleBehavior.PARALLEL, ModeScheduleBehavior.EXCLUSIVE):
        return Response(
            {"error": "behavior must be PARALLEL or EXCLUSIVE."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    sched = EquipmentModeSchedule(
        parent_equipment=parent,
        mode_equipment=mode,
        start_date=start_d,
        end_date=end_d,
        behavior=behavior,
        created_by=request.user,
    )
    _apply_schedule_display_fields(sched, data)
    # Ensure defaults applied even if keys omitted
    if not data.get("unavailable_label"):
        sched.unavailable_label = sched.unavailable_label or "Mode not scheduled"
    if not data.get("unavailable_color"):
        sched.unavailable_color = sched.unavailable_color or "#9ca3af"
    if not data.get("exclusive_blocked_label"):
        sched.exclusive_blocked_label = sched.exclusive_blocked_label or "Alternate mode active"
    if not data.get("exclusive_blocked_color"):
        sched.exclusive_blocked_color = sched.exclusive_blocked_color or "#9ca3af"
    try:
        sched.full_clean()
        sched.save()
    except DjangoValidationError as e:
        return Response({"error": e.message_dict if hasattr(e, "message_dict") else str(e)}, status=status.HTTP_400_BAD_REQUEST)

    return Response({"schedule": _serialize_mode_schedule(sched)}, status=status.HTTP_201_CREATED)


@api_view(["PATCH", "DELETE"])
@permission_classes([IsAuthenticated])
def oic_multi_mode_schedule_detail(request, schedule_id):
    """Update or delete a mode schedule."""
    if not _user_can_access_multimode_config(request.user):
        return Response(
            {"error": "Only Admin or Officer In Charge can configure Multi-Mode Equipment."},
            status=status.HTTP_403_FORBIDDEN,
        )

    from django.core.exceptions import ValidationError as DjangoValidationError
    from .models import EquipmentModeSchedule, ModeScheduleBehavior

    try:
        sched = EquipmentModeSchedule.objects.select_related(
            "parent_equipment", "mode_equipment"
        ).get(pk=schedule_id)
    except EquipmentModeSchedule.DoesNotExist:
        return Response({"error": "Schedule not found."}, status=status.HTTP_404_NOT_FOUND)

    if not _user_can_manage_oic_equipment(request.user, sched.parent_equipment_id):
        return Response({"error": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)

    if request.method == "DELETE":
        sched.delete()
        return Response({"message": "Schedule deleted."})

    data = request.data or {}
    if "start_date" in data:
        try:
            sched.start_date = date.fromisoformat(str(data.get("start_date")).strip()[:10])
        except ValueError:
            return Response({"error": "Invalid start_date."}, status=status.HTTP_400_BAD_REQUEST)
    if "end_date" in data:
        try:
            sched.end_date = date.fromisoformat(str(data.get("end_date")).strip()[:10])
        except ValueError:
            return Response({"error": "Invalid end_date."}, status=status.HTTP_400_BAD_REQUEST)
    if "behavior" in data:
        behavior = str(data.get("behavior") or "").strip().upper()
        if behavior not in (ModeScheduleBehavior.PARALLEL, ModeScheduleBehavior.EXCLUSIVE):
            return Response({"error": "behavior must be PARALLEL or EXCLUSIVE."}, status=status.HTTP_400_BAD_REQUEST)
        sched.behavior = behavior
    if "mode_equipment_id" in data:
        try:
            mode_id = int(data.get("mode_equipment_id"))
        except (TypeError, ValueError):
            return Response({"error": "Invalid mode_equipment_id."}, status=status.HTTP_400_BAD_REQUEST)
        if not _user_can_manage_oic_equipment(request.user, mode_id):
            return Response({"error": "Permission denied for mode equipment."}, status=status.HTTP_403_FORBIDDEN)
        try:
            mode = Equipment.objects.get(pk=mode_id)
        except Equipment.DoesNotExist:
            return Response({"error": "Mode equipment not found."}, status=status.HTTP_404_NOT_FOUND)
        if mode.parent_equipment_id != sched.parent_equipment_id:
            return Response(
                {"error": "Mode equipment must be a child of this parent."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        sched.mode_equipment = mode

    _apply_schedule_display_fields(sched, data)

    try:
        sched.full_clean()
        sched.save()
    except DjangoValidationError as e:
        return Response({"error": e.message_dict if hasattr(e, "message_dict") else str(e)}, status=status.HTTP_400_BAD_REQUEST)

    return Response({"schedule": _serialize_mode_schedule(sched)})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def admin_adjust_reward_points(request):
    if not _is_admin_or_oic_or_operator(request.user):
        return Response({"error": "Permission denied."}, status=status.HTTP_403_FORBIDDEN)
    serializer = AdminRewardAdjustSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    student_id = serializer.validated_data["student_id"]
    try:
        student = User.objects.get(pk=student_id)
    except User.DoesNotExist:
        return Response({"error": "Student not found."}, status=status.HTTP_404_NOT_FOUND)
    points = serializer.validated_data["points"]
    if points == 0:
        return Response({"error": "Points cannot be zero."}, status=status.HTTP_400_BAD_REQUEST)
    cfg = _get_reward_config()
    reason = serializer.validated_data["reason"]
    reference = serializer.validated_data.get("reference", "").strip()
    description = f"{reason}. Ref: {reference}" if reference else reason
    entry = TARewardLedger.objects.create(
        student=student,
        entry_type=TARewardLedgerEntryType.ADJUST,
        points=points,
        currency_value=(abs(points) * Decimal(cfg.currency_per_point)).quantize(Decimal("0.01")),
        source_type=TARewardLedgerSourceType.MANUAL,
        description=description,
        created_by=request.user,
    )
    return Response(
        {
            "ledger_entry_id": entry.id,
            "student_id": student.id,
            "points": str(entry.points),
            "entry_type": entry.entry_type,
            "description": entry.description,
            "updated_balance": str(_get_points_balance(student)),
        },
        status=status.HTTP_201_CREATED,
    )


# ----- Student equipment operating nomination (semester-wise, supervisor nominates) -----

@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_semesters(request):
    """List semesters for dropdowns. Optional ?active_only=1 to restrict to active semesters."""
    active_only = request.query_params.get("active_only", "").strip() == "1"
    qs = Semester.objects.all()
    if active_only:
        qs = qs.filter(is_active=True)
    qs = qs.order_by("-start_date")
    return Response({
        "semesters": [
            {
                "id": s.id,
                "code": s.code,
                "name": s.name,
                "start_date": s.start_date.isoformat() if s.start_date else None,
                "end_date": s.end_date.isoformat() if s.end_date else None,
                "is_active": s.is_active,
            }
            for s in qs
        ],
    })


def _nomination_to_dict(nom):
    """Build a dict for a StudentEquipmentNomination for API response."""
    student = nom.student
    approved_by = getattr(nom, "approved_by", None)
    approved_by_name = None
    outcome_summary = "Pending"
    if nom.approved_at and nom.approved_by_id:
        approved_by_name = approved_by.name or approved_by.email if approved_by else None
        if nom.status == StudentEquipmentNominationStatus.APPROVED:
            outcome_summary = f"Approved by {approved_by_name or '—'} on {nom.approved_at.strftime('%d %b %Y')}"
        elif nom.status == StudentEquipmentNominationStatus.REJECTED:
            outcome_summary = f"Rejected by {approved_by_name or '—'} on {nom.approved_at.strftime('%d %b %Y')}"
    resume_filename = None
    if nom.resume:
        resume_filename = nom.resume.name.split("/")[-1] if "/" in nom.resume.name else nom.resume.name
    academic_year_name = _academic_year_label_from_semester(getattr(nom, "semester", None))
    return {
        "id": nom.id,
        "student_id": nom.student_id,
        "student_name": student.name or student.email,
        "student_email": student.email,
        "student_branch_name": getattr(student, "branch_name", None) or "",
        "student_degree_name": getattr(student, "degree_name", None) or "",
        "student_department_name": student.department.name if getattr(student, "department", None) else "",
        "supervisor_id": nom.supervisor_id,
        "supervisor_name": nom.supervisor.name or nom.supervisor.email,
        "equipment_id": nom.equipment_id,
        "equipment_code": nom.equipment.code,
        "equipment_name": nom.equipment.name,
        "semester_id": nom.semester_id,
        "semester_code": nom.semester.code,
        "semester_name": academic_year_name,
        "academic_year_name": academic_year_name,
        "status": nom.status,
        "remarks": nom.remarks,
        "nominated_at": nom.nominated_at.isoformat() if nom.nominated_at else None,
        "approved_at": nom.approved_at.isoformat() if nom.approved_at else None,
        "approved_by_id": nom.approved_by_id,
        "approved_by_name": approved_by_name,
        "outcome_summary": outcome_summary,
        "ta_call_id": nom.ta_call_id,
        "resume_submitted_at": nom.resume_submitted_at.isoformat() if nom.resume_submitted_at else None,
        "resume_filename": resume_filename,
        "has_resume": bool(nom.resume),
    }


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_equipment_nomination(request):
    """
    Supervisor creates a nomination: student (must be their supervised user) + equipment + semester.
    Only faculty (supervisor) can create; student must have request.user as supervisor.
    """
    if request.user.user_type != UserType.FACULTY:
        return Response(
            {"error": "Only faculty (supervisors) can nominate students for equipment operation."},
            status=status.HTTP_403_FORBIDDEN,
        )
    student_id = request.data.get("student_id")
    equipment_id = request.data.get("equipment_id")
    semester_id = request.data.get("semester_id")
    remarks = (request.data.get("remarks") or "").strip() or None
    ta_call_id = request.data.get("ta_call_id")
    if not student_id or not equipment_id or not semester_id:
        return Response(
            {"error": "student_id, equipment_id, and semester_id are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    from iic_booking.users.models.user import User
    try:
        student = User.objects.get(pk=student_id)
    except User.DoesNotExist:
        return Response({"error": "Student not found."}, status=status.HTTP_404_NOT_FOUND)
    # Allow if faculty is the student's supervisor (User.supervisor) OR student has approved wallet join with this faculty
    from iic_booking.users.models.wallet import WalletJoinRequest, WalletJoinRequestStatus
    is_supervisor = student.supervisor_id == request.user.id
    has_approved_wallet_join = WalletJoinRequest.objects.filter(
        student=student,
        faculty=request.user,
        status=WalletJoinRequestStatus.APPROVED,
    ).exists()
    if not is_supervisor and not has_approved_wallet_join:
        return Response(
            {"error": "You can only nominate students for whom you are the supervisor or who have joined your wallet (approved)."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if student.user_type not in (UserType.STUDENT, UserType.INDIVIDUAL_STUDENT):
        return Response({"error": "Selected user is not a student."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        equipment = Equipment.objects.get(pk=equipment_id)
    except Equipment.DoesNotExist:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    try:
        semester = Semester.objects.get(pk=semester_id)
    except Semester.DoesNotExist:
        return Response({"error": "Semester not found."}, status=status.HTTP_404_NOT_FOUND)
    if not semester.is_active:
        return Response(
            {"error": "Nominations are not open for this semester."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    ta_call = None
    if ta_call_id:
        from django.utils import timezone as tz
        try:
            ta_call = EquipmentOperatingTACall.objects.get(pk=ta_call_id)
        except EquipmentOperatingTACall.DoesNotExist:
            return Response({"error": "TA call not found."}, status=status.HTTP_404_NOT_FOUND)
        if ta_call.status != EquipmentOperatingTACallStatus.OPEN:
            return Response(
                {"error": "This TA call is no longer open for nominations."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if ta_call.nomination_deadline < tz.localdate():
            return Response(
                {"error": "Nomination deadline for this TA call has passed."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if ta_call.equipment_id != equipment_id or ta_call.semester_id != semester_id:
            return Response(
                {"error": "Equipment and semester must match the selected TA call."},
                status=status.HTTP_400_BAD_REQUEST,
            )
    nom, created = StudentEquipmentNomination.objects.get_or_create(
        student=student,
        equipment=equipment,
        semester=semester,
        defaults={
            "supervisor": request.user,
            "status": StudentEquipmentNominationStatus.PENDING,
            "remarks": remarks,
            "ta_call": ta_call,
        },
    )
    if not created:
        return Response(
            {"error": "A nomination for this student, equipment, and semester already exists.", "nomination": _nomination_to_dict(nom)},
            status=status.HTTP_400_BAD_REQUEST,
        )
    # Send intimation email to the student so they can submit their resume
    try:
        nomination_requests_url = get_frontend_absolute_url("/my-nomination-requests") or get_frontend_absolute_url("/dashboard") or "IIC Booking Portal"
        academic_year_name = _academic_year_label_from_semester(semester)
        context = {
            "student_name": student.name or student.email,
            "student_email": student.email,
            "instrument_name": equipment.name or equipment.code,
            "instrument_code": equipment.code,
            "semester_name": academic_year_name,
            "academic_year_name": academic_year_name,
            "supervisor_name": request.user.name or request.user.email,
            "nomination_requests_url": nomination_requests_url,
        }
        CommunicationService.send_email(
            recipient=student,
            template="student_nomination_intimation_email",
            template_context=context,
            created_by=request.user,
        )
    except Exception as e:
        logger.warning("Failed to send nomination intimation email to student %s: %s", student.email, e)
    return Response({"nomination": _nomination_to_dict(nom)}, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_my_nominations_as_supervisor(request):
    """List nominations created by the current user (as supervisor). Optional filters: semester_id, status."""
    if request.user.user_type != UserType.FACULTY:
        return Response(
            {"error": "Only faculty (supervisors) can list their nominations."},
            status=status.HTTP_403_FORBIDDEN,
        )
    qs = StudentEquipmentNomination.objects.filter(supervisor=request.user).select_related(
        "student", "student__department", "supervisor", "equipment", "semester", "approved_by"
    )
    semester_id = request.query_params.get("semester_id")
    if semester_id:
        qs = qs.filter(semester_id=semester_id)
    status_filter = request.query_params.get("status")
    if status_filter and status_filter in ("PENDING", "APPROVED", "REJECTED"):
        qs = qs.filter(status=status_filter)
    qs = qs.order_by("-nominated_at")
    return Response({"nominations": [_nomination_to_dict(n) for n in qs]})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_my_nominations_as_student(request):
    """List nominations where the current user is the student."""
    if request.user.user_type not in (UserType.STUDENT, UserType.INDIVIDUAL_STUDENT):
        return Response(
            {"error": "Only students can view their own nominations."},
            status=status.HTTP_403_FORBIDDEN,
        )
    qs = StudentEquipmentNomination.objects.filter(student=request.user).select_related(
        "student", "student__department", "supervisor", "equipment", "semester", "approved_by"
    ).order_by("-nominated_at")
    return Response({"nominations": [_nomination_to_dict(n) for n in qs]})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def submit_nomination_resume(request, nomination_id):
    """
    Student: upload and submit resume for a nomination where they are the nominated student.
    Accepts multipart/form-data with key 'resume' or 'file'. Only the nominated student can submit.
    """
    if request.user.user_type not in (UserType.STUDENT, UserType.INDIVIDUAL_STUDENT):
        return Response(
            {"error": "Only students can submit a resume for their nomination."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        nom = StudentEquipmentNomination.objects.get(pk=nomination_id)
    except StudentEquipmentNomination.DoesNotExist:
        return Response({"error": "Nomination not found."}, status=status.HTTP_404_NOT_FOUND)
    if nom.student_id != request.user.id:
        return Response(
            {"error": "You can only submit a resume for your own nomination."},
            status=status.HTTP_403_FORBIDDEN,
        )
    if nom.status != StudentEquipmentNominationStatus.PENDING:
        return Response(
            {"error": "Resume can only be submitted for PENDING nominations."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    resume_file = request.FILES.get("resume") or request.FILES.get("file")
    if not resume_file:
        return Response(
            {"error": "No file provided. Send multipart form with key 'resume' or 'file'."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    from django.utils import timezone as tz
    nom.resume = resume_file
    nom.resume_submitted_at = tz.now()
    nom.save(update_fields=["resume", "resume_submitted_at"])
    return Response({"nomination": _nomination_to_dict(nom)}, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def get_nomination_resume(request, nomination_id):
    """
    Download resume for a nomination. Allowed for: the nominated student, the supervisor, or Admin/OIC for that equipment.
    """
    try:
        nom = StudentEquipmentNomination.objects.select_related("equipment").get(pk=nomination_id)
    except StudentEquipmentNomination.DoesNotExist:
        return Response({"error": "Nomination not found."}, status=status.HTTP_404_NOT_FOUND)
    if not nom.resume:
        return Response({"error": "No resume has been submitted for this nomination."}, status=status.HTTP_404_NOT_FOUND)
    can_access = (
        nom.student_id == request.user.id
        or nom.supervisor_id == request.user.id
        or (check_operator_permission(request.user) and (
            request.user.user_type == UserType.ADMIN
            or (request.user.user_type != UserType.ADMIN and nom.equipment_id in get_equipment_ids_managed_by_oic(request.user.id))
        ))
    )
    if not can_access:
        return Response(
            {"error": "You do not have permission to download this resume."},
            status=status.HTTP_403_FORBIDDEN,
        )
    file_name = nom.resume.name
    if not file_name or not default_storage.exists(file_name):
        logger.warning("Nomination resume file not found in storage: %s", file_name)
        return Response({"error": "Resume file not found in storage."}, status=status.HTTP_404_NOT_FOUND)
    try:
        with default_storage.open(file_name, "rb") as fh:
            content = fh.read()
    except Exception as e:
        logger.warning("Failed to open nomination resume file: %s", e)
        return Response({"error": "Resume file could not be opened."}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
    filename = file_name.split("/")[-1] if "/" in file_name else file_name
    content_type, _ = mimetypes.guess_type(filename)
    if not content_type:
        content_type = "application/octet-stream"
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = 'attachment; filename="{}"'.format(
        filename.replace('"', "%22")
    )
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    response["Expires"] = "0"
    return response


@api_view(["DELETE"])
@permission_classes([IsAuthenticated])
def revoke_equipment_nomination(request, nomination_id):
    """Supervisor can revoke (delete) a PENDING nomination they created."""
    if request.user.user_type != UserType.FACULTY:
        return Response(
            {"error": "Only faculty (supervisors) can revoke nominations."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        nom = StudentEquipmentNomination.objects.get(pk=nomination_id)
    except StudentEquipmentNomination.DoesNotExist:
        return Response({"error": "Nomination not found."}, status=status.HTTP_404_NOT_FOUND)
    if nom.supervisor_id != request.user.id:
        return Response({"error": "You can only revoke nominations you created."}, status=status.HTTP_403_FORBIDDEN)
    if nom.status != StudentEquipmentNominationStatus.PENDING:
        return Response(
            {"error": "Only PENDING nominations can be revoked. Approved/Rejected nominations cannot be deleted."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    nom.delete()
    return Response({"message": "Nomination revoked."}, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_equipment_nominations_admin(request):
    """
    Admin/OIC: list all nominations with optional filters semester_id, equipment_id, supervisor_id, status.
    OIC sees only nominations for equipment they manage.
    """
    if not check_operator_permission(request.user):
        return Response(
            {"error": "Only admin, manager (OIC), or operator can list all nominations."},
            status=status.HTTP_403_FORBIDDEN,
        )
    qs = StudentEquipmentNomination.objects.all().select_related(
        "student", "student__department", "supervisor", "equipment", "semester", "approved_by"
    )
    if request.user.user_type != UserType.ADMIN:
        oic_equipment_ids = get_equipment_ids_managed_by_oic(request.user.id)
        if not oic_equipment_ids:
            qs = qs.none()
        else:
            qs = qs.filter(equipment_id__in=oic_equipment_ids)
    semester_id = request.query_params.get("semester_id")
    if semester_id:
        qs = qs.filter(semester_id=semester_id)
    equipment_id = request.query_params.get("equipment_id")
    if equipment_id:
        qs = qs.filter(equipment_id=equipment_id)
    supervisor_id = request.query_params.get("supervisor_id")
    if supervisor_id:
        qs = qs.filter(supervisor_id=supervisor_id)
    status_filter = request.query_params.get("status")
    if status_filter and status_filter in ("PENDING", "APPROVED", "REJECTED"):
        qs = qs.filter(status=status_filter)
    ta_call_id = request.query_params.get("ta_call_id")
    if ta_call_id:
        try:
            qs = qs.filter(ta_call_id=int(ta_call_id))
        except (ValueError, TypeError):
            pass
    qs = qs.order_by("-nominated_at")
    return Response({"nominations": [_nomination_to_dict(n) for n in qs]})


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def approve_equipment_nomination(request, nomination_id):
    """Admin/OIC: approve a PENDING nomination."""
    if not check_operator_permission(request.user):
        return Response(
            {"error": "Only admin, manager (OIC), or operator can approve nominations."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        nom = StudentEquipmentNomination.objects.select_related("student", "equipment", "semester").get(pk=nomination_id)
    except StudentEquipmentNomination.DoesNotExist:
        return Response({"error": "Nomination not found."}, status=status.HTTP_404_NOT_FOUND)
    if request.user.user_type != UserType.ADMIN:
        oic_equipment_ids = get_equipment_ids_managed_by_oic(request.user.id)
        if nom.equipment_id not in oic_equipment_ids:
            return Response({"error": "You can only approve nominations for equipment you manage."}, status=status.HTTP_403_FORBIDDEN)
    if nom.status != StudentEquipmentNominationStatus.PENDING:
        return Response({"error": "Only PENDING nominations can be approved."}, status=status.HTTP_400_BAD_REQUEST)
    from django.utils import timezone as tz
    nom.status = StudentEquipmentNominationStatus.APPROVED
    nom.approved_at = tz.now()
    nom.approved_by = request.user
    nom.save(update_fields=["status", "approved_at", "approved_by"])
    # Intimate student via email
    try:
        portal_url = get_frontend_absolute_url("/my-nomination-requests") or get_frontend_absolute_url("/dashboard") or "IIC Booking Portal"
        academic_year_name = _academic_year_label_from_semester(nom.semester)
        context = {
            "student_name": nom.student.name or nom.student.email,
            "student_email": nom.student.email,
            "instrument_name": nom.equipment.name or nom.equipment.code,
            "instrument_code": nom.equipment.code,
            "semester_name": academic_year_name,
            "academic_year_name": academic_year_name,
            "portal_url": portal_url,
        }
        CommunicationService.send_email(
            recipient=nom.student,
            template="nomination_approved_student_email",
            template_context=context,
            created_by=request.user,
        )
    except Exception as e:
        logger.warning("Failed to send nomination approved email to student %s: %s", nom.student.email, e)
    return Response({"nomination": _nomination_to_dict(nom)}, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def reject_equipment_nomination(request, nomination_id):
    """Admin/OIC: reject a PENDING nomination. Optional body: { \"remarks\": \"...\" }."""
    if not check_operator_permission(request.user):
        return Response(
            {"error": "Only admin, manager (OIC), or operator can reject nominations."},
            status=status.HTTP_403_FORBIDDEN,
        )
    try:
        nom = StudentEquipmentNomination.objects.select_related("student", "equipment", "semester").get(pk=nomination_id)
    except StudentEquipmentNomination.DoesNotExist:
        return Response({"error": "Nomination not found."}, status=status.HTTP_404_NOT_FOUND)
    if request.user.user_type != UserType.ADMIN:
        oic_equipment_ids = get_equipment_ids_managed_by_oic(request.user.id)
        if nom.equipment_id not in oic_equipment_ids:
            return Response({"error": "You can only reject nominations for equipment you manage."}, status=status.HTTP_403_FORBIDDEN)
    if nom.status != StudentEquipmentNominationStatus.PENDING:
        return Response({"error": "Only PENDING nominations can be rejected."}, status=status.HTTP_400_BAD_REQUEST)
    remarks = (request.data.get("remarks") if hasattr(request, "data") and request.data else None) or ""
    from django.utils import timezone as tz
    nom.status = StudentEquipmentNominationStatus.REJECTED
    nom.approved_at = tz.now()
    nom.approved_by = request.user
    if remarks:
        nom.remarks = (nom.remarks or "") + ("\n" if nom.remarks else "") + remarks
    nom.save(update_fields=["status", "approved_at", "approved_by", "remarks"])
    # Intimate student via email
    try:
        portal_url = get_frontend_absolute_url("/my-nomination-requests") or get_frontend_absolute_url("/dashboard") or "IIC Booking Portal"
        academic_year_name = _academic_year_label_from_semester(nom.semester)
        context = {
            "student_name": nom.student.name or nom.student.email,
            "student_email": nom.student.email,
            "instrument_name": nom.equipment.name or nom.equipment.code,
            "instrument_code": nom.equipment.code,
            "semester_name": academic_year_name,
            "academic_year_name": academic_year_name,
            "remarks": remarks or "No additional remarks provided.",
            "portal_url": portal_url,
        }
        CommunicationService.send_email(
            recipient=nom.student,
            template="nomination_rejected_student_email",
            template_context=context,
            created_by=request.user,
        )
    except Exception as e:
        logger.warning("Failed to send nomination rejected email to student %s: %s", nom.student.email, e)
    return Response({"nomination": _nomination_to_dict(nom)}, status=status.HTTP_200_OK)


# ----- TA nomination call (OIC/Admin initiates; email to all Faculty) -----


def _academic_year_label_from_semester(semester_obj):
    if semester_obj is None:
        return ""
    text = f"{getattr(semester_obj, 'code', '')} {getattr(semester_obj, 'name', '')}".strip()
    m_short = re.search(r"\b(\d{4}-\d{2})\b", text)
    if m_short:
        return m_short.group(1)
    m_full = re.search(r"\b(\d{4}-\d{4})\b", text)
    if m_full:
        a, b = m_full.group(1).split("-")
        return f"{a}-{b[-2:]}"
    m_year = re.search(r"\b(20\d{2})\b", text)
    if m_year:
        return m_year.group(1)
    return getattr(semester_obj, "name", "") or getattr(semester_obj, "code", "") or ""


def _ta_call_to_dict(call):
    """Build API response dict for an EquipmentOperatingTACall."""
    academic_year_name = _academic_year_label_from_semester(getattr(call, "semester", None))
    return {
        "id": call.id,
        "equipment_id": call.equipment_id,
        "equipment_code": call.equipment.code,
        "equipment_name": call.equipment.name,
        "semester_id": call.semester_id,
        "semester_code": call.semester.code,
        "semester_name": academic_year_name,
        "academic_year_name": academic_year_name,
        "number_of_operators_required": call.number_of_operators_required,
        "eligibility_criteria": call.eligibility_criteria or "",
        "expected_duty_hours": call.expected_duty_hours or "",
        "expected_duty_time": getattr(call, "expected_duty_time", None) or "",
        "benefits": call.benefits or "",
        "nomination_deadline": call.nomination_deadline.isoformat() if call.nomination_deadline else None,
        "status": call.status,
        "created_by_id": call.created_by_id,
        "created_at": call.created_at.isoformat() if call.created_at else None,
        "email_sent_at": call.email_sent_at.isoformat() if call.email_sent_at else None,
    }


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def create_ta_nomination_call(request):
    """
    OIC or Admin initiates a TA nomination call for a particular equipment.
    OIC can only create for equipment they are marked as OIC; Admin can select any equipment.
    On success, sends an email to all Internal (Faculty) users with instrument name, number of
    operators required, eligibility criteria, expected duty hours, benefits, and nomination deadline.
    """
    if not check_operator_permission(request.user):
        return Response(
            {"error": "Only admin or OIC (manager) can initiate a TA nomination call."},
            status=status.HTTP_403_FORBIDDEN,
        )
    from iic_booking.users.models.user import User
    from django.utils import timezone as tz

    equipment_id = request.data.get("equipment_id")
    semester_id = request.data.get("semester_id")
    number_of_operators_required = request.data.get("number_of_operators_required")
    eligibility_criteria = (request.data.get("eligibility_criteria") or "").strip()
    expected_duty_hours = (request.data.get("expected_duty_hours") or "").strip()
    expected_duty_time = (request.data.get("expected_duty_time") or "").strip()
    benefits = (request.data.get("benefits") or "").strip()
    nomination_deadline_str = request.data.get("nomination_deadline")

    if not equipment_id or not semester_id or number_of_operators_required is None:
        return Response(
            {"error": "equipment_id, semester_id, and number_of_operators_required are required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        number_of_operators_required = int(number_of_operators_required)
    except (TypeError, ValueError):
        return Response(
            {"error": "number_of_operators_required must be a positive integer."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if number_of_operators_required < 1:
        return Response(
            {"error": "number_of_operators_required must be at least 1."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    if not nomination_deadline_str:
        return Response(
            {"error": "nomination_deadline is required."},
            status=status.HTTP_400_BAD_REQUEST,
        )
    try:
        from datetime import datetime
        nomination_deadline = datetime.strptime(nomination_deadline_str[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return Response(
            {"error": "nomination_deadline must be a date in YYYY-MM-DD format."},
            status=status.HTTP_400_BAD_REQUEST,
        )

    try:
        equipment = Equipment.objects.get(pk=equipment_id)
    except Equipment.DoesNotExist:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    if request.user.user_type != UserType.ADMIN:
        oic_equipment_ids = get_equipment_ids_managed_by_oic(request.user.id)
        if equipment_id not in oic_equipment_ids:
            return Response(
                {"error": "You can only initiate TA calls for equipment you are marked as OIC."},
                status=status.HTTP_403_FORBIDDEN,
            )
    try:
        semester = Semester.objects.get(pk=semester_id)
    except Semester.DoesNotExist:
        return Response({"error": "Semester not found."}, status=status.HTTP_404_NOT_FOUND)

    call = EquipmentOperatingTACall.objects.create(
        equipment=equipment,
        semester=semester,
        number_of_operators_required=number_of_operators_required,
        eligibility_criteria=eligibility_criteria or "",
        expected_duty_hours=expected_duty_hours or "",
        expected_duty_time=expected_duty_time or "",
        benefits=benefits or "",
        nomination_deadline=nomination_deadline,
        status=EquipmentOperatingTACallStatus.OPEN,
        created_by=request.user,
    )

    portal_url = get_frontend_absolute_url("/") or "IIC Booking Portal"
    academic_year_name = _academic_year_label_from_semester(semester)
    base_context = {
        "instrument_name": equipment.name or equipment.code,
        "instrument_code": equipment.code,
        "semester_name": academic_year_name,  # Backward-compatible key used by existing templates.
        "academic_year_name": academic_year_name,
        "number_of_operators_required": str(number_of_operators_required),
        "eligibility_criteria": eligibility_criteria or "Not specified.",
        "expected_duty_hours": expected_duty_hours or "Not specified.",
        "expected_duty_time": expected_duty_time or "Not specified.",
        "benefits": benefits or "Certificate and recognition for TA duty performance (details as per policy).",
        "nomination_deadline": nomination_deadline_str[:10],
        "portal_url": portal_url,
    }

    faculty_users = User.objects.filter(
        user_type=UserType.FACULTY,
        is_active=True,
    ).exclude(email="")
    sent_count = 0
    for faculty in faculty_users:
        try:
            context = {
                **base_context,
                "faculty_name": faculty.name or faculty.email,
                "faculty_email": faculty.email,
            }
            CommunicationService.send_email(
                recipient=faculty,
                template="ta_operating_nomination_call_email",
                template_context=context,
                created_by=request.user,
            )
            sent_count += 1
        except Exception as e:
            logger.warning("Failed to send TA nomination call email to %s: %s", faculty.email, e)

    call.email_sent_at = tz.now()
    call.save(update_fields=["email_sent_at"])

    return Response({
        "ta_call": _ta_call_to_dict(call),
        "message": f"TA nomination call created. Email sent to {sent_count} faculty member(s).",
        "emails_sent_count": sent_count,
    }, status=status.HTTP_201_CREATED)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_ta_nomination_calls(request):
    """List TA nomination calls. Admin sees all; OIC sees only calls for equipment they manage."""
    if not check_operator_permission(request.user):
        return Response(
            {"error": "Only admin or OIC (manager) can list TA nomination calls."},
            status=status.HTTP_403_FORBIDDEN,
        )
    qs = EquipmentOperatingTACall.objects.all().select_related("equipment", "semester", "created_by")
    if request.user.user_type != UserType.ADMIN:
        oic_equipment_ids = get_equipment_ids_managed_by_oic(request.user.id)
        if not oic_equipment_ids:
            qs = qs.none()
        else:
            qs = qs.filter(equipment_id__in=oic_equipment_ids)
    semester_id = request.query_params.get("semester_id")
    if semester_id:
        qs = qs.filter(semester_id=semester_id)
    equipment_id = request.query_params.get("equipment_id")
    if equipment_id:
        qs = qs.filter(equipment_id=equipment_id)
    status_filter = request.query_params.get("status")
    if status_filter and status_filter in ("OPEN", "CLOSED"):
        qs = qs.filter(status=status_filter)
    qs = qs.order_by("-created_at")
    return Response({"ta_calls": [_ta_call_to_dict(c) for c in qs]})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def list_open_ta_calls_faculty(request):
    """List TA nomination calls that are open for faculty to nominate (OPEN status, deadline not passed). Faculty only."""
    if request.user.user_type != UserType.FACULTY:
        return Response(
            {"error": "Only faculty can list open TA nomination calls."},
            status=status.HTTP_403_FORBIDDEN,
        )
    from django.utils import timezone as tz
    today = tz.localdate()
    qs = EquipmentOperatingTACall.objects.filter(
        status=EquipmentOperatingTACallStatus.OPEN,
        nomination_deadline__gte=today,
    ).select_related("equipment", "semester").order_by("nomination_deadline")
    return Response({"ta_calls": [_ta_call_to_dict(c) for c in qs]})


def _inventory_accessible_equipment_ids(user):
    if not user or not user.is_authenticated:
        return set()
    if user.user_type == UserType.ADMIN:
        return set(Equipment.objects.values_list("equipment_id", flat=True))

    ids = set(get_equipment_ids_managed_by_oic(user.id))
    now_ts = timezone.now()
    delegated_ids = EquipmentTemporaryOIC.objects.filter(
        temporary_oic=user,
        resume_at__gt=now_ts,
    ).values_list("equipment_id", flat=True)
    ids.update(delegated_ids)
    return ids


@extend_schema(
    methods=["GET"],
    tags=["Inventory"],
    summary="List inventory items",
    description="List inventory item master records. By default returns only active items.",
    parameters=[
        OpenApiParameter(
            name="active_only",
            type=int,
            location=OpenApiParameter.QUERY,
            required=False,
            description="1 to filter active items only (default), 0 for all.",
        )
    ],
    responses={
        200: inline_serializer(
            name="InventoryItemListResponse",
            fields={"items": InventoryItemSerializer(many=True)},
        )
    },
)
@extend_schema(
    methods=["POST"],
    tags=["Inventory"],
    summary="Create inventory item",
    description="Create a new inventory item master record (admin-panel users only).",
    request=InventoryItemSerializer,
    responses={201: InventoryItemSerializer},
)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def inventory_items_list(request):
    """List/create inventory item master records."""
    if not _require_admin_panel(request):
        return Response({"error": "Only admin-panel users can access this."}, status=status.HTTP_403_FORBIDDEN)
    if request.method == "POST":
        serializer = InventoryItemSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        item = serializer.save()
        return Response(InventoryItemSerializer(item).data, status=status.HTTP_201_CREATED)

    qs = InventoryItem.objects.all().order_by("name")
    if request.query_params.get("active_only", "1") == "1":
        qs = qs.filter(active=True)
    return Response({"items": InventoryItemSerializer(qs, many=True).data}, status=status.HTTP_200_OK)


@extend_schema(
    tags=["Inventory"],
    summary="Get equipment inventory stock",
    description="Return configured inventory items and current stock balances for one equipment.",
    responses={
        200: inline_serializer(
            name="EquipmentInventoryStockResponse",
            fields={
                "equipment_id": drf_serializers.IntegerField(),
                "configured_items": EquipmentInventoryItemSerializer(many=True),
                "stock": EquipmentItemStockSerializer(many=True),
            },
        )
    },
)
@api_view(["GET"])
@permission_classes([IsAuthenticated])
def equipment_inventory_stock(request, equipment_id):
    """List configured items and stock balances for an equipment."""
    allowed_ids = _inventory_accessible_equipment_ids(request.user)
    if request.user.user_type != UserType.ADMIN and equipment_id not in allowed_ids:
        return Response({"error": "You are not authorized for this equipment."}, status=status.HTTP_403_FORBIDDEN)

    mappings = (
        EquipmentInventoryItem.objects
        .filter(equipment_id=equipment_id, is_enabled=True)
        .select_related("item")
        .order_by("item__name")
    )
    stock_rows = (
        EquipmentItemStock.objects
        .filter(equipment_id=equipment_id)
        .select_related("item")
    )
    return Response(
        {
            "equipment_id": equipment_id,
            "configured_items": EquipmentInventoryItemSerializer(mappings, many=True).data,
            "stock": EquipmentItemStockSerializer(stock_rows, many=True).data,
        },
        status=status.HTTP_200_OK,
    )


@extend_schema(
    methods=["GET"],
    tags=["Inventory"],
    summary="List inventory requests",
    description="List inventory requests visible to the current user.",
    parameters=[
        OpenApiParameter(name="status", type=str, location=OpenApiParameter.QUERY, required=False),
        OpenApiParameter(name="equipment_id", type=int, location=OpenApiParameter.QUERY, required=False),
    ],
    responses={
        200: inline_serializer(
            name="InventoryRequestListResponse",
            fields={"requests": InventoryRequestSerializer(many=True)},
        )
    },
)
@extend_schema(
    methods=["POST"],
    tags=["Inventory"],
    summary="Create inventory request",
    description="Create request with one or more lines. Allowed for equipment OIC or active temporary OIC.",
    request=InventoryRequestSerializer,
    responses={201: InventoryRequestSerializer},
)
@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def inventory_requests(request):
    """Create/list inventory requests."""
    user = request.user
    if request.method == "GET":
        qs = InventoryRequest.objects.all().select_related("equipment", "requested_by", "decision_by").prefetch_related("lines__item")
        if user.user_type != UserType.ADMIN:
            allowed_ids = _inventory_accessible_equipment_ids(user)
            if not allowed_ids:
                return Response({"requests": []}, status=status.HTTP_200_OK)
            qs = qs.filter(equipment_id__in=allowed_ids)
        status_filter = request.query_params.get("status")
        if status_filter:
            qs = qs.filter(status=status_filter)
        equipment_id = request.query_params.get("equipment_id")
        if equipment_id:
            qs = qs.filter(equipment_id=equipment_id)
        qs = qs.order_by("-created_at")
        return Response({"requests": InventoryRequestSerializer(qs, many=True).data}, status=status.HTTP_200_OK)

    # POST
    equipment_id = request.data.get("equipment")
    if not equipment_id:
        return Response({"error": "equipment is required."}, status=status.HTTP_400_BAD_REQUEST)
    try:
        equipment = Equipment.objects.get(pk=int(equipment_id))
    except (Equipment.DoesNotExist, ValueError, TypeError):
        return Response({"error": "Invalid equipment."}, status=status.HTTP_400_BAD_REQUEST)

    if not InventoryRequest.is_user_authorized_for_equipment(user, equipment):
        return Response({"error": "Only OIC/temporary OIC for this equipment can raise requests."}, status=status.HTTP_403_FORBIDDEN)

    lines_payload = request.data.get("lines") or []
    if not isinstance(lines_payload, list) or not lines_payload:
        return Response({"error": "lines must be a non-empty list."}, status=status.HTTP_400_BAD_REQUEST)

    serializer = InventoryRequestSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    with transaction.atomic():
        inv_req = InventoryRequest.objects.create(
            equipment=equipment,
            requested_by=user,
            request_type=serializer.validated_data.get("request_type"),
            status=serializer.validated_data.get("status", InventoryRequestStatus.DRAFT),
            justification=serializer.validated_data.get("justification", ""),
            required_by_date=serializer.validated_data.get("required_by_date"),
            submitted_at=serializer.validated_data.get("submitted_at"),
        )
        for line in lines_payload:
            line_ser = InventoryRequestLineSerializer(data=line)
            line_ser.is_valid(raise_exception=True)
            InventoryRequestLine.objects.create(
                request=inv_req,
                item=line_ser.validated_data["item"],
                requested_qty=line_ser.validated_data.get("requested_qty", Decimal("0.000")),
                approved_qty=line_ser.validated_data.get("approved_qty", Decimal("0.000")),
                issued_qty=line_ser.validated_data.get("issued_qty", Decimal("0.000")),
                estimated_unit_cost=line_ser.validated_data.get("estimated_unit_cost"),
                remarks=line_ser.validated_data.get("remarks", ""),
            )
    return Response(InventoryRequestSerializer(inv_req).data, status=status.HTTP_201_CREATED)


@extend_schema(
    tags=["Inventory"],
    summary="Approve/reject inventory request",
    request=inline_serializer(
        name="InventoryRequestDecideRequest",
        fields={
            "action": drf_serializers.ChoiceField(choices=["APPROVE", "REJECT"]),
            "decision_note": drf_serializers.CharField(required=False, allow_blank=True),
        },
    ),
    responses={200: InventoryRequestSerializer},
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def inventory_request_decide(request, request_id):
    """Approve/reject inventory request."""
    if not _require_admin_panel(request):
        return Response({"error": "Only admin-panel users can decide requests."}, status=status.HTTP_403_FORBIDDEN)
    action = (request.data.get("action") or "").strip().upper()
    if action not in {"APPROVE", "REJECT"}:
        return Response({"error": "action must be APPROVE or REJECT."}, status=status.HTTP_400_BAD_REQUEST)

    try:
        inv_req = InventoryRequest.objects.select_for_update().get(pk=request_id)
    except InventoryRequest.DoesNotExist:
        return Response({"error": "Request not found."}, status=status.HTTP_404_NOT_FOUND)

    with transaction.atomic():
        inv_req = InventoryRequest.objects.select_for_update().get(pk=request_id)
        inv_req.status = InventoryRequestStatus.APPROVED if action == "APPROVE" else InventoryRequestStatus.REJECTED
        inv_req.decision_by = request.user
        inv_req.decision_at = timezone.now()
        inv_req.decision_note = (request.data.get("decision_note") or "").strip()
        inv_req.save(update_fields=["status", "decision_by", "decision_at", "decision_note", "updated_at"])

        # On approval, auto-issue from currently available stock.
        # This deducts inventory immediately for matching items and updates issued quantities.
        if action == "APPROVE":
            lines = list(InventoryRequestLine.objects.select_for_update().filter(request=inv_req).select_related("item"))
            for ln in lines:
                if ln.approved_qty <= 0:
                    ln.approved_qty = ln.requested_qty
                balance_to_issue = ln.approved_qty - ln.issued_qty
                if balance_to_issue <= 0:
                    ln.save(update_fields=["approved_qty", "updated_at"])
                    continue

                stock = EquipmentItemStock.objects.select_for_update().filter(
                    equipment=inv_req.equipment,
                    item=ln.item,
                ).first()
                available_qty = stock.current_qty if stock else Decimal("0.000")
                auto_issue_qty = min(available_qty, balance_to_issue)
                if auto_issue_qty > 0:
                    InventoryTransaction.objects.create(
                        equipment=inv_req.equipment,
                        item=ln.item,
                        tx_type=InventoryTransactionType.ISSUE,
                        quantity=auto_issue_qty,
                        unit_cost=ln.estimated_unit_cost,
                        reference_type="INVENTORY_REQUEST_AUTO_APPROVAL",
                        reference_id=str(inv_req.request_id),
                        performed_by=request.user,
                        remarks="Auto-issued from available stock on request approval.",
                    )
                    ln.issued_qty = ln.issued_qty + auto_issue_qty
                ln.save(update_fields=["approved_qty", "issued_qty", "updated_at"])

            all_lines = InventoryRequestLine.objects.filter(request=inv_req)
            if all(l.issued_qty >= l.approved_qty for l in all_lines):
                inv_req.status = InventoryRequestStatus.FULFILLED
            elif any(l.issued_qty > 0 for l in all_lines):
                inv_req.status = InventoryRequestStatus.PARTIALLY_FULFILLED
            else:
                inv_req.status = InventoryRequestStatus.APPROVED
            inv_req.save(update_fields=["status", "updated_at"])
    return Response(InventoryRequestSerializer(inv_req).data, status=status.HTTP_200_OK)


@extend_schema(
    tags=["Inventory"],
    summary="Add stock (receipt/opening)",
    description="Create a RECEIPT ledger entry to add current/available stock for an equipment item.",
    request=inline_serializer(
        name="InventoryStockAddRequest",
        fields={
            "equipment": drf_serializers.IntegerField(),
            "item": drf_serializers.IntegerField(),
            "quantity": drf_serializers.DecimalField(max_digits=12, decimal_places=3),
            "unit_cost": drf_serializers.DecimalField(max_digits=12, decimal_places=2, required=False),
            "remarks": drf_serializers.CharField(required=False, allow_blank=True),
        },
    ),
    responses={201: InventoryTransactionSerializer},
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def inventory_stock_add(request):
    if not _require_admin_panel(request):
        return Response({"error": "Only admin-panel users can add stock."}, status=status.HTTP_403_FORBIDDEN)

    equipment_id = request.data.get("equipment")
    item_id = request.data.get("item")
    quantity = Decimal(str(request.data.get("quantity", "0")))
    if not equipment_id or not item_id or quantity <= 0:
        return Response({"error": "equipment, item and positive quantity are required."}, status=status.HTTP_400_BAD_REQUEST)

    equipment = Equipment.objects.filter(pk=equipment_id).first()
    item = InventoryItem.objects.filter(pk=item_id).first()
    if not equipment or not item:
        return Response({"error": "Invalid equipment or item."}, status=status.HTTP_400_BAD_REQUEST)

    tx = InventoryTransaction.objects.create(
        equipment=equipment,
        item=item,
        tx_type=InventoryTransactionType.RECEIPT,
        quantity=quantity,
        unit_cost=request.data.get("unit_cost"),
        reference_type="MANUAL_STOCK_ADD",
        reference_id="",
        performed_by=request.user,
        remarks=(request.data.get("remarks") or "").strip(),
    )
    return Response(InventoryTransactionSerializer(tx).data, status=status.HTTP_201_CREATED)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def procurement_requests(request):
    if not _require_admin_panel(request):
        return Response({"error": "Only admin-panel users can access procurement requests."}, status=status.HTTP_403_FORBIDDEN)

    if request.method == "GET":
        qs = ProcurementRequest.objects.all().select_related("equipment", "initiated_by").prefetch_related("lines", "attachments", "action_logs")
        equipment_id = request.query_params.get("equipment_id")
        status_filter = request.query_params.get("status")
        if equipment_id:
            qs = qs.filter(equipment_id=equipment_id)
        if status_filter:
            qs = qs.filter(status=status_filter)
        return Response({"requests": ProcurementRequestSerializer(qs.order_by("-created_at"), many=True).data}, status=status.HTTP_200_OK)

    equipment_id = request.data.get("equipment")
    lines_payload = request.data.get("lines") or []
    if not equipment_id:
        return Response({"error": "equipment is required."}, status=status.HTTP_400_BAD_REQUEST)
    if not isinstance(lines_payload, list) or not lines_payload:
        return Response({"error": "lines must be a non-empty list."}, status=status.HTTP_400_BAD_REQUEST)
    equipment = Equipment.objects.filter(pk=equipment_id).first()
    if not equipment:
        return Response({"error": "Invalid equipment."}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        req = ProcurementRequest.objects.create(
            equipment=equipment,
            initiated_by=request.user,
            status=ProcurementRequestStatus.UNDER_OFFICE_VERIFICATION,
            remarks=(request.data.get("remarks") or "").strip(),
        )
        total = Decimal("0.00")
        for row in lines_payload:
            qty = Decimal(str(row.get("quantity", "0")))
            unit_cost = Decimal(str(row.get("tentative_unit_cost", "0")))
            line = ProcurementRequestLine.objects.create(
                request=req,
                item_master_id=row.get("item_master"),
                manual_item_name=(row.get("manual_item_name") or "").strip(),
                classification=row.get("classification"),
                quantity=qty,
                tentative_unit_cost=unit_cost,
            )
            total += line.tentative_total_cost
        req.total_estimated_cost = total
        initiator_ut = getattr(request.user, "user_type", None)
        if initiator_ut == UserType.OPERATOR:
            req.status = ProcurementRequestStatus.PENDING_OIC_REVIEW
            sub_msg = "Submitted by Lab Operator; pending Lab OIC endorsement."
        else:
            req.status = ProcurementRequestStatus.UNDER_OFFICE_VERIFICATION
            sub_msg = "Submitted; pending Office Superintendent verification."
        req.save(update_fields=["total_estimated_cost", "status", "updated_at"])
        ProcurementActionLog.objects.create(
            request=req,
            action=ProcurementActionType.SUBMITTED,
            by_user=request.user,
            comments=sub_msg,
        )
    return Response(ProcurementRequestSerializer(req).data, status=status.HTTP_201_CREATED)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def procurement_request_oic_endorse(request, request_id):
    """Lab OIC endorses or rejects a procurement request raised by a Lab Operator."""
    if not _require_admin_panel(request):
        return Response({"error": "Only admin-panel users can endorse procurement requests."}, status=status.HTTP_403_FORBIDDEN)
    req = ProcurementRequest.objects.select_related("equipment").filter(pk=request_id).first()
    if not req:
        return Response({"error": "Request not found."}, status=status.HTTP_404_NOT_FOUND)
    if req.status != ProcurementRequestStatus.PENDING_OIC_REVIEW:
        return Response({"error": "OIC action is only allowed when the request is pending Lab OIC review."}, status=status.HTTP_400_BAD_REQUEST)
    if not _user_can_procurement_oic_act(request, req.equipment):
        return Response({"error": "Only Admin or Officer In Charge for this equipment may endorse."}, status=status.HTTP_403_FORBIDDEN)
    decision = (request.data.get("decision") or "ENDORSE").upper()
    with transaction.atomic():
        req = ProcurementRequest.objects.select_for_update().select_related("equipment").get(pk=request_id)
        if req.status != ProcurementRequestStatus.PENDING_OIC_REVIEW:
            return Response({"error": "Request status changed; refresh and try again."}, status=status.HTTP_400_BAD_REQUEST)
        if decision == "REJECT":
            req.status = ProcurementRequestStatus.REJECTED_BY_OIC
            action = ProcurementActionType.OIC_REJECTED
        else:
            req.status = ProcurementRequestStatus.UNDER_OFFICE_VERIFICATION
            req.oic_endorsed_by = request.user
            req.oic_endorsed_at = timezone.now()
            action = ProcurementActionType.OIC_ENDORSED
        req.save()
        ProcurementActionLog.objects.create(
            request=req,
            action=action,
            by_user=request.user,
            comments=(request.data.get("comments") or "").strip(),
        )
    return Response(ProcurementRequestSerializer(req).data, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def procurement_request_office_verify(request, request_id):
    if not _require_admin_panel(request):
        return Response({"error": "Only admin-panel users can verify procurement requests."}, status=status.HTTP_403_FORBIDDEN)
    req = ProcurementRequest.objects.filter(pk=request_id).first()
    if not req:
        return Response({"error": "Request not found."}, status=status.HTTP_404_NOT_FOUND)
    terminal = {
        ProcurementRequestStatus.REJECTED_BY_OFFICE,
        ProcurementRequestStatus.REJECTED_BY_STORE,
        ProcurementRequestStatus.REJECTED_BY_HEAD,
        ProcurementRequestStatus.REJECTED_BY_OIC,
        ProcurementRequestStatus.CANCELLED,
    }
    if req.status in terminal:
        return Response({"error": "Request cannot be verified in current status."}, status=status.HTTP_400_BAD_REQUEST)
    if req.status == ProcurementRequestStatus.PENDING_OIC_REVIEW:
        return Response({"error": "Pending Lab OIC endorsement before Office Superintendent verification."}, status=status.HTTP_400_BAD_REQUEST)
    if req.status != ProcurementRequestStatus.UNDER_OFFICE_VERIFICATION:
        return Response({"error": "Office Superintendent verification applies only when the request is in office verification."}, status=status.HTTP_400_BAD_REQUEST)
    if not _supply_chain_role_permitted(request.user, EquipmentSupplyChainRole.OFFICE_SUPERINTENDENT):
        return Response({"error": "You are not designated as Office Superintendent for this workflow step."}, status=status.HTTP_403_FORBIDDEN)
    decision = (request.data.get("decision") or "VERIFY").upper()
    with transaction.atomic():
        req = ProcurementRequest.objects.select_for_update().get(pk=request_id)
        if decision == "REJECT":
            req.status = ProcurementRequestStatus.REJECTED_BY_OFFICE
            action = ProcurementActionType.OFFICE_REJECTED
        else:
            req.status = ProcurementRequestStatus.PENDING_STORE_APPROVAL
            req.office_verified_by = request.user
            req.office_verified_at = timezone.now()
            action = ProcurementActionType.OFFICE_VERIFIED
            corrections = request.data.get("line_corrections") or []
            if isinstance(corrections, list):
                for c in corrections:
                    line = ProcurementRequestLine.objects.filter(request=req, pk=c.get("line_id")).first()
                    if not line:
                        continue
                    line.office_corrected_name = (c.get("office_corrected_name") or line.office_corrected_name or "")
                    if c.get("office_corrected_classification"):
                        line.office_corrected_classification = c.get("office_corrected_classification")
                    if c.get("finalized_item_master"):
                        line.finalized_item_master_id = c.get("finalized_item_master")
                    line.save()
        req.save()
        ProcurementActionLog.objects.create(request=req, action=action, by_user=request.user, comments=(request.data.get("comments") or "").strip())
    return Response(ProcurementRequestSerializer(req).data, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def procurement_request_store_approve(request, request_id):
    if not _require_admin_panel(request):
        return Response({"error": "Only admin-panel users can approve procurement requests."}, status=status.HTTP_403_FORBIDDEN)
    req = ProcurementRequest.objects.filter(pk=request_id).first()
    if not req:
        return Response({"error": "Request not found."}, status=status.HTTP_404_NOT_FOUND)
    if req.status != ProcurementRequestStatus.PENDING_STORE_APPROVAL:
        return Response({"error": "Store approval is only allowed when the request is pending store approval."}, status=status.HTTP_400_BAD_REQUEST)
    if not _supply_chain_role_permitted(request.user, EquipmentSupplyChainRole.STORE_IN_CHARGE):
        return Response({"error": "You are not designated as Store In Charge for this workflow step."}, status=status.HTTP_403_FORBIDDEN)

    decision = (request.data.get("decision") or "APPROVE").upper()
    mode = (request.data.get("head_approval_mode") or ProcurementHeadApprovalMode.NOT_REQUIRED).upper()
    with transaction.atomic():
        req = ProcurementRequest.objects.select_for_update().get(pk=request_id)
        if decision == "REJECT":
            req.status = ProcurementRequestStatus.REJECTED_BY_STORE
            ProcurementActionLog.objects.create(request=req, action=ProcurementActionType.STORE_REJECTED, by_user=request.user, comments=(request.data.get("comments") or "").strip())
        else:
            req.store_approved_by = request.user
            req.store_approved_at = timezone.now()
            req.head_approval_mode = mode if mode in {ProcurementHeadApprovalMode.OFFLINE, ProcurementHeadApprovalMode.EMAIL, ProcurementHeadApprovalMode.NOT_REQUIRED} else ProcurementHeadApprovalMode.NOT_REQUIRED
            req.head_approval_required = req.head_approval_mode != ProcurementHeadApprovalMode.NOT_REQUIRED
            if req.head_approval_mode == ProcurementHeadApprovalMode.EMAIL:
                req.status = ProcurementRequestStatus.PENDING_HEAD_APPROVAL_EMAIL
            elif req.head_approval_mode == ProcurementHeadApprovalMode.OFFLINE:
                req.status = ProcurementRequestStatus.PENDING_HEAD_APPROVAL_OFFLINE
            else:
                req.status = ProcurementRequestStatus.HEAD_APPROVED
                req.head_approved_by = request.user
                req.head_approved_at = timezone.now()
            ProcurementActionLog.objects.create(request=req, action=ProcurementActionType.STORE_APPROVED, by_user=request.user, comments=(request.data.get("comments") or "").strip(), metadata={"head_approval_mode": req.head_approval_mode})
        req.save()
    return Response(ProcurementRequestSerializer(req).data, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def procurement_request_head_approve(request, request_id):
    if not _require_admin_panel(request):
        return Response({"error": "Only admin-panel users can record head approval."}, status=status.HTTP_403_FORBIDDEN)
    req = ProcurementRequest.objects.filter(pk=request_id).first()
    if not req:
        return Response({"error": "Request not found."}, status=status.HTTP_404_NOT_FOUND)
    if req.status not in {
        ProcurementRequestStatus.PENDING_HEAD_APPROVAL_EMAIL,
        ProcurementRequestStatus.PENDING_HEAD_APPROVAL_OFFLINE,
    }:
        return Response({"error": "Head approval applies only when head approval is pending."}, status=status.HTTP_400_BAD_REQUEST)
    if not _supply_chain_role_permitted(request.user, EquipmentSupplyChainRole.HEAD_OF_DEPARTMENT):
        return Response({"error": "You are not designated as Head of Department for this workflow step."}, status=status.HTTP_403_FORBIDDEN)
    decision = (request.data.get("decision") or "APPROVE").upper()
    with transaction.atomic():
        req = ProcurementRequest.objects.select_for_update().get(pk=request_id)
        if decision == "REJECT":
            req.status = ProcurementRequestStatus.REJECTED_BY_HEAD
            action = ProcurementActionType.HEAD_REJECTED
        else:
            req.status = ProcurementRequestStatus.HEAD_APPROVED
            req.head_approved_by = request.user
            req.head_approved_at = timezone.now()
            action = ProcurementActionType.HEAD_APPROVED
        req.save()
        ProcurementActionLog.objects.create(request=req, action=action, by_user=request.user, comments=(request.data.get("comments") or "").strip())
    return Response(ProcurementRequestSerializer(req).data, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def procurement_request_mark_purchase_complete(request, request_id):
    if not _require_admin_panel(request):
        return Response({"error": "Only admin-panel users can mark purchase completion."}, status=status.HTTP_403_FORBIDDEN)
    req = ProcurementRequest.objects.filter(pk=request_id).first()
    if not req:
        return Response({"error": "Request not found."}, status=status.HTTP_404_NOT_FOUND)
    if req.status != ProcurementRequestStatus.HEAD_APPROVED:
        return Response({"error": "Purchase completion is allowed only after head approval (or store waiver) is recorded."}, status=status.HTTP_400_BAD_REQUEST)
    if not _supply_chain_role_permitted(request.user, EquipmentSupplyChainRole.STORE_IN_CHARGE):
        return Response({"error": "You are not designated as Store In Charge for this workflow step."}, status=status.HTTP_403_FORBIDDEN)

    with transaction.atomic():
        req = ProcurementRequest.objects.select_for_update().get(pk=request_id)
        req.status = ProcurementRequestStatus.PURCHASE_COMPLETED_PENDING_OFFICE_SEEN
        req.purchase_completed_by = request.user
        req.purchase_completed_at = timezone.now()
        req.save()

        invoice_file = request.FILES.get("invoice_file")
        if invoice_file:
            ProcurementAttachment.objects.create(
                request=req,
                attachment_type=ProcurementAttachmentType.INVOICE,
                file=invoice_file,
                uploaded_by=request.user,
            )
        ProcurementActionLog.objects.create(request=req, action=ProcurementActionType.PURCHASE_MARKED_COMPLETE, by_user=request.user, comments=(request.data.get("comments") or "").strip())
    return Response(ProcurementRequestSerializer(req).data, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def procurement_request_mark_office_seen(request, request_id):
    if not _require_admin_panel(request):
        return Response({"error": "Only admin-panel users can mark office seen."}, status=status.HTTP_403_FORBIDDEN)
    req = ProcurementRequest.objects.filter(pk=request_id).first()
    if not req:
        return Response({"error": "Request not found."}, status=status.HTTP_404_NOT_FOUND)
    if req.status != ProcurementRequestStatus.PURCHASE_COMPLETED_PENDING_OFFICE_SEEN:
        return Response({"error": "Office seen applies only after purchase completion is recorded."}, status=status.HTTP_400_BAD_REQUEST)
    if not _supply_chain_role_permitted(request.user, EquipmentSupplyChainRole.OFFICE_SUPERINTENDENT):
        return Response({"error": "You are not designated as Office Superintendent for this workflow step."}, status=status.HTTP_403_FORBIDDEN)
    with transaction.atomic():
        req = ProcurementRequest.objects.select_for_update().get(pk=request_id)
        req.status = ProcurementRequestStatus.OFFICE_SEEN_COMPLETED
        req.office_seen_by = request.user
        req.office_seen_at = timezone.now()
        req.save()
        ProcurementActionLog.objects.create(request=req, action=ProcurementActionType.OFFICE_SEEN, by_user=request.user, comments=(request.data.get("comments") or "").strip())
        _ensure_procurement_office_seen_expense(req, request.user)
    return Response(ProcurementRequestSerializer(req).data, status=status.HTTP_200_OK)


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def equipment_lifecycle_equipment_choices(request):
    """Equipment pick-list for lifecycle hub (assigned operators, OICs, delegated OICs, or all for Admin)."""
    if not _require_admin_panel(request):
        return Response({"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
    eids = _lifecycle_accessible_equipment_ids(request.user)
    qs = Equipment.objects.filter(equipment_id__in=eids).order_by("code") if eids else Equipment.objects.none()
    return Response(
        {
            "equipments": [
                {"equipment_id": e.equipment_id, "code": e.code, "name": e.name}
                for e in qs
            ]
        },
        status=status.HTTP_200_OK,
    )


@api_view(["GET", "PATCH"])
@permission_classes([IsAuthenticated])
def equipment_lifecycle_detail(request, equipment_id):
    equipment = Equipment.objects.filter(pk=equipment_id).first()
    if not equipment:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    if equipment.equipment_id not in _lifecycle_accessible_equipment_ids(request.user):
        return Response({"error": "Not allowed to view this equipment lifecycle."}, status=status.HTTP_403_FORBIDDEN)
    if request.method == "GET":
        return Response(
            {
                "lifecycle": EquipmentLifecycleFieldsSerializer(equipment).data,
                "accessories": EquipmentAccessorySerializer(equipment.equipment_accessories.all(), many=True).data,
                "amc_contracts": EquipmentAMCContractSerializer(
                    equipment.amc_contracts.order_by("-start_date")[:80],
                    many=True,
                ).data,
                "expenses": EquipmentExpenseSerializer(
                    equipment.equipment_expenses.select_related("procurement_request", "amc_contract").order_by("-expense_date")[:200],
                    many=True,
                ).data,
                "write_off_requests": EquipmentWriteOffRequestSerializer(
                    EquipmentWriteOffRequest.objects.filter(equipment=equipment).prefetch_related("action_logs").order_by("-created_at")[:40],
                    many=True,
                ).data,
            },
            status=status.HTTP_200_OK,
        )
    if not _user_can_edit_equipment_lifecycle(request, equipment):
        return Response({"error": "Only Admin or Lab OIC may edit lifecycle master data."}, status=status.HTTP_403_FORBIDDEN)
    ser = EquipmentLifecycleFieldsSerializer(equipment, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    return Response(ser.data, status=status.HTTP_200_OK)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def equipment_amc_contracts(request, equipment_id):
    equipment = Equipment.objects.filter(pk=equipment_id).first()
    if not equipment:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    if equipment.equipment_id not in _lifecycle_accessible_equipment_ids(request.user):
        return Response({"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
    if not _user_can_edit_equipment_lifecycle(request, equipment):
        return Response({"error": "Only Admin or Lab OIC may manage AMC records."}, status=status.HTTP_403_FORBIDDEN)
    if request.method == "GET":
        qs = equipment.amc_contracts.order_by("-start_date")
        return Response({"contracts": EquipmentAMCContractSerializer(qs, many=True).data}, status=status.HTTP_200_OK)
    data_mutable = request.data.copy() if hasattr(request.data, "copy") else dict(request.data)
    data_mutable["equipment"] = equipment.equipment_id
    ser = EquipmentAMCContractSerializer(data=data_mutable)
    ser.is_valid(raise_exception=True)
    obj = ser.save(created_by=request.user)
    return Response(EquipmentAMCContractSerializer(obj).data, status=status.HTTP_201_CREATED)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def equipment_expenses(request, equipment_id):
    equipment = Equipment.objects.filter(pk=equipment_id).first()
    if not equipment:
        return Response({"error": "Equipment not found."}, status=status.HTTP_404_NOT_FOUND)
    if equipment.equipment_id not in _lifecycle_accessible_equipment_ids(request.user):
        return Response({"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
    if request.method == "GET":
        if not _user_can_view_equipment_lifecycle(request, equipment):
            return Response({"error": "Not allowed."}, status=status.HTTP_403_FORBIDDEN)
        qs = equipment.equipment_expenses.select_related("procurement_request", "amc_contract").order_by("-expense_date")
        return Response({"expenses": EquipmentExpenseSerializer(qs, many=True).data}, status=status.HTTP_200_OK)
    if not _user_can_record_equipment_expense(request, equipment):
        return Response({"error": "Not allowed to record expenses for this equipment."}, status=status.HTTP_403_FORBIDDEN)
    raw = request.data
    data = raw.dict() if hasattr(raw, "dict") else dict(raw)
    data["equipment"] = equipment.equipment_id
    ser = EquipmentExpenseSerializer(data=data)
    ser.is_valid(raise_exception=True)
    obj = ser.save(created_by=request.user)
    return Response(EquipmentExpenseSerializer(obj).data, status=status.HTTP_201_CREATED)


@api_view(["GET", "POST"])
@permission_classes([IsAuthenticated])
def equipment_write_off_requests(request):
    if not _require_admin_panel(request):
        return Response({"error": "Only admin-panel users may access write-off requests."}, status=status.HTTP_403_FORBIDDEN)
    if request.method == "GET":
        qs = EquipmentWriteOffRequest.objects.select_related("equipment", "initiated_by").prefetch_related("action_logs")
        eid = request.query_params.get("equipment_id")
        if eid:
            qs = qs.filter(equipment_id=eid)
        st = request.query_params.get("status")
        if st:
            qs = qs.filter(status=st)
        ut = getattr(request.user, "user_type", None)
        if ut not in (UserType.ADMIN, UserType.FINANCE):
            qs = qs.filter(equipment_id__in=_lifecycle_accessible_equipment_ids(request.user))
        return Response({"requests": EquipmentWriteOffRequestSerializer(qs.order_by("-created_at")[:200], many=True).data}, status=status.HTTP_200_OK)
    equipment_id = request.data.get("equipment")
    if not equipment_id:
        return Response({"error": "equipment is required."}, status=status.HTTP_400_BAD_REQUEST)
    equipment = Equipment.objects.filter(pk=equipment_id).first()
    if not equipment:
        return Response({"error": "Invalid equipment."}, status=status.HTTP_400_BAD_REQUEST)
    if not _user_can_initiate_write_off(request, equipment):
        return Response({"error": "Only Admin or Lab OIC for this equipment may initiate write-off."}, status=status.HTTP_403_FORBIDDEN)
    reason = (request.data.get("reason") or "").strip()
    if not reason:
        return Response({"error": "reason is required."}, status=status.HTTP_400_BAD_REQUEST)
    erv_raw = request.data.get("estimated_residual_value")
    erv = None
    if erv_raw not in (None, ""):
        try:
            erv = Decimal(str(erv_raw))
        except Exception:
            return Response({"error": "Invalid estimated_residual_value."}, status=status.HTTP_400_BAD_REQUEST)
    wo = EquipmentWriteOffRequest.objects.create(
        equipment=equipment,
        initiated_by=request.user,
        reason=reason,
        asset_classification=(request.data.get("asset_classification") or "").strip(),
        estimated_residual_value=erv,
        status=EquipmentWriteOffStatus.PENDING_OFFICE,
    )
    EquipmentWriteOffActionLog.objects.create(
        request=wo,
        action=EquipmentWriteOffActionType.SUBMITTED,
        by_user=request.user,
        comments="Write-off request submitted.",
    )
    return Response(EquipmentWriteOffRequestSerializer(wo).data, status=status.HTTP_201_CREATED)


def _write_off_office_action(request, wo_id, forward: bool):
    if not _require_admin_panel(request):
        return Response({"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
    if not _supply_chain_role_permitted(request.user, EquipmentSupplyChainRole.OFFICE_SUPERINTENDENT):
        return Response({"error": "Office Superintendent role required."}, status=status.HTTP_403_FORBIDDEN)
    wo = EquipmentWriteOffRequest.objects.select_related("equipment").filter(pk=wo_id).first()
    if not wo:
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    if wo.status != EquipmentWriteOffStatus.PENDING_OFFICE:
        return Response({"error": "Invalid status for office action."}, status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        wo = EquipmentWriteOffRequest.objects.select_for_update().get(pk=wo_id)
        if forward:
            wo.status = EquipmentWriteOffStatus.PENDING_STORE
            wo.office_reviewed_by = request.user
            wo.office_reviewed_at = timezone.now()
            act = EquipmentWriteOffActionType.OFFICE_FORWARDED
        else:
            wo.status = EquipmentWriteOffStatus.REJECTED
            wo.rejection_comments = (request.data.get("comments") or "").strip()
            wo.office_reviewed_by = request.user
            wo.office_reviewed_at = timezone.now()
            act = EquipmentWriteOffActionType.OFFICE_REJECTED
        wo.save()
        EquipmentWriteOffActionLog.objects.create(request=wo, action=act, by_user=request.user, comments=(request.data.get("comments") or "").strip())
    return Response(EquipmentWriteOffRequestSerializer(wo).data, status=status.HTTP_200_OK)


def _write_off_store_action(request, wo_id, forward: bool):
    if not _require_admin_panel(request):
        return Response({"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
    if not _supply_chain_role_permitted(request.user, EquipmentSupplyChainRole.STORE_IN_CHARGE):
        return Response({"error": "Store In Charge role required."}, status=status.HTTP_403_FORBIDDEN)
    wo = EquipmentWriteOffRequest.objects.filter(pk=wo_id).first()
    if not wo:
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    if wo.status != EquipmentWriteOffStatus.PENDING_STORE:
        return Response({"error": "Invalid status for store action."}, status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        wo = EquipmentWriteOffRequest.objects.select_for_update().get(pk=wo_id)
        if forward:
            wo.status = EquipmentWriteOffStatus.PENDING_HEAD
            wo.store_reviewed_by = request.user
            wo.store_reviewed_at = timezone.now()
            act = EquipmentWriteOffActionType.STORE_FORWARDED
        else:
            wo.status = EquipmentWriteOffStatus.REJECTED
            wo.rejection_comments = (request.data.get("comments") or "").strip()
            wo.store_reviewed_by = request.user
            wo.store_reviewed_at = timezone.now()
            act = EquipmentWriteOffActionType.STORE_REJECTED
        wo.save()
        EquipmentWriteOffActionLog.objects.create(request=wo, action=act, by_user=request.user, comments=(request.data.get("comments") or "").strip())
    return Response(EquipmentWriteOffRequestSerializer(wo).data, status=status.HTTP_200_OK)


def _write_off_head_action(request, wo_id, approve: bool):
    if not _require_admin_panel(request):
        return Response({"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
    if not _supply_chain_role_permitted(request.user, EquipmentSupplyChainRole.HEAD_OF_DEPARTMENT):
        return Response({"error": "Head of Department role required."}, status=status.HTTP_403_FORBIDDEN)
    wo = EquipmentWriteOffRequest.objects.filter(pk=wo_id).first()
    if not wo:
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    if wo.status != EquipmentWriteOffStatus.PENDING_HEAD:
        return Response({"error": "Invalid status for head action."}, status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        wo = EquipmentWriteOffRequest.objects.select_for_update().get(pk=wo_id)
        if approve:
            wo.status = EquipmentWriteOffStatus.APPROVED
            wo.head_reviewed_by = request.user
            wo.head_reviewed_at = timezone.now()
            act = EquipmentWriteOffActionType.HEAD_APPROVED
        else:
            wo.status = EquipmentWriteOffStatus.REJECTED
            wo.rejection_comments = (request.data.get("comments") or "").strip()
            wo.head_reviewed_by = request.user
            wo.head_reviewed_at = timezone.now()
            act = EquipmentWriteOffActionType.HEAD_REJECTED
        wo.save()
        EquipmentWriteOffActionLog.objects.create(request=wo, action=act, by_user=request.user, comments=(request.data.get("comments") or "").strip())
    return Response(EquipmentWriteOffRequestSerializer(wo).data, status=status.HTTP_200_OK)


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def equipment_write_off_office_action(request, write_off_id):
    decision = (request.data.get("decision") or "FORWARD").upper()
    return _write_off_office_action(request, write_off_id, forward=decision != "REJECT")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def equipment_write_off_store_action(request, write_off_id):
    decision = (request.data.get("decision") or "FORWARD").upper()
    return _write_off_store_action(request, write_off_id, forward=decision != "REJECT")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def equipment_write_off_head_action(request, write_off_id):
    decision = (request.data.get("decision") or "APPROVE").upper()
    return _write_off_head_action(request, write_off_id, approve=decision != "REJECT")


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def equipment_write_off_execute(request, write_off_id):
    if not _require_admin_panel(request):
        return Response({"error": "Forbidden."}, status=status.HTTP_403_FORBIDDEN)
    if not _supply_chain_role_permitted(request.user, EquipmentSupplyChainRole.STORE_IN_CHARGE):
        return Response({"error": "Store In Charge role required to execute write-off."}, status=status.HTTP_403_FORBIDDEN)
    wo = EquipmentWriteOffRequest.objects.select_related("equipment").filter(pk=write_off_id).first()
    if not wo:
        return Response({"error": "Not found."}, status=status.HTTP_404_NOT_FOUND)
    if wo.status != EquipmentWriteOffStatus.APPROVED:
        return Response({"error": "Only approved requests can be executed."}, status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        wo = EquipmentWriteOffRequest.objects.select_for_update().select_related("equipment").get(pk=write_off_id)
        eq = wo.equipment
        eq.status = EquipmentStatus.DISPOSED
        eq.save(update_fields=["status", "updated_at"])
        wo.status = EquipmentWriteOffStatus.EXECUTED
        wo.executed_by = request.user
        wo.executed_at = timezone.now()
        wo.save()
        EquipmentWriteOffActionLog.objects.create(
            request=wo,
            action=EquipmentWriteOffActionType.EXECUTED,
            by_user=request.user,
            comments=(request.data.get("comments") or "").strip(),
        )
    return Response(EquipmentWriteOffRequestSerializer(wo).data, status=status.HTTP_200_OK)


@extend_schema(
    tags=["Inventory"],
    summary="Issue inventory request lines",
    description="Issue quantities against approved request lines; creates ledger transactions.",
    request=inline_serializer(
        name="InventoryIssueRequest",
        fields={
            "lines": drf_serializers.ListField(
                child=inline_serializer(
                    name="InventoryIssueRequestLine",
                    fields={
                        "line_id": drf_serializers.IntegerField(),
                        "issue_qty": drf_serializers.DecimalField(max_digits=12, decimal_places=3),
                        "remarks": drf_serializers.CharField(required=False, allow_blank=True),
                        "serial_no": drf_serializers.CharField(required=False, allow_blank=True),
                        "issued_to": drf_serializers.IntegerField(required=False),
                        "condition_on_issue": drf_serializers.CharField(required=False, allow_blank=True),
                    },
                )
            )
        },
    ),
    responses={
        200: inline_serializer(
            name="InventoryIssueResponse",
            fields={
                "request": InventoryRequestSerializer(),
                "transactions": InventoryTransactionSerializer(many=True),
            },
        )
    },
)
@api_view(["POST"])
@permission_classes([IsAuthenticated])
def inventory_request_issue(request, request_id):
    """Issue items for an approved request; creates ledger transactions and updates stock."""
    if not _require_admin_panel(request):
        return Response({"error": "Only admin-panel users can issue items."}, status=status.HTTP_403_FORBIDDEN)

    lines_payload = request.data.get("lines") or []
    if not isinstance(lines_payload, list) or not lines_payload:
        return Response({"error": "lines must be a non-empty list."}, status=status.HTTP_400_BAD_REQUEST)

    with transaction.atomic():
        try:
            inv_req = InventoryRequest.objects.select_for_update().get(pk=request_id)
        except InventoryRequest.DoesNotExist:
            return Response({"error": "Request not found."}, status=status.HTTP_404_NOT_FOUND)
        if inv_req.status not in {InventoryRequestStatus.APPROVED, InventoryRequestStatus.PARTIALLY_FULFILLED}:
            return Response({"error": "Only approved/partially fulfilled requests can be issued."}, status=status.HTTP_400_BAD_REQUEST)

        line_map = {ln.id: ln for ln in InventoryRequestLine.objects.select_for_update().filter(request=inv_req)}
        tx_rows = []
        for row in lines_payload:
            line_id = row.get("line_id")
            issue_qty = Decimal(str(row.get("issue_qty", "0")))
            if not line_id or issue_qty <= 0:
                return Response({"error": "Each line needs line_id and positive issue_qty."}, status=status.HTTP_400_BAD_REQUEST)
            req_line = line_map.get(int(line_id))
            if not req_line:
                return Response({"error": f"line_id {line_id} not found for request."}, status=status.HTTP_400_BAD_REQUEST)
            remaining = req_line.approved_qty - req_line.issued_qty
            if issue_qty > remaining:
                return Response({"error": f"Issue qty exceeds approved balance for line {line_id}."}, status=status.HTTP_400_BAD_REQUEST)

            tx = InventoryTransaction.objects.create(
                equipment=inv_req.equipment,
                item=req_line.item,
                tx_type=InventoryTransactionType.ISSUE,
                quantity=issue_qty,
                unit_cost=req_line.estimated_unit_cost,
                reference_type="INVENTORY_REQUEST",
                reference_id=str(inv_req.request_id),
                performed_by=request.user,
                remarks=(row.get("remarks") or "").strip(),
            )
            tx_rows.append(tx)

            req_line.issued_qty = req_line.issued_qty + issue_qty
            req_line.save(update_fields=["issued_qty", "updated_at"])

            # Optional non-consumable asset tracking at issue time.
            serial_no = (row.get("serial_no") or "").strip()
            issued_to_id = row.get("issued_to")
            if req_line.item.category in {"MAS", "MIA_LLTA"} and (serial_no or issued_to_id):
                issued_to = None
                if issued_to_id:
                    issued_to = User.objects.filter(pk=issued_to_id).first()
                IssuedAsset.objects.create(
                    equipment=inv_req.equipment,
                    item=req_line.item,
                    serial_no=serial_no,
                    issued_to=issued_to,
                    condition_on_issue=(row.get("condition_on_issue") or "").strip(),
                )

        all_lines = InventoryRequestLine.objects.filter(request=inv_req)
        if all(l.issued_qty >= l.approved_qty for l in all_lines):
            inv_req.status = InventoryRequestStatus.FULFILLED
        else:
            inv_req.status = InventoryRequestStatus.PARTIALLY_FULFILLED
        inv_req.save(update_fields=["status", "updated_at"])

    return Response(
        {
            "request": InventoryRequestSerializer(inv_req).data,
            "transactions": InventoryTransactionSerializer(tx_rows, many=True).data,
        },
        status=status.HTTP_200_OK,
    )
