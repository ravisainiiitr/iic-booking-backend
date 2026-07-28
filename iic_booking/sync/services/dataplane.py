"""Operational data-plane services: equipment, bookings, workspaces, commands."""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Max, Min, Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from iic_booking.equipment.models import Booking, BookingStatus
from iic_booking.sync.exceptions import (
    SyncControlPlaneError,
)
from iic_booking.sync.models import (
    AgentCommand,
    AgentCommandStatus,
    BookingWorkspace,
    BookingWorkspaceStatus,
    DepartmentSyncAgent,
    SyncLogCategory,
    SyncLogSeverity,
)
from iic_booking.sync.services.logging import write_sync_log
from iic_booking.sync.services.scope import (
    agent_may_access_booking,
    assigned_profile_queryset,
    bookings_for_agent,
)
from iic_booking.sync.services.tokens import agent_expected_versions

# Milestone 5 SyncLog event codes
EVENT_EQUIPMENT_SYNCED = "SYNC-4001"
EVENT_BOOKINGS_DOWNLOADED = "SYNC-4002"
EVENT_WORKSPACE_CREATED = "SYNC-4003"
EVENT_WORKSPACE_EXISTS = "SYNC-4004"
EVENT_COMMANDS_DOWNLOADED = "SYNC-4005"
EVENT_COMMAND_ACKNOWLEDGED = "SYNC-4006"
EVENT_COMMAND_COMPLETED = "SYNC-4007"
EVENT_COMMAND_FAILED = "SYNC-4008"


class UnauthorizedResourceError(SyncControlPlaneError):
    code = "UNAUTHORIZED_RESOURCE"
    status_code = 403
    default_message = "Agent is not authorized for this resource."


class CommandNotFoundError(SyncControlPlaneError):
    code = "COMMAND_NOT_FOUND"
    status_code = 404
    default_message = "Command not found."


class CommandStateError(SyncControlPlaneError):
    code = "INVALID_COMMAND_STATE"
    status_code = 409
    default_message = "Invalid command state transition."


class BookingNotFoundError(SyncControlPlaneError):
    code = "BOOKING_NOT_FOUND"
    status_code = 404
    default_message = "Booking not found."


def _row_version_from_updated_at(updated_at: datetime | None) -> int:
    if updated_at is None:
        return 1
    return max(1, int(updated_at.timestamp()))


def _parse_modified_after(raw: str | None) -> datetime | None:
    if not raw:
        return None
    dt = parse_datetime(raw)
    if dt is None:
        raise SyncControlPlaneError(
            "Invalid modified_after timestamp.",
            code="INVALID_MODIFIED_AFTER",
        )
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone.get_current_timezone())
    return dt


