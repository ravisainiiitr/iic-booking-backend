"""Remote Analysis Portal domain models — enterprise workstation registry."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from iic_booking.remote_analysis.constants import (
    SCHEMA_VERSION,
    CommandStatus,
    CommandType,
    InventoryChangeType,
    WorkstationStatus,
)


class AnalysisWorkstation(models.Model):
    """Portal source of truth for a Remote Analysis workstation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    agent_id = models.CharField(max_length=64, unique=True, db_index=True)
    machine_guid = models.CharField(max_length=64, blank=True, default="", db_index=True)
    bios_uuid = models.CharField(max_length=64, blank=True, default="")
    machine_fingerprint = models.CharField(max_length=256, blank=True, default="", db_index=True)
    hostname = models.CharField(max_length=255, blank=True, default="")
    display_name = models.CharField(max_length=255, blank=True, default="")
    department = models.ForeignKey(
        "users.Department",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="analysis_workstations",
    )
    department_name = models.CharField(max_length=255, blank=True, default="")
    building = models.CharField(max_length=255, blank=True, default="")
    room = models.CharField(max_length=128, blank=True, default="")
    description = models.TextField(blank=True, default="")
    operating_system = models.CharField(max_length=255, blank=True, default="")
    windows_version = models.CharField(max_length=255, blank=True, default="")
    cpu = models.CharField(max_length=255, blank=True, default="")
    cpu_cores = models.PositiveIntegerField(default=0)
    memory_gb = models.FloatField(default=0)
    storage_gb = models.FloatField(default=0)
    gpu = models.CharField(max_length=255, blank=True, default="")
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    mac_address = models.CharField(max_length=64, blank=True, default="")
    agent_version = models.CharField(max_length=64, blank=True, default="")
    schema_version = models.PositiveIntegerField(default=SCHEMA_VERSION)
    registration_date = models.DateTimeField(null=True, blank=True)
    last_heartbeat = models.DateTimeField(null=True, blank=True)
    status = models.CharField(
        max_length=32,
        choices=WorkstationStatus.choices,
        default=WorkstationStatus.REGISTERING,
        db_index=True,
    )
    enabled = models.BooleanField(default=True)
    supports_rdp = models.BooleanField(default=True)
    supports_clipboard = models.BooleanField(default=True)
    supports_file_transfer = models.BooleanField(default=True)
    supports_audio = models.BooleanField(default=True)
    supports_multi_monitor = models.BooleanField(default=True)
    current_command = models.CharField(max_length=64, blank=True, default="")
    health_score = models.PositiveSmallIntegerField(default=100)
    last_inventory_update = models.DateTimeField(null=True, blank=True)
    # R9 — safe local data-workspace metadata from agent heartbeat (no secrets)
    data_root = models.CharField(max_length=1024, blank=True, default="")
    input_path = models.CharField(max_length=1024, blank=True, default="")
    output_path = models.CharField(max_length=1024, blank=True, default="")
    workspace_disk_free_bytes = models.BigIntegerField(null=True, blank=True)
    input_bytes = models.BigIntegerField(null=True, blank=True)
    output_bytes = models.BigIntegerField(null=True, blank=True)
    cleanup_status = models.CharField(max_length=32, blank=True, default="idle")
    last_sync_at = models.DateTimeField(null=True, blank=True)
    disk_low = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["hostname", "agent_id"]
        verbose_name = _("Analysis workstation")
        verbose_name_plural = _("Analysis workstations")
        indexes = [
            models.Index(fields=["status", "last_heartbeat"], name="ra_ws_status_hb_idx"),
        ]

    def __str__(self) -> str:
        return self.display_name or self.hostname or self.agent_id


