"""Remote Analysis constants and status enums."""

from __future__ import annotations

from django.db import models
from django.utils.translation import gettext_lazy as _


SCHEMA_VERSION = 1
DEFAULT_TOKEN_LIFETIME_DAYS = 90
HEARTBEAT_OFFLINE_SECONDS = 300
HEARTBEAT_STALE_SECONDS = 120
HIGH_CPU_THRESHOLD = 90.0
LOW_MEMORY_THRESHOLD = 90.0  # percent used
DISK_FULL_THRESHOLD = 95.0


class WorkstationStatus(models.TextChoices):
    REGISTERING = "REGISTERING", _("Registering")
    ONLINE = "ONLINE", _("Online")
    AVAILABLE = "AVAILABLE", _("Available")
    PREPARING = "PREPARING", _("Preparing")
    BUSY = "BUSY", _("Busy")
    RESERVED = "RESERVED", _("Reserved")
    CLEANING = "CLEANING", _("Cleaning")
    OFFLINE = "OFFLINE", _("Offline")
    MAINTENANCE = "MAINTENANCE", _("Maintenance")
    CALIBRATION = "CALIBRATION", _("Calibration")
    SOFTWARE_UPDATE = "SOFTWARE_UPDATE", _("Software update")
    HARDWARE_FAULT = "HARDWARE_FAULT", _("Hardware fault")
    DISABLED = "DISABLED", _("Disabled")
    ERROR = "ERROR", _("Error")
    UNKNOWN = "UNKNOWN", _("Unknown")


class MaintenanceKind(models.TextChoices):
    """Administrator-facing maintenance / outage classification."""

    MAINTENANCE = "MAINTENANCE", _("Scheduled maintenance")
    CALIBRATION = "CALIBRATION", _("Calibration")
    SOFTWARE_UPDATE = "SOFTWARE_UPDATE", _("Software update")
    HARDWARE_FAULT = "HARDWARE_FAULT", _("Hardware fault")
    CLEANING = "CLEANING", _("Cleaning")
    OFFLINE = "OFFLINE", _("Offline")
    DISABLED = "DISABLED", _("Disabled")


# Statuses that must never receive new analysis allocations.
NON_OPERATIONAL_STATUSES = frozenset(
    {
        WorkstationStatus.REGISTERING,
        WorkstationStatus.OFFLINE,
        WorkstationStatus.MAINTENANCE,
        WorkstationStatus.CALIBRATION,
        WorkstationStatus.SOFTWARE_UPDATE,
        WorkstationStatus.HARDWARE_FAULT,
        WorkstationStatus.CLEANING,
        WorkstationStatus.DISABLED,
        WorkstationStatus.ERROR,
        WorkstationStatus.UNKNOWN,
    }
)

# Heartbeat must not overwrite these admin / lifecycle states.
HEARTBEAT_PROTECTED_STATUSES = frozenset(
    {
        WorkstationStatus.MAINTENANCE,
        WorkstationStatus.CALIBRATION,
        WorkstationStatus.SOFTWARE_UPDATE,
        WorkstationStatus.HARDWARE_FAULT,
        WorkstationStatus.DISABLED,
        WorkstationStatus.PREPARING,
        WorkstationStatus.BUSY,
        WorkstationStatus.RESERVED,
        WorkstationStatus.CLEANING,
    }
)


class CommandType(models.TextChoices):
    PING = "PING", _("Ping")
    REFRESH = "REFRESH", _("Refresh")
    REFRESH_SOFTWARE = "REFRESH_SOFTWARE", _("Refresh software")
    COLLECT_LOGS = "COLLECT_LOGS", _("Collect logs")
    RESTART_AGENT = "RESTART_AGENT", _("Restart agent")
    PREPARE_WORKSTATION = "PREPARE_WORKSTATION", _("Prepare workstation")
    CLEAN_WORKSTATION = "CLEAN_WORKSTATION", _("Clean workstation")
    SYNC_WORKSPACE = "SYNC_WORKSPACE", _("Synchronize analysis workspace")
    COLLECT_WORKSPACE = "COLLECT_WORKSPACE", _("Collect workspace outputs")
    JOIN_TUNNEL = "JOIN_TUNNEL", _("Join reverse tunnel")
    CLOSE_TUNNEL = "CLOSE_TUNNEL", _("Close reverse tunnel")


