"""X.509-compatible certificate lifecycle for DSA devices (Milestone 12)."""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from iic_booking.sync.exceptions import SyncControlPlaneError
from iic_booking.sync.models import DepartmentSyncAgent, DeviceCertificate
from iic_booking.sync.services.security_audit import (
    EVENT_CERTIFICATE_EXPIRED,
    EVENT_CERTIFICATE_ISSUED,
    EVENT_CERTIFICATE_RENEWED,
    EVENT_CERTIFICATE_REVOKED,
    SecurityAuditService,
)


class CertificateServiceError(SyncControlPlaneError):
    code = "CERTIFICATE_FAILED"
    status_code = 400
    default_message = "Certificate operation failed."


class CertificateService:
    """
    Issues portal-side device certificates.

    Uses a modular abstraction: PEM + thumbprint stored today; future mTLS
    providers can replace issuance without changing callers.
    """

    def __init__(self) -> None:
        self._audit = SecurityAuditService()

    @transaction.atomic
    def issue(
        self,
        agent: DepartmentSyncAgent,
        *,
        public_key: str = "",
        validity_days: int = 365,
        correlation_id: uuid.UUID | None = None,
        renew: bool = False,
    ) -> dict[str, Any]:
        now = timezone.now()
        expires = now + timedelta(days=max(30, validity_days))
        # Placeholder certificate material — real CA integration plugs in here.
        serial = secrets.token_hex(16)
        material = f"DSA-DEV-CERT|{agent.agent_uuid}|{serial}|{expires.isoformat()}"
        thumbprint = hashlib.sha256(material.encode("utf-8")).hexdigest()
        pem = (
            "-----BEGIN CERTIFICATE-----\n"
            f"{secrets.token_urlsafe(96)}\n"
            "-----END CERTIFICATE-----\n"
        )

        DeviceCertificate.objects.filter(sync_agent=agent, is_current=True).update(is_current=False)
        DeviceCertificate.objects.create(
            sync_agent=agent,
            thumbprint=thumbprint,
            public_key=public_key or agent.device_public_key,
            certificate_pem=pem,
            issued_at=now,
            expires_at=expires,
            is_current=True,
        )

        agent.certificate_thumbprint = thumbprint
        agent.certificate_pem = pem
        agent.certificate_expires_at = expires
        agent.certificate_revoked_at = None
        if public_key:
            agent.device_public_key = public_key
        agent.security_registration_status = "REGISTERED"
        agent.save(
            update_fields=[
                "certificate_thumbprint",
                "certificate_pem",
                "certificate_expires_at",
                "certificate_revoked_at",
                "device_public_key",
                "security_registration_status",
                "updated_at",
            ]
        )

        self._audit.write(
            event_code=EVENT_CERTIFICATE_RENEWED if renew else EVENT_CERTIFICATE_ISSUED,
            message="Certificate renewed" if renew else "Certificate issued",
            sync_agent=agent,
            correlation_id=correlation_id,
            details={"thumbprint": thumbprint, "expires_at": expires.isoformat()},
        )
        return {
            "decision": "renewed" if renew else "issued",
            "thumbprint": thumbprint,
            "certificate_pem": pem,
            "expires_at": expires.isoformat(),
        }

    @transaction.atomic
    def renew_if_needed(
        self,
        agent: DepartmentSyncAgent,
        *,
        renewal_days: int = 30,
        correlation_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        if agent.certificate_revoked_at:
            raise CertificateServiceError("Certificate is revoked.")
        if not agent.certificate_expires_at:
            return self.issue(agent, correlation_id=correlation_id, renew=False)
        remaining = agent.certificate_expires_at - timezone.now()
        if remaining.total_seconds() <= renewal_days * 86400:
            return self.issue(agent, correlation_id=correlation_id, renew=True)
        if remaining.total_seconds() <= 0:
            self._audit.write(
                event_code=EVENT_CERTIFICATE_EXPIRED,
                message="Certificate expired",
                sync_agent=agent,
                correlation_id=correlation_id,
            )
            return self.issue(agent, correlation_id=correlation_id, renew=True)
        return {
            "decision": "valid",
            "thumbprint": agent.certificate_thumbprint,
            "expires_at": agent.certificate_expires_at.isoformat(),
        }

    @transaction.atomic
    def revoke(
        self,
        agent: DepartmentSyncAgent,
        *,
        correlation_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        now = timezone.now()
        DeviceCertificate.objects.filter(sync_agent=agent, is_current=True).update(
            is_current=False,
            revoked_at=now,
        )
        agent.certificate_revoked_at = now
        agent.security_registration_status = "REVOKED"
        agent.save(update_fields=["certificate_revoked_at", "security_registration_status", "updated_at"])
        self._audit.write(
            event_code=EVENT_CERTIFICATE_REVOKED,
            message="Certificate revoked",
            sync_agent=agent,
            correlation_id=correlation_id,
        )
        return {"decision": "revoked", "revoked_at": now.isoformat()}

    def validate(self, agent: DepartmentSyncAgent) -> dict[str, Any]:
        if agent.certificate_revoked_at:
            return {"valid": False, "reason": "revoked"}
        if not agent.certificate_thumbprint:
            return {"valid": False, "reason": "missing"}
        if agent.certificate_expires_at and agent.certificate_expires_at < timezone.now():
            return {"valid": False, "reason": "expired"}
        return {
            "valid": True,
            "thumbprint": agent.certificate_thumbprint,
            "expires_at": agent.certificate_expires_at.isoformat()
            if agent.certificate_expires_at
            else None,
        }
