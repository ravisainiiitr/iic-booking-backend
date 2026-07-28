"""Conflict detection and resolution for reservations."""

from __future__ import annotations

from datetime import datetime

from django.db import transaction
from django.utils import timezone

from iic_booking.remote_analysis.constants import AuditCategory, ConflictType, ReservationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.scheduler_models import (
    AnalysisReservation,
    MaintenanceWindow,
    ReservationAudit,
    ReservationConflict,
)
from iic_booking.remote_analysis.services.audit import record_event
from iic_booking.remote_analysis.services.availability import AvailabilityEngine


ACTIVE_RESERVATION_STATUSES = {
    ReservationStatus.RESERVED,
    ReservationStatus.PREPARING,
    ReservationStatus.READY,
    ReservationStatus.ACTIVE,
}


class ConflictResolver:
    def __init__(self):
        self.availability = AvailabilityEngine()

    def detect_for_window(
        self,
        workstation: AnalysisWorkstation,
        start: datetime,
        end: datetime,
        *,
        reservation: AnalysisReservation | None = None,
    ) -> list[ReservationConflict]:
        conflicts: list[ReservationConflict] = []
        if self.availability.is_under_maintenance(workstation, start, end):
            conflicts.append(
                ReservationConflict(
                    reservation=reservation,
                    workstation=workstation,
                    conflict_type=ConflictType.MAINTENANCE_OVERLAP,
                    resolution="Blocked by maintenance window",
                )
            )
        if not self.availability.agent_online(workstation):
            conflicts.append(
                ReservationConflict(
                    reservation=reservation,
                    workstation=workstation,
                    conflict_type=ConflictType.WORKSTATION_OFFLINE,
                    resolution="Workstation offline or heartbeat timed out",
                )
            )
        overlapping = AnalysisReservation.objects.filter(
            workstation=workstation,
            status__in=ACTIVE_RESERVATION_STATUSES,
            reserved_start__lt=end,
            reserved_end__gt=start,
        )
        if reservation:
            overlapping = overlapping.exclude(pk=reservation.pk)
        for other in overlapping:
            conflicts.append(
                ReservationConflict(
                    reservation=reservation,
                    conflicting_reservation=other,
                    workstation=workstation,
                    conflict_type=ConflictType.DOUBLE_BOOKING,
                    resolution="Overlapping reservation",
                )
            )
        return conflicts

    @transaction.atomic
    def persist_conflicts(
        self,
        reservation: AnalysisReservation,
        conflicts: list[ReservationConflict],
    ) -> list[ReservationConflict]:
        saved = []
        for c in conflicts:
            c.reservation = reservation
            c.save()
            saved.append(c)
            record_event(
                category=AuditCategory.CONFLICT,
                action=c.conflict_type,
                details=c.resolution,
                success=False,
                workstation=c.workstation,
                correlation_id=str(reservation.id),
            )
            ReservationAudit.objects.create(
                reservation=reservation,
                action="ConflictDetected",
                details=f"{c.conflict_type}: {c.resolution}",
                success=False,
            )
        return saved

    @transaction.atomic
    def apply_priority_override(
        self,
        winner: AnalysisReservation,
        loser: AnalysisReservation,
        *,
        actor=None,
        reason: str = "Priority override",
    ) -> ReservationConflict:
        from iic_booking.remote_analysis.services.reservation import ReservationService

        ReservationService().transition(
            loser,
            ReservationStatus.CANCELLED,
            reason=reason,
            actor=actor,
        )
        conflict = ReservationConflict.objects.create(
            reservation=winner,
            conflicting_reservation=loser,
            workstation=winner.workstation,
            conflict_type=ConflictType.PRIORITY_OVERRIDE,
            resolution=reason,
            resolved=True,
        )
        ReservationAudit.objects.create(
            reservation=winner,
            action="PriorityOverride",
            details=reason,
            actor=actor if getattr(actor, "pk", None) else None,
            success=True,
        )
        record_event(
            category=AuditCategory.CONFLICT,
            action="PriorityOverride",
            details=reason,
            workstation=winner.workstation,
            actor=actor,
            correlation_id=str(winner.id),
        )
        return conflict

    def detect_all_active(self) -> int:
        """Scan active reservations for offline/maintenance conflicts."""
        count = 0
        now = timezone.now()
        for reservation in AnalysisReservation.objects.filter(
            status__in=ACTIVE_RESERVATION_STATUSES,
            workstation__isnull=False,
        ).select_related("workstation"):
            conflicts = self.detect_for_window(
                reservation.workstation,
                reservation.reserved_start or reservation.requested_start,
                reservation.reserved_end or reservation.requested_end,
                reservation=reservation,
            )
            # Only persist newly relevant offline/maintenance
            fresh = [
                c
                for c in conflicts
                if c.conflict_type in {ConflictType.WORKSTATION_OFFLINE, ConflictType.MAINTENANCE_OVERLAP}
            ]
            if fresh:
                self.persist_conflicts(reservation, fresh)
                count += len(fresh)
        # Also flag upcoming maintenance overlaps
        _ = MaintenanceWindow.objects.filter(active=True, end__gte=now).count()
        return count
