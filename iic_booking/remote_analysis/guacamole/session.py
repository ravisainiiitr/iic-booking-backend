"""Portal session orchestrator for browser remote desktop."""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

from django.db import transaction
from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    SESSION_TOKEN_BYTES,
    AuditCategory,
    CommandStatus,
    CommandType,
    ReservationStatus,
    SessionStatus,
    WorkstationStatus,
)
from iic_booking.remote_analysis.guacamole.audit import audit_session
from iic_booking.remote_analysis.guacamole.authorization import (
    OPEN_SESSION_STATUSES,
    evaluate_session_create_gates,
    evaluate_session_launch_gates,
    find_reusable_open_session,
)
from iic_booking.remote_analysis.guacamole.client import GuacamoleClientError
from iic_booking.remote_analysis.guacamole.connection import ConnectionManager
from iic_booking.remote_analysis.guacamole.health import refresh_session_health, workstation_healthy_for_session
from iic_booking.remote_analysis.guacamole.permissions import can_create_for_reservation, can_launch_session, can_terminate_session
from iic_booking.remote_analysis.models import WorkstationStateHistory
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation, ReservationHistory
from iic_booking.remote_analysis.services.audit import record_event
from iic_booking.remote_analysis.services.commands import CommandService
from iic_booking.remote_analysis.session_models import (
    ConnectionHistory,
    RemoteAnalysisSettings,
    RemoteDesktopSession,
    SessionLaunch,
    SessionStateHistory,
    SessionStatistics,
    SessionTelemetry,
    SessionToken,
)

logger = logging.getLogger(__name__)

ACTIVE_RESERVATION_STATUSES = {
    ReservationStatus.RESERVED,
    ReservationStatus.PREPARING,
    ReservationStatus.READY,
    ReservationStatus.ACTIVE,
}


