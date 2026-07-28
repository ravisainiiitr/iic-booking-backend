"""Reservation lifecycle — create, transition, cancel, extend, release."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Max, Min
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from iic_booking.remote_analysis.constants import AuditCategory, ReservationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.scheduler_models import (
    AnalysisReservation,
    ReservationAudit,
    ReservationEvent,
    ReservationHistory,
    SoftwareRequirement,
)
from iic_booking.remote_analysis.services.allocation import AllocationService
from iic_booking.remote_analysis.services.audit import record_event
from iic_booking.remote_analysis.services.queue import QueueService


TERMINAL = {
    ReservationStatus.COMPLETED,
    ReservationStatus.EXPIRED,
    ReservationStatus.CANCELLED,
    ReservationStatus.FAILED,
}


class ReservationService:
    def __init__(self):
        self.allocation = AllocationService()
        self.queue = QueueService()

    def transition(
        self,
        reservation: AnalysisReservation,
        to_status: str,
        *,
        reason: str = "",
        actor=None,
    ) -> AnalysisReservation:
        from_status = reservation.status
        if from_status == to_status:
            return reservation
        reservation.status = to_status
        reservation.save(update_fields=["status", "updated_at"])
        ReservationHistory.objects.create(
            reservation=reservation,
            from_status=from_status,
            to_status=to_status,
            reason=reason,
            changed_by=actor if getattr(actor, "pk", None) else None,
        )
        ReservationEvent.objects.create(
            reservation=reservation,
            event_type=f"STATUS:{to_status}",
            details=reason,
        )
        ReservationAudit.objects.create(
            reservation=reservation,
            action=f"Status:{to_status}",
            details=reason or f"{from_status}->{to_status}",
            actor=actor if getattr(actor, "pk", None) else None,
        )
        record_event(
            category=AuditCategory.RESERVATION,
            action=to_status,
            details=reason,
            workstation=reservation.workstation,
            actor=actor,
            correlation_id=str(reservation.id),
        )
        return reservation

    def _booking_window(self, booking) -> tuple[datetime, datetime] | None:
        agg = booking.daily_slots.aggregate(start=Min("start_datetime"), end=Max("end_datetime"))
        if agg["start"] and agg["end"]:
            return agg["start"], agg["end"]
        return None

    @transaction.atomic
    def create_reservation(
        self,
        *,
        user,
        requested_start: datetime,
        requested_end: datetime,
        booking=None,
        department=None,
        software_profile: SoftwareRequirement | None = None,
        requested_capabilities: dict | None = None,
        requested_resources: dict | None = None,
        priority: int | None = None,
        created_by=None,
        auto_allocate: bool = True,
    ) -> AnalysisReservation:
        if requested_end <= requested_start:
            raise ValueError("requested_end must be after requested_start")

        if booking is not None:
            existing = AnalysisReservation.objects.filter(booking=booking).exclude(status__in=TERMINAL).first()
            if existing:
                raise ValueError("An active analysis reservation already exists for this booking")
            window = self._booking_window(booking)
            if window:
                requested_start, requested_end = window
            if department is None:
                department = getattr(booking.user, "department", None) or getattr(
                    booking.equipment, "internal_department", None
                )
            if user is None:
                user = booking.user

        boost = self.allocation.priority_boost(
            department_id=getattr(department, "id", department),
            user=user,
        )
        effective_priority = (priority if priority is not None else 100) - boost

        reservation = AnalysisReservation.objects.create(
            booking=booking,
            user=user,
            department=department,
            status=ReservationStatus.REQUESTED,
            requested_start=requested_start,
            requested_end=requested_end,
            priority=effective_priority,
            software_profile=software_profile,
            requested_capabilities=requested_capabilities or {},
            requested_resources=requested_resources or {},
            created_by=created_by,
        )
        ReservationAudit.objects.create(
            reservation=reservation,
            action="Created",
            details=f"booking={getattr(booking, 'booking_id', None)}",
            actor=created_by if getattr(created_by, "pk", None) else None,
        )
        record_event(
            category=AuditCategory.RESERVATION,
            action="Created",
            details=str(reservation.id),
            actor=created_by,
            correlation_id=str(reservation.id),
        )
        self.transition(reservation, ReservationStatus.VALIDATING, reason="Validating request", actor=created_by)

        if auto_allocate:
            from iic_booking.remote_analysis.services.scheduler import SchedulerService

            return SchedulerService().allocate(reservation, actor=created_by)

        self.transition(reservation, ReservationStatus.QUEUED, reason="Queued for allocation", actor=created_by)
        self.queue.enqueue(reservation)
        return reservation

    @transaction.atomic
    def cancel(self, reservation: AnalysisReservation, *, actor=None, reason: str = "Cancelled") -> AnalysisReservation:
        if reservation.status in TERMINAL:
            raise ValueError(f"Cannot cancel reservation in status {reservation.status}")
        self.queue.cancel(reservation)
        if reservation.workstation_id and reservation.status in {
            ReservationStatus.RESERVED,
            ReservationStatus.PREPARING,
            ReservationStatus.READY,
            ReservationStatus.ACTIVE,
        }:
            self.release(reservation, actor=actor, reason=reason, final_status=ReservationStatus.CANCELLED)
            return reservation
        return self.transition(reservation, ReservationStatus.CANCELLED, reason=reason, actor=actor)

    @transaction.atomic
    def extend(
        self,
        reservation: AnalysisReservation,
        new_end: datetime,
        *,
        actor=None,
    ) -> AnalysisReservation:
        if reservation.status not in {
            ReservationStatus.RESERVED,
            ReservationStatus.PREPARING,
            ReservationStatus.READY,
            ReservationStatus.ACTIVE,
        }:
            raise ValueError("Only reserved/active reservations can be extended")
        if new_end <= (reservation.reserved_end or reservation.requested_end):
            raise ValueError("new_end must be after current reserved_end")

        from iic_booking.remote_analysis.services.conflicts import ConflictResolver

        start = reservation.reserved_start or reservation.requested_start
        if reservation.workstation_id:
            conflicts = ConflictResolver().detect_for_window(
                reservation.workstation,
                start,
                new_end,
                reservation=reservation,
            )
            blocking = [c for c in conflicts if c.conflict_type != "MANUAL_OVERRIDE"]
            if blocking:
                ConflictResolver().persist_conflicts(reservation, blocking)
                raise ValueError("Extension conflicts with existing reservation or maintenance")

        reservation.reserved_end = new_end
        reservation.requested_end = max(reservation.requested_end, new_end)
        reservation.save(update_fields=["reserved_end", "requested_end", "updated_at"])
        ReservationAudit.objects.create(
            reservation=reservation,
            action="Extended",
            details=f"new_end={new_end.isoformat()}",
            actor=actor if getattr(actor, "pk", None) else None,
        )
        ReservationEvent.objects.create(
            reservation=reservation,
            event_type="EXTENDED",
            details=new_end.isoformat(),
        )
        return reservation

    @transaction.atomic
    def release(
        self,
        reservation: AnalysisReservation,
        *,
        actor=None,
        reason: str = "Released",
        final_status: str = ReservationStatus.COMPLETED,
    ) -> AnalysisReservation:
        reservation.released_at = timezone.now()
        reservation.save(update_fields=["released_at", "updated_at"])
        ReservationAudit.objects.create(
            reservation=reservation,
            action="Released",
            details=reason,
            actor=actor if getattr(actor, "pk", None) else None,
        )
        record_event(
            category=AuditCategory.RESERVATION,
            action="Released",
            details=reason,
            workstation=reservation.workstation,
            actor=actor,
            correlation_id=str(reservation.id),
        )
        return self.transition(reservation, final_status, reason=reason, actor=actor)

    @staticmethod
    def parse_dt(value) -> datetime:
        if isinstance(value, datetime):
            return value
        parsed = parse_datetime(str(value))
        if parsed is None:
            raise ValueError(f"Invalid datetime: {value}")
        if timezone.is_naive(parsed):
            parsed = timezone.make_aware(parsed, timezone.get_current_timezone())
        return parsed