class TransportMode(models.TextChoices):
    """How guacd reaches the Analysis PC RDP endpoint."""

    DIRECT_RDP = "direct_rdp", _("Direct RDP")
    REVERSE_TUNNEL = "reverse_tunnel", _("Reverse tunnel")


class TunnelSessionStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    WAITING_AGENT = "WAITING_AGENT", _("Waiting for agent")
    ACTIVE = "ACTIVE", _("Active")
    RECONNECTING = "RECONNECTING", _("Reconnecting")
    CLOSED = "CLOSED", _("Closed")
    FAILED = "FAILED", _("Failed")
    EXPIRED = "EXPIRED", _("Expired")


class CommandStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    DELIVERED = "DELIVERED", _("Delivered")
    RUNNING = "RUNNING", _("Running")
    COMPLETED = "COMPLETED", _("Completed")
    FAILED = "FAILED", _("Failed")
    EXPIRED = "EXPIRED", _("Expired")


class AuditCategory(models.TextChoices):
    REGISTRATION = "REGISTRATION", _("Registration")
    HEARTBEAT = "HEARTBEAT", _("Heartbeat")
    INVENTORY = "INVENTORY", _("Inventory")
    COMMANDS = "COMMANDS", _("Commands")
    AUTHENTICATION = "AUTHENTICATION", _("Authentication")
    STATUS = "STATUS", _("Status")
    MAINTENANCE = "MAINTENANCE", _("Maintenance")
    CONFIGURATION = "CONFIGURATION", _("Configuration")
    FAILURE = "FAILURE", _("Failure")
    RESERVATION = "RESERVATION", _("Reservation")
    SCHEDULER = "SCHEDULER", _("Scheduler")
    CONFLICT = "CONFLICT", _("Conflict")
    QUEUE = "QUEUE", _("Queue")
    SESSION = "SESSION", _("Session")
    GUACAMOLE = "GUACAMOLE", _("Guacamole")
    WORKSPACE = "WORKSPACE", _("Workspace")
    OPERATIONS = "OPERATIONS", _("Operations")
    REPORTING = "REPORTING", _("Reporting")
    ALERTS = "ALERTS", _("Alerts")
    COLLABORATION = "COLLABORATION", _("Collaboration")
    NOTIFICATIONS = "NOTIFICATIONS", _("Notifications")
    ASSISTANCE = "ASSISTANCE", _("Assistance")


class InventoryChangeType(models.TextChoices):
    ADDED = "ADDED", _("Added")
    REMOVED = "REMOVED", _("Removed")
    VERSION_CHANGED = "VERSION_CHANGED", _("Version changed")
    UPDATED = "UPDATED", _("Updated")


PERMISSION_REMOTE_ANALYSIS_MANAGE = "remote_analysis.manage"
PERMISSION_REMOTE_ANALYSIS_VIEW = "remote_analysis.view"

MANAGE_USER_TYPES = ("admin", "dept_admin", "manager")

# --- Milestone 3: Scheduler ---

MIN_HEALTH_SCORE_FOR_ALLOCATION = 50
DEFAULT_RESERVATION_GRACE_MINUTES = 15
DEFAULT_UNUSED_RESERVATION_MINUTES = 30
HEARTBEAT_TIMEOUT_FOR_RESERVATION_SECONDS = 120


class ReservationStatus(models.TextChoices):
    REQUESTED = "REQUESTED", _("Requested")
    VALIDATING = "VALIDATING", _("Validating")
    QUEUED = "QUEUED", _("Queued")
    RESERVED = "RESERVED", _("Reserved")
    AWAITING_CHECKIN = "AWAITING_CHECKIN", _("Awaiting user check-in")
    PREPARING = "PREPARING", _("Preparing")
    READY = "READY", _("Ready")
    ACTIVE = "ACTIVE", _("Active")
    COMPLETED = "COMPLETED", _("Completed")
    EXPIRED = "EXPIRED", _("Expired")
    CANCELLED = "CANCELLED", _("Cancelled")
    FAILED = "FAILED", _("Failed")


class MissedCheckinPolicy(models.TextChoices):
    END_OF_QUEUE = "END_OF_QUEUE", _("Move to end of queue")
    RETRY_LATER = "RETRY_LATER", _("Retry allocation later")
    CANCEL_AFTER_N = "CANCEL_AFTER_N", _("Cancel after N missed check-ins")


