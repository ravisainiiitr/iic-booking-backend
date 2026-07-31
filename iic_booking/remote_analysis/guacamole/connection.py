"""Ephemeral Guacamole connection management."""

from __future__ import annotations

import logging
import secrets
import uuid
from typing import Any

from django.utils import timezone

from iic_booking.remote_analysis.guacamole.client import GuacamoleClient, GuacamoleClientError
from iic_booking.remote_analysis.guacamole.secrets import decrypt_password, encrypt_password
from iic_booking.remote_analysis.session_models import (
    GuacamoleConnection,
    RemoteAnalysisSettings,
    RemoteDesktopSession,
    WorkstationRdpSecret,
)

logger = logging.getLogger(__name__)


def _disable_flag(enabled: bool) -> str:
    return "" if enabled else "true"


def build_rdp_parameters(
    session: RemoteDesktopSession,
    settings_obj: RemoteAnalysisSettings,
    *,
    tunnel=None,
) -> dict[str, Any]:
    ws = session.workstation
    secret = WorkstationRdpSecret.objects.filter(workstation=ws).first()
    if tunnel is not None and getattr(tunnel, "adapter_hostname", None) and tunnel.adapter_port:
        hostname = (tunnel.adapter_hostname or "").strip()
        port = str(tunnel.adapter_port)
    else:
        hostname = (ws.ip_address or ws.hostname or "").strip()
        port = str(secret.port if secret else 3389)
    username = secret.username if secret else ""
    password = decrypt_password(secret.password_encrypted) if secret else ""
    domain = secret.domain if secret else ""
    security = (secret.security if secret else "nla") or "nla"

    enable_drive = session.file_transfer_enabled and session.file_transfer_policy != "DISABLED"
    enable_audio = session.audio_enabled and settings_obj.audio_enabled
    disable_clipboard = not (session.clipboard_enabled and settings_obj.clipboard_enabled)

    params: dict[str, Any] = {
        "hostname": hostname,
        "port": port,
        "username": username,
        "password": password,
        "domain": domain,
        "security": security,
        "ignore-cert": "true",
        "width": str(session.display_width or settings_obj.default_display_width),
        "height": str(session.display_height or settings_obj.default_display_height),
        "color-depth": str(session.color_depth or settings_obj.default_color_depth),
        "enable-drive": "true" if enable_drive else "false",
        "enable-audio": "true" if enable_audio else "false",
        "disable-audio": _disable_flag(enable_audio),
        "disable-copy": "true" if disable_clipboard else "",
        "disable-paste": "true" if disable_clipboard else "",
        "enable-printing": "false",
        "disable-print": "true",
        "resize-method": "display-update",
    }
    if session.file_transfer_policy == "UPLOAD_ONLY":
        params["disable-download"] = "true"
    elif session.file_transfer_policy == "DOWNLOAD_ONLY":
        params["disable-upload"] = "true"
    return params


class ConnectionManager:
    def __init__(self, settings_obj: RemoteAnalysisSettings | None = None):
        self.settings = settings_obj or RemoteAnalysisSettings.get_solo()
        self.client = GuacamoleClient(self.settings)

    def create_ephemeral(self, session: RemoteDesktopSession) -> tuple[GuacamoleConnection, str, str]:
        """
        Create temporary Guacamole user + RDP connection.
        Returns (GuacamoleConnection, temp_username, temp_password).

        When transport_mode=reverse_tunnel, hostname/port point at the AWS
        GuacamoleSocketAdapter; the agent bridges to localhost:3389.
        """
        from iic_booking.remote_analysis.constants import TransportMode
        from iic_booking.remote_analysis.tunnel import TunnelOrchestrator

        tunnel = None
        if self.settings.transport_mode == TransportMode.REVERSE_TUNNEL:
            analysis_job = None
            try:
                from iic_booking.remote_analysis.workflow_models import AnalysisJob

                if session.booking_id:
                    analysis_job = (
                        AnalysisJob.objects.filter(booking_id=session.booking_id)
                        .exclude(status__in={"COMPLETED", "CANCELLED", "FAILED"})
                        .order_by("-created_at")
                        .first()
                    )
            except Exception:  # noqa: BLE001
                analysis_job = None
            tunnel = TunnelOrchestrator(self.settings).provision_for_session(
                session, analysis_job=analysis_job
            )

        params = build_rdp_parameters(session, self.settings, tunnel=tunnel)
        if not params.get("hostname"):
            if self.settings.mock_guacamole:
                params["hostname"] = session.workstation.hostname or "mock-host"
            else:
                raise GuacamoleClientError(
                    "Workstation has no hostname/IP for RDP (configure inventory / WorkstationRdpSecret)"
                )

        temp_user = f"ra-{session.id.hex[:16]}"
        temp_password = secrets.token_urlsafe(24)
        conn_name = f"ra-session-{session.id.hex[:12]}"

        self.client.create_user(temp_user, temp_password)
        created = self.client.create_connection(name=conn_name, parameters=params)
        identifier = str(created.get("identifier") or created.get("name") or uuid.uuid4())
        self.client.grant_connection(temp_user, identifier)

        meta = {
            "mock": bool(created.get("mock")),
            "connection_name": conn_name,
            # Server-side only — Fernet ciphertext; never expose via serializers
            "temp_password_encrypted": encrypt_password(temp_password),
            "transport_mode": self.settings.transport_mode,
        }
        if tunnel is not None:
            meta["tunnel_id"] = str(tunnel.id)
            meta["adapter_port"] = tunnel.adapter_port

        row, _ = GuacamoleConnection.objects.update_or_create(
            session=session,
            defaults={
                "guacamole_connection_id": identifier,
                "guacamole_identifier": identifier,
                "guacamole_username": temp_user,
                "protocol": "rdp",
                "is_active": True,
                "destroyed_at": None,
                "internal_hostname": params.get("hostname", ""),
                "metadata": meta,
            },
        )
        return row, temp_user, temp_password

    def ephemeral_password(self, conn: GuacamoleConnection) -> str:
        meta = conn.metadata or {}
        enc = meta.get("temp_password_encrypted") or ""
        if enc:
            return decrypt_password(enc)
        # Legacy plaintext (pre-Phase-3) — migrate in-memory and re-encrypt when possible
        legacy = meta.get("temp_password") or ""
        return legacy

    def destroy(self, session: RemoteDesktopSession) -> bool:
        try:
            conn = session.guacamole_connection
        except GuacamoleConnection.DoesNotExist:
            return True

        meta = conn.metadata or {}
        tunnel_id = meta.get("tunnel_id")
        if tunnel_id:
            try:
                from iic_booking.remote_analysis.tunnel import TunnelOrchestrator
                from iic_booking.remote_analysis.tunnel_models import TunnelSession

                tunnel = TunnelSession.objects.filter(pk=tunnel_id).first()
                if tunnel:
                    TunnelOrchestrator(self.settings).close(tunnel, reason="guacamole_destroy")
            except Exception:  # noqa: BLE001
                logger.exception("Failed to close reverse tunnel for session %s", session.id)

        self.client.delete_connection(conn.guacamole_connection_id)
        if conn.guacamole_username:
            self.client.delete_user(conn.guacamole_username)
        conn.is_active = False
        conn.destroyed_at = timezone.now()
        conn.save(update_fields=["is_active", "destroyed_at"])
        return True
