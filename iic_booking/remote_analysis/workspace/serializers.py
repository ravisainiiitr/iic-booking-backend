"""Serializers for Analysis Workspace APIs (never expose storage paths)."""

from __future__ import annotations

from rest_framework import serializers

from iic_booking.remote_analysis.workspace_models import (
    AnalysisWorkspace,
    WorkspaceAudit,
    WorkspaceFile,
    WorkspaceFolder,
    WorkspaceTransfer,
    WorkspaceVersion,
)


class WorkspaceFolderSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkspaceFolder
        fields = ["id", "name", "relative_path", "read_only", "category", "created_at"]


class WorkspaceFileSerializer(serializers.ModelSerializer):
    folder_name = serializers.CharField(source="folder.name", read_only=True, allow_null=True)
    uploaded_by_email = serializers.EmailField(source="uploaded_by.email", read_only=True, allow_null=True)

    class Meta:
        model = WorkspaceFile
        fields = [
            "id",
            "folder_name",
            "original_name",
            "relative_path",
            "size",
            "sha256",
            "mime_type",
            "category",
            "uploaded_by_email",
            "uploaded_at",
            "modified_at",
            "version",
            "is_current",
            "virus_status",
            "download_count",
            "locked",
            "deleted",
            "source",
        ]
        # storage_relpath / stored_name intentionally omitted


class WorkspaceVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkspaceVersion
        fields = ["id", "version", "size", "sha256", "created_at", "note"]


class WorkspaceTransferSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkspaceTransfer
        fields = [
            "id",
            "direction",
            "status",
            "bytes_total",
            "bytes_transferred",
            "retry_count",
            "error_message",
            "started_at",
            "completed_at",
            "created_at",
        ]


class WorkspaceAuditSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkspaceAudit
        fields = ["action", "details", "success", "created_at"]


class AnalysisWorkspaceSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source="user.email", read_only=True)
    workstation_hostname = serializers.CharField(source="workstation.hostname", read_only=True, allow_null=True)
    reservation_id = serializers.UUIDField(source="reservation.id", read_only=True)
    folders = WorkspaceFolderSerializer(many=True, read_only=True)
    quota_usage_percent = serializers.SerializerMethodField()
    current_usage_gb = serializers.FloatField(read_only=True)

    class Meta:
        model = AnalysisWorkspace
        fields = [
            "id",
            "reservation_id",
            "booking",
            "user_email",
            "workstation_hostname",
            "status",
            "sync_phase",
            "sync_progress_percent",
            "sync_message",
            "created_at",
            "activated_at",
            "archived_at",
            "retention_until",
            "quota_gb",
            "current_usage_bytes",
            "current_usage_gb",
            "quota_usage_percent",
            "read_only",
            "archive_status",
            "last_synced_at",
            "folders",
            "updated_at",
        ]
        # storage_key / local_agent_path omitted from public serializer

    def get_quota_usage_percent(self, obj) -> float:
        hard = obj.quota_gb * (1024**3)
        if hard <= 0:
            return 0.0
        return round(100.0 * obj.current_usage_bytes / hard, 2)


class CreateWorkspaceSerializer(serializers.Serializer):
    reservation_id = serializers.UUIDField()


class UploadMetaSerializer(serializers.Serializer):
    folder = serializers.CharField(required=False, default="RawData")
    sha256 = serializers.CharField(required=False, allow_blank=True, default="")