class QueueEntryStatus(models.TextChoices):
    WAITING = "WAITING", _("Waiting")
    ALLOCATING = "ALLOCATING", _("Allocating")
    RESERVED = "RESERVED", _("Reserved")
    EXPIRED = "EXPIRED", _("Expired")
    CANCELLED = "CANCELLED", _("Cancelled")


class ConflictType(models.TextChoices):
    DOUBLE_BOOKING = "DOUBLE_BOOKING", _("Double booking")
    MAINTENANCE_OVERLAP = "MAINTENANCE_OVERLAP", _("Maintenance overlap")
    WORKSTATION_OFFLINE = "WORKSTATION_OFFLINE", _("Workstation offline")
    EXTENSION_CONFLICT = "EXTENSION_CONFLICT", _("Reservation extension")
    MANUAL_OVERRIDE = "MANUAL_OVERRIDE", _("Manual override")
    PRIORITY_OVERRIDE = "PRIORITY_OVERRIDE", _("Priority override")


class AllocationRuleType(models.TextChoices):
    DEPARTMENT_PRIORITY = "DEPARTMENT_PRIORITY", _("Department priority")
    EQUIPMENT_PRIORITY = "EQUIPMENT_PRIORITY", _("Equipment priority")
    USER_PRIORITY = "USER_PRIORITY", _("User priority")
    FACULTY_OVERRIDE = "FACULTY_OVERRIDE", _("Faculty override")
    RESEARCH_PROJECT = "RESEARCH_PROJECT", _("Research project")
    ADMINISTRATIVE = "ADMINISTRATIVE", _("Administrative reservation")
    VIP = "VIP", _("VIP (future)")


DEFAULT_SCORING_WEIGHTS = {
    "health_score": 20.0,
    "cpu_load": 8.0,
    "memory_usage": 8.0,
    "recent_usage": 5.0,
    "software_match": 18.0,
    "capability_match": 12.0,
    "department_affinity": 8.0,
    "idle_time": 4.0,
    "gpu_score": 7.0,
    "historical_performance": 5.0,
    "multi_software_coverage": 5.0,
}
# --- Milestone 4: Browser remote desktop / Guacamole ---

DEFAULT_SESSION_TIMEOUT_MINUTES = 120
DEFAULT_IDLE_TIMEOUT_MINUTES = 15
DEFAULT_PREPARE_TIMEOUT_SECONDS = 120
DEFAULT_LAUNCH_TOKEN_LIFETIME_SECONDS = 90
SESSION_TOKEN_BYTES = 32


class SessionStatus(models.TextChoices):
    CREATED = "CREATED", _("Created")
    PREPARING = "PREPARING", _("Preparing")
    READY = "READY", _("Ready")
    TOKEN_GENERATED = "TOKEN_GENERATED", _("Token generated")
    LAUNCHED = "LAUNCHED", _("Launched")
    CONNECTING = "CONNECTING", _("Connecting")
    CONNECTED = "CONNECTED", _("Connected")
    ACTIVE = "ACTIVE", _("Active")
    IDLE = "IDLE", _("Idle")
    DISCONNECTING = "DISCONNECTING", _("Disconnecting")
    COMPLETED = "COMPLETED", _("Completed")
    EXPIRED = "EXPIRED", _("Expired")
    FAILED = "FAILED", _("Failed")
    TERMINATED = "TERMINATED", _("Terminated")


class FileTransferPolicy(models.TextChoices):
    DISABLED = "DISABLED", _("Disabled")
    UPLOAD_ONLY = "UPLOAD_ONLY", _("Upload only")
    DOWNLOAD_ONLY = "DOWNLOAD_ONLY", _("Download only")
    BIDIRECTIONAL = "BIDIRECTIONAL", _("Bidirectional")


class ClipboardPolicy(models.TextChoices):
    DISABLED = "DISABLED", _("Disabled")
    TEXT_ONLY = "TEXT_ONLY", _("Text only")
    FULL = "FULL", _("Full clipboard")


# --- Milestone 5: Analysis workspace / secure file exchange ---

DEFAULT_WORKSPACE_FOLDERS = (
    "RawData",
    "Processed",
    "FinalOutput",
    "Scratch",
    "Reports",
    "Exports",
    "Temp",
    "Logs",
    "Metadata",
)

# Additive workflow step folders are created dynamically as Step01, Step02, …


class WorkflowJobStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    PREPARING = "PREPARING", _("Preparing Analysis Workspace")
    ACTIVE = "ACTIVE", _("Analysis Session Active")
    PAUSED = "PAUSED", _("Paused")
    NEEDS_REVIEW = "NEEDS_REVIEW", _("Needs Operator Review")
    COMPLETED = "COMPLETED", _("Analysis Completed")
    FAILED = "FAILED", _("Failed")
    CANCELLED = "CANCELLED", _("Cancelled")


class WorkflowJobStepStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    READY = "READY", _("Ready")
    ACTIVE = "ACTIVE", _("Active")
    COMPLETED = "COMPLETED", _("Completed")
    SKIPPED = "SKIPPED", _("Skipped")
    FAILED = "FAILED", _("Failed")
    NEEDS_REVIEW = "NEEDS_REVIEW", _("Needs Operator Review")


class AnalysisJobCollaboratorRole(models.TextChoices):
    """Reserved for v2 multi-user collaboration — do not expose in Portal UX yet."""

    OWNER = "OWNER", _("Owner")
    COLLABORATOR = "COLLABORATOR", _("Collaborator")
    VIEWER = "VIEWER", _("Viewer")
    OBSERVER = "OBSERVER", _("Observer")


UX_STATUS_LABELS = {
    WorkflowJobStatus.PENDING: "Preparing Analysis Workspace",
    WorkflowJobStatus.PREPARING: "Preparing Analysis Workspace",
    WorkflowJobStatus.ACTIVE: "Analysis Session Active",
    WorkflowJobStatus.PAUSED: "Analysis Session Paused",
    WorkflowJobStatus.NEEDS_REVIEW: "Needs Operator Review",
    WorkflowJobStatus.COMPLETED: "Analysis Completed",
    WorkflowJobStatus.FAILED: "Analysis Failed",
    WorkflowJobStatus.CANCELLED: "Analysis Cancelled",
}
DEFAULT_WORKSPACE_QUOTA_GB = 50.0
DEFAULT_WORKSPACE_RETENTION_DAYS = 90
DEFAULT_CHUNK_SIZE_BYTES = 5 * 1024 * 1024
DEFAULT_MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
DEFAULT_MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024 * 1024
DEFAULT_VERSION_HISTORY = 20


class WorkspaceStatus(models.TextChoices):
    CREATING = "CREATING", _("Creating")
    READY = "READY", _("Ready")
    SYNCING = "SYNCING", _("Syncing")
    ACTIVE = "ACTIVE", _("Active")
    COLLECTING = "COLLECTING", _("Collecting outputs")
    ARCHIVING = "ARCHIVING", _("Archiving")
    ARCHIVED = "ARCHIVED", _("Archived")
    RESTORING = "RESTORING", _("Restoring")
    DELETED = "DELETED", _("Deleted")
    FAILED = "FAILED", _("Failed")


class ArchiveStatus(models.TextChoices):
    NONE = "NONE", _("Not archived")
    PENDING = "PENDING", _("Pending")
    ARCHIVED = "ARCHIVED", _("Archived")
    RESTORED = "RESTORED", _("Restored")
    FAILED = "FAILED", _("Failed")


class FileCategory(models.TextChoices):
    RAW = "RAW", _("Raw data")
    PROCESSED = "PROCESSED", _("Processed")
    REPORT = "REPORT", _("Report")
    EXPORT = "EXPORT", _("Export")
    TEMP = "TEMP", _("Temporary")
    LOG = "LOG", _("Log")
    METADATA = "METADATA", _("Metadata")
    OTHER = "OTHER", _("Other")


class VirusStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    CLEAN = "CLEAN", _("Clean")
    INFECTED = "INFECTED", _("Infected")
    ERROR = "ERROR", _("Scan error")
    SKIPPED = "SKIPPED", _("Skipped")


class TransferDirection(models.TextChoices):
    PORTAL_TO_WORKSPACE = "PORTAL_TO_WORKSPACE", _("Portal to workspace")
    WORKSPACE_TO_PORTAL = "WORKSPACE_TO_PORTAL", _("Workspace to portal")
    AGENT_PULL = "AGENT_PULL", _("Agent pull")
    AGENT_PUSH = "AGENT_PUSH", _("Agent push")


class TransferStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    IN_PROGRESS = "IN_PROGRESS", _("In progress")
    RETRYING = "RETRYING", _("Retrying")
    COMPLETED = "COMPLETED", _("Completed")
    FAILED = "FAILED", _("Failed")
    CANCELLED = "CANCELLED", _("Cancelled")


