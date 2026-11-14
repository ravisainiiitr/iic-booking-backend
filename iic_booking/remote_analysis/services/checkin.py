"""Reservation check-in — hold workstation until user explicitly starts the desktop."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    AuditCategory,
    MissedCheckinPolicy,
    NotificationType,
    ReservationStatus,
    WorkstationStatus,
)
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation, ReservationEvent
from iic_booking.remote_analysis.services.audit import record_event

logger = logging.getLogger(__name__)


def checkin_minutes_for(reservation: AnalysisReservation) -> int:
    booking = getattr(reservation, "booking", None)
    equipment = getattr(booking, "equipment", None) if booking else None
    minutes = int(getattr(equipment, "analysis_checkin_minutes", None) or 10)
    return max(1, min(minutes, 120))


def missed_policy_for(reservation: AnalysisReservation) -> tuple[str, int]:
    booking = getattr(reservation, "booking", None)
    equipment = getattr(booking, "equipment", None) if booking else None
    policy = getattr(equipment, "analysis_missed_checkin_policy", None) or MissedCheckinPolicy.END_OF_QUEUE
    limit = int(getattr(equipment, "analysis_missed_checkin_limit", None) or 3)
    return str(policy), max(1, limit)


class CheckinService:
    """Two-stage allocation: reserve → await check-in → start desktop."""

    @transaction.atomic
    def open_checkin_window(self, reservation: AnalysisReservation, *, actor=None) -> AnalysisReservation:
        minutes = checkin_minutes_for(reservation)
        now = timezone.now()
        reservation.checkin_expires_at = now + timedelta(minutes=minutes)
        reservation.checkin_notified_at = now
        reservation.save(update_fields=["checkin_expires_at", "checkin_notified_at", "updated_at"])

        from iic_booking.remote_analysis.services.reservation import ReservationService

        ReservationService().transition(
            reservation,
            ReservationStatus.AWAITING_CHECKIN,
            reason=f"Awaiting user check-in ({minutes} min)",
            actor=actor,
        )
        if reservation.workstation_id:
            ws = reservation.workstation
            if ws.status not in {
                WorkstationStatus.MAINTENANCE,
                WorkstationStatus.DISABLED,
                WorkstationStatus.CALIBRATION,
                WorkstationStatus.SOFTWARE_UPDATE,
                WorkstationStatus.HARDWARE_FAULT,
            }:
                from_status = ws.status
                if from_status != WorkstationStatus.RESERVED:
                    from iic_booking.remote_analysis.models import WorkstationStateHistory

                    WorkstationStateHistory.objects.create(
                        workstation=ws,
                        from_status=from_status,
                        to_status=WorkstationStatus.RESERVED,
                        reason="Reserved awaiting user check-in",
                    )
                    ws.status = WorkstationStatus.RESERVED
                    ws.save(update_fields=["status", "updated_at"])

        ReservationEvent.objects.create(
            reservation=reservation,
            event_type="CHECKIN_OPENED",
            details=f"Check-in window {minutes} minutes",
            metadata={"expires_at": reservation.checkin_expires_at.isoformat()},
        )
        from iic_booking.remote_analysis.services import ready_notice

        title, body = ready_notice.ready_message(reservation, minutes)
        self._notify(reservation, title=title, body=body)
        ready_notice.seat_ready(reservation, minutes)
        record_event(
            category=AuditCategory.SCHEDULER,
            action="CheckinOpened",
            details=f"expires={reservation.checkin_expires_at}",
            workstation=reservation.workstation,
            actor=actor,
            correlation_id=str(reservation.id),
        )
        return reservation

    def checkin_payload(self, reservation: AnalysisReservation | None) -> dict[str, Any] | None:
        if reservation is None:
            return None
        if reservation.status != ReservationStatus.AWAITING_CHECKIN:
            return {
                "required": False,
                "status": reservation.status,
            }
        now = timezone.now()
        expires = reservation.checkin_expires_at
        remaining = max(0, int((expires - now).total_seconds())) if expires else 0
        return {
            "required": True,
            "status": reservation.status,
            "title": "Your Analysis Environment is ready.",
            "expires_at": expires.isoformat() if expires else None,
            "remaining_seconds": remaining,
            "workstation_hostname": getattr(reservation.workstation, "hostname", None),
            "actions": ["start_analysis_session", "release_reservation"],
        }

    @transaction.atomic
    def release_checkin(self, reservation: AnalysisReservation, *, actor=None, reason: str = "Released by user") -> dict:
        return self._release(reservation, actor=actor, reason=reason, missed=False)

    @transaction.atomic
    def expire_due(self, *, limit: int = 50) -> dict[str, Any]:
        now = timezone.now()
        # Lock reservation PKs only — select_related on nullable FKs under
        # select_for_update() produces OUTER JOINs that PostgreSQL rejects.
        due_ids = list(
            AnalysisReservation.objects.filter(
                status=ReservationStatus.AWAITING_CHECKIN,
                checkin_expires_at__lte=now,
            )
            .order_by("checkin_expires_at")
            .values_list("pk", flat=True)[:limit]
        )
        if not due_ids:
            return {"expired": 0}
        qs = (
            AnalysisReservation.objects.select_for_update()
            .filter(pk__in=due_ids)
            .order_by("checkin_expires_at")
        )
        expired = 0
        for reservation in qs:
            self._release(reservation, reason="Check-in timer expired", missed=True)
            expired += 1
        if expired:
            try:
                from iic_booking.remote_analysis.services.scheduler import SchedulerService

                SchedulerService().process_queue(limit=50)
            except Exception:
                logger.exception("Queue reprocess after check-in expiry failed")
        return {"expired": expired}

    def _release(
        self,
        reservation: AnalysisReservation,
        *,
        actor=None,
        reason: str,
        missed: bool,
    ) -> dict[str, Any]:
        from iic_booking.remote_analysis.services.reservation import ReservationService
        from iic_booking.remote_analysis.services.scheduler import SchedulerService

        ws = reservation.workstation
        policy, limit = missed_policy_for(reservation)
        if missed:
            reservation.missed_checkin_count = int(reservation.missed_checkin_count or 0) + 1
            reservation.save(update_fields=["missed_checkin_count", "updated_at"])

        # Free workstation (unless the previous user is still on it during the no-show grace)
        from iic_booking.remote_analysis.services.handover import workstation_has_live_session

        if ws is not None and ws.status == WorkstationStatus.RESERVED and workstation_has_live_session(
            ws.pk, exclude_reservation_id=reservation.pk
        ):
            ws.status = WorkstationStatus.BUSY
            ws.save(update_fields=["status", "updated_at"])
        elif ws is not None and ws.status == WorkstationStatus.RESERVED:
            from iic_booking.remote_analysis.models import WorkstationStateHistory

            WorkstationStateHistory.objects.create(
                workstation=ws,
                from_status=ws.status,
                to_status=WorkstationStatus.AVAILABLE,
                reason=reason,
            )
            ws.status = WorkstationStatus.AVAILABLE
            ws.save(update_fields=["status", "updated_at"])

        reservation.workstation = None
        reservation.checkin_expires_at = None
        reservation.save(update_fields=["workstation", "checkin_expires_at", "updated_at"])

        action = "released"
        if not missed:
            # The user gave the PC back: leave the queue instead of rejoining it at the back.
            ReservationService().transition(
                reservation, ReservationStatus.CANCELLED, reason=reason, actor=actor
            )
            SchedulerService().queue.cancel(reservation)
            from iic_booking.remote_analysis.services import ready_notice

            ready_notice.released(reservation)
        elif missed and reservation.missed_checkin_count >= limit:
            # Every policy stops after the limit: otherwise a user who walked away is re-held a PC,
            # and notified, every few minutes for the whole access window.
            ReservationService().transition(
                reservation, ReservationStatus.CANCELLED, reason=f"Missed check-in limit {limit}", actor=actor
            )
            SchedulerService().queue.cancel(reservation)
            action = "cancelled"
            self._notify(
                reservation,
                title="Remote Analysis request closed",
                body=(
                    f"An Analysis PC was held for you {limit} times and the session was not started, so the request "
                    "has left the queue. You can request analysis again from your booking whenever you are ready."
                ),
            )
        elif missed and policy == MissedCheckinPolicy.RETRY_LATER:
            ReservationService().transition(
                reservation, ReservationStatus.QUEUED, reason=reason, actor=actor
            )
            SchedulerService().queue.enqueue(reservation, to_back=True)
            action = "retry_queued"
            self._notify(
                reservation,
                title="Check-in window expired",
                body="Your Analysis PC reservation expired. We will retry allocation shortly.",
            )
        else:
            # Default END_OF_QUEUE
            ReservationService().transition(
                reservation, ReservationStatus.QUEUED, reason=reason, actor=actor
            )
            SchedulerService().queue.enqueue(reservation, to_back=True)
            action = "end_of_queue"
            self._notify(
                reservation,
                title="Check-in window expired",
                body="Your Analysis PC was released. You have been moved to the end of the queue.",
            )

        ReservationEvent.objects.create(
            reservation=reservation,
            event_type="CHECKIN_EXPIRED" if missed else "CHECKIN_RELEASED",
            details=reason,
            metadata={"policy": policy, "missed_count": reservation.missed_checkin_count, "action": action},
        )
        record_event(
            category=AuditCategory.SCHEDULER,
            action="CheckinReleased",
            details=f"{action}: {reason}",
            workstation=ws,
            actor=actor,
            correlation_id=str(reservation.id),
        )
        return {"action": action, "reservation_id": str(reservation.id), "missed": missed}

    def _notify(self, reservation: AnalysisReservation, *, title: str, body: str) -> None:
        try:
            from iic_booking.remote_analysis.notifications import NotificationEngine
            from iic_booking.remote_analysis.services.ready_notice import workspace_link

            NotificationEngine().notify(
                reservation.user,
                NotificationType.RESERVATION_CONFIRMED,
                title,
                body,
                link=workspace_link(reservation),
                metadata={"reservation_id": str(reservation.id)},
            )
        except Exception:
            logger.debug("Check-in notify skipped", exc_info=True)
