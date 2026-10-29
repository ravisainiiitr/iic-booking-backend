"""Courier tracking vocabulary: arrival modes, the internal shipment status model and TrackCourier.io codes."""

from __future__ import annotations

import re

from django.db import models


class ArrivalMode(models.TextChoices):
    MANUAL = "MANUAL", "Manual delivery (bring the sample in person)"
    COURIER = "COURIER", "Courier / postal shipment"


class ShipmentStatus(models.TextChoices):
    """Internal status of a courier shipment. ``DELIVERY_REPORTED`` is the courier's claim, not laboratory receipt."""

    REGISTERED = "REGISTERED", "Registered (not yet checked)"
    INFO_RECEIVED = "INFO_RECEIVED", "Booked with courier"
    PENDING = "PENDING", "Awaiting courier update"
    IN_TRANSIT = "IN_TRANSIT", "In transit"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY", "Out for delivery"
    ATTEMPT_FAILED = "ATTEMPT_FAILED", "Delivery attempt failed"
    DELIVERY_REPORTED = "DELIVERY_REPORTED", "Delivery reported by courier"
    EXCEPTION = "EXCEPTION", "Courier reports a problem"
    RETURNED = "RETURNED", "Returned to sender"
    CANCELLED = "CANCELLED", "Cancelled"
    EXPIRED = "EXPIRED", "Tracking expired"
    TRACKING_PROBLEM = "TRACKING_PROBLEM", "Tracking details need attention"


# No further lookups: the courier will report nothing new (or the details cannot be tracked as given).
TERMINAL_STATUSES = frozenset(
    {
        ShipmentStatus.DELIVERY_REPORTED,
        ShipmentStatus.RETURNED,
        ShipmentStatus.CANCELLED,
        ShipmentStatus.EXPIRED,
    }
)
# Worth checking sooner than routine transit.
PRIORITY_STATUSES = frozenset(
    {ShipmentStatus.OUT_FOR_DELIVERY, ShipmentStatus.ATTEMPT_FAILED, ShipmentStatus.EXCEPTION}
)

# GET /v1/track ShipmentState / CheckpointState values (documented, exact spelling).
PROVIDER_STATE_MAP = {
    "inforeceived": ShipmentStatus.INFO_RECEIVED,
    "pending": ShipmentStatus.PENDING,
    "intransit": ShipmentStatus.IN_TRANSIT,
    "outfordelivery": ShipmentStatus.OUT_FOR_DELIVERY,
    "attemptfail": ShipmentStatus.ATTEMPT_FAILED,
    "delivered": ShipmentStatus.DELIVERY_REPORTED,
    "exception": ShipmentStatus.EXCEPTION,
    "expired": ShipmentStatus.EXPIRED,
}
# Webhook ``data.status`` values (snake_case). ``returned`` / ``cancelled`` arrive with tracking.exception and are
# terminal; ``exception`` is not.
WEBHOOK_STATE_MAP = {
    **PROVIDER_STATE_MAP,
    "in_transit": ShipmentStatus.IN_TRANSIT,
    "out_for_delivery": ShipmentStatus.OUT_FOR_DELIVERY,
    "info_received": ShipmentStatus.INFO_RECEIVED,
    "attempt_fail": ShipmentStatus.ATTEMPT_FAILED,
    "returned": ShipmentStatus.RETURNED,
    "cancelled": ShipmentStatus.CANCELLED,
}

WEBHOOK_EVENTS = frozenset(
    {
        "tracking.updated",
        "tracking.in_transit",
        "tracking.out_for_delivery",
        "tracking.delivered",
        "tracking.exception",
    }
)
SYNTHETIC_TEST_COURIER_PREFIX = "synthetic-test-"


class ProviderErrorCode:
    """``error.code`` values documented by TrackCourier.io. Branch on these, not on the HTTP status."""

    API_KEY_REQUIRED = "API_KEY_REQUIRED"
    INVALID_API_KEY = "INVALID_API_KEY"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    ACCOUNT_NEEDS_ATTENTION = "ACCOUNT_NEEDS_ATTENTION"
    COURIER_NOT_FOUND = "COURIER_NOT_FOUND"
    INVALID_TRACKING_NUMBER = "INVALID_TRACKING_NUMBER"
    COURIER_NOT_SUPPORTED = "COURIER_NOT_SUPPORTED"
    TRACKING_ERROR = "TRACKING_ERROR"
    TIMEOUT = "TIMEOUT"
    SERVICE_ERROR = "SERVICE_ERROR"


class LookupTrigger(models.TextChoices):
    INITIAL = "INITIAL", "First lookup after registration"
    SCHEDULED = "SCHEDULED", "Scheduled check"
    USER = "USER", "Refresh requested by the user"
    STAFF = "STAFF", "Refresh requested by staff"