class WorkspaceSyncPhase(models.TextChoices):
    """Explicit workspace lifecycle for automatic data synchronization.

    Success path:
      Preparing → DownloadingInput → VerifyingInput → InputReady →
      SessionStarting → SessionActive → CollectingOutput → UploadingOutput →
      UploadVerified → Cleanup → Completed

    Failure / control:
      PreparationFailed | UploadFailed | RetryPending | CleanupFailed | Cancelled
    """

    PREPARING = "Preparing", _("Preparing Workspace")
    DOWNLOADING_INPUT = "DownloadingInput", _("Downloading Input")
    VERIFYING_INPUT = "VerifyingInput", _("Verifying Input")
    INPUT_READY = "InputReady", _("Input Ready")
    SESSION_STARTING = "SessionStarting", _("Session Starting")
    SESSION_ACTIVE = "SessionActive", _("Session Active")
    COLLECTING_OUTPUT = "CollectingOutput", _("Collecting Output")
    UPLOADING_OUTPUT = "UploadingOutput", _("Uploading Output")
    UPLOAD_VERIFIED = "UploadVerified", _("Upload Verified")
    CLEANUP = "Cleanup", _("Cleanup")
    COMPLETED = "Completed", _("Completed")
    PREPARATION_FAILED = "PreparationFailed", _("Preparation Failed")
    UPLOAD_FAILED = "UploadFailed", _("Upload Failed")
    RETRY_PENDING = "RetryPending", _("Retry Pending")
    CLEANUP_FAILED = "CleanupFailed", _("Cleanup Failed")
    CANCELLED = "Cancelled", _("Cancelled")


# Map legacy sync_phase DB values → canonical lifecycle (migration + normalize)
LEGACY_SYNC_PHASE_MAP = {
    "QUEUED": "Preparing",
    "PREPARING": "Preparing",
    "DOWNLOADING": "DownloadingInput",
    "READY": "InputReady",
    "UPLOADING": "UploadingOutput",
    "RETRYING": "RetryPending",
    "COMPLETED": "Completed",
    "FAILED": "PreparationFailed",
    "CANCELLED": "Cancelled",
}


def normalize_sync_phase(value: str | None) -> str:
    if not value:
        return WorkspaceSyncPhase.PREPARING
    if value in WorkspaceSyncPhase.values:
        return value
    return LEGACY_SYNC_PHASE_MAP.get(value, value)


# Phases that allow Guacamole / RDP launch
WORKSPACE_INPUT_READY_PHASES = frozenset(
    {
        WorkspaceSyncPhase.INPUT_READY,
        WorkspaceSyncPhase.SESSION_STARTING,
        WorkspaceSyncPhase.SESSION_ACTIVE,
        WorkspaceSyncPhase.COLLECTING_OUTPUT,
        WorkspaceSyncPhase.UPLOADING_OUTPUT,
        WorkspaceSyncPhase.UPLOAD_VERIFIED,
        WorkspaceSyncPhase.CLEANUP,
        WorkspaceSyncPhase.COMPLETED,
    }
)

# Output may be deleted on the agent only after these phases
WORKSPACE_UPLOAD_VERIFIED_PHASES = frozenset(
    {
        WorkspaceSyncPhase.UPLOAD_VERIFIED,
        WorkspaceSyncPhase.CLEANUP,
        WorkspaceSyncPhase.COMPLETED,
    }
)


# Agent PC layout ↔ portal folder mapping (keep portal RawData/Processed for compat)
AGENT_LAYOUT_FOLDERS = ("Input", "Working", "Output", "Logs", "Temp")
AGENT_INPUT_PORTAL_FOLDERS = ("RawData", "Metadata")
AGENT_OUTPUT_PORTAL_FOLDERS = ("Processed", "Reports", "Exports", "Logs")
AGENT_TO_PORTAL_FOLDER = {
    "Input": "RawData",
    "Working": "Temp",
    "Output": "Processed",
    "Logs": "Logs",
    "Temp": "Temp",
}
PORTAL_TO_AGENT_FOLDER = {
    "RawData": "Input",
    "Metadata": "Input",
    "Processed": "Output",
    "Reports": "Output",
    "Exports": "Output",
    "Logs": "Logs",
    "Temp": "Temp",
}


