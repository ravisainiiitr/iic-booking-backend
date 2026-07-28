"""Bootstrap service — single source of truth for agent operational config."""

from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone

from iic_booking.sync.constants import (
    bootstrap_schema_version,
    heartbeat_interval_seconds,
)
from iic_booking.sync.exceptions import DisabledAgentError, RevokedAgentError
from iic_booking.sync.models import (
    AgentAssignment,
    AgentLifecycleStatus,
    DepartmentSyncAgent,
    SyncLogCategory,
    SyncLogSeverity,
)
from iic_booking.sync.services.logging import EVENT_BOOTSTRAP_GENERATED, write_sync_log
from iic_booking.sync.services.tokens import agent_expected_versions


class BootstrapService:
    @transaction.atomic
    def build(
        self,
        agent: DepartmentSyncAgent,
        *,
        correlation_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        if agent.status == AgentLifecycleStatus.REVOKED:
            raise RevokedAgentError()
        if agent.status == AgentLifecycleStatus.DISABLED or not agent.is_active:
            raise DisabledAgentError()

        agent = (
            DepartmentSyncAgent.objects.select_related("department", "laboratory")
            .prefetch_related(
                Prefetch(
                    "assignments",
                    queryset=AgentAssignment.objects.filter(is_active=True)
                    .select_related(
                        "sync_profile",
                        "sync_profile__equipment",
                        "sync_profile__equipment__internal_department",
                    )
                    .order_by("assigned_at"),
                )
            )
            .get(pk=agent.pk)
        )

        config_version, schema_version = agent_expected_versions(agent)
        portal_schema = bootstrap_schema_version()

        equipment_payload = []
        for assignment in agent.assignments.all():
            profile = assignment.sync_profile
            equipment = profile.equipment
            # Security: never expose smb_credential_reference or secrets.
            equipment_payload.append(
                {
                    "equipment_code": equipment.code,
                    "equipment_name": equipment.name,
                    "department_code": getattr(
                        getattr(equipment, "internal_department", None), "code", None
                    ),
                    "watch_folder": profile.watch_folder,
                    "share_name": profile.share_name,
                    "hostname": profile.hostname,
                    "ip_address": profile.ip_address,
                    "unc_path": profile.unc_path,
                    "sync_enabled": profile.sync_enabled,
                    "watch_enabled": profile.watch_enabled,
                    "upload_enabled": profile.upload_enabled,
                    "enabled_features": profile.enabled_features or {},
                    "sync_interval_seconds": profile.sync_interval_seconds,
                    "configuration_version": profile.configuration_version,
                    "schema_version": profile.schema_version,
                }
            )

        department = agent.department
        laboratory = agent.laboratory

        payload = {
            "bootstrap_schema_version": portal_schema,
            "configuration_version": config_version,
            "schema_version": schema_version,
            "server_time": timezone.now().isoformat(),
            "portal": {
                "name": "IIC Equipment Booking Portal",
                "api_namespace": "/api/v1/sync/",
            },
            "department": {
                "name": department.name if department else None,
                "code": department.code if department else None,
            },
            "laboratory": {
                "name": laboratory.name if laboratory else None,
                "code": laboratory.code if laboratory else None,
                "location": laboratory.location if laboratory else None,
            }
            if laboratory
            else None,
            "agent": {
                "agent_uuid": str(agent.agent_uuid),
                "agent_name": agent.agent_name,
                "machine_name": agent.machine_name,
                "lifecycle_status": agent.status,
                "heartbeat_interval_seconds": heartbeat_interval_seconds(),
            },
            "assigned_equipment": equipment_payload,
            "feature_flags": {
                "control_plane_only": False,
                "smb_watch_enabled": False,
                "upload_enabled": False,
                "booking_sync_enabled": True,
                "equipment_sync_enabled": True,
                "agent_commands_enabled": True,
            },
        }

        agent.bootstrap_required = False
        agent.last_reported_configuration_version = config_version
        agent.last_reported_schema_version = schema_version
        agent.save(
            update_fields=[
                "bootstrap_required",
                "last_reported_configuration_version",
                "last_reported_schema_version",
                "updated_at",
            ]
        )

        write_sync_log(
            event_code=EVENT_BOOTSTRAP_GENERATED,
            message="Bootstrap generated",
            category=SyncLogCategory.BOOTSTRAP,
            severity=SyncLogSeverity.INFO,
            sync_agent=agent,
            correlation_id=correlation_id,
            json_payload={
                "configuration_version": config_version,
                "schema_version": schema_version,
                "equipment_count": len(equipment_payload),
            },
        )
        return payload
