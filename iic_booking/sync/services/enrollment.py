"""Agent enrollment service."""

from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction
from django.utils import timezone

from iic_booking.sync.constants import heartbeat_interval_seconds
from iic_booking.sync.exceptions import EnrollmentFailedError
from iic_booking.sync.models import AgentLifecycleStatus, DepartmentSyncAgent, SyncLogCategory, SyncLogSeverity
from iic_booking.sync.services.logging import (
    EVENT_AGENT_ENROLLED,
    EVENT_ENROLLMENT_FAILED,
    EVENT_ENROLLMENT_STARTED,
    write_sync_log,
)
from iic_booking.sync.services.tokens import (
    agent_expected_versions,
    issue_access_token,
    verify_hash,
)

# Uniform client-facing failure (do not leak UUID existence / lifecycle / secret validity).
_UNIFORM_ENROLL_FAILURE = EnrollmentFailedError(
    "Enrollment failed.",
    code="ENROLLMENT_FAILED",
)


class EnrollmentService:
    """First-time (or re-)enrollment of a pre-provisioned Department Sync Agent."""

    @transaction.atomic
    def enroll(self, payload: dict[str, Any], *, correlation_id: uuid.UUID | None = None) -> dict[str, Any]:
        agent_uuid = payload.get("agent_uuid")
        enrollment_secret = (payload.get("enrollment_secret") or "").strip()
        machine_name = (payload.get("machine_name") or "").strip()
        hostname = (payload.get("hostname") or "").strip()
        operating_system = (payload.get("operating_system") or "").strip()
        service_version = (payload.get("service_version") or "").strip()
        sqlite_schema_version = (payload.get("sqlite_schema_version") or "").strip()
        portal_version = (payload.get("portal_version") or "").strip()

        write_sync_log(
            event_code=EVENT_ENROLLMENT_STARTED,
            message="Enrollment Started",
            category=SyncLogCategory.AUTH,
            severity=SyncLogSeverity.INFO,
            correlation_id=correlation_id,
            json_payload={"agent_uuid": str(agent_uuid) if agent_uuid else None},
        )

        # Lock the agent row to prevent concurrent enrollment with the same secret.
        agent = (
            DepartmentSyncAgent.objects.select_related("department", "equipment")
            .select_for_update()
            .filter(agent_uuid=agent_uuid)
            .first()
        )
        if agent is None:
            write_sync_log(
                event_code=EVENT_ENROLLMENT_FAILED,
                message="Enrollment Failed: unknown agent",
                category=SyncLogCategory.AUTH,
                severity=SyncLogSeverity.WARNING,
                correlation_id=correlation_id,
            )
            raise _UNIFORM_ENROLL_FAILURE

        if agent.status in {AgentLifecycleStatus.DISABLED, AgentLifecycleStatus.REVOKED}:
            write_sync_log(
                event_code=EVENT_ENROLLMENT_FAILED,
                message=f"Enrollment Failed: lifecycle={agent.status}",
                category=SyncLogCategory.AUTH,
                severity=SyncLogSeverity.WARNING,
                sync_agent=agent,
                correlation_id=correlation_id,
                json_payload={"status": agent.status},
            )
            raise _UNIFORM_ENROLL_FAILURE

        if agent.status not in {AgentLifecycleStatus.REGISTERED, AgentLifecycleStatus.ENROLLED}:
            write_sync_log(
                event_code=EVENT_ENROLLMENT_FAILED,
                message=f"Enrollment Failed: unexpected lifecycle={agent.status}",
                category=SyncLogCategory.AUTH,
                severity=SyncLogSeverity.WARNING,
                sync_agent=agent,
                correlation_id=correlation_id,
            )
            raise _UNIFORM_ENROLL_FAILURE

        if not agent.enrollment_token_hash or not verify_hash(
            enrollment_secret, agent.enrollment_token_hash
        ):
            write_sync_log(
                event_code=EVENT_ENROLLMENT_FAILED,
                message="Enrollment Failed: invalid enrollment secret",
                category=SyncLogCategory.AUTH,
                severity=SyncLogSeverity.WARNING,
                sync_agent=agent,
                correlation_id=correlation_id,
            )
            raise _UNIFORM_ENROLL_FAILURE

        # Update machine metadata from the agent host.
        agent.machine_name = machine_name or hostname or agent.machine_name
        agent.operating_system = operating_system or agent.operating_system
        agent.version = service_version or agent.version
        agent.status = AgentLifecycleStatus.ENROLLED
        agent.is_active = True
        agent.bootstrap_required = True
        agent.last_seen_at = timezone.now()
        # One-time enrollment secret is consumed on success (replay protection).
        agent.enrollment_token_hash = ""
        agent.save(
            update_fields=[
                "machine_name",
                "operating_system",
                "version",
                "status",
                "is_active",
                "bootstrap_required",
                "last_seen_at",
                "enrollment_token_hash",
                "updated_at",
            ]
        )

        access_token = issue_access_token(agent)
        config_version, schema_version = agent_expected_versions(agent)

        write_sync_log(
            event_code=EVENT_AGENT_ENROLLED,
            message="Enrollment Success",
            category=SyncLogCategory.AUTH,
            severity=SyncLogSeverity.INFO,
            sync_agent=agent,
            correlation_id=correlation_id,
            json_payload={
                "hostname": hostname,
                "service_version": service_version,
                "sqlite_schema_version": sqlite_schema_version,
                "portal_version": portal_version,
                "configuration_version": config_version,
                "schema_version": schema_version,
            },
        )

        return {
            "status": "enrolled",
            "message": "Enrollment successful",
            "access_token": access_token,
            "agent_uuid": str(agent.agent_uuid),
            "heartbeat_interval_seconds": heartbeat_interval_seconds(),
            "bootstrap_required": True,
            "configuration_version": config_version,
            "schema_version": schema_version,
            "server_time": timezone.now().isoformat(),
        }
