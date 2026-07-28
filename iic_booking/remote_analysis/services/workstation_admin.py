"""Workstation lifecycle actions: enable, disable, maintenance."""

from __future__ import annotations

from django.db import transaction

from iic_booking.remote_analysis.constants import AuditCategory, WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation, WorkstationStateHistory
from iic_booking.remote_analysis.services.audit import record_event
from iic_booking.remote_analysis.services.health import update_workstation_health


class WorkstationAdminService:
    @transaction.atomic
    def set_maintenance(self, workstation: AnalysisWorkstation, *, actor=None, reason: str = "") -> AnalysisWorkstation:
        return self._set_status(
            workstation,
            WorkstationStatus.MAINTENANCE,
            actor=actor,
            reason=reason or "Maintenance mode",
            category=AuditCategory.MAINTENANCE,
            action="Maintenance",
        )

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
