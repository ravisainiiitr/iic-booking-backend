"""API key rotation for Department Sync Agents (Milestone 12)."""

from __future__ import annotations

import secrets
import uuid
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from iic_booking.sync.exceptions import SyncControlPlaneError
from iic_booking.sync.models import AgentApiKey, DepartmentSyncAgent
from iic_booking.sync.services.security_audit import EVENT_API_KEY_ROTATED, SecurityAuditService
from iic_booking.sync.services.tokens import hash_value, verify_hash


class ApiKeyRotationError(SyncControlPlaneError):
    code = "API_KEY_ROTATION_FAILED"
    status_code = 400
    default_message = "API key rotation failed."


class ApiKeyRotationService:
    def __init__(self) -> None:
        self._audit = SecurityAuditService()

    @transaction.atomic
    def rotate(
        self,
        agent: DepartmentSyncAgent,
        *,
        lifetime_days: int = 90,
        grace_days: int = 7,
        correlation_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """
        Issue a new API key while keeping the previous key active for a grace window
        so synchronization is not interrupted.
        """
        now = timezone.now()
        previous = (
            AgentApiKey.objects.filter(sync_agent=agent, is_active=True)
            .order_by("-created_at")
            .first()
        )
        plaintext = secrets.token_urlsafe(48)
        key_id = f"dsa_{secrets.token_hex(8)}"
        expires = now + timedelta(days=max(1, lifetime_days))
        row = AgentApiKey.objects.create(
            sync_agent=agent,
            key_id=key_id,
            key_hash=hash_value(plaintext),
            expires_at=expires,
            is_active=True,
            rotated_from=previous,
        )
        agent.api_key_hash = row.key_hash
        agent.api_key_rotated_at = now
        agent.save(update_fields=["api_key_hash", "api_key_rotated_at", "updated_at"])

        if previous is not None:
            # Graceful rollover: keep old key until grace window ends.
            previous.expires_at = now + timedelta(days=max(0, grace_days))
            previous.save(update_fields=["expires_at"])

        self._audit.write(
            event_code=EVENT_API_KEY_ROTATED,
            message="API key rotated",
            sync_agent=agent,
            correlation_id=correlation_id,
            details={"key_id": key_id, "grace_days": grace_days},
        )
        return {
            "decision": "rotated",
            "key_id": key_id,
            "api_key": plaintext,  # returned once
            "expires_at": expires.isoformat(),
            "previous_key_id": previous.key_id if previous else None,
            "grace_days": grace_days,
        }

    def verify(self, agent: DepartmentSyncAgent, key_id: str, api_key: str) -> bool:
        now = timezone.now()
        rows = AgentApiKey.objects.filter(sync_agent=agent, key_id=key_id, is_active=True)
        for row in rows:
            if row.revoked_at:
                continue
            if row.expires_at and row.expires_at < now:
                continue
            if verify_hash(api_key, row.key_hash):
                return True
        return False

    @transaction.atomic
    def revoke(self, agent: DepartmentSyncAgent, key_id: str) -> dict[str, Any]:
        row = AgentApiKey.objects.filter(sync_agent=agent, key_id=key_id).first()
        if row is None:
            raise ApiKeyRotationError("API key not found.")
        row.revoked_at = timezone.now()
        row.is_active = False
        row.save(update_fields=["revoked_at", "is_active"])
        return {"decision": "revoked", "key_id": key_id}