class WorkspaceAuditAction(models.TextChoices):
    CREATE = "CREATE", _("Create")
    UPLOAD = "UPLOAD", _("Upload")
    DOWNLOAD = "DOWNLOAD", _("Download")
    DELETE = "DELETE", _("Delete")
    VERSION = "VERSION", _("Version")
    ARCHIVE = "ARCHIVE", _("Archive")
    RESTORE = "RESTORE", _("Restore")
    SYNC = "SYNC", _("Sync")
    QUOTA = "QUOTA", _("Quota")
    SCAN = "SCAN", _("Virus scan")
    SHARE = "SHARE", _("Share")
    INTEGRITY = "INTEGRITY", _("Integrity")

# --- Milestone 6: Operations Center ---

class AggregationPeriod(models.TextChoices):
    HOURLY = "HOURLY", _("Hourly")
    DAILY = "DAILY", _("Daily")
    WEEKLY = "WEEKLY", _("Weekly")
    MONTHLY = "MONTHLY", _("Monthly")
    QUARTERLY = "QUARTERLY", _("Quarterly")
    YEARLY = "YEARLY", _("Yearly")


class AlertSeverity(models.TextChoices):
    INFO = "INFO", _("Info")
    WARNING = "WARNING", _("Warning")
    CRITICAL = "CRITICAL", _("Critical")
    EMERGENCY = "EMERGENCY", _("Emergency")


class AlertCategory(models.TextChoices):
    AGENT = "AGENT", _("Agent")
    HEARTBEAT = "HEARTBEAT", _("Heartbeat")
    PERFORMANCE = "PERFORMANCE", _("Performance")
    SESSION = "SESSION", _("Session")
    SYNC = "SYNC", _("Synchronization")
    RESERVATION = "RESERVATION", _("Reservation")
    WORKSPACE = "WORKSPACE", _("Workspace")
    CAPACITY = "CAPACITY", _("Capacity")
    AVAILABILITY = "AVAILABILITY", _("Availability")


class AlertStatus(models.TextChoices):
    OPEN = "OPEN", _("Open")
    ACKNOWLEDGED = "ACKNOWLEDGED", _("Acknowledged")
    RESOLVED = "RESOLVED", _("Resolved")
    SUPPRESSED = "SUPPRESSED", _("Suppressed")


class ReportType(models.TextChoices):
    DAILY_OPERATIONS = "DAILY_OPERATIONS", _("Daily operations")
    WEEKLY_UTILIZATION = "WEEKLY_UTILIZATION", _("Weekly utilization")
    MONTHLY_UTILIZATION = "MONTHLY_UTILIZATION", _("Monthly utilization")
    DEPARTMENT_SUMMARY = "DEPARTMENT_SUMMARY", _("Department summary")
    WORKSTATION_SUMMARY = "WORKSTATION_SUMMARY", _("Workstation summary")
    SESSION_SUMMARY = "SESSION_SUMMARY", _("Session summary")
    RESERVATION_REPORT = "RESERVATION_REPORT", _("Reservation report")
    CAPACITY_REPORT = "CAPACITY_REPORT", _("Capacity report")
    FAILURE_REPORT = "FAILURE_REPORT", _("Failure report")
    ALERT_REPORT = "ALERT_REPORT", _("Alert report")
    WORKSPACE_USAGE = "WORKSPACE_USAGE", _("Workspace usage")


class ReportFormat(models.TextChoices):
    JSON = "JSON", _("JSON")
    CSV = "CSV", _("CSV")
    EXCEL = "EXCEL", _("Excel")
    PDF = "PDF", _("PDF")


class ReportStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    GENERATING = "GENERATING", _("Generating")
    READY = "READY", _("Ready")
    FAILED = "FAILED", _("Failed")


# --- Milestone 7: Collaboration / Notifications / Assistance ---

class NotificationChannel(models.TextChoices):
    PORTAL = "PORTAL", _("Portal")
    EMAIL = "EMAIL", _("Email")
    SMS = "SMS", _("SMS (future)")
    WHATSAPP = "WHATSAPP", _("WhatsApp (future)")
    PUSH = "PUSH", _("Push (future)")


