"""Priority-based reservation queue (FIFO within same priority)."""

from __future__ import annotations

from django.db import transaction
from django.utils import timezone

from iic_booking.remote_analysis.constants import AuditCategory, QueueEntryStatus, ReservationStatus
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation, ReservationAudit, ReservationQueue
from iic_booking.remote_analysis.services.audit import record_event


class QueueService:
    @transaction.atomic
    def enqueue(self, reservation: AnalysisReservation) -> ReservationQueue:
        entry, created = ReservationQueue.objects.get_or_create(
            reservation=reservation,
            defaults={
                "status": QueueEntryStatus.WAITING,
                "priority": reservation.priority,
            },
        )
        if not created:
            entry.status = QueueEntryStatus.WAITING
            entry.priority = reservation.priority
            entry.dequeued_at = None
            entry.save(update_fields=["status", "priority", "dequeued_at"])
        ReservationAudit.objects.create(
            reservation=reservation,
            action="Queued",
            details=f"priority={reservation.priority}",
        )
        record_event(
            category=AuditCategory.QUEUE,
            action="Enqueued",
            details=f"priority={reservation.priority}",
            correlation_id=str(reservation.id),
        )
        return entry

    def next_waiting(self, *, limit: int = 20) -> list[ReservationQueue]:
        return list(
            ReservationQueue.objects.select_related("reservation", "reservation__user", "reservation__software_profile")
            .filter(status=QueueEntryStatus.WAITING)
            .order_by("priority", "enqueued_at")[:limit]
        )

    @transaction.atomic
    def mark_allocating(self, entry: ReservationQueue) -> ReservationQueue:
        entry.status = QueueEntryStatus.ALLOCATING
        entry.save(update_fields=["status"])
        record_event(
            category=AuditCategory.QUEUE,
            action="Allocating",
            details="",
            correlation_id=str(entry.reservation_id),
        )
        return entry

    @transaction.atomic
    def mark_reserved(self, entry: ReservationQueue) -> ReservationQueue:
        entry.status = QueueEntryStatus.RESERVED
        entry.dequeued_at = timezone.now()
        entry.save(update_fields=["status", "dequeued_at"])
        record_event(
            category=AuditCategory.QUEUE,
            action="Reserved",
            details="",
            correlation_id=str(entry.reservation_id),
        )
        return entry

    @transaction.atomic
    def cancel(self, reservation: AnalysisReservation) -> None:
        try:
            entry = reservation.queue_entry
        except ReservationQueue.DoesNotExist:
            return
        entry.status = QueueEntryStatus.CANCELLED
        entry.dequeued_at = timezone.now()
        entry.save(update_fields=["status", "dequeued_at"])
        record_event(
            category=AuditCategory.QUEUE,
            action="Cancelled",
            details="",
            correlation_id=str(reservation.id),
        )

    @transaction.atomic
    def expire(self, reservation: AnalysisReservation) -> None:
        try:
            entry = reservation.queue_entry
        except ReservationQueue.DoesNotExist:
            return
        entry.status = QueueEntryStatus.EXPIRED
        entry.dequeued_at = timezone.now()
        entry.save(update_fields=["status", "dequeued_at"])
