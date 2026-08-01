"""Reverse tunnel token issuance and Gateway admin client."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import secrets
import time
import uuid
from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urljoin

import requests
from django.conf import settings as django_settings
from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    AuditCategory,
    CommandType,
    TunnelSessionStatus,
    TransportMode,
)
from iic_booking.remote_analysis.session_models import RemoteAnalysisSettings
from iic_booking.remote_analysis.tunnel_models import TunnelEvent, TunnelSession

logger = logging.getLogger(__name__)


def _b64url(data: bytes) -> str:
    return urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return urlsafe_b64decode(data + pad)


def tunnel_token_secret() -> bytes:
    """Resolve HMAC secret for tunnel tokens.

    Production (DEBUG=False) requires an explicit RA_TUNNEL_TOKEN_SECRET.
    DEBUG may still fall back to Django SECRET_KEY for local loops only.
    """
    raw = getattr(django_settings, "RA_TUNNEL_TOKEN_SECRET", None) or __import__(
        "os"
    ).environ.get("RA_TUNNEL_TOKEN_SECRET")
    if raw:
        return str(raw).encode("utf-8")
    if getattr(django_settings, "DEBUG", False):
        return str(getattr(django_settings, "SECRET_KEY", "")).encode("utf-8")
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured(
        "RA_TUNNEL_TOKEN_SECRET must be set when DEBUG is False "
        "(Django SECRET_KEY fallback is disabled outside DEBUG)."
    )


@dataclass
class TunnelTokenClaims:
    tunnel_id: str
    booking_id: int | None
    analysis_job_id: str | None
    workstation_id: str
    user_id: int
    exp: int
    nonce: str
    session_version: int
    permissions: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "tunnel_id": self.tunnel_id,
            "booking_id": self.booking_id,
            "analysis_job_id": self.analysis_job_id,
            "workstation_id": self.workstation_id,
            "user_id": self.user_id,
            "exp": self.exp,
            "nonce": self.nonce,
            "session_version": self.session_version,
            "permissions": self.permissions,
        }


class TunnelTokenService:
    """Short-lived HMAC-signed tunnel tokens (no DB lookup per packet)."""

    def issue(self, tunnel: TunnelSession, *, lifetime_seconds: int | None = None) -> str:
        settings_obj = RemoteAnalysisSettings.get_solo()
        lifetime = lifetime_seconds or int(settings_obj.tunnel_token_lifetime_seconds or 120)
        exp = int(time.time()) + max(30, lifetime)
        claims = TunnelTokenClaims(
            tunnel_id=str(tunnel.id),
            booking_id=tunnel.booking_id,
            analysis_job_id=str(tunnel.analysis_job_id) if tunnel.analysis_job_id else None,
            workstation_id=str(tunnel.workstation_id),
            user_id=int(tunnel.user_id),
            exp=exp,
            nonce=tunnel.nonce,
            session_version=int(tunnel.session_version),
            permissions=["rdp_bridge"],
        )
        body = _b64url(json.dumps(claims.to_dict(), separators=(",", ":"), sort_keys=True).encode())
        sig = _b64url(hmac.new(tunnel_token_secret(), body.encode("ascii"), hashlib.sha256).digest())
        tunnel.token_expires_at = timezone.now() + timedelta(seconds=lifetime)
        tunnel.save(update_fields=["token_expires_at", "updated_at"])
        return f"{body}.{sig}"

    def verify(self, token: str) -> TunnelTokenClaims:
        try:
            body, sig = token.split(".", 1)
        except ValueError as exc:
            raise ValueError("Malformed tunnel token") from exc
        expected = _b64url(hmac.new(tunnel_token_secret(), body.encode("ascii"), hashlib.sha256).digest())
        if not hmac.compare_digest(expected, sig):
            raise ValueError("Invalid tunnel token signature")
        payload = json.loads(_b64url_decode(body))
        if int(payload.get("exp") or 0) < int(time.time()):
            raise ValueError("Tunnel token expired")
        return TunnelTokenClaims(
            tunnel_id=str(payload["tunnel_id"]),
            booking_id=payload.get("booking_id"),
            analysis_job_id=payload.get("analysis_job_id"),
            workstation_id=str(payload["workstation_id"]),
            user_id=int(payload["user_id"]),
            exp=int(payload["exp"]),
            nonce=str(payload["nonce"]),
            session_version=int(payload["session_version"]),
            permissions=list(payload.get("permissions") or []),
        )


def tunnel_admin_key() -> str:
    """Resolve Gateway admin key (never log the value)."""
    return (
        getattr(django_settings, "RA_TUNNEL_GATEWAY_ADMIN_KEY", None)
        or __import__("os").environ.get("RA_TUNNEL_GATEWAY_ADMIN_KEY")
        or ""
    )


def tunnel_token_secret_configured() -> bool:
    """True when an explicit tunnel HMAC secret is available (no DEBUG fallback)."""
    raw = getattr(django_settings, "RA_TUNNEL_TOKEN_SECRET", None) or __import__("os").environ.get(
        "RA_TUNNEL_TOKEN_SECRET"
    )
    return bool(raw and str(raw).strip())


def reverse_tunnel_config_status(settings_obj: RemoteAnalysisSettings | None = None) -> dict[str, str]:
    """
    Presence-only checks for reverse-tunnel cutover config.
    Values are never included — only configured / missing.
    """
    import os

    settings_obj = settings_obj or RemoteAnalysisSettings.get_solo()
    admin_url = (
        (settings_obj.tunnel_gateway_admin_url or "").strip()
        or (os.environ.get("RA_TUNNEL_GATEWAY_ADMIN_URL") or "").strip()
    )
    wss_url = (
        (settings_obj.tunnel_gateway_wss_url or "").strip()
        or (os.environ.get("RA_TUNNEL_GATEWAY_WSS_URL") or "").strip()
    )
    adapter = (
        (settings_obj.tunnel_adapter_hostname or "").strip()
        or (os.environ.get("RA_TUNNEL_ADAPTER_HOSTNAME") or "").strip()
    )
    return {
        "RA_TUNNEL_TOKEN_SECRET": "configured" if tunnel_token_secret_configured() else "missing",
        "RA_TUNNEL_GATEWAY_ADMIN_KEY": "configured" if bool(tunnel_admin_key().strip()) else "missing",
        "RA_TUNNEL_GATEWAY_ADMIN_URL": "configured" if admin_url else "missing",
        "RA_TUNNEL_GATEWAY_WSS_URL": "configured" if wss_url else "missing",
        "RA_TUNNEL_ADAPTER_HOSTNAME": "configured" if adapter else "missing",
    }


class TunnelGatewayClient:
    """Portal → Gateway admin HTTP (allocate adapter port, register pending tunnel)."""

    def __init__(self, settings_obj: RemoteAnalysisSettings | None = None):
        self.settings = settings_obj or RemoteAnalysisSettings.get_solo()

    @property
    def base_url(self) -> str:
        return (self.settings.tunnel_gateway_admin_url or "").rstrip("/") + "/"

    def allocate(self, tunnel: TunnelSession, *, token: str) -> dict[str, Any]:
        """
        Register pending tunnel and allocate adapter TCP port for guacd.
        Mock/local: when admin URL empty, allocate a synthetic port for DEBUG only.
        Production reverse_tunnel must never use the mock allocator.
        """
        if not (self.settings.tunnel_gateway_admin_url or "").strip():
            is_rt = self.settings.transport_mode == TransportMode.REVERSE_TUNNEL
            if is_rt and not getattr(django_settings, "DEBUG", False):
                from django.core.exceptions import ImproperlyConfigured

                raise ImproperlyConfigured(
                    "RA_TUNNEL_GATEWAY_ADMIN_URL (tunnel_gateway_admin_url) is required "
                    "when transport_mode=reverse_tunnel and DEBUG=False; "
                    "local mock allocation is disabled in production."
                )
            # Dev/test fallback only.
            port = 40000 + (abs(hash(str(tunnel.id))) % 10000)
            return {
                "adapter_hostname": self.settings.tunnel_adapter_hostname or "127.0.0.1",
                "adapter_port": port,
                "gateway_instance": "local-mock",
                "mock": True,
            }

        url = urljoin(self.base_url, "api/v1/tunnels/allocate")
        body = {
            "tunnel_id": str(tunnel.id),
            "token": token,
            "workstation_id": str(tunnel.workstation_id),
            "booking_id": tunnel.booking_id,
            "idle_timeout_seconds": int(self.settings.tunnel_idle_timeout_seconds or 900),
            "max_lifetime_seconds": int(self.settings.tunnel_max_lifetime_seconds or 14400),
        }
        headers = {"Content-Type": "application/json", "X-Tunnel-Admin-Key": self._admin_key()}
        resp = requests.post(url, json=body, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()

    def close(self, tunnel: TunnelSession, *, reason: str = "") -> None:
        if not (self.settings.tunnel_gateway_admin_url or "").strip():
            return
        url = urljoin(self.base_url, f"api/v1/tunnels/{tunnel.id}/close")
        try:
            requests.post(
                url,
                json={"reason": reason},
                headers={"X-Tunnel-Admin-Key": self._admin_key()},
                timeout=10,
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to close tunnel %s on gateway", tunnel.id)

    def health(self) -> dict[str, Any]:
        if not (self.settings.tunnel_gateway_admin_url or "").strip():
            return {"ok": False, "detail": "tunnel_gateway_admin_url not configured"}
        url = urljoin(self.base_url, "api/v1/health")
        try:
            resp = requests.get(url, timeout=5)
            data = resp.json() if resp.content else {}
            return {"ok": resp.ok, "status_code": resp.status_code, **data}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc)}

    def metrics(self) -> dict[str, Any]:
        if not (self.settings.tunnel_gateway_admin_url or "").strip():
            return {"ok": False, "detail": "tunnel_gateway_admin_url not configured"}
        url = urljoin(self.base_url, "api/v1/metrics")
        try:
            resp = requests.get(url, headers={"X-Tunnel-Admin-Key": self._admin_key()}, timeout=5)
            data = resp.json() if resp.content else {}
            return {"ok": resp.ok, **data}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "detail": str(exc)}

    def _admin_key(self) -> str:
        return tunnel_admin_key()


class TunnelOrchestrator:
    """Create pending tunnel, allocate adapter, enqueue JOIN_TUNNEL for the agent."""

    def __init__(self, settings_obj: RemoteAnalysisSettings | None = None):
        self.settings = settings_obj or RemoteAnalysisSettings.get_solo()
        self.tokens = TunnelTokenService()
        self.gateway = TunnelGatewayClient(self.settings)

    def is_reverse_tunnel(self) -> bool:
        return self.settings.transport_mode == TransportMode.REVERSE_TUNNEL

    def apply_join_result(
        self,
        tunnel: TunnelSession,
        *,
        success: bool,
        message: str = "",
    ) -> TunnelSession:
        """
        Drive TunnelSession lifecycle from JOIN_TUNNEL command completion.

        WAITING_AGENT → ACTIVE (success) or FAILED (failure).
        Terminal CLOSED/EXPIRED rows are left unchanged.
        """
        if tunnel.status in {
            TunnelSessionStatus.CLOSED,
            TunnelSessionStatus.EXPIRED,
        }:
            return tunnel

        now = timezone.now()
        if success:
            tunnel.status = TunnelSessionStatus.ACTIVE
            tunnel.agent_joined_at = now
            tunnel.activated_at = now
            tunnel.save(
                update_fields=[
                    "status",
                    "agent_joined_at",
                    "activated_at",
                    "updated_at",
                ]
            )
            TunnelEvent.objects.create(
                tunnel=tunnel,
                event_type="JOINED",
                detail=message or "agent joined",
            )
            TunnelEvent.objects.create(
                tunnel=tunnel,
                event_type="ACTIVE",
                detail=message or "tunnel active",
            )
            return tunnel

        tunnel.status = TunnelSessionStatus.FAILED
        tunnel.close_reason = (message or "JOIN_TUNNEL failed")[:255]
        tunnel.save(update_fields=["status", "close_reason", "updated_at"])
        TunnelEvent.objects.create(
            tunnel=tunnel,
            event_type="JOIN_FAILED",
            detail=message or "JOIN_TUNNEL failed",
        )
        return tunnel

    def provision_for_session(self, desktop_session, *, analysis_job=None) -> TunnelSession:
        """Create TunnelSession, allocate adapter port, command agent to join."""
        from iic_booking.remote_analysis.services.commands import CommandService
        from iic_booking.remote_analysis.services.audit import record_event

        # One active tunnel per analysis job when present.
        if analysis_job is not None:
            active = TunnelSession.objects.filter(
                analysis_job=analysis_job,
                status__in={
                    TunnelSessionStatus.PENDING,
                    TunnelSessionStatus.WAITING_AGENT,
                    TunnelSessionStatus.ACTIVE,
                    TunnelSessionStatus.RECONNECTING,
                },
            ).first()
            if active:
                raise ValueError("An active tunnel already exists for this analysis job")

        nonce = secrets.token_urlsafe(24)
        tunnel = TunnelSession.objects.create(
            desktop_session=desktop_session,
            booking=getattr(desktop_session, "booking", None),
            analysis_job=analysis_job,
            workstation=desktop_session.workstation,
            user=desktop_session.user,
            status=TunnelSessionStatus.PENDING,
            nonce=nonce,
            adapter_hostname=self.settings.tunnel_adapter_hostname or "reverse-tunnel-gateway",
        )
        token = self.tokens.issue(tunnel)
        alloc = self.gateway.allocate(tunnel, token=token)
        tunnel.adapter_hostname = str(alloc.get("adapter_hostname") or tunnel.adapter_hostname)
        tunnel.adapter_port = int(alloc["adapter_port"])
        tunnel.gateway_instance = str(alloc.get("gateway_instance") or "")
        tunnel.status = TunnelSessionStatus.WAITING_AGENT
        tunnel.save(
            update_fields=[
                "adapter_hostname",
                "adapter_port",
                "gateway_instance",
                "status",
                "updated_at",
            ]
        )
        TunnelEvent.objects.create(
            tunnel=tunnel,
            event_type="allocated",
            detail=f"port={tunnel.adapter_port}",
            metadata={"mock": bool(alloc.get("mock"))},
        )

        wss = (self.settings.tunnel_gateway_wss_url or "").rstrip("/")
        CommandService().create_command(
            desktop_session.workstation,
            CommandType.JOIN_TUNNEL,
            payload={
                "tunnel_id": str(tunnel.id),
                "token": token,
                "wss_url": wss,
                "rdp_host": "127.0.0.1",
                "rdp_port": 3389,
                "session_version": tunnel.session_version,
            },
            created_by=desktop_session.user,
        )
        record_event(
            category=AuditCategory.SESSION,
            action="TunnelProvisioned",
            workstation=desktop_session.workstation,
            actor=desktop_session.user,
            details=f"tunnel={tunnel.id}",
            correlation_id=str(tunnel.id),
            success=True,
        )
        return tunnel

    def close(self, tunnel: TunnelSession, *, reason: str = "", actor=None) -> None:
        from iic_booking.remote_analysis.services.commands import CommandService

        self.gateway.close(tunnel, reason=reason)
        if tunnel.workstation_id:
            CommandService().create_command(
                tunnel.workstation,
                CommandType.CLOSE_TUNNEL,
                payload={"tunnel_id": str(tunnel.id), "reason": reason},
                created_by=actor,
            )
        tunnel.status = TunnelSessionStatus.CLOSED
        tunnel.closed_at = timezone.now()
        tunnel.close_reason = reason[:255]
        tunnel.save(update_fields=["status", "closed_at", "close_reason", "updated_at"])
        TunnelEvent.objects.create(tunnel=tunnel, event_type="closed", detail=reason)
