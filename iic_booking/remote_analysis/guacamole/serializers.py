"""Serializers for remote desktop sessions (never expose secrets / IPs / Guacamole URLs)."""

from __future__ import annotations

from rest_framework import serializers

from iic_booking.remote_analysis.session_models import (
    ConnectionHistory,
    RemoteDesktopSession,
    SessionAudit,
    SessionHealth,
    SessionStatistics,
    SessionStateHistory,
)


class SessionStatisticsSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionStatistics
        fields = [
            "duration_seconds",
            "idle_seconds",
            "reconnect_count",
            "bytes_in",
            "bytes_out",
            "clipboard_events",
            "file_transfer_events",
            "error_count",
            "launch_latency_ms",
            "prepare_latency_ms",
            "updated_at",
        ]


class SessionHealthSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionHealth
        fields = [
            "guacamole_reachable",
            "agent_online",
            "workstation_healthy",
            "last_check_at",
            "detail",
            "score",
        ]


class SessionStateHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionStateHistory
        fields = ["from_status", "to_status", "reason", "created_at"]


class SessionAuditSerializer(serializers.ModelSerializer):
    class Meta:
        model = SessionAudit
        fields = ["action", "details", "success", "created_at"]


class ConnectionHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = ConnectionHistory
        fields = ["event", "detail", "created_at"]


class RemoteDesktopSessionSerializer(serializers.ModelSerializer):
    workstation_hostname = serializers.CharField(source="workstation.hostname", read_only=True)
    workstation_display_name = serializers.CharField(source="workstation.display_name", read_only=True)
    user_email = serializers.EmailField(source="user.email", read_only=True)
    reservation_id = serializers.UUIDField(source="reservation.id", read_only=True)
    statistics = SessionStatisticsSerializer(read_only=True)
    health = SessionHealthSerializer(read_only=True)
    # Explicitly omit: guacamole connection internals, IPs of workstation, credentials

    class Meta:
        model = RemoteDesktopSession
        fields = [
            "id",
            "reservation_id",
            "booking",
            "user_email",
            "workstation_hostname",
            "workstation_display_name",
            "status",
            "created_at",
            "launch_time",
            "connected_at",
            "disconnected_at",
            "expires_at",
            "idle_timeout_minutes",
            "termination_reason",
            "browser",
            "client_platform",
            "recording_enabled",
            "clipboard_enabled",
            "clipboard_policy",
            "file_transfer_enabled",
            "file_transfer_policy",
            "audio_enabled",
            "multi_monitor",
            "display_width",
            "display_height",
            "color_depth",
            "reconnect_count",
            "failure_detail",
            "statistics",
            "health",
            "updated_at",
        ]
        read_only_fields = fields


class CreateSessionSerializer(serializers.Serializer):
    reservation_id = serializers.UUIDField()
    wait_for_prepare = serializers.BooleanField(required=False, default=False)
    browser = serializers.CharField(required=False, allow_blank=True, default="")
    client_platform = serializers.CharField(required=False, allow_blank=True, default="")


class SessionActivitySerializer(serializers.Serializer):
    bytes_in = serializers.IntegerField(required=False, default=0, min_value=0)
    bytes_out = serializers.IntegerField(required=False, default=0, min_value=0)
