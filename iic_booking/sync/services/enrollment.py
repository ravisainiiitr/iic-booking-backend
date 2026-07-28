"""Agent enrollment service."""

from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction
from django.utils import timezone

from iic_booking.sync.constants import heartbeat_interval_seconds
from iic_booking.sync.exceptions import (
    EnrollmentFailedError,
    InvalidEnrollmentSecretError,
    InvalidLifecycleStateError,
    UnknownAgentError,
)
from iic_booking.sync.models import AgentLifecycleStatus, DepartmentSyncAgent, SyncLogCategory, SyncLogSeverity
from iic_booking.sync.services.logging import (
    EVENT_AGENT_ENROLLED,
    EVENT_ENROLLMENT_FAILED,
    write_sync_log,
)
from iic_booking.sync.services.tokens import (
    agent_expected_versions,
    issue_access_token,
    verify_hash,
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

        agent = (
            DepartmentSyncAgent.objects.select_related("department", "equipment")
            .filter(agent_uuid=agent_uuid)
            .first()
        )
        if agent is None:
            raise UnknownAgentError("Unknown agent UUID.")

        if agent.status in {AgentLifecycleStatus.DISABLED, AgentLifecycleStatus.REVOKED}:
            write_sync_log(
                event_code=EVENT_ENROLLMENT_FAILED,
                message=f"Enrollment rejected: lifecycle={agent.status}",
                category=SyncLogCategory.AUTH,
                severity=SyncLogSeverity.WARNING,
                sync_agent=agent,
                correlation_id=correlation_id,
                json_payload={"status": agent.status},
            )
            raise InvalidLifecycleStateError(
                f"Agent lifecycle status '{agent.status}' does not allow enrollment."
            )

        if agent.status not in {AgentLifecycleStatus.REGISTERED, AgentLifecycleStatus.ENROLLED}:
            write_sync_log(
                event_code=EVENT_ENROLLMENT_FAILED,
                message=f"Enrollment rejected: unexpected lifecycle={agent.status}",
                category=SyncLogCategory.AUTH,
                severity=SyncLogSeverity.WARNING,
                sync_agent=agent,
                correlation_id=correlation_id,
            )
            raise InvalidLifecycleStateError()

        if not verify_hash(enrollment_secret, agent.enrollment_token_hash):
            write_sync_log(
                event_code=EVENT_ENROLLMENT_FAILED,
                message="Enrollment failed: invalid enrollment secret",
                category=SyncLogCategory.AUTH,
                severity=SyncLogSeverity.WARNING,
                sync_agent=agent,
                correlation_id=correlation_id,
            )
            raise InvalidEnrollmentSecretError()

        # Update machine metadata from the agent host.
        agent.machine_name = machine_name or hostname or agent.machine_name
        agent.operating_system = operating_system or agent.operating_system
        agent.version = service_version or agent.version
        agent.status = AgentLifecycleStatus.ENROLLED
        agent.is_active = True
        agent.bootstrap_required = True
        agent.last_seen_at = timezone.now()
        # One-time enrollment secret is consumed on success.
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
            message="Agent enrolled",
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
