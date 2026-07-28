"""Request signature verification and security façade (Milestone 12)."""

from __future__ import annotations

import hashlib
import hmac
import time
import uuid
from typing import Any

from django.conf import settings
from django.utils.encoding import force_bytes

from iic_booking.sync.constants import signature_replay_window_seconds
from iic_booking.sync.models import DepartmentSyncAgent
from iic_booking.sync.services.certificate_service import CertificateService
from iic_booking.sync.services.device_identity import DeviceIdentityService
from iic_booking.sync.services.api_key_rotation import ApiKeyRotationService
from iic_booking.sync.services.security_audit import (
    EVENT_AUTHENTICATION_SUCCESS,
    EVENT_PERMISSION_DENIED,
    EVENT_REPLAY_DETECTED,
    EVENT_SIGNATURE_INVALID,
    EVENT_SIGNATURE_MISSING,
    SecurityAuditService,
)
from iic_booking.sync.services.tokens import verify_hash


class RequestSigningService:
    """
    Verifies agent request signatures.

    Canonical string:
      METHOD\\nPATH\\nTIMESTAMP\\nNONCE\\nBODY_SHA256\\nDEVICE_ID\\nAGENT_UUID
    """

    HEADER_SIGNATURE = "HTTP_X_DSA_SIGNATURE"
    HEADER_TIMESTAMP = "HTTP_X_DSA_TIMESTAMP"
    HEADER_NONCE = "HTTP_X_DSA_NONCE"
    HEADER_DEVICE_ID = "HTTP_X_DSA_DEVICE_ID"
    HEADER_KEY_ID = "HTTP_X_DSA_KEY_ID"
    HEADER_CORRELATION = "HTTP_X_CORRELATION_ID"

    def __init__(self) -> None:
        self._audit = SecurityAuditService()
        self._seen_nonces: dict[str, float] = {}

    def required_for(self, agent: DepartmentSyncAgent) -> bool:
        if getattr(settings, "DSA_REQUEST_SIGNING_REQUIRED", False):
            return True
        return bool(agent.signing_required)

    def verify_request(
        self,
        request,
        agent: DepartmentSyncAgent,
        *,
        signing_secret_plaintext: str | None = None,
    ) -> tuple[bool, str | None]:
        if not self.required_for(agent) and not self._has_signature_headers(request):
            return True, None

        signature = request.META.get(self.HEADER_SIGNATURE) or request.headers.get("X-DSA-Signature")
        timestamp = request.META.get(self.HEADER_TIMESTAMP) or request.headers.get("X-DSA-Timestamp")
        nonce = request.META.get(self.HEADER_NONCE) or request.headers.get("X-DSA-Nonce")
        device_id = request.META.get(self.HEADER_DEVICE_ID) or request.headers.get("X-DSA-Device-Id")

        if not signature or not timestamp or not nonce:
            self._audit.write(
                event_code=EVENT_SIGNATURE_MISSING,
                message="Missing request signature headers",
                sync_agent=agent,
                durable=True,
            )
            return False, "Missing signature headers."

        try:
            ts = int(timestamp)
        except (TypeError, ValueError):
            return False, "Invalid signature timestamp."

        now = int(time.time())
        window = signature_replay_window_seconds()
        if abs(now - ts) > window:
            self._audit.write(
                event_code=EVENT_REPLAY_DETECTED,
                message="Signature timestamp outside replay window",
                sync_agent=agent,
                details={"timestamp": ts, "now": now},
                durable=True,
            )
            return False, "Replay window exceeded."

        nonce_key = f"{agent.pk}:{nonce}"
        if nonce_key in self._seen_nonces and now - self._seen_nonces[nonce_key] < window:
            self._audit.write(
                event_code=EVENT_REPLAY_DETECTED,
                message="Replay nonce detected",
                sync_agent=agent,
                durable=True,
            )
            return False, "Replay detected."
        self._seen_nonces[nonce_key] = float(now)
        # Opportunistic cleanup
        if len(self._seen_nonces) > 5000:
            cutoff = now - window
            self._seen_nonces = {k: v for k, v in self._seen_nonces.items() if v >= cutoff}

        body = b""
        try:
            body = request.body or b""
        except Exception:
            body = b""
        body_hash = hashlib.sha256(body).hexdigest()
        path = request.get_full_path()
        method = request.method.upper()
        canonical = "\n".join(
            [
                method,
                path,
                str(ts),
                str(nonce),
                body_hash,
                str(device_id or agent.device_id or agent.machine_guid),
                str(agent.agent_uuid),
            ]
        )

        # Prefer explicit plaintext secret from registration; never soft-accept with only a hash.
        if signing_secret_plaintext:
            expected = hmac.new(
                force_bytes(signing_secret_plaintext),
                force_bytes(canonical),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, signature.strip().lower()):
                self._audit.write(
                    event_code=EVENT_SIGNATURE_INVALID,
                    message="Invalid request signature",
                    sync_agent=agent,
                    durable=True,
                )
                return False, "Invalid signature."
            return True, None

        # Fail closed: hash-only agents cannot complete HMAC verification.
        if agent.signing_secret_hash or self.required_for(agent) or self._has_signature_headers(request):
            self._audit.write(
                event_code=EVENT_SIGNATURE_INVALID,
                message="Signing required but HMAC secret unavailable (fail closed)",
                sync_agent=agent,
                durable=True,
            )
            return False, "Signing secret not available for verification."

        return True, None

    def _has_signature_headers(self, request) -> bool:
        return bool(
            request.META.get(self.HEADER_SIGNATURE)
            or request.headers.get("X-DSA-Signature")
        )


class SecurityService:
    """Façade composing device identity, certificates, API keys, signing, audit."""

    def __init__(self) -> None:
        self.device_identity = DeviceIdentityService()
        self.certificates = CertificateService()
        self.api_keys = ApiKeyRotationService()
        self.signing = RequestSigningService()
        self.audit = SecurityAuditService()

    def authorize_remote_command(
        self,
        agent: DepartmentSyncAgent,
        *,
        command_type: str,
        correlation_id: uuid.UUID | None = None,
        user_name: str = "",
        ip_address: str | None = None,
    ) -> bool:
        if agent.security_registration_status == "REVOKED" or agent.certificate_revoked_at:
            self.audit.write(
                event_code=EVENT_PERMISSION_DENIED,
                message=f"Remote command denied: revoked ({command_type})",
                sync_agent=agent,
                correlation_id=correlation_id,
                user_name=user_name,
                ip_address=ip_address,
                durable=True,
            )
            return False
        cert = self.certificates.validate(agent)
        if agent.signing_required and not cert.get("valid"):
            self.audit.write(
                event_code=EVENT_PERMISSION_DENIED,
                message=f"Remote command denied: certificate invalid ({command_type})",
                sync_agent=agent,
                correlation_id=correlation_id,
                user_name=user_name,
                ip_address=ip_address,
                details=cert,
                durable=True,
            )
            return False
        self.audit.write(
            event_code=EVENT_AUTHENTICATION_SUCCESS,
            message=f"Remote command authorized ({command_type})",
            sync_agent=agent,
            correlation_id=correlation_id,
            user_name=user_name,
            ip_address=ip_address,
            details={"command_type": command_type},
        )
        return True
