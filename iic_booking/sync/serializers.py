"""Serializers for Department Sync control-plane and data-plane APIs."""

from __future__ import annotations

from rest_framework import serializers


class EnrollRequestSerializer(serializers.Serializer):
    machine_name = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    agent_uuid = serializers.UUIDField()
    enrollment_secret = serializers.CharField(max_length=256, trim_whitespace=True)
    hostname = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    operating_system = serializers.CharField(max_length=200, required=False, allow_blank=True, default="")
    service_version = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
    sqlite_schema_version = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")
    portal_version = serializers.CharField(max_length=50, required=False, allow_blank=True, default="")


class HeartbeatRequestSerializer(serializers.Serializer):
    cpu_percent = serializers.FloatField(required=False, allow_null=True)
    memory_percent = serializers.FloatField(required=False, allow_null=True)
    disk_percent = serializers.FloatField(required=False, allow_null=True)
    active_workers = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    queue_size = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    configuration_version = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    schema_version = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    agent_uptime_seconds = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    hostname = serializers.CharField(required=False, allow_blank=True, default="", max_length=200)
    service_version = serializers.CharField(required=False, allow_blank=True, default="", max_length=50)
    windows_build = serializers.CharField(required=False, allow_blank=True, default="", max_length=100)
    sqlite_schema_version = serializers.CharField(required=False, allow_blank=True, default="", max_length=50)
    last_upload_at = serializers.DateTimeField(required=False, allow_null=True)
    status_message = serializers.CharField(required=False, allow_blank=True, default="", max_length=500)
    details = serializers.JSONField(required=False, default=dict)
    # Phase 2: Equipment PC rollup from DSA (persisted into AgentHeartbeat.details)
    equipment_pcs = serializers.JSONField(required=False, allow_null=True)


class BootstrapRequestSerializer(serializers.Serializer):
    """Bootstrap is authenticated; body is optional for future client hints."""

    client_bootstrap_schema_version = serializers.IntegerField(required=False, allow_null=True, min_value=1)


class WorkspaceCreateSerializer(serializers.Serializer):
    booking_id = serializers.IntegerField(min_value=1)


class CommandCompleteSerializer(serializers.Serializer):
    result_payload = serializers.JSONField(required=False, default=dict)


class CommandFailSerializer(serializers.Serializer):
    failure_reason = serializers.CharField(max_length=2000)
    error_details = serializers.JSONField(required=False, default=dict)
    retry_recommended = serializers.BooleanField(required=False, default=False)


class UploadStartSerializer(serializers.Serializer):
    agent_upload_id = serializers.UUIDField()
    file_name = serializers.CharField(max_length=500)
    relative_path = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")
    expected_size = serializers.IntegerField(required=False, default=0, min_value=0)
    equipment_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    booking_id = serializers.IntegerField(required=False, allow_null=True, min_value=1)
    workspace_id = serializers.UUIDField(required=False, allow_null=True)


class UploadChunkSerializer(serializers.Serializer):
    upload_id = serializers.UUIDField()
    resume_token = serializers.CharField(max_length=128)
    chunk_index = serializers.IntegerField(min_value=0)
    total_chunks = serializers.IntegerField(required=False, default=0, min_value=0)


class UploadCompleteSerializer(serializers.Serializer):
    upload_id = serializers.UUIDField()
    resume_token = serializers.CharField(max_length=128)
    expected_size = serializers.IntegerField(required=False, allow_null=True, min_value=0)
    chunk_count = serializers.IntegerField(required=False, allow_null=True, min_value=0)


class ResultImportSerializer(serializers.Serializer):
    agent_upload_id = serializers.UUIDField()
    booking_id = serializers.IntegerField(min_value=1)
    equipment_id = serializers.IntegerField(min_value=1)
    upload_session_id = serializers.UUIDField(required=False, allow_null=True)
    file_name = serializers.CharField(max_length=500, required=False, allow_blank=True, default="")
    relative_path = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")
    content_type = serializers.CharField(max_length=128, required=False, allow_blank=True, default="")
    size_bytes = serializers.IntegerField(required=False, default=0, min_value=0)
    sha256 = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    storage_path = serializers.CharField(max_length=1000, required=False, allow_blank=True, default="")
    parser_used = serializers.CharField(max_length=64, required=False, allow_blank=True, default="")
    metadata = serializers.JSONField(required=False, default=dict)
    measurements = serializers.ListField(
        child=serializers.DictField(),
        required=False,
        default=list,
    )


