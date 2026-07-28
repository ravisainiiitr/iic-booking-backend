"""Django admin for Remote Analysis workstation registry."""

from __future__ import annotations

from django.contrib import admin, messages
from django.utils.html import format_html

from iic_booking.remote_analysis.constants import CommandType
from iic_booking.remote_analysis.models import (
    AgentToken,
    AnalysisWorkstation,
    CommandExecution,
    InstalledSoftware,
    RemoteCommand,
    SoftwareLicense,
    TelemetrySnapshot,
    WorkstationCapability,
    WorkstationEvent,
    WorkstationHeartbeat,
    WorkstationInventory,
    WorkstationStateHistory,
)
from iic_booking.remote_analysis.scheduler_models import (
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
from iic_booking.remote_analysis.services.commands import CommandService
from iic_booking.remote_analysis.services.workstation_admin import WorkstationAdminService


class CapabilityInline(admin.StackedInline):
    model = WorkstationCapability
    extra = 0


class InventoryInline(admin.StackedInline):
    model = WorkstationInventory
    extra = 0
    readonly_fields = ("software_count", "license_count", "content_hash", "last_synced_at", "updated_at")


@admin.register(AnalysisWorkstation)
class AnalysisWorkstationAdmin(admin.ModelAdmin):
    list_display = (
        "hostname",
        "agent_id",
        "status",
        "enabled",
        "health_score",
        "last_heartbeat",
        "department_name",
        "building",
        "room",
    )
    list_filter = ("status", "enabled", "building")
    search_fields = ("hostname", "agent_id", "display_name", "ip_address", "mac_address")
    readonly_fields = ("id", "registration_date", "created_at", "updated_at", "health_score")
    inlines = [CapabilityInline, InventoryInline]
    actions = ["action_enable", "action_disable", "action_maintenance", "action_ping"]

    @admin.action(description="Enable selected workstations")
    def action_enable(self, request, queryset):
        svc = WorkstationAdminService()
        for ws in queryset:
            svc.enable(ws, actor=request.user)
        self.message_user(request, f"Enabled {queryset.count()} workstation(s).", messages.SUCCESS)

    @admin.action(description="Disable selected workstations")
    def action_disable(self, request, queryset):
        svc = WorkstationAdminService()
        for ws in queryset:
            svc.disable(ws, actor=request.user)
        self.message_user(request, f"Disabled {queryset.count()} workstation(s).", messages.WARNING)

    @admin.action(description="Set maintenance mode")
    def action_maintenance(self, request, queryset):
        svc = WorkstationAdminService()
        for ws in queryset:
            svc.set_maintenance(ws, actor=request.user, reason="Admin action")
        self.message_user(request, f"Maintenance set on {queryset.count()} workstation(s).", messages.INFO)

    @admin.action(description="Issue PING command")
    def action_ping(self, request, queryset):
        svc = CommandService()
        for ws in queryset:
            svc.create_command(ws, CommandType.PING, created_by=request.user)
        self.message_user(request, f"PING queued for {queryset.count()} workstation(s).", messages.SUCCESS)


@admin.register(InstalledSoftware)
class InstalledSoftwareAdmin(admin.ModelAdmin):
    list_display = ("software_name", "version", "publisher", "workstation", "licensed", "is_present", "last_updated")
    list_filter = ("licensed", "is_present", "category")
    search_fields = ("software_name", "publisher", "version", "workstation__hostname")


@admin.register(RemoteCommand)
class RemoteCommandAdmin(admin.ModelAdmin):
    list_display = ("command_type", "status", "workstation", "created_at", "completed_at")
    list_filter = ("command_type", "status")
    search_fields = ("workstation__hostname", "workstation__agent_id")
    readonly_fields = ("id", "created_at", "delivered_at", "started_at", "completed_at")


@admin.register(CommandExecution)
class CommandExecutionAdmin(admin.ModelAdmin):
    list_display = ("command", "status", "duration_ms", "created_at")
    list_filter = ("status",)


@admin.register(WorkstationHeartbeat)
class WorkstationHeartbeatAdmin(admin.ModelAdmin):
    list_display = ("workstation", "received_at", "cpu", "memory", "disk", "logged_in_user", "current_state")
    list_filter = ("current_state", "idle", "online")
    readonly_fields = ("raw_payload",)


@admin.register(TelemetrySnapshot)
class TelemetrySnapshotAdmin(admin.ModelAdmin):
    list_display = ("workstation", "metric_name", "value", "unit", "recorded_at")
    list_filter = ("metric_name",)


@admin.register(WorkstationEvent)
class WorkstationEventAdmin(admin.ModelAdmin):
    list_display = ("category", "action", "workstation", "success", "created_at")
    list_filter = ("category", "success")
    search_fields = ("action", "details", "workstation__hostname")


@admin.register(WorkstationCapability)
class WorkstationCapabilityAdmin(admin.ModelAdmin):
    list_display = ("workstation", "supports_rdp", "gpu_available", "ram_gb", "cpu_cores", "disk_space_gb")


@admin.register(WorkstationStateHistory)
class WorkstationStateHistoryAdmin(admin.ModelAdmin):
    list_display = ("workstation", "from_status", "to_status", "reason", "created_at")
    list_filter = ("to_status", "from_status")


@admin.register(AgentToken)
class AgentTokenAdmin(admin.ModelAdmin):
    list_display = ("workstation", "token_prefix", "is_active", "issued_at", "expires_at", "revoked_at", "last_used_at")
    list_filter = ("is_active",)
    readonly_fields = ("token_hash", "token_prefix", "issued_at")


@admin.register(SoftwareLicense)
class SoftwareLicenseAdmin(admin.ModelAdmin):
    list_display = ("software", "workstation", "status", "expiry", "seats")
    search_fields = ("software", "workstation__hostname")


@admin.register(WorkstationInventory)
class WorkstationInventoryAdmin(admin.ModelAdmin):
    list_display = ("workstation", "software_count", "license_count", "last_synced_at")
    readonly_fields = ("hardware_json", "content_hash")


@admin.register(AnalysisReservation)
class AnalysisReservationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "status",
        "user",
        "workstation",
        "requested_start",
        "requested_end",
        "priority",
        "booking",
    )
    list_filter = ("status",)
    search_fields = ("user__email", "workstation__hostname")
    readonly_fields = ("id", "allocated_at", "released_at", "allocation_score", "created_at", "updated_at")


