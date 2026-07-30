"""Remote command queue service."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from iic_booking.remote_analysis.constants import AuditCategory, CommandStatus, CommandType
from iic_booking.remote_analysis.models import AnalysisWorkstation, CommandExecution, RemoteCommand
from iic_booking.remote_analysis.services.audit import record_event

logger = logging.getLogger(__name__)

SUPPORTED_COMMANDS = {c.value for c in CommandType}


class CommandService:
    def create_command(
        self,
        workstation: AnalysisWorkstation,
        command_type: str,
        *,
        payload: dict[str, Any] | None = None,
        created_by=None,
        expires_in_hours: int = 24,
    ) -> RemoteCommand:
        command_type = command_type.upper().strip()
        if command_type not in SUPPORTED_COMMANDS:
            raise ValueError(f"Unsupported command type: {command_type}")

        cmd = RemoteCommand.objects.create(
            workstation=workstation,
            command_type=command_type,
            status=CommandStatus.PENDING,
            payload=payload or {},
            created_by=created_by,
            expires_at=timezone.now() + timedelta(hours=expires_in_hours),
        )
        record_event(
            category=AuditCategory.COMMANDS,
            action="Created",
            details=command_type,
            workstation=workstation,
            actor=created_by,
            correlation_id=str(cmd.id),
        )
        return cmd

    @transaction.atomic
    def poll_pending(self, workstation: AnalysisWorkstation) -> list[RemoteCommand]:
        now = timezone.now()
        expired = RemoteCommand.objects.filter(
            workstation=workstation,
            status=CommandStatus.PENDING,
            expires_at__lt=now,
        )
        for cmd in expired:
            cmd.status = CommandStatus.EXPIRED
            cmd.save(update_fields=["status"])
            CommandExecution.objects.create(
                command=cmd,
                status=CommandStatus.EXPIRED,
                message="Command expired before delivery",
            )

        pending = list(
            RemoteCommand.objects.select_for_update()
            .filter(workstation=workstation, status=CommandStatus.PENDING)
            .order_by("created_at")[:20]
        )
        for cmd in pending:
            cmd.status = CommandStatus.DELIVERED
            cmd.delivered_at = now
            cmd.save(update_fields=["status", "delivered_at"])
            CommandExecution.objects.create(
                command=cmd,
                status=CommandStatus.DELIVERED,
                message="Delivered to agent",
            )
            workstation.current_command = cmd.command_type
        if pending:
            workstation.save(update_fields=["current_command", "updated_at"])
        return pending

    @transaction.atomic
    def complete(
        self,
        command: RemoteCommand,
        *,
        success: bool,
        message: str = "",
    ) -> RemoteCommand:
        now = timezone.now()
        command.status = CommandStatus.COMPLETED if success else CommandStatus.FAILED
        command.completed_at = now
        if not command.started_at:
            command.started_at = command.delivered_at or now
        command.result_message = message if success else command.result_message
        command.error_message = "" if success else message
        command.save(
            update_fields=[
                "status",
                "completed_at",
                "started_at",
                "result_message",
                "error_message",
            ]
        )
        duration = None
        if command.started_at:
            duration = (now - command.started_at).total_seconds() * 1000
        CommandExecution.objects.create(
            command=command,
            status=command.status,
            message=message,
            duration_ms=duration,
        )
        ws = command.workstation
        if ws.current_command == command.command_type:
            ws.current_command = ""
            ws.save(update_fields=["current_command", "updated_at"])

        record_event(
            category=AuditCategory.COMMANDS,
            action="Completed" if success else "Failed",
            details=message,
            success=success,
            workstation=ws,
            correlation_id=str(command.id),
        )

        # Milestone 4/5+: advance remote desktop session after PREPARE_WORKSTATION (+ input sync)
        if command.command_type == CommandType.PREPARE_WORKSTATION:
            try:
                from iic_booking.remote_analysis.guacamole.services import GuacamoleIntegrationService
                from iic_booking.remote_analysis.session_models import RemoteDesktopSession
                from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
                from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace

                workspace_id = (command.payload or {}).get("workspace_id")
                if workspace_id:
                    ws_obj = AnalysisWorkspace.objects.filter(pk=workspace_id).first()
                    if ws_obj:
                        WorkspaceSyncService().mark_prepared(ws_obj, success=success, message=message)

                for session in RemoteDesktopSession.objects.filter(prepare_command=command):
                    GuacamoleIntegrationService().retry_prepare(session)
            except Exception:
                logger.exception(
                    "Failed to advance workspace/session after PREPARE_WORKSTATION complete (%s)",
                    command.id,
                )

        # Milestone 5: mark workspace synced after SYNC/COLLECT
        if command.command_type in {CommandType.SYNC_WORKSPACE, CommandType.COLLECT_WORKSPACE}:
            try:
                from iic_booking.remote_analysis.workspace.sync import WorkspaceSyncService
                from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace

                workspace_id = (command.payload or {}).get("workspace_id")
                if workspace_id:
                    ws_obj = AnalysisWorkspace.objects.filter(pk=workspace_id).first()
                    if ws_obj:
                        WorkspaceSyncService().mark_synced(
                            ws_obj,
                            success=success,
                            message=message,
                        )
            except Exception:
                logger.exception(
                    "Failed to mark workspace synced after %s complete (%s)",
                    command.command_type,
                    command.id,
                )

        return command
