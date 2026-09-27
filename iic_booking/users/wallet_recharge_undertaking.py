"""Undertaking declarations and project eligibility for offline wallet recharge requests."""

from __future__ import annotations

from .models import Project
from .models.user_type import UserType
from .models.wallet import WalletRechargeMode
from .models.wallet import WalletRechargeRequest

PROJECT_GRANT_UNDERTAKING = (
    "I hereby undertake that the project code selected above is correct to the best of my "
    "knowledge and that sufficient funds are available under the project to meet the "
    "requested recharge amount."
)
CASH_UNDERTAKING_IITR_STUDENT = (
    "I undertake that no project funds are currently available to fund this recharge and that "
    "I have sufficient personal funds to meet the requested recharge amount."
)
CASH_UNDERTAKING_IITR_FACULTY = (
    "I undertake that no project funds are currently available to fund this recharge, or that "
    "I have already availed the applicable temporary credit facility."
)
CASH_UNDERTAKING_DEFAULT = (
    "This option should be used only when no active project grant is available for funding the "
    "requested recharge. Direct Cash Deposit / Bank Transfer should be chosen only in such situations."
)


def undertaking_text(user, recharge_mode: str) -> str:
    """Declaration shown to (and accepted by) ``user`` for ``recharge_mode``."""
    if recharge_mode == WalletRechargeMode.PROJECT_GRANT:
        return PROJECT_GRANT_UNDERTAKING
    user_type = str(getattr(user, "user_type", "") or "").lower()
    if user_type == UserType.STUDENT:
        return CASH_UNDERTAKING_IITR_STUDENT
    if user_type == UserType.FACULTY:
        return CASH_UNDERTAKING_IITR_FACULTY
    return CASH_UNDERTAKING_DEFAULT


def active_project_for_recharge(user, project_id) -> Project | None:
    """The user's own project if it can fund a recharge right now, else ``None``.

    Uses the same "active" rule as project management: ``is_active`` and not past ``end_date``.
    """
    if not project_id:
        return None
    project = Project.objects.filter(id=project_id, faculty=user, is_active=True).first()
    if project is None or project.is_expired:
        return None
    return project


def record_undertaking(recharge_request: WalletRechargeRequest, actor) -> None:
    """Audit-log the accepted declaration with the project snapshot at submission time."""
    from .wallet_recharge_workflow import append_audit_log

    project = recharge_request.project if recharge_request.project_id else None
    append_audit_log(
        recharge_request,
        action="undertaking_accepted",
        from_status=recharge_request.status,
        to_status=recharge_request.status,
        actor=actor,
        message=undertaking_text(recharge_request.user, recharge_request.recharge_mode),
        metadata={
            "recharge_mode": recharge_request.recharge_mode,
            "undertaking_accepted": bool(recharge_request.undertaking_accepted),
            "user_otp_verified": bool(recharge_request.user_otp_verified),
            "amount": str(recharge_request.amount),
            "department_id": recharge_request.department_id,
            "project_id": project.id if project else None,
            "project_code": (project.project_code or "") if project else "",
            "project_name": (project.name or "") if project else "",
            "project_agency": (project.agency or "") if project else "",
        },
    )
