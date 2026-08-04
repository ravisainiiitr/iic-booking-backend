"""Workstation identity helpers — fingerprint normalize, duplicate detection, merge."""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any

from django.db import transaction
from django.db.models import Count, Max, Q
from django.utils import timezone

from iic_booking.remote_analysis.constants import AuditCategory, WorkstationStatus
from iic_booking.remote_analysis.models import AnalysisWorkstation, WorkstationStateHistory
from iic_booking.remote_analysis.services.audit import record_event

logger = logging.getLogger(__name__)


def normalize_fingerprint(value: str | None) -> str:
    return (value or "").strip().lower()


def normalize_hostname(value: str | None) -> str:
    return (value or "").strip().upper()


class WorkstationIdentityService:
    """Detect and merge duplicate Analysis PC registrations."""

    def list_duplicates(self) -> list[dict[str, Any]]:
        groups: dict[str, list[AnalysisWorkstation]] = defaultdict(list)
        qs = AnalysisWorkstation.objects.filter(enabled=True).order_by(
            "hostname", "-last_heartbeat", "-updated_at"
        )
        for ws in qs:
            fp = normalize_fingerprint(ws.machine_fingerprint)
            if fp.startswith("archived:"):
                continue
            guid = normalize_fingerprint(ws.machine_guid)
            host = normalize_hostname(ws.hostname)
            if fp:
                key = f"fp:{fp}"
            elif guid:
                key = f"guid:{guid}"
            elif host:
                key = f"host:{host}"
            else:
                key = f"id:{ws.id}"
            groups[key].append(ws)

        result = []
        for key, rows in groups.items():
            if len(rows) < 2:
                continue
            result.append(
                {
                    "group_key": key,
                    "count": len(rows),
                    "workstations": [self._row_summary(w) for w in rows],
                    "recommended_survivor_id": str(self._pick_survivor(rows).id),
                }
            )
        return sorted(result, key=lambda g: -g["count"])

    def _row_summary(self, ws: AnalysisWorkstation) -> dict[str, Any]:
        return {
            "id": str(ws.id),
            "agent_id": ws.agent_id,
            "hostname": ws.hostname,
            "status": ws.status,
            "enabled": ws.enabled,
            "last_heartbeat": ws.last_heartbeat.isoformat() if ws.last_heartbeat else None,
            "machine_fingerprint": ws.machine_fingerprint,
            "machine_guid": ws.machine_guid,
            "health_score": ws.health_score,
            "agent_version": ws.agent_version,
        }

    def _pick_survivor(self, rows: list[AnalysisWorkstation]) -> AnalysisWorkstation:
        def score(w: AnalysisWorkstation) -> tuple:
            hb = w.last_heartbeat.timestamp() if w.last_heartbeat else 0
            return (
                1 if w.enabled else 0,
                1 if w.status not in {WorkstationStatus.DISABLED, WorkstationStatus.OFFLINE} else 0,
                1 if (w.machine_fingerprint or "").strip() else 0,
                hb,
                w.health_score or 0,
            )

        return sorted(rows, key=score, reverse=True)[0]

    @transaction.atomic
    def merge(
        self,
        *,
        survivor_id,
        duplicate_ids: list,
        actor=None,
        archive: bool = True,
        delete: bool = False,
    ) -> dict[str, Any]:
        survivor = AnalysisWorkstation.objects.select_for_update().get(pk=survivor_id)
        dupes = list(
            AnalysisWorkstation.objects.select_for_update().filter(pk__in=duplicate_ids).exclude(pk=survivor_id)
        )
        if not dupes:
            return {"merged": 0, "survivor_id": str(survivor.id), "detail": "No duplicates to merge"}

        reassigned = self._reassign_foreign_keys(survivor, dupes)
        archived = []
        deleted = []
        for dup in dupes:
            if delete:
                deleted.append(str(dup.id))
                dup.delete()
                continue
            if archive:
                WorkstationStateHistory.objects.create(
                    workstation=dup,
                    from_status=dup.status,
                    to_status=WorkstationStatus.DISABLED,
                    reason=f"Merged into {survivor.id}",
                    changed_by=actor if getattr(actor, "pk", None) else None,
                )
                dup.enabled = False
                dup.status = WorkstationStatus.DISABLED
                # Avoid unique agent_id collision if survivor later takes fingerprint reconnect
                if not str(dup.agent_id).endswith("-archived") and "-archived-" not in str(dup.agent_id):
                    suffix = f"-archived-{str(dup.id)[:8]}"
                    dup.agent_id = (dup.agent_id[: 64 - len(suffix)] + suffix)[:64]
                if dup.machine_fingerprint and not dup.machine_fingerprint.startswith("archived:"):
                    dup.machine_fingerprint = f"archived:{dup.machine_fingerprint}"[:256]
                if dup.hostname and not dup.hostname.startswith("ARCHIVED-"):
                    dup.hostname = f"ARCHIVED-{dup.hostname}"[:255]
                dup.save()
                archived.append(str(dup.id))
            else:
                deleted.append(str(dup.id))
                dup.delete()

        record_event(
            category=AuditCategory.CONFIGURATION,
            action="WorkstationMerge",
            details=f"Merged {len(dupes)} into {survivor.hostname or survivor.agent_id}",
            workstation=survivor,
            actor=actor,
        )
        return {
            "survivor_id": str(survivor.id),
            "merged": len(dupes),
            "reassigned": reassigned,
            "archived": archived,
            "deleted": deleted,
        }

    def auto_merge_hostname_duplicates(self, *, actor=None, archive: bool = True) -> dict[str, Any]:
        """Merge obvious hostname-only duplicates (e.g. multiple RAVI rows)."""
        groups = self.list_duplicates()
        results = []
        for g in groups:
            survivor = g["recommended_survivor_id"]
            others = [w["id"] for w in g["workstations"] if w["id"] != survivor]
            results.append(self.merge(survivor_id=survivor, duplicate_ids=others, actor=actor, archive=archive))
        return {"groups": len(results), "results": results}

    def _reassign_foreign_keys(self, survivor: AnalysisWorkstation, dupes: list[AnalysisWorkstation]) -> dict[str, int]:
        """Point historical FKs at survivor where safe."""
        counts: dict[str, int] = {}
        dupe_ids = [d.id for d in dupes]

        # Late imports avoid circulars
        from iic_booking.remote_analysis.scheduler_models import AnalysisReservation, MaintenanceWindow
        from iic_booking.remote_analysis.session_models import RemoteDesktopSession
        from iic_booking.remote_analysis.tunnel_models import TunnelSession
        from iic_booking.remote_analysis.workspace_models import AnalysisWorkspace
        from iic_booking.remote_analysis.models import (
            InstalledSoftware,
            RemoteCommand,
            WorkstationEvent,
            WorkstationHeartbeat,
        )

        pairs = [
            (AnalysisReservation, "workstation"),
            (RemoteDesktopSession, "workstation"),
            (TunnelSession, "workstation"),
            (AnalysisWorkspace, "workstation"),
            (MaintenanceWindow, "workstation"),
            (RemoteCommand, "workstation"),
            (WorkstationEvent, "workstation"),
            (WorkstationHeartbeat, "workstation"),
        ]
        for model, field in pairs:
            n = model.objects.filter(**{f"{field}_id__in": dupe_ids}).update(**{field: survivor})
            counts[model.__name__] = n

        # Software inventory: keep survivor rows; drop duplicate inventory to avoid unique clashes
        InstalledSoftware.objects.filter(workstation_id__in=dupe_ids).delete()
        counts["InstalledSoftware_deleted"] = 1
        return counts