@admin.register(ReservationQueue)
class ReservationQueueAdmin(admin.ModelAdmin):
    list_display = ("reservation", "status", "priority", "enqueued_at", "dequeued_at")
    list_filter = ("status",)


@admin.register(MaintenanceWindow)
class MaintenanceWindowAdmin(admin.ModelAdmin):
    list_display = ("workstation", "start", "end", "active", "reason", "created_by")
    list_filter = ("active",)


@admin.register(AllocationRule)
class AllocationRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "rule_type", "priority_boost", "is_active", "department", "user")
    list_filter = ("rule_type", "is_active")


@admin.register(SoftwareRequirement)
class SoftwareRequirementAdmin(admin.ModelAdmin):
    list_display = ("name", "software", "minimum_version", "required", "gpu_required", "license_required")


@admin.register(ReservationHistory)
class ReservationHistoryAdmin(admin.ModelAdmin):
    list_display = ("reservation", "from_status", "to_status", "reason", "created_at")


@admin.register(ReservationConflict)
class ReservationConflictAdmin(admin.ModelAdmin):
    list_display = ("reservation", "conflict_type", "resolved", "created_at")
    list_filter = ("conflict_type", "resolved")


@admin.register(ReservationAudit)
class ReservationAuditAdmin(admin.ModelAdmin):
    list_display = ("reservation", "action", "success", "actor", "created_at")


@admin.register(ReservationEvent)
class ReservationEventAdmin(admin.ModelAdmin):
    list_display = ("reservation", "event_type", "created_at")


@admin.register(ReservationPreference)
class ReservationPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "preferred_workstation", "preferred_building", "updated_at")


@admin.register(SchedulerTelemetry)
class SchedulerTelemetryAdmin(admin.ModelAdmin):
    list_display = ("metric_name", "value", "unit", "recorded_at")
    list_filter = ("metric_name",)


# --- Milestone 4 ---
from iic_booking.remote_analysis.session_models import (  # noqa: E402
    GuacamoleConnection,
    RemoteAnalysisSettings,
    RemoteDesktopSession,
    SessionAudit,
    SessionHealth,
    SessionLaunch,
    SessionRecording,
    SessionStatistics,
    SessionTermination,
    SessionToken,
    WorkstationRdpSecret,
)


