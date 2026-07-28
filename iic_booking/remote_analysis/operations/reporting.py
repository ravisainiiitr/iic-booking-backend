"""Reporting Engine + export helpers (CSV / Excel / PDF)."""

from __future__ import annotations

import csv
import io
import json
import logging
from datetime import timedelta
from pathlib import Path

from django.conf import settings as django_settings
from django.utils import timezone

from iic_booking.remote_analysis.constants import (
    AggregationPeriod,
    AuditCategory,
    ReportFormat,
    ReportStatus,
    ReportType,
)
from iic_booking.remote_analysis.operations.analytics import AnalyticsEngine
from iic_booking.remote_analysis.operations.alerts import AlertEngine
from iic_booking.remote_analysis.operations.capacity import AvailabilityEngine, CapacityPlanner
from iic_booking.remote_analysis.operations.performance import PerformanceMonitor
from iic_booking.remote_analysis.operations.utilization import UtilizationEngine
from iic_booking.remote_analysis.operations_models import AnalysisReport, UsageTrend
from iic_booking.remote_analysis.services.audit import record_event

logger = logging.getLogger(__name__)


class ReportingEngine:
    def build_payload(self, report_type: str, *, period_start=None, period_end=None) -> dict:
        now = timezone.now()
        period_end = period_end or now
        period_start = period_start or (period_end - timedelta(days=1))
        analytics = AnalyticsEngine().analytics_payload(AggregationPeriod.DAILY)
        utilization = UtilizationEngine().summary(AggregationPeriod.DAILY)
        capacity = CapacityPlanner().summary(AggregationPeriod.HOURLY)
        performance = PerformanceMonitor().summary(AggregationPeriod.HOURLY)
        availability = AvailabilityEngine().summary(AggregationPeriod.DAILY)
        alerts = [
            {
                "id": str(a.id),
                "title": a.title,
                "severity": a.severity,
                "status": a.status,
                "created_at": a.created_at.isoformat(),
            }
            for a in AlertEngine().list_alerts(limit=50)
        ]

        base = {
            "report_type": report_type,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "generated_at": now.isoformat(),
            "analytics": analytics,
            "utilization": utilization,
            "capacity": capacity,
            "performance": performance,
            "availability": availability,
            "alerts": alerts,
        }

        if report_type == ReportType.FAILURE_REPORT:
            base["focus"] = "failures"
            base["session_failures"] = analytics["session_analytics"].get("cancellation_rate")
        elif report_type == ReportType.ALERT_REPORT:
            base["focus"] = "alerts"
        elif report_type == ReportType.CAPACITY_REPORT:
            base["focus"] = "capacity"
        elif report_type == ReportType.WORKSPACE_USAGE:
            from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace
            from django.db.models import Sum

            usage = AnalysisWorkspace.objects.aggregate(t=Sum("current_usage_bytes"))
            base["workspace_storage_bytes"] = usage["t"] or 0
        return base

    def generate(
        self,
        report_type: str,
        *,
        fmt: str = ReportFormat.JSON,
        actor=None,
        period_start=None,
        period_end=None,
        parameters: dict | None = None,
    ) -> AnalysisReport:
        report = AnalysisReport.objects.create(
            report_type=report_type,
            format=fmt,
            status=ReportStatus.GENERATING,
            period_start=period_start,
            period_end=period_end,
            parameters=parameters or {},
            generated_by=actor if actor is not None and getattr(actor, "pk", None) else None,
        )
        try:
            payload = self.build_payload(report_type, period_start=period_start, period_end=period_end)
            report.payload = payload
            relpath = ""
            if fmt != ReportFormat.JSON:
                from iic_booking.remote_analysis.operations.exports import ExportService

                relpath = ExportService().export_report(report, payload, fmt)
            report.storage_relpath = relpath
            report.status = ReportStatus.READY
            report.completed_at = timezone.now()
            report.save()
            record_event(
                category=AuditCategory.REPORTING,
                action="ReportGenerated",
                details=f"{report_type}/{fmt}",
                actor=actor if actor is not None and getattr(actor, "is_authenticated", False) else None,
                correlation_id=str(report.id),
            )
        except Exception as exc:
            logger.exception("Report generation failed")
            report.status = ReportStatus.FAILED
            report.error_message = str(exc)[:2000]
            report.completed_at = timezone.now()
            report.save(update_fields=["status", "error_message", "completed_at"])
        return report

    def refresh_trends(self, period: str = AggregationPeriod.DAILY) -> int:
        """Aggregate UsageTrend rows from existing telemetry (no duplicate collection)."""
        from iic_booking.remote_analysis.operations.utilization import _period_bounds

        start, _ = _period_bounds(period)
        analytics = AnalyticsEngine().compute_session_analytics(period)
        util = UtilizationEngine().summary(period)
        count = 0
        mapping = {
            "sessions": analytics.total_sessions,
            "utilization": util.get("average_utilization") or 0,
            "availability": util.get("average_availability") or 0,
            "session_hours": util.get("session_hours") or 0,
            "failures": analytics.cancellation_rate,
        }
        for name, value in mapping.items():
            UsageTrend.objects.update_or_create(
                metric_name=name,
                period=period,
                period_start=start,
                defaults={"value": float(value), "unit": ""},
            )
            count += 1
        return count


