"""Device identity registration for Department Sync Agents (Milestone 12)."""

from __future__ import annotations

import uuid
from typing import Any

from django.db import transaction
from django.utils import timezone

from iic_booking.sync.exceptions import SyncControlPlaneError
from iic_booking.sync.models import DepartmentSyncAgent
from iic_booking.sync.services.security_audit import (
    EVENT_DEVICE_REGISTERED,
    EVENT_DEVICE_REGISTRATION_FAILED,
    SecurityAuditService,
)
from iic_booking.sync.services.tokens import hash_value


class DeviceIdentityError(SyncControlPlaneError):
    code = "DEVICE_IDENTITY_FAILED"
    status_code = 400
    default_message = "Device identity registration failed."


class DeviceIdentityService:
    def __init__(self) -> None:
        self._audit = SecurityAuditService()

    @transaction.atomic
    def register_or_update(
        self,
        agent: DepartmentSyncAgent,
        payload: dict[str, Any],
        *,
        correlation_id: uuid.UUID | None = None,
        ip_address: str | None = None,
    ) -> dict[str, Any]:
        try:
            device_id = uuid.UUID(str(payload.get("device_id") or agent.machine_guid))
        except (TypeError, ValueError) as exc:
            self._audit.write(
                event_code=EVENT_DEVICE_REGISTRATION_FAILED,
                message="Invalid device_id",
                sync_agent=agent,
                correlation_id=correlation_id,
                ip_address=ip_address,
                durable=True,
            )
            raise DeviceIdentityError("device_id must be a valid UUID.") from exc

        # Idempotent: never create a second device for the same agent.
        if agent.device_id and agent.device_id != device_id:
            raise DeviceIdentityError("Device identity mismatch for this agent.")

        public_key = (payload.get("public_key") or payload.get("device_public_key") or "")[:8000]
        thumbprint = (payload.get("certificate_thumbprint") or payload.get("thumbprint") or "")[:128]
        signing_secret = (payload.get("signing_secret") or "").strip()
        security_version = int(payload.get("security_version") or agent.security_version or 1)

        agent.device_id = device_id
        if public_key:
            agent.device_public_key = public_key
        if thumbprint:
            agent.certificate_thumbprint = thumbprint
        if signing_secret:
            agent.signing_secret_hash = hash_value(signing_secret)
        agent.security_version = max(1, security_version)
        agent.security_registration_status = "REGISTERED"
        agent.save(
            update_fields=[
                "device_id",
                "device_public_key",
                "certificate_thumbprint",
                "signing_secret_hash",
                "security_version",
                "security_registration_status",
                "updated_at",
            ]
        )

        self._audit.write(
            event_code=EVENT_DEVICE_REGISTERED,
            message="Device identity registered",
            sync_agent=agent,
            device_id=device_id,
            correlation_id=correlation_id,
            ip_address=ip_address,
            details={"thumbprint": thumbprint, "security_version": agent.security_version},
        )
        return {
            "decision": "registered",
            "device_id": str(device_id),
            "agent_uuid": str(agent.agent_uuid),
            "security_version": agent.security_version,
            "certificate_thumbprint": agent.certificate_thumbprint,
            "security_registration_status": agent.security_registration_status,
        }

    def get_identity(self, agent: DepartmentSyncAgent) -> dict[str, Any]:
        return {
            "device_id": str(agent.device_id or agent.machine_guid),
            "agent_uuid": str(agent.agent_uuid),
            "department_id": agent.department_id,
            "public_key": agent.device_public_key,
            "certificate_thumbprint": agent.certificate_thumbprint,
            "certificate_expires_at": agent.certificate_expires_at.isoformat()
            if agent.certificate_expires_at
            else None,
            "security_version": agent.security_version,
            "security_registration_status": agent.security_registration_status,
            "signing_required": agent.signing_required,
            "registered_at": agent.registered_at.isoformat() if agent.registered_at else None,
        }