@admin.register(RemoteAnalysisSettings)
class RemoteAnalysisSettingsAdmin(admin.ModelAdmin):
    list_display = ("id", "mock_guacamole", "session_timeout", "idle_timeout", "max_concurrent_sessions", "updated_at")
    fieldsets = (
        (
            "Guacamole (internal)",
            {
                "fields": (
                    "guacamole_base_url",
                    "guacamole_api_url",
                    "guacamole_admin_username",
                    "guacamole_admin_password",
                    "guacamole_data_source",
                    "verify_tls",
                    "mock_guacamole",
                )
            },
        ),
        (
            "Session policy",
            {
                "fields": (
                    "connection_timeout",
                    "session_timeout",
                    "idle_timeout",
                    "idle_warning_seconds",
                    "max_concurrent_sessions",
                    "prepare_timeout_seconds",
                    "launch_token_lifetime_seconds",
                    "bind_token_to_ip",
                )
            },
        ),
        (
            "Features",
            {
                "fields": (
                    "clipboard_enabled",
                    "clipboard_policy",
                    "file_transfer_enabled",
                    "file_transfer_policy",
                    "audio_enabled",
                    "recording_enabled",
                    "default_display_width",
                    "default_display_height",
                    "default_color_depth",
                )
            },
        ),
        (
            "Workspace / file exchange",
            {
                "fields": (
                    "workspace_root",
                    "archive_root",
                    "default_quota_gb",
                    "retention_days",
                    "chunk_size_bytes",
                    "compression_enabled",
                    "virus_scanner",
                    "checksum_algorithm",
                    "maximum_upload_size",
                    "maximum_download_size",
                    "version_history_limit",
                    "allowed_extensions",
                    "blocked_extensions",
                    "folder_template",
                )
            },
        ),
    )


class WorkstationRdpSecretInline(admin.StackedInline):
    model = WorkstationRdpSecret
    extra = 0
    fields = ("username", "password_encrypted", "domain", "port", "security", "updated_at")
    readonly_fields = ("updated_at",)


# Attach RDP secret inline to workstation admin
AnalysisWorkstationAdmin.inlines = list(AnalysisWorkstationAdmin.inlines) + [WorkstationRdpSecretInline]


@admin.register(RemoteDesktopSession)
class RemoteDesktopSessionAdmin(admin.ModelAdmin):
    list_display = ("id", "status", "user", "workstation", "created_at", "connected_at", "expires_at")
    list_filter = ("status",)
    search_fields = ("user__email", "workstation__hostname")
    readonly_fields = ("id", "created_at", "updated_at", "launch_time", "connected_at", "disconnected_at")


@admin.register(GuacamoleConnection)
class GuacamoleConnectionAdmin(admin.ModelAdmin):
    list_display = ("session", "guacamole_connection_id", "is_active", "created_at", "destroyed_at")
    readonly_fields = ("id", "internal_hostname", "metadata", "created_at")


@admin.register(SessionToken)
class SessionTokenAdmin(admin.ModelAdmin):
    list_display = ("token_prefix", "session", "bound_user", "expires_at", "consumed_at", "revoked_at")
    readonly_fields = ("token_hash", "token_prefix", "issued_at")


@admin.register(SessionAudit)
class SessionAuditAdmin(admin.ModelAdmin):
    list_display = ("session", "action", "success", "actor", "created_at")
    list_filter = ("success", "action")


@admin.register(SessionStatistics)
class SessionStatisticsAdmin(admin.ModelAdmin):
    list_display = ("session", "duration_seconds", "reconnect_count", "launch_latency_ms", "prepare_latency_ms")


@admin.register(SessionHealth)
class SessionHealthAdmin(admin.ModelAdmin):
    list_display = ("session", "score", "guacamole_reachable", "agent_online", "workstation_healthy", "last_check_at")


@admin.register(SessionTermination)
class SessionTerminationAdmin(admin.ModelAdmin):
    list_display = ("session", "reason", "terminated_by", "terminated_at", "cleanup_completed", "guacamole_destroyed")


@admin.register(SessionLaunch)
class SessionLaunchAdmin(admin.ModelAdmin):
    list_display = ("session", "launched_at", "client_ip", "success")


@admin.register(SessionRecording)
class SessionRecordingAdmin(admin.ModelAdmin):
    list_display = ("session", "enabled", "started_at", "ended_at", "notes")


@admin.register(WorkstationRdpSecret)
class WorkstationRdpSecretAdmin(admin.ModelAdmin):
    list_display = ("workstation", "username", "domain", "port", "updated_at")
    readonly_fields = ("password_encrypted", "updated_at")


# --- Milestone 5 ---
from iic_booking.remote_analysis.workspace_models import (  # noqa: E402
    AnalysisWorkspace,
    TransferPolicy,
    VirusScanResult,
    WorkspaceArchive,
    WorkspaceAudit,
    WorkspaceFile,
    WorkspaceFolder,
    WorkspaceQuota,
    WorkspaceShare,
    WorkspaceTransfer,
)