class ExportService:
    def _root(self) -> Path:
        media = Path(getattr(django_settings, "MEDIA_ROOT", ".") or ".")
        path = media / "remote_analysis" / "reports"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def export_report(self, report: AnalysisReport, payload: dict, fmt: str) -> str:
        name = f"{report.id}_{report.report_type.lower()}"
        if fmt == ReportFormat.CSV:
            path = self._root() / f"{name}.csv"
            self._write_csv(path, payload)
            return f"remote_analysis/reports/{path.name}"
        if fmt == ReportFormat.EXCEL:
            path = self._root() / f"{name}.xlsx"
            self._write_excel(path, payload)
            return f"remote_analysis/reports/{path.name}"
        if fmt == ReportFormat.PDF:
            path = self._root() / f"{name}.pdf"
            self._write_pdf(path, payload)
            return f"remote_analysis/reports/{path.name}"
        return ""

    def _write_csv(self, path: Path, payload: dict) -> None:
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["section", "key", "value"])
            for section, data in payload.items():
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, (dict, list)):
                            writer.writerow([section, k, json.dumps(v)])
                        else:
                            writer.writerow([section, k, v])
                else:
                    writer.writerow([section, "", json.dumps(data)])

    def _write_excel(self, path: Path, payload: dict) -> None:
        from openpyxl import Workbook

        wb = Workbook()
        ws = wb.active
        ws.title = "Report"
        ws.append(["Section", "Key", "Value"])
        for section, data in payload.items():
            if isinstance(data, dict):
                for k, v in data.items():
                    ws.append([section, k, json.dumps(v) if isinstance(v, (dict, list)) else v])
            else:
                ws.append([section, "", json.dumps(data)])
        wb.save(path)

    def _write_pdf(self, path: Path, payload: dict) -> None:
        from reportlab.lib.pagesizes import letter
        from reportlab.pdfgen import canvas

        c = canvas.Canvas(str(path), pagesize=letter)
        width, height = letter
        y = height - 40
        c.setFont("Helvetica-Bold", 14)
        c.drawString(40, y, f"Remote Analysis Report: {payload.get('report_type', '')}")
        y -= 24
        c.setFont("Helvetica", 9)
        c.drawString(40, y, f"Generated: {payload.get('generated_at', '')}")
        y -= 20
        kpis = (payload.get("analytics") or {}).get("kpis") or {}
        for k, v in list(kpis.items())[:30]:
            if y < 60:
                c.showPage()
                y = height - 40
                c.setFont("Helvetica", 9)
            c.drawString(40, y, f"{k}: {v}")
            y -= 14
        c.save()
