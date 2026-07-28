"""Rate limiting for Department Sync control-plane endpoints."""

from __future__ import annotations

from rest_framework.throttling import SimpleRateThrottle

from iic_booking.sync.models import SyncLogCategory, SyncLogSeverity
from iic_booking.sync.services.logging import (
    EVENT_RATE_LIMITED,
    write_sync_log,
)


class SyncEnrollRateThrottle(SimpleRateThrottle):
    """Brute-force mitigation for POST /api/v1/sync/enroll/."""

    scope = "sync_enroll"

    def get_cache_key(self, request, view):
        ident = self.get_ident(request)
        agent_uuid = ""
        try:
            agent_uuid = str((request.data or {}).get("agent_uuid") or "")
        except Exception:
            agent_uuid = ""
        return self.cache_format % {
            "scope": self.scope,
            "ident": f"{ident}:{agent_uuid}",
        }

    def throttle_failure(self):
        write_sync_log(
            event_code=EVENT_RATE_LIMITED,
            message="Rate Limited",
            category=SyncLogCategory.AUTH,
            severity=SyncLogSeverity.WARNING,
            durable=True,
        )
        return super().throttle_failure()
