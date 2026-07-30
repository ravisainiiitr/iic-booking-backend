"""Milestone 5 — Analysis Workspace & secure file exchange models."""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models
from django.utils.translation import gettext_lazy as _

from iic_booking.remote_analysis.constants import (
    ArchiveStatus,
    FileCategory,
    TransferDirection,
    TransferStatus,
    VirusStatus,
    WorkspaceStatus,
    WorkspaceSyncPhase,
)


class AnalysisWorkspace(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    reservation = models.OneToOneField(
        "remote_analysis.AnalysisReservation",
        on_delete=models.PROTECT,
        related_name="workspace",
    )
    booking = models.ForeignKey(
        "equipment.Booking",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="analysis_workspaces",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="analysis_workspaces",
    )
    department = models.ForeignKey(
        "users.Department",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="analysis_workspaces",
    )
    workstation = models.ForeignKey(
        "remote_analysis.AnalysisWorkstation",
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="analysis_workspaces",
    )
    status = models.CharField(
        max_length=32,
        choices=WorkspaceStatus.choices,
        default=WorkspaceStatus.CREATING,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)
    retention_until = models.DateTimeField(null=True, blank=True)
    quota_gb = models.FloatField(default=50.0)
    current_usage_bytes = models.BigIntegerField(default=0)
    read_only = models.BooleanField(default=False)
    archive_status = models.CharField(
        max_length=32,
        choices=ArchiveStatus.choices,
        default=ArchiveStatus.NONE,
    )
    # Internal storage key — never expose absolute paths to users
    storage_key = models.CharField(max_length=255, unique=True)
    local_agent_path = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        help_text=_("Agent-local relative path hint (not a Portal filesystem path)."),
    )
    last_synced_at = models.DateTimeField(null=True, blank=True)
    sync_phase = models.CharField(
        max_length=32,
        choices=WorkspaceSyncPhase.choices,
        default=WorkspaceSyncPhase.PREPARING,
        db_index=True,
        help_text=_("Automatic data sync lifecycle phase shown to users."),
    )
    sync_progress_percent = models.PositiveSmallIntegerField(default=0)
    sync_message = models.CharField(max_length=512, blank=True, default="")
    upload_verified_at = models.DateTimeField(
        null=True,
        blank=True,
        help_text=_("Set when collect upload checksums are verified on the portal."),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["user", "status"]),
            models.Index(fields=["status", "retention_until"]),
            models.Index(fields=["sync_phase", "updated_at"]),
        ]

    def __str__(self) -> str:
        return f"Workspace {self.id} ({self.status})"

    @property
    def current_usage_gb(self) -> float:
        return self.current_usage_bytes / (1024**3)


class WorkspaceFolder(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(AnalysisWorkspace, on_delete=models.CASCADE, related_name="folders")
    name = models.CharField(max_length=255)
    relative_path = models.CharField(max_length=1024)
    read_only = models.BooleanField(default=False)
    category = models.CharField(max_length=32, choices=FileCategory.choices, default=FileCategory.OTHER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("workspace", "relative_path")]
        ordering = ["relative_path"]

    def __str__(self) -> str:
        return self.relative_path


class WorkspaceFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(AnalysisWorkspace, on_delete=models.CASCADE, related_name="files")
    folder = models.ForeignKey(
        WorkspaceFolder,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="files",
    )
    original_name = models.CharField(max_length=512)
    stored_name = models.CharField(max_length=512)
    relative_path = models.CharField(max_length=1024, db_index=True)
    size = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, default="", db_index=True)
    mime_type = models.CharField(max_length=255, blank=True, default="")
    category = models.CharField(max_length=32, choices=FileCategory.choices, default=FileCategory.OTHER)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="workspace_uploads",
    )
    uploaded_at = models.DateTimeField(auto_now_add=True)
    modified_at = models.DateTimeField(auto_now=True)
    version = models.PositiveIntegerField(default=1)
    is_current = models.BooleanField(default=True)
    virus_status = models.CharField(
        max_length=32,
        choices=VirusStatus.choices,
        default=VirusStatus.PENDING,
    )
    download_count = models.PositiveIntegerField(default=0)
    locked = models.BooleanField(default=False)
    deleted = models.BooleanField(default=False)
    # Internal relative storage path under workspace root — never absolute to clients
    storage_relpath = models.CharField(max_length=1024, blank=True, default="")
    source = models.CharField(max_length=64, blank=True, default="portal")  # portal | agent | admin

    class Meta:
        ordering = ["-uploaded_at"]
        indexes = [
            models.Index(fields=["workspace", "relative_path", "is_current"]),
            models.Index(fields=["workspace", "deleted"]),
        ]

    def __str__(self) -> str:
        return f"{self.original_name} v{self.version}"