class ResultFinalizeSerializer(serializers.Serializer):
    agent_upload_id = serializers.UUIDField()
    booking_id = serializers.IntegerField(min_value=1)
    processing_duration_ms = serializers.IntegerField(required=False, allow_null=True, min_value=0)


class DeviceIdentityRegisterSerializer(serializers.Serializer):
    device_id = serializers.UUIDField(required=False)
    public_key = serializers.CharField(required=False, allow_blank=True, default="")
    device_public_key = serializers.CharField(required=False, allow_blank=True, default="")
    certificate_thumbprint = serializers.CharField(required=False, allow_blank=True, default="", max_length=128)
    thumbprint = serializers.CharField(required=False, allow_blank=True, default="", max_length=128)
    signing_secret = serializers.CharField(required=False, allow_blank=True, default="")
    security_version = serializers.IntegerField(required=False, min_value=1, default=1)


class CertificateIssueSerializer(serializers.Serializer):
    public_key = serializers.CharField(required=False, allow_blank=True, default="")
    validity_days = serializers.IntegerField(required=False, min_value=30, default=365)
    renew = serializers.BooleanField(required=False, default=False)


class ApiKeyRotateSerializer(serializers.Serializer):
    lifetime_days = serializers.IntegerField(required=False, min_value=1, default=90)
    grace_days = serializers.IntegerField(required=False, min_value=0, default=7)


class RecoveryReconcileSerializer(serializers.Serializer):
    offline_duration_seconds = serializers.IntegerField(required=False, min_value=0, default=0)
    pending_uploads = serializers.IntegerField(required=False, min_value=0, default=0)
    pending_processing = serializers.IntegerField(required=False, min_value=0, default=0)
    queue_repairs = serializers.IntegerField(required=False, min_value=0, default=0)
    conflicts = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    device_id = serializers.UUIDField(required=False)


class RecoveryEventSerializer(serializers.Serializer):
    event_code = serializers.CharField(max_length=32)
    message = serializers.CharField(max_length=500)
    component = serializers.CharField(required=False, allow_blank=True, default="", max_length=64)
    from_state = serializers.CharField(required=False, allow_blank=True, default="", max_length=32)
    to_state = serializers.CharField(required=False, allow_blank=True, default="", max_length=32)
    device_id = serializers.UUIDField(required=False)
    details = serializers.JSONField(required=False, default=dict)


class IntegrityReportSerializer(serializers.Serializer):
    ok = serializers.BooleanField(default=True)
    pragma_result = serializers.CharField(required=False, allow_blank=True, default="")
    failures = serializers.ListField(child=serializers.CharField(), required=False, default=list)
    database_size_bytes = serializers.IntegerField(required=False, allow_null=True, min_value=0)


class ConflictResolveSerializer(serializers.Serializer):
    conflict_type = serializers.CharField(max_length=64)
    resolution = serializers.CharField(required=False, allow_blank=True, default="", max_length=64)
    upload_id = serializers.UUIDField(required=False)
    processing_id = serializers.UUIDField(required=False)
    details = serializers.JSONField(required=False, default=dict)


class EnterpriseAssignSerializer(serializers.Serializer):
    agent_id = serializers.UUIDField()
    assignment_type = serializers.ChoiceField(
        choices=["AUTOMATIC", "MANUAL", "PRIORITY", "BUILDING", "EQUIPMENT", "DEPARTMENT"],
        default="MANUAL",
    )
    building_id = serializers.UUIDField(required=False)
    laboratory_id = serializers.UUIDField(required=False)
    equipment_id = serializers.IntegerField(required=False, min_value=1)
    group_id = serializers.UUIDField(required=False)
    priority = serializers.IntegerField(required=False, min_value=1, default=100)
    notes = serializers.CharField(required=False, allow_blank=True, default="")
    make_primary = serializers.BooleanField(required=False, default=True)


