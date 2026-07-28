"""Experiment session operational APIs (Milestone 18)."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

from django.db.models import Avg, Count
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from iic_booking.sync.models import (
    ExperimentSession,
    ExperimentSessionStatus,
    ExperimentTelemetrySnapshot,
    InstrumentPluginCatalog,
    SyncLogCategory,
    SyncLogSeverity,
)
from iic_booking.sync.services.logging import write_sync_log


def _parse_ts(raw):
    if raw is None:
        return None
    if hasattr(raw, "isoformat"):
        return raw
    parsed = parse_datetime(str(raw))
    return parsed


class ExperimentService:
    def list_sessions(
        self,
        *,
        department_id=None,
        agent_id=None,
        status: str | None = None,
        plugin_id: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        qs = ExperimentSession.objects.select_related("sync_agent", "department", "equipment")
        if department_id:
            qs = qs.filter(department_id=department_id)
        if agent_id:
            qs = qs.filter(sync_agent_id=agent_id)
        if status:
            qs = qs.filter(status=status)
        if plugin_id:
            qs = qs.filter(plugin_id=plugin_id)
        return [self.serialize(s) for s in qs.order_by("-created_at")[: max(1, min(limit, 500))]]

    def get(self, experiment_id) -> dict[str, Any] | None:
        row = ExperimentSession.objects.filter(experiment_id=experiment_id).first()
        if row is None:
            row = ExperimentSession.objects.filter(pk=experiment_id).first()
        return self.serialize(row) if row else None

    def upsert_from_agent(self, sync_agent, payload: dict[str, Any], *, correlation_id=None) -> dict[str, Any]:
        exp_id = payload.get("experiment_id") or uuid.uuid4()
        if isinstance(exp_id, str):
            exp_id = uuid.UUID(exp_id)
        corr = correlation_id or payload.get("correlation_id") or uuid.uuid4()
        if isinstance(corr, str):
            try:
                corr = uuid.UUID(corr)
            except ValueError:
                corr = uuid.uuid4()

        status = (payload.get("status") or ExperimentSessionStatus.SCHEDULED).upper()
        session, created = ExperimentSession.objects.update_or_create(
            experiment_id=exp_id,
            defaults={
                "sync_agent": sync_agent,
                "department": getattr(sync_agent, "department", None),
                "equipment_id": payload.get("equipment_id"),
                "booking_id": str(payload.get("booking_id") or payload.get("portal_booking_id") or "")[:64],
                "workspace_path": (payload.get("workspace_path") or "")[:1000],
                "operator_name": (payload.get("operator_name") or "")[:200],
                "plugin_id": (payload.get("plugin_id") or "unknown")[:128],
                "plugin_version": (payload.get("plugin_version") or "")[:64],
                "status": status,
                "current_step": (payload.get("current_step") or "")[:200],
                "session_start": _parse_ts(payload.get("session_start")),
                "session_end": _parse_ts(payload.get("session_end")),
                "metadata": payload.get("metadata") or {},
                "execution_history": payload.get("execution_history") or [],
                "correlation_id": corr,
                "last_error": (payload.get("last_error") or "")[:1000],
                "duration_ms": payload.get("duration_ms"),
            },
        )
        write_sync_log(
            event_code="EXP-UPSERT",
            category=SyncLogCategory.EXPERIMENTS,
            severity=SyncLogSeverity.INFO,
            message=f"Experiment {'created' if created else 'updated'}: {status}",
            sync_agent=sync_agent,
            correlation_id=corr,
            json_payload={"experiment_id": str(exp_id), "plugin_id": session.plugin_id, "status": status},
        )
        return self.serialize(session)

    def record_telemetry(self, sync_agent, payload: dict[str, Any]) -> dict[str, Any]:
        snap = ExperimentTelemetrySnapshot.objects.create(
            sync_agent=sync_agent,
            department=getattr(sync_agent, "department", None),
            reported_at=_parse_ts(payload.get("reported_at")) or timezone.now(),
            experiments_completed=int(payload.get("experiments_completed") or 0),
            experiments_failed=int(payload.get("experiments_failed") or 0),
            recovery_count=int(payload.get("recovery_count") or 0),
            total_duration_ms=float(payload.get("total_duration_ms") or 0),
            total_plugin_execution_ms=float(payload.get("total_plugin_execution_ms") or 0),
            instrument_availability=payload.get("instrument_availability") or {},
            plugin_versions=payload.get("plugin_versions") or {},
            details=payload.get("details") or {},
        )
        # Refresh catalog versions from agent inventory.
        for plugin_id, version in (payload.get("plugin_versions") or {}).items():
            InstrumentPluginCatalog.objects.filter(plugin_id=plugin_id).update(version=str(version)[:64])
        return {"id": snap.id, "reported_at": snap.reported_at.isoformat()}

    def summary(self, *, department_id=None) -> dict[str, Any]:
        qs = ExperimentSession.objects.all()
        if department_id:
            qs = qs.filter(department_id=department_id)
        by_status = {r["status"]: r["c"] for r in qs.values("status").annotate(c=Count("id"))}
        since = timezone.now() - timedelta(days=7)
        recent = qs.filter(created_at__gte=since)
        avg_duration = recent.exclude(duration_ms__isnull=True).aggregate(avg=Avg("duration_ms"))
        return {
            "by_status": by_status,
            "total": qs.count(),
            "last_7_days": recent.count(),
            "avg_duration_ms": avg_duration.get("avg"),
            "failed_7d": recent.filter(status=ExperimentSessionStatus.FAILED).count(),
            "generated_at": timezone.now().isoformat(),
        }

    @staticmethod
    def serialize(session: ExperimentSession | None) -> dict[str, Any] | None:
        if session is None:
            return None
        return {
            "id": str(session.id),
            "experiment_id": str(session.experiment_id),
            "agent_id": str(session.sync_agent_id) if session.sync_agent_id else None,
            "department_id": str(session.department_id) if session.department_id else None,
            "equipment_id": str(session.equipment_id) if session.equipment_id else None,
            "booking_id": session.booking_id,
            "workspace_path": session.workspace_path,
            "operator_name": session.operator_name,
            "plugin_id": session.plugin_id,
            "plugin_version": session.plugin_version,
            "status": session.status,
            "current_step": session.current_step,
            "session_start": session.session_start.isoformat() if session.session_start else None,
            "session_end": session.session_end.isoformat() if session.session_end else None,
            "metadata": session.metadata or {},
            "execution_history": session.execution_history or [],
            "correlation_id": str(session.correlation_id) if session.correlation_id else None,
            "last_error": session.last_error,
            "duration_ms": session.duration_ms,
            "created_at": session.created_at.isoformat() if session.created_at else None,
            "updated_at": session.updated_at.isoformat() if session.updated_at else None,
        }


class InstrumentCatalogService:
    DEFAULT_PLUGINS = [
        ("mass-spectrometer", "Mass Spectrometer", "MassSpectrometer", True, True, True, True),
        ("sem", "Scanning Electron Microscope", "SEM", True, False, True, True),
        ("tem", "Transmission Electron Microscope", "TEM", True, False, True, True),
        ("xrd", "X-Ray Diffraction", "XRD", False, True, True, False),
        ("gc-ms", "GC-MS", "GC-MS", True, False, True, True),
        ("hplc", "HPLC", "HPLC", True, False, True, True),
        ("pcr", "PCR", "PCR", True, False, True, True),
        ("custom-research", "Custom Research Equipment", "Custom", False, True, True, False),
    ]

    def ensure_defaults(self) -> int:
        created = 0
        for plugin_id, name, itype, live, disco, export, diag in self.DEFAULT_PLUGINS:
            _, was_created = InstrumentPluginCatalog.objects.get_or_create(
                plugin_id=plugin_id,
                defaults={
                    "display_name": name,
                    "instrument_type": itype,
                    "version": "1.0.0",
                    "description": f"{name} instrument plugin",
                    "capabilities": {
                        "supports_live_status": live,
                        "supports_auto_discovery": disco,
                        "supports_file_export": export,
                        "supports_remote_trigger": False,
                        "supports_diagnostics": diag,
                        "supports_remote_shutdown": False,
                        "supports_health_monitoring": live,
                    },
                    "supported_task_types": [
                        f"instrument.{plugin_id}",
                        f"instrument.{plugin_id}.prepare",
                        f"instrument.{plugin_id}.start",
                        f"instrument.{plugin_id}.complete",
                    ],
                    "is_active": True,
                },
            )
            if was_created:
                created += 1
        return created

    def list_plugins(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        self.ensure_defaults()
        qs = InstrumentPluginCatalog.objects.all()
        if active_only:
            qs = qs.filter(is_active=True)
        return [
            {
                "id": str(p.id),
                "plugin_id": p.plugin_id,
                "display_name": p.display_name,
                "instrument_type": p.instrument_type,
                "version": p.version,
                "description": p.description,
                "capabilities": p.capabilities or {},
                "supported_task_types": p.supported_task_types or [],
                "is_active": p.is_active,
            }
            for p in qs.order_by("display_name")
        ]

    def list_instruments(self, *, department_id=None) -> list[dict[str, Any]]:
        """Operational instrument view: equipment sync profiles + plugin type hints."""
        from iic_booking.sync.models import EquipmentSyncProfile
        from iic_booking.sync.services.agent_registry import AgentRegistryService

        profiles = EquipmentSyncProfile.objects.select_related("equipment", "primary_agent", "building")
        if department_id:
            profiles = profiles.filter(equipment__internal_department_id=department_id)
        agents = {str(a.id): a for a in AgentRegistryService().scoped_agents(department_id=department_id)}
        rows = []
        for profile in profiles[:500]:
            eq = profile.equipment
            rows.append(
                {
                    "equipment_id": str(eq.id) if eq else None,
                    "equipment_name": getattr(eq, "name", None) or getattr(eq, "equipment_code", ""),
                    "hostname": profile.hostname,
                    "building_id": str(profile.building_id) if profile.building_id else None,
                    "primary_agent_id": str(profile.primary_agent_id) if profile.primary_agent_id else None,
                    "watch_folder": getattr(profile, "watch_folder", "") or "",
                    "online": bool(
                        profile.primary_agent_id
                        and str(profile.primary_agent_id) in agents
                        and agents[str(profile.primary_agent_id)].last_heartbeat_at
                    ),
                }
            )
        return rows