class EquipmentSyncService:
    def list_for_agent(
        self,
        agent: DepartmentSyncAgent,
        *,
        modified_after: str | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        qs = assigned_profile_queryset(agent)
        cutoff = _parse_modified_after(modified_after)
        if cutoff is not None:
            qs = qs.filter(updated_at__gt=cutoff)

        config_version, schema_version = agent_expected_versions(agent)
        items = []
        for profile in qs:
            equipment = profile.equipment
            assignment = next(iter(profile.assignments.all()), None)
            primary_eq = None
            if assignment and assignment.sync_agent.equipment_id:
                primary_eq = assignment.sync_agent.equipment
            dept = equipment.internal_department
            items.append(
                {
                    "equipment_id": equipment.equipment_id,
                    "equipment_code": equipment.code,
                    "equipment_name": equipment.name,
                    "department": {
                        "name": dept.name if dept else None,
                        "code": dept.code if dept else None,
                    },
                    "primary_equipment": {
                        "equipment_id": primary_eq.equipment_id if primary_eq else None,
                        "name": primary_eq.name if primary_eq else None,
                        "code": primary_eq.code if primary_eq else None,
                    }
                    if primary_eq
                    else None,
                    "sync_profile": {
                        "watch_folder": profile.watch_folder,
                        "share_name": profile.share_name,
                        "hostname": profile.hostname,
                        "ip_address": profile.ip_address,
                        "unc_path": profile.unc_path,
                        "enabled_features": profile.enabled_features or {},
                        "sync_enabled": profile.sync_enabled,
                        "watch_enabled": profile.watch_enabled,
                        "upload_enabled": profile.upload_enabled,
                        "sync_interval_seconds": profile.sync_interval_seconds,
                        "configuration_version": profile.configuration_version,
                        "schema_version": profile.schema_version,
                    },
                    "assignment": {
                        "assigned_at": assignment.assigned_at.isoformat() if assignment else None,
                        "is_active": bool(assignment and assignment.is_active),
                    },
                    "updated_at": profile.updated_at.isoformat() if profile.updated_at else None,
                    "version": _row_version_from_updated_at(profile.updated_at),
                }
            )

        write_sync_log(
            event_code=EVENT_EQUIPMENT_SYNCED,
            message="Equipment synchronized",
            category=SyncLogCategory.SYNC,
            severity=SyncLogSeverity.INFO,
            sync_agent=agent,
            correlation_id=correlation_id,
            json_payload={"count": len(items), "modified_after": modified_after},
        )
        return {
            "count": len(items),
            "configuration_version": config_version,
            "schema_version": schema_version,
            "server_time": timezone.now().isoformat(),
            "results": items,
        }


class BookingSyncService:
    ACTIVE_STATUSES = {
        BookingStatus.BOOKED,
        BookingStatus.HOLD,
        BookingStatus.DISRUPTION_PENDING,
        BookingStatus.UNDER_MAINTENANCE,
        BookingStatus.OTHER_DISRUPTION,
        BookingStatus.PENDING,
        BookingStatus.PENDING_PAYMENT,
        BookingStatus.WAITLISTED,
    }

    def list_for_agent(
        self,
        agent: DepartmentSyncAgent,
        *,
        today: bool = False,
        active: bool = False,
        future: bool = False,
        booking_status: str | None = None,
        last_modified_after: str | None = None,
        modified_after: str | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        qs = bookings_for_agent(agent)
        now = timezone.now()
        today_start = timezone.make_aware(datetime.combine(now.date(), time.min))
        today_end = today_start + timedelta(days=1)

        if booking_status:
            qs = qs.filter(status=booking_status)
        if active:
            qs = qs.filter(status__in=self.ACTIVE_STATUSES)
        if today:
            qs = qs.filter(
                daily_slots__start_datetime__lt=today_end,
                daily_slots__end_datetime__gt=today_start,
            ).distinct()
        if future:
            qs = qs.filter(daily_slots__start_datetime__gte=now).distinct()

        cutoff = _parse_modified_after(last_modified_after or modified_after)
        if cutoff is not None:
            qs = qs.filter(updated_at__gt=cutoff)

        qs = qs.annotate(
            slot_start=Min("daily_slots__start_datetime"),
            slot_end=Max("daily_slots__end_datetime"),
        ).order_by("slot_start", "booking_id")

        config_version, _ = agent_expected_versions(agent)
        items = []
        for booking in qs:
            sample_status = None
            workspace_path = ""
            events = list(booking.sample_trace_events.all())
            if events:
                # Prefetched unordered — pick latest by id
                latest = max(events, key=lambda e: e.id)
                sample_status = latest.status
                workspace_path = latest.results_folder_path or ""

            user = booking.user
            items.append(
                {
                    "booking_id": booking.booking_id,
                    "display_id": booking.virtual_booking_id,
                    "equipment": {
                        "equipment_id": booking.equipment_id,
                        "equipment_code": booking.equipment.code,
                        "equipment_name": booking.equipment.name,
                    },
                    "user": {
                        "user_id": user.id,
                        "name": user.name or user.email,
                        "email": user.email,
                        "department": getattr(getattr(user, "department", None), "name", None),
                    },
                    "slot_start": booking.slot_start.isoformat() if booking.slot_start else None,
                    "slot_end": booking.slot_end.isoformat() if booking.slot_end else None,
                    "booking_status": booking.status,
                    "sample_status": sample_status,
                    "priority": "NORMAL",
                    "workspace_path": workspace_path,
                    "configuration_version": config_version,
                    "updated_at": booking.updated_at.isoformat() if booking.updated_at else None,
                    "version": _row_version_from_updated_at(booking.updated_at),
                }
            )

        write_sync_log(
            event_code=EVENT_BOOKINGS_DOWNLOADED,
            message="Bookings downloaded",
            category=SyncLogCategory.BOOKING,
            severity=SyncLogSeverity.INFO,
            sync_agent=agent,
            correlation_id=correlation_id,
            json_payload={"count": len(items)},
        )
        return {
            "count": len(items),
            "server_time": timezone.now().isoformat(),
            "results": items,
        }


class WorkspaceService:
    def create_or_get(
        self,
        agent: DepartmentSyncAgent,
        *,
        booking_id: int,
        correlation_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        try:
            booking = Booking.objects.select_related("equipment", "user", "user__department").get(
                booking_id=booking_id
            )
        except Booking.DoesNotExist as exc:
            raise BookingNotFoundError() from exc

        if not agent_may_access_booking(agent, booking):
            raise UnauthorizedResourceError("Booking is not assigned to this agent.")

        existing = BookingWorkspace.objects.filter(sync_agent=agent, booking=booking).first()
        if existing:
            write_sync_log(
                event_code=EVENT_WORKSPACE_EXISTS,
                message="Workspace already exists",
                category=SyncLogCategory.WORKSPACE,
                severity=SyncLogSeverity.INFO,
                sync_agent=agent,
                equipment=booking.equipment,
                correlation_id=correlation_id,
                json_payload={"booking_id": booking_id, "workspace_id": str(existing.id)},
            )
            return self._serialize(existing, created=False)

        with transaction.atomic():
            # Re-check inside transaction for races.
            existing = (
                BookingWorkspace.objects.select_for_update()
                .filter(sync_agent=agent, booking=booking)
                .first()
            )
            if existing:
                return self._serialize(existing, created=False)

            display = booking.virtual_booking_id or f"BOOKING-{booking.booking_id}"
            year = timezone.now().year
            user_dept = getattr(getattr(booking.user, "department", None), "name", None) or "Unknown"
            user_label = booking.user.name or booking.user.email or str(booking.user_id)
            relative = (
                f"{booking.equipment.code}/{year}/{user_dept}/{user_label}/{display}"
            ).replace("\\", "/")
            config_version, _ = agent_expected_versions(agent)
            workspace = BookingWorkspace.objects.create(
                sync_agent=agent,
                booking=booking,
                equipment=booking.equipment,
                workspace_name=display,
                relative_folder=relative,
                expected_result_folder=f"{relative}/Results",
                sample_folder=f"{relative}/Samples",
                status=BookingWorkspaceStatus.READY,
                configuration_version=config_version,
            )
            write_sync_log(
                event_code=EVENT_WORKSPACE_CREATED,
                message="Workspace created",
                category=SyncLogCategory.WORKSPACE,
                severity=SyncLogSeverity.INFO,
                sync_agent=agent,
                equipment=booking.equipment,
                correlation_id=correlation_id,
                json_payload={"booking_id": booking_id, "workspace_id": str(workspace.id)},
            )
            return self._serialize(workspace, created=True)

    def _serialize(self, workspace: BookingWorkspace, *, created: bool) -> dict[str, Any]:
        return {
            "created": created,
            "workspace_id": str(workspace.id),
            "booking_id": workspace.booking_id,
            "workspace_name": workspace.workspace_name,
            "relative_folder": workspace.relative_folder,
            "expected_result_folder": workspace.expected_result_folder,
            "sample_folder": workspace.sample_folder,
            "status": workspace.status,
            "configuration_version": workspace.configuration_version,
            "version": workspace.version,
            "updated_at": workspace.updated_at.isoformat() if workspace.updated_at else None,
        }


class CommandService:
    PENDING_STATUSES = {
        AgentCommandStatus.PENDING,
        AgentCommandStatus.ACKNOWLEDGED,
        AgentCommandStatus.RUNNING,
    }

    def list_for_agent(
        self,
        agent: DepartmentSyncAgent,
        *,
        status_filter: str | None = None,
        priority: str | None = None,
        created_after: str | None = None,
        command_type: str | None = None,
        modified_after: str | None = None,
        pending_only: bool = True,
        correlation_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        qs = AgentCommand.objects.filter(sync_agent=agent).select_related(
            "equipment",
            "booking",
        )
        if status_filter:
            qs = qs.filter(status=status_filter)
        elif pending_only:
            qs = qs.filter(status__in=self.PENDING_STATUSES)
        if priority:
            qs = qs.filter(priority=priority)
        if command_type:
            qs = qs.filter(command_type=command_type)
        created_cutoff = _parse_modified_after(created_after)
        if created_cutoff is not None:
            qs = qs.filter(created_at__gt=created_cutoff)
        modified_cutoff = _parse_modified_after(modified_after)
        if modified_cutoff is not None:
            qs = qs.filter(updated_at__gt=modified_cutoff)

        # Only return due commands (scheduled_at null or <= now)
        now = timezone.now()
        qs = qs.filter(Q(scheduled_at__isnull=True) | Q(scheduled_at__lte=now)).order_by(
            "-priority",
            "created_at",
        )

        items = [self._serialize(cmd) for cmd in qs[:500]]
        write_sync_log(
            event_code=EVENT_COMMANDS_DOWNLOADED,
            message="Commands downloaded",
            category=SyncLogCategory.COMMAND,
            severity=SyncLogSeverity.INFO,
            sync_agent=agent,
            correlation_id=correlation_id,
            json_payload={"count": len(items)},
        )
        return {
            "count": len(items),
            "server_time": timezone.now().isoformat(),
            "results": items,
        }

    def _get_agent_command(self, agent: DepartmentSyncAgent, command_id) -> AgentCommand:
        try:
            command = AgentCommand.objects.select_related("equipment", "booking").get(pk=command_id)
        except (AgentCommand.DoesNotExist, ValueError) as exc:
            raise CommandNotFoundError() from exc
        if command.sync_agent_id != agent.pk:
            raise UnauthorizedResourceError("Command is not assigned to this agent.")
        return command

    @transaction.atomic
    def acknowledge(
        self,
        agent: DepartmentSyncAgent,
        command_id,
        *,
        correlation_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        command = self._get_agent_command(agent, command_id)
        if command.status not in {AgentCommandStatus.PENDING, AgentCommandStatus.ACKNOWLEDGED}:
            raise CommandStateError(
                f"Cannot acknowledge command in status {command.status}."
            )
        now = timezone.now()
        command.status = AgentCommandStatus.ACKNOWLEDGED
        command.started_at = command.started_at or now
        command.bump_version()
        if correlation_id and not command.correlation_id:
            command.correlation_id = correlation_id
        command.save(
            update_fields=[
                "status",
                "started_at",
                "version",
                "correlation_id",
                "updated_at",
            ]
        )
        write_sync_log(
            event_code=EVENT_COMMAND_ACKNOWLEDGED,
            message="Command acknowledged",
            category=SyncLogCategory.COMMAND,
            severity=SyncLogSeverity.INFO,
            sync_agent=agent,
            equipment=command.equipment,
            correlation_id=correlation_id or command.correlation_id,
            json_payload={"command_id": str(command.id), "command_type": command.command_type},
        )
        return self._serialize(command)

    @transaction.atomic
    def complete(
        self,
        agent: DepartmentSyncAgent,
        command_id,
        *,
        result_payload: dict | None = None,
        correlation_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        command = self._get_agent_command(agent, command_id)
        if command.status in {AgentCommandStatus.COMPLETED, AgentCommandStatus.CANCELLED}:
            raise CommandStateError(f"Command already {command.status}.")
        if command.status == AgentCommandStatus.FAILED:
            raise CommandStateError("Cannot complete a failed command; re-queue instead.")
        now = timezone.now()
        command.status = AgentCommandStatus.COMPLETED
        command.completed_at = now
        command.started_at = command.started_at or now
        command.result_payload = result_payload or {}
        command.last_error = ""
        command.bump_version()
        command.save(
            update_fields=[
                "status",
                "completed_at",
                "started_at",
                "result_payload",
                "last_error",
                "version",
                "updated_at",
            ]
        )
        write_sync_log(
            event_code=EVENT_COMMAND_COMPLETED,
            message="Command completed",
            category=SyncLogCategory.COMMAND,
            severity=SyncLogSeverity.INFO,
            sync_agent=agent,
            equipment=command.equipment,
            correlation_id=correlation_id or command.correlation_id,
            json_payload={"command_id": str(command.id), "command_type": command.command_type},
        )
        return self._serialize(command)

    @transaction.atomic
    def fail(
        self,
        agent: DepartmentSyncAgent,
        command_id,
        *,
        failure_reason: str,
        error_details: dict | None = None,
        retry_recommended: bool = False,
        correlation_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        command = self._get_agent_command(agent, command_id)
        if command.status in {AgentCommandStatus.COMPLETED, AgentCommandStatus.CANCELLED}:
            raise CommandStateError(f"Cannot fail command in status {command.status}.")
        now = timezone.now()
        command.status = AgentCommandStatus.FAILED
        command.completed_at = now
        command.started_at = command.started_at or now
        command.retry_count = (command.retry_count or 0) + (1 if retry_recommended else 0)
        command.last_error = failure_reason
        command.result_payload = {
            "failure_reason": failure_reason,
            "error_details": error_details or {},
            "retry_recommended": retry_recommended,
        }
        command.bump_version()
        command.save(
            update_fields=[
                "status",
                "completed_at",
                "started_at",
                "retry_count",
                "last_error",
                "result_payload",
                "version",
                "updated_at",
            ]
        )
        write_sync_log(
            event_code=EVENT_COMMAND_FAILED,
            message=f"Command failed: {failure_reason}",
            category=SyncLogCategory.COMMAND,
            severity=SyncLogSeverity.ERROR,
            sync_agent=agent,
            equipment=command.equipment,
            correlation_id=correlation_id or command.correlation_id,
            json_payload={
                "command_id": str(command.id),
                "command_type": command.command_type,
                "retry_recommended": retry_recommended,
            },
        )
        return self._serialize(command)

    def _serialize(self, command: AgentCommand) -> dict[str, Any]:
        return {
            "command_id": str(command.id),
            "command_type": command.command_type,
            "priority": command.priority,
            "status": command.status,
            "payload": command.payload or {},
            "result_payload": command.result_payload or {},
            "correlation_id": str(command.correlation_id) if command.correlation_id else None,
            "equipment_id": command.equipment_id,
            "booking_id": command.booking_id,
            "created_at": command.created_at.isoformat() if command.created_at else None,
            "scheduled_at": command.scheduled_at.isoformat() if command.scheduled_at else None,
            "started_at": command.started_at.isoformat() if command.started_at else None,
            "completed_at": command.completed_at.isoformat() if command.completed_at else None,
            "retry_count": command.retry_count,
            "last_error": command.last_error,
            "updated_at": command.updated_at.isoformat() if command.updated_at else None,
            "version": command.version,
        }
