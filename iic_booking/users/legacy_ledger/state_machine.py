"""Explicit, auditable portal migration phase transitions. No silent next-step."""

from __future__ import annotations

from iic_booking.users.models.portal_migration import (
    PortalMigrationPhase,
    PortalMigrationPhaseTransition,
    PortalMigrationState,
)

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    PortalMigrationPhase.PREPARATION: {PortalMigrationPhase.PARALLEL_OPERATION},
    PortalMigrationPhase.PARALLEL_OPERATION: {PortalMigrationPhase.FINANCIAL_FREEZE},
    PortalMigrationPhase.FINANCIAL_FREEZE: {PortalMigrationPhase.FINAL_SYNC, PortalMigrationPhase.PARALLEL_OPERATION},
    PortalMigrationPhase.FINAL_SYNC: {PortalMigrationPhase.RECONCILIATION, PortalMigrationPhase.FINANCIAL_FREEZE},
    PortalMigrationPhase.RECONCILIATION: {PortalMigrationPhase.NEW_PORTAL_ACTIVE, PortalMigrationPhase.FINAL_SYNC},
    PortalMigrationPhase.NEW_PORTAL_ACTIVE: {
        PortalMigrationPhase.OLD_PORTAL_READ_ONLY,
        PortalMigrationPhase.RECONCILIATION,
    },
    PortalMigrationPhase.OLD_PORTAL_READ_ONLY: {
        PortalMigrationPhase.OLD_PORTAL_REDIRECT,
        PortalMigrationPhase.NEW_PORTAL_ACTIVE,
    },
    PortalMigrationPhase.OLD_PORTAL_REDIRECT: {
        PortalMigrationPhase.ARCHIVED,
        PortalMigrationPhase.OLD_PORTAL_READ_ONLY,
    },
    PortalMigrationPhase.ARCHIVED: set(),
}

# Side effects that MUST remain operator-controlled (documented, not auto-run).
PHASE_OPERATOR_HINTS = {
    PortalMigrationPhase.PARALLEL_OPERATION: (
        "Optional next operator actions (not automatic): disable end-user booking; "
        "enable incremental_sync_enabled."
    ),
    PortalMigrationPhase.FINANCIAL_FREEZE: (
        "Old portal wallet mutations must be stopped by old-portal operators. "
        "This transition does not write to old MySQL."
    ),
    PortalMigrationPhase.NEW_PORTAL_ACTIVE: (
        "Requires zero reconciliation mismatches. Then freeze legacy sync and enable booking — separately."
    ),
    PortalMigrationPhase.OLD_PORTAL_REDIRECT: (
        "DNS/Apache redirect to https://equip.iitr.ac.in requires a separate explicit change."
    ),
}


class IllegalPhaseTransition(Exception):
    pass


class ReconciliationGateFailed(Exception):
    pass


def transition_phase(*, to_phase: str, actor_email: str = "", note: str = "", mismatch_count: int | None = None) -> PortalMigrationState:
    if to_phase not in PortalMigrationPhase.values:
        raise IllegalPhaseTransition(f"Unknown phase {to_phase}")
    state = PortalMigrationState.get_solo()
    current = state.phase
    if to_phase == current:
        return state
    allowed = ALLOWED_TRANSITIONS.get(current, set())
    if to_phase not in allowed:
        raise IllegalPhaseTransition(f"Cannot move {current} -> {to_phase}. Allowed: {sorted(allowed)}")
    if to_phase == PortalMigrationPhase.NEW_PORTAL_ACTIVE:
        if mismatch_count is None:
            raise ReconciliationGateFailed("Pass mismatch_count from the reconciliation engine.")
        if mismatch_count != 0:
            raise ReconciliationGateFailed(
                "NEW_PORTAL_ACTIVE requires zero unresolved financial mismatches."
            )
    PortalMigrationPhaseTransition.objects.create(
        from_phase=current,
        to_phase=to_phase,
        actor_email=actor_email[:255],
        note=note,
    )
    state.phase = to_phase
    state.save(update_fields=["phase", "updated_at"])
    return state