def hash_session_token(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class SessionError(Exception):
    def __init__(self, message: str, *, code: str = "session_error"):
        super().__init__(message)
        self.code = code


class SessionOrchestrator:
    def __init__(self):
        self.settings = RemoteAnalysisSettings.get_solo()

    def transition(self, session: RemoteDesktopSession, to_status: str, *, reason: str = "") -> RemoteDesktopSession:
        from_status = session.status
        if from_status == to_status:
            return session
        SessionStateHistory.objects.create(
            session=session,
            from_status=from_status,
            to_status=to_status,
            reason=reason[:512],
        )
        session.status = to_status
        session.save(update_fields=["status", "updated_at"])
        ConnectionHistory.objects.create(session=session, event="state", detail=f"{from_status}->{to_status}: {reason}"[:512])
        if to_status in {SessionStatus.ACTIVE, SessionStatus.CONNECTED, SessionStatus.LAUNCHED}:
            try:
                from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
                from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace

                ws_obj = AnalysisWorkspace.objects.filter(reservation_id=session.reservation_id).first()
                if ws_obj:
                    WorkspaceSyncService().mark_session_active(ws_obj)
            except Exception:
                logger.debug("mark_session_active skipped", exc_info=True)
        return session

    def _apply_policies(self, session: RemoteDesktopSession) -> None:
        s = self.settings
        session.idle_timeout_minutes = s.idle_timeout
        session.clipboard_enabled = s.clipboard_enabled
        session.clipboard_policy = s.clipboard_policy
        session.file_transfer_enabled = s.file_transfer_enabled
        session.file_transfer_policy = s.file_transfer_policy
        session.audio_enabled = s.audio_enabled
        session.recording_enabled = False  # Milestone 4: not implemented
        session.display_width = s.default_display_width
        session.display_height = s.default_display_height
        session.color_depth = s.default_color_depth
        minutes = int(s.session_timeout or 30)
        booking = getattr(session, "booking", None) or getattr(getattr(session, "reservation", None), "booking", None)
        if booking is not None:
            eq_minutes = getattr(getattr(booking, "equipment", None), "analysis_default_session_minutes", None)
            if eq_minutes:
                minutes = int(eq_minutes)
        session.expires_at = timezone.now() + timedelta(minutes=max(1, minutes))

    @transaction.atomic
    def create_session(
        self,
        *,
        reservation: AnalysisReservation,
        user,
        client_ip: str | None = None,
        browser: str = "",
        client_platform: str = "",
        wait_for_prepare: bool = False,
    ) -> RemoteDesktopSession:
        if not can_create_for_reservation(user, reservation):
            raise SessionError("Not authorized for this reservation", code="forbidden")

        gate = evaluate_session_create_gates(
            reservation=reservation,
            user=user,
            client_ip=client_ip,
            settings_obj=self.settings,
        )
        if not gate.ok:
            gate.raise_session_error()

        ws = reservation.workstation
        if not workstation_healthy_for_session(ws):
            record_event(
                category=AuditCategory.SESSION,
                action="SessionAuthzRejected",
                details=f"workstation_unhealthy ip={client_ip or ''}",
                success=False,
                workstation=ws,
                actor=user,
                correlation_id=str(reservation.id),
            )
            raise SessionError("Workstation is not healthy or agent is offline", code="workstation_unhealthy")

        open_count = RemoteDesktopSession.objects.filter(status__in=OPEN_SESSION_STATUSES).count()
        if open_count >= self.settings.max_concurrent_sessions:
            raise SessionError("Maximum concurrent sessions reached", code="capacity")

        existing = find_reusable_open_session(reservation, settings_obj=self.settings)
        if existing:
            return existing

        session = RemoteDesktopSession(
            reservation=reservation,
            booking=reservation.booking,
            user=reservation.user,
            workstation=ws,
            status=SessionStatus.CREATED,
            client_ip=client_ip,
            browser=(browser or "")[:255],
            client_platform=(client_platform or "")[:255],
        )
        self._apply_policies(session)
        session.save()
        SessionStateHistory.objects.create(session=session, from_status="", to_status=SessionStatus.CREATED, reason="Created")
        SessionStatistics.objects.get_or_create(session=session)
        audit_session(session, "Create", details=f"reservation={reservation.id}", actor=user)
        try:
            from iic_booking.remote_analysis.collaboration.hooks import on_session_created

            on_session_created(session, actor=user)
        except Exception:
            pass

        # Milestone 5+: ensure isolated analysis workspace, seed booking results, prepare with input sync
        try:
            from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService

            sync_svc = WorkspaceSyncService()
            workspace = sync_svc.ensure_for_reservation(reservation, actor=user)
            if workspace.workstation_id is None:
                workspace.workstation = ws
                workspace.save(update_fields=["workstation", "updated_at"])
            workspace_id = str(workspace.id)
            local_path = workspace.local_agent_path
            prepare_payload = sync_svc.prepare_payload(workspace, session_id=str(session.id))
            sync_svc.set_sync_phase(
                workspace,
                "DownloadingInput",
                percent=25,
                message="Preparing workstation and downloading input",
            )
            # Nothing to download — do not leave the UI stuck on "Synchronizing input data".
            manifest_files = (prepare_payload.get("manifest") or {}).get("files") or []
            if not manifest_files:
                sync_svc.mark_prepared(
                    workspace,
                    success=True,
                    message="No input files to synchronize",
                )
        except Exception:
            logger.exception("Workspace ensure/ingest failed during session create")
            workspace_id = ""
            local_path = ""
            prepare_payload = {
                "session_id": str(session.id),
                "reservation_id": str(reservation.id),
                "workspace_id": workspace_id,
                "local_path": local_path,
            }

        ReservationHistory.objects.create(
            reservation=reservation,
            from_status=reservation.status,
            to_status=ReservationStatus.PREPARING,
            reason="Session create — prepare workstation",
            changed_by=user if getattr(user, "pk", None) else None,
        )
        reservation.status = ReservationStatus.PREPARING
        reservation.save(update_fields=["status", "updated_at"])

        self.transition(session, SessionStatus.PREPARING, reason="Issuing PREPARE_WORKSTATION")
        WorkstationStateHistory.objects.create(
            workstation=ws,
            from_status=ws.status,
            to_status=WorkstationStatus.PREPARING,
            reason="Remote desktop session prepare",
        )
        ws.status = WorkstationStatus.PREPARING
        ws.save(update_fields=["status", "updated_at"])

        cmd = CommandService().create_command(
            ws,
            CommandType.PREPARE_WORKSTATION,
            payload=prepare_payload,
            created_by=user if getattr(user, "pk", None) else None,
        )
        session.prepare_command = cmd
        session.save(update_fields=["prepare_command", "updated_at"])
        audit_session(session, "PrepareIssued", details=str(cmd.id), actor=user)

        refresh_session_health(session)

        if wait_for_prepare:
            self._wait_prepare(session)
        else:
            # In mock / fast path, advance if command already completed or mock guacamole
            self.try_advance_after_prepare(session)

        return RemoteDesktopSession.objects.get(pk=session.pk)

    def _wait_prepare(self, session: RemoteDesktopSession, timeout: int | None = None) -> None:
        import time

        timeout = timeout or self.settings.prepare_timeout_seconds
        deadline = time.time() + timeout
        while time.time() < deadline:
            session.refresh_from_db()
            if self.try_advance_after_prepare(session):
                return
            time.sleep(1)
        self.fail_session(session, "Preparation timeout")

    def try_advance_after_prepare(self, session: RemoteDesktopSession) -> bool:
        """Advance PREPARING → READY → Guacamole connection + token when prepare succeeds."""
        session.refresh_from_db()
        if session.status != SessionStatus.PREPARING:
            return session.status in {
                SessionStatus.READY,
                SessionStatus.TOKEN_GENERATED,
                SessionStatus.LAUNCHED,
                SessionStatus.CONNECTED,
                SessionStatus.ACTIVE,
            }

        cmd = session.prepare_command
        prepare_ok = False
        if self.settings.mock_guacamole and (cmd is None or cmd.status in {CommandStatus.PENDING, CommandStatus.DELIVERED, CommandStatus.COMPLETED}):
            # Dev: allow progression without waiting forever; still prefer COMPLETED when present
            if cmd is None or cmd.status == CommandStatus.COMPLETED or self.settings.mock_guacamole:
                prepare_ok = True
        if cmd and cmd.status == CommandStatus.COMPLETED:
            prepare_ok = True
        if cmd and cmd.status == CommandStatus.FAILED:
            try:
                from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
                from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace

                ws_obj = AnalysisWorkspace.objects.filter(reservation=session.reservation).first()
                if ws_obj:
                    WorkspaceSyncService().mark_prepared(
                        ws_obj, success=False, message=cmd.error_message or "Prepare failed"
                    )
            except Exception:
                pass
            self.fail_session(session, cmd.error_message or "Prepare failed")
            return False
        if not prepare_ok:
            # Zero-input shortcut: InputReady already set and booking has no RAW —
            # do not block Guacamole forever waiting for agent ack.
            try:
                from iic_booking.equipment.remote_analysis_integration.raw_staging import (
                    BookingRawStagingService,
                )
                from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
                from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace, WorkspaceFile

                ws_obj = AnalysisWorkspace.objects.filter(reservation=session.reservation).first()
                booking = getattr(session.reservation, "booking", None)
                if (
                    ws_obj
                    and booking is not None
                    and WorkspaceSyncService().is_input_ready(ws_obj)
                    and (cmd is None or cmd.status != CommandStatus.FAILED)
                    and not BookingRawStagingService().has_raw_files(booking)
                ):
                    prepare_ok = True
            except Exception:
                logger.exception("Empty-input prepare shortcut failed")
        if not prepare_ok:
            return False

        # Gate RDP: workspace must reach InputReady after verified input sync
        try:
            from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
            from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace

            ws_obj = AnalysisWorkspace.objects.filter(reservation=session.reservation).first()
            if ws_obj:
                sync_svc = WorkspaceSyncService()
                if self.settings.mock_guacamole and not sync_svc.is_input_ready(ws_obj):
                    sync_svc.mark_prepared(ws_obj, success=True, message="Mock prepare ready")
                    ws_obj.refresh_from_db()
                if not sync_svc.is_input_ready(ws_obj):
                    if cmd and cmd.status == CommandStatus.COMPLETED:
                        # Agent reported prepare complete with verified input — promote
                        sync_svc.mark_prepared(ws_obj, success=True, message="Prepare completed")
                        ws_obj.refresh_from_db()
                    if not sync_svc.is_input_ready(ws_obj):
                        return False
                sync_svc.mark_session_starting(ws_obj)
        except Exception:
            logger.exception("Workspace InputReady gate check failed")
            return False

        prepare_latency = None
        if cmd and cmd.completed_at and session.created_at:
            prepare_latency = (cmd.completed_at - session.created_at).total_seconds() * 1000

        self.transition(session, SessionStatus.READY, reason="Workstation prepared")
        reservation = session.reservation
        if reservation.status == ReservationStatus.PREPARING:
            ReservationHistory.objects.create(
                reservation=reservation,
                from_status=reservation.status,
                to_status=ReservationStatus.READY,
                reason="Workstation prepared for remote desktop",
            )
            reservation.status = ReservationStatus.READY
            reservation.save(update_fields=["status", "updated_at"])

        # Input sync is part of PREPARE; do not issue a second SYNC that races Guacamole launch

        try:
            self._provision_guacamole(session)
        except Exception as exc:
            logger.exception("Guacamole provision failed")
            self.fail_session(session, str(exc))
            return False

        SessionStatistics.objects.update_or_create(
            session=session,
            defaults={"prepare_latency_ms": prepare_latency},
        )
        SessionTelemetry.objects.create(
            metric_name="prepare_latency_ms",
            value=float(prepare_latency or 0),
            unit="ms",
            session=session,
        )
        return True

    def _provision_guacamole(self, session: RemoteDesktopSession) -> None:
        ConnectionManager(self.settings).create_ephemeral(session)
        self.transition(session, SessionStatus.TOKEN_GENERATED, reason="Ephemeral Guacamole connection created")
        audit_session(session, "GuacamoleProvisioned", details="ephemeral connection")
        record_event(
            category=AuditCategory.GUACAMOLE,
            action="ConnectionCreated",
            details=str(session.id),
            workstation=session.workstation,
            correlation_id=str(session.id),
        )

    def issue_launch_token(self, session: RemoteDesktopSession, *, user, client_ip: str | None = None) -> tuple[SessionToken, str]:
        if not can_launch_session(user, session):
            raise SessionError("Only the reservation owner may launch this session", code="forbidden")

        gate = evaluate_session_launch_gates(
            session=session,
            user=user,
            client_ip=client_ip,
            settings_obj=self.settings,
        )
        if not gate.ok:
            gate.raise_session_error()

        if session.status not in {
            SessionStatus.TOKEN_GENERATED,
            SessionStatus.READY,
            SessionStatus.LAUNCHED,
            SessionStatus.CONNECTING,
            SessionStatus.CONNECTED,
            SessionStatus.ACTIVE,
            SessionStatus.IDLE,
        }:
            # Allow launch after prepare advance
            if session.status == SessionStatus.PREPARING:
                self.try_advance_after_prepare(session)
                session.refresh_from_db()
            if session.status not in {SessionStatus.TOKEN_GENERATED, SessionStatus.READY, SessionStatus.LAUNCHED}:
                raise SessionError(f"Session not ready to launch (status={session.status})", code="not_ready")

        plaintext = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
        lifetime = self.settings.launch_token_lifetime_seconds or 90
        bound_ip = client_ip if self.settings.bind_token_to_ip else None
        row = SessionToken.objects.create(
            session=session,
            token_hash=hash_session_token(plaintext),
            token_prefix=plaintext[:8],
            expires_at=timezone.now() + timedelta(seconds=lifetime),
            bound_user=user,
            bound_ip=bound_ip,
            is_single_use=True,
        )
        audit_session(session, "TokenGenerated", details=row.token_prefix, actor=user)
        if session.status in {SessionStatus.READY, SessionStatus.TOKEN_GENERATED}:
            self.transition(session, SessionStatus.TOKEN_GENERATED, reason="Launch token issued")
        return row, plaintext

    def consume_token(self, session: RemoteDesktopSession, plaintext: str, *, user, client_ip: str | None = None) -> SessionToken:
        now = timezone.now()
        token_hash = hash_session_token(plaintext)
        token = SessionToken.objects.select_related("bound_user").filter(session=session, token_hash=token_hash).first()
        if not token:
            raise SessionError("Invalid session token", code="invalid_token")
        if token.revoked_at:
            raise SessionError("Token revoked", code="token_revoked")
        if token.consumed_at:
            raise SessionError("Token already used", code="token_replay")
        if token.expires_at < now:
            raise SessionError("Token expired", code="token_expired")
        # Prefer bound_user when caller is anonymous (iframe / AllowAny connect).
        # If an authenticated user is present, it must match the token binding.
        if user is not None and getattr(user, "is_authenticated", False):
            if token.bound_user_id != getattr(user, "pk", None):
                raise SessionError("Token bound to another user", code="token_user_mismatch")
        elif not token.bound_user_id:
            raise SessionError("Token has no bound user", code="invalid_token")
        if token.bound_ip and client_ip and token.bound_ip != client_ip:
            raise SessionError("Token IP mismatch", code="token_ip_mismatch")

        token.consumed_at = now
        token.save(update_fields=["consumed_at"])
        return token

    def build_launch_payload(
        self,
        session: RemoteDesktopSession,
        *,
        user,
        request_absolute_uri_builder,
        client_ip: str | None = None,
        user_agent: str = "",
        redirect: bool = False,
    ) -> dict[str, Any]:
        """
        Returns a Portal-owned launch URL. Never returns Guacamole admin URLs,
        workstation IPs, or RDP credentials.
        """
        if session.status == SessionStatus.PREPARING:
            self.try_advance_after_prepare(session)
            session.refresh_from_db()

        _, plaintext = self.issue_launch_token(session, user=user, client_ip=client_ip)
        qs = urlencode({"t": plaintext})
        launch_path = f"/api/v1/analysis/session/{session.id}/connect/?{qs}"
        # Prefer relative path for SPA; also provide absolute when builder given
        launch_url = request_absolute_uri_builder(launch_path) if request_absolute_uri_builder else launch_path

        SessionLaunch.objects.create(
            session=session,
            client_ip=client_ip,
            user_agent=(user_agent or "")[:512],
            success=True,
            detail="Launch URL issued",
        )
        session.launch_time = timezone.now()
        session.save(update_fields=["launch_time", "updated_at"])
        self.transition(session, SessionStatus.LAUNCHED, reason="Browser launch URL issued")
        audit_session(session, "Launch", details="URL issued", actor=user)

        payload: dict[str, Any] = {
            "session_id": str(session.id),
            "status": session.status,
            "launch_url": launch_url,
            "expires_in_seconds": self.settings.launch_token_lifetime_seconds,
            "mock": bool(self.settings.mock_guacamole),
            "redirect": redirect,
        }
        return payload

    def connect_with_token(
        self,
        session: RemoteDesktopSession,
        plaintext: str,
        *,
        user,
        client_ip: str | None = None,
        user_agent: str = "",
    ) -> dict[str, Any]:
        """
        Consume one-time token and return client connection info.
        Guacamole credentials are exchanged server-side; browser receives only a
        short-lived client auth token + connection id when not in mock mode.
        """
        with transaction.atomic():
            token_row = self.consume_token(session, plaintext, user=user, client_ip=client_ip)
            acting_user = user if (user is not None and getattr(user, "is_authenticated", False)) else token_row.bound_user
            self.transition(session, SessionStatus.CONNECTING, reason="Token consumed")

        session.browser = (user_agent or "")[:255]
        session.last_activity_at = timezone.now()
        session.save(update_fields=["browser", "last_activity_at", "updated_at"])

        result: dict[str, Any] = {
            "session_id": str(session.id),
            "status": SessionStatus.CONNECTING,
            "mock": bool(self.settings.mock_guacamole),
            "display": {
                "width": session.display_width,
                "height": session.display_height,
                "color_depth": session.color_depth,
                "audio": session.audio_enabled,
                "clipboard": session.clipboard_enabled,
                "file_transfer": session.file_transfer_enabled,
            },
        }

        if self.settings.mock_guacamole:
            self.mark_connected(session)
            result["status"] = session.status
            result["mock_desktop"] = True
            result["message"] = "Mock Guacamole session — no remote host contacted."
            return result

        # Live Guacamole: mint user token server-side for the ephemeral user
        try:
            conn = session.guacamole_connection
            from iic_booking.remote_analysis.guacamole.client import GuacamoleClient, encode_client_identifier

            client = GuacamoleClient(self.settings)
            temp_password = ConnectionManager(self.settings).ephemeral_password(conn)
            if not temp_password:
                raise SessionError("Ephemeral Guacamole credentials missing — recreate session", code="guac_creds")
            user_token = client.create_user_token(conn.guacamole_username, temp_password)
            # Public base URL only (no admin credentials). Client uses Guacamole redirect.
            public_base = (self.settings.guacamole_base_url or "").rstrip("/")
            client_id = encode_client_identifier(
                str(conn.guacamole_connection_id),
                data_source=self.settings.guacamole_data_source or "postgresql",
            )
            result["client"] = {
                "guacamole_token": user_token,
                "connection_id": conn.guacamole_connection_id,
                "client_url": f"{public_base}/#/client/{client_id}?token={user_token}" if public_base else "",
            }
            self.mark_connected(session)
            result["status"] = session.status
            # Do not include guacamole_base_url separately if empty; never include API URL
            result["redirect_url"] = result["client"].get("client_url") or ""
            meta = getattr(conn, "metadata", None) or {}
            logger.info(
                "Guacamole client URL issued session=%s rdp_username_injected=%s rdp_password_injected=%s client_url_set=%s actor=%s",
                session.id,
                meta.get("rdp_username_injected"),
                meta.get("rdp_password_injected"),
                bool(result["redirect_url"]),
                getattr(acting_user, "pk", None),
            )
        except Exception as exc:
            logger.exception("connect_with_token Guacamole error")
            raise SessionError(str(exc), code="guac_connect_failed") from exc

        return result

    def mark_connected(self, session: RemoteDesktopSession) -> RemoteDesktopSession:
        now = timezone.now()
        if not session.connected_at:
            session.connected_at = now
            latency = None
            if session.launch_time:
                latency = (now - session.launch_time).total_seconds() * 1000
            SessionStatistics.objects.update_or_create(
                session=session,
                defaults={"launch_latency_ms": latency},
            )
            SessionTelemetry.objects.create(
                metric_name="launch_latency_ms",
                value=float(latency or 0),
                unit="ms",
                session=session,
            )
        session.last_activity_at = now
        session.save(update_fields=["connected_at", "last_activity_at", "updated_at"])
        self.transition(session, SessionStatus.CONNECTED, reason="Browser connected")
        self.transition(session, SessionStatus.ACTIVE, reason="Session active")

        reservation = session.reservation
        if reservation.status in {ReservationStatus.READY, ReservationStatus.PREPARING, ReservationStatus.RESERVED}:
            ReservationHistory.objects.create(
                reservation=reservation,
                from_status=reservation.status,
                to_status=ReservationStatus.ACTIVE,
                reason="Remote desktop connected",
            )
            reservation.status = ReservationStatus.ACTIVE
            reservation.save(update_fields=["status", "updated_at"])

        ws = session.workstation
        if ws.status != WorkstationStatus.BUSY:
            WorkstationStateHistory.objects.create(
                workstation=ws,
                from_status=ws.status,
                to_status=WorkstationStatus.BUSY,
                reason="Remote desktop active",
            )
            ws.status = WorkstationStatus.BUSY
            ws.save(update_fields=["status", "updated_at"])

        audit_session(session, "Connected")
        ConnectionHistory.objects.create(session=session, event="connected", detail="")
        return session

    def heartbeat_activity(self, session: RemoteDesktopSession, *, bytes_in: int = 0, bytes_out: int = 0) -> None:
        session.last_activity_at = timezone.now()
        if session.status == SessionStatus.IDLE:
            self.transition(session, SessionStatus.ACTIVE, reason="Activity resumed")
        session.save(update_fields=["last_activity_at", "updated_at"])
        if bytes_in or bytes_out:
            stats, _ = SessionStatistics.objects.get_or_create(session=session)
            stats.bytes_in += max(0, bytes_in)
            stats.bytes_out += max(0, bytes_out)
            stats.save(update_fields=["bytes_in", "bytes_out", "updated_at"])

    def terminate(self, session: RemoteDesktopSession, *, user=None, reason: str = "Terminated") -> RemoteDesktopSession:
        if user is not None and not can_terminate_session(user, session):
            raise SessionError("Not authorized to terminate this session", code="forbidden")
        if session.status in {
            SessionStatus.COMPLETED,
            SessionStatus.TERMINATED,
            SessionStatus.EXPIRED,
            SessionStatus.FAILED,
        }:
            return session
        self.transition(session, SessionStatus.DISCONNECTING, reason=reason)
        from iic_booking.remote_analysis.guacamole.cleanup import SessionCleanupService

        cleaned = SessionCleanupService().cleanup(
            session,
            reason=reason,
            actor=user,
            final_status=SessionStatus.TERMINATED,
        )
        try:
            from iic_booking.remote_analysis.collaboration.hooks import on_session_terminated

            on_session_terminated(cleaned, actor=user, reason=reason)
        except Exception:
            pass
        return cleaned

    def fail_session(self, session: RemoteDesktopSession, reason: str) -> RemoteDesktopSession:
        session.failure_detail = reason[:2000]
        session.save(update_fields=["failure_detail", "updated_at"])
        self.transition(session, SessionStatus.FAILED, reason=reason)
        audit_session(session, "Failed", details=reason, success=False)
        from iic_booking.remote_analysis.guacamole.cleanup import SessionCleanupService

        return SessionCleanupService().cleanup(
            session,
            reason=reason,
            final_status=SessionStatus.FAILED,
            release_reservation=False,
        )