@admin.register(AnalysisWorkspace)
class AnalysisWorkspaceAdmin(admin.ModelAdmin):
    list_display = ("id", "status", "user", "workstation", "quota_gb", "current_usage_bytes", "archive_status", "created_at")
    list_filter = ("status", "archive_status")
    search_fields = ("user__email", "storage_key", "workstation__hostname")
    readonly_fields = ("id", "storage_key", "created_at", "updated_at", "current_usage_bytes")


@admin.register(WorkspaceFile)
class WorkspaceFileAdmin(admin.ModelAdmin):
    list_display = ("original_name", "workspace", "relative_path", "size", "version", "virus_status", "deleted")
    list_filter = ("virus_status", "category", "deleted")
    search_fields = ("original_name", "relative_path", "sha256")
    readonly_fields = ("sha256", "storage_relpath", "stored_name")


@admin.register(WorkspaceFolder)
class WorkspaceFolderAdmin(admin.ModelAdmin):
    list_display = ("workspace", "name", "relative_path", "read_only", "category")


@admin.register(WorkspaceTransfer)
class WorkspaceTransferAdmin(admin.ModelAdmin):
    list_display = ("workspace", "direction", "status", "bytes_transferred", "bytes_total", "created_at")
    list_filter = ("direction", "status")


@admin.register(WorkspaceArchive)
class WorkspaceArchiveAdmin(admin.ModelAdmin):
    list_display = ("workspace", "archive_key", "size_bytes", "created_at", "restored_at")
    readonly_fields = ("archive_key", "sha256")


@admin.register(WorkspaceAudit)
class WorkspaceAuditAdmin(admin.ModelAdmin):
    list_display = ("workspace", "action", "success", "actor", "created_at")
    list_filter = ("action", "success")


@admin.register(TransferPolicy)
class TransferPolicyAdmin(admin.ModelAdmin):
    list_display = ("name", "department", "workstation", "is_active", "max_file_size")


@admin.register(WorkspaceQuota)
class WorkspaceQuotaAdmin(admin.ModelAdmin):
    list_display = ("workspace", "user", "department", "soft_limit_bytes", "hard_limit_bytes", "override_allowed")


@admin.register(WorkspaceShare)
class WorkspaceShareAdmin(admin.ModelAdmin):
    list_display = ("workspace", "shared_with", "department_only", "read_only", "expires_at", "revoked_at")


@admin.register(VirusScanResult)
class VirusScanResultAdmin(admin.ModelAdmin):
    list_display = ("file", "scanner", "status", "scanned_at")
    list_filter = ("status", "scanner")


# --- Milestone 6 ---
from iic_booking.remote_analysis.operations_models import (  # noqa: E402
    AlertEvent,
    AlertRule,
    AnalysisReport,
    CapacitySnapshot,
    DashboardSnapshot,
    OperationalKPI,
    PerformanceMetric,
    SessionAnalytics,
    UsageTrend,
    WorkstationAvailability,
    WorkstationUtilization,
)


@admin.register(OperationalKPI)
class OperationalKPIAdmin(admin.ModelAdmin):
    list_display = ("period", "period_start", "total_workstations", "online_workstations", "average_utilization", "open_alerts")
    list_filter = ("period",)


@admin.register(WorkstationUtilization)
class WorkstationUtilizationAdmin(admin.ModelAdmin):
    list_display = ("workstation", "period", "period_start", "utilization_percent", "availability_percent", "session_hours")
    list_filter = ("period",)


@admin.register(SessionAnalytics)
class SessionAnalyticsAdmin(admin.ModelAdmin):
    list_display = ("period", "period_start", "total_sessions", "average_duration_seconds", "success_rate")


@admin.register(AlertRule)
class AlertRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "category", "severity", "metric_name", "threshold", "is_active")
    list_filter = ("category", "severity", "is_active")


@admin.register(AlertEvent)
class AlertEventAdmin(admin.ModelAdmin):
    list_display = ("title", "severity", "category", "status", "workstation", "created_at", "acknowledged", "resolved")
    list_filter = ("severity", "category", "status")


@admin.register(AnalysisReport)
class AnalysisReportAdmin(admin.ModelAdmin):
    list_display = ("report_type", "format", "status", "created_at", "completed_at")
    list_filter = ("report_type", "format", "status")
    readonly_fields = ("storage_relpath", "payload")