class WorkstationCapability(models.Model):
    workstation = models.OneToOneField(
        AnalysisWorkstation,
        on_delete=models.CASCADE,
        related_name="capabilities",
    )
    supports_rdp = models.BooleanField(default=True)
    supports_clipboard = models.BooleanField(default=True)
    supports_file_transfer = models.BooleanField(default=True)
    supports_audio = models.BooleanField(default=True)
    supports_multi_monitor = models.BooleanField(default=True)
    maximum_resolution = models.CharField(max_length=64, blank=True, default="")
    gpu_available = models.BooleanField(default=False)
    ram_gb = models.FloatField(default=0)
    cpu_cores = models.PositiveIntegerField(default=0)
    disk_space_gb = models.FloatField(default=0)
    network_speed_mbps = models.FloatField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Workstation capability")
        verbose_name_plural = _("Workstation capabilities")

    def __str__(self) -> str:
        return f"Capabilities:{self.workstation_id}"


class WorkstationStateHistory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workstation = models.ForeignKey(
        AnalysisWorkstation,
        on_delete=models.CASCADE,
        related_name="state_history",
    )
    from_status = models.CharField(max_length=32, choices=WorkstationStatus.choices, blank=True, default="")
    to_status = models.CharField(max_length=32, choices=WorkstationStatus.choices)
    reason = models.CharField(max_length=512, blank=True, default="")
    changed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ra_state_changes",
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Workstation state history")
        verbose_name_plural = _("Workstation state histories")


