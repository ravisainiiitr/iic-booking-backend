"""Capacity Planner + Availability Engine."""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta

from django.db.models import Avg, Count
from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    AggregationPeriod,
    HEARTBEAT_OFFLINE_SECONDS,
    ReservationStatus,
    SessionStatus,
    WorkstationStatus,
)
from iic_booking.remote_analysis.models import AnalysisWorkstation, WorkstationHeartbeat, WorkstationStateHistory
from iic_booking.remote_analysis.operations.utilization import _period_bounds
from iic_booking.remote_analysis.operations_models import CapacitySnapshot, PeakUsageWindow, WorkstationAvailability
from iic_booking.remote_analysis.scheduler_models import AnalysisReservation
from iic_booking.remote_analysis.session_models import RemoteDesktopSession


class CapacityPlanner:
    def snapshot(self, period: str = AggregationPeriod.HOURLY, *, now=None) -> CapacitySnapshot:
        start, end = _period_bounds(period, now)
        lookback_start = start - timedelta(days=7)

        sessions = RemoteDesktopSession.objects.filter(created_at__gte=lookback_start, created_at__lt=end)
        buckets: dict[str, int] = defaultdict(int)
        for s in sessions.only("connected_at", "created_at"):
            t = s.connected_at or s.created_at
            if t and lookback_start <= t < end:
                key = t.replace(minute=0, second=0, microsecond=0).isoformat()
                buckets[key] += 1

        peak_sessions = max(buckets.values()) if buckets else 0
        PeakUsageWindow.objects.filter(period_start__gte=lookback_start).delete()
        for key, count in sorted(buckets.items(), key=lambda x: -x[1])[:10]:
            ps = timezone.datetime.fromisoformat(key)
            if timezone.is_naive(ps):
                ps = timezone.make_aware(ps)
            PeakUsageWindow.objects.create(
                period_start=ps,
                period_end=ps + timedelta(hours=1),
                concurrent_sessions=count,
                label="peak_hour",
            )

        reservations = AnalysisReservation.objects.filter(
            requested_start__lt=end,
            requested_end__gt=lookback_start,
        ).exclude(status=ReservationStatus.CANCELLED)
        peak_demand = reservations.count()

        total_ws = max(1, AnalysisWorkstation.objects.filter(enabled=True).count())
        active_now = RemoteDesktopSession.objects.filter(
            status__in=[SessionStatus.ACTIVE, SessionStatus.CONNECTED, SessionStatus.IDLE]
        ).count()
        occupancy = round(100.0 * active_now / total_ws, 2)
        unused = round(100.0 - occupancy, 2)

        dept_demand: dict[str, int] = defaultdict(int)
        dow: dict[str, int] = defaultdict(int)
        hod: dict[str, int] = defaultdict(int)
        for r in reservations.select_related("department")[:2000]:
            dept = str(getattr(r.department, "name", None) or r.department_id or "unknown")
            dept_demand[dept] += 1
            if r.requested_start:
                dow[str(r.requested_start.weekday())] += 1
                hod[str(r.requested_start.hour)] += 1

        from django.db.models.functions import TruncDate

        daily_counts = (
            RemoteDesktopSession.objects.filter(created_at__gte=lookback_start, created_at__lt=end)
            .annotate(day=TruncDate("created_at"))
            .values("day")
            .annotate(c=Count("id"))
        )
        vals = [row["c"] for row in daily_counts]
        avg_daily = sum(vals) / len(vals) if vals else 0
        predicted = round(avg_daily * 1.1, 2)  # rule-based headroom, not ML
        overbooked = sum(1 for c in buckets.values() if c > total_ws)

        row, _ = CapacitySnapshot.objects.update_or_create(
            period=period,
            period_start=start,
            defaults={
                "peak_concurrent_sessions": peak_sessions,
                "peak_reservation_demand": peak_demand,
                "average_occupancy_percent": occupancy,
                "unused_capacity_percent": unused,
                "overbooked_periods": overbooked,
                "department_demand": dict(dept_demand),
                "day_of_week_demand": dict(dow),
                "hour_of_day_demand": dict(hod),
                "predicted_capacity_need": predicted,
            },
        )
        return row

    def summary(self, period: str = AggregationPeriod.HOURLY) -> dict:
        row = CapacitySnapshot.objects.filter(period=period).order_by("-period_start").first()
        if not row:
            row = self.snapshot(period)
        return {
            "period": row.period,
            "period_start": row.period_start.isoformat(),
            "peak_concurrent_sessions": row.peak_concurrent_sessions,
            "peak_reservation_demand": row.peak_reservation_demand,
            "average_occupancy_percent": row.average_occupancy_percent,
            "unused_capacity_percent": row.unused_capacity_percent,
            "overbooked_periods": row.overbooked_periods,
            "department_demand": row.department_demand,
            "day_of_week_demand": row.day_of_week_demand,
            "hour_of_day_demand": row.hour_of_day_demand,
            "predicted_capacity_need": row.predicted_capacity_need,
            "peaks": list(
                PeakUsageWindow.objects.order_by("-concurrent_sessions")[:10].values(
                    "period_start", "period_end", "concurrent_sessions", "label"
                )
            ),
        }


