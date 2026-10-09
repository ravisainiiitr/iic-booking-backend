"""
Project Grant recharge mode retired: soft-delete every PENDING Project Grant request (Main Admin delete feature),
releasing any matched cash-book entry. Other statuses and modes are never touched; no emails are sent.
Output is counts and request ids only.
"""

from __future__ import annotations

from typing import Any

from iic_booking.users.models.wallet import WalletRechargeMode, WalletRechargeRequest, WalletRechargeRequestStatus

REASON = "Project grant mode retired — use SRIC wallet recharge"
CONFIRM = "RETIRE"


def pending_project_grant_requests():
    return WalletRechargeRequest.objects.filter(
        recharge_mode=WalletRechargeMode.PROJECT_GRANT,
        status=WalletRechargeRequestStatus.PENDING,
        is_deleted=False,
    ).order_by("pk")


def run(*, apply: bool = False, confirm: str = "") -> dict[str, Any]:
    from iic_booking.users.wallet_recharge_admin_actions import (
        RechargeAdminActionError,
        delete_blocked_reason,
        soft_delete_request,
    )

    selected = list(pending_project_grant_requests())
    report: dict[str, Any] = {
        "mode": "APPLY" if apply else "DRY RUN",
        "selected": len(selected),
        "selected_ids": [r.pk for r in selected],
        "with_cashbook_link_ids": [r.pk for r in selected if (r.cashbook_receipt_no or "").strip()],
        "blocked_ids": [r.pk for r in selected if delete_blocked_reason(r)],
        "untouched_other_pending": WalletRechargeRequest.objects.filter(
            status=WalletRechargeRequestStatus.PENDING, is_deleted=False
        )
        .exclude(recharge_mode=WalletRechargeMode.PROJECT_GRANT)
        .count(),
    }
    if not apply:
        return report
    if confirm != CONFIRM:
        raise ValueError(f"Type {CONFIRM} to apply.")
    deleted, failed = [], []
    for req in selected:
        try:
            soft_delete_request(req, actor=None, reason=REASON, inform_requester=False)
            deleted.append(req.pk)
        except RechargeAdminActionError as exc:
            failed.append({"id": req.pk, "code": exc.code or "BLOCKED"})
    report["deleted"] = len(deleted)
    report["deleted_ids"] = deleted
    report["failed"] = failed
    report["remaining_pending_project_grant"] = pending_project_grant_requests().count()
    return report
