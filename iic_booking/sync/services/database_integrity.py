"""Portal-side integrity report acceptance (Milestone 13)."""

from __future__ import annotations

import uuid
from typing import Any

from django.utils import timezone

from iic_booking.sync.models import DepartmentSyncAgent
from iic_booking.sync.models import SyncLogSeverity
from iic_booking.sync.services.logging import write_sync_log


class DatabaseIntegrityService:
    """Accepts agent SQLite integrity reports; portal DB repair is out of band."""

    def accept_report(
        self,
        sync_agent: DepartmentSyncAgent,
        payload: dict[str, Any],
        *,
        correlation_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        ok = bool(payload.get("ok", True))
        failures = payload.get("failures") or []
        write_sync_log(
            event_code="REC-4001",
            message="Database integrity report received",
            category="recovery",
            severity=SyncLogSeverity.WARNING if not ok else SyncLogSeverity.INFO,
            sync_agent=sync_agent,
            correlation_id=correlation_id,
            json_payload={
                "ok": ok,
                "failure_count": len(failures) if isinstance(failures, list) else 0,
                "pragma": payload.get("pragma_result"),
            },
        )
        return {
            "accepted": True,
            "ok": ok,
            "accepted_at": timezone.now().isoformat(),
            "guidance": "restore_from_backup" if not ok else "continue",
        }