class EnterpriseLifecycleSerializer(serializers.Serializer):
    agent_id = serializers.UUIDField()
    reason = serializers.CharField(required=False, allow_blank=True, default="", max_length=500)


class EnterpriseCapabilitySerializer(serializers.Serializer):
    supported_plugins = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    plugin_inventory = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    plugin_versions = serializers.DictField(required=False, default=dict)
    storage_free_bytes = serializers.IntegerField(required=False, allow_null=True)
    storage_total_bytes = serializers.IntegerField(required=False, allow_null=True)
    cpu_percent = serializers.FloatField(required=False, allow_null=True)
    memory_percent = serializers.FloatField(required=False, allow_null=True)
    network_summary = serializers.CharField(required=False, allow_blank=True, default="")
    windows_version = serializers.CharField(required=False, allow_blank=True, default="")
    windows_build = serializers.CharField(required=False, allow_blank=True, default="")
    schema_version = serializers.IntegerField(required=False, allow_null=True)
    recovery_version = serializers.IntegerField(required=False, allow_null=True)
    security_version = serializers.IntegerField(required=False, allow_null=True)
    processing_capacity = serializers.IntegerField(required=False, allow_null=True)
    max_parallel_uploads = serializers.IntegerField(required=False, allow_null=True)
    max_parallel_processing = serializers.IntegerField(required=False, allow_null=True)
    capabilities = serializers.DictField(required=False, default=dict)
    custom_tags = serializers.ListField(child=serializers.CharField(), required=False)


class MonitoringTelemetrySerializer(serializers.Serializer):
    correlation_id = serializers.UUIDField(required=False, allow_null=True)
    reported_at = serializers.DateTimeField(required=False, allow_null=True)
    include_capacity = serializers.BooleanField(required=False, default=True)
    health = serializers.DictField(required=False, default=dict)
    performance = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    capacity = serializers.DictField(required=False, default=dict)
    metrics = serializers.DictField(required=False, default=dict)


class AlertResolveSerializer(serializers.Serializer):
    resolution = serializers.CharField(required=False, allow_blank=True, default="", max_length=500)


class ReleaseCreateSerializer(serializers.Serializer):
    package_type = serializers.CharField(required=False, default="AGENT")
    channel = serializers.CharField(required=False, default="PRODUCTION")
    version = serializers.CharField(max_length=64)
    display_name = serializers.CharField(required=False, allow_blank=True, default="")
    description = serializers.CharField(required=False, allow_blank=True, default="")
    download_url = serializers.CharField(required=False, allow_blank=True, default="", max_length=1000)
    package_size_bytes = serializers.IntegerField(required=False, default=0)
    sha256 = serializers.CharField(required=False, allow_blank=True, default="")
    signature = serializers.CharField(required=False, allow_blank=True, default="")
    publisher = serializers.CharField(required=False, allow_blank=True, default="IIC Portal")
    min_agent_version = serializers.CharField(required=False, allow_blank=True, default="")
    min_schema_version = serializers.IntegerField(required=False, allow_null=True)
    security_version = serializers.IntegerField(required=False, allow_null=True)
    recovery_version = serializers.IntegerField(required=False, allow_null=True)
    api_version = serializers.CharField(required=False, allow_blank=True, default="")
    compatibility = serializers.DictField(required=False, default=dict)
    dependencies = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    plugin_id = serializers.CharField(required=False, allow_blank=True, default="")
    plugin_name = serializers.CharField(required=False, allow_blank=True, default="")
    plugin_version = serializers.CharField(required=False, allow_blank=True, default="")
    supports_hot_reload = serializers.BooleanField(required=False, default=True)
    requires_agent_restart = serializers.BooleanField(required=False, default=False)
    department_id = serializers.UUIDField(required=False, allow_null=True)
    content = serializers.DictField(required=False)


class ReleasePublishSerializer(serializers.Serializer):
    package_id = serializers.UUIDField()


