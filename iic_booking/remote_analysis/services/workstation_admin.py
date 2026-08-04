"""Workstation admin lifecycle — enable / disable / maintenance states."""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from iic_booking.remote_analysis.constants import AuditCategory, MaintenanceKind, WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation, WorkstationStateHistory
from iic_booking.remote_analysis.services.audit import record_event
from iic_booking.remote_analysis.services.health import update_workstation_health
from iic_booking.remote_analysis.services.maintenance import MaintenanceService


class WorkstationAdminService:
    @transaction.atomic
    def set_maintenance(
        self,
        workstation: AnalysisWorkstation,
        *,
        actor=None,
        reason: str = "",
        kind: str = MaintenanceKind.MAINTENANCE,
        end=None,
        description: str = "",
        assigned_engineer: str = "",
        amc_reference: str = "",
        ticket_number: str = "",
        maintenance_notes: str = "",
    ) -> AnalysisWorkstation:
        end_dt = end or (timezone.now() + timedelta(hours=4))
        MaintenanceService().schedule(
            workstation=workstation,
            kind=kind or MaintenanceKind.MAINTENANCE,
            start=timezone.now(),
            end=end_dt,
            reason=reason or "Maintenance mode",
            description=description,
            assigned_engineer=assigned_engineer,
            amc_reference=amc_reference,
            ticket_number=ticket_number,
            maintenance_notes=maintenance_notes,
            actor=actor,
            apply_immediately=True,
        )
        workstation.refresh_from_db()
        return workstation

    @transaction.atomic
    def enable(self, workstation: AnalysisWorkstation, *, actor=None) -> AnalysisWorkstation:
        workstation.enabled = True
        workstation.save(update_fields=["enabled", "updated_at"])
        return self._set_status(
            workstation,
            WorkstationStatus.AVAILABLE,
            actor=actor,
            reason="Enabled",
            category=AuditCategory.CONFIGURATION,
            action="Enabled",
        )

    @transaction.atomic
    def disable(self, workstation: AnalysisWorkstation, *, actor=None) -> AnalysisWorkstation:
        workstation.enabled = False
        workstation.save(update_fields=["enabled", "updated_at"])
        return self._set_status(
            workstation,
            WorkstationStatus.DISABLED,
            actor=actor,
            reason="Disabled",
            category=AuditCategory.CONFIGURATION,
            action="Disabled",
        )

    def _set_status(
        self,
        workstation: AnalysisWorkstation,
        to_status: str,
        *,
        actor=None,
        reason: str = "",
        category: str = AuditCategory.STATUS,
        action: str = "StatusChange",
    ) -> AnalysisWorkstation:
        from_status = workstation.status
        if from_status != to_status:
            WorkstationStateHistory.objects.create(
                workstation=workstation,
                from_status=from_status,
                to_status=to_status,
                reason=reason,
                changed_by=actor if getattr(actor, "pk", None) else None,
            )
            workstation.status = to_status
            workstation.save(update_fields=["status", "updated_at"])
        record_event(
            category=category,
            action=action,
            details=reason or f"{from_status}->{to_status}",
            workstation=workstation,
            actor=actor,
        )
        update_workstation_health(workstation)
        return workstation
