"""Milestone 4 — browser remote desktop / Guacamole session models."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from iic_booking.remote_analysis.constants import (
    ClipboardPolicy,
    FileTransferPolicy,
    SessionStatus,
    TransportMode,
)


class RemoteAnalysisSettings(models.Model):
    """Singleton Portal settings for Guacamole integration (admin-managed)."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    guacamole_base_url = models.URLField(
        blank=True,
        default="",
        help_text=_("Public Guacamole base URL used only server-side to build launch redirects."),
    )
    guacamole_api_url = models.URLField(
        blank=True,
        default="",
        help_text=_("Guacamole REST API base URL (internal). Never returned to browsers."),
    )
    guacamole_admin_username = models.CharField(max_length=255, blank=True, default="")
    guacamole_admin_password = models.CharField(max_length=512, blank=True, default="")
    guacamole_data_source = models.CharField(max_length=64, blank=True, default="postgresql")
    verify_tls = models.BooleanField(default=True)
    connection_timeout = models.PositiveIntegerField(default=30, help_text=_("Seconds"))
    session_timeout = models.PositiveIntegerField(default=120, help_text=_("Minutes"))
    idle_timeout = models.PositiveIntegerField(default=15, help_text=_("Minutes"))
    idle_warning_seconds = models.PositiveIntegerField(default=60)
    max_concurrent_sessions = models.PositiveIntegerField(default=50)
    single_active_session_per_booking = models.BooleanField(
        default=True,
        help_text=_("When True, only one open remote desktop session is allowed per booking (or reservation)."),
    )
    clipboard_enabled = models.BooleanField(default=True)
    clipboard_policy = models.CharField(
        max_length=32, choices=ClipboardPolicy.choices, default=ClipboardPolicy.TEXT_ONLY
    )
    file_transfer_enabled = models.BooleanField(default=False)
    file_transfer_policy = models.CharField(
        max_length=32, choices=FileTransferPolicy.choices, default=FileTransferPolicy.DISABLED
    )
    audio_enabled = models.BooleanField(default=True)
    recording_enabled = models.BooleanField(
        default=False,
        help_text=_("Reserved for future recording support — not implemented in Milestone 4."),
    )
    default_display_width = models.PositiveIntegerField(default=1920)
    default_display_height = models.PositiveIntegerField(default=1080)
    default_color_depth = models.PositiveSmallIntegerField(default=24)
    prepare_timeout_seconds = models.PositiveIntegerField(default=120)
    launch_token_lifetime_seconds = models.PositiveIntegerField(default=90)
    bind_token_to_ip = models.BooleanField(default=False)
    mock_guacamole = models.BooleanField(
        default=True,
        help_text=_("When True, use in-process mock Guacamole responses (dev/test)."),
    )
    transport_mode = models.CharField(
        max_length=32,
        choices=TransportMode.choices,
        default=TransportMode.REVERSE_TUNNEL,
        help_text=_(
            "Sole supported mode: reverse_tunnel — guacd dials the AWS adapter; "
            "the agent bridges to localhost RDP."
        ),
    )
    tunnel_gateway_admin_url = models.URLField(
        blank=True,
        default="",
        help_text=_("Internal Gateway admin HTTP base (Portal→Gateway). Never returned to browsers."),
    )
    tunnel_gateway_wss_url = models.URLField(
        blank=True,
        default="",
        help_text=_("Public WSS URL agents use for reverse tunnels."),
    )
    tunnel_adapter_hostname = models.CharField(
        max_length=255,
        blank=True,
        default="reverse-tunnel-gateway",
        help_text=_("Hostname guacd uses to reach the GuacamoleSocketAdapter (compose DNS)."),
    )
    tunnel_token_lifetime_seconds = models.PositiveIntegerField(default=120)
    tunnel_idle_timeout_seconds = models.PositiveIntegerField(default=900)
    tunnel_max_lifetime_seconds = models.PositiveIntegerField(default=14400)
    # Milestone 5 — Analysis workspace / secure file exchange
    workspace_root = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        help_text=_("Absolute Portal storage root for workspaces. Empty = MEDIA_ROOT/remote_analysis/workspaces"),
    )
    archive_root = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        help_text=_("Absolute archive root. Empty = MEDIA_ROOT/remote_analysis/archives"),
    )
    default_quota_gb = models.FloatField(default=50.0)
    retention_days = models.PositiveIntegerField(default=90)
    chunk_size_bytes = models.PositiveIntegerField(default=5 * 1024 * 1024)
    compression_enabled = models.BooleanField(default=False)
    virus_scanner = models.CharField(
        max_length=64,
        blank=True,
        default="noop",
        help_text=_("noop | defender | clamav (only noop implemented in Milestone 5)"),
    )
    checksum_algorithm = models.CharField(max_length=32, default="sha256")
    maximum_upload_size = models.BigIntegerField(default=2 * 1024 * 1024 * 1024)
    maximum_download_size = models.BigIntegerField(default=2 * 1024 * 1024 * 1024)
    version_history_limit = models.PositiveIntegerField(default=20)
    allowed_extensions = models.TextField(
        blank=True,
        default="",
        help_text=_("Comma-separated allowlist. Empty = allow all except blocked."),
    )
    blocked_extensions = models.TextField(
        blank=True,
        default=".exe,.bat,.cmd,.ps1,.msi,.scr,.vbs",
    )
    folder_template = models.JSONField(
        default=list,
        blank=True,
        help_text=_("Folder names created for each workspace. Empty = default template."),
    )
    # Automatic data sync policies
    workspace_sync_mode = models.CharField(
        max_length=32,
        default="end_of_session",
        help_text=_("end_of_session | interval"),
    )
    workspace_sync_interval_seconds = models.PositiveIntegerField(
        default=300,
        help_text=_("Used when workspace_sync_mode=interval."),
    )
    transfer_max_retries = models.PositiveSmallIntegerField(default=3)
    compression_min_bytes = models.BigIntegerField(
        default=5 * 1024 * 1024,
        help_text=_("Compress agent uploads at or above this size when compression_enabled."),
    )
    bandwidth_limit_kbps = models.PositiveIntegerField(
        default=0,
        help_text=_("Advisory agent bandwidth cap; 0 = unlimited."),
    )
    # Analyze Data (booking-facing post-analysis CTA)
    analyze_data_button_label = models.CharField(
        max_length=128,
        default="Open Analysis Workspace",
        help_text=_("Default user-facing CTA label for opening Analysis Workspace."),
    )
    analyze_data_require_s3_files = models.BooleanField(
        default=True,
        help_text=_("When True, Analyze Data requires RAW/results files (S3/DSA/operator) to be present."),
    )
    analyze_data_stage_raw_on_launch = models.BooleanField(
        default=True,
        help_text=_("When True, stage booking RAW files into workspace RawData before desktop launch."),
    )
    analyze_data_prefer_workflow = models.BooleanField(
        default=True,
        help_text=_("When True, Analyze Data prefers Equipment→Workflow mappings over legacy single-software."),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Remote analysis settings")
        verbose_name_plural = _("Remote analysis settings")

    def __str__(self) -> str:
        return "Remote Analysis Settings"

    @classmethod
    def get_solo(cls) -> "RemoteAnalysisSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        # Production: allow env secrets/URLs to override DB without code changes.
        from iic_booking.remote_analysis.guacamole.settings_env import overlay_from_environ

        return overlay_from_environ(obj)


class WorkstationRdpSecret(models.Model):
    """
    Server-side RDP credentials for Guacamole connection creation.
    NEVER expose via user-facing serializers or APIs.
    """

    workstation = models.OneToOneField(
        "remote_analysis.AnalysisWorkstation",
        on_delete=models.CASCADE,
        related_name="rdp_secret",
    )
    username = models.CharField(max_length=255, blank=True, default="")
    password_encrypted = models.TextField(blank=True, default="")
    domain = models.CharField(max_length=255, blank=True, default="")
    port = models.PositiveIntegerField(default=3389)
    security = models.CharField(max_length=32, blank=True, default="nla")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("Workstation RDP secret")
        verbose_name_plural = _("Workstation RDP secrets")


class RemoteDesktopSession(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reservation = models.ForeignKey(
        "remote_analysis.AnalysisReservation",
        on_delete=models.PROTECT,
        related_name="desktop_sessions",
    )
    booking = models.ForeignKey(
        "equipment.Booking",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="desktop_sessions",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="ra_desktop_sessions",
    )
    workstation = models.ForeignKey(
        "remote_analysis.AnalysisWorkstation",
        on_delete=models.PROTECT,
        related_name="desktop_sessions",
    )
    status = models.CharField(
        max_length=32,
        choices=SessionStatus.choices,
        default=SessionStatus.CREATED,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    launch_time = models.DateTimeField(null=True, blank=True)
    connected_at = models.DateTimeField(null=True, blank=True)
    disconnected_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    extension_grace_used = models.BooleanField(
        default=False,
        help_text=_("True after a one-shot grace extension while others were waiting (R9)."),
    )
    idle_timeout_minutes = models.PositiveIntegerField(default=15)
    termination_reason = models.CharField(max_length=512, blank=True, default="")
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    browser = models.CharField(max_length=255, blank=True, default="")
    client_platform = models.CharField(max_length=255, blank=True, default="")
    recording_enabled = models.BooleanField(default=False)
    clipboard_enabled = models.BooleanField(default=True)
    clipboard_policy = models.CharField(
        max_length=32, choices=ClipboardPolicy.choices, default=ClipboardPolicy.TEXT_ONLY
    )
    file_transfer_enabled = models.BooleanField(default=False)
    file_transfer_policy = models.CharField(
        max_length=32, choices=FileTransferPolicy.choices, default=FileTransferPolicy.DISABLED
    )
    audio_enabled = models.BooleanField(default=True)
    multi_monitor = models.BooleanField(default=False)
    display_width = models.PositiveIntegerField(default=1920)
    display_height = models.PositiveIntegerField(default=1080)
    color_depth = models.PositiveSmallIntegerField(default=24)
    prepare_command = models.ForeignKey(
        "remote_analysis.RemoteCommand",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="prepare_sessions",
    )
    cleanup_command = models.ForeignKey(
        "remote_analysis.RemoteCommand",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="cleanup_sessions",
    )
    last_activity_at = models.DateTimeField(null=True, blank=True)
    reconnect_count = models.PositiveIntegerField(default=0)
    failure_detail = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["user", "status"]),
            models.Index(fields=["status", "expires_at"]),
            models.Index(fields=["status", "last_activity_at"]),
        ]

    def __str__(self) -> str:
        return f"Session {self.id} ({self.status})"


class SessionStateHistory(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(RemoteDesktopSession, on_delete=models.CASCADE, related_name="state_history")
    from_status = models.CharField(max_length=32, blank=True, default="")
    to_status = models.CharField(max_length=32, choices=SessionStatus.choices)
    reason = models.CharField(max_length=512, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class GuacamoleConnection(models.Model):
    """Ephemeral Guacamole connection metadata (no user-visible credentials/IPs)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.OneToOneField(
        RemoteDesktopSession,
        on_delete=models.CASCADE,
        related_name="guacamole_connection",
    )
    guacamole_connection_id = models.CharField(max_length=128, blank=True, default="")
    guacamole_identifier = models.CharField(max_length=255, blank=True, default="")
    guacamole_username = models.CharField(max_length=255, blank=True, default="")
    protocol = models.CharField(max_length=32, default="rdp")
    created_at = models.DateTimeField(auto_now_add=True)
    destroyed_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    # Internal-only hostname/IP for Guacamole — never serialize to end users
    internal_hostname = models.CharField(max_length=255, blank=True, default="")
    metadata = models.JSONField(default=dict, blank=True)

    class Meta:
        verbose_name = _("Guacamole connection")
        verbose_name_plural = _("Guacamole connections")


class SessionToken(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(RemoteDesktopSession, on_delete=models.CASCADE, related_name="tokens")
    token_hash = models.CharField(max_length=128, unique=True)
    token_prefix = models.CharField(max_length=12, blank=True, default="")
    issued_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    bound_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="ra_session_tokens",
    )
    bound_ip = models.GenericIPAddressField(null=True, blank=True)
    is_single_use = models.BooleanField(default=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-issued_at"]


class SessionLaunch(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(RemoteDesktopSession, on_delete=models.CASCADE, related_name="launches")
    launched_at = models.DateTimeField(auto_now_add=True)
    client_ip = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=512, blank=True, default="")
    success = models.BooleanField(default=True)
    detail = models.CharField(max_length=512, blank=True, default="")


class SessionAudit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(
        RemoteDesktopSession,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audits",
    )
    action = models.CharField(max_length=128)
    details = models.TextField(blank=True, default="")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    success = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class SessionRecording(models.Model):
    """Placeholder for future recording support (Milestone 4 stores metadata only)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(RemoteDesktopSession, on_delete=models.CASCADE, related_name="recordings")
    enabled = models.BooleanField(default=False)
    storage_path = models.CharField(max_length=1024, blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    notes = models.CharField(max_length=512, blank=True, default="Future recording support")


class SessionStatistics(models.Model):
    session = models.OneToOneField(RemoteDesktopSession, on_delete=models.CASCADE, related_name="statistics")
    duration_seconds = models.FloatField(default=0)
    idle_seconds = models.FloatField(default=0)
    reconnect_count = models.PositiveIntegerField(default=0)
    bytes_in = models.BigIntegerField(default=0)
    bytes_out = models.BigIntegerField(default=0)
    clipboard_events = models.PositiveIntegerField(default=0)
    file_transfer_events = models.PositiveIntegerField(default=0)
    error_count = models.PositiveIntegerField(default=0)
    launch_latency_ms = models.FloatField(null=True, blank=True)
    prepare_latency_ms = models.FloatField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)


class SessionTermination(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.OneToOneField(RemoteDesktopSession, on_delete=models.CASCADE, related_name="termination")
    reason = models.CharField(max_length=512, blank=True, default="")
    terminated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    terminated_at = models.DateTimeField(auto_now_add=True)
    cleanup_completed = models.BooleanField(default=False)
    guacamole_destroyed = models.BooleanField(default=False)
    reservation_released = models.BooleanField(default=False)


class SessionHealth(models.Model):
    session = models.OneToOneField(RemoteDesktopSession, on_delete=models.CASCADE, related_name="health")
    guacamole_reachable = models.BooleanField(default=False)
    agent_online = models.BooleanField(default=False)
    workstation_healthy = models.BooleanField(default=False)
    last_check_at = models.DateTimeField(null=True, blank=True)
    detail = models.CharField(max_length=512, blank=True, default="")
    score = models.PositiveSmallIntegerField(default=100)


class ConnectionHistory(models.Model):
    id = models.BigAutoField(primary_key=True)
    session = models.ForeignKey(RemoteDesktopSession, on_delete=models.CASCADE, related_name="connection_history")
    event = models.CharField(max_length=64)
    detail = models.CharField(max_length=512, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class SessionTelemetry(models.Model):
    id = models.BigAutoField(primary_key=True)
    metric_name = models.CharField(max_length=128, db_index=True)
    value = models.FloatField()
    unit = models.CharField(max_length=32, blank=True, default="")
    session = models.ForeignKey(
        RemoteDesktopSession,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="telemetry",
    )
    tags = models.JSONField(default=dict, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-recorded_at"]