class NotificationType(models.TextChoices):
    RESERVATION_CONFIRMED = "RESERVATION_CONFIRMED", _("Reservation confirmed")
    RESERVATION_REMINDER = "RESERVATION_REMINDER", _("Reservation reminder")
    SESSION_STARTING = "SESSION_STARTING", _("Session starting")
    SESSION_ENDING = "SESSION_ENDING", _("Session ending")
    WORKSPACE_SYNCED = "WORKSPACE_SYNCED", _("Workspace synchronized")
    WORKSPACE_READY = "WORKSPACE_READY", _("Workspace ready")
    WORKSPACE_SYNC_STARTED = "WORKSPACE_SYNC_STARTED", _("Synchronization started")
    WORKSPACE_SYNC_FAILED = "WORKSPACE_SYNC_FAILED", _("Synchronization failed")
    FILES_AVAILABLE = "FILES_AVAILABLE", _("Files available")
    UPLOAD_COMPLETE = "UPLOAD_COMPLETE", _("Upload complete")
    DOWNLOAD_COMPLETE = "DOWNLOAD_COMPLETE", _("Download complete")
    AGENT_OFFLINE = "AGENT_OFFLINE", _("Agent offline")
    MAINTENANCE_SCHEDULED = "MAINTENANCE_SCHEDULED", _("Maintenance scheduled")
    SESSION_TERMINATED = "SESSION_TERMINATED", _("Session terminated")
    ALERT = "ALERT", _("Alert")
    INVITATION = "INVITATION", _("Invitation")
    ASSISTANCE = "ASSISTANCE", _("Assistance")
    COMMENT = "COMMENT", _("Comment")
    SHARE = "SHARE", _("Share")
    ANNOUNCEMENT = "ANNOUNCEMENT", _("Announcement")


class NotificationStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    DELIVERED = "DELIVERED", _("Delivered")
    READ = "READ", _("Read")
    FAILED = "FAILED", _("Failed")


class SharePermissionLevel(models.TextChoices):
    READ = "READ", _("Read")
    WRITE = "WRITE", _("Write")
    DOWNLOAD = "DOWNLOAD", _("Download")
    COMMENT = "COMMENT", _("Comment")


class InvitationStatus(models.TextChoices):
    PENDING = "PENDING", _("Pending")
    ACCEPTED = "ACCEPTED", _("Accepted")
    DECLINED = "DECLINED", _("Declined")
    EXPIRED = "EXPIRED", _("Expired")
    REVOKED = "REVOKED", _("Revoked")


class InvitationKind(models.TextChoices):
    COLLABORATOR = "COLLABORATOR", _("Invite collaborator")
    FACULTY_SUPERVISION = "FACULTY_SUPERVISION", _("Faculty supervision")
    RESEARCH_GROUP = "RESEARCH_GROUP", _("Research group access")


class AssistanceStatus(models.TextChoices):
    REQUESTED = "REQUESTED", _("Requested")
    ASSIGNED = "ASSIGNED", _("Assigned")
    ACCEPTED = "ACCEPTED", _("Accepted")
    RESOLVED = "RESOLVED", _("Resolved")
    CLOSED = "CLOSED", _("Closed")
    CANCELLED = "CANCELLED", _("Cancelled")


class AssistancePriority(models.TextChoices):
    LOW = "LOW", _("Low")
    NORMAL = "NORMAL", _("Normal")
    HIGH = "HIGH", _("High")
    URGENT = "URGENT", _("Urgent")


class NoteVisibility(models.TextChoices):
    PUBLIC = "PUBLIC", _("Public")
    PRIVATE = "PRIVATE", _("Private")
    ADMIN = "ADMIN", _("Administrator notes")


class ActivityVerb(models.TextChoices):
    RESERVATION = "RESERVATION", _("Reservation")
    WORKSPACE = "WORKSPACE", _("Workspace")
    SESSION = "SESSION", _("Session")
    UPLOAD = "UPLOAD", _("Upload")
    DOWNLOAD = "DOWNLOAD", _("Download")
    COMMENT = "COMMENT", _("Comment")
    NOTE = "NOTE", _("Note")
    INVITATION = "INVITATION", _("Invitation")
    ALERT = "ALERT", _("Alert")
    SESSION_START = "SESSION_START", _("Session start")
    SESSION_END = "SESSION_END", _("Session end")
    SYNC = "SYNC", _("Synchronization")
    SHARE = "SHARE", _("Share")
    ASSISTANCE = "ASSISTANCE", _("Assistance")
    ANNOUNCEMENT = "ANNOUNCEMENT", _("Announcement")