class AvailabilityEngine:
    def aggregate(self, period: str = AggregationPeriod.DAILY, *, now=None) -> list[WorkstationAvailability]:
        start, end = _period_bounds(period, now)
        hours = max(0.001, (end - start).total_seconds() / 3600.0)
        rows = []
        for ws in AnalysisWorkstation.objects.all():
            hb_count = WorkstationHeartbeat.objects.filter(
                workstation=ws, received_at__gte=start, received_at__lt=end
            ).count()
            expected = max(1, int(hours * (3600 / max(30, HEARTBEAT_OFFLINE_SECONDS / 3))))
            reliability = round(100.0 * min(1.0, hb_count / expected), 2)

            offline_transitions = WorkstationStateHistory.objects.filter(
                workstation=ws,
                to_status=WorkstationStatus.OFFLINE,
                created_at__gte=start,
                created_at__lt=end,
            ).count()
            recovery_transitions = WorkstationStateHistory.objects.filter(
                workstation=ws,
                from_status=WorkstationStatus.OFFLINE,
                created_at__gte=start,
                created_at__lt=end,
            ).count()

            downtime = offline_transitions * 0.25
            mtbf = round(hours / offline_transitions, 2) if offline_transitions else hours
            mttr = round(downtime / recovery_transitions, 2) if recovery_transitions else 0
            maint = 100.0 if ws.status != WorkstationStatus.MAINTENANCE else 0.0
            operational = max(0.0, round(100.0 - (100.0 * downtime / hours), 2))

            res = AnalysisReservation.objects.filter(workstation=ws, created_at__gte=start, created_at__lt=end)
            r_total = res.count() or 1
            r_ok = res.exclude(status__in=[ReservationStatus.FAILED, ReservationStatus.CANCELLED]).count()

            row, _ = WorkstationAvailability.objects.update_or_create(
                workstation=ws,
                period=period,
                period_start=start,
                defaults={
                    "operational_availability": operational,
                    "maintenance_availability": maint,
                    "unexpected_downtime_hours": round(downtime, 3),
                    "mtbf_hours": mtbf,
                    "mttr_hours": mttr,
                    "heartbeat_reliability": reliability,
                    "reservation_success_rate": round(r_ok / r_total, 4),
                },
            )
            rows.append(row)
        return rows

    def summary(self, period: str = AggregationPeriod.DAILY) -> dict:
        start, _ = _period_bounds(period)
        qs = WorkstationAvailability.objects.filter(period=period, period_start=start)
        if not qs.exists():
            self.aggregate(period)
            qs = WorkstationAvailability.objects.filter(period=period, period_start=start)
        agg = qs.aggregate(
            op=Avg("operational_availability"),
            hb=Avg("heartbeat_reliability"),
            mtbf=Avg("mtbf_hours"),
            mttr=Avg("mttr_hours"),
            res=Avg("reservation_success_rate"),
        )
        return {
            "period": period,
            "period_start": start.isoformat(),
            "operational_availability": agg["op"] or 100,
            "heartbeat_reliability": agg["hb"] or 100,
            "mtbf_hours": agg["mtbf"] or 0,
            "mttr_hours": agg["mttr"] or 0,
            "reservation_success_rate": agg["res"] or 1,
            "by_workstation": list(
                qs.select_related("workstation").values(
                    "workstation__hostname",
                    "operational_availability",
                    "heartbeat_reliability",
                    "mtbf_hours",
                    "mttr_hours",
                )[:100]
            ),
        }