class WorkspaceVersion(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file = models.ForeignKey(WorkspaceFile, on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField()
    size = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    storage_relpath = models.CharField(max_length=1024, blank=True, default="")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    note = models.CharField(max_length=512, blank=True, default="")

    class Meta:
        ordering = ["-version"]
        unique_together = [("file", "version")]


class WorkspaceTransfer(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(AnalysisWorkspace, on_delete=models.CASCADE, related_name="transfers")
    file = models.ForeignKey(
        WorkspaceFile,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="transfers",
    )
    direction = models.CharField(max_length=32, choices=TransferDirection.choices)
    status = models.CharField(
        max_length=32,
        choices=TransferStatus.choices,
        default=TransferStatus.PENDING,
        db_index=True,
    )
    bytes_total = models.BigIntegerField(default=0)
    bytes_transferred = models.BigIntegerField(default=0)
    chunk_size = models.PositiveIntegerField(default=0)
    checksum_expected = models.CharField(max_length=64, blank=True, default="")
    checksum_actual = models.CharField(max_length=64, blank=True, default="")
    error_message = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    retry_count = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]


class TransferHistory(models.Model):
    id = models.BigAutoField(primary_key=True)
    transfer = models.ForeignKey(WorkspaceTransfer, on_delete=models.CASCADE, related_name="history")
    event = models.CharField(max_length=64)
    detail = models.CharField(max_length=512, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]


class TransferPolicy(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    department = models.ForeignKey(
        "users.Department",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="workspace_transfer_policies",
    )
    workstation = models.ForeignKey(
        "remote_analysis.AnalysisWorkstation",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="workspace_transfer_policies",
    )
    max_file_size = models.BigIntegerField(null=True, blank=True)
    allowed_extensions = models.TextField(blank=True, default="")
    blocked_extensions = models.TextField(blank=True, default="")
    read_only_folders = models.JSONField(default=list, blank=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]


class WorkspaceQuota(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.OneToOneField(
        AnalysisWorkspace,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="quota",
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="workspace_quotas",
    )
    department = models.ForeignKey(
        "users.Department",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="workspace_quotas",
    )
    soft_limit_bytes = models.BigIntegerField(default=0)
    hard_limit_bytes = models.BigIntegerField(default=0)
    warning_percent = models.PositiveSmallIntegerField(default=80)
    override_allowed = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)


class WorkspaceArchive(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(AnalysisWorkspace, on_delete=models.CASCADE, related_name="archives")
    archive_key = models.CharField(max_length=255)
    size_bytes = models.BigIntegerField(default=0)
    sha256 = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    restored_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
    )
    note = models.CharField(max_length=512, blank=True, default="")

    class Meta:
        ordering = ["-created_at"]


class WorkspaceAudit(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(
        AnalysisWorkspace,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="audits",
    )
    action = models.CharField(max_length=64, db_index=True)
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


class VirusScanResult(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file = models.ForeignKey(WorkspaceFile, on_delete=models.CASCADE, related_name="scan_results")
    scanner = models.CharField(max_length=64, default="noop")
    status = models.CharField(max_length=32, choices=VirusStatus.choices, default=VirusStatus.PENDING)
    detail = models.CharField(max_length=512, blank=True, default="")
    scanned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-scanned_at"]


class WorkspaceShare(models.Model):
    """Internal sharing only — no public anonymous access."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    workspace = models.ForeignKey(AnalysisWorkspace, on_delete=models.CASCADE, related_name="shares")
    shared_with = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="workspace_shares_received",
    )
    department_only = models.BooleanField(default=False)
    read_only = models.BooleanField(default=True)
    token_hash = models.CharField(max_length=128, blank=True, default="")
    expires_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="workspace_shares_created",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]


class WorkspaceTelemetry(models.Model):
    id = models.BigAutoField(primary_key=True)
    metric_name = models.CharField(max_length=128, db_index=True)
    value = models.FloatField()
    unit = models.CharField(max_length=32, blank=True, default="")
    workspace = models.ForeignKey(
        AnalysisWorkspace,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="telemetry",
    )
    tags = models.JSONField(default=dict, blank=True)
    recorded_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-recorded_at"]
