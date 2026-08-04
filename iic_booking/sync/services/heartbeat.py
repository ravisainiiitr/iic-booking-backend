"""Heartbeat service — runtime telemetry + operational commands only."""

from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction
from django.utils import timezone

from iic_booking.sync.exceptions import (
    DisabledAgentError,
    InvalidConfigurationVersionError,
    InvalidSchemaVersionError,
    RevokedAgentError,
)
from iic_booking.sync.models import (
    AgentHeartbeat,
    AgentLifecycleStatus,
    DepartmentSyncAgent,
    SyncLogCategory,
    SyncLogSeverity,
)
from iic_booking.sync.services.logging import (
    EVENT_BOOTSTRAP_REQUIRED,
    EVENT_HEARTBEAT_RECEIVED,
    write_sync_log,
)
from iic_booking.sync.services.tokens import agent_expected_versions


class HeartbeatCommand:
    CONTINUE = "continue"
    BOOTSTRAP_REQUIRED = "bootstrap_required"
    RESTART_REQUIRED = "restart_required"
    UPGRADE_REQUIRED = "upgrade_required"
    DISABLE_AGENT = "disable_agent"


class HeartbeatService:
    @transaction.atomic
    def process(
        self,
        agent: DepartmentSyncAgent,
        payload: dict[str, Any],
        *,
        correlation_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        if agent.status == AgentLifecycleStatus.REVOKED:
            raise RevokedAgentError()
        if agent.status == AgentLifecycleStatus.DISABLED or not agent.is_active:
            write_sync_log(
                event_code=EVENT_HEARTBEAT_RECEIVED,
                message="Heartbeat from disabled agent",
                category=SyncLogCategory.HEARTBEAT,
                severity=SyncLogSeverity.WARNING,
                sync_agent=agent,
                correlation_id=correlation_id,
            )
            return {"status": "ok", "command": HeartbeatCommand.DISABLE_AGENT}

        reported_config = payload.get("configuration_version")
        reported_schema = payload.get("schema_version")
        if reported_config is not None:
            try:
                reported_config = int(reported_config)
                if reported_config < 1:
                    raise InvalidConfigurationVersionError()
            except (TypeError, ValueError) as exc:
                raise InvalidConfigurationVersionError() from exc
        if reported_schema is not None:
            try:
                reported_schema = int(reported_schema)
                if reported_schema < 1:
                    raise InvalidSchemaVersionError()
            except (TypeError, ValueError) as exc:
                raise InvalidSchemaVersionError() from exc

        now = timezone.now()
        details = dict(payload.get("details") or {})
        equipment_pcs = payload.get("equipment_pcs")
        if equipment_pcs is not None:
            details["equipment_pcs"] = equipment_pcs

        AgentHeartbeat.objects.create(
            sync_agent=agent,
            reported_at=now,
            cpu_percent=payload.get("cpu_percent"),
            memory_percent=payload.get("memory_percent"),
            disk_percent=payload.get("disk_percent"),
            queue_size=payload.get("queue_size"),
            active_workers=payload.get("active_workers"),
            last_upload_at=payload.get("last_upload_at"),
            agent_uptime_seconds=payload.get("agent_uptime_seconds"),
            service_version=(payload.get("service_version") or "")[:50],
            sqlite_schema_version=(payload.get("sqlite_schema_version") or "")[:50],
            windows_build=(payload.get("windows_build") or "")[:100],
            hostname=(payload.get("hostname") or "")[:200],
            reported_configuration_version=reported_config,
            reported_schema_version=reported_schema,
            status_message=(payload.get("status_message") or "")[:500],
            details=details,
        )

        agent.last_heartbeat_at = now
        agent.last_seen_at = now
        if payload.get("service_version"):
            agent.version = str(payload["service_version"])[:50]
        if reported_config is not None:
            agent.last_reported_configuration_version = reported_config
        if reported_schema is not None:
            agent.last_reported_schema_version = reported_schema
        agent.save(
            update_fields=[
                "last_heartbeat_at",
                "last_seen_at",
                "version",
                "last_reported_configuration_version",
                "last_reported_schema_version",
                "updated_at",
            ]
        )

        write_sync_log(
            event_code=EVENT_HEARTBEAT_RECEIVED,
            message="Heartbeat received",
            category=SyncLogCategory.HEARTBEAT,
            severity=SyncLogSeverity.INFO,
            sync_agent=agent,
            correlation_id=correlation_id,
            json_payload={
                "configuration_version": reported_config,
                "schema_version": reported_schema,
                "hostname": payload.get("hostname"),
            },
        )

        command = self._resolve_command(agent, reported_config, reported_schema)
        if command == HeartbeatCommand.BOOTSTRAP_REQUIRED:
            write_sync_log(
                event_code=EVENT_BOOTSTRAP_REQUIRED,
                message="Heartbeat instructed bootstrap_required",
                category=SyncLogCategory.BOOTSTRAP,
                severity=SyncLogSeverity.INFO,
                sync_agent=agent,
                correlation_id=correlation_id,
            )

        return {"status": "ok", "command": command}

    def _resolve_command(
        self,
        agent: DepartmentSyncAgent,
        reported_config: int | None,
        reported_schema: int | None,
    ) -> str:
        if agent.upgrade_required:
            return HeartbeatCommand.UPGRADE_REQUIRED
        if agent.restart_required:
            return HeartbeatCommand.RESTART_REQUIRED
        if agent.bootstrap_required:
            return HeartbeatCommand.BOOTSTRAP_REQUIRED

        expected_config, expected_schema = agent_expected_versions(agent)
        if reported_config is None or reported_schema is None:
            return HeartbeatCommand.BOOTSTRAP_REQUIRED
        if reported_config != expected_config or reported_schema != expected_schema:
            return HeartbeatCommand.BOOTSTRAP_REQUIRED
        return HeartbeatCommand.CONTINUE
