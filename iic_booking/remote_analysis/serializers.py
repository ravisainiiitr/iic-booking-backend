"""DRF serializers for Remote Analysis."""

from __future__ import annotations

from rest_framework import serializers

from iic_booking.remote_analysis.models import (
    AnalysisWorkstation,
    InstalledSoftware,
    RemoteCommand,
    TelemetrySnapshot,
    WorkstationCapability,
    WorkstationEvent,
    WorkstationHeartbeat,
    WorkstationStateHistory,
)


class WorkstationCapabilitySerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkstationCapability
        fields = [
            "supports_rdp",
            "supports_clipboard",
            "supports_file_transfer",
            "supports_audio",
            "supports_multi_monitor",
            "maximum_resolution",
            "gpu_available",
            "ram_gb",
            "cpu_cores",
            "disk_space_gb",
            "network_speed_mbps",
            "updated_at",
        ]


class AnalysisWorkstationSerializer(serializers.ModelSerializer):
    capabilities = WorkstationCapabilitySerializer(read_only=True)
    department_id = serializers.IntegerField(source="department.id", read_only=True, allow_null=True)

    class Meta:
        model = AnalysisWorkstation
        fields = [
            "id",
            "agent_id",
            "hostname",
            "display_name",
            "department_id",
            "department_name",
            "building",
            "room",
            "description",
            "operating_system",
            "windows_version",
            "cpu",
            "cpu_cores",
            "memory_gb",
            "storage_gb",
            "gpu",
            # ip_address intentionally omitted — never expose workstation IP to portal users
            "mac_address",
            "agent_version",
            "schema_version",
            "registration_date",
            "last_heartbeat",
            "status",
            "enabled",
            "supports_rdp",
            "supports_clipboard",
            "supports_file_transfer",
            "supports_audio",
            "supports_multi_monitor",
            "current_command",
            "health_score",
            "last_inventory_update",
            "capabilities",
            "created_at",
            "updated_at",
        ]


class WorkstationHeartbeatSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkstationHeartbeat
        fields = [
            "id",
            "received_at",
            "cpu",
            "memory",
            "disk",
            "gpu",
            "windows_uptime_hours",
            "idle",
            "idle_time_minutes",
            "logged_in_user",
            "running_software",
            "running_processes",
            "software_count",
            "portal_latency_ms",
            "current_state",
            "network",
            "online",
            "antivirus_status",
            "windows_updates_pending",
        ]


class InstalledSoftwareSerializer(serializers.ModelSerializer):
    workstation_hostname = serializers.CharField(source="workstation.hostname", read_only=True)

    class Meta:
        model = InstalledSoftware
        fields = [
            "id",
            "workstation",
            "workstation_hostname",
            "software_name",
            "publisher",
            "version",
            "executable",
            "install_path",
            "install_date",
            "licensed",
            "license_type",
            "category",
            "is_present",
            "last_updated",
            "first_seen_at",
        ]


class RemoteCommandSerializer(serializers.ModelSerializer):
    workstation_hostname = serializers.CharField(source="workstation.hostname", read_only=True)

    class Meta:
        model = RemoteCommand
        fields = [
            "id",
            "workstation",
            "workstation_hostname",
            "command_type",
            "status",
            "payload",
            "created_at",
            "delivered_at",
            "started_at",
            "completed_at",
            "expires_at",
            "result_message",
            "error_message",
        ]


class CreateCommandSerializer(serializers.Serializer):
    command_type = serializers.CharField(max_length=64)
    payload = serializers.DictField(required=False)


class WorkstationEventSerializer(serializers.ModelSerializer):
    workstation_hostname = serializers.CharField(source="workstation.hostname", read_only=True, allow_null=True)

    class Meta:
        model = WorkstationEvent
        fields = [
            "id",
            "workstation",
            "workstation_hostname",
            "category",
            "action",
            "details",
            "success",
            "correlation_id",
            "created_at",
        ]


class WorkstationStateHistorySerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkstationStateHistory
        fields = ["id", "from_status", "to_status", "reason", "created_at"]


class TelemetrySnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = TelemetrySnapshot
        fields = ["id", "metric_name", "value", "unit", "recorded_at", "tags"]


class AgentCommandPollSerializer(serializers.Serializer):
    id = serializers.UUIDField()
    type = serializers.CharField()
    payloadJson = serializers.CharField(allow_null=True, required=False)


# --- Milestone 3: Reservations ---

from iic_booking.remote_analysis.scheduler_models import (  # noqa: E402
    AnalysisReservation,
    MaintenanceWindow,
    ReservationQueue,
)


class AnalysisReservationSerializer(serializers.ModelSerializer):
    workstation_hostname = serializers.CharField(source="workstation.hostname", read_only=True, allow_null=True)
    user_email = serializers.CharField(source="user.email", read_only=True)
    booking_id = serializers.IntegerField(source="booking.booking_id", read_only=True, allow_null=True)
    department_name = serializers.CharField(source="department.name", read_only=True, allow_null=True)

    class Meta:
        model = AnalysisReservation
        fields = [
            "id",
            "booking",
            "booking_id",
            "user",
            "user_email",
            "department",
            "department_name",
            "workstation",
            "workstation_hostname",
            "status",
            "requested_start",
            "requested_end",
            "reserved_start",
            "reserved_end",
            "allocated_at",
            "released_at",
            "priority",
            "software_profile",
            "requested_capabilities",
            "requested_resources",
            "allocation_score",
            "allocation_notes",
            "created_by",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "status",
            "reserved_start",
            "reserved_end",
            "allocated_at",
            "released_at",
            "allocation_score",
            "allocation_notes",
            "created_at",
            "updated_at",
        ]


class CreateReservationSerializer(serializers.Serializer):
    booking_id = serializers.IntegerField(required=False, allow_null=True)
    user_id = serializers.IntegerField(required=False, allow_null=True)
    department_id = serializers.IntegerField(required=False, allow_null=True)
    requested_start = serializers.DateTimeField(required=False)
    requested_end = serializers.DateTimeField(required=False)
    priority = serializers.IntegerField(required=False, default=100)
    software_profile_id = serializers.UUIDField(required=False, allow_null=True)
    requested_capabilities = serializers.DictField(required=False)
    requested_resources = serializers.DictField(required=False)
    auto_allocate = serializers.BooleanField(required=False, default=True)


class ExtendReservationSerializer(serializers.Serializer):
    new_end = serializers.DateTimeField()


class ReservationQueueSerializer(serializers.ModelSerializer):
    reservation_id = serializers.UUIDField(source="reservation.id", read_only=True)
    reservation_status = serializers.CharField(source="reservation.status", read_only=True)
    user_email = serializers.CharField(source="reservation.user.email", read_only=True)
    requested_start = serializers.DateTimeField(source="reservation.requested_start", read_only=True)

    class Meta:
        model = ReservationQueue
        fields = [
            "id",
            "reservation_id",
            "reservation_status",
            "user_email",
            "requested_start",
            "status",
            "priority",
            "enqueued_at",
            "dequeued_at",
            "position_hint",
        ]


class MaintenanceWindowSerializer(serializers.ModelSerializer):
    workstation_hostname = serializers.CharField(source="workstation.hostname", read_only=True, allow_null=True)

    class Meta:
        model = MaintenanceWindow
        fields = [
            "id",
            "workstation",
            "workstation_hostname",
            "start",
            "end",
            "reason",
            "created_by",
            "active",
            "created_at",
        ]
