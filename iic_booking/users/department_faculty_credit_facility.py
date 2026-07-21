"""
Department-based one-time Faculty Credit Facility (controlled negative sub-wallet balance).

Distinct from the institute wallet-recharge temporary credit facility in wallet_credit_facility.py.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any, Optional

from django.db import IntegrityError, transaction
from django.utils import timezone

from .models.department_faculty_credit_facility import (
    DepartmentFacultyCreditFacilitySettings,
    FacultyDepartmentCreditFacility,
    FacultyDepartmentCreditFacilityAuditEvent,
    FacultyDepartmentCreditFacilityAuditLog,
    FacultyDepartmentCreditFacilityStatus,
)
from .models.user_type import UserType
from .models.wallet import SubWallet

logger = logging.getLogger(__name__)

ZERO = Decimal("0.00")


def get_or_create_settings(department_id: int) -> DepartmentFacultyCreditFacilitySettings:
    obj, _ = DepartmentFacultyCreditFacilitySettings.objects.get_or_create(
        department_id=department_id,
        defaults={
            "enabled": False,
            "max_credit_limit": ZERO,
        },
    )
    return obj


def outstanding_credit(balance: Decimal) -> Decimal:
    """Amount still owed against the facility (positive when balance is negative)."""
    bal = Decimal(str(balance))
    if bal >= 0:
        return ZERO
    return (-bal).quantize(Decimal("0.01"))


def remaining_credit(credit_limit: Decimal, balance: Decimal) -> Decimal:
    """Unused portion of the credit line given current balance."""
    limit = Decimal(str(credit_limit))
    rem = (limit - outstanding_credit(balance)).quantize(Decimal("0.01"))
    return rem if rem > 0 else ZERO


def _write_audit(
    *,
    department_id: int,
    event_type: str,
    message: str = "",
    facility: Optional[FacultyDepartmentCreditFacility] = None,
    faculty_user=None,
    actor=None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    FacultyDepartmentCreditFacilityAuditLog.objects.create(
        facility=facility,
        department_id=department_id,
        faculty_user=faculty_user or (facility.user if facility else None),
        actor=actor,
        event_type=event_type,
        message=message or "",
        metadata=metadata or {},
    )


def is_eligible_for_new_facility(
    user,
    department,
    *,
    balance: Optional[Decimal] = None,
    sub: Optional[SubWallet] = None,
) -> bool:
    """
    True if the faculty may start using the department credit facility (no row yet).
    """
    if not user or str(getattr(user, "user_type", "") or "").lower() != UserType.FACULTY:
        return False
    if FacultyDepartmentCreditFacility.objects.filter(
        user_id=user.id, department_id=department.id
    ).exists():
        return False

    settings = get_or_create_settings(department.id)
    if not settings.enabled:
        return False
    if settings.joining_date_cutoff is None:
        return False
    limit = Decimal(str(settings.max_credit_limit or 0))
    if limit <= 0:
        return False

    joining = getattr(user, "joining_date", None)
    if joining is None or joining < settings.joining_date_cutoff:
        return False

    if balance is None:
        if sub is not None:
            sub.refresh_from_db()
            balance = Decimal(str(sub.balance))
        else:
            balance = ZERO
    else:
        balance = Decimal(str(balance))

    if balance > ZERO:
        return False
    return True


def department_faculty_credit_floor(sub: SubWallet) -> Decimal:
    """
    Lowest allowed department sub-wallet balance after debit for this facility.
    Returns 0 when not applicable; otherwise -credit_limit.
    """
    try:
        wallet = sub.wallet
        user = wallet.user
    except Exception:
        return ZERO

    if str(getattr(user, "user_type", "") or "").lower() != UserType.FACULTY:
        return ZERO

    facility = (
        FacultyDepartmentCreditFacility.objects.filter(
            user_id=user.id, department_id=sub.department_id
        )
        .only("status", "credit_limit")
        .first()
    )
    if facility:
        if facility.status == FacultyDepartmentCreditFacilityStatus.CLOSED:
            return ZERO
        return (-Decimal(str(facility.credit_limit))).quantize(Decimal("0.01"))

    if not is_eligible_for_new_facility(user, sub.department, sub=sub):
        return ZERO
    settings = get_or_create_settings(sub.department_id)
    return (-Decimal(str(settings.max_credit_limit))).quantize(Decimal("0.01"))


def _refresh_status_from_balance(
    facility: FacultyDepartmentCreditFacility,
    sub: SubWallet,
    *,
    actor=None,
    source: str = "",
) -> None:
    if facility.status == FacultyDepartmentCreditFacilityStatus.CLOSED:
        return

    sub.refresh_from_db()
    balance = Decimal(str(sub.balance))
    prev_status = facility.status

    if balance >= ZERO:
        facility.status = FacultyDepartmentCreditFacilityStatus.CLOSED
        facility.closed_at = timezone.now()
        facility.save(update_fields=["status", "closed_at", "updated_at"])
        _write_audit(
            department_id=facility.department_id,
            facility=facility,
            faculty_user=facility.user,
            actor=actor,
            event_type=FacultyDepartmentCreditFacilityAuditEvent.CLOSED,
            message="Credit facility permanently closed after outstanding credit recovered.",
            metadata={
                "source": source,
                "balance": str(balance),
                "previous_status": prev_status,
            },
        )
        return

    rem = remaining_credit(facility.credit_limit, balance)
    new_status = (
        FacultyDepartmentCreditFacilityStatus.EXHAUSTED
        if rem <= ZERO
        else FacultyDepartmentCreditFacilityStatus.ACTIVE
    )
    if new_status != prev_status:
        facility.status = new_status
        facility.save(update_fields=["status", "updated_at"])
        _write_audit(
            department_id=facility.department_id,
            facility=facility,
            faculty_user=facility.user,
            actor=actor,
            event_type=FacultyDepartmentCreditFacilityAuditEvent.STATUS_CHANGED,
            message=f"Status changed from {prev_status} to {new_status}.",
            metadata={
                "source": source,
                "balance": str(balance),
                "outstanding": str(outstanding_credit(balance)),
                "remaining": str(rem),
                "previous_status": prev_status,
                "new_status": new_status,
            },
        )


@transaction.atomic
def on_subwallet_debited(sub: SubWallet, amount: Decimal, *, actor=None) -> None:
    """Activate / refresh facility after a successful debit that may use overdraft."""
    try:
        user = sub.wallet.user
    except Exception:
        return
    if str(getattr(user, "user_type", "") or "").lower() != UserType.FACULTY:
        return

    sub.refresh_from_db()
    balance = Decimal(str(sub.balance))
    facility = (
        FacultyDepartmentCreditFacility.objects.select_for_update()
        .filter(user_id=user.id, department_id=sub.department_id)
        .first()
    )

    if facility:
        if facility.status == FacultyDepartmentCreditFacilityStatus.CLOSED:
            return
        _write_audit(
            department_id=facility.department_id,
            facility=facility,
            faculty_user=user,
            actor=actor,
            event_type=FacultyDepartmentCreditFacilityAuditEvent.OUTSTANDING_CHANGED,
            message="Outstanding credit updated after wallet debit.",
            metadata={
                "debit_amount": str(Decimal(str(amount))),
                "balance": str(balance),
                "outstanding": str(outstanding_credit(balance)),
                "remaining": str(remaining_credit(facility.credit_limit, balance)),
            },
        )
        _refresh_status_from_balance(facility, sub, actor=actor, source="debit")
        return

    if balance >= ZERO:
        return

    settings = get_or_create_settings(sub.department_id)
    if not settings.enabled or settings.joining_date_cutoff is None:
        return
    limit = Decimal(str(settings.max_credit_limit or 0))
    if limit <= 0:
        return
    joining = getattr(user, "joining_date", None)
    if joining is None or joining < settings.joining_date_cutoff:
        return

    try:
        with transaction.atomic():
            facility = FacultyDepartmentCreditFacility.objects.create(
                user=user,
                department_id=sub.department_id,
                status=FacultyDepartmentCreditFacilityStatus.ACTIVE,
                credit_limit=limit,
                availed_at=timezone.now(),
            )
    except IntegrityError:
        facility = (
            FacultyDepartmentCreditFacility.objects.select_for_update()
            .filter(user_id=user.id, department_id=sub.department_id)
            .first()
        )
        if not facility:
            return
        if facility.status != FacultyDepartmentCreditFacilityStatus.CLOSED:
            _refresh_status_from_balance(facility, sub, actor=actor, source="debit")
        return
    _write_audit(
        department_id=facility.department_id,
        facility=facility,
        faculty_user=user,
        actor=actor,
        event_type=FacultyDepartmentCreditFacilityAuditEvent.ACTIVATED,
        message="Faculty credit facility activated via controlled negative balance.",
        metadata={
            "credit_limit": str(limit),
            "debit_amount": str(Decimal(str(amount))),
            "balance": str(balance),
            "outstanding": str(outstanding_credit(balance)),
            "remaining": str(remaining_credit(limit, balance)),
        },
    )
    _refresh_status_from_balance(facility, sub, actor=actor, source="activation")


@transaction.atomic
def on_subwallet_credited(sub: SubWallet, amount: Decimal, *, actor=None, source: str = "credit") -> None:
    """Recover outstanding credit on recharge; permanently close when balance ≥ 0."""
    try:
        user = sub.wallet.user
    except Exception:
        return
    if str(getattr(user, "user_type", "") or "").lower() != UserType.FACULTY:
        return

    facility = (
        FacultyDepartmentCreditFacility.objects.select_for_update()
        .filter(user_id=user.id, department_id=sub.department_id)
        .first()
    )
    if not facility or facility.status == FacultyDepartmentCreditFacilityStatus.CLOSED:
        return

    sub.refresh_from_db()
    balance = Decimal(str(sub.balance))
    _write_audit(
        department_id=facility.department_id,
        facility=facility,
        faculty_user=user,
        actor=actor,
        event_type=FacultyDepartmentCreditFacilityAuditEvent.RECHARGE_RECOVERY,
        message="Wallet credit applied against outstanding faculty credit facility.",
        metadata={
            "source": source,
            "credit_amount": str(Decimal(str(amount))),
            "balance": str(balance),
            "outstanding": str(outstanding_credit(balance)),
            "remaining": str(remaining_credit(facility.credit_limit, balance)),
            "status": facility.status,
        },
    )
    _refresh_status_from_balance(facility, sub, actor=actor, source=source)


def serialize_facility_row(
    facility: FacultyDepartmentCreditFacility,
    *,
    balance: Optional[Decimal] = None,
) -> dict[str, Any]:
    if balance is None:
        sw = SubWallet.objects.filter(
            wallet__user_id=facility.user_id, department_id=facility.department_id
        ).first()
        balance = Decimal(str(sw.balance)) if sw else ZERO
    else:
        balance = Decimal(str(balance))

    out = outstanding_credit(balance) if facility.status != FacultyDepartmentCreditFacilityStatus.CLOSED else ZERO
    rem = (
        ZERO
        if facility.status == FacultyDepartmentCreditFacilityStatus.CLOSED
        else remaining_credit(facility.credit_limit, balance)
    )
    return {
        "id": facility.id,
        "faculty_user_id": facility.user_id,
        "faculty_name": facility.user.name or facility.user.email,
        "faculty_email": facility.user.email,
        "joining_date": facility.user.joining_date.isoformat() if facility.user.joining_date else None,
        "department_id": facility.department_id,
        "department_name": facility.department.name if facility.department_id else "",
        "status": facility.status,
        "status_display": facility.get_status_display(),
        "credit_limit": str(Decimal(str(facility.credit_limit)).quantize(Decimal("0.01"))),
        "wallet_balance": str(balance.quantize(Decimal("0.01"))),
        "outstanding_credit": str(out.quantize(Decimal("0.01"))),
        "remaining_credit": str(rem.quantize(Decimal("0.01"))),
        "availed_at": facility.availed_at.isoformat() if facility.availed_at else None,
        "closed_at": facility.closed_at.isoformat() if facility.closed_at else None,
    }


def serialize_available_row(user, department, settings: DepartmentFacultyCreditFacilitySettings, balance: Decimal) -> dict[str, Any]:
    limit = Decimal(str(settings.max_credit_limit or 0))
    return {
        "id": None,
        "faculty_user_id": user.id,
        "faculty_name": user.name or user.email,
        "faculty_email": user.email,
        "joining_date": user.joining_date.isoformat() if user.joining_date else None,
        "department_id": department.id,
        "department_name": department.name,
        "status": "available",
        "status_display": "Available",
        "credit_limit": str(limit.quantize(Decimal("0.01"))),
        "wallet_balance": str(Decimal(str(balance)).quantize(Decimal("0.01"))),
        "outstanding_credit": "0.00",
        "remaining_credit": str(limit.quantize(Decimal("0.01"))),
        "availed_at": None,
        "closed_at": None,
    }


def update_settings(
    *,
    department_id: int,
    enabled: bool,
    joining_date_cutoff,
    max_credit_limit,
    actor=None,
) -> DepartmentFacultyCreditFacilitySettings:
    settings = get_or_create_settings(department_id)
    before = {
        "enabled": settings.enabled,
        "joining_date_cutoff": settings.joining_date_cutoff.isoformat() if settings.joining_date_cutoff else None,
        "max_credit_limit": str(settings.max_credit_limit),
    }
    settings.enabled = bool(enabled)
    settings.joining_date_cutoff = joining_date_cutoff
    settings.max_credit_limit = Decimal(str(max_credit_limit)).quantize(Decimal("0.01"))
    settings.updated_by = actor
    settings.save()
    after = {
        "enabled": settings.enabled,
        "joining_date_cutoff": settings.joining_date_cutoff.isoformat() if settings.joining_date_cutoff else None,
        "max_credit_limit": str(settings.max_credit_limit),
    }
    _write_audit(
        department_id=department_id,
        actor=actor,
        event_type=FacultyDepartmentCreditFacilityAuditEvent.CONFIG_UPDATED,
        message="Department faculty credit facility settings updated.",
        metadata={"before": before, "after": after},
    )
    return settings