class AgentToken(models.Model):
    """Hashed agent authentication token with expiry, rotation, and revocation."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workstation = models.ForeignKey(
        AnalysisWorkstation,
        on_delete=models.CASCADE,
        related_name="tokens",
    )
    token_hash = models.CharField(max_length=128)
    token_prefix = models.CharField(max_length=12, blank=True, default="")
    issued_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    rotation_of = models.ForeignKey(
        "self",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="rotations",
    )

    class Meta:
        ordering = ["-issued_at"]
        verbose_name = _("Agent token")
        verbose_name_plural = _("Agent tokens")

    def __str__(self) -> str:
        return f"Token:{self.token_prefix}… ({self.workstation.agent_id})"


class WorkstationHeartbeat(models.Model):
    id = models.BigAutoField(primary_key=True)
    workstation = models.ForeignKey(
        AnalysisWorkstation,
        on_delete=models.CASCADE,
        related_name="heartbeats",
    )
    received_at = models.DateTimeField(auto_now_add=True, db_index=True)
    cpu = models.FloatField(default=0)
    memory = models.FloatField(default=0)
    disk = models.FloatField(default=0)
    gpu = models.FloatField(null=True, blank=True)
    windows_uptime_hours = models.FloatField(default=0)
    idle = models.BooleanField(default=False)
    idle_time_minutes = models.FloatField(default=0)
    logged_in_user = models.CharField(max_length=255, blank=True, default="")
    running_software = models.TextField(blank=True, default="")
    running_processes = models.PositiveIntegerField(default=0)
    software_count = models.PositiveIntegerField(default=0)
    portal_latency_ms = models.FloatField(null=True, blank=True)
    current_state = models.CharField(max_length=32, blank=True, default="")
    network = models.BooleanField(default=True)
    online = models.BooleanField(default=True)
    antivirus_status = models.CharField(max_length=128, blank=True, default="")
    windows_updates_pending = models.PositiveIntegerField(default=0)
    raw_payload = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-received_at"]
        verbose_name = _("Workstation heartbeat")
        verbose_name_plural = _("Workstation heartbeats")


class WorkstationInventory(models.Model):
    """Hardware inventory snapshot metadata for a workstation."""

    workstation = models.OneToOneField(
        AnalysisWorkstation,
        on_delete=models.CASCADE,
        related_name="inventory",
    )
    hardware_json = models.JSONField(default=dict, blank=True)
    software_count = models.PositiveIntegerField(default=0)
    license_count = models.PositiveIntegerField(default=0)
    content_hash = models.CharField(max_length=128, blank=True, default="")
    last_synced_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Workstation inventory")
        verbose_name_plural = _("Workstation inventories")


class InstalledSoftware(models.Model):
    id = models.BigAutoField(primary_key=True)
    workstation = models.ForeignKey(
        AnalysisWorkstation,
        on_delete=models.CASCADE,
        related_name="installed_software",
    )
    software_name = models.CharField(max_length=512)
    publisher = models.CharField(max_length=512, blank=True, default="")
    version = models.CharField(max_length=128, blank=True, default="")
    executable = models.CharField(max_length=1024, blank=True, default="")
    install_path = models.CharField(max_length=1024, blank=True, default="")
    install_date = models.DateTimeField(null=True, blank=True)
    licensed = models.BooleanField(default=False)
    license_type = models.CharField(max_length=128, blank=True, default="")
    category = models.CharField(max_length=128, blank=True, default="")
    content_hash = models.CharField(max_length=128, blank=True, default="", db_index=True)
    is_present = models.BooleanField(default=True)
    # R11: per-workstation allocation eligibility (disable without uninstalling).
    allocation_enabled = models.BooleanField(
        default=True,
        help_text=_("When False, this install is ignored by the Remote Analysis allocator."),
    )
    catalog = models.ForeignKey(
        "remote_analysis.AnalysisSoftwareCatalog",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="installed_on",
        help_text=_("Canonical catalog entry discovered/linked from this install."),
    )
    last_updated = models.DateTimeField(auto_now=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["software_name"]
        indexes = [
            models.Index(fields=["workstation", "software_name"]),
        ]
        verbose_name = _("Installed software")
        verbose_name_plural = _("Installed software")

    def __str__(self) -> str:
        return f"{self.software_name} {self.version}".strip()


class SoftwareInventoryHistory(models.Model):
    id = models.BigAutoField(primary_key=True)
    workstation = models.ForeignKey(
        AnalysisWorkstation,
        on_delete=models.CASCADE,
        related_name="software_history",
    )
    software_name = models.CharField(max_length=512)
    change_type = models.CharField(max_length=32, choices=InventoryChangeType.choices)
    old_version = models.CharField(max_length=128, blank=True, default="")
    new_version = models.CharField(max_length=128, blank=True, default="")
    details = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class SoftwareLicense(models.Model):
    id = models.BigAutoField(primary_key=True)
    workstation = models.ForeignKey(
        AnalysisWorkstation,
        on_delete=models.CASCADE,
        related_name="software_licenses",
    )
    software = models.CharField(max_length=512)
    expiry = models.DateTimeField(null=True, blank=True)
    seats = models.PositiveIntegerField(null=True, blank=True)
    license_server = models.CharField(max_length=255, blank=True, default="")
    license_key_hash = models.CharField(max_length=128, blank=True, default="")
    status = models.CharField(max_length=64, blank=True, default="Unknown")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["software"]
        verbose_name = _("Software license")
        verbose_name_plural = _("Software licenses")


class RemoteCommand(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workstation = models.ForeignKey(
        AnalysisWorkstation,
        on_delete=models.CASCADE,
        related_name="commands",
    )
    command_type = models.CharField(max_length=64, choices=CommandType.choices)
    status = models.CharField(
        max_length=32,
        choices=CommandStatus.choices,
        default=CommandStatus.PENDING,
        db_index=True,
    )
    payload = models.JSONField(default=dict, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ra_commands_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    result_message = models.TextField(blank=True, default="")
    error_message = models.TextField(blank=True, default="")

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Remote command")
        verbose_name_plural = _("Remote commands")

    def __str__(self) -> str:
        return f"{self.command_type} ({self.status})"


class CommandExecution(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    command = models.ForeignKey(
        RemoteCommand,
        on_delete=models.CASCADE,
        related_name="executions",
    )
    status = models.CharField(max_length=32, choices=CommandStatus.choices)
    message = models.TextField(blank=True, default="")
    duration_ms = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Command execution")
        verbose_name_plural = _("Command executions")


class WorkstationEvent(models.Model):
    """Audit / operational event log for Remote Analysis."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workstation = models.ForeignKey(
        AnalysisWorkstation,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="events",
    )
    category = models.CharField(max_length=64, db_index=True)
    action = models.CharField(max_length=128)
    details = models.TextField(blank=True, default="")
    success = models.BooleanField(default=True)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="ra_events",
    )
    correlation_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = _("Workstation event")
        verbose_name_plural = _("Workstation events")


