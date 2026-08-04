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
            DepartmentSyncAgent.objects.select_related("department", "equipment")
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
                    # Phase 1 Configuration Push extensions (from templates / policy)
                    "network_mode": (profile.enabled_features or {}).get("network_mode", "dhcp"),
                    "windows_account_policy": (profile.enabled_features or {}).get(
                        "windows_account_policy", {}
                    ),
                    "folder_layout": (profile.enabled_features or {}).get("folder_layout", {}),
                    "firewall_profile": (profile.enabled_features or {}).get("firewall_profile", {}),
                    "retry_policy": (profile.enabled_features or {}).get("retry_policy", {}),
                    "required_software": (profile.enabled_features or {}).get(
                        "required_software", []
                    ),
                    "health_thresholds": (profile.enabled_features or {}).get(
                        "health_thresholds", {}
                    ),
                    "template_code": (profile.enabled_features or {}).get("template_code"),
                }
            )

        department = agent.department
        equipment = agent.equipment

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
            "equipment": {
                "equipment_id": equipment.equipment_id if equipment else None,
                "name": equipment.name if equipment else None,
                "code": equipment.code if equipment else None,
                "location": equipment.location if equipment else None,
            }
            if equipment
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

        # Phase 2: signed configuration pack (HMAC of canonical JSON subset)
        try:
            import hashlib
            import hmac
            import json

            from django.conf import settings

            secret = (
                getattr(settings, "DSA_BOOTSTRAP_SIGNING_KEY", None)
                or getattr(settings, "SECRET_KEY", "")
                or ""
            )
            body = json.dumps(
                {
                    "configuration_version": config_version,
                    "schema_version": schema_version,
                    "agent_uuid": str(agent.agent_uuid),
                    "assigned_equipment": equipment_payload,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            payload["configuration_signature"] = {
                "alg": "HMAC-SHA256",
                "value": hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest(),
            }
        except Exception:
            payload["configuration_signature"] = None

        agent.bootstrap_required = False
        # Do NOT set last_reported_* here — heartbeat after apply is source of truth (avoids false "in sync")
        agent.save(
            update_fields=[
                "bootstrap_required",
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
