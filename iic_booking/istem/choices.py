"""State and code enums for the I-STEM integration (https://www.istem.gov.in/).

I-STEM's own code lists (service, usage, analysis, user type, unit) are not known yet, so they are stored as
admin-entered values in ``IstemCodeMapping`` rather than enumerated here.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _


class UserLinkState(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    LINKED = "LINKED", _("Linked")
    NEEDS_REVIEW = "NEEDS_REVIEW", _("Needs review")
    DISABLED = "DISABLED", _("Disabled")


class CodeType(models.TextChoices):
    SERVICE_TYPE = "SERVICE_TYPE", _("Service type")
    USAGE_TYPE = "USAGE_TYPE", _("Usage type")
    USER_TYPE = "USER_TYPE", _("User type")
    ANALYSIS_TYPE = "ANALYSIS_TYPE", _("Analysis type")
    EQUIPMENT_UOM = "EQUIPMENT_UOM", _("Equipment unit of measure")


class SyncState(models.TextChoices):
    """Where a booking stands in the Create → Confirm → Execute sequence on I-STEM.

    ``*_UNCERTAIN`` means the request may have been processed remotely; it is never re-sent automatically.
    """

    NOT_ELIGIBLE = "NOT_ELIGIBLE", _("Not eligible")
    BLOCKED_CONFIG = "BLOCKED_CONFIG", _("Blocked: configuration incomplete")
    USER_PENDING = "USER_PENDING", _("Waiting for I-STEM user")
    CREATE_READY = "CREATE_READY", _("Create ready")
    CREATE_IN_FLIGHT = "CREATE_IN_FLIGHT", _("Create in flight")
    CREATE_FAILED = "CREATE_FAILED", _("Create failed")
    CREATE_UNCERTAIN = "CREATE_UNCERTAIN", _("Create outcome uncertain")
    CREATED = "CREATED", _("Created")
    CONFIRM_READY = "CONFIRM_READY", _("Confirm ready")
    CONFIRM_IN_FLIGHT = "CONFIRM_IN_FLIGHT", _("Confirm in flight")
    CONFIRM_FAILED = "CONFIRM_FAILED", _("Confirm failed")
    CONFIRM_UNCERTAIN = "CONFIRM_UNCERTAIN", _("Confirm outcome uncertain")
    CONFIRMED = "CONFIRMED", _("Confirmed")
    CONFIRMED_ID_MISSING = "CONFIRMED_ID_MISSING", _("Confirmed, confirmation ID missing")
    EXECUTE_READY = "EXECUTE_READY", _("Execute ready")
    EXECUTE_IN_FLIGHT = "EXECUTE_IN_FLIGHT", _("Execute in flight")
    EXECUTE_FAILED = "EXECUTE_FAILED", _("Execute failed")
    EXECUTE_UNCERTAIN = "EXECUTE_UNCERTAIN", _("Execute outcome uncertain")
    EXECUTED_PENDING_VERIFY = "EXECUTED_PENDING_VERIFY", _("Executed, awaiting status check")
    EXECUTED = "EXECUTED", _("Executed")
    REMOTE_CANCELLED = "REMOTE_CANCELLED", _("Cancelled on I-STEM")
    REMOTE_REJECTED = "REMOTE_REJECTED", _("Rejected on I-STEM")
    LOCAL_CLOSED_REMOTE_OPEN = "LOCAL_CLOSED_REMOTE_OPEN", _("Closed locally, open on I-STEM")
    MANUAL_REVIEW = "MANUAL_REVIEW", _("Manual review")
    MANUAL_LINKED = "MANUAL_LINKED", _("Linked manually")
    ABANDONED = "ABANDONED", _("Abandoned (manual FBR flow)")


class RemoteStatus(models.TextChoices):
    """Statuses documented for I-STEM Get Bookings, plus UNKNOWN for anything else."""

    PENDING = "PENDING", _("Pending")
    SLOT_ALLOCATED = "SLOT_ALLOCATED", _("Slot Allocated")
    EXECUTED = "EXECUTED", _("Executed")
    CANCELLED = "CANCELLED", _("Cancelled")
    REJECTED = "REJECTED", _("Rejected")
    UNKNOWN = "UNKNOWN", _("Unknown")


class Operation(models.TextChoices):
    ENSURE_USER = "ENSURE_USER", _("Create / look up user")
    CREATE_BOOKING = "CREATE_BOOKING", _("Create booking")
    CONFIRM_BOOKING = "CONFIRM_BOOKING", _("Confirm booking")
    EXECUTE_FBR = "EXECUTE_FBR", _("FBR execute")
    GET_BOOKINGS = "GET_BOOKINGS", _("Get bookings")
    GET_FBR_DETAILS = "GET_FBR_DETAILS", _("Get FBR details")
    REFRESH_STATUS = "REFRESH_STATUS", _("Refresh status")
    RECONCILE = "RECONCILE", _("Reconcile")


class JobState(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    CLAIMED = "CLAIMED", _("Claimed")
    RETRY_WAIT = "RETRY_WAIT", _("Waiting to retry")
    BLOCKED = "BLOCKED", _("Blocked")
    DONE = "DONE", _("Done")
    CANCELLED = "CANCELLED", _("Cancelled")


# Job states that still hold work: at most one such job per dedupe key.
LIVE_JOB_STATES = (JobState.PENDING, JobState.CLAIMED, JobState.RETRY_WAIT)


class AttemptOutcome(models.TextChoices):
    IN_FLIGHT = "IN_FLIGHT", _("In flight")
    SUCCESS = "SUCCESS", _("Success")
    DEFINITE_FAILURE = "DEFINITE_FAILURE", _("Definite failure")
    UNCERTAIN = "UNCERTAIN", _("Uncertain (may have been processed)")


class ErrorCategory(models.TextChoices):
    CONFIG = "CONFIG", _("Configuration")
    CONNECT_FAILED = "CONNECT_FAILED", _("Connection failed (not sent)")
    TLS = "TLS", _("TLS failure (not sent)")
    AUTH_UNAVAILABLE = "AUTH_UNAVAILABLE", _("Authentication unavailable (not sent)")
    READ_TIMEOUT = "READ_TIMEOUT", _("Read timeout")
    CONNECTION_LOST = "CONNECTION_LOST", _("Connection lost after sending")
    AUTH = "AUTH", _("Authentication rejected")
    FORBIDDEN = "FORBIDDEN", _("Forbidden")
    VALIDATION = "VALIDATION", _("Validation error")
    NOT_FOUND = "NOT_FOUND", _("Not found")
    CONFLICT = "CONFLICT", _("Conflict / duplicate")
    RATE_LIMITED = "RATE_LIMITED", _("Rate limited")
    SERVER_ERROR = "SERVER_ERROR", _("Server error")
    INVALID_RESPONSE = "INVALID_RESPONSE", _("Invalid response")
    BUSINESS_ERROR = "BUSINESS_ERROR", _("Business error")
    WORKER_LOST = "WORKER_LOST", _("Worker lost mid-request")


class EvidenceType(models.TextChoices):
    RECONCILIATION = "RECONCILIATION", _("Reconciliation result")
    ISTEM_PORTAL = "ISTEM_PORTAL", _("Checked on the I-STEM portal")
    ISTEM_TEAM = "ISTEM_TEAM", _("Written confirmation from the I-STEM team")
    OTHER = "OTHER", _("Other")
