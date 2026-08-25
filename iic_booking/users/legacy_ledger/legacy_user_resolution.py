"""Phase 10D — legacy user mapping enrichment (non-destructive, emp_id authoritative)."""

from __future__ import annotations

from typing import Any

from django.utils import timezone

from iic_booking.users.models import User
from iic_booking.users.models.portal_migration import (
    LegacyBookingBlock,
    LegacyUserMappingStatus,
)


def lookup_new_portal_user_by_employee_id(employee_id: str | None) -> tuple[User | None, str]:
    """Resolve new portal user by authoritative emp_id only. Never use email/name."""
    emp = (employee_id or "").strip()
    if not emp:
        return None, "missing_employee_id"
    matches = list(User.objects.filter(emp_id=emp).only("id", "emp_id")[:2])
    if len(matches) == 1:
        return matches[0], "resolved"
    if not matches:
        return None, "no_new_portal_user"
    return None, "ambiguous_emp_id"


def classify_user_mapping_for_row(
    *,
    legacy_employee_id: str | None,
    legacy_user_id: int | None = None,
) -> dict[str, Any]:
    """Classify user mapping for discovery display. Does NOT block slot occupancy."""
    emp = (legacy_employee_id or "").strip()
    user, reason = lookup_new_portal_user_by_employee_id(emp)
    if user is not None:
        return {
            "user_mapping_status": LegacyUserMappingStatus.RESOLVED_CHANNEL_I,
            "user_mapping_source": "emp_id",
            "resolved_user_id": user.pk,
            "legacy_employee_id": emp,
            "legacy_user_id": legacy_user_id,
        }
    return {
        "user_mapping_status": LegacyUserMappingStatus.UNRESOLVED,
        "user_mapping_source": reason,
        "resolved_user_id": None,
        "legacy_employee_id": emp,
        "legacy_user_id": legacy_user_id,
    }


def resolve_legacy_blocks_for_channel_i_user(user: User) -> dict[str, Any]:
    """
    Enrich migration metadata when a user logs in via Channel-I.
    Does NOT change slot occupancy, times, equipment, or create Booking rows.
    """
    emp = (getattr(user, "emp_id", None) or "").strip()
    if not emp:
        return {"updated": 0, "reason": "user_has_no_emp_id"}

    qs = LegacyBookingBlock.objects.filter(
        legacy_employee_id=emp,
        user_mapping_status=LegacyUserMappingStatus.UNRESOLVED,
    )
    updated = 0
    now = timezone.now()
    for block in qs.iterator(chunk_size=200):
        block.user_mapping_status = LegacyUserMappingStatus.RESOLVED_CHANNEL_I
        block.user_mapping_source = "channel_i_login"
        block.resolved_user = user
        payload = dict(block.legacy_payload or {})
        payload["user_resolved_at"] = now.isoformat()
        payload["resolved_via"] = "channel_i_login"
        block.legacy_payload = payload
        block.save(
            update_fields=[
                "user_mapping_status",
                "user_mapping_source",
                "resolved_user",
                "legacy_payload",
            ]
        )
        updated += 1
    return {"updated": updated, "employee_id": emp}


def summarize_user_mapping_counts(rows: list[dict]) -> dict[str, int]:
    resolved = 0
    unresolved = 0
    for row in rows:
        st = row.get("user_mapping_status") or LegacyUserMappingStatus.UNRESOLVED
        if st == LegacyUserMappingStatus.RESOLVED_CHANNEL_I:
            resolved += 1
        elif st == LegacyUserMappingStatus.UNRESOLVED:
            unresolved += 1
    return {"resolved": resolved, "unresolved": unresolved, "total": len(rows)}
