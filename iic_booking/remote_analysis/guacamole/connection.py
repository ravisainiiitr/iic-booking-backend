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
        raise GuacamoleClientError(
            "Reverse tunnel adapter hostname/port are required. "
            "Direct RDP (guacd → workstation) is not supported."
        )
    username = (secret.username if secret else "") or ""
    password = decrypt_password(secret.password_encrypted) if secret else ""
    domain = (secret.domain if secret else "") or ""
    security = (secret.security if secret else "nla") or "nla"

    # Local Windows accounts: FreeRDP/guacd through the reverse tunnel rejects
    # ".\user" with NLA ("Credentials expired"). Prefer bare username + computer
    # name as domain (matches whoami COMPUTER\user).
    host_label = (ws.hostname or "").strip()
    if username:
        local_user = username.replace("/", "\\").split("\\")[-1].lstrip(".")
        if local_user.startswith("\\"):
            local_user = local_user[1:]
        domain_is_local = (not domain) or (
            domain.lower() in {host_label.lower(), local_user.lower(), ".", "local"}
        )
        if domain_is_local:
            username = local_user
            domain = host_label or domain

    enable_drive = session.file_transfer_enabled and session.file_transfer_policy != "DISABLED"
    enable_audio = session.audio_enabled and settings_obj.audio_enabled
    disable_clipboard = not (session.clipboard_enabled and settings_obj.clipboard_enabled)

    # Prefer a moderate default resolution. Do NOT use resize-method=display-update
    # for portal iframe embeds: when Guacamole's display element briefly reports a
    # near-zero height, Windows RDP is resized to a few pixels (black screen with a
    # thin wallpaper strip). Leave resize-method empty so the client scales the
    # fixed desktop into the viewport instead.
    width = int(session.display_width or settings_obj.default_display_width or 1280)
    height = int(session.display_height or settings_obj.default_display_height or 720)
    if width < 1024:
        width = 1280
    if height < 600:
        height = 720

    params: dict[str, Any] = {
        "hostname": hostname,
        "port": port,
        "username": username,
        "password": password,
        "domain": domain,
        "security": security,
        "ignore-cert": "true",
        "width": str(width),
        "height": str(height),
        "color-depth": str(session.color_depth or settings_obj.default_color_depth),
        "enable-drive": "true" if enable_drive else "false",
        "enable-audio": "true" if enable_audio else "false",
        "disable-audio": _disable_flag(enable_audio),
        "disable-copy": "true" if disable_clipboard else "",
        "disable-paste": "true" if disable_clipboard else "",
        "enable-printing": "false",
        "disable-print": "true",
        # intentional empty — see comment above
        "resize-method": "",
    }
    if session.file_transfer_policy == "UPLOAD_ONLY":
        params["disable-download"] = "true"
    elif session.file_transfer_policy == "DOWNLOAD_ONLY":
        params["disable-upload"] = "true"
    return params


def _credential_diagnostics(session: RemoteDesktopSession, params: dict[str, Any]) -> dict[str, Any]:
    """Safe diagnostics for logs — never includes password plaintext."""
    ws = session.workstation
    secret = WorkstationRdpSecret.objects.filter(workstation=ws).first()
    return {
        "session_id": str(session.id),
        "workstation_id": str(ws.id) if ws else None,
        "workstation_hostname": (ws.hostname if ws else "") or "",
        "rdp_secret_present": secret is not None,
        "rdp_username_present": bool((params.get("username") or "").strip()),
        "rdp_password_present": bool((params.get("password") or "").strip()),
        "rdp_domain_set": bool((params.get("domain") or "").strip()),
        "rdp_hostname": (params.get("hostname") or "")[:120],
        "rdp_port": params.get("port"),
        "rdp_security": params.get("security"),
    }


class ConnectionManager:
    def __init__(self, settings_obj: RemoteAnalysisSettings | None = None):
        self.settings = settings_obj or RemoteAnalysisSettings.get_solo()
        self.client = GuacamoleClient(self.settings)

    def create_ephemeral(self, session: RemoteDesktopSession) -> tuple[GuacamoleConnection, str, str]:
        """
        Create temporary Guacamole user + RDP connection.
        Returns (GuacamoleConnection, temp_username, temp_password).

        Always provisions a reverse tunnel so hostname/port point at the AWS
        GuacamoleSocketAdapter; the agent bridges to localhost:3389.

        Windows credentials come only from WorkstationRdpSecret (installer /
        admin). They are injected into Guacamole connection parameters
        server-side and never returned to the browser.
        """
        from iic_booking.remote_analysis.tunnel import TunnelOrchestrator

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
        diag = _credential_diagnostics(session, params)
        logger.info("Guacamole RDP credential diagnostics: %s", diag)

        if not params.get("hostname"):
            if self.settings.mock_guacamole:
                params["hostname"] = session.workstation.hostname or "mock-host"
            else:
                raise GuacamoleClientError(
                    "Workstation has no hostname/IP for RDP (configure inventory / WorkstationRdpSecret)"
                )

        # Without stored Windows credentials Guacamole shows an interactive
        # Username/Password/Domain prompt — refuse rather than silently fall back.
        if not self.settings.mock_guacamole:
            if not (params.get("username") or "").strip() or not (params.get("password") or "").strip():
                logger.error(
                    "Refusing Guacamole connection without workstation RDP credentials: %s",
                    diag,
                )
                raise GuacamoleClientError(
                    "Workstation Windows credentials are not configured. "
                    "Re-run the Remote Analysis Agent installer (or set Workstation RDP Secret "
                    "in Django Admin) so automatic login can succeed."
                )

        temp_user = f"ra-{session.id.hex[:16]}"
        temp_password = secrets.token_urlsafe(24)
        conn_name = f"ra-session-{session.id.hex[:12]}"

        self.client.create_user(temp_user, temp_password)
        created = self.client.create_connection(name=conn_name, parameters=params)
        identifier = str(created.get("identifier") or created.get("name") or uuid.uuid4())
        # Ensure parameters (including credentials) are persisted — some Guacamole
        # builds ignore password on POST and require an explicit PUT.
        try:
            self.client.update_connection_parameters(identifier, parameters=params, name=conn_name)
            verified = self.client.get_connection_parameters(identifier)
            has_user = bool((verified.get("username") or "").strip())
            has_pass = bool((verified.get("password") or "").strip())
            logger.info(
                "Guacamole connection parameters verified id=%s username_set=%s password_set=%s",
                identifier,
                has_user,
                has_pass,
            )
            if not has_user or not has_pass:
                logger.error(
                    "Guacamole accepted connection but credentials missing after update id=%s diag=%s",
                    identifier,
                    diag,
                )
                raise GuacamoleClientError(
                    "Guacamole connection was created without Windows credentials; automatic login cannot proceed."
                )
        except GuacamoleClientError:
            raise
        except Exception:
            logger.exception(
                "Guacamole parameter verify/update failed id=%s — continuing with create response",
                identifier,
            )

        self.client.grant_connection(temp_user, identifier)

        meta = {
            "mock": bool(created.get("mock")),
            "connection_name": conn_name,
            # Server-side only — Fernet ciphertext; never expose via serializers
            "temp_password_encrypted": encrypt_password(temp_password),
            "transport_mode": self.settings.transport_mode,
            "rdp_username_injected": bool((params.get("username") or "").strip()),
            "rdp_password_injected": bool((params.get("password") or "").strip()),
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