class ReleaseDeploySerializer(serializers.Serializer):
    package_id = serializers.UUIDField()
    strategy = serializers.CharField(required=False, default="MANUAL")
    channel = serializers.CharField(required=False, allow_blank=True, default="")
    percentage = serializers.IntegerField(required=False, default=100)
    department_id = serializers.UUIDField(required=False, allow_null=True)
    building_id = serializers.UUIDField(required=False, allow_null=True)
    agent_group_id = serializers.UUIDField(required=False, allow_null=True)
    scheduled_at = serializers.DateTimeField(required=False, allow_null=True)
    maintenance_window_start = serializers.DateTimeField(required=False, allow_null=True)
    maintenance_window_end = serializers.DateTimeField(required=False, allow_null=True)
    requires_approval = serializers.BooleanField(required=False, default=False)
    target_agent_ids = serializers.ListField(child=serializers.UUIDField(), required=False, default=list)
    start_immediately = serializers.BooleanField(required=False, default=True)


class ReleaseRollbackSerializer(serializers.Serializer):
    package_id = serializers.UUIDField(required=False, allow_null=True)
    agent_id = serializers.UUIDField(required=False, allow_null=True)
    to_version = serializers.CharField(required=False, allow_blank=True, default="")
    reason = serializers.CharField(required=False, allow_blank=True, default="", max_length=500)


class UpdateStatusReportSerializer(serializers.Serializer):
    correlation_id = serializers.UUIDField(required=False, allow_null=True)
    package_id = serializers.UUIDField(required=False, allow_null=True)
    deployment_id = serializers.UUIDField(required=False, allow_null=True)
    history_id = serializers.UUIDField(required=False, allow_null=True)
    state = serializers.CharField()
    from_version = serializers.CharField(required=False, allow_blank=True, default="")
    to_version = serializers.CharField(required=False, allow_blank=True, default="")
    package_type = serializers.CharField(required=False, allow_blank=True, default="")
    message = serializers.CharField(required=False, allow_blank=True, default="", max_length=500)
    download_bytes = serializers.IntegerField(required=False, default=0)
    download_ms = serializers.IntegerField(required=False, allow_null=True)
    install_ms = serializers.IntegerField(required=False, allow_null=True)
    validation_ms = serializers.IntegerField(required=False, allow_null=True)
    auto_rollback = serializers.BooleanField(required=False, default=False)
    rollback_reason = serializers.CharField(required=False, allow_blank=True, default="")
    details = serializers.DictField(required=False, default=dict)
    current_version = serializers.CharField(required=False, allow_blank=True, default="")


class ExperimentReportSerializer(serializers.Serializer):
    experiment_id = serializers.UUIDField(required=False, allow_null=True)
    correlation_id = serializers.UUIDField(required=False, allow_null=True)
    equipment_id = serializers.UUIDField(required=False, allow_null=True)
    booking_id = serializers.CharField(required=False, allow_blank=True, default="")
    portal_booking_id = serializers.CharField(required=False, allow_blank=True, default="")
    workspace_path = serializers.CharField(required=False, allow_blank=True, default="")
    operator_name = serializers.CharField(required=False, allow_blank=True, default="")
    plugin_id = serializers.CharField()
    plugin_version = serializers.CharField(required=False, allow_blank=True, default="")
    status = serializers.CharField()
    current_step = serializers.CharField(required=False, allow_blank=True, default="")
    session_start = serializers.DateTimeField(required=False, allow_null=True)
    session_end = serializers.DateTimeField(required=False, allow_null=True)
    metadata = serializers.DictField(required=False, default=dict)
    execution_history = serializers.ListField(child=serializers.DictField(), required=False, default=list)
    last_error = serializers.CharField(required=False, allow_blank=True, default="")
    duration_ms = serializers.IntegerField(required=False, allow_null=True)


class ExperimentTelemetrySerializer(serializers.Serializer):
    reported_at = serializers.DateTimeField(required=False, allow_null=True)
    experiments_completed = serializers.IntegerField(required=False, default=0)
    experiments_failed = serializers.IntegerField(required=False, default=0)
    recovery_count = serializers.IntegerField(required=False, default=0)
    total_duration_ms = serializers.FloatField(required=False, default=0)
    total_plugin_execution_ms = serializers.FloatField(required=False, default=0)
    instrument_availability = serializers.DictField(required=False, default=dict)
    plugin_versions = serializers.DictField(required=False, default=dict)
    details = serializers.DictField(required=False, default=dict)
