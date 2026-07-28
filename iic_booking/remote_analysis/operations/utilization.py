"""Utilization Engine — daily/weekly/monthly workstation utilization."""

from __future__ import annotations

from datetime import timedelta

from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

from iic_booking.remote_analysis.constants import AggregationPeriod, ReservationStatus, SessionStatus, WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation, WorkstationHeartbeat
from iic_booking.remote_analysis.operations_models import WorkstationUtilization
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
from iic_booking.remote_analysis.session_models import RemoteDesktopSession, SessionStatistics


def _period_bounds(period: str, now=None):
    now = now or timezone.now()
    if period == AggregationPeriod.HOURLY:
        start = now.replace(minute=0, second=0, microsecond=0)
        return start, start + timedelta(hours=1)
    if period == AggregationPeriod.DAILY:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)
    if period == AggregationPeriod.WEEKLY:
        start = (now - timedelta(days=now.weekday())).replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=7)
    if period == AggregationPeriod.MONTHLY:
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if start.month == 12:
            end = start.replace(year=start.year + 1, month=1)
        else:
            end = start.replace(month=start.month + 1)
        return start, end
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


class UtilizationEngine:
    def aggregate(self, period: str = AggregationPeriod.DAILY, *, now=None) -> list[WorkstationUtilization]:
        start, end = _period_bounds(period, now)
        hours = max(0.001, (end - start).total_seconds() / 3600.0)
        rows: list[WorkstationUtilization] = []

        for ws in AnalysisWorkstation.objects.all():
            sessions = RemoteDesktopSession.objects.filter(
                workstation=ws,
                created_at__gte=start,
                created_at__lt=end,
            )
            stats = SessionStatistics.objects.filter(session__in=sessions)
            session_hours = (stats.aggregate(s=Sum("duration_seconds"))["s"] or 0) / 3600.0
            idle_hours = (stats.aggregate(s=Sum("idle_seconds"))["s"] or 0) / 3600.0

            reservations = AnalysisReservation.objects.filter(
                workstation=ws,
                requested_start__lt=end,
                requested_end__gt=start,
            ).exclude(status__in=[ReservationStatus.CANCELLED, ReservationStatus.FAILED])
            reservation_hours = 0.0
            for r in reservations:
                rs = max(r.requested_start, start)
                re = min(r.requested_end, end)
                reservation_hours += max(0.0, (re - rs).total_seconds() / 3600.0)

            maint_hours = 0.0
            if ws.status == WorkstationStatus.MAINTENANCE:
                maint_hours = hours * 0.5  # approximate within window

            hb_count = WorkstationHeartbeat.objects.filter(
                workstation=ws, received_at__gte=start, received_at__lt=end
            ).count()
            expected_hb = max(1, int(hours * 120))  # ~30s heartbeats → 120/hr
            uptime_ratio = min(1.0, hb_count / expected_hb) if expected_hb else 0
            uptime_hours = hours * uptime_ratio
            availability = round(100.0 * uptime_ratio, 2)
            utilization = round(100.0 * min(1.0, session_hours / hours), 2)

            row, _ = WorkstationUtilization.objects.update_or_create(
                workstation=ws,
                period=period,
                period_start=start,
                defaults={
                    "period_end": end,
                    "uptime_hours": round(uptime_hours, 3),
                    "session_hours": round(session_hours, 3),
                    "idle_hours": round(idle_hours, 3),
                    "reservation_hours": round(reservation_hours, 3),
                    "maintenance_hours": round(maint_hours, 3),
                    "availability_percent": availability,
                    "utilization_percent": utilization,
                },
            )
            rows.append(row)

        # Fleet-level row (workstation=null) — use a sentinel via first null unique; skip if unique requires workstation
        # Store fleet summary as UsageTrend instead when workstation is required unique with nulls
        return rows

    def summary(self, period: str = AggregationPeriod.DAILY) -> dict:
        start, end = _period_bounds(period)
        qs = WorkstationUtilization.objects.filter(period=period, period_start=start)
        if not qs.exists():
            self.aggregate(period)
            qs = WorkstationUtilization.objects.filter(period=period, period_start=start)
        agg = qs.aggregate(
            avg_util=Avg("utilization_percent"),
            avg_avail=Avg("availability_percent"),
            sum_session=Sum("session_hours"),
            sum_idle=Sum("idle_hours"),
            sum_res=Sum("reservation_hours"),
            sum_maint=Sum("maintenance_hours"),
            sum_uptime=Sum("uptime_hours"),
        )
        return {
            "period": period,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "average_utilization": agg["avg_util"] or 0,
            "average_availability": agg["avg_avail"] or 0,
            "session_hours": agg["sum_session"] or 0,
            "idle_hours": agg["sum_idle"] or 0,
            "reservation_hours": agg["sum_res"] or 0,
            "maintenance_hours": agg["sum_maint"] or 0,
            "uptime_hours": agg["sum_uptime"] or 0,
            "by_workstation": list(
                qs.select_related("workstation").values(
                    "workstation_id",
                    "workstation__hostname",
                    "utilization_percent",
                    "availability_percent",
                    "session_hours",
                    "idle_hours",
                )[:100]
            ),
        }