class LookupOutcome(models.TextChoices):
    RESERVED = "RESERVED", "In flight"
    SUCCESS = "SUCCESS", "Tracking returned"
    INVALID_TRACKING_NUMBER = "INVALID_TRACKING_NUMBER", "Invalid tracking number"
    COURIER_NOT_FOUND = "COURIER_NOT_FOUND", "Unknown courier"
    COURIER_NOT_SUPPORTED = "COURIER_NOT_SUPPORTED", "Courier not supported"
    TRACKING_ERROR = "TRACKING_ERROR", "Courier could not track the number"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED", "Monthly quota exceeded"
    ACCOUNT_NEEDS_ATTENTION = "ACCOUNT_NEEDS_ATTENTION", "Provider account needs attention"
    AUTH_ERROR = "AUTH_ERROR", "API key missing or invalid"
    TIMEOUT = "TIMEOUT", "Courier or provider timed out"
    SERVICE_ERROR = "SERVICE_ERROR", "Temporary provider error"
    NETWORK_ERROR = "NETWORK_ERROR", "Provider unreachable"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE", "Unreadable provider response"
    ABANDONED = "ABANDONED", "Reservation never completed"


# Outcomes after which the shipment's details must be fixed by the user before another lookup is worth a request.
DETAIL_PROBLEM_OUTCOMES = frozenset(
    {
        LookupOutcome.INVALID_TRACKING_NUMBER,
        LookupOutcome.COURIER_NOT_FOUND,
        LookupOutcome.COURIER_NOT_SUPPORTED,
        LookupOutcome.TRACKING_ERROR,
    }
)
# Outcomes that stop every lookup until an administrator acts (or the month rolls over for quota).
ACCOUNT_BLOCKING_OUTCOMES = frozenset(
    {LookupOutcome.QUOTA_EXCEEDED, LookupOutcome.ACCOUNT_NEEDS_ATTENTION, LookupOutcome.AUTH_ERROR}
)
# Outcomes that did not use the quota: INVALID_TRACKING_NUMBER is documented as "Nothing was looked up or counted";
# a request refused for quota or authentication was not served. Every other outcome is counted against the local
# budget, conservatively (the provider's ``usage`` block, when present, is authoritative).
NOT_CONSUMED_OUTCOMES = frozenset(
    {
        LookupOutcome.INVALID_TRACKING_NUMBER,
        LookupOutcome.QUOTA_EXCEEDED,
        LookupOutcome.AUTH_ERROR,
    }
)

ERROR_CODE_OUTCOMES = {
    ProviderErrorCode.API_KEY_REQUIRED: LookupOutcome.AUTH_ERROR,
    ProviderErrorCode.INVALID_API_KEY: LookupOutcome.AUTH_ERROR,
    ProviderErrorCode.QUOTA_EXCEEDED: LookupOutcome.QUOTA_EXCEEDED,
    ProviderErrorCode.ACCOUNT_NEEDS_ATTENTION: LookupOutcome.ACCOUNT_NEEDS_ATTENTION,
    ProviderErrorCode.COURIER_NOT_FOUND: LookupOutcome.COURIER_NOT_FOUND,
    ProviderErrorCode.INVALID_TRACKING_NUMBER: LookupOutcome.INVALID_TRACKING_NUMBER,
    ProviderErrorCode.COURIER_NOT_SUPPORTED: LookupOutcome.COURIER_NOT_SUPPORTED,
    ProviderErrorCode.TRACKING_ERROR: LookupOutcome.TRACKING_ERROR,
    ProviderErrorCode.TIMEOUT: LookupOutcome.TIMEOUT,
    ProviderErrorCode.SERVICE_ERROR: LookupOutcome.SERVICE_ERROR,
}

# India Post family: 13 characters, two letters, nine digits, two letters (e.g. EE123456789IN). The provider removes
# spaces and upper-cases the number; we apply the same normalisation to every courier and validate only these.
POSTAL_FORMAT_SLUGS = frozenset({"indiapost", "speedpost", "registeredpost", "indiapostinternational"})
POSTAL_TRACKING_RE = re.compile(r"^[A-Z]{2}\d{9}[A-Z]{2}$")
# Generic bounds for any courier (no courier-specific format is invented).
TRACKING_NUMBER_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-/]{3,48}[A-Z0-9]$")

# Couriers the provider follows only when looked up (no re-checks of its own; webhooks never arrive for them).
POLL_ONLY_SLUGS = frozenset({"arccarrier", "omlogistics"})

# Booking statuses in which a sample may still be on its way to the lab.
ARRIVAL_OPEN_BOOKING_STATUSES = ("PENDING", "PENDING_PAYMENT", "BOOKED", "HOLD")

# Booking statuses in which a registered shipment is looked up (spends quota). Unpaid, held, cancelled or finished
# bookings keep their registration but are not looked up; webhooks (no quota) still apply.
LOOKUP_BOOKING_STATUSES = ("BOOKED",)
# Booking statuses in which a user is reminded to say how the sample will arrive (paid, confirmed bookings only).
ARRIVAL_REMINDER_BOOKING_STATUSES = ("BOOKED",)

# Sample-trace statuses the booking user records. Any other status is recorded by the lab, so the sample is (or was)
# in the lab's hands and the arrival declaration is frozen.
USER_TRACE_STATUSES = ("SAMPLE_SENT",)

# Abuse limits on registrations (each new courier registration may cost a first lookup).
MAX_COURIER_REGISTRATIONS_PER_BOOKING = 3
MAX_COURIER_REGISTRATIONS_PER_USER_PER_DAY = 5
# Staff refreshes may use the safety reserve and pass the per-shipment cap, so each staff member gets a daily limit.
STAFF_REFRESH_DAILY_LIMIT = 10
