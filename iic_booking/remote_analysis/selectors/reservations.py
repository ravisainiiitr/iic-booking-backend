"""Reservation / scheduler read selectors."""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from iic_booking.remote_analysis.constants import QueueEntryStatus, ReservationStatus
from iic_booking.remote_analysis.scheduler_models import (
    AnalysisReservation,
    MaintenanceWindow,
    ReservationQueue,
    SchedulerTelemetry,
)


def reservations_queryset(*, department_id: int | None = None) -> QuerySet[AnalysisReservation]:
    qs = AnalysisReservation.objects.select_related(
        "user",
        "department",
        "workstation",
        "booking",
        "software_profile",
        "created_by",
    )
    if department_id is not None:
        qs = qs.filter(Q(department_id=department_id) | Q(department_id__isnull=True))
    return qs


def reservation_by_id(reservation_id) -> AnalysisReservation | None:
    return reservations_queryset().filter(pk=reservation_id).first()


def queue_entries(*, limit: int = 100):
    return (
        ReservationQueue.objects.select_related("reservation", "reservation__user", "reservation__workstation")
        .filter(status__in=[QueueEntryStatus.WAITING, QueueEntryStatus.ALLOCATING])
        .order_by("priority", "enqueued_at")[:limit]
    )


def upcoming_reservations(*, limit: int = 50, department_id: int | None = None):
    now = timezone.now()
    return (
        reservations_queryset(department_id=department_id)
        .filter(
            status__in=[
                ReservationStatus.QUEUED,
                ReservationStatus.RESERVED,
                ReservationStatus.PREPARING,
                ReservationStatus.READY,
            ],
            requested_start__gte=now - timedelta(hours=1),
        )
        .order_by("requested_start")[:limit]
    )


def expired_reservations(*, limit: int = 50, department_id: int | None = None):
    return (
        reservations_queryset(department_id=department_id)
        .filter(status=ReservationStatus.EXPIRED)
        .order_by("-updated_at")[:limit]
    )


def maintenance_windows(*, active_only: bool = True):
    qs = MaintenanceWindow.objects.select_related("workstation", "created_by").order_by("start")
    if active_only:
        qs = qs.filter(active=True)
    return qs


def calendar_events(*, start=None, end=None, department_id: int | None = None):
    qs = reservations_queryset(department_id=department_id).exclude(
        status__in=[ReservationStatus.CANCELLED, ReservationStatus.FAILED]
    )
    if start:
        qs = qs.filter(requested_end__gte=start)
    if end:
        qs = qs.filter(requested_start__lte=end)
    return qs.order_by("requested_start")


def allocation_statistics(*, department_id: int | None = None) -> dict:
    qs = reservations_queryset(department_id=department_id)
    by_status = {row["status"]: row["c"] for row in qs.values("status").annotate(c=Count("id"))}
    day_ago = timezone.now() - timedelta(hours=24)
    return {
        "by_status": by_status,
        "created_24h": qs.filter(created_at__gte=day_ago).count(),
        "allocated_24h": qs.filter(allocated_at__gte=day_ago).count(),
        "expired_24h": qs.filter(status=ReservationStatus.EXPIRED, updated_at__gte=day_ago).count(),
        "cancelled_24h": qs.filter(status=ReservationStatus.CANCELLED, updated_at__gte=day_ago).count(),
        "telemetry_samples_24h": SchedulerTelemetry.objects.filter(recorded_at__gte=day_ago).count(),
    }
