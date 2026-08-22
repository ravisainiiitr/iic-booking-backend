"""One-time migration refund / settlement for freeze-mode bookings.

Reuses SubWallet.credit — never mutates wallet balances directly.
Does not free slots, unlock end-user booking, or create new bookings.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from django.db import IntegrityError, transaction
from django.utils import timezone

from iic_booking.equipment.models import Booking, BookingStatus
from iic_booking.equipment.reports import get_equipment_ids_managed_by_oic
from iic_booking.users.models.portal_migration import (
    MigrationBookingSettlement,
    MigrationSettlementStatus,
    MigrationSettlementType,
    PortalMigrationPhase,
    PortalMigrationState,
)
from iic_booking.users.models.user_type import UserType
from iic_booking.users.repositories.wallet_repository import WalletRepository

ALREADY_PROCESSED = "Migration refund already processed."
NOT_AUTHORIZED = "Only Officer-in-Charge (OIC) or Main Administrator may issue a migration refund."
WINDOW_CLOSED = "Migration settlement is only available during old-portal freeze / migration mode."
NON_REFUNDABLE = "This booking is not eligible for a migration refund."
ZERO_AMOUNT = "Refundable amount is zero; migration refund rejected."
NO_WALLET = "User does not have a wallet. Cannot process migration refund."


class MigrationRefundError(Exception):
    """Business-rule failure for migration refund (maps to 4xx)."""

    def __init__(self, message: str, *, code: str = "migration_refund_rejected", http_status: int = 400):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


def migration_settlement_window_open(state: PortalMigrationState | None = None) -> bool:
    """True when OIC/Main Admin may clear pending financial cases."""
    state = state or PortalMigrationState.get_solo()
    if not state.end_user_booking_enabled:
        return True
    if state.legacy_ledger_frozen:
        return True
    return state.phase in {
        PortalMigrationPhase.FINANCIAL_FREEZE,
        PortalMigrationPhase.FINAL_SYNC,
        PortalMigrationPhase.RECONCILIATION,
        PortalMigrationPhase.OLD_PORTAL_READ_ONLY,
    }


def actor_role_label(user) -> str:
    ut = getattr(user, "user_type", None) or ""
    return {
        UserType.ADMIN: "Main Administrator",
        UserType.MANAGER: "Officer-in-Charge",
        UserType.OPERATOR: "Lab-in-Charge",
        UserType.DEPT_ADMIN: "Admin",
        UserType.FACULTY: "Faculty",
        UserType.STUDENT: "Normal/User",
    }.get(ut, ut or "unknown")


def can_issue_migration_refund(user) -> bool:
    """Backend RBAC: OIC (manager) or Main Administrator (admin) only."""
    if not user or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_superuser", False):
        return True
    return getattr(user, "user_type", None) in (UserType.ADMIN, UserType.MANAGER)


def actor_can_access_booking_for_settlement(user, booking: Booking) -> bool:
    if not can_issue_migration_refund(user):
        return False
    if getattr(user, "is_superuser", False) or user.user_type == UserType.ADMIN:
        return True
    # OIC: equipment assignment scope
    eq_id = getattr(booking.equipment, "equipment_id", None) or getattr(booking, "equipment_id", None)
    if eq_id is None:
        return False
    return int(eq_id) in set(get_equipment_ids_managed_by_oic(user.id))


def compute_refundable_amount(booking: Booking) -> Decimal:
    """Derive refundable amount from existing booking financial fields (no arbitrary override)."""
    wallet_applied = Decimal(str(getattr(booking, "wallet_amount_applied", 0) or 0))
    total = Decimal(str(getattr(booking, "total_charge", 0) or 0))
    if wallet_applied > 0:
        return wallet_applied
    return total


def classify_settlement_eligibility(booking: Booking) -> str:
    """Reporting bucket for a booking."""
    completed = MigrationBookingSettlement.objects.filter(
        booking=booking,
        settlement_type=MigrationSettlementType.MIGRATION_REFUND,
        status=MigrationSettlementStatus.COMPLETED,
    ).exists()
    if completed:
        return "already_settled"
    if booking.status in (BookingStatus.REFUNDED, BookingStatus.BOOKING_NOT_UTILIZED):
        return "non_refundable"
    amount = compute_refundable_amount(booking)
    if amount <= 0:
        return "non_refundable"
    failed = MigrationBookingSettlement.objects.filter(
        booking=booking,
        settlement_type=MigrationSettlementType.MIGRATION_REFUND,
        status=MigrationSettlementStatus.FAILED,
    ).exists()
    if failed:
        return "refund_failed"
    return "pending_migration_settlement"


def get_completed_settlement(booking: Booking) -> MigrationBookingSettlement | None:
    return (
        MigrationBookingSettlement.objects.filter(
            booking=booking,
            settlement_type=MigrationSettlementType.MIGRATION_REFUND,
            status=MigrationSettlementStatus.COMPLETED,
        )
        .order_by("-processed_at", "-id")
        .first()
    )


def settlement_payload(settlement: MigrationBookingSettlement | None, booking: Booking | None = None) -> dict[str, Any]:
    booking = booking or (settlement.booking if settlement else None)
    eligibility = classify_settlement_eligibility(booking) if booking else "unknown"
    original = compute_refundable_amount(booking) if booking else Decimal("0")
    base = {
        "settlement_type": MigrationSettlementType.MIGRATION_REFUND,
        "eligibility": eligibility,
        "original_amount": str(original),
        "refundable_amount": str(original if eligibility == "pending_migration_settlement" else "0.00"),
        "currency": "INR",
        "can_issue": eligibility == "pending_migration_settlement",
        "migration_window_open": migration_settlement_window_open(),
    }
    if not settlement:
        return {**base, "status": None, "reference": None, "refund_amount": None}
    return {
        **base,
        "id": settlement.id,
        "status": settlement.status,
        "refund_amount": str(settlement.refund_amount),
        "original_amount": str(settlement.original_amount),
        "reference": settlement.reference or None,
        "reason": settlement.reason,
        "processed_by": getattr(settlement.processed_by, "email", None),
        "processed_by_role": settlement.processed_by_role,
        "processed_at": settlement.processed_at.isoformat() if settlement.processed_at else None,
        "wallet_transaction_id": settlement.wallet_transaction_id,
        "failure_detail": settlement.failure_detail or None,
        "can_issue": False
        if settlement.status == MigrationSettlementStatus.COMPLETED
        else base["can_issue"],
    }


def issue_migration_refund(
    *,
    booking: Booking,
    actor,
    reason: str = "",
    confirm: bool = False,
) -> MigrationBookingSettlement:
    """Issue a one-time migration refund via SubWallet.credit.

    Idempotent for completed settlements (raises ALREADY_PROCESSED).
    Failed attempts stay FAILED and never flip to COMPLETED without a successful credit.
    """
    if not confirm:
        raise MigrationRefundError(
            "Explicit confirmation required. Pass confirm=true to issue the one-time migration refund.",
            code="confirmation_required",
        )
    if not can_issue_migration_refund(actor):
        raise MigrationRefundError(NOT_AUTHORIZED, code="forbidden", http_status=403)
    if not actor_can_access_booking_for_settlement(actor, booking):
        raise MigrationRefundError(
            "Booking is outside your operational settlement scope.",
            code="forbidden_scope",
            http_status=403,
        )
    if not migration_settlement_window_open():
        raise MigrationRefundError(WINDOW_CLOSED, code="window_closed")

    existing = get_completed_settlement(booking)
    if existing:
        raise MigrationRefundError(ALREADY_PROCESSED, code="already_processed", http_status=409)

    if booking.status in (BookingStatus.REFUNDED, BookingStatus.BOOKING_NOT_UTILIZED):
        raise MigrationRefundError(NON_REFUNDABLE, code="non_refundable")

    amount = compute_refundable_amount(booking)
    if amount <= 0:
        raise MigrationRefundError(ZERO_AMOUNT, code="zero_amount")

    refund_target, _ = WalletRepository.get_booking_wallet_target(
        booking.user, getattr(booking.equipment, "internal_department", None)
    )
    if not refund_target:
        raise MigrationRefundError(NO_WALLET, code="no_wallet")

    role = actor_role_label(actor)
    reason_clean = (reason or "").strip()

    # Create PENDING row first (audit of attempt); complete only after successful credit.
    settlement = MigrationBookingSettlement.objects.create(
        booking=booking,
        legacy_booking_id=booking.booking_id,
        user=booking.user,
        settlement_type=MigrationSettlementType.MIGRATION_REFUND,
        original_amount=amount,
        refund_amount=amount,
        currency="INR",
        reason=reason_clean,
        status=MigrationSettlementStatus.PENDING,
        processed_by=actor,
        processed_by_role=role,
    )

    try:
        with transaction.atomic():
            # Re-check under lock for races
            if (
                MigrationBookingSettlement.objects.select_for_update()
                .filter(
                    booking=booking,
                    settlement_type=MigrationSettlementType.MIGRATION_REFUND,
                    status=MigrationSettlementStatus.COMPLETED,
                )
                .exclude(pk=settlement.pk)
                .exists()
            ):
                raise MigrationRefundError(ALREADY_PROCESSED, code="already_processed", http_status=409)

            description = (
                f"Migration refund (MIGRATION_REFUND) for Booking #{booking.booking_id} "
                f"- {getattr(booking.equipment, 'code', '')} | Settlement:{settlement.id}"
            )
            if reason_clean:
                description += f" - {reason_clean}"

            txn = refund_target.credit(
                amount=amount,
                description=description,
                related_user=booking.user,
            )
            settlement.wallet_transaction = txn
            settlement.reference = f"MIG-REF-{booking.booking_id}-{settlement.id}"
            settlement.status = MigrationSettlementStatus.COMPLETED
            settlement.processed_at = timezone.now()
            settlement.failure_detail = ""
            settlement.save(
                update_fields=[
                    "wallet_transaction",
                    "reference",
                    "status",
                    "processed_at",
                    "failure_detail",
                    "updated_at",
                ]
            )
    except MigrationRefundError:
        MigrationBookingSettlement.objects.filter(pk=settlement.pk).update(
            status=MigrationSettlementStatus.FAILED,
            failure_detail=ALREADY_PROCESSED,
            updated_at=timezone.now(),
        )
        raise
    except IntegrityError:
        MigrationBookingSettlement.objects.filter(pk=settlement.pk).update(
            status=MigrationSettlementStatus.FAILED,
            failure_detail=ALREADY_PROCESSED,
            updated_at=timezone.now(),
        )
        raise MigrationRefundError(ALREADY_PROCESSED, code="already_processed", http_status=409) from None
    except Exception as exc:
        MigrationBookingSettlement.objects.filter(pk=settlement.pk).update(
            status=MigrationSettlementStatus.FAILED,
            failure_detail=str(exc)[:2000],
            updated_at=timezone.now(),
        )
        raise MigrationRefundError(
            f"Migration refund failed: {exc}",
            code="refund_failed",
            http_status=500,
        ) from exc

    settlement.refresh_from_db()
    # Safety: never flip freeze / never free slots (no booking.status change here).
    return settlement


def scoped_bookings_queryset(user):
    """Bookings visible for settlement reporting."""
    qs = Booking.objects.select_related("user", "equipment", "equipment__internal_department")
    if getattr(user, "is_superuser", False) or user.user_type == UserType.ADMIN:
        return qs
    if user.user_type == UserType.MANAGER:
        eq_ids = get_equipment_ids_managed_by_oic(user.id)
        return qs.filter(equipment_id__in=eq_ids)
    return qs.none()