class TelemetrySnapshot(models.Model):
    id = models.BigAutoField(primary_key=True)
    workstation = models.ForeignKey(
        AnalysisWorkstation,
        on_delete=models.CASCADE,
        related_name="telemetry",
    )
    metric_name = models.CharField(max_length=128, db_index=True)
    value = models.FloatField()
    unit = models.CharField(max_length=32, blank=True, default="")
    recorded_at = models.DateTimeField(auto_now_add=True, db_index=True)
    tags = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-recorded_at"]
        verbose_name = _("Telemetry snapshot")
        verbose_name_plural = _("Telemetry snapshots")
        indexes = [
            models.Index(fields=["workstation", "metric_name", "recorded_at"]),
        ]


# Milestone 3 scheduler models (imported for Django model discovery)
from iic_booking.remote_analysis.scheduler_models import (  # noqa: E402,F401
    AllocationRule,
    AnalysisReservation,
    MaintenanceWindow,
    ReservationAudit,
    ReservationConflict,
    ReservationEvent,
    ReservationHistory,
    ReservationPreference,
    ReservationQueue,
    SchedulerTelemetry,
    SoftwareRequirement,
)

from iic_booking.remote_analysis.catalog_models import (  # noqa: E402,F401
    AnalysisSoftwareCatalog,
    EquipmentAnalysisPool,
    EquipmentAnalysisSoftware,
)

from iic_booking.remote_analysis.workflow_models import (  # noqa: E402,F401
    AnalysisCapability,
    AnalysisJob,
    AnalysisJobCollaborator,
    AnalysisJobStep,
    AnalysisWorkflow,
    AnalysisWorkflowStep,
    AnalysisWorkflowVersion,
    EquipmentAnalysisWorkflow,
)

# Milestone 4 browser remote desktop / Guacamole models
from iic_booking.remote_analysis.session_models import (  # noqa: E402,F401
    ConnectionHistory,
    GuacamoleConnection,
    RemoteAnalysisSettings,
    RemoteDesktopSession,
    SessionAudit,
    SessionHealth,
    SessionLaunch,
    SessionRecording,
    SessionStateHistory,
    SessionStatistics,
    SessionTelemetry,
    SessionTermination,
    SessionToken,
    WorkstationRdpSecret,
)

# Reverse tunnel lifecycle metadata
from iic_booking.remote_analysis.tunnel_models import (  # noqa: E402,F401
    TunnelEvent,
    TunnelMetric,
    TunnelSession,
)

# Milestone 5 analysis workspace models
from iic_booking.remote_analysis.workspace_models import (  # noqa: E402,F401
    AnalysisWorkspace,
    TransferHistory,
    TransferPolicy,
    VirusScanResult,
    WorkspaceArchive,
    WorkspaceAudit,
    WorkspaceFile,
    WorkspaceFolder,
    WorkspaceQuota,
    WorkspaceShare,
    WorkspaceTelemetry,
    WorkspaceTransfer,
    WorkspaceVersion,
)

# Milestone 6 operations center models
from iic_booking.remote_analysis.operations_models import (  # noqa: E402,F401
    AlertEvent,
    AlertRule,
    AnalysisReport,
    CapacitySnapshot,
    CommissioningFailureSnapshot,
    CommissioningRun,
    CommissioningRunStep,
    DashboardSnapshot,
    OperationalKPI,
    PeakUsageWindow,
    PerformanceMetric,
    SessionAnalytics,
    UsageTrend,
    WorkstationAvailability,
    WorkstationUtilization,
)

# Milestone 7 collaboration center models
from iic_booking.remote_analysis.collaboration_models import (  # noqa: E402,F401
    ActivityEvent,
    ActivityFeed,
    Announcement,
    Bookmark,
    CollaborationTelemetry,
    FavoriteWorkstation,
    Notification,
    NotificationPreference,
    RecentWorkspace,
    SessionAssistanceEvent,
    SessionAssistanceRequest,
    SessionComment,
    SessionInvitation,
    SessionNote,
    SharedWorkspace,
    WorkspaceComment,
    WorkspaceSharePermission,
)

from iic_booking.remote_analysis.installer.models import AgentInstallerRelease  # noqa: E402,F401
