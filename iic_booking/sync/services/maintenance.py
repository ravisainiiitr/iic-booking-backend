"""Portal maintenance jobs: prune heavy sync tables (Milestone 19 / production RC)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.utils import timezone

from iic_booking.sync.constants import (
    alert_expiry_hours,
    monitoring_history_retention_days,
)
from iic_booking.sync.models import (
    AgentHealthSnapshot,
    AgentHeartbeat,
    AgentPerformanceMetric,
    HistoricalMetric,
    SyncLog,
    UpdateHistory,
)
from iic_booking.sync.services.alerts import AlertService
from iic_booking.sync.services.history import HistoryService

logger = logging.getLogger(__name__)

DEFAULT_SYNC_LOG_RETENTION_DAYS = 90
DEFAULT_HEARTBEAT_RETENTION_DAYS = 30
DEFAULT_UPDATE_HISTORY_RETENTION_DAYS = 180


class MaintenanceService:
    def run(
        self,
        *,
        department_id=None,
        dry_run: bool = False,
        sync_log_days: int | None = None,
        heartbeat_days: int | None = None,
        update_history_days: int | None = None,
        monitoring_days: int | None = None,
    ) -> dict[str, Any]:
        now = timezone.now()
        log_cutoff = now - timedelta(days=max(1, sync_log_days or DEFAULT_SYNC_LOG_RETENTION_DAYS))
        hb_cutoff = now - timedelta(days=max(1, heartbeat_days or DEFAULT_HEARTBEAT_RETENTION_DAYS))
        upd_cutoff = now - timedelta(
            days=max(1, update_history_days or DEFAULT_UPDATE_HISTORY_RETENTION_DAYS)
        )
        mon_days = max(1, monitoring_days or monitoring_history_retention_days())

        sync_logs = SyncLog.objects.filter(created_at__lt=log_cutoff)
        heartbeats = AgentHeartbeat.objects.filter(reported_at__lt=hb_cutoff)
        updates = UpdateHistory.objects.filter(started_at__lt=upd_cutoff)
        if department_id:
            sync_logs = sync_logs.filter(sync_agent__department_id=department_id)
            heartbeats = heartbeats.filter(sync_agent__department_id=department_id)
            updates = updates.filter(department_id=department_id)

        counts = {
            "sync_logs": sync_logs.count(),
            "heartbeats": heartbeats.count(),
            "update_history": updates.count(),
        }

        expired_alerts = AlertService().expire_stale()
        pruned_history = 0
        if not dry_run:
            counts["sync_logs_deleted"], _ = sync_logs.delete()
            counts["heartbeats_deleted"], _ = heartbeats.delete()
            counts["update_history_deleted"], _ = updates.delete()
            pruned_history = HistoryService().prune(retention_days=mon_days)
            # Also prune performance metrics older than monitoring retention
            perf_cutoff = now - timedelta(days=mon_days)
            perf_qs = AgentPerformanceMetric.objects.filter(reported_at__lt=perf_cutoff)
            if department_id:
                perf_qs = perf_qs.filter(department_id=department_id)
            counts["performance_metrics_deleted"], _ = perf_qs.delete()
        else:
            counts["sync_logs_deleted"] = counts["sync_logs"]
            counts["heartbeats_deleted"] = counts["heartbeats"]
            counts["update_history_deleted"] = counts["update_history"]
            counts["performance_metrics_deleted"] = AgentPerformanceMetric.objects.filter(
                reported_at__lt=now - timedelta(days=mon_days)
            ).count()

        logger.info(
            "maintenance.run dry_run=%s deleted=%s expired_alerts=%s pruned_history=%s",
            dry_run,
            counts,
            expired_alerts,
            pruned_history,
        )
        return {
            "dry_run": dry_run,
            "counts": counts,
            "expired_alerts": expired_alerts,
            "historical_metrics_pruned": pruned_history,
            "alert_expiry_hours": alert_expiry_hours(),
            "generated_at": now.isoformat(),
        }