@admin.register(CapacitySnapshot)
class CapacitySnapshotAdmin(admin.ModelAdmin):
    list_display = ("period", "period_start", "peak_concurrent_sessions", "average_occupancy_percent", "predicted_capacity_need")


@admin.register(PerformanceMetric)
class PerformanceMetricAdmin(admin.ModelAdmin):
    list_display = ("metric_name", "value", "unit", "period", "period_start", "workstation")
    list_filter = ("metric_name", "period")


@admin.register(UsageTrend)
class UsageTrendAdmin(admin.ModelAdmin):
    list_display = ("metric_name", "period", "period_start", "value", "unit")


@admin.register(WorkstationAvailability)
class WorkstationAvailabilityAdmin(admin.ModelAdmin):
    list_display = ("workstation", "period", "period_start", "operational_availability", "mtbf_hours", "mttr_hours")


@admin.register(DashboardSnapshot)
class DashboardSnapshotAdmin(admin.ModelAdmin):
    list_display = ("dashboard_key", "generated_at")
    readonly_fields = ("payload",)


# --- Milestone 7 ---
from iic_booking.remote_analysis.collaboration_models import (  # noqa: E402
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


@admin.register(SessionComment)
class SessionCommentAdmin(admin.ModelAdmin):
    list_display = ("session", "author", "pinned", "created_at", "deleted")
    list_filter = ("pinned", "deleted")


@admin.register(WorkspaceComment)
class WorkspaceCommentAdmin(admin.ModelAdmin):
    list_display = ("workspace", "author", "pinned", "created_at", "deleted")
    list_filter = ("pinned", "deleted")


@admin.register(SessionNote)
class SessionNoteAdmin(admin.ModelAdmin):
    list_display = ("title", "author", "visibility", "pinned", "created_at")
    list_filter = ("visibility", "pinned")


@admin.register(SharedWorkspace)
class SharedWorkspaceAdmin(admin.ModelAdmin):
    list_display = ("name", "workspace", "created_by", "expires_at", "revoked_at", "created_at")


@admin.register(WorkspaceSharePermission)
class WorkspaceSharePermissionAdmin(admin.ModelAdmin):
    list_display = ("shared_workspace", "user", "department", "permission", "created_at")
    list_filter = ("permission",)


@admin.register(SessionInvitation)
class SessionInvitationAdmin(admin.ModelAdmin):
    list_display = ("kind", "status", "invited_by", "invited_user", "expires_at", "created_at")
    list_filter = ("kind", "status")


@admin.register(Notification)
class NotificationAdmin(admin.ModelAdmin):
    list_display = ("title", "user", "notification_type", "channel", "status", "created_at")
    list_filter = ("notification_type", "channel", "status")


@admin.register(NotificationPreference)
class NotificationPreferenceAdmin(admin.ModelAdmin):
    list_display = ("user", "portal_enabled", "email_enabled", "digest_frequency", "updated_at")


@admin.register(ActivityFeed)
class ActivityFeedAdmin(admin.ModelAdmin):
    list_display = ("name", "user", "created_at")


@admin.register(ActivityEvent)
class ActivityEventAdmin(admin.ModelAdmin):
    list_display = ("verb", "summary", "actor", "created_at")
    list_filter = ("verb",)


@admin.register(SessionAssistanceRequest)
class SessionAssistanceRequestAdmin(admin.ModelAdmin):
    list_display = ("subject", "status", "priority", "requested_by", "assigned_to", "created_at")
    list_filter = ("status", "priority")


@admin.register(SessionAssistanceEvent)
class SessionAssistanceEventAdmin(admin.ModelAdmin):
    list_display = ("request", "from_status", "to_status", "actor", "created_at")


@admin.register(Announcement)
class AnnouncementAdmin(admin.ModelAdmin):
    list_display = ("title", "active", "created_by", "created_at")
    list_filter = ("active",)


@admin.register(Bookmark)
class BookmarkAdmin(admin.ModelAdmin):
    list_display = ("label", "user", "target_type", "created_at")


@admin.register(FavoriteWorkstation)
class FavoriteWorkstationAdmin(admin.ModelAdmin):
    list_display = ("user", "workstation", "created_at")


@admin.register(RecentWorkspace)
class RecentWorkspaceAdmin(admin.ModelAdmin):
    list_display = ("user", "workspace", "last_accessed_at")


@admin.register(CollaborationTelemetry)
class CollaborationTelemetryAdmin(admin.ModelAdmin):
    list_display = ("metric_name", "value", "unit", "recorded_at")
    list_filter = ("metric_name",)

