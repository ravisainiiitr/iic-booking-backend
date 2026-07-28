"""Performance Monitor — aggregate workstation and portal latencies."""

from __future__ import annotations

from django.db.models import Avg
from django.utils import timezone

from iic_booking.remote_analysis.constants import AggregationPeriod, HEARTBEAT_OFFLINE_SECONDS
from iic_booking.remote_analysis.models import AnalysisWorkstation, TelemetrySnapshot, WorkstationHeartbeat
from iic_booking.remote_analysis.operations.utilization import _period_bounds
from iic_booking.remote_analysis.operations_models import PerformanceMetric
from iic_booking.remote_analysis.session_models import SessionStatistics, SessionTelemetry
from iic_booking.remote_analysis.workspace_models import WorkspaceTelemetry


class PerformanceMonitor:
    def aggregate(self, period: str = AggregationPeriod.HOURLY, *, now=None) -> dict:
        start, end = _period_bounds(period, now)
        metrics: list[dict] = []

        def _store(name: str, value: float, unit: str = "", workstation=None, tags=None):
            PerformanceMetric.objects.create(
                workstation=workstation,
                metric_name=name,
                value=float(value or 0),
                unit=unit,
                period=period,
                period_start=start,
                tags=tags or {},
            )
            metrics.append({"metric_name": name, "value": value or 0, "unit": unit})

        hb = WorkstationHeartbeat.objects.filter(received_at__gte=start, received_at__lt=end)
        _store("cpu_utilization", hb.aggregate(a=Avg("cpu"))["a"] or 0, "%")
        _store("memory_utilization", hb.aggregate(a=Avg("memory"))["a"] or 0, "%")
        _store("disk_usage", hb.aggregate(a=Avg("disk"))["a"] or 0, "%")
        _store("portal_response_latency", hb.aggregate(a=Avg("portal_latency_ms"))["a"] or 0, "ms")

        snaps = TelemetrySnapshot.objects.filter(recorded_at__gte=start, recorded_at__lt=end)
        for name in ("network_latency", "agent_heartbeat_latency"):
            val = snaps.filter(metric_name=name).aggregate(a=Avg("value"))["a"]
            if val is not None:
                _store(name, val, "ms")

        stats = SessionStatistics.objects.filter(session__created_at__gte=start, session__created_at__lt=end)
        _store("remote_desktop_launch_latency", stats.aggregate(a=Avg("launch_latency_ms"))["a"] or 0, "ms")
        _store("preparation_latency", stats.aggregate(a=Avg("prepare_latency_ms"))["a"] or 0, "ms")

        sync = WorkspaceTelemetry.objects.filter(
            recorded_at__gte=start,
            recorded_at__lt=end,
            metric_name__in=["upload_speed_bps", "download_speed_bps"],
        ).aggregate(a=Avg("value"))["a"]
        _store("workspace_sync_throughput", sync or 0, "bps")

        # Per-workstation latest health
        by_ws = []
        for ws in AnalysisWorkstation.objects.all()[:100]:
            last = WorkstationHeartbeat.objects.filter(workstation=ws).order_by("-received_at").first()
            by_ws.append(
                {
                    "workstation_id": str(ws.id),
                    "hostname": ws.hostname,
                    "cpu": last.cpu if last else None,
                    "memory": last.memory if last else None,
                    "disk": last.disk if last else None,
                    "health_score": ws.health_score,
                    "status": ws.status,
                }
            )

        return {
            "period": period,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "metrics": metrics,
            "workstations": by_ws,
        }

    def summary(self, period: str = AggregationPeriod.HOURLY) -> dict:
        start, _ = _period_bounds(period)
        qs = PerformanceMetric.objects.filter(period=period, period_start=start)
        if not qs.exists():
            return self.aggregate(period)
        grouped = {}
        for m in qs.values("metric_name").annotate(avg=Avg("value")):
            grouped[m["metric_name"]] = m["avg"]
        return {"period": period, "period_start": start.isoformat(), "metrics": grouped}
