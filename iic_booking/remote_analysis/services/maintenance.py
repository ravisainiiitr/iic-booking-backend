"""Enterprise Analysis PC maintenance window orchestration."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from iic_booking.remote_analysis.constants import (
    AuditCategory,
    MaintenanceKind,
    NotificationType,
    QueueEntryStatus,
    ReservationStatus,
    WorkstationStatus,
)
from iic_booking.remote_analysis.models import AnalysisWorkstation, WorkstationStateHistory
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation, MaintenanceWindow, ReservationQueue
from iic_booking.remote_analysis.services.audit import record_event

logger = logging.getLogger(__name__)

KIND_TO_STATUS = {
    MaintenanceKind.MAINTENANCE: WorkstationStatus.MAINTENANCE,
    MaintenanceKind.CALIBRATION: WorkstationStatus.CALIBRATION,
    MaintenanceKind.SOFTWARE_UPDATE: WorkstationStatus.SOFTWARE_UPDATE,
    MaintenanceKind.HARDWARE_FAULT: WorkstationStatus.HARDWARE_FAULT,
    MaintenanceKind.CLEANING: WorkstationStatus.CLEANING,
    MaintenanceKind.OFFLINE: WorkstationStatus.OFFLINE,
    MaintenanceKind.DISABLED: WorkstationStatus.DISABLED,
}

STATUS_LABELS = {
    WorkstationStatus.MAINTENANCE: "Scheduled Maintenance",
    WorkstationStatus.CALIBRATION: "Calibration",
    WorkstationStatus.SOFTWARE_UPDATE: "Software Update",
    WorkstationStatus.HARDWARE_FAULT: "Hardware Fault",
    WorkstationStatus.CLEANING: "Cleaning",
    WorkstationStatus.OFFLINE: "Offline",
    WorkstationStatus.DISABLED: "Disabled",
}


def _parse_dt(value) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value if timezone.is_aware(value) else timezone.make_aware(value)
    parsed = parse_datetime(str(value))
    if parsed is None:
        return None
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)


def friendly_reason_label(kind: str | None = None, status: str | None = None) -> str:
    if kind and kind in dict(MaintenanceKind.choices):
        return dict(MaintenanceKind.choices).get(kind, kind)
    if status:
        return STATUS_LABELS.get(status, status)
    return "Scheduled Maintenance"


def format_availability(dt: datetime | None) -> str:
    if dt is None:
        return "Unknown"
    local = timezone.localtime(dt)
    today = timezone.localtime().date()
    if local.date() == today:
        return f"Today {local.strftime('%I:%M %p').lstrip('0')}"
    if local.date() == today.fromordinal(today.toordinal() + 1):
        return f"Tomorrow {local.strftime('%I:%M %p').lstrip('0')}"
    return local.strftime("%d %b %Y %I:%M %p").lstrip("0")


class MaintenanceService:
    """Schedule, apply, restore, and communicate Analysis PC maintenance."""

    @transaction.atomic
    def schedule(
        self,
        *,
        workstation: AnalysisWorkstation | None = None,
        kind: str = MaintenanceKind.MAINTENANCE,
        start: datetime | str | None = None,
        end: datetime | str | None = None,
        reason: str = "",
        description: str = "",
        assigned_engineer: str = "",
        amc_reference: str = "",
        ticket_number: str = "",
        maintenance_notes: str = "",
        restore_status: str = WorkstationStatus.AVAILABLE,
        actor=None,
        apply_immediately: bool = True,
    ) -> MaintenanceWindow:
        now = timezone.now()
        start_dt = _parse_dt(start) or now
        end_dt = _parse_dt(end)
        if end_dt is None:
            end_dt = start_dt + timedelta(hours=4)
        if end_dt <= start_dt:
            raise ValueError("Maintenance end must be after start")
        if kind not in {c.value for c in MaintenanceKind}:
            kind = MaintenanceKind.MAINTENANCE

        window = MaintenanceWindow.objects.create(
            workstation=workstation,
            kind=kind,
            start=start_dt,
            end=end_dt,
            reason=reason or friendly_reason_label(kind=kind),
            description=description,
            assigned_engineer=assigned_engineer,
            amc_reference=amc_reference,
            ticket_number=ticket_number,
            maintenance_notes=maintenance_notes,
            restore_status=restore_status or WorkstationStatus.AVAILABLE,
            created_by=actor if getattr(actor, "pk", None) else None,
            active=True,
        )
        record_event(
            category=AuditCategory.MAINTENANCE,
            action="Scheduled",
            details=f"{kind}: {window.reason} ticket={ticket_number} end={end_dt.isoformat()}",
            workstation=workstation,
            actor=actor,
        )
        if apply_immediately and start_dt <= now <= end_dt:
            self.apply_window(window, actor=actor)
        return window

    @transaction.atomic
    def apply_window(self, window: MaintenanceWindow, *, actor=None) -> dict[str, Any]:
        target = window.target_status
        applied = 0
        notified = 0
        workstations = []
        if window.workstation_id:
            workstations = [window.workstation]
        else:
            workstations = list(AnalysisWorkstation.objects.filter(enabled=True))

        for ws in workstations:
            if ws.status == target and window.applied_at:
                continue
            if not window.previous_status:
                window.previous_status = ws.status
            WorkstationStateHistory.objects.create(
                workstation=ws,
                from_status=ws.status,
                to_status=target,
                reason=window.reason or friendly_reason_label(kind=window.kind),
                changed_by=actor if getattr(actor, "pk", None) else None,
            )
            ws.status = target
            if window.kind == MaintenanceKind.DISABLED:
                ws.enabled = False
            ws.save(update_fields=["status", "enabled", "updated_at"] if window.kind == MaintenanceKind.DISABLED else ["status", "updated_at"])
            applied += 1
            notified += self._notify_queued_users_for_workstation(
                ws,
                window=window,
                message=(
                    "This Analysis Workstation is currently undergoing "
                    f"{friendly_reason_label(kind=window.kind).lower()}. "
                    "Your request has automatically been reassigned to the next suitable workstation."
                ),
            )
            record_event(
                category=AuditCategory.MAINTENANCE,
                action="Applied",
                details=window.reason or window.kind,
                workstation=ws,
                actor=actor,
            )

        window.applied_at = timezone.now()
        window.save(update_fields=["previous_status", "applied_at", "updated_at"])
        # Kick the queue so waiting bookings recalculate against remaining PCs.
        try:
            from iic_booking.remote_analysis.services.scheduler import SchedulerService

            SchedulerService().process_queue(limit=50)
        except Exception:
            logger.exception("Queue reprocess after maintenance apply failed")
        return {"applied": applied, "notified": notified, "window_id": str(window.id)}

    @transaction.atomic
    def restore_window(self, window: MaintenanceWindow, *, actor=None) -> dict[str, Any]:
        restored = 0
        workstations = []
        if window.workstation_id:
            workstations = [window.workstation]
        else:
            workstations = list(
                AnalysisWorkstation.objects.filter(
                    status__in=list(KIND_TO_STATUS.values()),
                )
            )

        restore_to = window.restore_status or WorkstationStatus.AVAILABLE
        for ws in workstations:
            # Only restore if still in the maintenance-driven status for this kind.
            if ws.status != window.target_status and ws.status not in KIND_TO_STATUS.values():
                continue
            to_status = restore_to
            if window.previous_status and window.previous_status not in {
                WorkstationStatus.MAINTENANCE,
                WorkstationStatus.CALIBRATION,
                WorkstationStatus.SOFTWARE_UPDATE,
                WorkstationStatus.HARDWARE_FAULT,
                WorkstationStatus.DISABLED,
            }:
                # Prefer last operational status when it was Available/Online.
                if window.previous_status in {
                    WorkstationStatus.AVAILABLE,
                    WorkstationStatus.ONLINE,
                }:
                    to_status = window.previous_status
            WorkstationStateHistory.objects.create(
                workstation=ws,
                from_status=ws.status,
                to_status=to_status,
                reason="Maintenance window ended",
                changed_by=actor if getattr(actor, "pk", None) else None,
            )
            ws.status = to_status
            if window.kind == MaintenanceKind.DISABLED:
                ws.enabled = True
                ws.save(update_fields=["status", "enabled", "updated_at"])
            else:
                ws.save(update_fields=["status", "updated_at"])
            restored += 1
            record_event(
                category=AuditCategory.MAINTENANCE,
                action="Restored",
                details=f"Restored to {to_status}",
                workstation=ws,
                actor=actor,
            )

        window.active = False
        window.restored_at = timezone.now()
        window.save(update_fields=["active", "restored_at", "updated_at"])
        try:
            from iic_booking.remote_analysis.services.scheduler import SchedulerService

            SchedulerService().process_queue(limit=50)
        except Exception:
            logger.exception("Queue reprocess after maintenance restore failed")
        return {"restored": restored, "window_id": str(window.id)}

    def monitor(self) -> dict[str, Any]:
        now = timezone.now()
        applied = 0
        restored = 0
        notified = 0

        active_now = MaintenanceWindow.objects.filter(
            active=True, start__lte=now, end__gte=now
        ).select_related("workstation")
        for window in active_now:
            result = self.apply_window(window)
            applied += int(result.get("applied") or 0)
            notified += int(result.get("notified") or 0)

        expired = MaintenanceWindow.objects.filter(active=True, end__lt=now).select_related("workstation")
        for window in expired:
            result = self.restore_window(window)
            restored += int(result.get("restored") or 0)

        return {"applied": applied, "restored": restored, "notified": notified}

    def fleet_dashboard(self, *, department_id: int | None = None) -> dict[str, Any]:
        qs = AnalysisWorkstation.objects.all()
        if department_id is not None:
            qs = qs.filter(Q(department_id=department_id) | Q(department_id__isnull=True))
        total = qs.count()
        by_status = {s.value: qs.filter(status=s.value).count() for s in WorkstationStatus}
        open_windows = (
            MaintenanceWindow.objects.filter(active=True)
            .select_related("workstation")
            .order_by("start")[:50]
        )
        return {
            "total_analysis_pcs": total,
            "available": by_status.get(WorkstationStatus.AVAILABLE, 0)
            + by_status.get(WorkstationStatus.ONLINE, 0),
            "busy": by_status.get(WorkstationStatus.BUSY, 0)
            + by_status.get(WorkstationStatus.PREPARING, 0)
            + by_status.get(WorkstationStatus.RESERVED, 0),
            "maintenance": by_status.get(WorkstationStatus.MAINTENANCE, 0),
            "calibration": by_status.get(WorkstationStatus.CALIBRATION, 0),
            "software_update": by_status.get(WorkstationStatus.SOFTWARE_UPDATE, 0),
            "offline": by_status.get(WorkstationStatus.OFFLINE, 0),
            "faulty": by_status.get(WorkstationStatus.HARDWARE_FAULT, 0)
            + by_status.get(WorkstationStatus.ERROR, 0),
            "cleaning": by_status.get(WorkstationStatus.CLEANING, 0),
            "disabled": by_status.get(WorkstationStatus.DISABLED, 0),
            "by_status": by_status,
            "active_windows": [
                {
                    "id": str(w.id),
                    "workstation_id": str(w.workstation_id) if w.workstation_id else None,
                    "workstation_hostname": getattr(w.workstation, "hostname", None),
                    "kind": w.kind,
                    "reason": w.reason,
                    "start": w.start.isoformat(),
                    "end": w.end.isoformat(),
                    "assigned_engineer": w.assigned_engineer,
                    "ticket_number": w.ticket_number,
                    "amc_reference": w.amc_reference,
                }
                for w in open_windows
            ],
        }

    def next_compatible_availability(
        self,
        *,
        required_software: list[str] | None = None,
        matching_workstation_ids: list | None = None,
    ) -> dict[str, Any]:
        """Estimate when any compatible PC leaves maintenance."""
        now = timezone.now()
        qs = AnalysisWorkstation.objects.filter(enabled=True)
        if matching_workstation_ids is not None:
            qs = qs.filter(id__in=matching_workstation_ids)
        if required_software:
            from iic_booking.remote_analysis.models import InstalledSoftware

            matched = []
            for ws in qs.only("id"):
                if all(
                    InstalledSoftware.objects.filter(
                        workstation_id=ws.id, is_present=True, software_name__icontains=name
                    ).exists()
                    for name in required_software
                ):
                    matched.append(ws.id)
            qs = qs.filter(id__in=matched) if matched else qs.none()

        operational = qs.exclude(
            status__in=[
                WorkstationStatus.MAINTENANCE,
                WorkstationStatus.CALIBRATION,
                WorkstationStatus.SOFTWARE_UPDATE,
                WorkstationStatus.HARDWARE_FAULT,
                WorkstationStatus.OFFLINE,
                WorkstationStatus.DISABLED,
                WorkstationStatus.ERROR,
                WorkstationStatus.CLEANING,
                WorkstationStatus.REGISTERING,
            ]
        )
        if operational.filter(status__in=[WorkstationStatus.AVAILABLE, WorkstationStatus.ONLINE]).exists():
            return {"all_under_maintenance": False, "estimated_available_at": None, "reason": None}

        windows = (
            MaintenanceWindow.objects.filter(active=True, end__gte=now)
            .filter(Q(workstation__in=qs) | Q(workstation__isnull=True))
            .order_by("end")
        )
        soonest = windows.first()
        if soonest is None:
            return {
                "all_under_maintenance": True,
                "estimated_available_at": None,
                "reason": "Scheduled Maintenance",
                "estimated_availability_display": "Unknown",
            }
        return {
            "all_under_maintenance": True,
            "estimated_available_at": soonest.end.isoformat(),
            "estimated_availability_display": format_availability(soonest.end),
            "reason": friendly_reason_label(kind=soonest.kind),
            "window_id": str(soonest.id),
        }

    def _notify_queued_users_for_workstation(
        self,
        workstation: AnalysisWorkstation,
        *,
        window: MaintenanceWindow,
        message: str,
    ) -> int:
        from iic_booking.remote_analysis.notifications import NotificationEngine

        reserved_user_ids = set(
            AnalysisReservation.objects.filter(
                workstation=workstation,
                status__in=[
                    ReservationStatus.REQUESTED,
                    ReservationStatus.QUEUED,
                    ReservationStatus.RESERVED,
                    ReservationStatus.PREPARING,
                ],
            ).values_list("user_id", flat=True)
        )
        waiting_user_ids = set(
            ReservationQueue.objects.filter(status=QueueEntryStatus.WAITING)
            .filter(
                Q(reservation__workstation=workstation)
                | Q(reservation__workstation__isnull=True)
            )
            .values_list("reservation__user_id", flat=True)
        )
        user_ids = {uid for uid in (reserved_user_ids | waiting_user_ids) if uid}
        count = 0
        engine = NotificationEngine()
        for uid in user_ids:
            try:
                from django.contrib.auth import get_user_model

                user = get_user_model().objects.filter(pk=uid).first()
                if not user:
                    continue
                engine.notify(
                    user,
                    NotificationType.MAINTENANCE_SCHEDULED,
                    "Analysis Workstation reassigned",
                    message,
                    metadata={
                        "workstation_id": str(workstation.id),
                        "window_id": str(window.id),
                        "kind": window.kind,
                    },
                )
                count += 1
            except Exception:
                logger.exception("Maintenance notify failed for user %s", uid)
        return count
