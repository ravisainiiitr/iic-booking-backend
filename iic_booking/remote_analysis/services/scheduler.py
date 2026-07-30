"""SchedulerService — validate, allocate, expire, process queue."""

from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    DEFAULT_RESERVATION_GRACE_MINUTES,
    DEFAULT_UNUSED_RESERVATION_MINUTES,
    HEARTBEAT_TIMEOUT_FOR_RESERVATION_SECONDS,
    AuditCategory,
    ReservationStatus,
    WorkstationStatus,
)
from iic_booking.remote_analysis.models import AnalysisWorkstation
from iic_booking.remote_analysis.scheduler_models import (
    AnalysisReservation,
    ReservationAudit,
    ReservationEvent,
    SchedulerTelemetry,
)
from iic_booking.remote_analysis.services.allocation import AllocationService
from iic_booking.remote_analysis.services.audit import record_event
from iic_booking.remote_analysis.services.availability import AvailabilityEngine
from iic_booking.remote_analysis.services.conflicts import ConflictResolver
from iic_booking.remote_analysis.services.health import update_workstation_health
from iic_booking.remote_analysis.services.queue import QueueService
from iic_booking.remote_analysis.services.reservation import ReservationService, TERMINAL


class SchedulerService:
    """Intelligent workstation scheduler — allocates only; never launches sessions."""

    def __init__(self):
        self.allocation = AllocationService()
        self.availability = AvailabilityEngine()
        self.conflicts = ConflictResolver()
        self.queue = QueueService()
        self.reservations = ReservationService()

    def _record_metric(self, name: str, value: float, unit: str = "", **tags):
        SchedulerTelemetry.objects.create(metric_name=name, value=value, unit=unit, tags=tags or {})

    @transaction.atomic
    def allocate(self, reservation: AnalysisReservation, *, actor=None) -> AnalysisReservation:
        started = timezone.now()
        self.reservations.transition(
            reservation, ReservationStatus.VALIDATING, reason="Allocation started", actor=actor
        )

        equipment = getattr(reservation.booking, "equipment", None) if reservation.booking_id else None
        catalog_max = 0
        software_name = ""
        if reservation.software_profile_id:
            software_name = reservation.software_profile.software or ""
            catalog = getattr(reservation.software_profile, "catalog_entry", None)
            if catalog is not None:
                catalog_max = int(catalog.max_concurrent or 0)
                software_name = catalog.name or software_name

        caps = dict(reservation.requested_capabilities or {})
        required_software_names = caps.pop("required_software_names", None) or None
        prefer_workstation_id = caps.pop("prefer_workstation_id", None) or None

        candidate = self.allocation.select_best(
            start=reservation.requested_start,
            end=reservation.requested_end,
            department_id=reservation.department_id,
            requirement=reservation.software_profile,
            requested_capabilities={
                **caps,
                "resources": reservation.requested_resources or {},
            },
            user=reservation.user,
            exclude_reservation_id=reservation.id,
            equipment=equipment,
            catalog_max_concurrent=catalog_max,
            software_name=software_name,
            required_software_names=required_software_names,
            prefer_workstation_id=prefer_workstation_id,
        )

        if candidate is None:
            self.reservations.transition(
                reservation, ReservationStatus.QUEUED, reason="No eligible workstation", actor=actor
            )
            self.queue.enqueue(reservation)
            self._record_metric("reservation_failures", 1, tags={"reason": "no_candidate"})
            latency = (timezone.now() - started).total_seconds() * 1000
            self._record_metric("allocation_latency", latency, "ms", success=False)
            return reservation

        # Double-check conflicts
        detected = self.conflicts.detect_for_window(
            candidate.workstation,
            reservation.requested_start,
            reservation.requested_end,
            reservation=reservation,
        )
        if detected:
            self.conflicts.persist_conflicts(reservation, detected)
            self.reservations.transition(
                reservation, ReservationStatus.QUEUED, reason="Conflict during allocation", actor=actor
            )
            self.queue.enqueue(reservation)
            self._record_metric("conflict_count", len(detected))
            return reservation

        reservation.workstation = candidate.workstation
        reservation.reserved_start = reservation.requested_start
        reservation.reserved_end = reservation.requested_end
        reservation.allocated_at = timezone.now()
        reservation.allocation_score = candidate.score
        reservation.allocation_notes = f"Selected {candidate.workstation.hostname} score={candidate.score:.2f}"
        reservation.save(
            update_fields=[
                "workstation",
                "reserved_start",
                "reserved_end",
                "allocated_at",
                "allocation_score",
                "allocation_notes",
                "updated_at",
            ]
        )
        self.reservations.transition(
            reservation,
            ReservationStatus.RESERVED,
            reason=reservation.allocation_notes,
            actor=actor,
        )
        try:
            entry = reservation.queue_entry
            self.queue.mark_reserved(entry)
        except Exception:
            pass

        ReservationAudit.objects.create(
            reservation=reservation,
            action="Allocated",
            details=reservation.allocation_notes,
            actor=actor if getattr(actor, "pk", None) else None,
        )
        ReservationEvent.objects.create(
            reservation=reservation,
            event_type="ALLOCATED",
            details=reservation.allocation_notes,
            metadata=candidate.breakdown,
        )
        record_event(
            category=AuditCategory.SCHEDULER,
            action="Allocated",
            details=reservation.allocation_notes,
            workstation=candidate.workstation,
            actor=actor,
            correlation_id=str(reservation.id),
        )

        # Mark workstation busy for scheduling visibility (no Guacamole session)
        ws = candidate.workstation
        if ws.status in {WorkstationStatus.AVAILABLE, WorkstationStatus.ONLINE}:
            from iic_booking.remote_analysis.models import WorkstationStateHistory

            WorkstationStateHistory.objects.create(
                workstation=ws,
                from_status=ws.status,
                to_status=WorkstationStatus.BUSY,
                reason=f"Reserved {reservation.id}",
                changed_by=actor if getattr(actor, "pk", None) else None,
            )
            ws.status = WorkstationStatus.BUSY
            ws.save(update_fields=["status", "updated_at"])

        latency = (timezone.now() - started).total_seconds() * 1000
        self._record_metric("allocation_latency", latency, "ms", success=True)
        self._record_metric("reservation_success", 1)
        self._record_metric(
            "reservation_latency",
            (timezone.now() - reservation.created_at).total_seconds() * 1000,
            "ms",
        )
        try:
            from iic_booking.remote_analysis.collaboration.hooks import on_reservation_confirmed

            on_reservation_confirmed(reservation)
        except Exception:
            pass
        return reservation

    def process_queue(self, *, limit: int = 20) -> dict:
        processed = allocated = failed = 0
        for entry in self.queue.next_waiting(limit=limit):
            processed += 1
            enqueued_at = entry.enqueued_at
            self.queue.mark_allocating(entry)
            reservation = entry.reservation
            if reservation.status in TERMINAL:
                self.queue.cancel(reservation)
                continue
            wait_ms = (timezone.now() - enqueued_at).total_seconds() * 1000
            self._record_metric("queue_time", wait_ms, "ms")
            before = reservation.status
            self.allocate(reservation)
            reservation.refresh_from_db()
            if reservation.status == ReservationStatus.RESERVED:
                allocated += 1
            elif reservation.status == ReservationStatus.QUEUED:
                # put back to waiting
                entry.status = entry.status  # already reserved or still queued
                from iic_booking.remote_analysis.constants import QueueEntryStatus

                entry.status = QueueEntryStatus.WAITING
                entry.save(update_fields=["status"])
                failed += 1
            else:
                failed += 1
            _ = before
        return {"processed": processed, "allocated": allocated, "failed": failed}

    def expire_stale(self) -> dict:
        now = timezone.now()
        grace = timedelta(minutes=DEFAULT_RESERVATION_GRACE_MINUTES)
        unused = timedelta(minutes=DEFAULT_UNUSED_RESERVATION_MINUTES)
        expired = 0

        # Missed start time
        for reservation in AnalysisReservation.objects.filter(
            status__in=[
                ReservationStatus.RESERVED,
                ReservationStatus.READY,
                ReservationStatus.PREPARING,
            ],
            reserved_start__lt=now - grace,
        ):
            self.reservations.release(
                reservation,
                reason="Missed start time / unused reservation",
                final_status=ReservationStatus.EXPIRED,
            )
            self.queue.expire(reservation)
            self._free_workstation(reservation)
            expired += 1

        # Queued too long past requested start
        for reservation in AnalysisReservation.objects.filter(
            status=ReservationStatus.QUEUED,
            requested_start__lt=now - unused,
        ):
            self.reservations.transition(reservation, ReservationStatus.EXPIRED, reason="Queue expired")
            self.queue.expire(reservation)
            expired += 1
            self._record_metric("reservation_failures", 1, tags={"reason": "queue_expired"})

        # Offline workstation while reserved
        heartbeat_cut = timedelta(seconds=HEARTBEAT_TIMEOUT_FOR_RESERVATION_SECONDS)
        for reservation in AnalysisReservation.objects.filter(
            status__in=[
                ReservationStatus.RESERVED,
                ReservationStatus.PREPARING,
                ReservationStatus.READY,
                ReservationStatus.ACTIVE,
            ],
            workstation__isnull=False,
        ).select_related("workstation"):
            ws = reservation.workstation
            if ws.last_heartbeat is None or (now - ws.last_heartbeat) > heartbeat_cut:
                self.conflicts.persist_conflicts(
                    reservation,
                    self.conflicts.detect_for_window(
                        ws,
                        reservation.reserved_start or reservation.requested_start,
                        reservation.reserved_end or reservation.requested_end,
                        reservation=reservation,
                    ),
                )
                self.reservations.release(
                    reservation,
                    reason="Workstation offline / heartbeat timeout",
                    final_status=ReservationStatus.EXPIRED,
                )
                self.queue.expire(reservation)
                self._free_workstation(reservation)
                expired += 1

        # Natural end
        for reservation in AnalysisReservation.objects.filter(
            status__in=[
                ReservationStatus.RESERVED,
                ReservationStatus.PREPARING,
                ReservationStatus.READY,
                ReservationStatus.ACTIVE,
            ],
            reserved_end__lt=now,
        ):
            self.reservations.release(
                reservation,
                reason="Reservation window ended",
                final_status=ReservationStatus.COMPLETED,
            )
            self._free_workstation(reservation)
            expired += 1

        return {"expired": expired}

    def _free_workstation(self, reservation: AnalysisReservation) -> None:
        ws = reservation.workstation
        if ws is None:
            return
        # Only free if no other active reservation
        others = AnalysisReservation.objects.filter(
            workstation=ws,
            status__in=[
                ReservationStatus.RESERVED,
                ReservationStatus.PREPARING,
                ReservationStatus.READY,
                ReservationStatus.ACTIVE,
            ],
        ).exclude(pk=reservation.pk)
        if others.exists():
            return
        if ws.status == WorkstationStatus.BUSY:
            from iic_booking.remote_analysis.models import WorkstationStateHistory

            WorkstationStateHistory.objects.create(
                workstation=ws,
                from_status=ws.status,
                to_status=WorkstationStatus.AVAILABLE,
                reason=f"Released reservation {reservation.id}",
            )
            ws.status = WorkstationStatus.AVAILABLE
            ws.save(update_fields=["status", "updated_at"])

    def refresh_health(self) -> int:
        from iic_booking.remote_analysis.models import AnalysisWorkstation

        count = 0
        for ws in AnalysisWorkstation.objects.filter(enabled=True):
            update_workstation_health(ws)
            count += 1
        return count

    def utilization_stats(self) -> dict:
        from iic_booking.remote_analysis.models import AnalysisWorkstation

        now = timezone.now()
        day_ago = now - timedelta(hours=24)
        total_ws = AnalysisWorkstation.objects.filter(enabled=True).count()
        busy = (
            AnalysisReservation.objects.filter(
                status__in=[
                    ReservationStatus.RESERVED,
                    ReservationStatus.PREPARING,
                    ReservationStatus.READY,
                    ReservationStatus.ACTIVE,
                ]
            )
            .values("workstation")
            .distinct()
            .count()
        )
        success = SchedulerTelemetry.objects.filter(
            metric_name="reservation_success", recorded_at__gte=day_ago
        ).count()
        failures = SchedulerTelemetry.objects.filter(
            metric_name="reservation_failures", recorded_at__gte=day_ago
        ).count()
        wait_values = list(
            SchedulerTelemetry.objects.filter(metric_name="queue_time", recorded_at__gte=day_ago).values_list(
                "value", flat=True
            )[:500]
        )
        avg_wait_ms = sum(wait_values) / len(wait_values) if wait_values else 0
        utilization = (busy / total_ws * 100.0) if total_ws else 0.0
        self._record_metric("average_utilization", utilization, "%")
        return {
            "total_workstations": total_ws,
            "busy_workstations": busy,
            "average_utilization": round(utilization, 2),
            "reservations_success_24h": success,
            "reservations_failed_24h": failures,
            "average_wait_ms": round(avg_wait_ms, 2),
            "queue_waiting": len(self.queue.next_waiting(limit=1000)),
        }

    def status(self) -> dict:
        from iic_booking.remote_analysis.scheduler_models import MaintenanceWindow

        now = timezone.now()
        return {
            "scheduler": "operational",
            "timestamp": now.isoformat(),
            "queue": self.utilization_stats(),
            "active_reservations": AnalysisReservation.objects.filter(
                status__in=[
                    ReservationStatus.RESERVED,
                    ReservationStatus.PREPARING,
                    ReservationStatus.READY,
                    ReservationStatus.ACTIVE,
                ]
            ).count(),
            "active_maintenance_windows": MaintenanceWindow.objects.filter(
                active=True, start__lte=now, end__gte=now
            ).count(),
            "note": "Scheduler allocates workstations only. Guacamole/browser sessions are future milestones.",
        }
